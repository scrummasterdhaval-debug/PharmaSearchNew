"""Romanian national medicines register (ANMDMR nomenclator).

The nomenclator renders one row per authorised pack, and carries every field
we need in ``data-`` attributes on the row's details button, so no per-product
request is needed. Notably it publishes the manufacturer (``data-firmtarp``)
separately from the marketing authorisation holder (``data-firmtard``), which
most registries do not.
"""

import re

import requests
from bs4 import BeautifulSoup

from core.logging_config import get_logger
from sources.parser import extract_strength


BASE_URL = "https://nomenclator.anm.ro"
SEARCH_URL = f"{BASE_URL}/medicamente"
REQUEST_TIMEOUT = 30
MAX_RESULTS = 200
logger = get_logger(__name__)

# The register abbreviates the pharmaceutical form ("COMPR. FILM."), and also
# spells some out in full. Longest key first, so the qualified forms win.
DOSAGE_FORM_TRANSLATIONS = {
    "COMPR. PT. DISPERSIE ORALA/ORODISPERSABILE": "Orodispersible tablet",
    "CONC. PT. SOL. INJ./PERF.": "Concentrate for solution for injection/infusion",
    "COMPRIMATE CU ELIBERARE PRELUNGITA": "Prolonged-release tablet",
    "COMPRIMATE CU ELIBERARE MODIFICATA": "Modified-release tablet",
    "CONC. SI SOLV. PT. SOL. ORALA": "Concentrate and solvent for oral solution",
    "GRANULE PT. SUSP. ORALA IN PLIC": "Granules for oral suspension in sachet",
    "GRANULE PT. SOL. ORALA IN PLIC": "Granules for oral solution in sachet",
    "PULBERE PENTRU SOLUTIE INJECTABILA": "Powder for solution for injection",
    "PULB. PT. SUSP. ORALA IN PLIC": "Powder for oral suspension in sachet",
    "COMPRIMATE GASTROREZISTENTE": "Gastro-resistant tablet",
    "PULB. PT. SOL. ORALA IN PLIC": "Powder for oral solution in sachet",
    "PULB. PT. SOL. INJ./PERF.": "Powder for solution for injection/infusion",
    "CAPSULE GASTROREZISTENTE": "Gastro-resistant capsule",
    "COMPRIMATE ORODISPERSABILE": "Orodispersible tablet",
    "GRAN. PT. SOL. ORALA": "Granules for oral solution",
    "PULB. PT. SUSP. ORALA": "Powder for oral suspension",
    "COMPR. CU ELIB. PREL.": "Prolonged-release tablet",
    "COMPR. CU ELIB. MODIF.": "Modified-release tablet",
    "CAPS. MOI MASTICABILE": "Chewable soft capsule",
    "COMPRIMATE EFERVESCENTE": "Effervescent tablet",
    "PULB. PT. SOL. ORALA": "Powder for oral solution",
    "COMPRIMATE MASTICABILE": "Chewable tablet",
    "COMPR. GASTROREZ.": "Gastro-resistant tablet",
    "CAPS. GASTROREZ.": "Gastro-resistant capsule",
    "SOLUTIE INJECTABILA": "Solution for injection",
    "COMPRIMATE FILMATE": "Film-coated tablet",
    "CONC. PT. SOL. ORALA": "Concentrate for oral solution",
    "SOL. INJ./PERF.": "Solution for injection/infusion",
    "GRANULE EFF.": "Effervescent granules",
    "COMPR. FILM.": "Film-coated tablet",
    "SUSPENSIE ORALA": "Oral suspension",
    "COMPR. MAST.": "Chewable tablet",
    "SOLUTIE ORALA": "Oral solution",
    "SUSP. ORALA": "Oral suspension",
    "SOL. INJ.": "Solution for injection",
    "COMPR. EFF.": "Effervescent tablet",
    "SUPOZITOARE": "Suppository",
    "SOL. ORALA": "Oral solution",
    "SPRAY NAZAL": "Nasal spray",
    "SOL. PERF.": "Solution for infusion",
    "COMPRIMATE": "Tablet",
    "COMPRIMAT": "Tablet",
    "CAPS. MOI": "Soft capsule",
    "CAPSULE": "Capsule",
    "UNGUENT": "Ointment",
    "PICATURI": "Drops",
    "PULBERE": "Powder",
    "SOLUTIE": "Solution",
    "COMPR.": "Tablet",
    "SUPOZ.": "Suppository",
    "GRANULE": "Granules",
    "DRAJ.": "Coated tablet",
    "CAPS.": "Capsule",
    "CREMA": "Cream",
    "SIROP": "Syrup",
    "GEL": "Gel",
}

COUNTRY_TRANSLATIONS = {
    "ROMANIA": "Romania",
    "GERMANIA": "Germany",
    "ITALIA": "Italy",
    "FRANTA": "France",
    "SPANIA": "Spain",
    "UNGARIA": "Hungary",
    "OLANDA": "Netherlands",
    "MAREA BRITANIE": "United Kingdom",
    "MARFA BRITANIE": "United Kingdom",
    "AUSTRIA": "Austria",
    "BELGIA": "Belgium",
    "BULGARIA": "Bulgaria",
    "CEHIA": "Czech Republic",
    "CIPRU": "Cyprus",
    "CROATIA": "Croatia",
    "DANEMARCA": "Denmark",
    "ELVETIA": "Switzerland",
    "ESTONIA": "Estonia",
    "FINLANDA": "Finland",
    "GRECIA": "Greece",
    "IRLANDA": "Ireland",
    "ISLANDA": "Iceland",
    "LETONIA": "Latvia",
    "LITUANIA": "Lithuania",
    "LUXEMBURG": "Luxembourg",
    "MALTA": "Malta",
    "NORVEGIA": "Norway",
    "POLONIA": "Poland",
    "PORTUGALIA": "Portugal",
    "SLOVACIA": "Slovakia",
    "SLOVENIA": "Slovenia",
    "SUEDIA": "Sweden",
    "S.U.A.": "United States",
    "SUA": "United States",
    "CANADA": "Canada",
    "INDIA": "India",
    "CHINA": "China",
    "JAPONIA": "Japan",
    "ISRAEL": "Israel",
    "TURCIA": "Turkey",
    "COREEA DE SUD": "South Korea",
}


def _translated(value, table):
    text = " ".join(str(value or "").strip().upper().split())
    if not text:
        return ""
    for source, target in sorted(table.items(), key=lambda item: len(item[0]), reverse=True):
        if text.startswith(source):
            return target
    return str(value or "").strip()


def _split_company(value):
    """ANMDMR writes companies as "NAME - COUNTRY"."""
    text = " ".join(str(value or "").strip().split())
    if not text:
        return "", ""
    name, _, country = text.rpartition(" - ")
    if not name:
        return text, ""
    return name.strip(), _translated(country, COUNTRY_TRANSLATIONS)


def romanian_search_terms(substance):
    """The register lists Latin INNs (TRIAMCINOLONUM) and matches on a prefix,
    so an English name ending in "e" has to lose it to match."""
    original = substance.strip()
    terms = [original]
    trimmed = re.sub(r"e$", "", original, flags=re.IGNORECASE)
    if trimmed and trimmed.lower() != original.lower():
        terms.append(trimmed)
    return terms


def _open_session():
    """The search form's CSRF token is only valid for the session that was
    issued it, so the token and the query must share one cookie jar."""
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    response = session.get(SEARCH_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    field = soup.find("input", {"name": "form[_token]"})
    return session, field.get("value") if field else ""


def _record(button, substance):
    data = button.attrs
    product = str(data.get("data-dencom") or "").strip()
    if not product:
        return None
    holder, holder_country = _split_company(data.get("data-firmtard"))
    manufacturer, manufacturer_country = _split_company(data.get("data-firmtarp"))
    registration_number = str(data.get("data-nrdtamb") or "").strip()
    cim_code = str(data.get("data-cim") or "").strip()
    return {
        "substance": substance,
        "source_substance": str(data.get("data-dci") or "").strip(),
        "product": product,
        "company": holder,
        "manufacturer_name": manufacturer,
        # The holder's country is not evidence of the producer's country.
        "manufacturer_country": manufacturer_country,
        "manufacturer_source": "ANMDMR nomenclator producer field",
        "manufacturers": ([{
            "name": manufacturer,
            "country": manufacturer_country,
            "role": "MANUFACTURER_UNKNOWN_ROLE",
            "verification_status": "VERIFIED_REGULATOR_RECORD",
        }] if manufacturer else []),
        "country": "Romania",
        "region": "EU",
        "status": "Authorised",
        "strength": str(data.get("data-conc") or "").strip() or extract_strength(product),
        "dosage_form": _translated(data.get("data-formafarm"), DOSAGE_FORM_TRANSLATIONS),
        "pack_size": str(data.get("data-ambalaj") or "").strip(),
        "atc_code": str(data.get("data-codatc") or "").strip(),
        "therapeutic_category": str(data.get("data-actter") or "").strip().title(),
        "registration_number": registration_number or cim_code,
        "source": "ANMDMR Romania",
        "source_url": SEARCH_URL,
        "product_url": f"{SEARCH_URL}?form%5Bcim%5D={cim_code}" if cim_code else SEARCH_URL,
        "url": f"{SEARCH_URL}?form%5Bcim%5D={cim_code}" if cim_code else SEARCH_URL,
        "smpc_url": str(data.get("data-linkrcp") or "").strip(),
        "pil_url": str(data.get("data-linkpro") or "").strip(),
    }


def _search_term(session, token, term, substance, limit):
    params = {
        "form[denCom]": "",
        "form[dci]": term,
        "form[formaFarm]": "",
        "form[codAtc]": "",
        "form[cim]": "",
        "form[firmTarD]": "",
        "form[order]": "",
        "form[direction]": "",
        "form[limit]": str(limit),
        "form[_token]": token,
    }
    response = session.get(SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    records = []
    for button in soup.select("table tbody tr button[data-cim]"):
        record = _record(button, substance)
        if record:
            records.append(record)
        if len(records) >= limit:
            break
    return records


PACK_NUMBER = re.compile(r"^(?P<base>.+?/\d{4})/(?P<pack>\d{1,3})$")
PACK_COUNT = re.compile(
    r"^(?P<head>.*?\bx\s*)(?P<count>\d+(?:[.,]\d+)?(?:x\d+)?)(?P<tail>\s*\D.*)?$", re.IGNORECASE
)


def _pack_label(values):
    """Pack descriptions for one product, as one line.

    "Cutie cu blist. PVC-PVDC/Al x 10 compr. film." and the same with 12 and 14
    read as "Cutie cu blist. PVC-PVDC/Al x 10 / 12 / 14 compr. film.". Packs of
    another kind -- unit-dose blisters -- get their own line of the same shape,
    and a pack that fits no pattern is listed as it is.
    """
    values = list(dict.fromkeys(value for value in values if value))
    kinds: dict[tuple[str, str], list[str]] = {}
    loose = []
    for value in values:
        match = PACK_COUNT.match(value)
        if not match:
            loose.append(value)
            continue
        kinds.setdefault((match["head"], (match["tail"] or "").strip()), []).append(match["count"])
    lines = [
        f"{head}{' / '.join(counts)} {tail}".strip()
        for (head, tail), counts in kinds.items()
    ]
    return "; ".join(lines + loose)


def _pack_numbers(base, packs):
    """16519/2026 with packs 01..11 -> "16519/2026/01-11"; gaps are listed."""
    numbers = sorted(packs, key=int)
    if len(numbers) == 1:
        return f"{base}/{numbers[0]}"
    ints = [int(number) for number in numbers]
    if ints == list(range(ints[0], ints[-1] + 1)):
        return f"{base}/{numbers[0]}-{numbers[-1]}"
    return f"{base}/{', '.join(numbers)}"


def fold_pack_registrations(records):
    """One row per product, not one per pack.

    ANMDMR registers every pack of a product under its own number --
    16519/2026/01 for 10 tablets, /02 for 12, and so on -- so one medicine
    filled a result page with rows that differed only in pack size. Packs of
    the same product, strength, form and holder are folded into one row that
    lists every pack and every pack number. Nothing is dropped.
    """
    groups = {}
    order = []
    for record in records:
        match = PACK_NUMBER.match(str(record.get("registration_number") or "").strip())
        base = match["base"] if match else str(record.get("registration_number") or "")
        key = (
            str(record.get("product") or "").strip().lower(),
            str(record.get("strength") or "").strip().lower(),
            str(record.get("dosage_form") or "").strip().lower(),
            str(record.get("company") or "").strip().lower(),
            base,
        )
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((match["pack"] if match else "", record))

    folded = []
    for key in order:
        members = sorted(groups[key], key=lambda item: int(item[0]) if item[0] else 0)
        packs = [pack for pack, _record in members if pack]
        first = dict(members[0][1])
        if len(members) > 1 and len(packs) == len(members):
            first["registration_number"] = _pack_numbers(key[4], packs)
            first["pack_size"] = _pack_label(record.get("pack_size") for _pack, record in members)
            for field in ("smpc_url", "pil_url"):
                if not first.get(field):
                    first[field] = next((r.get(field) for _p, r in members if r.get(field)), "")
        folded.append(first)
    return folded


def run_romania_anmdmr_search(substance, limit=MAX_RESULTS):
    if not substance.strip():
        return []
    try:
        session, token = _open_session()
    except requests.RequestException as exc:
        logger.warning("ANMDMR Romania unavailable: %s", exc)
        return []
    for term in romanian_search_terms(substance):
        try:
            records = _search_term(session, token, term, substance, limit)
        except (requests.RequestException, ValueError) as exc:
            logger.warning("ANMDMR Romania search failed for %s: %s", term, exc)
            return []
        if records:
            if term != substance.strip():
                logger.info("ANMDMR matched %s using Romanian term %s", substance, term)
            return fold_pack_registrations(records)
    return []
