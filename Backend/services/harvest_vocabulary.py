"""The molecule list that drives a source-wide harvest.

Only a handful of registries hand over their whole catalogue in one request.
The other connectors are search endpoints: they answer a molecule and nothing
else, so harvesting them without the user typing anything means supplying the
molecules ourselves.

The list is assembled from what we can already reach:

* the openFDA label facet, which returns the substance names of every US label
  ranked by how many labels carry them, in a single request;
* the cached CDSCO corpus, which is the Indian catalogue and names molecules
  that never reach a US label;
* the substances already sitting in product_details, so anything a user has
  searched stays covered.

Each entry keeps the sources it came from and an evidence count, so a harvest
can run the best-attested molecules first and stop wherever the operator wants
rather than having to finish the whole list to be useful.
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import requests

from config import BASE_DIR, DB_PATH
from core.logging_config import get_logger
from services.english_normalizer import _strip_latin_accents


OPENFDA_FACET_URL = "https://api.fda.gov/drug/label.json"
OPENFDA_FACET_LIMIT = 1000
REQUEST_TIMEOUT = 60

CDSCO_CORPUS_PATH = BASE_DIR / "data" / "cdsco_india_approvals.json"
VOCABULARY_PATH = BASE_DIR / "data" / "harvest_vocabulary.json"

logger = get_logger(__name__)

# Salt, hydrate and ester words attached to a molecule, plus the pharmacopoeia
# marks Indian records carry. Stripped only in trailing position: "diclofenac
# sodium" is diclofenac, but "sodium chloride" is its own molecule.
MODIFIER_WORDS = {
    "acetate", "anhydrous", "besilate", "besylate", "bp", "bromide", "calcium",
    "camsylate", "carbonate", "chloride", "citrate", "decanoate", "dihydrate",
    "dihydrochloride", "magnesium",
    "dipropionate", "disodium", "enantate", "enanthate", "esylate", "ep",
    "fumarate", "furoate", "gluconate", "hbr", "hcl", "hemihydrate", "hydrate",
    "hydrobromide", "hydrochloride", "ip", "lactate", "maleate", "malate",
    "mesilate", "mesylate", "monohydrate", "napsylate", "nitrate", "oxalate",
    "palmitate", "pamoate", "phosphate", "potassium", "propanediol", "propionate",
    "sodium", "succinate", "sulfate", "sulphate", "tartrate", "tosylate",
    "trihydrate", "usp", "valerate",
}

# Words that mark a composition string as describing a quantity or an
# equivalence rather than naming a molecule.
NOISE_WORDS = {"and", "eq", "equivalent", "to", "qs", "q.s", "na", "each", "per"}

# Composition fields list excipients, containers and dosage forms beside the
# active ingredients, and splitting a combination leaves salt fragments behind.
# None of them is a molecule any registry will answer a search for. Substances
# that are both excipient and drug -- mannitol, dextrose, sodium chloride --
# stay in: they are genuinely registered products somewhere.
NON_MOLECULE_TERMS = {
    "ampoule", "anhydrous", "bulk", "capsule", "capsules", "concentrate",
    "dibasic", "diluent", "heptahydrate", "injection", "l-histidine",
    "monobasic", "pentahydrate", "polysorbate 20", "polysorbate 80",
    "polyethylene glycol 400", "polyethylene glycol 3350", "silicon dioxide",
    "colloidal silicon dioxide", "solution", "sterile water", "suspension",
    "syrup", "tablet", "tablets", "vial", "water", "water for injection",
}

COMBINATION_SEPARATORS = re.compile(r"\s*(?:\+|/|,|;| and | with )\s*", flags=re.IGNORECASE)
# Indian records state the salt they weigh, then the molecule it is equivalent
# to: "Amoxycillin Trihydrate IP eq. To Amoxycillin". The molecule is the part
# after the equivalence, and it is already the plain name we want.
EQUIVALENCE_PATTERN = re.compile(r"\b(?:eq|equivalent)\b\.?\s*(?:to\b)?", flags=re.IGNORECASE)
STRENGTH_PATTERN = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:mg|g|mcg|ug|ml|l|iu|units?|%|milligram|microgram|gram)\b\.?",
    flags=re.IGNORECASE,
)
PARENTHETICAL_PATTERN = re.compile(r"\([^)]*\)")
MOLECULE_PATTERN = re.compile(r"^[a-z][a-z0-9\- ']{2,59}$")

MIN_LENGTH = 3
MAX_WORDS = 4


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def normalize_molecule(value: object) -> str:
    """Reduce a registry's substance string to the molecule it names.

    Registry substance fields carry strengths, pharmacopoeia marks, salt forms
    and parenthetical qualifiers in no consistent order, so "Dapagliflozin
    Propanediol Monohydrate IP eq. To Dapagliflozin 10.0000 Milligram (Mg)" and
    "DAPAGLIFLOZIN" have to end up as the same harvest term.
    """
    text = _clean(value).lower()
    if not text:
        return ""
    text = EQUIVALENCE_PATTERN.split(text)[-1]
    text = PARENTHETICAL_PATTERN.sub(" ", text)
    text = STRENGTH_PATTERN.sub(" ", text)
    text = re.sub(r"[.;:]+", " ", text)
    words = [word for word in text.split() if word and word not in NOISE_WORDS]
    # Trailing modifiers only; a leading one is part of the name. Stop before
    # the last word, or "sodium chloride" would be reduced to sodium.
    while len(words) > 1 and words[-1] in MODIFIER_WORDS:
        if len(words) == 2 and words[0] in MODIFIER_WORDS:
            break
        words.pop()
    molecule = " ".join(words).strip(" -'")
    if len(molecule) < MIN_LENGTH or len(molecule.split()) > MAX_WORDS:
        return ""
    if not MOLECULE_PATTERN.match(molecule):
        return ""
    if molecule in NON_MOLECULE_TERMS:
        return ""
    return molecule


def split_combination(value: object) -> list[str]:
    """Split a fixed-dose combination into the molecules it contains.

    A combination is worth harvesting as its parts: every registry indexes the
    single molecules, and few index the same combination under the same name.
    """
    parts = COMBINATION_SEPARATORS.split(_clean(value))
    molecules = []
    for part in parts:
        molecule = normalize_molecule(part)
        if molecule and molecule not in molecules:
            molecules.append(molecule)
    return molecules


class _Accumulator:
    """Collects molecules with the sources and evidence that back them."""

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}

    def add(self, value: object, origin: str, evidence: int = 1) -> None:
        for molecule in split_combination(value):
            entry = self._entries.setdefault(
                molecule, {"molecule": molecule, "origins": [], "evidence": 0}
            )
            if origin not in entry["origins"]:
                entry["origins"].append(origin)
            entry["evidence"] += max(1, evidence)

    def entries(self) -> list[dict[str, Any]]:
        """Best-attested molecules first, so a partial run is still the most
        useful part of the list.

        Molecules confirmed by more than one registry rank above ones seen in a
        single place, ahead of raw label counts: the US label facet is topped by
        sunscreen and OTC monograph ingredients, which would otherwise fill the
        front of a harvest that stops early.
        """
        return sorted(
            self._entries.values(),
            key=lambda entry: (-len(entry["origins"]), -entry["evidence"], entry["molecule"]),
        )


def openfda_terms() -> list[tuple[str, int]]:
    """Substance names across every US label, with their label counts."""
    response = requests.get(
        OPENFDA_FACET_URL,
        params={"count": "openfda.substance_name.exact", "limit": OPENFDA_FACET_LIMIT},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    results = response.json().get("results") or []
    return [(str(row.get("term") or ""), int(row.get("count") or 0)) for row in results]


def cdsco_terms(path: Path = CDSCO_CORPUS_PATH) -> list[str]:
    """Molecules named by the cached Indian approvals corpus."""
    if not path.exists():
        logger.info("CDSCO corpus not cached yet; skipping it as a vocabulary source")
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records") if isinstance(payload, dict) else payload
    terms = []
    for record in records or []:
        for field in ("str_drug_name", "str_composition"):
            value = _clean(record.get(field))
            if value and value.upper() != "NA":
                terms.append(value)
    return terms


def database_terms(db_path: Path = DB_PATH) -> list[str]:
    """Substances already searched, so nothing a user cares about drops out."""
    if not Path(db_path).exists():
        return []
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT DISTINCT substance FROM product_details WHERE substance <> ''"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        logger.warning("Could not read substances from the database: %s", exc)
        return []
    finally:
        connection.close()
    return [str(row[0]) for row in rows]


def build_vocabulary(include_openfda: bool = True) -> list[dict[str, Any]]:
    """Assemble the harvest vocabulary from every reachable source."""
    accumulator = _Accumulator()

    if include_openfda:
        try:
            for term, count in openfda_terms():
                # The label count is real evidence of how widely a molecule is
                # marketed, so it carries straight into the ordering.
                accumulator.add(term, "openfda", evidence=count)
        except requests.RequestException as exc:
            logger.warning("openFDA substance facet unavailable: %s", exc)

    for term in cdsco_terms():
        accumulator.add(term, "cdsco")
    for term in database_terms():
        accumulator.add(term, "database")

    return accumulator.entries()


def write_vocabulary(
    entries: Iterable[dict[str, Any]] | None = None, path: Path = VOCABULARY_PATH
) -> Path:
    """Persist the vocabulary so a harvest run does not rebuild it."""
    payload = list(entries) if entries is not None else build_vocabulary()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    logger.info("Wrote %s harvest molecules to %s", len(payload), path)
    return path


def load_vocabulary(path: Path = VOCABULARY_PATH, limit: int | None = None) -> list[str]:
    """The molecule names to harvest, best-attested first."""
    if not path.exists():
        write_vocabulary(path=path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    molecules = [str(entry.get("molecule") or "") for entry in payload]
    molecules = [molecule for molecule in molecules if molecule]
    return molecules[:limit] if limit else molecules



def molecule_group_key(substance: object) -> str:
    """The key that decides which rows describe the same molecule.

    Registries write the substance in their own language and register, so
    grouping on the plain name splits a molecule across several groups and each
    fragment ends up with nobody to borrow from. France files IBUPROFÈNE, Spain
    files combinations joined by semicolons, and the Latin INN carries a final
    "e" that English drops -- amoxicilline against amoxicillin.

    Accents are stripped and a trailing "e" is dropped from the stem. That rule
    is applied to every name alike, so it does not matter that "omeprazol" is
    nobody's spelling: both spellings reach it, which is all a grouping key has
    to do.

    A combination is keyed on all of its molecules, sorted, joined by "+". It
    used to be keyed on its first molecule, which put Janumet in metformin's
    group: plain metformin rows could inherit the combination's A10BD07, and a
    combination with no code of its own inherited metformin's A10BA02. Registries
    also list one molecule's salt beside it -- "ATORVASTATINE, ATORVASTATINE
    CALCIUM" -- and that is still one molecule.
    """
    stems = _molecule_stems(substance)
    if not stems:
        return _molecule_stem(_clean(substance))
    return "+".join(stems)


def _molecule_stem(molecule: str) -> str:
    stem = _strip_latin_accents(molecule).lower().strip()
    # Romance registers name the salt first: "chlorhydrate de metformine" is
    # metformin's, and the molecule is whatever follows the last "de".
    if " de " in stem:
        stem = stem.rsplit(" de ", 1)[1].strip()
    return " ".join(
        word[:-1] if len(word) > 5 and word.endswith("e") else word
        for word in stem.split()
    )


def _molecule_stems(substance: object) -> list[str]:
    """The distinct molecules a substance string names, as sorted stems."""
    stems: list[str] = []
    for molecule in split_combination(_strip_latin_accents(_clean(substance))):
        words = _molecule_stem(molecule).split()
        if not words:
            continue
        merged = False
        for index, other in enumerate(stems):
            other_words = other.split()
            # A salt or form written after the molecule is the same molecule:
            # keep the shorter name of the two.
            shorter, longer = sorted((words, other_words), key=len)
            if longer[: len(shorter)] == shorter:
                stems[index] = " ".join(shorter)
                merged = True
                break
        if not merged:
            stems.append(" ".join(words))
    return sorted(set(stems))


def is_combination(substance: object) -> bool:
    return len(_molecule_stems(substance)) > 1


def row_molecule_key(row: dict[str, Any]) -> str:
    """The molecule key for a stored or live row.

    A search stores the term that was typed as the row's substance and keeps
    what the registry said in source_substance, so a search for metformin files
    Janumet under "metformin". The registry's own statement decides whether the
    product is a combination; otherwise the substance is kept, because it is
    what groups a molecule across the registries' languages.
    """
    substance = row.get("substance")
    stated = row.get("source_substance")
    if stated and _molecule_stems(stated):
        if is_combination(stated) or is_combination(substance):
            return molecule_group_key(stated)
    return molecule_group_key(substance)


if __name__ == "__main__":
    write_vocabulary()
