"""Russia: the State Register of Medicinal Products (GRLS), downloaded whole.

grls.rosminzdrav.ru offers the register as one ZIP ("Загрузите ГРЛС одним
файлом", about 19 MB, rebuilt daily) from a link on its search page whose file
id changes with each build. The ZIP holds one workbook per registration state:
valid, amended, expired, excluded, issued under EAEU rules and a few smaller
groups. Each row gives the registration certificate, the holder and its
country, trade name, INN, dosage forms with strengths and packs, and each
production stage with its site and country.

The amended workbook keeps every earlier version of a registration; one row per
certificate is kept, the last one listed.

Everything is in Russian. INNs are transliterated and matched on a folded
spelling (z/s, ks/x, ts/c, g/h) that English INNs fold to as well, so
"Розувастатин" is found by "rosuvastatin". Legal forms, dosage forms, stages
and countries are put into English; company names are transliterated.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable, Iterator

import requests

from core.logging_config import get_logger
from services.english_normalizer import CYRILLIC_TRANSLITERATION
from sources.open_registers import (
    DOWNLOAD_TIMEOUT,
    OpenRegister,
    _connect,
    _is_stale,
    _status_rank,
    add_register,
    current_index,
    download_file,
    index_info,
    inn_tokens,
    query_tokens,
    refresh_in_background,
    RegisterNotReady,
)


logger = get_logger(__name__)

SEARCH_PAGE = "https://grls.rosminzdrav.ru/grls.aspx"
EXPORT_LINK = re.compile(r"GetGRLS\.ashx\?FileGUID=([0-9a-fA-F-]+)(?:&amp;|&)UserReq=(\d+)")

# Workbook name -> state, most authoritative first: a certificate listed in
# several workbooks takes the state of the first.
STATES = [
    ("Действующий", "Registered"),
    ("Выдано_по_правилам_ЕАЭС", "Registered (EAEU rules)"),
    ("Действует_на_подтверждении", "Registered (confirmation pending)"),
    ("Действует_в_иностранных_упаковках", "Registered (foreign packaging)"),
    ("Приостановлено", "Use suspended"),
    ("Изменённый", "Registered (amended)"),
    ("Истёкший", "Registration expired"),
    ("Исключённый", "Registration cancelled"),
]
LEGAL_FORMS = [
    ("Федеральное государственное унитарное предприятие", "FSUE"),
    ("Публичное акционерное общество", "PJSC"),
    ("Открытое акционерное общество", "OJSC"),
    ("Закрытое акционерное общество", "CJSC"),
    ("Непубличное акционерное общество", "JSC"),
    ("Акционерное общество", "JSC"),
    ("Общество с ограниченной ответственностью", "LLC"),
    ("ООО", "LLC"), ("ЗАО", "CJSC"), ("ПАО", "PJSC"), ("ОАО", "OJSC"), ("НАО", "JSC"), ("АО", "JSC"),
    ("ФГУП", "FSUE"), ("ФГБУ", "FSBI"),
]
COUNTRIES = {
    "россия": "Russia", "германия": "Germany", "индия": "India", "швейцария": "Switzerland",
    "словения": "Slovenia", "франция": "France", "сша": "United States", "венгрия": "Hungary",
    "израиль": "Israel", "нидерланды": "Netherlands", "великобритания": "United Kingdom",
    "соединенное королевство": "United Kingdom", "республика беларусь": "Belarus", "беларусь": "Belarus",
    "италия": "Italy", "китай": "China", "дания": "Denmark", "польша": "Poland", "болгария": "Bulgaria",
    "австрия": "Austria", "украина": "Ukraine", "ирландия": "Ireland", "чешская республика": "Czech Republic",
    "швеция": "Sweden", "исландия": "Iceland", "латвия": "Latvia", "республика хорватия": "Croatia",
    "хорватия": "Croatia", "бельгия": "Belgium", "кипр": "Cyprus", "турция": "Turkey", "испания": "Spain",
    "сербия": "Serbia", "республика сербия": "Serbia", "румыния": "Romania", "финляндия": "Finland",
    "республика северная македония": "North Macedonia", "республика македония": "North Macedonia",
    "северная македония": "North Macedonia", "республика казахстан": "Kazakhstan", "казахстан": "Kazakhstan",
    "люксембург": "Luxembourg", "норвегия": "Norway", "пакистан": "Pakistan", "япония": "Japan",
    "канада": "Canada", "португалия": "Portugal", "греция": "Greece", "литва": "Lithuania",
    "эстония": "Estonia", "мальта": "Malta", "словакия": "Slovakia", "словацкая республика": "Slovakia",
    "республика корея": "South Korea", "корея": "South Korea", "аргентина": "Argentina",
    "бразилия": "Brazil", "мексика": "Mexico", "австралия": "Australia", "иран": "Iran",
    "египет": "Egypt", "иордания": "Jordan", "армения": "Armenia", "республика армения": "Armenia",
    "узбекистан": "Uzbekistan", "республика узбекистан": "Uzbekistan", "киргизия": "Kyrgyzstan",
    "кыргызская республика": "Kyrgyzstan", "молдова": "Moldova", "республика молдова": "Moldova",
    "грузия": "Georgia", "азербайджан": "Azerbaijan", "бангладеш": "Bangladesh", "вьетнам": "Vietnam",
    "таиланд": "Thailand", "индонезия": "Indonesia", "малайзия": "Malaysia", "сингапур": "Singapore",
    "тайвань": "Taiwan", "куба": "Cuba", "босния и герцеговина": "Bosnia and Herzegovina",
    "черногория": "Montenegro", "юар": "South Africa", "оаэ": "United Arab Emirates",
    "объединенные арабские эмираты": "United Arab Emirates", "саудовская аравия": "Saudi Arabia",
    "пуэрто-рико": "Puerto Rico", "новая зеландия": "New Zealand",
}
FORMS = [
    ("таблетки, покрытые пленочной оболочкой", "Film-coated tablets"),
    ("таблетки покрытые пленочной оболочкой", "Film-coated tablets"),
    ("таблетки, покрытые оболочкой", "Coated tablets"), ("таблетки покрытые оболочкой", "Coated tablets"),
    ("таблетки с пролонгированным высвобождением", "Prolonged-release tablets"),
    ("таблетки с модифицированным высвобождением", "Modified-release tablets"),
    ("таблетки кишечнорастворимые", "Gastro-resistant tablets"), ("таблетки жевательные", "Chewable tablets"),
    ("таблетки диспергируемые", "Dispersible tablets"), ("таблетки для рассасывания", "Lozenges"),
    ("таблетки шипучие", "Effervescent tablets"), ("таблетки", "Tablets"),
    ("капсулы кишечнорастворимые", "Gastro-resistant capsules"),
    ("капсулы с пролонгированным высвобождением", "Prolonged-release capsules"), ("капсулы", "Capsules"),
    ("порошок для приготовления суспензии для приема внутрь", "Powder for oral suspension"),
    ("порошок для приготовления раствора для приема внутрь", "Powder for oral solution"),
    ("порошок для приготовления раствора для", "Powder for solution for injection/infusion"),
    ("лиофилизат для приготовления", "Lyophilisate for solution"),
    ("концентрат для приготовления раствора для инфузий", "Concentrate for solution for infusion"),
    ("раствор для инфузий", "Solution for infusion"), ("раствор для инъекций", "Solution for injection"),
    ("раствор для внутривенного", "Solution for injection"), ("раствор для внутримышечного", "Solution for injection"),
    ("раствор для подкожного", "Solution for injection"), ("раствор для приема внутрь", "Oral solution"),
    ("раствор для наружного", "Cutaneous solution"), ("суспензия для приема внутрь", "Oral suspension"),
    ("капли глазные", "Eye drops"), ("капли назальные", "Nasal drops"), ("капли для приема внутрь", "Oral drops"),
    ("спрей назальный", "Nasal spray"), ("суппозитории ректальные", "Suppositories"),
    ("суппозитории вагинальные", "Vaginal suppositories"), ("мазь", "Ointment"), ("гель", "Gel"),
    ("крем", "Cream"), ("сироп", "Syrup"), ("гранулы", "Granules"), ("субстанция", "Active substance"),
]
STAGES = [
    ("Производитель (Все стадии", "Manufacturer (all stages)"),
    ("Все стадии", "Manufacturer (all stages)"),
    ("Производитель (готовой ЛФ)", "Manufacturer (finished dosage form)"),
    ("Производство готовой лекарственной формы", "Manufacturer (finished dosage form)"),
    ("Производитель (Выпускающий контроль качества)", "Batch release"),
    ("Выпускающий контроль качества", "Batch release"),
    ("Упаковщик/фасовщик (вторичная/третичная упаковка)", "Secondary packaging"),
    ("Упаковщик/фасовщик (в первичную упаковку)", "Primary packaging"),
    ("Производитель субстанции", "Active substance manufacturer"),
    ("Производитель растворителя", "Solvent manufacturer"),
]
# "д." is a house number only before a digit: "д.д." is Slovenian for a company.
ADDRESS_START = re.compile(
    r"\d|~|(?<![А-Яа-яЁё.])(?:ул|г|обл|пр|пос|р-н|стр|шоссе|проспект|г\.о)\.?(?:\s|$)", re.IGNORECASE
)
MAKES_PRODUCT = {"Manufacturer (all stages)", "Manufacturer (finished dosage form)"}
UNITS = {"мг": "mg", "г": "g", "мкг": "mcg", "мл": "ml", "ме": "IU", "%": "%"}


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("_x000D_", " ").split())


def latin(text: str) -> str:
    return _clean(text).translate(CYRILLIC_TRANSLITERATION)


def fold(token: str) -> str:
    """One spelling for an INN in Russian transliteration and in English."""
    token = token.replace("qu", "cv").replace("x", "cs").replace("ts", "c").replace("w", "v").replace("z", "s")
    token = re.sub(r"(?<!c)h", "g", token)
    return re.sub(r"([a-z])\1+", r"\1", token)


# Russian names an acid with an adjective: "Золедроновая кислота" is
# zoledronic acid, "Фолиевая кислота" folic acid.
_ACID = re.compile(r"([А-Яа-яЁё]+?)(?:ин)?(?:ов|ев)?ая\s+кислота", re.IGNORECASE)


def _acids_in_english(inn: str) -> str:
    def english(found: re.Match) -> str:
        stem = found.group(1)
        # Фоли-евая is folic, Золедрон-овая zoledronic.
        return f"{stem}{'c' if stem[-1:].lower() == 'и' else 'ic'} acid"

    return _ACID.sub(english, inn)


def match_text(inn: str) -> str:
    return " " + " ".join(fold(token) for token in inn_tokens(latin(_acids_in_english(inn)))) + " "


def english_company(name: str) -> str:
    text = _clean(name)
    short = re.search(r"\(([^()]*(?:\([^()]*\))?[^()]*)\)\s*$", text)
    if short and re.search(r"\b(?:ООО|ЗАО|ПАО|ОАО|АО|НАО|ФГУП|ФГБУ)\b", short.group(1)):
        text = short.group(1)
    for russian, english in LEGAL_FORMS:
        text = re.sub(rf"(?<![А-Яа-яЁё]){re.escape(russian)}(?![А-Яа-яЁё])", english, text)
    return latin(text)


def english_country(value: object) -> str:
    text = _clean(value)
    return COUNTRIES.get(text.lower(), latin(text))


def english_form(text: str) -> str:
    lower = _clean(text).lower()
    for russian, english in FORMS:
        if lower.startswith(russian):
            return english
    return ""


def strength(forms: str) -> str:
    """The first strength of the first form: "таблетки, 10 мг+10 мг - блистеры ..." -> "10 mg+10 mg"."""
    first = _clean(forms).split(";")[0]
    for part in first.split(",")[1:]:
        amount = part.split(" - ")[0].strip()
        if re.match(r"^\d", amount) and re.search(r"(мг|мкг|мл|г|МЕ|%)", amount):
            return re.sub(
                r"(мкг|мг|мл|МЕ|г)", lambda unit: UNITS.get(unit.group(1).lower(), unit.group(1)), amount
            )
    return ""


def production(text: str) -> list[dict[str, str]]:
    sites = []
    for line in _clean(text.replace("\n", "|")).split("|"):
        line = line.strip()
        if not line:
            continue
        stage = next((english for russian, english in STAGES if line.startswith(russian)), "")
        rest = line
        closing = line.find("),")
        if line.startswith("Производитель (") and closing != -1:
            rest = line[closing + 2:]
        elif "," in line:
            rest = line.split(",", 1)[1]
        pieces = [piece.strip() for piece in rest.split(",")]
        if not pieces or not pieces[0]:
            continue
        # The name runs until the address starts: a number, "~", or a street.
        name_parts = []
        for piece in pieces[:-1] or pieces:
            if name_parts and ADDRESS_START.search(piece):
                break
            name_parts.append(piece)
        sites.append({
            "name": english_company(", ".join(name_parts)),
            "country": english_country(pieces[-1]) if len(pieces) > 1 else "",
            "role": stage,
        })
    return sites


def fetch(register: OpenRegister, workdir: Path) -> Path:
    page = requests.get(SEARCH_PAGE, timeout=DOWNLOAD_TIMEOUT, headers={"User-Agent": "PharmaSearch/1.0 (regulatory register download)"})
    page.raise_for_status()
    link = EXPORT_LINK.search(page.text)
    if not link:
        raise RuntimeError("GRLS search page no longer links to the register download")
    url = f"https://grls.rosminzdrav.ru/GetGRLS.ashx?FileGUID={link.group(1)}&UserReq={link.group(2)}"
    return download_file(register, url, workdir / "grls.zip")


def read_records(register: OpenRegister, path: Path) -> Iterator[dict[str, Any]]:
    import openpyxl

    chosen: dict[str, dict[str, Any]] = {}
    with zipfile.ZipFile(path) as archive:
        members = {info.filename: info for info in archive.infolist()}
        for key, state in STATES:
            name = next((member for member in members if key in member), None)
            if name is None:
                continue
            workbook = openpyxl.load_workbook(io.BytesIO(archive.read(name)), read_only=True)
            try:
                seen_here: dict[str, dict[str, Any]] = {}
                for values in workbook.worksheets[0].iter_rows(min_row=7, values_only=True):
                    if not values or len(values) < 12:
                        continue
                    number = _clean(values[2])
                    if not number or number == "Номер регистрационного удостоверения" or number in chosen:
                        continue
                    # The amended workbook lists every version; the last wins.
                    seen_here[number] = {
                        "number": number, "registered": _clean(values[3]), "expires": _clean(values[4]),
                        "cancelled": _clean(values[5]), "holder": _clean(values[6]),
                        "holder_country": _clean(values[7]), "trade_name": _clean(values[8]),
                        "inn": _clean(values[9]), "forms": _clean(values[10]),
                        "production": str(values[11] or ""), "group": _clean(values[13]) if len(values) > 13 else "",
                        "state": state,
                    }
                chosen.update(seen_here)
            finally:
                workbook.close()
    yield from chosen.values()


def _iso(value: str) -> str:
    match = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", value)
    return f"{match.group(3)}-{match.group(2)}-{match.group(1)}" if match else value


def build_rows(records: Iterable[dict[str, Any]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for record in records:
        inn = record["inn"] if record["inn"] not in ("~", "") else ""
        if not (inn or record["trade_name"]):
            continue
        sites = production(record["production"])
        makers = [site for site in sites if site["role"] in MAKES_PRODUCT] or sites
        active = latin(inn)
        form = english_form(record["forms"])
        yield match_text(inn or record["trade_name"]), {
            "substance": active,
            "active_substance": active,
            "source_substance": active,
            "local_substance": inn,
            "product": latin(record["trade_name"]),
            "local_product_name": record["trade_name"],
            "company": english_company(record["holder"]),
            "ma_holder_country": english_country(record["holder_country"]),
            "country": "Russia",
            "region": "RU",
            "status": record["state"],
            "authorisation_scope": "Active substance (API)" if form == "Active substance" else "",
            "strength": strength(record["forms"]),
            "dosage_form": form,
            "local_pack_size": record["forms"][:400],
            "registration_number": latin(record["number"]),
            "registration_date": _iso(record["registered"]),
            "expiry_date": _iso(record["expires"]),
            "cancellation_date": _iso(record["cancelled"]),
            "manufacturer_name": "; ".join(dict.fromkeys(site["name"] for site in makers)),
            "manufacturer_country": "; ".join(dict.fromkeys(site["country"] for site in makers if site["country"])),
            "manufacturer_source": "State Register of Medicinal Products (GRLS)" if makers else "",
            "manufacturers": [{**site, "verification_status": "VERIFIED_REGISTER"} for site in sites],
            # The INN in the folded spelling the search matched on, so the
            # relevance check can compare "Rozuvastatin" with "rosuvastatin".
            "inn_fold_tokens": match_text(inn or record["trade_name"]).strip(),
            "source": "GRLS Russia",
            "source_url": SEARCH_PAGE,
            "product_url": "",
            "document_type": "GRLS registration certificate",
            "last_checked": fetched_at,
        }


GRLS_REGISTER = add_register(OpenRegister(
    source="GRLS Russia",
    country="Russia",
    region="RU",
    url=SEARCH_PAGE,
    slug="grls_russia",
    max_age_seconds=7 * 24 * 3600,
    build_rows=build_rows,
    fetch=fetch,
    read_records=read_records,
))


def run_grls_register_search(substance: str) -> list[dict[str, Any]]:
    """Rows whose INN holds every word of the molecule, both spelled the folded way."""
    import json

    tokens = [fold(token) for token in query_tokens(substance)]
    if not tokens:
        return []
    path = current_index(GRLS_REGISTER)
    if path is None:
        started = refresh_in_background(GRLS_REGISTER)
        raise RegisterNotReady(
            "GRLS Russia register is being downloaded for the first time"
            f"{'' if started else ' (already in progress)'}; search again in a few minutes."
        )
    if _is_stale(GRLS_REGISTER, index_info(GRLS_REGISTER)):
        refresh_in_background(GRLS_REGISTER)
    where = " AND ".join("match LIKE ?" for _ in tokens)
    with _connect(path) as conn:
        rows = [json.loads(payload) for (payload,) in conn.execute(
            f"SELECT payload FROM rows WHERE {where}", [f"% {token} %" for token in tokens]
        )]
    rows.sort(key=lambda row: (_status_rank(row), row.get("product", "").lower()))
    return rows[:GRLS_REGISTER.max_results]
