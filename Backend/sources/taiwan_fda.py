"""Taiwan: the TFDA's register of every drug licence, with English company names.

    Licences  TFDA open data set 36, "全部藥品許可證資料集" (all drug licences),
              ZIP of one CSV, weekly: licence number and state, English and
              Chinese names, dosage form, ingredients, applicant with its
              business number, and one line per manufacturer with its country.
              https://data.fda.gov.tw/data/opendata/export/36/csv
    Names     The International Trade Administration's register of importers
              and exporters, daily: every registered trader's business number
              with its Chinese and official English name.
              https://fbfh.trade.gov.tw/opendata/companyData.csv

Taiwanese companies appear in the licence file under their Chinese names only.
Every pharmaceutical importer or exporter must register an English name with
the Trade Administration, so the business number gives the company's own
English name, not a translation. A manufacturer has no business number in the
file; its Chinese name is matched against the same register, plant suffix
included. A name the register does not hold stays in Chinese.
"""
from __future__ import annotations

import csv
import io
import re
import shutil
import time
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator

import requests

from core.logging_config import get_logger
from sources.open_registers import INDEX_DIR, OpenRegister, _match_text, add_register, download_file, search_register


logger = get_logger(__name__)


LICENCES_URL = "https://data.fda.gov.tw/data/opendata/export/36/csv"
NAMES_URL = "https://fbfh.trade.gov.tw/opendata/companyData.csv"
SEARCH_URL = "https://info.fda.gov.tw/MLMS/H0001.aspx"

LICENCE_TYPES = [
    ("衛部藥製字", "MOHW-PM"), ("衛署藥製字", "DOH-PM"),
    ("衛部藥輸字", "MOHW-PI"), ("衛署藥輸字", "DOH-PI"),
    ("衛部藥陸輸字", "MOHW-PC"), ("衛署藥陸輸字", "DOH-PC"),
    ("衛部菌疫製字", "MOHW-BM"), ("衛部菌疫輸字", "MOHW-BI"),
    ("衛署菌疫製字", "DOH-BM"), ("衛署菌疫輸字", "DOH-BI"),
    ("內衛藥製字", "MOI-PM"), ("內衛藥輸字", "MOI-PI"),
]
CATEGORIES = {
    "須由醫師處方使用": "Prescription only",
    "醫師藥師藥劑生指示藥品": "Pharmacist-directed (non-prescription)",
    "成藥": "Over the counter",
    "乙類成藥": "Over the counter (class B)",
    "甲類成藥": "Over the counter (class A)",
    "製劑原料": "Active substance (API)",
}
FORMS = [
    ("持續性藥效膜衣錠", "Prolonged-release film-coated tablet"), ("持續性藥效錠", "Prolonged-release tablet"),
    ("持續性藥效膠囊", "Prolonged-release capsule"), ("腸溶膜衣錠", "Gastro-resistant film-coated tablet"),
    ("腸溶錠", "Gastro-resistant tablet"), ("腸溶膠囊", "Gastro-resistant capsule"),
    ("膜衣錠", "Film-coated tablet"), ("糖衣錠", "Sugar-coated tablet"), ("口溶錠", "Orodispersible tablet"),
    ("口溶膜", "Orodispersible film"), ("咀嚼錠", "Chewable tablet"), ("發泡錠", "Effervescent tablet"),
    ("舌下錠", "Sublingual tablet"), ("錠劑", "Tablet"), ("軟膠囊", "Soft capsule"), ("膠囊劑", "Capsule"),
    ("口服懸液用粉劑", "Powder for oral suspension"), ("口服液用粉劑", "Powder for oral solution"),
    ("凍晶注射劑", "Powder for solution for injection (lyophilised)"), ("乾粉注射劑", "Powder for injection"),
    ("注射液劑", "Solution for injection"), ("注射劑", "Injection"), ("輸注液", "Solution for infusion"),
    ("懸液劑", "Suspension"), ("口服液劑", "Oral solution"), ("內服液劑", "Oral solution"), ("液劑", "Solution"),
    ("糖漿劑", "Syrup"), ("顆粒劑", "Granules"), ("細粒劑", "Fine granules"), ("散劑", "Powder"),
    ("點眼液劑", "Eye drops"), ("眼藥水", "Eye drops"), ("眼用軟膏", "Eye ointment"), ("點鼻液劑", "Nasal drops"),
    ("鼻用噴霧劑", "Nasal spray"), ("吸入劑", "Inhalation"), ("噴霧劑", "Spray"), ("軟膏劑", "Ointment"),
    ("乳膏劑", "Cream"), ("凝膠劑", "Gel"), ("乳液劑", "Lotion"), ("貼片劑", "Transdermal patch"),
    ("貼布劑", "Patch"), ("栓劑", "Suppository"), ("陰道錠", "Vaginal tablet"), ("粉劑", "Powder"),
    ("錠", "Tablet"), ("膠囊", "Capsule"),
]
COUNTRIES = {
    "TW": "Taiwan", "US": "United States", "GB": "United Kingdom", "UK": "United Kingdom", "DE": "Germany",
    "FR": "France", "IT": "Italy", "ES": "Spain", "IE": "Ireland", "NL": "Netherlands", "BE": "Belgium",
    "CH": "Switzerland", "AT": "Austria", "SE": "Sweden", "DK": "Denmark", "FI": "Finland", "NO": "Norway",
    "PL": "Poland", "PT": "Portugal", "GR": "Greece", "HU": "Hungary", "CZ": "Czech Republic", "SI": "Slovenia",
    "SK": "Slovakia", "HR": "Croatia", "RO": "Romania", "BG": "Bulgaria", "MT": "Malta", "CY": "Cyprus",
    "LT": "Lithuania", "LV": "Latvia", "EE": "Estonia", "LU": "Luxembourg", "IS": "Iceland", "JP": "Japan",
    "KR": "South Korea", "CN": "China", "HK": "Hong Kong", "IN": "India", "SG": "Singapore", "MY": "Malaysia",
    "TH": "Thailand", "ID": "Indonesia", "PH": "Philippines", "VN": "Vietnam", "PK": "Pakistan",
    "BD": "Bangladesh", "AU": "Australia", "NZ": "New Zealand", "CA": "Canada", "MX": "Mexico", "BR": "Brazil",
    "AR": "Argentina", "IL": "Israel", "TR": "Turkey", "PR": "Puerto Rico", "ZA": "South Africa",
    "JO": "Jordan", "SA": "Saudi Arabia", "AE": "United Arab Emirates", "EG": "Egypt", "RU": "Russia",
    "UA": "Ukraine", "RS": "Serbia", "MK": "North Macedonia", "BA": "Bosnia and Herzegovina", "CL": "Chile",
    "CO": "Colombia", "UY": "Uruguay", "CU": "Cuba", "LK": "Sri Lanka",
}
PLANT = re.compile(r"(?:股份有限公司|有限公司)(?P<plant>.*廠)$")


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _has_chinese(text: str) -> bool:
    return bool(re.search(r"[㐀-鿿]", text))


def english_licence_number(value: object) -> str:
    """ "衛部藥製字第058818號" -> "MOHW-PM 058818"."""
    text = _clean(value)
    for chinese, english in LICENCE_TYPES:
        if text.startswith(chinese):
            number = re.search(r"第\s*(\d+)\s*號", text)
            return f"{english} {number.group(1)}" if number else english
    return text


def english_form(value: object) -> str:
    text = _clean(value).strip("（）() ")
    for chinese, english in FORMS:
        if chinese in text:
            return english
    return "" if _has_chinese(text) else text


def english_country(value: object) -> str:
    code = _clean(value).upper()
    return COUNTRIES.get(code, code)


NAMES_MAX_AGE_SECONDS = 30 * 24 * 3600
NAMES_TIMEOUT = (30, 300)


def _names_cache() -> Path:
    return INDEX_DIR / "taiwan_trade_names.csv"


def refresh_trade_names() -> Path | None:
    """The trade register, kept beside the indexes and fetched at most monthly.

    The file is about 100 MB and the Trade Administration serves it at some
    20 KB/s, so a download takes over an hour. It is resumed where an earlier
    attempt stopped, and swapped in only when complete.
    """
    cache = _names_cache()
    if cache.exists() and time.time() - cache.stat().st_mtime < NAMES_MAX_AGE_SECONDS:
        return cache
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    partial = cache.with_suffix(".part")
    try:
        head = requests.head(NAMES_URL, timeout=NAMES_TIMEOUT, allow_redirects=True)
        total = int(head.headers.get("Content-Length") or 0)
        for _attempt in range(20):
            have = partial.stat().st_size if partial.exists() else 0
            if total and have >= total:
                break
            headers = {"User-Agent": "PharmaSearch/1.0 (regulatory register download)"}
            if have:
                headers["Range"] = f"bytes={have}-"
            try:
                with requests.get(NAMES_URL, headers=headers, stream=True, timeout=NAMES_TIMEOUT) as response:
                    response.raise_for_status()
                    mode = "ab" if have and response.status_code == 206 else "wb"
                    with partial.open(mode) as handle:
                        for chunk in response.iter_content(chunk_size=1 << 20):
                            handle.write(chunk)
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
                logger.info("Taiwan trade register: download interrupted at %s bytes, resuming", have)
                continue
            if not total:
                break
        if total and partial.stat().st_size < total:
            raise RuntimeError(f"trade register incomplete: {partial.stat().st_size} of {total} bytes")
        partial.replace(cache)
        return cache
    except Exception:
        logger.exception("Taiwan trade register could not be refreshed")
        return cache if cache.exists() else None


def fetch(register: OpenRegister, workdir: Path) -> Path:
    download_file(register, LICENCES_URL, workdir / "tfda_licences.zip")
    names = refresh_trade_names()
    if names is not None:
        shutil.copyfile(names, workdir / "trade_names.csv")
    else:
        # Indexed now with the names the licence file gives; the English
        # names arrive with the next refresh once the register is fetched.
        logger.warning("Taiwan: indexing without English company names")
    return workdir


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp950", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_names(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """English names by business number, and by Chinese name."""
    by_number: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(_decode(path.read_bytes()))):
        english = _clean(row.get("廠商英文名稱"))
        if not english:
            continue
        number = _clean(row.get("統一編號"))
        chinese = _clean(row.get("廠商中文名稱"))
        if number:
            by_number[number] = english
        if chinese:
            by_name.setdefault(chinese, english)
    return by_number, by_name


def english_company(chinese: str, number: str, by_number: dict[str, str], by_name: dict[str, str]) -> str:
    """The company's registered English name; a plant is named after its company."""
    name = _clean(chinese)
    if not _has_chinese(name):
        return name
    if number and number in by_number:
        return by_number[number]
    if name in by_name:
        return by_name[name]
    plant = PLANT.search(name)
    if plant:
        company = name[: plant.start("plant")]
        if company in by_name:
            return f"{by_name[company]} (manufacturing plant)"
    return name


def read_records(register: OpenRegister, workdir: Path) -> Iterator[dict[str, Any]]:
    names = workdir / "trade_names.csv"
    by_number, by_name = read_names(names) if names.exists() else ({}, {})
    with zipfile.ZipFile(workdir / "tfda_licences.zip") as archive:
        member = next(name for name in archive.namelist() if name.lower().endswith(".csv"))
        with archive.open(member) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
            licences: dict[str, dict[str, Any]] = {}
            for row in reader:
                number = _clean(row.get("許可證字號"))
                if not number:
                    continue
                licence = licences.get(number)
                if licence is None:
                    licence = dict(row)
                    licence["_makers"] = []
                    licence["_holder"] = english_company(
                        row.get("申請商名稱", ""), _clean(row.get("申請商統一編號")), by_number, by_name
                    )
                    licences[number] = licence
                maker = _clean(row.get("製造商名稱"))
                if maker:
                    entry = {
                        "name": english_company(maker, "", by_number, by_name),
                        "country": english_country(row.get("製造廠國別")),
                        "process": _clean(row.get("製程")),
                    }
                    if entry not in licence["_makers"]:
                        licence["_makers"].append(entry)
    yield from licences.values()


def _status(record: dict[str, Any], today: str) -> str:
    cancelled = _clean(record.get("註銷狀態"))
    if cancelled:
        when = _clean(record.get("註銷日期")).replace("/", "-")
        return f"Cancelled ({when})" if when else "Cancelled"
    valid = _clean(record.get("有效日期")).replace("/", "-")
    if valid and valid < today:
        return f"Expired ({valid})"
    return "Valid"


def build_rows(records: Iterable[dict[str, Any]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    today = date.today().isoformat()
    for record in records:
        english_name = _clean(record.get("英文品名"))
        ingredients = list(dict.fromkeys(
            part.strip() for part in _clean(record.get("主成分略述")).split(";;") if part.strip()
        ))
        active = "; ".join(ingredients)
        category = _clean(record.get("藥品類別"))
        is_api = category == "製劑原料"
        if not active and is_api:
            active = english_name
        if not (active or english_name):
            continue
        makers = record.get("_makers") or []
        yield _match_text(active or english_name), {
            "substance": active,
            "active_substance": active,
            "source_substance": active,
            "product": english_name or _clean(record.get("中文品名")),
            "local_product_name": _clean(record.get("中文品名")),
            "company": record.get("_holder", ""),
            "ma_holder_country": "Taiwan",
            "country": "Taiwan",
            "region": "AS",
            "status": _status(record, today),
            "classification": CATEGORIES.get(category, category if not _has_chinese(category) else ""),
            "authorisation_scope": "Active substance (API)" if is_api else "",
            "dosage_form": english_form(record.get("劑型")),
            "registration_number": english_licence_number(record.get("許可證字號")),
            "registration_date": _clean(record.get("發證日期")).replace("/", "-"),
            "expiry_date": _clean(record.get("有效日期")).replace("/", "-"),
            "manufacturer_name": "; ".join(dict.fromkeys(maker["name"] for maker in makers)),
            "manufacturer_country": "; ".join(dict.fromkeys(maker["country"] for maker in makers if maker["country"])),
            "manufacturer_source": "TFDA drug licence register" if makers else "",
            "manufacturers": [
                {"name": maker["name"], "country": maker["country"], "role": maker["process"],
                 "verification_status": "VERIFIED_REGISTER"}
                for maker in makers
            ],
            "source": "TFDA Taiwan",
            "source_url": SEARCH_URL,
            "product_url": "",
            "document_type": "TFDA drug licence",
            "last_checked": fetched_at,
        }


TFDA_TAIWAN = add_register(OpenRegister(
    source="TFDA Taiwan",
    country="Taiwan",
    region="AS",
    url=LICENCES_URL,
    slug="tfda_taiwan",
    max_age_seconds=7 * 24 * 3600,
    build_rows=build_rows,
    fetch=fetch,
    read_records=read_records,
))


def run_tfda_taiwan_search(substance: str) -> list[dict[str, Any]]:
    return search_register(TFDA_TAIWAN, substance)
