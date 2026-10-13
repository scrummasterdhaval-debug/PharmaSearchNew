"""China, from NMPA's quarterly National Drug Code list (国家药品编码本位码).

NMPA's product database sits behind an anti-bot challenge that refuses
scripts and automated browsers, and getting past it would be bot evasion.
The same approvals are published openly each quarter on nmpa.gov.cn as a zip
of two workbooks, domestic and imported drugs: approval number (批准文号),
generic name, dosage form, strength, marketing authorisation holder,
manufacturer (生产单位, domestic only) and the national drug code.

NMPA names drugs in Chinese only, so an English search is first turned into
the Chinese generic name. That name is used to search and is never stored:
every stored value is NMPA's own. The list names no brands, so the product
is the generic name with its form and strength.
"""
from __future__ import annotations

import io
import json
import re
import threading
import zipfile
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from core.logging_config import get_logger
from sources.open_registers import (
    DOWNLOAD_TIMEOUT,
    INDEX_DIR,
    OpenRegister,
    RegisterNotReady,
    _connect,
    _is_stale,
    _status_rank,
    add_register,
    current_index,
    download_file,
    index_info,
    refresh_in_background,
)

logger = get_logger(__name__)

NMPA_LIST_PAGE = "https://www.nmpa.gov.cn/zwfw/zwfwzxfw/zxfwsjxz/zxfwsjxzypbwm/index.html"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
MAX_RESULTS = 500
_CJK = re.compile(r"[一-鿿]")

# Chinese generic names for molecules searched often, checked against NMPA's
# own list. Anything else is looked up on Wikidata and cached.
CHINESE_NAMES = {
    "metformin": "二甲双胍",
    "paracetamol": "对乙酰氨基酚",
    "acetaminophen": "对乙酰氨基酚",
    "atorvastatin": "阿托伐他汀",
    "dapagliflozin": "达格列净",
    "semaglutide": "司美格鲁肽",
    "tirzepatide": "替尔泊肽",
    "enalapril": "依那普利",
    "ibuprofen": "布洛芬",
    "lidocaine": "利多卡因",
    "quetiapine": "喹硫平",
    "solifenacin": "索利那新",
    "mirabegron": "米拉贝隆",
    "glimepiride": "格列美脲",
    "amlodipine": "氨氯地平",
    "omeprazole": "奥美拉唑",
    "amoxicillin": "阿莫西林",
    "sevelamer": "司维拉姆",
}
_CACHE_PATH = INDEX_DIR / "nmpa_china_names.json"
_cache_lock = threading.Lock()


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


# ----------------------------------------------------------------- fetching

def fetch_nmpa(register: OpenRegister, workdir: Path) -> Path:
    """The list page links one release per quarter, newest first; take the newest zip."""
    listing = requests.get(NMPA_LIST_PAGE, headers=HEADERS, timeout=DOWNLOAD_TIMEOUT)
    listing.raise_for_status()
    soup = BeautifulSoup(listing.content.decode("utf-8", "replace"), "html.parser")
    releases = [
        urljoin(NMPA_LIST_PAGE, a["href"])
        for a in soup.select("a[href]")
        if "国家药品编码本位码（截至" in a.get_text()
    ]
    if not releases:
        raise RuntimeError("NMPA drug code page lists no release")
    page = requests.get(releases[0], headers=HEADERS, timeout=DOWNLOAD_TIMEOUT)
    page.raise_for_status()
    zips = [
        urljoin(releases[0], a["href"])
        for a in BeautifulSoup(page.content.decode("utf-8", "replace"), "html.parser").select("a[href]")
        if a["href"].lower().endswith(".zip")
    ]
    if not zips:
        raise RuntimeError(f"NMPA drug code release {releases[0]} has no zip attachment")
    return download_file(register, zips[0], workdir / "nmpa_drug_codes.zip")


def read_nmpa(register: OpenRegister, path: Path) -> Iterator[dict[str, str]]:
    """Both workbooks: row 1 is a title, row 2 the headers."""
    import openpyxl

    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            if not member.filename.lower().endswith(".xlsx"):
                continue
            workbook = openpyxl.load_workbook(io.BytesIO(archive.read(member)), read_only=True)
            try:
                rows = workbook.worksheets[0].iter_rows(values_only=True)
                next(rows, None)
                headers = [_clean(h) for h in next(rows, ())]
                imported = "上市许可持有人英文" in headers
                for values in rows:
                    record = {h: _clean(v) for h, v in zip(headers, values) if h}
                    if record.get("批准文号"):
                        record["_imported"] = "1" if imported else ""
                        yield record
            finally:
                workbook.close()


# NMPA's dosage forms, as the Chinese Pharmacopoeia names them, with the
# qualifier it sometimes adds in brackets. Matched on the longest prefix. A
# form or qualifier not listed keeps its Chinese wording rather than a guess.
NMPA_DOSAGE_FORMS = {
    "片剂": "Tablet",
    "注射剂": "Injection",
    "软胶囊剂": "Soft capsule",
    "胶囊剂": "Capsule",
    "颗粒剂": "Granules",
    "丸剂": "Pill",
    "原料药": "Active substance",
    "合剂": "Mixture",
    "糖浆剂": "Syrup",
    "散剂": "Powder",
    "口服溶液剂": "Oral solution",
    "口服混悬剂": "Oral suspension",
    "溶液剂": "Solution",
    "乳膏剂": "Cream",
    "软膏剂": "Ointment",
    "凝胶剂": "Gel",
    "眼用制剂": "Ophthalmic preparation",
    "滴眼剂": "Eye drops",
    "栓剂": "Suppository",
    "酊剂": "Tincture",
    "搽剂": "Liniment",
    "酒剂": "Medicinal wine",
    "吸入制剂": "Inhalation preparation",
    "贴膏剂": "Medicated plaster",
}
NMPA_FORM_QUALIFIERS = {
    "糖衣": "sugar-coated",
    "糖衣片": "sugar-coated",
    "薄膜衣": "film-coated",
    "薄膜衣片": "film-coated",
    "素片": "uncoated",
    "肠溶": "gastro-resistant",
    "大蜜丸": "large honeyed pill",
    "小蜜丸": "small honeyed pill",
    "水蜜丸": "water-honeyed pill",
    "水丸": "water pill",
    "浓缩丸": "concentrated pill",
    "冻干": "lyophilised",
    "冻干粉针剂": "lyophilised powder",
    "注射液": "solution",
    "小容量注射剂": "small volume",
}


def english_dosage_form(form: str) -> str:
    match = re.fullmatch(r"(.+?)[（(](.+?)[）)]", form)
    base, qualifier = (match.group(1), match.group(2)) if match else (form, "")
    english = NMPA_DOSAGE_FORMS.get(base)
    if english is None:
        return form
    if not qualifier:
        return english
    return f"{english} ({NMPA_FORM_QUALIFIERS.get(qualifier, qualifier)})"


def build_nmpa_rows(records: Iterable[dict[str, str]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for record in records:
        name = record.get("药品通用名称", "")
        if not name:
            continue
        imported = bool(record.get("_imported"))
        form = record.get("剂型", "")
        strength = record.get("规格", "")
        holder = record.get("上市许可持有人英文") or record.get("上市许可持有人中文") or record.get("上市许可持有人", "")
        maker = "" if imported else record.get("生产单位", "")
        yield f" {name} ", {
            "substance": name,
            "active_substance": name,
            "source_substance": name,
            # The form has its own column; repeating it here made every
            # company's row read the same twice over.
            "product": " ".join(part for part in (name, strength) if part),
            "company": holder,
            "country": "China",
            "region": "AS",
            "status": "Approved",
            "classification": "Imported" if imported else "Domestic",
            "dosage_form": english_dosage_form(form),
            "strength": strength,
            "registration_number": record.get("批准文号", ""),
            "drug_code": record.get("药品编码", ""),
            "manufacturer_name": maker,
            "manufacturer_country": "China" if maker else "",
            "manufacturer_source": "NMPA National Drug Code list (生产单位)" if maker else "",
            "source": "NMPA China",
            "source_url": NMPA_LIST_PAGE,
            "product_url": "",
            "document_type": "NMPA National Drug Code list",
            "last_checked": fetched_at,
        }


NMPA_CHINA = add_register(OpenRegister(
    source="NMPA China",
    country="China",
    region="AS",
    url=NMPA_LIST_PAGE,
    slug="nmpa_china",
    max_age_seconds=14 * 24 * 3600,  # released quarterly; checked fortnightly
    build_rows=build_nmpa_rows,
    fetch=fetch_nmpa,
    read_records=read_nmpa,
))


# ---------------------------------------------------------------- searching

def _load_cache() -> dict[str, str]:
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _wikidata_chinese_name(english: str) -> str:
    """Chinese label of the Wikidata item for this drug, or "" if none is found.

    Only items carrying a drug identifier (INN, ATC, CAS or ChemSpider) are
    accepted, so a same-named person or place cannot supply the name.
    """
    api = "https://www.wikidata.org/w/api.php"
    headers = {"User-Agent": "PharmaSearch/1.0 (regulatory search)"}
    found = requests.get(
        api,
        params={"action": "wbsearchentities", "search": english, "language": "en", "type": "item", "limit": 3, "format": "json"},
        headers=headers,
        timeout=15,
    ).json()
    for hit in found.get("search", []):
        entity = requests.get(
            api,
            params={"action": "wbgetentities", "ids": hit["id"], "props": "labels|claims", "languages": "zh-hans|zh-cn|zh", "format": "json"},
            headers=headers,
            timeout=15,
        ).json()["entities"][hit["id"]]
        if not any(prop in entity.get("claims", {}) for prop in ("P2275", "P267", "P231", "P661")):
            continue
        labels = entity.get("labels", {})
        for language in ("zh-hans", "zh-cn", "zh"):
            if language in labels:
                return labels[language]["value"]
    return ""


def chinese_names(substance: str) -> list[str]:
    """The Chinese generic names to search NMPA's list for."""
    text = _clean(substance)
    if _CJK.search(text):
        return [text]
    from sources.synonyms import names_for

    names: list[str] = []
    for english in [text.lower(), *[n.lower() for n in names_for(text) or []]]:
        chinese = CHINESE_NAMES.get(english)
        if chinese is None:
            with _cache_lock:
                cache = _load_cache()
            chinese = cache.get(english)
            if chinese is None:
                try:
                    chinese = _wikidata_chinese_name(english)
                except (requests.RequestException, ValueError, KeyError) as exc:
                    logger.warning("NMPA China: no Chinese name for %s: %s", english, exc)
                    continue
                with _cache_lock:
                    cache = _load_cache()
                    cache[english] = chinese
                    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                    _CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        if chinese and chinese not in names:
            names.append(chinese)
    return names


def run_nmpa_china_search(substance: str) -> list[dict[str, Any]]:
    names = chinese_names(substance)
    if not names:
        return []
    path = current_index(NMPA_CHINA)
    if path is None:
        started = refresh_in_background(NMPA_CHINA)
        raise RegisterNotReady(
            "NMPA China register is being downloaded for the first time"
            f"{'' if started else ' (already in progress)'}; search again in a few minutes."
        )
    if _is_stale(NMPA_CHINA, index_info(NMPA_CHINA)):
        refresh_in_background(NMPA_CHINA)
    where = " OR ".join("match LIKE ?" for _ in names)
    with _connect(path) as conn:
        payloads = [p for (p,) in conn.execute(f"SELECT payload FROM rows WHERE {where}", [f"%{n}%" for n in names])]
    rows = [json.loads(p) for p in payloads]
    for row in rows:
        row["searched_substance"] = substance
    rows.sort(key=lambda row: (_status_rank(row), row.get("product", "")))
    if len(rows) > MAX_RESULTS:
        for row in rows[:MAX_RESULTS]:
            row["available_total"] = len(rows)
    return rows[:MAX_RESULTS]
