import re
import threading
from typing import Any
from urllib.parse import quote

from repository import (
    clean_search_term,
    save_live_saves,
    save_product_details,
    search_medicines,
    search_product_details,
)
from sources.deep_enrichment import enrich_deep_results
from sources.ema import EU_COUNTRIES
from sources.mhra_document_parser import enrich_mhra_document_metadata
from sources.parser import clean_product_name
from sources.search_engine import LIVE_SEARCH_TIMEOUT_SECONDS, search_substance
from core.logging_config import get_logger
from services.ai_enrichment import attach_ai_enrichment_metadata
from services.freshness import SourceFreshness, covered_sources, source_freshness
from services.harvest_vocabulary import is_combination, molecule_group_key
from services.regulatory_dates import parse_regulatory_date
from services.result_formatter import formatted_result_row

logger = get_logger(__name__)


DEFAULT_SOURCES = [
    "EMA",
    "EU MRI Product Index",
    "Belgium FAMHP",
    "France BDPM",
    "Ireland medicines.ie",
    "Spain CIMA",
    "ANMDMR Romania",
    "AIFA Italy",
    "MHRA",
    "FDA",
    "Drugs@FDA",
    "FDA Purple Book",
    "Health Canada",
    "TGA Australia",
    "Medsafe New Zealand",
    "SAHPRA South Africa",
    "FDA Ghana",
    "SFDA Saudi Arabia",
    "MoPH Lebanon",
    "Israel Drug Registry",
    "CDSCO India",
    "NMPA China",
    "BPOM Indonesia",
    "NPRA Malaysia",
    "FDA Philippines",
    "HSA Singapore",
    "MFDS South Korea",
    "Thai FDA",
    "DAV Vietnam",
    "MHLW Japan",
    "Hong Kong Drug Office",
    "Cyprus Pharmaceutical Services",
    "Ukraine DRLZ",
    "GRLS Russia",
    "ANVISA Brazil",
    "EOF Greece",
    "HPRA Ireland",
    "CBG Netherlands",
    "URPL Poland",
    "SUKL Czech Republic",
    "DKMA Denmark",
    "DMP Norway",
    "SUKL Slovakia",
    "ZVA Latvia",
    "ALIMS Serbia",
    "Malta Medicines Authority",
    "BDA Bulgaria",
    "INVIMA Colombia",
    "NDDA Kazakhstan",
    "TITCK Turkey",
    "TFDA Taiwan",
    "BfArM Germany",
]
EU_NATIONAL_SOURCES = [
    "Belgium FAMHP",
    "EU MRI Product Index",
    "France BDPM",
    "Ireland medicines.ie",
    "Spain CIMA",
    "ANMDMR Romania",
    "AIFA Italy",
    "EOF Greece",
    "HPRA Ireland",
    "CBG Netherlands",
    "URPL Poland",
    "SUKL Czech Republic",
    "DKMA Denmark",
    "DMP Norway",
    "SUKL Slovakia",
    "ZVA Latvia",
    "ALIMS Serbia",
    "Malta Medicines Authority",
    "BDA Bulgaria",
    "BfArM Germany",
    "Cyprus Pharmaceutical Services",
    "Ukraine DRLZ",
]
AFRICA_COUNTRIES = [
    "Algeria",
    "Angola",
    "Benin",
    "Botswana",
    "Burkina Faso",
    "Burundi",
    "Cabo Verde",
    "Cameroon",
    "Central African Republic",
    "Chad",
    "Comoros",
    "Democratic Republic of the Congo",
    "Djibouti",
    "Egypt",
    "Equatorial Guinea",
    "Eritrea",
    "Eswatini",
    "Ethiopia",
    "Gabon",
    "Gambia",
    "Ghana",
    "Guinea",
    "Guinea-Bissau",
    "Ivory Coast",
    "Kenya",
    "Lesotho",
    "Liberia",
    "Libya",
    "Madagascar",
    "Malawi",
    "Mali",
    "Mauritania",
    "Mauritius",
    "Morocco",
    "Mozambique",
    "Namibia",
    "Niger",
    "Nigeria",
    "Republic of the Congo",
    "Rwanda",
    "Sao Tome and Principe",
    "Senegal",
    "Seychelles",
    "Sierra Leone",
    "Somalia",
    "South Africa",
    "South Sudan",
    "Sudan",
    "Tanzania",
    "Togo",
    "Tunisia",
    "Uganda",
    "Zambia",
    "Zimbabwe",
]
MIDDLE_EAST_COUNTRIES = [
    "Bahrain",
    "Cyprus",
    "Egypt",
    "Iran",
    "Iraq",
    "Israel",
    "Jordan",
    "Kuwait",
    "Lebanon",
    "Oman",
    "Palestine",
    "Qatar",
    "Saudi Arabia",
    "Syria",
    "Turkey",
    "United Arab Emirates",
    "Yemen",
]
ASIA_COUNTRIES = [
    "Afghanistan",
    "Armenia",
    "Azerbaijan",
    "Bahrain",
    "Bangladesh",
    "Bhutan",
    "Brunei",
    "Cambodia",
    "China",
    "Cyprus",
    "Georgia",
    "Hong Kong",
    "India",
    "Indonesia",
    "Iran",
    "Iraq",
    "Israel",
    "Japan",
    "Jordan",
    "Kazakhstan",
    "Kuwait",
    "Kyrgyzstan",
    "Laos",
    "Lebanon",
    "Malaysia",
    "Maldives",
    "Mongolia",
    "Myanmar",
    "Nepal",
    "North Korea",
    "Oman",
    "Pakistan",
    "Palestine",
    "Philippines",
    "Qatar",
    "Saudi Arabia",
    "Singapore",
    "South Korea",
    "Sri Lanka",
    "Syria",
    "Taiwan",
    "Tajikistan",
    "Thailand",
    "Timor-Leste",
    "Turkmenistan",
    "Ukraine",
    "United Arab Emirates",
    "Uzbekistan",
    "Vietnam",
    "Yemen",
]
LATIN_AMERICA_COUNTRIES = [
    "Argentina",
    "Bahamas",
    "Barbados",
    "Belize",
    "Bolivia",
    "Brazil",
    "Chile",
    "Colombia",
    "Costa Rica",
    "Cuba",
    "Dominican Republic",
    "Ecuador",
    "El Salvador",
    "Guatemala",
    "Guyana",
    "Haiti",
    "Honduras",
    "Jamaica",
    "Mexico",
    "Nicaragua",
    "Panama",
    "Paraguay",
    "Peru",
    "Suriname",
    "Trinidad and Tobago",
    "Uruguay",
    "Venezuela",
]
REGIONAL_COUNTRIES = {
    "AF": AFRICA_COUNTRIES,
    "ME": MIDDLE_EAST_COUNTRIES,
    "AS": ASIA_COUNTRIES,
    "LA": LATIN_AMERICA_COUNTRIES,
}
# A region's rows can carry a narrower region of their own: Brazil keeps "BR".
REGION_MEMBERS = {"LA": {"LA", "BR"}}
REGION_OPTIONS = ["ALL", "EU", "UK", "US", "CA", "AU", "NZ", "AF", "ME", "AS", "LA", "CH", "JP", "RU", "BR"]
GLOBAL_COUNTRIES = [
    "Afghanistan",
    "Albania",
    "Algeria",
    "Andorra",
    "Angola",
    "Argentina",
    "Armenia",
    "Australia",
    "Austria",
    "Azerbaijan",
    "Bahamas",
    "Bahrain",
    "Bangladesh",
    "Barbados",
    "Belarus",
    "Belgium",
    "Belize",
    "Benin",
    "Bhutan",
    "Bolivia",
    "Bosnia and Herzegovina",
    "Botswana",
    "Brazil",
    "Brunei",
    "Bulgaria",
    "Burkina Faso",
    "Burundi",
    "Cambodia",
    "Cameroon",
    "Canada",
    "Chile",
    "China",
    "Colombia",
    "Costa Rica",
    "Croatia",
    "Cyprus",
    "Czech Republic",
    "Denmark",
    "Dominican Republic",
    "Ecuador",
    "Egypt",
    "El Salvador",
    "Estonia",
    "Ethiopia",
    "Finland",
    "France",
    "Georgia",
    "Germany",
    "Ghana",
    "Greece",
    "Guatemala",
    "Honduras",
    "Hong Kong",
    "Hungary",
    "Iceland",
    "India",
    "Indonesia",
    "Ireland",
    "Israel",
    "Italy",
    "Japan",
    "Jordan",
    "Kazakhstan",
    "Kenya",
    "Kuwait",
    "Latvia",
    "Lebanon",
    "Lithuania",
    "Luxembourg",
    "Malaysia",
    "Malta",
    "Mexico",
    "Morocco",
    "Netherlands",
    "New Zealand",
    "Nigeria",
    "Norway",
    "Pakistan",
    "Panama",
    "Paraguay",
    "Peru",
    "Philippines",
    "Poland",
    "Portugal",
    "Qatar",
    "Romania",
    "Saudi Arabia",
    "Singapore",
    "Slovakia",
    "Slovenia",
    "South Africa",
    "South Korea",
    "Spain",
    "Sri Lanka",
    "Sweden",
    "Switzerland",
    "Taiwan",
    "Thailand",
    "Tunisia",
    "Turkey",
    "Ukraine",
    "United Arab Emirates",
    "United Kingdom",
    "United States",
    "Uruguay",
    "Vietnam",
    "Russia",
]
LEGACY_GLOBAL_COUNTRIES = [
    "Argentina",
    "Australia",
    "Brazil",
    "Canada",
    "China",
    "India",
    "Japan",
    "Mexico",
    "New Zealand",
    "South Korea",
    "Switzerland",
    "United Kingdom",
    "United States",
]
COUNTRY_OPTIONS = sorted(
    set(
        EU_COUNTRIES
        + GLOBAL_COUNTRIES
        + LEGACY_GLOBAL_COUNTRIES
        + AFRICA_COUNTRIES
        + MIDDLE_EAST_COUNTRIES
        + ASIA_COUNTRIES
    ),
    key=str.lower,
)
SORT_COLUMNS = {
    "substance": "substance",
    "product": "product",
    "company": "company",
    "country": "country",
    "region": "region",
    "status": "status",
    "source": "source",
    "strength": "strength",
    "dosage_form": "dosage_form",
    "pack_size": "pack_size",
    "atc_code": "atc_code",
    "therapeutic_category": "therapeutic_category",
    "ma_holder": "company",
    "manufacturer_name": "manufacturer_name",
    "manufacturer_country": "manufacturer_country",
    "registration_number": "registration_number",
    "registration_date": "registration_date",
}
DATE_SORT_FIELDS = {"registration_date", "expiry_date"}
PROVENANCE_FIELDS = ("evidence_url", "evidence_section", "verification_status", "missing_reason")
SOURCE_COUNTRIES = {
    "fda": {"United States"},
    "health canada": {"Canada"},
    "tga australia": {"Australia"},
    "medsafe new zealand": {"New Zealand"},
    "sahpra south africa": {"South Africa"},
    "fda ghana": {"Ghana"},
    "sfda saudi arabia": {"Saudi Arabia"},
    "moph lebanon": {"Lebanon"},
    "israel drug registry": {"Israel"},
    "cdsco india": {"India"},
    "nmpa china": {"China"},
    "bpom indonesia": {"Indonesia"},
    "npra malaysia": {"Malaysia"},
    "fda philippines": {"Philippines"},
    "hsa singapore": {"Singapore"},
    "hong kong drug office": {"Hong Kong"},
    "mfds south korea": {"South Korea"},
    "thai fda": {"Thailand"},
    "dav vietnam": {"Vietnam"},
    "pmda japan": {"Japan"},
    "mhlw japan": {"Japan"},
    "fda orange book": {"United States"},
    "drugs@fda": {"United States"},
    "fda purple book": {"United States"},
    "mhra": {"United Kingdom"},
    "ema": {
        "Austria",
        "Belgium",
        "Bulgaria",
        "Croatia",
        "Cyprus",
        "Czech Republic",
        "Denmark",
        "Estonia",
        "Finland",
        "France",
        "Germany",
        "Greece",
        "Hungary",
        "Ireland",
        "Italy",
        "Latvia",
        "Lithuania",
        "Luxembourg",
        "Malta",
        "Netherlands",
        "Poland",
        "Portugal",
        "Romania",
        "Slovakia",
        "Slovenia",
        "Spain",
        "Sweden",
        "Norway",
        "Iceland",
        "Liechtenstein",
        "European Union",
    },
    "eu mri product index": {
        "Austria",
        "Belgium",
        "Bulgaria",
        "Croatia",
        "Cyprus",
        "Czech Republic",
        "Denmark",
        "Estonia",
        "Finland",
        "France",
        "Germany",
        "Greece",
        "Hungary",
        "Ireland",
        "Italy",
        "Latvia",
        "Lithuania",
        "Luxembourg",
        "Malta",
        "Netherlands",
        "Poland",
        "Portugal",
        "Romania",
        "Slovakia",
        "Slovenia",
        "Spain",
        "Sweden",
        "European Union",
    },
    "belgium famhp": {"Belgium"},
    "france bdpm": {"France"},
    "ireland medicines.ie": {"Ireland"},
    "spain cima": {"Spain"},
    "aifa italy": {"Italy"},
    "eof greece": {"Greece"},
    "bfarm germany": {"Germany"},
    "hpra ireland": {"Ireland"},
    "cbg netherlands": {"Netherlands"},
    "urpl poland": {"Poland"},
    "sukl czech republic": {"Czech Republic"},
    "dkma denmark": {"Denmark"},
    "dmp norway": {"Norway"},
    "sukl slovakia": {"Slovakia"},
    "zva latvia": {"Latvia"},
    "titck turkey": {"Turkey"},
    "alims serbia": {"Serbia"},
    "malta medicines authority": {"Malta"},
    "bda bulgaria": {"Bulgaria"},
    "invima colombia": {"Colombia"},
    "ndda kazakhstan": {"Kazakhstan"},
    "tfda taiwan": {"Taiwan"},
    "anvisa brazil": {"Brazil"},
    "cyprus pharmaceutical services": {"Cyprus"},
    "ukraine drlz": {"Ukraine"},
    "grls russia": {"Russia"},
}
COUNTRY_SOURCE_DEFAULTS = {
    "Australia": "TGA Australia",
    "Canada": "Health Canada",
    "Japan": "MHLW Japan",
    "Hong Kong": "Hong Kong Drug Office",
    "New Zealand": "Medsafe New Zealand",
    "Ghana": "FDA Ghana",
    "China": "NMPA China",
    "India": "CDSCO India",
    "Indonesia": "BPOM Indonesia",
    "Israel": "Israel Drug Registry",
    "Malaysia": "NPRA Malaysia",
    "Philippines": "FDA Philippines",
    "Saudi Arabia": "SFDA Saudi Arabia",
    "Lebanon": "MoPH Lebanon",
    "Singapore": "HSA Singapore",
    "South Africa": "SAHPRA South Africa",
    "South Korea": "MFDS South Korea",
    "Thailand": "Thai FDA",
    "Vietnam": "DAV Vietnam",
    "Switzerland": "Swissmedic",
    "United Kingdom": "MHRA",
    "United States": "FDA",
    "Ukraine": "Ukraine DRLZ",
    "Russia": "GRLS Russia",
    "Cyprus": "Cyprus Pharmaceutical Services",
    "Romania": "ANMDMR Romania",
    "Italy": "AIFA Italy",
    "Greece": "EOF Greece",
    "Germany": "BfArM Germany",
    "Taiwan": "TFDA Taiwan",
    "Brazil": "ANVISA Brazil",
    "Netherlands": "CBG Netherlands",
    "Poland": "URPL Poland",
    "Czech Republic": "SUKL Czech Republic",
    "Denmark": "DKMA Denmark",
    "Norway": "DMP Norway",
    "Slovakia": "SUKL Slovakia",
    "Latvia": "ZVA Latvia",
    "Turkey": "TITCK Turkey",
    "Serbia": "ALIMS Serbia",
    "Malta": "Malta Medicines Authority",
    "Bulgaria": "BDA Bulgaria",
    "Colombia": "INVIMA Colombia",
    "Kazakhstan": "NDDA Kazakhstan",
}
REGION_SOURCE_DEFAULTS = {
    "AU": "TGA Australia",
    "CA": "Health Canada",
    "CH": "Swissmedic",
    "JP": "MHLW Japan",
    "NZ": "Medsafe New Zealand",
    "UK": "MHRA",
    "US": "FDA",
    "RU": "GRLS Russia",
    "BR": "ANVISA Brazil",
}
EU_LOOKUP_SOURCE = "EU National Registry"
REGISTRY_LOOKUP_SOURCE = "Regulatory Registry Lookup"
GLOBAL_LOOKUP_SOURCE = "Global Generic Registry Lookup"
AFRICA_LOOKUP_SOURCE = "Africa Generic Registry Lookup"
MIDDLE_EAST_LOOKUP_SOURCE = "Middle East Generic Registry Lookup"
ASIA_LOOKUP_SOURCE = "Asia Generic Registry Lookup"
LATIN_AMERICA_LOOKUP_SOURCE = "Latin America Generic Registry Lookup"
LOOKUP_ONLY_SOURCE = "__regional_lookup_only__"
GENERIC_LOOKUP_SOURCES = {
    REGISTRY_LOOKUP_SOURCE,
    GLOBAL_LOOKUP_SOURCE,
    AFRICA_LOOKUP_SOURCE,
    MIDDLE_EAST_LOOKUP_SOURCE,
    ASIA_LOOKUP_SOURCE,
    LATIN_AMERICA_LOOKUP_SOURCE,
    EU_LOOKUP_SOURCE,
}
REGIONAL_LIVE_SOURCES = {
    "AF": ["SAHPRA South Africa", "FDA Ghana"],
    "ME": ["SFDA Saudi Arabia", "MoPH Lebanon", "Israel Drug Registry", "TITCK Turkey"],
    "AS": [
        "CDSCO India",
        "NMPA China",
        "BPOM Indonesia",
        "NPRA Malaysia",
        "FDA Philippines",
        "HSA Singapore",
        "MFDS South Korea",
        "Thai FDA",
        "DAV Vietnam",
        "MHLW Japan",
        "TFDA Taiwan",
        "NDDA Kazakhstan",
        "Hong Kong Drug Office",
        "Israel Drug Registry",
        "SFDA Saudi Arabia",
    ],
    "EU": EU_NATIONAL_SOURCES,
    "US": ["FDA", "Drugs@FDA", "FDA Purple Book"],
    "RU": ["GRLS Russia"],
    "LA": ["ANVISA Brazil", "INVIMA Colombia"],
}
REGIONAL_REGISTRY_URLS = {
    "Egypt": "https://edaegypt.gov.eg/",
    "Ghana": "https://fdaghana.gov.gh/product-register/",
    "Kenya": "https://products.pharmacyboardkenya.org/",
    "Nigeria": "https://registration.nafdac.gov.ng/",
    "South Africa": "https://www.sahpra.org.za/registered-health-products/",
    "Saudi Arabia": "https://www.sfda.gov.sa/en/drugs-list",
    "United Arab Emirates": "https://mohap.gov.ae/en/services/registered-medical-product-directory",
    "Israel": "https://israeldrugs.health.gov.il/",
    "Jordan": "https://www.jfda.jo/",
    "Qatar": "https://www.moph.gov.qa/",
    "China": "https://english.nmpa.gov.cn/database.html",
    "India": "https://cdsco.gov.in/opencms/opencms/en/Approval_new/FDC-New-Drugs-Marketing/",
    "Indonesia": "https://cekbpom.pom.go.id/",
    "Malaysia": "https://www.npra.gov.my/index.php/my/consumers-2/maklumat/carian-produk-berdaftar-bernotifikasi.html",
    "Philippines": "https://verification.fda.gov.ph/drug_productslist.php",
    "Singapore": "https://eservice.hsa.gov.sg/prism/common/enquirepublic/SearchDRBProduct.do?action=load",
    "South Korea": "https://nedrug.mfds.go.kr/",
    "Thailand": "https://pertento.fda.moph.go.th/FDA_SEARCH_DRUG/SEARCH_DRUG/FRM_SEARCH_DRUG.aspx",
    "Vietnam": "https://dichvucong.dav.gov.vn/congbothuoc/index",
    "Japan": "https://www.pmda.go.jp/PmdaSearch/iyakuSearch/",
    "Hong Kong": "https://www.drugoffice.gov.hk/eps/do/en/consumer/search_drug_database2.html",
    "Ukraine": "http://www.drlz.com.ua/ibp/ddsite.nsf/all/shlist?opendocument",
    "Russia": "https://grls.rosminzdrav.ru/grls.aspx",
    "Cyprus": "https://www.phs.moh.gov.cy/human-search/home.xhtml?lang=en",
}
# Therapeutic category describes the molecule, so every row of a search can share
# it. An ATC code describes the product: plain metformin is A10BA02 while its
# combinations are A10BD/A10BK codes, so it may only be shared between rows for
# the same product.
# Nothing is shared across a molecule: a value one regulator published is not
# another regulator's record, and showing it on that row presents it as if it
# were.
SHARED_MOLECULE_FIELDS: list[str] = []
SHARED_PRODUCT_FIELDS = [
    "atc_code",
]


def _ensure_source(selected: list[str], source_name: str) -> list[str]:
    if any(item.lower() == source_name.lower() for item in selected):
        return selected
    return selected + [source_name]


def _lookup_url(substance: str, country: str) -> str:
    clean_substance = substance.strip()
    if country in EU_COUNTRIES:
        return "https://www.ema.europa.eu/en/search?" f"search_api_fulltext={quote(clean_substance)}"
    if country == "United Kingdom":
        return "https://products.mhra.gov.uk/search/?" f"search={quote(clean_substance)}&page=1"
    if country == "United States":
        return "https://api.fda.gov/drug/label.json?" f"search=openfda.substance_name:{quote(clean_substance)}"
    if country == "Canada":
        return "https://health-products.canada.ca/dpd-bdpp/index-eng.jsp"
    if country == "Australia":
        return "https://www.tga.gov.au/resources/artg?" f"keywords={quote(clean_substance)}"
    if country == "New Zealand":
        return "https://www.medsafe.govt.nz/DbSearch/"
    if country in REGIONAL_REGISTRY_URLS:
        return REGIONAL_REGISTRY_URLS[country]
    if country == "Switzerland":
        return "https://www.swissmedic.ch/swissmedic/en/home/services/listen_neu.html"
    return "https://www.google.com/search?" f"q={quote(country + ' medicine register ' + clean_substance)}"


def _country_region(country: str) -> str:
    if country in EU_COUNTRIES:
        return "EU"
    if country == "United Kingdom":
        return "UK"
    if country == "United States":
        return "US"
    if country == "Canada":
        return "CA"
    if country == "Australia":
        return "AU"
    if country == "New Zealand":
        return "NZ"
    if country in AFRICA_COUNTRIES:
        return "AF"
    if country in MIDDLE_EAST_COUNTRIES:
        return "ME"
    if country == "Switzerland":
        return "CH"
    if country == "Japan":
        return "JP"
    if country == "Russia":
        return "RU"
    if country == "Brazil":
        return "BR"
    if country in LATIN_AMERICA_COUNTRIES:
        return "LA"
    if country in ASIA_COUNTRIES:
        return "AS"
    return "Global"


def _registry_lookup_row(substance: str, country: str, region_override: str = "") -> dict[str, Any]:
    clean_substance = substance.strip()
    search_url = _lookup_url(clean_substance, country)
    region = region_override or _country_region(country)
    if region_override == "ALL":
        source = GLOBAL_LOOKUP_SOURCE
        document_type = "Generic global registry lookup fallback"
    elif region_override == "AF":
        source = AFRICA_LOOKUP_SOURCE
        document_type = "Generic Africa registry lookup fallback"
    elif region_override == "ME":
        source = MIDDLE_EAST_LOOKUP_SOURCE
        document_type = "Generic Middle East registry lookup fallback"
    elif region_override == "AS":
        source = ASIA_LOOKUP_SOURCE
        document_type = "Generic Asia registry lookup fallback"
    elif region_override == "LA" or region == "LA":
        source = LATIN_AMERICA_LOOKUP_SOURCE
        document_type = "Generic Latin America registry lookup fallback"
    elif country in EU_COUNTRIES:
        source = EU_LOOKUP_SOURCE
        document_type = "EU national lookup fallback"
    elif region == "AF":
        source = AFRICA_LOOKUP_SOURCE
        document_type = "Generic Africa registry lookup fallback"
    elif region == "ME":
        source = MIDDLE_EAST_LOOKUP_SOURCE
        document_type = "Generic Middle East registry lookup fallback"
    elif region == "AS":
        source = ASIA_LOOKUP_SOURCE
        document_type = "Generic Asia registry lookup fallback"
    else:
        source = REGISTRY_LOOKUP_SOURCE
        document_type = "Country registry lookup fallback"
    return {
        "substance": clean_substance,
        "product": f"{clean_substance} {country} generic regulatory registry lookup",
        "company": "",
        "country": country,
        "region": region,
        "status": "Country-specific live connector unavailable or no direct match - verify national register",
        "strength": "",
        "dosage_form": "",
        "pack_size": "",
        "registration_number": "",
        "document_type": document_type,
        "source": source,
        "source_url": search_url,
        "product_url": search_url,
        "url": search_url,
    }


def _eu_lookup_row(substance: str, country: str) -> dict[str, Any]:
    return _registry_lookup_row(substance, country)


def _eu_lookup_rows(
    substance: str,
    country: str = "",
    region: str = "",
    existing_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if country in EU_COUNTRIES:
        return [_eu_lookup_row(substance, country)]
    if region != "EU":
        return []

    existing_countries = {
        row.get("country")
        for row in existing_rows or []
        if row.get("region") == "EU" and row.get("country")
    }
    return [
        _registry_lookup_row(substance, eu_country)
        for eu_country in EU_COUNTRIES
        if eu_country not in existing_countries
    ]


def _country_lookup_rows(
    substance: str,
    country: str = "",
    region: str = "",
    existing_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if country:
        return [_registry_lookup_row(substance, country)]
    if region == "ALL":
        existing_countries = {
            row.get("country")
            for row in existing_rows or []
            if row.get("country")
        }
        return [
            _registry_lookup_row(substance, lookup_country, region_override=region)
            for lookup_country in COUNTRY_OPTIONS
            if lookup_country not in existing_countries
        ]
    if region == "EU":
        return _eu_lookup_rows(substance, region=region, existing_rows=existing_rows)
    if region in REGIONAL_COUNTRIES:
        existing_countries = {
            row.get("country")
            for row in existing_rows or []
            if row.get("region") in REGION_MEMBERS.get(region, {region}) and row.get("country")
        }
        return [
            _registry_lookup_row(substance, region_country, region_override=region)
            for region_country in REGIONAL_COUNTRIES[region]
            if region_country not in existing_countries
        ]
    if region in {"UK", "US", "CA", "AU", "NZ", "CH", "JP"}:
        region_country = {
            "UK": "United Kingdom",
            "US": "United States",
            "CA": "Canada",
            "AU": "Australia",
            "NZ": "New Zealand",
            "CH": "Switzerland",
            "JP": "Japan",
        }[region]
        return [_registry_lookup_row(substance, region_country)]
    return []


def result_key(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        (item.get("source") or "").strip().lower(),
        (item.get("product") or "").strip().lower(),
        (item.get("country") or "").strip().lower(),
        (item.get("registration_number") or "").strip().lower(),
        (item.get("product_url") or item.get("url") or "").strip().lower(),
    )


def molecule_key(item: dict[str, Any]) -> str:
    # A combination found by searching one of its molecules shares nothing
    # molecule-level with that molecule: Janumet's class is not metformin's.
    if is_combination(item.get("source_substance")):
        return molecule_group_key(item.get("source_substance"))
    for field in ["searched_substance", "substance", "source_substance"]:
        value = str(item.get(field) or "").strip().lower()
        if value:
            return value
    return ""


def product_key(item: dict[str, Any]) -> str:
    """Identify one marketed product, so product-level values are not shared
    between a plain molecule and its combination products."""
    product = clean_product_name(item.get("product", ""))
    normalized = " ".join(str(product or "").strip().lower().split())
    if not normalized:
        return ""
    # The same product from the same regulator only.
    source = str(item.get("source") or "").strip().lower()
    return f"{source}|{molecule_key(item)}|{normalized}"


def normalized_tokens(value: object) -> list[str]:
    import re

    from services.english_normalizer import _strip_latin_accents

    # Accents folded first: split on non-ASCII, "céfalexine" became "c" and
    # "falexine", and no row contains both.
    return [
        token
        for token in re.split(r"[^a-z0-9]+", _strip_latin_accents(str(value or "")).lower())
        if token
    ]


SEARCHED_STRENGTH = re.compile(
    r"^(?P<molecule>.*?\D)\s+(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<unit>mg|g|mcg|µg|ug|microgram|micrograms|ml|%|iu)?\.?$",
    re.IGNORECASE,
)
_UNIT_IN_MG = {"g": 1000.0, "mg": 1.0, "mcg": 0.001, "µg": 0.001, "ug": 0.001, "microgram": 0.001, "micrograms": 0.001}
# ".8 g" is how the FDA writes 0.8 g.
_AMOUNT = re.compile(r"(\d*[.,]?\d+)\s*(mg|g|mcg|µg|μg|ug|microgram)?\b", re.IGNORECASE)


def split_searched_strength(term: object) -> tuple[str, str]:
    """ "sevelamer carbonate 800" -> ("sevelamer carbonate", "800").

    A strength typed after the molecule is not part of its name. Matched as a
    name it kept only the registers that print "800" in the product name --
    the USA, the UK and Vietnam -- and lost every other market.
    """
    text = " ".join(str(term or "").split())
    match = SEARCHED_STRENGTH.match(text)
    if not match or not match.group("molecule").strip(" +,"):
        return text, ""
    unit = (match.group("unit") or "").lower()
    return match.group("molecule").strip(" +,"), f"{match.group('amount')}{(' ' + unit) if unit else ''}"


def _in_mg(amount: str, unit: str) -> float | None:
    factor = _UNIT_IN_MG.get((unit or "mg").lower().replace("μ", "µ"))
    if factor is None:
        return None
    return float(amount.replace(",", ".")) * factor


def matches_searched_strength(row: dict[str, Any], strength: str) -> bool:
    """The row is at the searched strength, or does not say what strength it is.

    Amounts are compared in milligrams, so "800" matches "800MG/TAB" and
    Canada's "0.8 G". A row whose register publishes no strength is kept: the
    search would otherwise drop a licence for want of a field.
    """
    if not strength:
        return True
    amount, _, unit = strength.partition(" ")
    if unit and unit not in _UNIT_IN_MG:
        return strength.lower() in f"{row.get('strength', '')} {row.get('product', '')}".lower()
    wanted = _in_mg(amount, unit)
    text = f"{row.get('strength') or ''} {row.get('product') or ''}"
    found = [
        _in_mg(number, found_unit)
        for number, found_unit in _AMOUNT.findall(text)
        if found_unit or not unit
    ]
    found = [value for value in found if value is not None]
    if not found:
        return True
    return any(abs(value - wanted) < 1e-6 for value in found)


def contains_all_tokens(value: object, tokens: list[str]) -> bool:
    haystack = set(normalized_tokens(value))
    return bool(tokens) and all(token in haystack for token in tokens)


def _contains_every_molecule(row: dict[str, Any], substance: str, parts: list[str]) -> bool:
    """The row's own ingredients or name include each molecule of a combination.

    Matched on the stems the open registers use, so Italy's "PARACETAMOLO;
    CAFFEINA" and Brazil's "paracetamol + cafeína" count, and on each
    molecule's other names, so the FDA's "ACETAMINOPHEN; CAFFEINE" counts too.
    The searched term stored on the row is not evidence of anything.
    """
    from sources.open_registers import inn_tokens, query_tokens
    from sources.synonyms import names_for

    text = " ".join(
        str(row.get(field) or "")
        for field in ("product", "source_substance", "active_substance", "active_substances", "substance")
        if not (field == "substance" and str(row.get(field) or "").strip().lower() == substance.strip().lower())
    )
    stems = set(inn_tokens(text))
    return all(
        any(set(query_tokens(name)) <= stems for name in names_for(part))
        for part in parts
    )


def row_relevant_to_substance(row: dict[str, Any], substance: str) -> bool:
    import re

    from sources.synonyms import combination_parts

    query_tokens = normalized_tokens(substance)
    if not query_tokens:
        return True

    parts = combination_parts(substance)
    folded = str(row.get("inn_fold_tokens") or "")
    if folded:
        # A register written in another script (Russia): its INN was
        # transliterated and folded, and the search is compared the same way.
        from sources.grls_russia import fold
        from sources.open_registers import inn_tokens, query_tokens

        have = set(folded.split())
        if parts:
            return all(all(fold(token) in have for token in query_tokens(part)) for part in parts)
        return all(fold(token) in have for token in inn_tokens(substance))
    if parts:
        return _contains_every_molecule(row, substance, parts)

    product = str(row.get("product") or "")
    product_has_query = contains_all_tokens(product, query_tokens)
    if product_has_query:
        return True

    # A bracket in a product name usually holds the salt -- "Amlodipin
    # (besilat)" -- and then it says what the product is. Germany's register
    # names products by pack, "Renagel 800 mg Filmtabletten - OP(1x100)", and
    # a bracket holding no word at all says nothing about the molecule, so it
    # must not be allowed to rule the row out.
    parenthetical_values = [
        value
        for value in re.findall(r"\(([^)]{3,120})\)", product)
        if len(re.findall(r"[^\W\d_]", value)) >= 3
    ]
    if parenthetical_values:
        return any(contains_all_tokens(value, query_tokens) for value in parenthetical_values)

    active_substance = " ".join(
        str(row.get(field) or "")
        for field in ["active_substance", "active_substances"]
    )
    if contains_all_tokens(active_substance, query_tokens):
        return True

    source_substance = str(row.get("source_substance") or "")
    if contains_all_tokens(source_substance, query_tokens):
        return True

    # Combination searches must not accept records returned for only one of
    # the requested ingredients. Single-substance searches remain permissive
    # so regulator-specific synonyms and brand-only product names still work.
    if len(query_tokens) > 1:
        return False

    if str(row.get("source") or "").strip().lower() == "mhra":
        return False

    return True


def is_connector_lookup_fallback(item: dict[str, Any]) -> bool:
    document_type = str(item.get("document_type") or "").strip().lower()
    connector_mode = str(item.get("connector_mode") or "").strip().lower()
    product = str(item.get("product") or "").strip().lower()
    source = str(item.get("source") or "").strip()
    return (
        bool(source)
        and (
            "lookup fallback" in document_type
            or "registry search handoff" in document_type
            or connector_mode == "manual_registry"
            or product.endswith("regulator lookup")
        )
        and source not in GENERIC_LOOKUP_SOURCES
    )


def _propagate_shared_fields(
    rows: list[dict[str, Any]],
    fields: list[str],
    key_for_row,
) -> None:
    values_by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = key_for_row(row)
        if not key:
            continue
        bucket = values_by_key.setdefault(key, {})
        for field in fields:
            if row.get(field) and not bucket.get(field):
                bucket[field] = row[field]

    for row in rows:
        key = key_for_row(row)
        if not key:
            continue
        for field, value in values_by_key.get(key, {}).items():
            if value and not row.get(field):
                row[field] = value


def propagate_molecule_fields(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _propagate_shared_fields(rows, SHARED_MOLECULE_FIELDS, molecule_key)
    _propagate_shared_fields(rows, SHARED_PRODUCT_FIELDS, product_key)
    return rows


def source_rank(source: str | None, selected_sources: list[str]) -> int:
    selected = [item.strip().lower() for item in selected_sources if item.strip()]
    source_name = (source or "").strip().lower()
    if source_name in selected:
        return selected.index(source_name)
    return len(selected)


def filter_rows(
    rows: list[dict[str, Any]],
    country: str = "",
    region: str = "",
    source: str = "",
    status: str = "",
    table_search: str = "",
    substance_filter: str = "",
    product_filter: str = "",
    company_filter: str = "",
    strength_filter: str = "",
    dosage_form_filter: str = "",
    pack_size_filter: str = "",
    atc_code_filter: str = "",
    therapeutic_category_filter: str = "",
    ma_holder_filter: str = "",
    manufacturer_name_filter: str = "",
    manufacturer_country_filter: str = "",
    registration_number_filter: str = "",
    registration_date_filter: str = "",
) -> list[dict[str, Any]]:
    query = table_search.strip().lower()
    text_filters = [
        ("substance", substance_filter),
        ("product", product_filter),
        ("company", company_filter),
        ("strength", strength_filter),
        ("dosage_form", dosage_form_filter),
        ("pack_size", pack_size_filter),
        ("atc_code", atc_code_filter),
        ("therapeutic_category", therapeutic_category_filter),
        ("ma_holder", ma_holder_filter),
        ("manufacturer_name", manufacturer_name_filter),
        ("manufacturer_country", manufacturer_country_filter),
        ("registration_number", registration_number_filter),
        ("registration_date", registration_date_filter),
    ]
    filtered = []
    for row in rows:
        display_row = formatted_result_row(row)
        searchable_values = {
            "substance": display_row.get("molecule", row.get("substance", "")),
            "product": display_row.get("product", row.get("product", "")),
            "company": display_row.get("company", row.get("company", "")),
            "ma_holder": display_row.get("ma_holder", row.get("company", "")),
            "strength": display_row.get("strength", row.get("strength", "")),
            "dosage_form": display_row.get("dosage_form", row.get("dosage_form", "")),
            "pack_size": display_row.get("pack_size", row.get("pack_size", "")),
            "atc_code": display_row.get("atc_code", row.get("atc_code", "")),
            "therapeutic_category": display_row.get(
                "therapeutic_category",
                row.get("therapeutic_category", ""),
            ),
            "manufacturer_name": display_row.get(
                "manufacturer_name",
                row.get("manufacturer_name", ""),
            ),
            "manufacturer_country": display_row.get(
                "manufacturer_country",
                row.get("manufacturer_country", ""),
            ),
            "registration_number": display_row.get(
                "registration_number",
                row.get("registration_number", ""),
            ),
            "registration_date": display_row.get(
                "registration_date",
                row.get("registration_date", ""),
            ),
        }
        if any(
            filter_value.strip().lower()
            and filter_value.strip().lower() not in str(searchable_values.get(field, "")).lower()
            for field, filter_value in text_filters
        ):
            continue
        if country and row.get("country") != country:
            continue
        if region and region != "ALL" and row.get("region") not in REGION_MEMBERS.get(region, {region}):
            continue
        if source and row.get("source") != source:
            continue
        if status and display_row.get("registration_status") != status:
            continue
        if query:
            haystack = " ".join(
                str(row.get(field) or "")
                for field in [
                    "substance",
                    "product",
                    "company",
                    "country",
                    "region",
                    "status",
                    "source",
                    "registration_number",
                    "registration_date",
                    "document_type",
                ]
            ).lower()
            haystack = f"{haystack} " + " ".join(str(value or "") for value in searchable_values.values()).lower()
            if query not in haystack:
                continue
        filtered.append(row)
    return filtered


def sort_rows(
    rows: list[dict[str, Any]],
    selected_sources: list[str],
    sort_by: str = "",
    sort_dir: str = "asc",
) -> list[dict[str, Any]]:
    if sort_by not in SORT_COLUMNS:
        return sorted(
            rows,
            key=lambda item: (
                source_rank(item.get("source"), selected_sources),
                (item.get("country") or ""),
                (item.get("product") or ""),
                (item.get("document_type") or ""),
            ),
        )
    field = SORT_COLUMNS[sort_by]
    reverse = sort_dir == "desc"
    if field in DATE_SORT_FIELDS:
        # Registries write dates in eight different shapes, so compare the
        # dates they name rather than the text. Rows with no readable date go
        # last whichever way the list runs -- they answer neither "newest" nor
        # "oldest".
        dated = [(parse_regulatory_date(item.get(field)), item) for item in rows]
        with_dates = sorted(
            (pair for pair in dated if pair[0] is not None),
            key=lambda pair: pair[0],
            reverse=reverse,
        )
        return [item for _, item in with_dates] + [
            item for parsed, item in dated if parsed is None
        ]
    return sorted(
        rows,
        key=lambda item: str(item.get(field) or "").lower(),
        reverse=reverse,
    )


def _centrally_authorised(row: dict[str, Any]) -> bool:
    return (
        str(row.get("registration_number") or "").upper().startswith("EU/")
        or str(row.get("authorisation_scope") or "") == "Centralised"
    )


def suppress_ema_copies(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Leave out EMA's row for a country whose own register lists the product.

    EMA's centrally authorised products are listed once per member state, and
    the national registers list the same products with what EMA's row lacks:
    the local holder, the packs, whether it is marketed. EMA writes its
    procedure number (EMEA/H/C/004162) and the registers the EU number
    (EU/1/15/1051), so the two are matched by name: the register's product
    name begins with EMA's ("Ebymect" -> "Ebymect 5 mg/1000 mg ...").
    """
    national: dict[str, list[str]] = {}
    for row in rows:
        if row.get("source") != "EMA" and _centrally_authorised(row):
            national.setdefault(str(row.get("country") or ""), []).append(
                " ".join(re.findall(r"[a-z0-9]+", str(row.get("product") or "").lower()))
            )
    if not national:
        return rows

    def listed_nationally(row: dict[str, Any]) -> bool:
        names = national.get(str(row.get("country") or ""))
        brand = " ".join(re.findall(r"[a-z0-9]+", str(row.get("product") or "").lower()))
        return bool(names and brand) and any(name == brand or name.startswith(brand + " ") for name in names)

    return [row for row in rows if not (row.get("source") == "EMA" and listed_nationally(row))]


def suppress_generic_lookup_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    countries_with_connector_rows = {
        row.get("country")
        for row in rows
        if row.get("country") and row.get("source") not in GENERIC_LOOKUP_SOURCES
    }
    if not countries_with_connector_rows:
        return rows
    return [
        row
        for row in rows
        if not (
            row.get("country") in countries_with_connector_rows
            and row.get("source") in GENERIC_LOOKUP_SOURCES
        )
    ]


def parse_sources(sources: list[str] | str | None) -> list[str]:
    if isinstance(sources, str):
        parsed = [source.strip() for source in sources.split(",") if source.strip()]
    else:
        parsed = [source.strip() for source in sources or [] if source.strip()]

    normalized = {source.lower() for source in parsed}
    if "ema" in normalized or "eu" in normalized or "eu national" in normalized:
        for source in EU_NATIONAL_SOURCES:
            if source.lower() not in normalized:
                parsed.append(source)
                normalized.add(source.lower())
    return parsed


def item_matches_selected_sources(item: dict[str, Any], selected_sources: list[str]) -> bool:
    if not selected_sources:
        return True
    selected = {source.strip().lower() for source in selected_sources}
    item_source = (item.get("source") or "").strip().lower()
    if item_source in selected:
        return True
    country = (item.get("country") or "").strip()
    return any(country in SOURCE_COUNTRIES.get(source, set()) for source in selected)


def sources_for_scope(
    selected_sources: list[str],
    country: str = "",
    region: str = "",
    source: str = "",
) -> list[str]:
    selected = parse_sources(selected_sources)
    if source:
        return [source]

    if region == "EU":
        allowed = {"ema"} | {item.lower() for item in EU_NATIONAL_SOURCES}
        return [item for item in selected if item.lower() in allowed]
    if region == "UK":
        return [item for item in selected if item.lower() == "mhra"]
    if region == "US":
        return [item for item in selected if item.lower() == "fda"]
    if region == "CA":
        return [item for item in selected if item.lower() == "health canada"]
    if region == "AU":
        return [item for item in _ensure_source(selected, "TGA Australia") if item.lower() == "tga australia"]
    if region == "NZ":
        return [
            item
            for item in _ensure_source(selected, "Medsafe New Zealand")
            if item.lower() == "medsafe new zealand"
        ]
    if region == "BR":
        return [
            item
            for item in _ensure_source(selected, "ANVISA Brazil")
            if item.lower() == "anvisa brazil"
        ]
    if region == "ALL":
        regional_sources = []
        for sources_for_region in REGIONAL_LIVE_SOURCES.values():
            regional_sources.extend(sources_for_region)
        return [
            item
            for item in selected
            if item in regional_sources or item in DEFAULT_SOURCES
        ] or [LOOKUP_ONLY_SOURCE]
    if region in REGIONAL_COUNTRIES:
        live_sources = REGIONAL_LIVE_SOURCES.get(region, [])
        selected_live_sources = [item for item in selected if item in live_sources]
        return selected_live_sources or [LOOKUP_ONLY_SOURCE]

    if region in REGION_SOURCE_DEFAULTS:
        selected = _ensure_source(selected, REGION_SOURCE_DEFAULTS[region])

    if country:
        if country in COUNTRY_SOURCE_DEFAULTS:
            selected = _ensure_source(selected, COUNTRY_SOURCE_DEFAULTS[country])
        elif any(country in countries for countries in REGIONAL_COUNTRIES.values()):
            return [LOOKUP_ONLY_SOURCE]
        scoped = []
        for item in selected:
            name = item.lower()
            if country in SOURCE_COUNTRIES.get(name, set()):
                scoped.append(item)
        return scoped or selected

    return selected


def combined_search(
    substance: str,
    include_live: bool = True,
    sources: list[str] | None = None,
    live_sources: list[str] | None = None,
    live_timeout: int | None = None,
    force_live: bool = False,
    freshness_report: list[SourceFreshness] | None = None,
) -> list[dict[str, Any]]:
    rows = []
    seen = set()
    index_by_key = {}
    selected_sources = parse_sources(sources)

    for item in search_product_details(substance) + search_medicines(substance):
        if is_connector_lookup_fallback(item):
            continue
        if not item_matches_selected_sources(item, selected_sources):
            continue
        key = result_key(item)
        if key not in seen:
            seen.add(key)
            index_by_key[key] = len(rows)
            rows.append(item)

    if include_live:
        selected_live_sources = parse_sources(live_sources) if live_sources is not None else selected_sources
        if not force_live:
            # Store first: a source checked recently whose answer the store
            # kept is not asked again. See services/freshness.py.
            decisions = source_freshness(substance, selected_live_sources)
            if freshness_report is not None:
                freshness_report.extend(decisions)
            selected_live_sources = [item.source for item in decisions if item.ask_live]
        live_items = search_substance(
            substance,
            source_names=selected_live_sources,
            timeout_seconds=live_timeout or LIVE_SEARCH_TIMEOUT_SECONDS,
        ) if selected_live_sources else []
        store_live_rows_in_background(substance, live_items)
        for item in live_items:
            if is_connector_lookup_fallback(item):
                continue
            key = result_key(item)
            if key in index_by_key:
                existing = rows[index_by_key[key]]
                for field, value in item.items():
                    if value and not existing.get(field):
                        existing[field] = value
            else:
                seen.add(key)
                index_by_key[key] = len(rows)
                rows.append(item)

    return sort_rows(propagate_molecule_fields(rows), selected_sources)


_live_store_lock = threading.Lock()


def store_live_rows(substance: str, items: list[dict[str, Any]]) -> dict[str, int]:
    """Keep every row a live search found, so the next search need not ask.

    Only rows about the molecule are kept: a full-text register returns
    products that merely mention it, and storing those under it is how
    amlodipine came to be saved as atorvastatin. View-only rows and search
    handoffs are refused by the save itself.

    The rows are then read back from the store, and each source whose every
    row came back is logged in live_saves -- what lets the store answer for
    it next time (services/freshness.py). A source that answered only with
    rows about other molecules has nothing to hold, and is logged too.
    """
    answered = {
        str(item.get("source") or "")
        for item in items
        if not is_connector_lookup_fallback(item) and not item.get("view_only")
        and str(item.get("connector_mode") or "").strip().lower() != "manual_registry"
    }
    keep = [
        item for item in items
        if not is_connector_lookup_fallback(item) and row_relevant_to_substance(item, substance)
    ]
    with _live_store_lock:
        stored = save_product_details(keep) if keep else []
        kept = [
            item for item, saved in zip(keep, stored)
            if saved.get("persistence_status") != "SOURCE_RUN_ONLY"
        ]
        with_rows = {str(item.get("source") or "") for item in kept}
        counts = covered_sources(substance, kept)
        counts.update({source: 0 for source in answered - with_rows})
        save_live_saves(substance, counts)
    return counts


def store_live_rows_in_background(substance: str, items: list[dict[str, Any]]) -> None:
    """Store without making the reader wait: thousands of rows take seconds."""
    if not items:
        return
    # Copied here, not in the thread: the page goes on merging into these rows.
    rows = [dict(item) for item in items]

    def run() -> None:
        try:
            store_live_rows(substance, rows)
        except Exception:
            logger.exception("Could not store the live rows for %s", substance)

    threading.Thread(target=run, name=f"store-live-{substance[:20]}", daemon=True).start()


def enriched_cached_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [dict(item) for item in results]
    mhra_rows = [row for row in rows if row.get("source") == "MHRA"]
    if mhra_rows:
        enrich_mhra_document_metadata(mhra_rows)
    enriched_results = attach_ai_enrichment_metadata(
        propagate_molecule_fields(enrich_deep_results(rows))
    )
    # One transaction for the lot: saving row by row cost 0.25 s each, which
    # made exporting a large search take the better part of an hour.
    stored = save_product_details(enriched_results)
    for item, saved in zip(enriched_results, stored):
        # Saving records where each value came from -- the evidence URL, how
        # it was verified, why a blank field is blank -- but on the stored row,
        # not the one in hand. The export writes the row in hand, so without
        # this every row of a job export said "Evidence URL: Not available".
        for field in PROVENANCE_FIELDS:
            if saved.get(field) and not item.get(field):
                item[field] = saved[field]
    return enriched_results


def prepared_cached_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prepare persisted results for display without network or document parsing."""
    rows = [dict(item) for item in results]
    return attach_ai_enrichment_metadata(propagate_molecule_fields(rows))


def filtered_search_results(
    substance: str,
    live: bool,
    sources: list[str],
    country: str = "",
    region: str = "",
    source: str = "",
    status: str = "",
    table_search: str = "",
    substance_filter: str = "",
    product_filter: str = "",
    company_filter: str = "",
    strength_filter: str = "",
    dosage_form_filter: str = "",
    pack_size_filter: str = "",
    atc_code_filter: str = "",
    therapeutic_category_filter: str = "",
    ma_holder_filter: str = "",
    manufacturer_name_filter: str = "",
    manufacturer_country_filter: str = "",
    registration_number_filter: str = "",
    registration_date_filter: str = "",
    sort_by: str = "",
    sort_dir: str = "asc",
    live_timeout: int | None = None,
    live_sources: list[str] | None = None,
    include_lookup_rows: bool = True,
    force_live: bool = False,
    freshness_report: list[SourceFreshness] | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    substance, searched_strength = split_searched_strength(clean_search_term(substance))
    selected_sources = parse_sources(sources)
    scoped_sources = sources_for_scope(
        selected_sources,
        country=country,
        region=region,
        source=source,
    )
    scoped_live_sources = (
        sources_for_scope(
            parse_sources(live_sources),
            country=country,
            region=region,
            source=source,
        )
        if live_sources is not None
        else None
    )
    all_rows = combined_search(
        substance,
        include_live=live,
        sources=scoped_sources,
        live_sources=scoped_live_sources,
        live_timeout=live_timeout,
        force_live=force_live,
        freshness_report=freshness_report,
    )
    all_rows = suppress_ema_copies([
        row for row in all_rows
        if row_relevant_to_substance(row, substance) and matches_searched_strength(row, searched_strength)
    ])
    normalized_sort_dir = "desc" if sort_dir == "desc" else "asc"
    rows = filter_rows(
        all_rows,
        country=country,
        region=region,
        source=source,
        status=status,
        table_search=table_search,
        substance_filter=substance_filter,
        product_filter=product_filter,
        company_filter=company_filter,
        strength_filter=strength_filter,
        dosage_form_filter=dosage_form_filter,
        pack_size_filter=pack_size_filter,
        atc_code_filter=atc_code_filter,
        therapeutic_category_filter=therapeutic_category_filter,
        ma_holder_filter=ma_holder_filter,
        manufacturer_name_filter=manufacturer_name_filter,
        manufacturer_country_filter=manufacturer_country_filter,
        registration_number_filter=registration_number_filter,
        registration_date_filter=registration_date_filter,
    )
    if include_lookup_rows and not source:
        fallback_rows = []
        if region == "EU":
            fallback_rows = _country_lookup_rows(
                substance,
                region=region,
                existing_rows=rows,
            )
        elif region in REGIONAL_COUNTRIES or region == "ALL":
            fallback_rows = _country_lookup_rows(
                substance,
                region=region,
                existing_rows=rows,
            )
        elif country and not rows:
            fallback_rows = _country_lookup_rows(substance, country=country)
        elif region and not rows:
            fallback_rows = _country_lookup_rows(substance, region=region)
        if fallback_rows:
            rows.extend(
                filter_rows(
                    fallback_rows,
                    country=country,
                    region=region,
                    status=status,
                    table_search=table_search,
                    substance_filter=substance_filter,
                    product_filter=product_filter,
                    company_filter=company_filter,
                    strength_filter=strength_filter,
                    dosage_form_filter=dosage_form_filter,
                    pack_size_filter=pack_size_filter,
                    atc_code_filter=atc_code_filter,
                    therapeutic_category_filter=therapeutic_category_filter,
                    ma_holder_filter=ma_holder_filter,
                    manufacturer_name_filter=manufacturer_name_filter,
                    manufacturer_country_filter=manufacturer_country_filter,
                    registration_number_filter=registration_number_filter,
                    registration_date_filter=registration_date_filter,
                )
            )
            all_rows.extend(fallback_rows)

    visible_rows = suppress_generic_lookup_rows(rows)
    if not include_lookup_rows:
        visible_rows = [row for row in visible_rows if row.get("source") not in GENERIC_LOOKUP_SOURCES]
    return (
        sort_rows(visible_rows, selected_sources, sort_by=sort_by, sort_dir=normalized_sort_dir),
        selected_sources,
        all_rows,
    )
