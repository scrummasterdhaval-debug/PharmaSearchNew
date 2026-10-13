"""Registers three more regulators publish whole as open data.

    Singapore  HSA "Listing of Registered Therapeutic Products" on data.gov.sg,
               CSV through the portal's download API; holder, manufacturer and
               its country, ATC, strength.
    Ukraine    State Register of Medicinal Products, the Ministry's own CSV
               (semicolon, Windows-1251); applicant, up to five manufacturers
               with countries, registration number and validity.
    Ireland    HPRA's daily XML list of every authorised human medicine;
               holder, licence number, marketing state, legal basis, ATC.

Each is indexed locally like Italy's and Brazil's registers (open_registers).
Ukrainian text is Ukrainian; the country, the dosage form, the salt and the
status are put into English here, names are transliterated where shown.
"""
from __future__ import annotations

import csv
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

import requests

from sources.parser import extract_strength
from sources.open_registers import (
    DOWNLOAD_TIMEOUT,
    OpenRegister,
    _match_text,
    add_register,
    download_file,
    search_register,
)


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _multi(value: object) -> str:
    """HSA joins several values with "&&": "Singapore&&Puerto Rico"."""
    return "; ".join(dict.fromkeys(part.strip() for part in _clean(value).split("&&") if part.strip()))


def _date(value: object, day_first: bool = True) -> str:
    text = _clean(value)
    for pattern in ("%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    return text


# ---------------------------------------------------------------- Singapore

SINGAPORE_DATASET = "d_767279312753558cbf19d48344577084"
SINGAPORE_API = f"https://api-open.data.gov.sg/v1/public/api/datasets/{SINGAPORE_DATASET}"
SINGAPORE_PAGE = f"https://data.gov.sg/datasets/{SINGAPORE_DATASET}/view"


def fetch_singapore(register: OpenRegister, workdir: Path) -> Path:
    """data.gov.sg hands out a short-lived link to the current CSV."""
    response = requests.get(f"{SINGAPORE_API}/initiate-download", timeout=DOWNLOAD_TIMEOUT)
    response.raise_for_status()
    url = (response.json().get("data") or {}).get("url")
    if not url:
        poll = requests.get(f"{SINGAPORE_API}/poll-download", timeout=DOWNLOAD_TIMEOUT)
        poll.raise_for_status()
        url = (poll.json().get("data") or {}).get("url")
    if not url:
        raise RuntimeError("data.gov.sg gave no download link for the HSA listing")
    return download_file(register, url, workdir / "hsa_therapeutic_products.csv")


def read_singapore(register: OpenRegister, path: Path) -> Iterator[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        yield from csv.DictReader(handle)


def build_singapore_rows(records: Iterable[dict[str, str]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for record in records:
        active = _multi(record.get("Activeingredients"))
        product = _clean(record.get("Productname"))
        if not (active or product):
            continue
        maker = _multi(record.get("Manufacturer"))
        yield _match_text(active or product), {
            "substance": active,
            "active_substance": active,
            "source_substance": active,
            "product": product,
            "company": _clean(record.get("Licenseholder")),
            "country": "Singapore",
            "region": "AS",
            "status": "Registered",
            "classification": _clean(record.get("Forensicclassification")),
            # One strength per ingredient, repeats included: "10mg&&10mg".
            "strength": "/".join(part.strip() for part in _clean(record.get("Strength")).split("&&") if part.strip()),
            "dosage_form": _clean(record.get("Dosageform")).capitalize(),
            "route": _clean(record.get("RouteofAdministration")).capitalize(),
            "atc_code": _multi(record.get("ATCCode")),
            "registration_number": _clean(record.get("LicenceNo")),
            "registration_date": _date(record.get("Approvaldate")),
            "manufacturer_name": maker,
            "manufacturer_country": _multi(record.get("Countryofmanufacturer")).title(),
            "manufacturer_source": "HSA listing of registered therapeutic products" if maker else "",
            "source": "HSA Singapore",
            "source_url": SINGAPORE_PAGE,
            "product_url": "",
            "document_type": "HSA registered therapeutic product",
            "last_checked": fetched_at,
        }


HSA_SINGAPORE = add_register(OpenRegister(
    source="HSA Singapore",
    country="Singapore",
    region="AS",
    url=SINGAPORE_PAGE,
    slug="hsa_singapore",
    max_age_seconds=7 * 24 * 3600,
    build_rows=build_singapore_rows,
    fetch=fetch_singapore,
    read_records=read_singapore,
))


def run_hsa_singapore_search(substance: str) -> list[dict[str, Any]]:
    return search_register(HSA_SINGAPORE, substance)


# ---------------------------------------------------------------- Ukraine

UKRAINE_URL = "http://www.drlz.com.ua/ibp/zvity.nsf/all/zvit/$file/reestr.csv"
UKRAINE_SEARCH = "http://www.drlz.com.ua/"

UKRAINE_COUNTRIES = {
    "україна": "Ukraine", "індія": "India", "німеччина": "Germany", "китай": "China",
    "китайська народна республіка": "China", "іспанія": "Spain", "італія": "Italy",
    "словенія": "Slovenia", "туреччина": "Turkey", "франція": "France", "польща": "Poland",
    "греція": "Greece", "угорщина": "Hungary", "швейцарія": "Switzerland", "австрія": "Austria",
    "румунія": "Romania", "бельгія": "Belgium", "болгарія": "Bulgaria", "ірландія": "Ireland",
    "чеська республіка": "Czech Republic", "чехія": "Czech Republic", "кіпр": "Cyprus",
    "сша": "United States", "сполучені штати америки": "United States", "латвія": "Latvia",
    "велика британія": "United Kingdom", "англія": "United Kingdom", "сполучене королівство": "United Kingdom",
    "нідерланди": "Netherlands", "хорватія": "Croatia", "канада": "Canada", "данія": "Denmark",
    "швеція": "Sweden", "мальта": "Malta", "республіка північна македонія": "North Macedonia",
    "північна македонія": "North Macedonia", "фінляндія": "Finland", "японія": "Japan",
    "португалія": "Portugal", "ізраїль": "Israel", "словацька республіка": "Slovakia",
    "словаччина": "Slovakia", "боснія і герцеговина": "Bosnia and Herzegovina",
    "республіка корея": "South Korea", "корея": "South Korea", "литва": "Lithuania",
    "пакистан": "Pakistan", "тайвань": "Taiwan", "мексика": "Mexico", "естонія": "Estonia",
    "іран": "Iran", "йорданія": "Jordan", "грузія": "Georgia", "республіка вірменія": "Armenia",
    "вірменія": "Armenia", "таїланд": "Thailand", "індонезія": "Indonesia",
    "республіка аргентина": "Argentina", "аргентина": "Argentina", "республіка сербія": "Serbia",
    "сербія": "Serbia", "єгипет": "Egypt", "оман": "Oman", "бразилія": "Brazil",
    "бангладеш": "Bangladesh", "австралія": "Australia", "норвегія": "Norway", "ісландія": "Iceland",
    "люксембург": "Luxembourg", "молдова": "Moldova", "республіка молдова": "Moldova",
    "білорусь": "Belarus", "казахстан": "Kazakhstan", "узбекистан": "Uzbekistan",
    "в'єтнам": "Vietnam", "сінгапур": "Singapore", "малайзія": "Malaysia", "філіппіни": "Philippines",
    "південна африка": "South Africa", "пар": "South Africa", "об'єднані арабські емірати": "United Arab Emirates",
    "оае": "United Arab Emirates", "саудівська аравія": "Saudi Arabia", "ліван": "Lebanon",
    "сирія": "Syria", "ірак": "Iraq", "куба": "Cuba", "чилі": "Chile", "колумбія": "Colombia",
    "нова зеландія": "New Zealand", "ліхтенштейн": "Liechtenstein", "монако": "Monaco",
    "чорногорія": "Montenegro", "албанія": "Albania", "шрі-ланка": "Sri Lanka", "непал": "Nepal",
}

UKRAINE_FORMS = [
    ("таблетки, вкриті плівковою оболонкою", "Film-coated tablets"),
    ("таблетки вкриті плівковою оболонкою", "Film-coated tablets"),
    ("таблетки, вкриті оболонкою", "Coated tablets"),
    ("таблетки пролонгованої дії", "Prolonged-release tablets"),
    ("таблетки з модифікованим вивільненням", "Modified-release tablets"),
    ("таблетки, що диспергуються в ротовій порожнині", "Orodispersible tablets"),
    ("таблетки жувальні", "Chewable tablets"),
    ("таблетки шипучі", "Effervescent tablets"),
    ("таблетки гастрорезистентні", "Gastro-resistant tablets"),
    ("таблетки", "Tablets"),
    ("капсули тверді", "Hard capsules"),
    ("капсули м'які", "Soft capsules"),
    ("капсули", "Capsules"),
    ("порошок для оральної суспензії", "Powder for oral suspension"),
    ("порошок для орального розчину", "Powder for oral solution"),
    ("порошок для розчину для ін'єкцій", "Powder for solution for injection"),
    ("порошок для розчину для інфузій", "Powder for solution for infusion"),
    ("порошок для концентрату для розчину для інфузій", "Powder for concentrate for solution for infusion"),
    ("ліофілізат для розчину для ін'єкцій", "Lyophilisate for solution for injection"),
    ("ліофілізат для розчину для інфузій", "Lyophilisate for solution for infusion"),
    ("концентрат для розчину для інфузій", "Concentrate for solution for infusion"),
    ("розчин для ін'єкцій", "Solution for injection"),
    ("розчин для інфузій", "Solution for infusion"),
    ("розчин оральний", "Oral solution"),
    ("розчин для зовнішнього застосування", "Cutaneous solution"),
    ("суспензія оральна", "Oral suspension"),
    ("суспензія для ін'єкцій", "Suspension for injection"),
    ("краплі очні", "Eye drops"),
    ("краплі оральні", "Oral drops"),
    ("спрей назальний", "Nasal spray"),
    ("супозиторії ректальні", "Suppositories"),
    ("супозиторії вагінальні", "Vaginal suppositories"),
    ("сироп", "Syrup"),
    ("мазь", "Ointment"),
    ("гель", "Gel"),
    ("крем", "Cream"),
    ("гранули", "Granules"),
    ("порошок", "Powder"),
]
UKRAINE_SALTS = [
    ("гідрохлорид", "hydrochloride"), ("гідробромід", "hydrobromide"), ("карбонат", "carbonate"),
    ("кальцію", "calcium"), ("натрію", "sodium"), ("калію", "potassium"), ("магнію", "magnesium"),
    ("малеат", "maleate"), ("безилат", "besilate"), ("мезилат", "mesilate"), ("тартрат", "tartrate"),
    ("фумарат", "fumarate"), ("сукцинат", "succinate"), ("сульфат", "sulfate"), ("фосфат", "phosphate"),
    ("ацетат", "acetate"), ("цитрат", "citrate"), ("моногідрат", "monohydrate"), ("дигідрат", "dihydrate"),
    ("тригідрат", "trihydrate"), ("гемігідрат", "hemihydrate"),
]
UKRAINE_UNITS = {"мг": "mg", "г": "g", "мкг": "mcg", "мл": "ml", "%": "%", "мо": "IU", "од": "units"}


def _apostrophes(text: str) -> str:
    return re.sub(r"[’`ʼ]", "'", text)


def ukraine_country(value: object) -> str:
    text = _apostrophes(_clean(value)).lower()
    return UKRAINE_COUNTRIES.get(text, _clean(value))


def ukraine_form(value: object) -> str:
    text = _apostrophes(_clean(value)).lower()
    for ukrainian, english in UKRAINE_FORMS:
        if text.startswith(ukrainian):
            return english
    return ""


def ukraine_strength(value: object) -> str:
    """ "таблетки по 10 мг+10 мг, по 10 таблеток ..." -> "10 mg+10 mg"."""
    text = _clean(value).replace(",", ".")
    match = re.search(
        r"(\d+(?:\.\d+)?\s*(?:мг|мкг|мл|г|%|МО|ОД)(?:\s*(?:/|\+)\s*\d+(?:\.\d+)?\s*(?:мг|мкг|мл|г|%|МО|ОД)?)*)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return ""
    return re.sub(
        r"(мкг|мг|мл|МО|ОД|г)",
        lambda unit: UKRAINE_UNITS.get(unit.group(1).lower(), unit.group(1)),
        match.group(1),
        flags=re.IGNORECASE,
    )


def ukraine_active(inn: str, composition: str) -> str:
    """The Latin INN, with the salt the composition names: "Sevelamer hydrochloride"."""
    if not inn or " and " in inn.lower() or "," in inn:
        return inn
    lower = composition.lower()
    salts = [english for ukrainian, english in UKRAINE_SALTS if ukrainian in lower]
    extra = [salt for salt in dict.fromkeys(salts) if salt not in inn.lower()]
    return " ".join([inn, *extra])


def build_ukraine_rows(records: Iterable[dict[str, str]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    today = date.today().isoformat()
    for record in records:
        inn = _clean(record.get("Міжнародне непатентоване найменування"))
        trade = _clean(record.get("Торгівельне найменування"))
        if not (inn or trade) or _clean(record.get("Гомеопатичний ЛЗ")) == "Так":
            continue
        form_text = _clean(record.get("Форма випуску"))
        is_substance = "субстанці" in form_text.lower()
        makers = []
        for index in range(1, 6):
            name = _clean(record.get(f"Виробник {index}: назва українською"))
            if not name:
                continue
            role = re.search(r"\(([^()]*)\)\s*$", name)
            makers.append({
                "name": re.sub(r"\s*\([^()]*\)\s*$", "", name),
                "country": ukraine_country(record.get(f"Виробник {index}: країна ")),
                "role": role.group(1) if role else "",
            })
        expiry = _clean(record.get("Дата закінчення"))
        expiry_iso = "" if expiry.lower() == "необмежений" else _date(expiry)
        if _clean(record.get("Дострокове припинення")) == "Так":
            status = "Registration terminated early"
        elif expiry_iso and expiry_iso < today:
            status = "Registration expired"
        else:
            status = "Registered"
        active = ukraine_active(inn, _clean(record.get("Склад (діючі)")))
        yield _match_text(active or inn or trade), {
            "substance": active,
            "active_substance": active,
            "source_substance": active,
            "product": trade,
            "company": _clean(record.get("Заявник: назва українською")),
            "ma_holder_country": ukraine_country(record.get("Заявник: країна")),
            "country": "Ukraine",
            "region": "EU",
            "status": status,
            "authorisation_scope": "Active substance (API)" if is_substance else "",
            "strength": ukraine_strength(form_text),
            "dosage_form": "Active substance" if is_substance else ukraine_form(form_text),
            "atc_code": _clean(record.get("Код АТС 1")),
            "registration_number": _clean(record.get("Номер Реєстраційного посвідчення")),
            "registration_date": _date(record.get("Дата початку дії")),
            "expiry_date": expiry_iso or ("Unlimited" if expiry else ""),
            "manufacturer_name": "; ".join(maker["name"] for maker in makers),
            "manufacturer_country": "; ".join(dict.fromkeys(maker["country"] for maker in makers if maker["country"])),
            "manufacturer_source": "State Register of Medicinal Products of Ukraine" if makers else "",
            "manufacturers": [
                {**maker, "verification_status": "VERIFIED_REGISTER"} for maker in makers
            ],
            "pil_url": _clean(record.get("URL інструкції")),
            "source": "Ukraine DRLZ",
            "source_url": UKRAINE_SEARCH,
            "product_url": "",
            "document_type": "State Register of Medicinal Products entry",
            "last_checked": fetched_at,
        }


UKRAINE_REGISTER = add_register(OpenRegister(
    source="Ukraine DRLZ",
    country="Ukraine",
    region="EU",
    url=UKRAINE_URL,
    slug="ukraine_drlz",
    encoding="cp1251",
    max_age_seconds=7 * 24 * 3600,
    build_rows=build_ukraine_rows,
))


def run_ukraine_drlz_search(substance: str) -> list[dict[str, Any]]:
    return search_register(UKRAINE_REGISTER, substance)


# ---------------------------------------------------------------- Ireland

IRELAND_URL = "https://assets.hpra.ie/products/xml/latestHumanlist.xml"
IRELAND_SEARCH = "https://www.hpra.ie/find-a-medicine/for-human-use/authorised-medicines"
MARKET_INFO = {"Marketed": "Marketed", "Not marketed": "Authorised, not marketed", "Unknown": "Authorised"}
LEGAL_BASIS = [
    ("Generic application", "Generic"),
    ("Hybrid application", "Hybrid"),
    ("Parallel importation", "Parallel import"),
    ("Well-established use", "Well-established use"),
    ("New active substance", "New active substance"),
    ("Full application", "Full application"),
    ("Similar biological", "Biosimilar"),
    ("Fixed combination", "Fixed combination"),
    ("Informed consent", "Informed consent"),
]


def read_ireland(register: OpenRegister, path: Path) -> Iterator[dict[str, Any]]:
    for _event, element in ET.iterparse(path):
        if not element.tag.endswith("}Product"):
            continue
        record: dict[str, Any] = {}
        for child in element:
            name = child.tag.split("}", 1)[1]
            items = [_clean(item.text) for item in child.iter() if item is not child and _clean(item.text)]
            record[name] = items if items else _clean(child.text)
        yield record
        element.clear()


def _as_list(value: Any) -> list[str]:
    return value if isinstance(value, list) else ([value] if value else [])


def build_ireland_rows(records: Iterable[dict[str, Any]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for record in records:
        actives = _as_list(record.get("ActiveSubstances"))
        product = _clean(record.get("ProductName"))
        if not (actives or product):
            continue
        active = "; ".join(actives)
        basis = _clean(record.get("LegalBasis"))
        scope = next((label for prefix, label in LEGAL_BASIS if basis.startswith(prefix)), basis)
        licence = _clean(record.get("LicenceNumber"))
        yield _match_text(active or product), {
            "substance": active,
            "active_substance": active,
            "source_substance": active,
            "product": product,
            "company": _clean(record.get("PAHolder")),
            "country": "Ireland",
            "region": "EU",
            "status": MARKET_INFO.get(_clean(record.get("MarketInfo")), "Authorised"),
            "authorisation_scope": scope,
            "strength": extract_strength(product),
            "dosage_form": _clean(record.get("DosageForm")),
            "route": "; ".join(_as_list(record.get("RoutesOfAdministration"))),
            "atc_code": "; ".join(_as_list(record.get("ATCs"))),
            "classification": "; ".join(_as_list(record.get("DispensingLegalStatus"))),
            "registration_number": licence,
            "registration_date": _date(record.get("AuthorisedDate")),
            "source": "HPRA Ireland",
            "source_url": IRELAND_SEARCH,
            "product_url": IRELAND_SEARCH,
            "document_type": "HPRA authorised human medicine",
            "last_checked": fetched_at,
        }


HPRA_IRELAND = add_register(OpenRegister(
    source="HPRA Ireland",
    country="Ireland",
    region="EU",
    url=IRELAND_URL,
    slug="hpra_ireland",
    max_age_seconds=7 * 24 * 3600,
    build_rows=build_ireland_rows,
    read_records=read_ireland,
))


def run_hpra_ireland_search(substance: str) -> list[dict[str, Any]]:
    return search_register(HPRA_IRELAND, substance)
