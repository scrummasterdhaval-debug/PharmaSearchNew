"""National registers six more European regulators publish whole.

    Norway    DMP's FEST, the prescribing catalogue, published every two weeks
              as one XML file (NLOD 2.0); product, form and strength, ATC, the
              company FEST names for it, English substance names, SPC link.
    Slovakia  SUKL's list of every medicine with a valid registration, daily,
              from its JSON service (CC0); holder and country, registration
              number and type, and SUKL's own English form, route and status.
    Latvia    ZVA's Medicines Register export, daily (CC0); English product
              name and form, holder, manufacturer, parallel importer,
              procedure, SmPC and leaflet.
    Turkey    TITCK's weekly list of licensed human medicinal products; holder,
              licence number and date, ATC, and whether the licence is
              suspended.
    Serbia    ALIMS's register of medicines for human use, daily (Serbian open
              data licence); holder, manufacturer and its country, decision
              number and dates, ATC, route.
    Malta     The Medicines Authority's list of authorised medicines, in
              English; holder, authorisation number and date, status, ATC.

Indexed locally like the others (open_registers). Norway's substances come in
English from FEST itself; Slovakia's list names no substance, so its rows are
matched on the English WHO name of their ATC code and their product name.
Turkish spells an INN as it is said ("dekzametazon", "parasetamol"), so
Turkey and Serbia are matched the way Russia is: both sides folded to one
spelling.
"""
from __future__ import annotations

import json
import re
import io
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from sources.national_registers import _clean, _date, _indexed, _unique, national_match_text
from sources.parser import extract_strength
from sources.open_registers import (
    OpenRegister,
    RegisterNotReady,
    _connect,
    _fold,
    _is_stale,
    _status_rank,
    add_register,
    current_index,
    download_file,
    index_info,
    inn_tokens,
    query_tokens,
    refresh_in_background,
    search_register,
)


# ----------------------------------------------------------------- Norway

NORWAY_URL = (
    "https://www.dmp.no/globalassets/documents/om-oss/distribusjon-av-legemiddeldata/"
    "fest/festfiler/fest251.zip"
)
NORWAY_PAGE = "https://www.dmp.no/om-oss/distribusjon-av-legemiddeldata/fest"
_FEST = "{http://www.kith.no/xmlstds/eresept/forskrivning/2014-12-01}"
NORWAY_FORMS = {
    "tablett": "Tablet",
    "tablett, filmdrasjert": "Film-coated tablet",
    "filmdrasjert tablett": "Film-coated tablet",
    "depottablett": "Prolonged-release tablet",
    "enterotablett": "Gastro-resistant tablet",
    "smeltetablett": "Orodispersible tablet",
    "tyggetablett": "Chewable tablet",
    "brusetablett": "Effervescent tablet",
    "kapsel, hard": "Capsule, hard",
    "kapsel, myk": "Capsule, soft",
    "injeksjonsvæske, oppløsning": "Solution for injection",
    "infusjonsvæske, oppløsning": "Solution for infusion",
    "konsentrat til infusjonsvæske, oppløsning": "Concentrate for solution for infusion",
    "mikstur, oppløsning": "Oral solution",
    "mikstur, suspensjon": "Oral suspension",
    "øyedråper, oppløsning": "Eye drops, solution",
    "krem": "Cream",
    "salve": "Ointment",
    "gel": "Gel",
    "depotplaster": "Transdermal patch",
}


# FEST also lists what is not authorised in Norway: medicines supplied on an
# exemption ("Krever godkj. Fritak"), food supplements, hospital and pharmacy
# preparations. Only these types are marketing authorisations or registrations.
NORWAY_AUTHORISED_TYPES = {
    "Legemiddel": "Medicinal product",
    "Vaksine": "Vaccine",
    "Medisinsk gass": "Medicinal gas",
    "Radiofarmaka": "Radiopharmaceutical",
    "Tradisjonelt plantebasert": "Traditional herbal medicinal product",
    "Veletablert plantebasert": "Well-established herbal medicinal product",
}


def fetch_norway(register: OpenRegister, workdir: Path) -> Path:
    return download_file(register, register.url, workdir / "fest.zip")


def _text(element: ET.Element | None, tag: str) -> str:
    found = element.find(f"{_FEST}{tag}") if element is not None else None
    return _clean(found.text) if found is not None else ""


def _display(element: ET.Element | None, tag: str) -> tuple[str, str]:
    found = element.find(f"{_FEST}{tag}") if element is not None else None
    return (found.get("V", ""), found.get("DN", "")) if found is not None else ("", "")


def _iterparse_releasing(handle) -> Iterator[ET.Element]:
    """Every element as it closes, each catalogue entry let go once it is read.

    clear() empties an element but leaves it in its parent, and FEST's
    catalogues hold tens of thousands of entries: the emptied shells alone
    took the read past 800 MB, more than a small server has. Removing an
    entry from its catalogue once it has closed is what frees it.
    """
    open_elements: list[ET.Element] = []
    for event, element in ET.iterparse(handle, events=("start", "end")):
        if event == "start":
            open_elements.append(element)
            continue
        open_elements.pop()
        yield element
        if 0 < len(open_elements) <= 2:
            open_elements[-1].remove(element)


def read_norway(register: OpenRegister, path: Path) -> Iterator[dict[str, Any]]:
    """Products with their substances resolved to FEST's English names.

    FEST keeps substances apart from products: a product refers to a
    substance-with-strength, which refers to a substance. The file is read
    once for the substances, then again for the products.
    """
    with zipfile.ZipFile(path) as archive:
        name = next(item for item in archive.namelist() if item.lower().endswith(".xml"))
        substances: dict[str, str] = {}
        with_strength: dict[str, str] = {}
        with archive.open(name) as handle:
            for element in _iterparse_releasing(handle):
                if element.tag == f"{_FEST}Virkestoff":
                    substances[_text(element, "Id")] = _text(element, "NavnEngelsk") or _text(element, "Navn")
                    element.clear()
                elif element.tag == f"{_FEST}VirkestoffMedStyrke":
                    with_strength[_text(element, "Id")] = _text(element, "RefVirkestoff")
                    element.clear()
                elif element.tag.endswith(("OppfLegemiddelpakning", "OppfLegemiddelMerkevare")):
                    element.clear()
        records = []
        with archive.open(name) as handle:
            for element in _iterparse_releasing(handle):
                if not element.tag.endswith("}OppfLegemiddelMerkevare"):
                    continue
                status = element.find("{*}Status")
                product = element.find(f"{_FEST}LegemiddelMerkevare")
                if product is None or (status is not None and status.get("V") != "A"):
                    element.clear()
                    continue
                refs = [
                    _clean(ref.text)
                    for ref in product.iter(f"{_FEST}RefVirkestoffMedStyrke")
                ]
                spc = next((link.get("V", "") for link in product.iter(f"{_FEST}Www")), "")
                producer = product.find(f"{_FEST}ProduktInfo")
                kind = _display(product, "Preparattype")[1]
                if kind not in NORWAY_AUTHORISED_TYPES:
                    element.clear()
                    continue
                records.append({
                    "name": _text(product, "NavnFormStyrke"),
                    "brand": _text(product, "Varenavn"),
                    "form": _text(product, "LegemiddelformLang") or _display(product, "LegemiddelformKort")[1],
                    "atc": _display(product, "Atc"),
                    "prescription": _display(product, "Reseptgruppe")[1],
                    "type": NORWAY_AUTHORISED_TYPES[kind],
                    "route": _unique(
                        route.get("DN", "") for route in product.iter(f"{_FEST}Administrasjonsvei")
                    ),
                    "company": _text(producer, "Produsent"),
                    "actives": [substances.get(with_strength.get(ref, ""), "") for ref in refs],
                    "spc": spc,
                })
                element.clear()
    # One name, several entries: the holder and each parallel importer, which
    # FEST does not tell apart. The company, then the form, tells them apart.
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_name[record["name"]].append(record)
    for same in by_name.values():
        if len(same) > 1:
            for record in same:
                record["label"] = f"{record['name']} ({record['company']})"
            if len({record["label"] for record in same}) < len(same):
                for record in same:
                    record["label"] = f"{record['name']} ({record['company']}, {record['form']})"
    yield from records


def build_norway_rows(records: Iterable[dict[str, Any]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for record in records:
        active = _unique(record["actives"])
        atc_code, atc_name = record["atc"]
        name = record["name"]
        if not (active or name):
            continue
        yield _indexed(national_match_text(active or atc_name or name), {
            "substance": active,
            "active_substance": active,
            "source_substance": active,
            "product": record.get("label") or name,
            # FEST calls it the product's "produsent": the company DMP gave the
            # permission to market it in Norway.
            "company": record["company"],
            "country": "Norway",
            "region": "EU",
            "status": "Listed in FEST",
            "classification": _unique([record["prescription"], record["type"]]),
            "strength": re.sub(r"^.*?(?=\d)", "", name) if re.search(r"\d", name) else "",
            "dosage_form": NORWAY_FORMS.get(record["form"].lower(), record["form"]),
            "route": record["route"],
            "atc_code": atc_code,
            "registration_number": "",
            "source": "DMP Norway",
            "source_url": NORWAY_PAGE,
            "product_url": "",
            "smpc_url": record["spc"],
            "document_type": "DMP FEST prescribing catalogue record",
            "last_checked": fetched_at,
        })


DMP_NORWAY = add_register(OpenRegister(
    source="DMP Norway",
    country="Norway",
    region="EU",
    url=NORWAY_URL,
    slug="dmp_norway",
    max_age_seconds=7 * 24 * 3600,
    build_rows=build_norway_rows,
    fetch=fetch_norway,
    read_records=read_norway,
))


def run_dmp_norway_search(substance: str) -> list[dict[str, Any]]:
    return search_register(DMP_NORWAY, substance)


# --------------------------------------------------------------- Slovakia

# SUKL's monthly list of every medicine with a valid registration, the same
# data its JSON API served. That API (api.sukl.sk) has been resetting every
# connection, while this file, which SUKL lists on data.slovensko.sk, is
# served from its main website.
SLOVAKIA_URL = "https://www.sukl.sk/verejne/Zoznam_liekov/zoznam_liekov_aktualny.zip"
SLOVAKIA_PAGE = "https://www.sukl.sk/hlavna-stranka/slovenska-verzia/databazy-a-servis"
SLOVAKIA_STATUS = {
    "D": "Registered, unlimited validity",
    "P": "Registered, renewal applied for",
    "R": "Registered",
    "E": "Centralised authorisation",
    "Ex": "Conditional centralised authorisation",
    "Ev": "Centralised authorisation under exceptional circumstances",
}
SLOVAKIA_PROCEDURE = {
    "EU": "Centralised", "EUO": "Centralised (orphan)", "NAR": "National",
    "MRP": "Mutual recognition", "DCP": "Decentralised", "REF": "Referral",
}
SLOVAKIA_SUPPLY = {
    "R": "Prescription only", "Rx": "Prescription only, restricted",
    "Rb": "Prescription on a special form", "F": "Non-prescription",
}
# SUKL's dosage-form codes; a code not listed is shown as SUKL writes it.
SLOVAKIA_FORMS = {
    "TBL": "Tablet", "TBL FLM": "Film-coated tablet", "TBL OBD": "Coated tablet",
    "TBL PLG": "Prolonged-release tablet", "TBL MOD": "Modified-release tablet",
    "TBL ENT": "Gastro-resistant tablet", "TBL ORO": "Orodispersible tablet",
    "TBL EFF": "Effervescent tablet", "TBL MND": "Chewable tablet",
    "CPS DUR": "Capsule, hard", "CPS MOL": "Capsule, soft", "CPS END": "Gastro-resistant capsule, hard",
    "CPS PLD": "Prolonged-release capsule, hard", "GRA": "Granules",
    "SOL INJ": "Solution for injection", "SOL INF": "Solution for infusion",
    "SUS INJ": "Suspension for injection", "CON INF": "Concentrate for solution for infusion",
    "GEL": "Gel", "EMP TDM": "Transdermal patch",
}


def fetch_slovakia(register: OpenRegister, workdir: Path) -> Path:
    return download_file(register, register.url, workdir / "sukl_zoznam_liekov.zip")


def _xls_rows(archive: zipfile.ZipFile, name: str) -> list[list[Any]]:
    """SUKL's code tables are old-format .xls workbooks; their first row is the header."""
    import xlrd

    sheet = xlrd.open_workbook(file_contents=archive.read(name), logfile=io.StringIO()).sheet_by_index(0)
    return [sheet.row_values(row) for row in range(1, sheet.nrows)]


def read_slovakia(register: OpenRegister, path: Path) -> Iterator[dict[str, Any]]:
    """One record per registration, its packs gathered, in the field names SUKL's API used."""
    import openpyxl

    with zipfile.ZipFile(path) as archive:
        holders = {_clean(row[0]): _clean(row[1]) for row in _xls_rows(archive, "firmy.xls") if row and row[0]}
        atc_names = {_clean(row[0]): _clean(row[1]) for row in _xls_rows(archive, "atc.xls") if row and row[0]}
        workbook = openpyxl.load_workbook(io.BytesIO(archive.read("zoznam_liekov.xlsx")), read_only=True)
    try:
        rows = workbook.worksheets[0].iter_rows(values_only=True)
        header = [_clean(cell) for cell in next(rows)]
        column = {name: index for index, name in enumerate(header)}

        def cell(values, name):
            index = column.get(name)
            return values[index] if index is not None and index < len(values) else None

        validity_column = next((name for name in header if name.startswith("Platnosť do")), "")
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for values in rows:
            name = _clean(cell(values, "Názov"))
            number = _clean(cell(values, "Registračné číslo"))
            if not name:
                continue
            # An EU number names the pack in its last part: EU/1/15/1051/012.
            if number.startswith("EU/"):
                number = "/".join(number.split("/")[:4])
            key = (number, name)
            record = grouped.get(key)
            if record is None:
                atc_code = _clean(cell(values, "ATC kód"))
                validity = _clean(cell(values, validity_column))
                registered = cell(values, "Dátum registrácie")
                form_code = _clean(cell(values, "form_kod"))
                record = grouped[key] = {
                    "lie_nazov": name,
                    "atc_kod": atc_code,
                    "atc_nazov": atc_names.get(atc_code) or _clean(cell(values, "atc_nazov_sk")),
                    "drz_nazov": holders.get(_clean(cell(values, "Kód držiteľa"))) or _clean(cell(values, "Kód držiteľa")),
                    "stav_kod": validity if validity in SLOVAKIA_STATUS else "R",
                    "reg_typ_nazov_en": SLOVAKIA_PROCEDURE.get(_clean(cell(values, "Typ registrácie")), _clean(cell(values, "Typ registrácie"))),
                    "lie_sila": _clean(cell(values, "lie_sila")),
                    "form_nazov_en": SLOVAKIA_FORMS.get(form_code, form_code),
                    "vyd_nazov_en": SLOVAKIA_SUPPLY.get(_clean(cell(values, "Výdaj")), ""),
                    "lie_registracia": registered.date().isoformat() if hasattr(registered, "date") else _clean(registered),
                    "registration": number,
                    "packs": [],
                }
            record["packs"].append(_clean(cell(values, "Doplnok")))
    finally:
        workbook.close()
    yield from grouped.values()


def _sk(record: dict[str, Any], field: str) -> str:
    """SUKL writes "?" where it has no value."""
    value = _clean(record.get(field))
    return "" if value == "?" else value


def build_slovakia_rows(records: Iterable[dict[str, Any]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for record in records:
        product = _clean(record.get("lie_nazov"))
        atc_name = _clean(record.get("atc_nazov"))
        if not product:
            continue
        # SUKL's list names no substance; the WHO name of the ATC code is the
        # molecule for most products, "and" joining a combination's parts.
        active = "; ".join(part.strip() for part in re.split(r"\s+and\s+", atc_name) if part.strip())
        status_code = _clean(record.get("stav_kod"))
        yield _indexed(national_match_text(f"{atc_name} {product}"), {
            "substance": active,
            "active_substance": active,
            "source_substance": atc_name,
            "product": product,
            "company": _clean(record.get("drz_nazov")),
            "country": "Slovakia",
            "region": "EU",
            "status": SLOVAKIA_STATUS.get(status_code) or _clean(record.get("stav_nazov_en")) or _clean(record.get("stav_nazov")),
            "authorisation_scope": _sk(record, "reg_typ_nazov_en"),
            "strength": _clean(record.get("lie_sila")),
            "dosage_form": _sk(record, "form_nazov_en").rstrip("*") or _sk(record, "form_nazov"),
            "route": _sk(record, "pod_nazov_en") or _sk(record, "pod_nazov"),
            "pack_size": _unique(record["packs"]),
            "atc_code": _clean(record.get("atc_kod")),
            "classification": _sk(record, "vyd_nazov_en"),
            "registration_number": record["registration"],
            "registration_date": _date(record.get("lie_registracia")),
            "source": "SUKL Slovakia",
            "source_url": SLOVAKIA_PAGE,
            "product_url": "",
            "document_type": "SUKL list of medicines with a valid registration",
            "last_checked": fetched_at,
        })


SUKL_SLOVAKIA = add_register(OpenRegister(
    source="SUKL Slovakia",
    country="Slovakia",
    region="EU",
    url=SLOVAKIA_URL,
    slug="sukl_slovakia",
    max_age_seconds=24 * 3600,
    build_rows=build_slovakia_rows,
    fetch=fetch_slovakia,
    read_records=read_slovakia,
))


def run_sukl_slovakia_search(substance: str) -> list[dict[str, Any]]:
    return search_register(SUKL_SLOVAKIA, substance)


# ----------------------------------------------------------------- Latvia

LATVIA_URL = "https://dati.zva.gov.lv/zalu-registrs/export/HumanProducts.json.zip"
LATVIA_PAGE = "https://dati.zva.gov.lv/zalu-registrs/"
LATVIA_PROCEDURES = {
    "Nacionālā reģistrācijas procedūra": "National",
    "Decentralizētā reģistrācijas procedūra": "Decentralised",
    "Savstarpējās atzīšanas procedūra": "Mutual recognition",
    "Eiropas centralizētā reģistrācijas procedūra": "Centralised",
    "Paralēlais imports": "Parallel import",
    "Paralēlā izplatīšana": "Parallel distribution",
    "Nereģistrētas zāles": "Unregistered medicine",
}


def fetch_latvia(register: OpenRegister, workdir: Path) -> Path:
    return download_file(register, register.url, workdir / "zva_human_products.json.zip")


def read_latvia(register: OpenRegister, path: Path) -> Iterator[dict[str, Any]]:
    """One record per authorisation, strength and form, its packs gathered together."""
    with zipfile.ZipFile(path) as archive:
        name = next(item for item in archive.namelist() if item.lower().endswith(".json"))
        packs = json.loads(archive.read(name))
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    for pack in packs:
        if _clean(pack.get("prd_removed")) == "1":
            continue
        number = _clean(pack.get("authorisation_no"))
        # An EU number names the pack in its last part: EU/1/05/308/001.
        if number.startswith("EU/"):
            number = "/".join(number.split("/")[:4])
        pack["authorisation_no"] = number
        key = (
            number, _clean(pack.get("strength")),
            _clean(pack.get("pharmaceutical_form")), _clean(pack.get("parallel_importer_en")),
        )
        record = grouped.get(key)
        if record is None:
            record = grouped[key] = {**pack, "packs": []}
        record["packs"].append(_clean(pack.get("package_en")) or _clean(pack.get("package")))
    yield from grouped.values()


def build_latvia_rows(records: Iterable[dict[str, Any]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for record in records:
        active = _unique(re.split(r"\s*(?:,|/|\+)\s*", _clean(record.get("active_substance"))))
        product = _clean(record.get("original_name")) or _clean(record.get("medicine_name"))
        if not (active or product):
            continue
        importer = _clean(record.get("parallel_importer_en")) or _clean(record.get("parallel_importer"))
        procedure = _clean(record.get("authorisation_procedure"))
        number = _clean(record.get("authorisation_no"))
        if number.startswith("EU/"):
            # A centrally authorised product is named without its strength
            # ("Abasaglar"), and one EU number covers every strength.
            product = " ".join(
                part for part in (product, _clean(record.get("strength")), _clean(record.get("pharmaceutical_form"))) if part
            )
        if importer:
            # Each parallel importer is listed under the original's name.
            product = f"{product} (parallel import: {importer})"
        maker = _clean(record.get("manufacturer"))
        date_text = _clean(record.get("date_of_authorisation"))
        try:
            registered = datetime.strptime(date_text.title(), "%d-%b-%y").date().isoformat()
        except ValueError:
            registered = _date(date_text)
        yield _indexed(national_match_text(active or product, latin=True), {
            "substance": active,
            "active_substance": active,
            "source_substance": _clean(record.get("active_substance")),
            "product": product,
            "company": _clean(record.get("marketing_authorisation_holder")),
            "country": "Latvia",
            "region": "EU",
            "status": "Authorised" if _clean(record.get("status")) == "1" else "Not currently authorised",
            "authorisation_scope": LATVIA_PROCEDURES.get(procedure, procedure),
            "strength": _clean(record.get("strength")),
            "dosage_form": _clean(record.get("pharmaceutical_form")),
            "pack_size": _unique(record["packs"]),
            "atc_code": _clean(record.get("atc_code")),
            "registration_number": number,
            "registration_date": registered,
            "manufacturer_name": maker,
            "manufacturer_source": "Latvian Medicines Register" if maker else "",
            "source": "ZVA Latvia",
            "source_url": LATVIA_PAGE,
            "product_url": "",
            "smpc_url": _clean(record.get("summary_of_product_characteristics")),
            "pil_url": _clean(record.get("package_leaflet")),
            "document_type": "ZVA Medicines Register record",
            "last_checked": fetched_at,
        })


ZVA_LATVIA = add_register(OpenRegister(
    source="ZVA Latvia",
    country="Latvia",
    region="EU",
    url=LATVIA_URL,
    slug="zva_latvia",
    max_age_seconds=24 * 3600,
    build_rows=build_latvia_rows,
    fetch=fetch_latvia,
    read_records=read_latvia,
))


def run_zva_latvia_search(substance: str) -> list[dict[str, Any]]:
    return search_register(ZVA_LATVIA, substance)


# ----------------------------------------------------------------- Turkey

TURKEY_PAGE = "https://www.titck.gov.tr/dinamikmodul/85"
TURKEY_SHEET = "RUHSATLI ÜRÜNLER LİSTESİ"
TURKEY_SUSPENSION = {
    "1": "Suspended (Article 23)",
    "2": "Suspended (pharmacovigilance)",
    "3": "Suspended (Article 22)",
}
# Salt words as Turkish writes them, put into English so a search that names
# the salt still finds the row.
TURKISH_WORDS = {
    "hidroklorur": "hydrochloride", "hcl": "hydrochloride", "klorur": "chloride", "bromur": "bromide",
    "sodyum": "sodium", "disodyum": "disodium", "kalsiyum": "calcium", "potasyum": "potassium",
    "magnezyum": "magnesium", "asit": "acid",
}


def fetch_turkey(register: OpenRegister, workdir: Path) -> Path:
    """The list is republished weekly under a new file name; the page names the latest."""
    import requests

    from sources.open_registers import DOWNLOAD_TIMEOUT

    page = requests.get(
        TURKEY_PAGE, timeout=DOWNLOAD_TIMEOUT,
        headers={"User-Agent": "PharmaSearch/1.0 (regulatory register download)"},
    )
    page.raise_for_status()
    links = re.findall(r'https?://[^"\']*RuhsatlBeeri[^"\']*\.xlsx', page.text)
    if not links:
        raise RuntimeError("TITCK's page names no licensed products list")

    def published(link: str) -> datetime:
        found = re.search(r"(\d{2}\.\d{2}\.\d{4})", link)
        return datetime.strptime(found.group(1), "%d.%m.%Y") if found else datetime.min

    return download_file(register, max(links, key=published), workdir / "titck_licensed.xlsx")


def turkish_tokens(text: object) -> list[str]:
    """Turkish INN words folded the way English ones are, both spellings kept.

    Turkish writes a soft c as s ("parasetamol", "setirizin"), so each word is
    also kept with that s put back; the fold then meets the English spelling.
    """
    from sources.grls_russia import fold

    words = _fold(str(text or "").replace("ı", "i").replace("I", "i").replace("İ", "i")).split()
    tokens: list[str] = []
    for word in words:
        word = TURKISH_WORDS.get(word, word)
        for variant in dict.fromkeys((word, re.sub(r"s(?=[eiy])", "c", word))):
            tokens.extend(fold(token) for token in inn_tokens(variant))
    return list(dict.fromkeys(tokens))


def read_turkey(register: OpenRegister, path: Path) -> Iterator[dict[str, Any]]:
    """One record per licence number and product name, its packs gathered together."""
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook[TURKEY_SHEET] if TURKEY_SHEET in workbook.sheetnames else workbook[workbook.sheetnames[0]]
        rows = sheet.iter_rows(values_only=True)
        header: list[str] = []
        for row in rows:
            cells = [_clean(cell) for cell in row]
            if "ÜRÜN ADI" in cells and "ETKİN MADDE" in cells:
                header = cells
                break
        if not header:
            raise RuntimeError("TITCK's list has changed: no ÜRÜN ADI / ETKİN MADDE header")
        column = {name: header.index(name) for name in header if name}
        suspended_column = next((index for index, name in enumerate(header) if name.startswith("RUHSATI ASKIDA")), None)
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            name = _clean(row[column["ÜRÜN ADI"]]) if len(row) > column["ÜRÜN ADI"] else ""
            if not name:
                continue
            number = _clean(row[column["RUHSAT NUMARASI"]])
            # "ONADRON 0.75 MG TABLET, 100 TABLET": the product, then the pack.
            product, _, pack = name.rpartition(", ") if ", " in name else (name, "", "")
            key = (number, product)
            record = grouped.get(key)
            if record is None:
                record = grouped[key] = {
                    "product": product,
                    "active": _clean(row[column["ETKİN MADDE"]]),
                    "atc": _clean(row[column["ATC KODU"]]),
                    "holder": _clean(row[column["RUHSAT SAHİBİ"]]),
                    "date": row[column["RUHSAT TARİHİ"]],
                    "number": number,
                    "suspended": _clean(row[suspended_column]) if suspended_column is not None else "",
                    "packs": [],
                }
            if pack:
                record["packs"].append(pack)
        yield from grouped.values()
    finally:
        workbook.close()


def build_turkey_rows(records: Iterable[dict[str, Any]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for record in records:
        active = "; ".join(
            part.strip() for part in re.split(r",\s*|\s+ve\s+|\s*\+\s*", record["active"]) if part.strip()
        )
        tokens = turkish_tokens(record["active"] or record["product"])
        row = {
            "substance": active,
            "active_substance": active,
            "source_substance": record["active"],
            "product": record["product"],
            "company": record["holder"],
            "country": "Turkey",
            "region": "ME",
            "status": TURKEY_SUSPENSION.get(record["suspended"], "Licensed"),
            "strength": extract_strength(record["product"]),
            "pack_size": _unique(record["packs"]),
            "atc_code": record["atc"],
            "registration_number": record["number"],
            "registration_date": _date(record["date"]),
            "source": "TITCK Turkey",
            "source_url": TURKEY_PAGE,
            "product_url": "",
            "document_type": "TITCK licensed human medicinal products list",
            "inn_fold_tokens": " ".join(tokens),
            "last_checked": fetched_at,
        }
        yield " " + " ".join(tokens) + " ", row


TITCK_TURKEY = add_register(OpenRegister(
    source="TITCK Turkey",
    country="Turkey",
    region="ME",
    url=TURKEY_PAGE,
    slug="titck_turkey",
    max_age_seconds=7 * 24 * 3600,
    build_rows=build_turkey_rows,
    fetch=fetch_turkey,
    read_records=read_turkey,
))


def _folded_search(register: OpenRegister, substance: str, label: str) -> list[dict[str, Any]]:
    """Rows whose folded INN holds every folded word of the molecule, as Russia's are searched."""
    from sources.grls_russia import fold

    tokens = [fold(token) for token in query_tokens(substance)]
    if not tokens:
        return []
    path = current_index(register)
    if path is None:
        started = refresh_in_background(register)
        raise RegisterNotReady(
            f"{label} is being downloaded for the first time"
            f"{'' if started else ' (already in progress)'}; search again in a few minutes."
        )
    if _is_stale(register, index_info(register)):
        refresh_in_background(register)
    where = " AND ".join("match LIKE ?" for _ in tokens)
    with _connect(path) as conn:
        rows = [json.loads(payload) for (payload,) in conn.execute(
            f"SELECT payload FROM rows WHERE {where}", [f"% {token} %" for token in tokens]
        )]
    rows.sort(key=lambda row: (_status_rank(row), row.get("product", "").lower()))
    bound = register.max_results
    if len(rows) > bound:
        for row in rows[:bound]:
            row["available_total"] = len(rows)
    return rows[:bound]


def run_titck_turkey_search(substance: str) -> list[dict[str, Any]]:
    return _folded_search(TITCK_TURKEY, substance, "TITCK Turkey list")


# ----------------------------------------------------------------- Serbia

SERBIA_URL = "https://www.alims.gov.rs/lekovi/lekovi_humani.csv"
SERBIA_PAGE = "https://www.alims.gov.rs/humani-lekovi/pretrazivanje-humanih-lekova/"
# The CSV has no header row; ALIMS's XLS of the same register names the columns.
SERBIA_COLUMNS = (
    "decision", "name", "inn", "dispensing", "form_strength_pack", "number", "issued", "valid_until",
    "manufacturer", "holder", "atc", "ean", "jkl", "kind", "product_code", "cooperation_code",
    "cooperation", "manufacturer_code", "holder_code", "holder_address", "route",
)
SERBIAN_WORDS = {
    "hidrohlorid": "hydrochloride", "dihidrohlorid": "dihydrochloride", "hlorid": "chloride",
    "natrijum": "sodium", "dinatrijum": "disodium", "kalcijum": "calcium", "kalijum": "potassium",
    "magnezijum": "magnesium", "kiselina": "acid",
}
SERBIAN_ROUTES = {"oralno": "Oral use", "parenteralno": "Parenteral use", "kutano": "Cutaneous use"}


def serbian_tokens(text: object) -> list[str]:
    """Serbian INN words folded the way English ones are.

    Serbian writes an acid as an adjective before "kiselina" ("zoledronska
    kiselina", "acetilsalicilna kiselina"); the adjective ending becomes the
    English -ic. An h stands where English writes ch ("hidrohlorid").
    """
    from sources.grls_russia import fold

    words = _fold(text).split()
    tokens: list[str] = []
    for index, word in enumerate(words):
        if index + 1 < len(words) and words[index + 1] == "kiselina":
            word = re.sub(r"(?:in)?(?:ska|na|ova)$", "", word) + "ic"
        word = SERBIAN_WORDS.get(word, word).replace("hl", "chl")
        tokens.extend(fold(token) for token in inn_tokens(word))
    return list(dict.fromkeys(tokens))


def read_serbia(register: OpenRegister, path: Path) -> Iterator[dict[str, Any]]:
    """One record per decision and product, its packs gathered together.

    ALIMS lists every pack on its own line ("film tableta; 1000mg; blister,
    2x15kom"), and one decision often covers several of them.
    """
    import csv
    import html

    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for values in csv.reader(handle, delimiter=";"):
            if len(values) < len(SERBIA_COLUMNS):
                continue
            record = {name: html.unescape(value) for name, value in zip(SERBIA_COLUMNS, values)}
            form, _, rest = _clean(record["form_strength_pack"]).partition("; ")
            strength, _, pack = rest.partition("; ")
            key = (_clean(record["number"]), _clean(record["name"]), strength, form)
            found = grouped.get(key)
            if found is None:
                found = grouped[key] = {**record, "form": form, "strength": strength, "packs": []}
            if pack:
                found["packs"].append(pack)
    yield from grouped.values()


def _serbian_party(value: str) -> tuple[str, str]:
    """ "INFAI GMBH - Nemačka" -> ("INFAI GMBH", "Nemačka")."""
    name, _, country = _clean(value).rpartition(" - ")
    return (name, country) if name else (_clean(value), "")


def build_serbia_rows(records: Iterable[dict[str, Any]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for record in records:
        inn = _clean(record.get("inn"))
        product = _clean(record.get("name"))
        if not (inn or product):
            continue
        form, strength = record["form"], record["strength"]
        maker, maker_country = _serbian_party(record.get("manufacturer", ""))
        tokens = serbian_tokens(inn or product)
        yield " " + " ".join(tokens) + " ", {
            "substance": "; ".join(part.strip() for part in inn.split(",") if part.strip()),
            "active_substance": inn,
            "source_substance": inn,
            # One registration per pack: the name is shared, the strength and form are not.
            "product": " ".join(part for part in (product, strength, form) if part),
            "company": _clean(record.get("holder")),
            "country": "Serbia",
            "region": "EU",
            "status": "Authorised",
            "authorisation_scope": "Renewal" if record.get("decision") == "OBNOVA" else "Registration",
            "strength": strength,
            "dosage_form": form,
            "pack_size": _unique(record["packs"]),
            "route": SERBIAN_ROUTES.get(_clean(record.get("route")), _clean(record.get("route"))),
            "atc_code": _clean(record.get("atc")),
            "registration_number": _clean(record.get("number")),
            "registration_date": _date(record.get("issued")),
            "expiry_date": _date(record.get("valid_until")),
            "manufacturer_name": maker,
            "manufacturer_country": maker_country,
            "manufacturer_source": "ALIMS register of medicines for human use" if maker else "",
            "source": "ALIMS Serbia",
            "source_url": SERBIA_PAGE,
            "product_url": "",
            "document_type": "ALIMS register of medicines for human use",
            "inn_fold_tokens": " ".join(tokens),
            "last_checked": fetched_at,
        }


ALIMS_SERBIA = add_register(OpenRegister(
    source="ALIMS Serbia",
    country="Serbia",
    region="EU",
    url=SERBIA_URL,
    slug="alims_serbia",
    max_age_seconds=24 * 3600,
    build_rows=build_serbia_rows,
    read_records=read_serbia,
))


def run_alims_serbia_search(substance: str) -> list[dict[str, Any]]:
    return _folded_search(ALIMS_SERBIA, substance, "ALIMS Serbia register")


# ------------------------------------------------------------------ Malta

MALTA_URL = "https://medicinesauthority.gov.mt/Exports/Local/AdvancedSearchResultsLocal.xls"
MALTA_PAGE = "https://medicinesauthority.gov.mt/medicinesdatabase"


def fetch_malta(register: OpenRegister, workdir: Path) -> Path:
    # Named .xls, but a current Excel workbook; openpyxl goes by the extension.
    return download_file(register, register.url, workdir / "malta_medicines.xlsx")


def _malta(value: object) -> str:
    """Every value is wrapped in single quotes: " 'Authorised'"."""
    return _clean(value).strip("'").strip()


def read_malta(register: OpenRegister, path: Path) -> Iterator[dict[str, str]]:
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        rows = workbook[workbook.sheetnames[0]].iter_rows(values_only=True)
        header = [_clean(cell).strip("[] ") for cell in next(rows)]
        for row in rows:
            record = {name: _malta(value) for name, value in zip(header, row)}
            if record.get("Medicine Name"):
                yield record
    finally:
        workbook.close()


def build_malta_rows(records: Iterable[dict[str, str]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for record in records:
        ingredients = [part.strip() for part in record.get("Active Ingredients", "").split("|") if part.strip()]
        # "METFORMIN HYDROCHLORIDE 500 milligram(s)": the substance, then its strength.
        actives = [re.split(r"\s+\d", part, maxsplit=1)[0].strip() for part in ingredients]
        strengths = [part[len(name):].strip() for part, name in zip(ingredients, actives)]
        active = "; ".join(name.capitalize() for name in actives if name)
        product = record.get("Medicine Name", "")
        number = record.get("Authorisation Number", "")
        origin = record.get("Licence Number", "")
        yield _indexed(national_match_text(" ".join(actives) or product), {
            "substance": active,
            "active_substance": active,
            "source_substance": record.get("Active Ingredients", ""),
            "product": product,
            "company": record.get("Authorization Holder", ""),
            "country": "Malta",
            "region": "EU",
            "status": record.get("Status", ""),
            "authorisation_scope": "Parallel import" if number.startswith("PI") else "",
            "strength": " / ".join(strength for strength in strengths if strength),
            "dosage_form": record.get("Pharmaceutical Forms", "").capitalize(),
            "atc_code": record.get("ATC Code", ""),
            "classification": record.get("Classification", ""),
            "therapeutic_category": record.get("Therapeutic Class", "").capitalize(),
            "registration_number": number,
            "registration_date": _date(record.get("Authorisation Date")),
            "source": "Malta Medicines Authority",
            "source_url": MALTA_PAGE,
            "product_url": "",
            "document_type": "Malta Medicines Authority list of authorised medicines"
            + (f" (source licence: {origin})" if origin and origin != "NOT APPLICABLE" else ""),
            "last_checked": fetched_at,
        })


MALTA_MEDICINES = add_register(OpenRegister(
    source="Malta Medicines Authority",
    country="Malta",
    region="EU",
    url=MALTA_URL,
    slug="malta_medicines",
    max_age_seconds=7 * 24 * 3600,
    build_rows=build_malta_rows,
    fetch=fetch_malta,
    read_records=read_malta,
))


def run_malta_medicines_search(substance: str) -> list[dict[str, Any]]:
    return search_register(MALTA_MEDICINES, substance)


# --------------------------------------------------------------- Bulgaria

BULGARIA_PAGE = (
    "https://www.bda.bg/bg/%D1%80%D0%B5%D0%B3%D0%B8%D1%81%D1%82%D1%80%D0%B8/"
    "%D1%80%D0%B5%D0%B3%D0%B8%D1%81%D1%82%D1%80%D0%B8-%D0%BD%D0%B0-"
    "%D0%BB%D0%B5%D0%BA%D0%B0%D1%80%D1%81%D1%82%D0%B2%D0%B5%D0%BD%D0%B8-"
    "%D0%BF%D1%80%D0%BE%D0%B4%D1%83%D0%BA%D1%82%D0%B8"
)
BULGARIAN_COUNTRIES = {
    "България": "Bulgaria", "Германия": "Germany", "Словения": "Slovenia", "Австрия": "Austria",
    "Полша": "Poland", "Кипър": "Cyprus", "Ирландия": "Ireland", "Унгария": "Hungary", "Франция": "France",
    "Чешка Република": "Czech Republic", "Белгия": "Belgium", "Румъния": "Romania", "Латвия": "Latvia",
    "Италия": "Italy", "Испания": "Spain", "Люксембург": "Luxembourg", "Малта": "Malta", "Хърватия": "Croatia",
    "Дания": "Denmark", "Норвегия": "Norway", "Словакия": "Slovakia", "Финландия": "Finland",
    "Гърция": "Greece", "Португалия": "Portugal", "Швеция": "Sweden", "Нидерландия": "Netherlands",
    "Литва": "Lithuania", "Естония": "Estonia", "Исландия": "Iceland", "Швейцария": "Switzerland",
    "Великобритания": "United Kingdom", "Обединено кралство": "United Kingdom", "САЩ": "United States",
    "Индия": "India", "Израел": "Israel", "Турция": "Turkey", "Сърбия": "Serbia",
}
BULGARIA_COLUMNS = {
    "Рег. №": "number", "Търговско име": "name", "Лек. форма EN": "form",
    "Количество на акт.в-во": "strength", "Опаковка": "container", "Обем/Дозова единица": "volume",
    "Количество в крайна опаковка": "count", "Притежател на РУ": "holder", "Държава /BG/": "holder_country",
    "INN": "inn", "АТС-Код": "atc", "Режим на предписване": "prescription",
}


def _bulgaria_session():
    """A session on Python's standard TLS settings.

    bda.bg resets connections made with urllib3's own TLS context, though it
    answers the same request, same headers, over Python's default context (and
    over curl). The client still names itself; only the TLS set-up differs.
    """
    import ssl

    import requests
    from requests.adapters import HTTPAdapter

    class _StandardTLS(HTTPAdapter):
        def init_poolmanager(self, *args: Any, **kwargs: Any) -> Any:
            kwargs["ssl_context"] = ssl.create_default_context()
            return super().init_poolmanager(*args, **kwargs)

    session = requests.Session()
    session.mount("https://", _StandardTLS())
    session.headers["User-Agent"] = "PharmaSearch/1.0 (regulatory register download)"
    return session


def fetch_bulgaria(register: OpenRegister, workdir: Path) -> Path:
    """BDA republishes its register each month under a new name; the page names the latest."""
    from sources.open_registers import DOWNLOAD_TIMEOUT

    session = _bulgaria_session()
    page = session.get(BULGARIA_PAGE, timeout=DOWNLOAD_TIMEOUT)
    page.raise_for_status()
    links = re.findall(r'href="([^"]*IAL_Register_(\d{2})_(\d{4})\.xlsx)"', page.text)
    if not links:
        raise RuntimeError("BDA's page names no IAL register")
    href, _month, _year = max(links, key=lambda link: (link[2], link[1]))
    url = href if href.startswith("http") else "https://www.bda.bg" + href
    destination = workdir / "bda_register.xlsx"
    with session.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                handle.write(chunk)
    return destination


def bulgarian_atc(value: object) -> str:
    """ "J01MA 2" -> "J01MA02": BDA drops the fifth level's leading zero."""
    text = _clean(value)
    found = re.match(r"^([A-Z]\d{2}[A-Z]{2})\s*(\d{1,2})$", text)
    return f"{found.group(1)}{int(found.group(2)):02d}" if found else text


def read_bulgaria(register: OpenRegister, path: Path) -> Iterator[dict[str, Any]]:
    """One record per registration, strength and form, its packs gathered together."""
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        rows = workbook[workbook.sheetnames[0]].iter_rows(values_only=True)
        header = [_clean(cell) for cell in next(rows)]
        missing = [name for name in BULGARIA_COLUMNS if name not in header]
        if missing:
            raise RuntimeError(f"BDA's register has changed: no {', '.join(missing)} column")
        position = {field: header.index(name) for name, field in BULGARIA_COLUMNS.items()}
        grouped: dict[tuple[str, ...], dict[str, Any]] = {}
        for row in rows:
            record = {field: row[index] if index < len(row) else None for field, index in position.items()}
            if not record["number"]:
                continue
            key = (_clean(record["number"]), _clean(record["strength"]), _clean(record["form"]), _clean(record["name"]))
            found = grouped.get(key)
            if found is None:
                found = grouped[key] = {**record, "packs": []}
            pack = " ".join(_clean(part) for part in (record["container"], record["volume"]) if _clean(part))
            count = _clean(record["count"])
            found["packs"].append(f"{pack} x {count}".strip(" x") if count else pack)
        yield from grouped.values()
    finally:
        workbook.close()


def build_bulgaria_rows(records: Iterable[dict[str, Any]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for record in records:
        inn = _clean(record.get("inn"))
        name = _clean(record.get("name"))
        if not (inn or name):
            continue
        strength = _clean(record.get("strength"))
        active = "; ".join(part.strip() for part in re.split(r"\s+and\s+|,\s*", inn) if part.strip())
        country = _clean(record.get("holder_country"))
        yield _indexed(national_match_text(f"{inn} {name}"), {
            "substance": active,
            "active_substance": active,
            "source_substance": inn,
            "product": f"{name} {strength}".strip(),
            "company": _clean(record.get("holder")),
            "country": "Bulgaria",
            "region": "EU",
            "status": "Authorised",
            "strength": strength,
            "dosage_form": _clean(record.get("form")).capitalize(),
            "pack_size": _unique(record["packs"]),
            "atc_code": bulgarian_atc(record.get("atc")),
            "classification": _clean(record.get("prescription")),
            "registration_number": _clean(record.get("number")),
            "source": "BDA Bulgaria",
            "source_url": BULGARIA_PAGE,
            "product_url": "",
            "document_type": "BDA register of medicinal products authorised for use in Bulgaria"
            + (f" (holder in {BULGARIAN_COUNTRIES.get(country, country)})" if country else ""),
            "last_checked": fetched_at,
        })


BDA_BULGARIA = add_register(OpenRegister(
    source="BDA Bulgaria",
    country="Bulgaria",
    region="EU",
    url=BULGARIA_PAGE,
    slug="bda_bulgaria",
    max_age_seconds=7 * 24 * 3600,
    build_rows=build_bulgaria_rows,
    fetch=fetch_bulgaria,
    read_records=read_bulgaria,
))


def run_bda_bulgaria_search(substance: str) -> list[dict[str, Any]]:
    return search_register(BDA_BULGARIA, substance)
