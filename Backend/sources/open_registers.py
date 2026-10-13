"""Registers that regulators publish whole, as a file anyone can download.

Most connectors search a regulator's website one molecule at a time. Italy and
Brazil instead publish their entire register as a CSV:

    AIFA Italy     https://drive.aifa.gov.it/farmaci/confezioni.csv
                   every authorised pack, ~37 MB, refreshed daily
    ANVISA Brazil  https://dados.anvisa.gov.br/dados/DADOS_ABERTOS_MEDICAMENTOS.csv
                   every registration, active or not, ~8 MB, refreshed monthly

So the file is downloaded once, indexed into a small local SQLite file, and
searched there; the regulator is not contacted again until the copy is stale.
Nothing is scraped, so there is no page layout to break -- which is why these
two were the cheapest large markets to add.

The index lives in data/open_registers/, which is git-ignored: it is a cache of
the regulator's file, not data this project curates, and deleting it only
means the next search downloads the register again.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import certifi
import requests

from core.logging_config import get_logger
from sources.parser import extract_strength


logger = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
# The indexes are a cache of regulators' files, several gigabytes together. A
# deployment points this at a mounted disk so a new release does not refetch
# every register.
INDEX_DIR = Path(os.getenv("PHARMASEARCH_REGISTER_DIR") or (BASE_DIR / "data" / "open_registers"))
CERT_DIR = Path(__file__).resolve().parent / "certs"

# A bound, not a sample: the largest molecule in either register (paracetamol,
# 766 Brazilian registrations) sits well inside it.
MAX_RESULTS = 2000
DOWNLOAD_TIMEOUT = (30, 180)  # connect, read

# csv refuses fields over 131 KB by default; the registers stay far below it,
# but a malformed line should fail one row, not the whole index.
csv.field_size_limit(10 * 1024 * 1024)


class RegisterNotReady(RuntimeError):
    """The register has not been downloaded yet; a download has been started."""


# --------------------------------------------------------------------------
# Matching a molecule written in English against Italian and Portuguese INNs
# --------------------------------------------------------------------------

_JOINING_WORDS = {"de", "di", "da", "do", "del", "della", "e", "and", "of", "con", "com", "per"}

# Salts, hydrates and esters in English, Italian and Portuguese. They are
# ignored in what a user types, so "metformin hydrochloride" finds Italy's
# METFORMINA CLORIDRATO and Brazil's cloridrato de metformina alike, while the
# register's own text keeps them.
_SALT_WORDS_RAW = """
    hydrochloride hcl sodium potassium calcium magnesium sulfate sulphate phosphate
    maleate besylate besilate tartrate fumarate succinate acetate citrate mesylate
    mesilate hydrate monohydrate dihydrate trihydrate hemihydrate anhydrous bromide
    hydrobromide dipropionate propionate valerate
    cloridrato idrocloruro sodico sodica sodio potassico potassica potassio calcico
    calcica calcio magnesio solfato fosfato maleato besilato tartrato fumarato
    succinato acetato citrato mesilato idrato monoidrato diidrato triidrato
    emiidrato anidro bromidrato dipropionato propionato valerato
    sulfato sodica potassica hidratada hidratado anidra anidro diidratado
    tri-hidratada tri-hidratado
    chloride cloruro cloreto iodide ioduro iodeto carbonate carbonato oxide ossido
    oxido nitrate nitrato hydroxide idrossido hidroxido
"""


def _fold(text: object) -> str:
    """Lower case, with accents and anything but letters and digits removed."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", plain.lower())


# The version of the matching rules (_stem and what feeds it). An index keeps
# the version it was built under; one built under another is rebuilt.
MATCH_VERSION = 3


def _stem(word: str) -> str:
    """One INN word reduced to what English, Italian and Portuguese share.

    The three languages spell the same molecule differently in a handful of
    regular ways, and each rule below undoes one of them on both sides:

        levothyroxine / levotiroxina      ph th y k  ->  f t i c
        amoxicillin   / amoxicilina       doubled letters collapse
        amlodipine    / anlodipino        Portuguese writes m as n before a
        simvastatin   / sinvastatina      consonant other than b or p
        metformin     / metformina        a final e, a or o is dropped
        paracetamol   / paracetamolo
        hydrochlorothiazide / idroclorotiazide / hidroclorotiazida
                                      ch -> c; Italian idro- is hydro-

    Changing a rule changes every index's match text: raise MATCH_VERSION
    with it, so indexes built under the old rules are rebuilt.
    """
    word = word.replace("ph", "f").replace("th", "t").replace("y", "i").replace("k", "c")
    word = word.replace("ch", "c")
    if word.startswith("idro"):
        word = "h" + word
    word = re.sub(r"([a-z])\1+", r"\1", word)
    word = re.sub(r"m(?=[^aeioubpm])", "n", word)
    if len(word) > 4:
        word = re.sub(r"[aeo]$", "", word)
    return word


def inn_tokens(text: object) -> list[str]:
    """The stemmed words of an active-ingredient text, joining words removed."""
    return [
        _stem(word)
        for word in _fold(text).split()
        if word not in _JOINING_WORDS
    ]


_SALT_STEMS = {_stem(word) for word in _fold(_SALT_WORDS_RAW).split()}


def query_tokens(substance: object) -> list[str]:
    """What a searched molecule must match: its words, less salts and hydrates.

    If the search is nothing but salt words ("sodium chloride"), every word
    counts, or the search would match everything.
    """
    tokens = inn_tokens(substance)
    core = [token for token in tokens if token not in _SALT_STEMS]
    return core or tokens


def _match_text(active_ingredient: object) -> str:
    """Tokens stored space-padded, so SQL can test whole-token membership."""
    return " " + " ".join(inn_tokens(active_ingredient)) + " "


# --------------------------------------------------------------------------
# The registers
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class OpenRegister:
    """A register published whole, and how to fetch, read and index it.

    Most are one CSV at one address, which is what the defaults do. A register
    published another way supplies ``fetch`` (put the published data in a
    working directory and return where it is) and ``read_records`` (yield one
    dict per record from there).
    """
    source: str
    country: str
    region: str
    url: str
    slug: str
    max_age_seconds: int
    build_rows: Callable[[Iterable[dict[str, Any]], str], Iterator[tuple[str, dict[str, Any]]]]
    encoding: str = "utf-8"
    extra_ca_certs: tuple[str, ...] = ()
    fetch: Callable[["OpenRegister", Path], Path] | None = None
    read_records: Callable[["OpenRegister", Path], Iterable[dict[str, Any]]] | None = None
    max_results: int = MAX_RESULTS


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


# Italy ------------------------------------------------------------------

ITALY_STATUS = {"Autorizzata": "Authorised", "Sospesa": "Suspended", "Revocata": "Revoked"}

ITALY_PROCEDURE = {
    "Procedura Nazionale": "National",
    "Procedura Mutuo riconoscimento o Decentrata": "Mutual recognition / decentralised",
    "Procedura Centralizzata": "Centralised",
    "Importazione Parallela": "Parallel import",
    "Registrazione Nazionale": "National registration",
    "Registrazione Mutuo Riconoscimento": "Mutual recognition registration",
}

# EDQM standard terms as AIFA writes them in Italian. Matched on the longest
# prefix, so "Soluzione iniettabile in siringa pre-riempita" falls to "Solution
# for injection"; together these cover over 90% of the non-homeopathic packs.
# A form not listed keeps its Italian wording rather than a guess.
ITALY_DOSAGE_FORMS = {
    "COMPRESSA RIVESTITA CON FILM": "Film-coated tablet",
    "COMPRESSA RIVESTITA": "Coated tablet",
    "COMPRESSA A RILASCIO PROLUNGATO": "Prolonged-release tablet",
    "COMPRESSA A RILASCIO MODIFICATO": "Modified-release tablet",
    "COMPRESSA GASTRORESISTENTE": "Gastro-resistant tablet",
    "COMPRESSA ORODISPERSIBILE": "Orodispersible tablet",
    "COMPRESSA MASTICABILE": "Chewable tablet",
    "COMPRESSA SUBLINGUALE": "Sublingual tablet",
    "COMPRESSA DISPERSIBILE": "Dispersible tablet",
    "COMPRESSA EFFERVESCENTE": "Effervescent tablet",
    "COMPRESSA": "Tablet",
    "CAPSULA RIGIDA GASTRORESISTENTE": "Gastro-resistant capsule, hard",
    "CAPSULA RIGIDA A RILASCIO PROLUNGATO": "Prolonged-release capsule, hard",
    "CAPSULA RIGIDA A RILASCIO MODIFICATO": "Modified-release capsule, hard",
    "CAPSULA RIGIDA": "Capsule, hard",
    "CAPSULA MOLLE": "Capsule, soft",
    "SOLUZIONE INIETTABILE O PER INFUSIONE": "Solution for injection/infusion",
    "SOLUZIONE INIETTABILE": "Solution for injection",
    "CONCENTRATO PER SOLUZIONE PER INFUSIONE": "Concentrate for solution for infusion",
    "SOLUZIONE PER INFUSIONE": "Solution for infusion",
    "SOLUZIONE PER DIALISI PERITONEALE": "Solution for peritoneal dialysis",
    "SOLUZIONE ORALE": "Oral solution",
    "SOLUZIONE CUTANEA": "Cutaneous solution",
    "SOSPENSIONE INIETTABILE": "Suspension for injection",
    "SOSPENSIONE ORALE": "Oral suspension",
    "POLVERE E SOLVENTE PER SOLUZIONE INIETTABILE": "Powder and solvent for solution for injection",
    "POLVERE PER CONCENTRATO PER SOLUZIONE PER INFUSIONE": "Powder for concentrate for solution for infusion",
    "POLVERE PER SOLUZIONE PER INFUSIONE": "Powder for solution for infusion",
    "POLVERE PER SOLUZIONE INIETTABILE": "Powder for solution for injection",
    "POLVERE PER SOLUZIONE ORALE": "Powder for oral solution",
    "POLVERE PER SOSPENSIONE ORALE": "Powder for oral suspension",
    "POLVERE PER INALAZIONE": "Inhalation powder",
    "EMULSIONE PER INFUSIONE": "Emulsion for infusion",
    "GOCCE ORALI, SOLUZIONE": "Oral drops, solution",
    "COLLIRIO, SOLUZIONE": "Eye drops, solution",
    "CEROTTO TRANSDERMICO": "Transdermal patch",
    "GAS MEDICINALE": "Medicinal gas",
    "GAS PER INALAZIONE": "Inhalation gas",
    "CREMA": "Cream",
    "GEL": "Gel",
    "PASTIGLIA": "Lozenge",
}


def english_italian_dosage_form(value: object) -> str:
    text = _clean(value)
    if not text or text.lower() == "non nota":
        return ""
    folded = "".join(
        char for char in unicodedata.normalize("NFKD", text.upper()) if not unicodedata.combining(char)
    )
    for italian in sorted(ITALY_DOSAGE_FORMS, key=len, reverse=True):
        if folded.startswith(italian):
            return ITALY_DOSAGE_FORMS[italian]
    return text


def _italy_pack(description: str) -> str:
    """The pack part of an AIFA description, in either order AIFA writes it.

        500 MG COMPRESSE RIVESTITE- 30 COMPRESSE     ->  30 COMPRESSE
        20 COMPRESSE IN BLISTER (PVC/ALL) DA 1000 MG ->  20 COMPRESSE
    """
    parts = re.split(r"\s*-\s+(?=\d)", description, maxsplit=1)
    if len(parts) == 2:
        return _clean(parts[1])
    leading = re.match(r"^(\d+\s+[A-Z]+)", description)
    return leading.group(1) if leading else ""


def build_italy_rows(
    records: Iterable[dict[str, str]], fetched_at: str
) -> Iterator[tuple[str, dict[str, Any]]]:
    """One row per product, strength and form -- the unit France's BDPM uses.

    AIFA lists every pack, so metformin alone is 1,378 lines. Packs of the same
    product at the same strength and form are folded into one row that lists
    its pack sizes. Homeopathic registrations -- 46% of the file, sold as
    granules under plant and mineral names -- are left out: they never answer a
    molecule search and would double the index.
    """
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        procedure = _clean(record.get("TIPO_PROCEDURA"))
        if procedure == "Omeopatico":
            continue
        brand = _clean(record.get("DENOMINAZIONE"))
        description = _clean(record.get("DESCRIZIONE"))
        strength = extract_strength(description)
        form = _clean(record.get("FORMA"))
        key = (_clean(record.get("COD_FARMACO")), strength.upper(), form.upper())
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "brand": brand,
                "strength": strength,
                "form": form,
                "active": _clean(record.get("PA_ASSOCIATI")),
                "company": _clean(record.get("RAGIONE_SOCIALE")),
                "status": _clean(record.get("STATO_AMMINISTRATIVO")),
                "procedure": procedure,
                "atc": _clean(record.get("CODICE_ATC")),
                "packs": [],
                "aic_codes": [],
            }
        pack = _italy_pack(description)
        if pack and pack not in group["packs"]:
            group["packs"].append(pack)
        aic = _clean(record.get("CODICE_AIC"))
        if aic:
            group["aic_codes"].append(aic)
        if not group["atc"]:
            group["atc"] = _clean(record.get("CODICE_ATC"))

    for (product_code, _strength, _form), group in groups.items():
        form = english_italian_dosage_form(group["form"])
        # The form is part of the name, as in France's BDPM: one AIC code can
        # cover GLUCOPHAGE 500 MG as a tablet and as a powder for oral solution,
        # and without it the two share a name and number and are merged into
        # one row -- on screen, and again when the row is saved.
        product = " ".join(part for part in (group["brand"], group["strength"]) if part)
        if form:
            product = f"{product}, {form}"
        packs = group["packs"]
        yield _match_text(group["active"]), {
            "substance": group["active"],
            "product": product,
            "company": group["company"],
            "country": "Italy",
            "region": "EU",
            "status": ITALY_STATUS.get(group["status"], group["status"]),
            "authorisation_scope": ITALY_PROCEDURE.get(group["procedure"], group["procedure"]),
            "strength": group["strength"],
            "dosage_form": form,
            "pack_size": "; ".join(packs[:8]) + ("; …" if len(packs) > 8 else ""),
            "atc_code": group["atc"],
            # The six-digit AIC code identifies the medicine; each pack adds a
            # three-digit suffix to it, and those are all the same product.
            "registration_number": product_code,
            "source": "AIFA Italy",
            "source_url": ITALY.url,
            "product_url": "",
            "document_type": "",
            "last_checked": fetched_at,
        }


# Brazil -----------------------------------------------------------------

BRAZIL_STATUS = {"Ativo": "Active", "Inativo": "Inactive"}

BRAZIL_CATEGORY = {
    "GENERICO": "Generic",
    "SIMILAR": "Similar",
    "NOVO": "New medicine",
    "ESPECIFICO": "Specific medicine",
    "BIOLOGICO": "Biological",
    "FITOTERAPICO": "Herbal medicine",
    "BAIXO RISCO": "Low-risk notified medicine",
    "RADIOFARMACO": "Radiopharmaceutical",
}


def _brazil_category(value: object) -> str:
    text = _clean(value)
    folded = "".join(
        char for char in unicodedata.normalize("NFKD", text.upper()) if not unicodedata.combining(char)
    )
    return BRAZIL_CATEGORY.get(folded, text)


def _brazil_expiry(value: object) -> str:
    """ANVISA writes expiry as MMYYYY; kept as the month it names, 2027-05."""
    text = _clean(value)
    if re.fullmatch(r"\d{6}", text) and 1 <= int(text[:2]) <= 12:
        return f"{text[2:]}-{text[:2]}"
    return ""


def _brazil_company(value: object) -> str:
    """The holder is written "<CNPJ> - <name>"; the tax number is not a name."""
    return _clean(re.sub(r"^\s*\d{8,14}\s*-\s*", "", str(value or "")))


def build_brazil_rows(
    records: Iterable[dict[str, str]], fetched_at: str
) -> Iterator[tuple[str, dict[str, Any]]]:
    """One row per registration, active or inactive, as ANVISA publishes them.

    Dynamised (homeopathic) registrations are left out, as for Italy. The
    regulatory category -- Generic, Similar, New -- is the distinction a generic
    search most needs, so it is kept as the authorisation scope.
    """
    for record in records:
        category = _clean(record.get("CATEGORIA_REGULATORIA"))
        if category.upper() == "DINAMIZADO":
            continue
        active = _clean(record.get("PRINCIPIO_ATIVO"))
        product = _clean(record.get("NOME_PRODUTO"))
        if not active and not product:
            continue
        yield _match_text(active or product), {
            "substance": active,
            "product": product,
            "company": _brazil_company(record.get("EMPRESA_DETENTORA_REGISTRO")),
            "country": "Brazil",
            "region": "BR",
            "status": BRAZIL_STATUS.get(_clean(record.get("SITUACAO_REGISTRO")), _clean(record.get("SITUACAO_REGISTRO"))),
            "authorisation_scope": _brazil_category(category),
            "therapeutic_category": _clean(record.get("CLASSE_TERAPEUTICA")),
            "registration_number": _clean(record.get("NUMERO_REGISTRO_PRODUTO")),
            # ANVISA's data dictionary: "Data em que o registro foi concedido".
            "registration_date": _clean(record.get("DATA_FINALIZACAO_PROCESSO")),
            "expiry_date": _brazil_expiry(record.get("DATA_VENCIMENTO_REGISTRO")),
            "source": "ANVISA Brazil",
            "source_url": BRAZIL.url,
            "product_url": "",
            "document_type": "",
            "last_checked": fetched_at,
        }


ITALY = OpenRegister(
    source="AIFA Italy",
    country="Italy",
    region="EU",
    url="https://drive.aifa.gov.it/farmaci/confezioni.csv",
    slug="aifa_italy",
    encoding="utf-8",
    max_age_seconds=7 * 24 * 3600,
    build_rows=build_italy_rows,
)

BRAZIL = OpenRegister(
    source="ANVISA Brazil",
    country="Brazil",
    region="BR",
    url="https://dados.anvisa.gov.br/dados/DADOS_ABERTOS_MEDICAMENTOS.csv",
    slug="anvisa_brazil",
    # The data dictionary names code page 1252, not Latin-1; they differ on the
    # typographic quotes and dashes ANVISA's free text uses.
    encoding="cp1252",
    max_age_seconds=31 * 24 * 3600,
    build_rows=build_brazil_rows,
    # dados.anvisa.gov.br sends its own certificate without the intermediate
    # that links it to a trusted root. Browsers fetch the missing certificate
    # themselves; Python's TLS stack does not and refuses the connection. The
    # intermediate is shipped here and added to certifi's roots, so the chain
    # is still verified end to end -- verification is never switched off.
    extra_ca_certs=("sectigo_public_server_authentication_ca_ov_r36.pem",),
)

REGISTERS: dict[str, OpenRegister] = {}


def add_register(register: OpenRegister) -> OpenRegister:
    """Make a register searchable and refreshed; its own module calls this."""
    REGISTERS[register.source] = register
    return register


add_register(ITALY)
add_register(BRAZIL)


# --------------------------------------------------------------------------
# Download and index
# --------------------------------------------------------------------------

_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


def _refresh_lock(register: OpenRegister) -> threading.Lock:
    with _refresh_locks_guard:
        return _refresh_locks.setdefault(register.slug, threading.Lock())


# How many registers download and index at once. Startup asks for every
# missing register together; on a small instance that many parsers at once is
# an out-of-memory kill, so the rest wait their turn.
_build_slots = threading.BoundedSemaphore(max(1, int(os.getenv("PHARMASEARCH_REGISTER_BUILDS") or 4)))
_ca_bundle_lock = threading.Lock()


def _ca_bundle(register: OpenRegister) -> str | bool:
    """certifi's roots plus any intermediates a register's server omits."""
    if not register.extra_ca_certs:
        return True
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    bundle = INDEX_DIR / f"{register.slug}_ca_bundle.pem"
    with _ca_bundle_lock:
        contents = Path(certifi.where()).read_text(encoding="ascii")
        for name in register.extra_ca_certs:
            contents += "\n" + (CERT_DIR / name).read_text(encoding="ascii")
        bundle.write_text(contents, encoding="ascii")
    return str(bundle)


def _pointer(register: OpenRegister) -> Path:
    return INDEX_DIR / f"{register.slug}.current"


def current_index(register: OpenRegister) -> Path | None:
    """The index searches should read, or None if none has been built."""
    try:
        name = _pointer(register).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    path = INDEX_DIR / name
    return path if name and path.exists() else None


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    """A connection that is closed on the way out, not merely committed.

    sqlite3's own context manager only ends the transaction and leaves the
    connection open. On Windows an open connection keeps the file locked, so
    a refresh could never remove the index it replaced.
    """
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def index_info(register: OpenRegister) -> dict[str, Any] | None:
    path = current_index(register)
    if path is None:
        return None
    with _connect(path) as conn:
        return dict(conn.execute("SELECT key, value FROM meta").fetchall())


def _is_stale(register: OpenRegister, info: dict[str, Any] | None) -> bool:
    if not info:
        return True
    if info.get("match_version") != str(MATCH_VERSION):
        # Built under older matching rules: its match text no longer meets
        # the search's, so it is out of date whatever its age.
        return True
    try:
        return time.time() - float(info.get("fetched_epoch", 0)) > register.max_age_seconds
    except (TypeError, ValueError):
        return True


def download_file(register: OpenRegister, url: str, destination: Path) -> Path:
    """Stream one published file to disk, verifying TLS with the register's roots.

    A dropped connection is tried again: HPRA's server resets about one
    connection in two, and a weekly refresh should not wait a week for it.
    """
    for attempt in range(3):
        try:
            with requests.get(
                url,
                stream=True,
                timeout=DOWNLOAD_TIMEOUT,
                headers={"User-Agent": "PharmaSearch/1.0 (regulatory register download)"},
                verify=_ca_bundle(register),
            ) as response:
                response.raise_for_status()
                with destination.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1 << 20):
                        handle.write(chunk)
            return destination
        except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError):
            if attempt == 2:
                raise
            logger.info("%s: download interrupted, trying again", register.source)
            time.sleep(5 * (attempt + 1))
    return destination


def _fetch_single_file(register: OpenRegister, workdir: Path) -> Path:
    return download_file(register, register.url, workdir / f"{register.slug}.download")


def _read_semicolon_csv(register: OpenRegister, path: Path) -> Iterator[dict[str, str]]:
    with path.open("r", encoding=register.encoding, errors="replace", newline="") as handle:
        yield from csv.DictReader(handle, delimiter=";")


def _size_on_disk(path: Path) -> int:
    if path.is_dir():
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    return path.stat().st_size


def build_index(register: OpenRegister, source_path: Path) -> Path:
    """Index a fetched register and make it the one searches read.

    ``source_path`` is whatever the register's fetch step produced: a single
    downloaded file, or a directory of them.

    The index is written to a new file and only then pointed at, so a search
    running during a refresh reads the old copy whole rather than a half-built
    one.
    """
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    fetched_epoch = time.time()
    fetched_at = datetime.fromtimestamp(fetched_epoch, timezone.utc).isoformat(timespec="seconds")
    path = INDEX_DIR / f"{register.slug}-{time.time_ns()}.sqlite"
    records = (register.read_records or _read_semicolon_csv)(register, source_path)
    with _connect(path) as conn:
        conn.execute("CREATE TABLE rows (match TEXT NOT NULL, payload TEXT NOT NULL)")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        count = 0
        batch: list[tuple[str, str]] = []
        for match, row in register.build_rows(records, fetched_at):
            batch.append((match, json.dumps(row, ensure_ascii=False)))
            if len(batch) >= 5000:
                conn.executemany("INSERT INTO rows VALUES (?, ?)", batch)
                count += len(batch)
                batch.clear()
        conn.executemany("INSERT INTO rows VALUES (?, ?)", batch)
        count += len(batch)
        conn.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [
                ("source", register.source),
                ("url", register.url),
                ("fetched_at", fetched_at),
                ("fetched_epoch", str(fetched_epoch)),
                ("rows", str(count)),
                ("match_version", str(MATCH_VERSION)),
            ],
        )
    previous = current_index(register)
    _pointer(register).write_text(path.name, encoding="utf-8")
    if previous and previous != path:
        try:
            previous.unlink()
        except OSError:
            # Still open in a search on Windows; the next refresh removes it.
            pass
    for leftover in INDEX_DIR.glob(f"{register.slug}-*.sqlite"):
        if leftover != path:
            try:
                leftover.unlink()
            except OSError:
                pass
    logger.info("Indexed %s: %s rows from %s", register.source, count, register.url)
    return path


# Where a register is built. "inline" builds it in this process. "child"
# builds it in a separate, short-lived process, so a register that needs more
# memory than the server has takes down only that process and never the
# website; the deployments set it.
REGISTER_PROCESS = (os.getenv("PHARMASEARCH_REGISTER_PROCESS") or "inline").strip().lower()
# After a failed build, a register waits this long before it is tried again,
# so one that cannot fit cannot keep a server busy failing.
FAILED_BUILD_PAUSE_SECONDS = 6 * 3600
CHILD_BUILD_TIMEOUT_SECONDS = 45 * 60


def _failure_marker(register: OpenRegister) -> Path:
    return INDEX_DIR / f"{register.slug}.failed"


def _recently_failed(register: OpenRegister) -> bool:
    try:
        return time.time() - _failure_marker(register).stat().st_mtime < FAILED_BUILD_PAUSE_SECONDS
    except OSError:
        return False


def _refresh_in_child(register: OpenRegister) -> Path | None:
    """Build the register in its own process and report how that went."""
    import shutil
    import subprocess
    import sys

    if _recently_failed(register):
        logger.info("Skipping %s: its last build failed less than %d hours ago",
                    register.source, FAILED_BUILD_PAUSE_SECONDS // 3600)
        return None
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PHARMASEARCH_REGISTER_PROCESS="inline")
    with _build_slots:
        try:
            # Lowest priority where the system offers it: on a one-core
            # server, a register being indexed must not slow the website.
            nice = ["nice", "-n", "19"] if shutil.which("nice") else []
            done = subprocess.run(
                [*nice, sys.executable, "-m", "sources.register_worker", register.source],
                cwd=str(BASE_DIR), env=env, capture_output=True, text=True,
                timeout=CHILD_BUILD_TIMEOUT_SECONDS,
            )
            code, tail = done.returncode, (done.stderr or done.stdout or "")[-600:]
        except subprocess.TimeoutExpired:
            code, tail = "timeout", ""
    if code == 0:
        _failure_marker(register).unlink(missing_ok=True)
        logger.info("Built %s in its own process", register.source)
        return current_index(register)
    _failure_marker(register).write_text(str(code), encoding="utf-8")
    reason = "it ran out of memory" if code in (-9, 137) else f"exit {code}"
    logger.warning("Building %s failed (%s); next try in %d hours. %s",
                   register.source, reason, FAILED_BUILD_PAUSE_SECONDS // 3600, tail.strip())
    return None


def refresh_register(register: OpenRegister) -> Path | None:
    """Download and index a register, unless another thread is already doing so."""
    lock = _refresh_lock(register)
    if not lock.acquire(blocking=False):
        return None
    if REGISTER_PROCESS == "child":
        try:
            return _refresh_in_child(register)
        finally:
            lock.release()
    try:
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        with _build_slots, tempfile.TemporaryDirectory(dir=INDEX_DIR) as scratch:
            started = time.time()
            fetched = (register.fetch or _fetch_single_file)(register, Path(scratch))
            logger.info(
                "Downloaded %s (%.1f MB) in %.0fs",
                register.source, _size_on_disk(fetched) / 1e6, time.time() - started,
            )
            return build_index(register, fetched)
    except Exception:
        logger.exception("Refreshing %s failed", register.source)
        return None
    finally:
        lock.release()


def refresh_in_background(register: OpenRegister) -> bool:
    """Start a refresh on a daemon thread; False if one is already running."""
    if _refresh_lock(register).locked():
        return False
    threading.Thread(
        target=refresh_register, args=(register,), name=f"refresh-{register.slug}", daemon=True
    ).start()
    return True


def warm_open_registers() -> None:
    """Fetch any register that is missing or stale, without holding up startup."""
    for register in REGISTERS.values():
        if _is_stale(register, index_info(register)):
            refresh_in_background(register)


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

def _status_rank(row: dict[str, Any]) -> int:
    status = str(row.get("status") or "")
    current = {"Authorised", "Active", "Marketed", "Approved"}
    return 0 if status in current or status.startswith("Marketed (") else 1


def search_register(register: OpenRegister, substance: str, limit: int | None = None) -> list[dict[str, Any]]:
    """Rows whose own active ingredient contains every word of the molecule.

    Matching the register's active-ingredient field, and not the product name
    or free text, keeps other drugs out: a leaflet that merely mentions
    atorvastatin does not make an amlodipine tablet an atorvastatin result.
    """
    tokens = query_tokens(substance)
    if not tokens:
        return []
    path = current_index(register)
    if path is None:
        started = refresh_in_background(register)
        raise RegisterNotReady(
            f"{register.source} register is being downloaded for the first time"
            f"{'' if started else ' (already in progress)'}; search again in a few minutes."
        )
    if _is_stale(register, index_info(register)):
        refresh_in_background(register)

    where = " AND ".join("match LIKE ?" for _ in tokens)
    params = [f"% {token} %" for token in tokens]
    with _connect(path) as conn:
        payloads = [payload for (payload,) in conn.execute(f"SELECT payload FROM rows WHERE {where}", params)]
    rows = [json.loads(payload) for payload in payloads]
    rows.sort(key=lambda row: (_status_rank(row), row.get("product", "").lower()))
    bound = register.max_results if limit is None else limit
    if len(rows) > bound:
        # Say so rather than trim silently: the search page names a registry
        # whose rows stop short of what it holds.
        for row in rows[:bound]:
            row["available_total"] = len(rows)
    return rows[:bound]


def run_aifa_italy_search(substance: str) -> list[dict[str, Any]]:
    return search_register(ITALY, substance)


def run_anvisa_brazil_search(substance: str) -> list[dict[str, Any]]:
    return search_register(BRAZIL, substance)
