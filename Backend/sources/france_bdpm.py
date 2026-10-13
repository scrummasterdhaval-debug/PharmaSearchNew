from functools import lru_cache
import unicodedata

import requests

from core.logging_config import get_logger


BDPM_BASE_URL = "https://base-donnees-publique.medicaments.gouv.fr"
BDPM_PRODUCTS_URL = f"{BDPM_BASE_URL}/download/file/CIS_bdpm.txt"
BDPM_COMPOSITIONS_URL = f"{BDPM_BASE_URL}/download/file/CIS_COMPO_bdpm.txt"
logger = get_logger(__name__)


def _decode_lines(content):
    return content.decode("cp1252", errors="replace").splitlines()


def _split_line(line):
    return [part.strip() for part in line.split("\t")]


def _normalize(text):
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(char for char in decomposed if not unicodedata.combining(char)).lower()


@lru_cache(maxsize=1)
def _fetch_products():
    response = requests.get(
        BDPM_PRODUCTS_URL,
        timeout=60,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    products = {}
    for line in _decode_lines(response.content):
        fields = _split_line(line)
        if len(fields) < 12:
            continue
        cis = fields[0]
        products[cis] = {
            "cis": cis,
            "product": fields[1],
            "pharmaceutical_form": fields[2],
            "route": fields[3],
            "status": fields[4],
            "procedure": fields[5],
            "marketing_status": fields[6],
            "registration_date": fields[7],
            "company": fields[10],
        }
    return products


@lru_cache(maxsize=1)
def _fetch_compositions():
    response = requests.get(
        BDPM_COMPOSITIONS_URL,
        timeout=60,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    compositions = {}
    for line in _decode_lines(response.content):
        fields = _split_line(line)
        if len(fields) < 8:
            continue
        cis = fields[0]
        compositions.setdefault(cis, []).append(
            {
                "form": fields[1],
                "substance_code": fields[2],
                "substance": fields[3],
                "strength": fields[4],
                "reference": fields[5],
                "role": fields[6],
            }
        )
    return compositions


def _matches_substance(compositions, substance):
    query = _normalize(substance.strip())
    return any(query in _normalize(item.get("substance", "")) for item in compositions)


def run_france_bdpm_search(substance, limit=1000):
    try:
        products = _fetch_products()
        compositions_by_cis = _fetch_compositions()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("France BDPM request failed: %s", exc)
        return []

    results = []
    for cis, compositions in compositions_by_cis.items():
        if not _matches_substance(compositions, substance):
            continue
        product = products.get(cis)
        if not product:
            continue
        product_url = f"{BDPM_BASE_URL}/medicament/{cis}/extrait"
        substance_names = sorted({item["substance"] for item in compositions if item.get("substance")})
        strengths = sorted({item["strength"] for item in compositions if item.get("strength")})
        results.append(
            {
                "substance": ", ".join(substance_names) or substance,
                "product": product.get("product", ""),
                "company": product.get("company", ""),
                "country": "France",
                "region": "EU",
                "status": product.get("marketing_status") or product.get("status", ""),
                "strength": ", ".join(strengths),
                "dosage_form": product.get("pharmaceutical_form", ""),
                "registration_number": cis,
                "registration_date": product.get("registration_date", ""),
                "source": "France BDPM",
                "source_url": "https://www.data.gouv.fr/fr/datasets/base-de-donnees-publique-des-medicaments-base-officielle/",
                "product_url": product_url,
                "url": product_url,
                # The BDPM product page is the French product information: it
                # carries the "Resume des caracteristiques du produit" -- the
                # SmPC -- and the "Notice", which is the patient leaflet, both
                # rendered on the page itself under a CIS unique to the
                # product. Without them a French row has no document of its own
                # and borrows another country's label.
                "smpc_url": product_url,
                "pil_url": product_url,
                "document_type": "BDPM product information (RCP and notice)",
            }
        )
        if len(results) >= limit:
            break
    return results
