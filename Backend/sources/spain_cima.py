from datetime import datetime, timezone
import re
import unicodedata
from urllib.parse import quote

import requests

from core.logging_config import get_logger
from sources.parser import extract_dosage_form, extract_pack_size, extract_strength


CIMA_SEARCH_URL = "https://cima.aemps.es/cima/rest/medicamentos"
CIMA_DETAIL_URL = "https://cima.aemps.es/cima/rest/medicamento"
CIMA_BASE_URL = "https://cima.aemps.es/cima"
CIMA_PAGE_SIZE = 200
CIMA_MAX_RESULTS = 1000
DETAIL_TIMEOUT = 10
PACK_SIZE_PATTERN = re.compile(
    r",\s*\d+(?:[.,]\d+)?\s*(?:x\s*\d+\s*)?"
    r"(?:comprimidos?|c[aá]psulas?|sobres?|viales?|ampollas?|jeringas?|parches?"
    r"|frascos?|bolsas?|envases?|unidades?|ml|mg|g)\b[^,]*",
    flags=re.IGNORECASE,
)
logger = get_logger(__name__)


def _document_urls(docs):
    urls = {"smpc_url": "", "pil_url": ""}
    for doc in docs or []:
        doc_type = doc.get("tipo")
        url = doc.get("url") or doc.get("urlHtml") or ""
        if doc_type == 1:
            urls["smpc_url"] = url
        elif doc_type == 2:
            urls["pil_url"] = url
    return urls


def _status(row):
    if row.get("comerc") is True:
        return "Marketed"
    if row.get("estado"):
        return "Authorised"
    return ""


def _epoch_date(value):
    """CIMA reports dates as epoch milliseconds."""
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _authorisation_date(row):
    estado = row.get("estado") or {}
    if not isinstance(estado, dict):
        return ""
    return _epoch_date(estado.get("aut"))


def _named_value(row, field):
    value = row.get(field)
    if isinstance(value, dict):
        return str(value.get("nombre") or "").strip()
    return str(value or "").strip()


# CIMA names the pharmaceutical form in Spanish. Longest form first, so
# "CAPSULA DURA GASTRORRESISTENTE" is not shortened to "Hard capsule".
DOSAGE_FORM_TRANSLATIONS = {
    "COMPRIMIDO DE LIBERACION PROLONGADA": "Prolonged-release tablet",
    "COMPRIMIDO DE LIBERACION MODIFICADA": "Modified-release tablet",
    "COMPRIMIDO RECUBIERTO CON PELICULA": "Film-coated tablet",
    "CAPSULA DURA DE LIBERACION PROLONGADA": "Prolonged-release hard capsule",
    "CAPSULA DURA GASTRORRESISTENTE": "Gastro-resistant hard capsule",
    "COMPRIMIDO GASTRORRESISTENTE": "Gastro-resistant tablet",
    "COMPRIMIDO BUCODISPERSABLE": "Orodispersible tablet",
    "COMPRIMIDO EFERVESCENTE": "Effervescent tablet",
    "COMPRIMIDO MASTICABLE": "Chewable tablet",
    "COMPRIMIDO RECUBIERTO": "Coated tablet",
    "POLVO PARA SOLUCION INYECTABLE": "Powder for solution for injection",
    "POLVO PARA SUSPENSION ORAL": "Powder for oral suspension",
    "SOLUCION PARA PERFUSION": "Solution for infusion",
    "SOLUCION INYECTABLE": "Solution for injection",
    "PARCHE TRANSDERMICO": "Transdermal patch",
    "SUSPENSION ORAL": "Oral suspension",
    "CAPSULA BLANDA": "Soft capsule",
    "SOLUCION ORAL": "Oral solution",
    "CAPSULA DURA": "Hard capsule",
    "SUPOSITORIO": "Suppository",
    "COMPRIMIDO": "Tablet",
    "GRANULADO": "Granules",
    "EMULSION": "Emulsion",
    "CAPSULA": "Capsule",
    "SOLUCION": "Solution",
    "POMADA": "Ointment",
    "COLIRIO": "Eye drops",
    "JARABE": "Syrup",
    "CREMA": "Cream",
    "POLVO": "Powder",
    "GEL": "Gel",
}


def _english_dosage_form(value):
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text.upper())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = " ".join(normalized.split())
    for spanish, english in sorted(
        DOSAGE_FORM_TRANSLATIONS.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if normalized.startswith(spanish):
            return english
    return text.capitalize()


def _extract_record(row, substance):
    docs = _document_urls(row.get("docs"))
    registration_number = str(row.get("nregistro") or "")
    product_url = f"{CIMA_BASE_URL}/dochtml/ft/{registration_number}/FT_{registration_number}.html"
    if not docs.get("smpc_url"):
        product_url = f"{CIMA_BASE_URL}/medicamento/{registration_number}"
    product = row.get("nombre", "")
    holder = str(row.get("labtitular") or "").strip()
    marketer = str(row.get("labcomercializador") or "").strip()
    return {
        "substance": substance,
        "product": product,
        "company": holder or marketer,
        "commercial_company": marketer,
        "country": "Spain",
        "region": "EU",
        "status": _status(row),
        "strength": str(row.get("dosis") or "").strip() or extract_strength(product),
        "dosage_form": _english_dosage_form(
            _named_value(row, "formaFarmaceutica")
            or _named_value(row, "formaFarmaceuticaSimplificada")
        )
        or extract_dosage_form(product),
        "pack_size": extract_pack_size(product),
        "route": _first_named(row.get("viasAdministracion")),
        "registration_number": registration_number,
        "registration_date": _authorisation_date(row),
        "source": "Spain CIMA",
        "source_url": f"{CIMA_SEARCH_URL}?practiv1={quote(substance)}",
        "product_url": product_url,
        "url": product_url,
        "smpc_url": docs.get("smpc_url", ""),
        "pil_url": docs.get("pil_url", ""),
    }


def _first_named(values):
    for value in values or []:
        name = str((value or {}).get("nombre") or "").strip()
        if name:
            return name
    return ""


def _best_atc_code(atcs):
    """CIMA returns the ATC hierarchy; the deepest level is the product ATC."""
    best_code = ""
    best_level = -1
    for entry in atcs or []:
        code = str((entry or {}).get("codigo") or "").strip()
        try:
            level = int((entry or {}).get("nivel") or 0)
        except (TypeError, ValueError):
            level = len(code)
        if code and level > best_level:
            best_code = code
            best_level = level
    return best_code


def _spanish_pack_size(name):
    """CIMA presentation names end with the pack quantity, e.g. ", 50 comprimidos"."""
    match = PACK_SIZE_PATTERN.search(str(name or ""))
    if match:
        return " ".join(match.group(0).strip(" ,").split())
    return extract_pack_size(name)


def _detail_pack_size(presentaciones):
    sizes = []
    for entry in presentaciones or []:
        name = str((entry or {}).get("nombre") or "").strip()
        pack = _spanish_pack_size(name)
        if pack and pack not in sizes:
            sizes.append(pack)
    return "; ".join(sizes[:3])


def fetch_cima_detail(registration_number):
    """Fetch the per-product record that carries ATC codes and pack sizes."""
    if not registration_number:
        return {}
    try:
        response = requests.get(
            CIMA_DETAIL_URL,
            params={"nregistro": registration_number},
            timeout=DETAIL_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.info("Spain CIMA detail lookup failed for %s: %s", registration_number, exc)
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "atc_code": _best_atc_code(payload.get("atcs")),
        "pack_size": _detail_pack_size(payload.get("presentaciones")),
        "registration_date": _authorisation_date(payload),
    }


def spanish_search_terms(substance):
    """CIMA indexes substances under their Spanish INN, and matches on a prefix.

    English INNs that only differ by spelling therefore find nothing:
    "omeprazole" misses "omeprazol", "amoxicillin" misses "amoxicilina" and
    "levothyroxine" misses "levotiroxina". Reducing the English name to a stem
    the Spanish spelling starts with recovers them.
    """
    original = substance.strip()
    terms = [original]
    stem = original.lower()
    stem = stem.replace("ph", "f").replace("th", "t").replace("y", "i")
    stem = re.sub(r"([bcdfglmnprst])\1", r"\1", stem)
    for candidate in (stem, re.sub(r"e$", "", stem)):
        if candidate and candidate not in terms:
            terms.append(candidate)
    return terms


def _search_page(term, page):
    response = requests.get(
        CIMA_SEARCH_URL,
        params={"practiv1": term, "pagina": page},
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    return response.json()


def _search_term(term, substance, limit):
    results = []
    page = 1
    while len(results) < limit:
        payload = _search_page(term, page)
        records = payload.get("resultados", [])
        if not records:
            break
        for row in records:
            if not row.get("nombre", ""):
                continue
            results.append(_extract_record(row, substance))
            if len(results) >= limit:
                break
        total = int(payload.get("totalFilas") or 0)
        if page * CIMA_PAGE_SIZE >= total:
            break
        page += 1
    return results


def run_spain_cima_search(substance, limit=CIMA_MAX_RESULTS):
    if not substance.strip():
        return []
    for term in spanish_search_terms(substance):
        try:
            results = _search_term(term, substance, limit)
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Spain CIMA request failed for %s: %s", term, exc)
            return []
        if results:
            if term != substance.strip():
                logger.info("Spain CIMA matched %s using Spanish term %s", substance, term)
            return results
    return []
