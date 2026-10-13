"""EOF Greece: the National Organization for Medicines' human medicines register.

The register is a JSF page. A search runs in one HTTP session: the active
substance autocomplete gives the register's substance ids, the search button
fills the results table, the table pages through 50 rows at a time, and each
row links to a product page that only opens inside the same session.

The product page names the marketing authorisation holder, the local
representative, packs with retail prices, dosage form, ATC code, active
substances and the SmPC and leaflet. It does not name a manufacturer; that is
in the leaflet, which is linked.
"""
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from core.logging_config import get_logger
from sources.salts import base_name
from sources.synonyms import combination_parts


BASE_URL = "https://services.eof.gr/human-search/"
HOME_URL = urljoin(BASE_URL, "home.xhtml")
VIEW_URL = urljoin(BASE_URL, "view.xhtml")
REQUEST_TIMEOUT = 40
PAGE_ROWS = 50
MAX_RESULTS = 300
# The product page shows whichever product the session last opened, so one
# session reads one page at a time. Parallel reading takes parallel sessions,
# each with its own search.
SESSIONS = 4
DETAIL_DEADLINE_SECONDS = 90
HEADERS = {"User-Agent": "Mozilla/5.0"}
AJAX_HEADERS = {**HEADERS, "Faces-Request": "partial/ajax", "X-Requested-With": "XMLHttpRequest"}
SOURCE = "EOF Greece"
logger = get_logger(__name__)


class RegisterUnavailable(RuntimeError):
    pass


# ---------------------------------------------------------------- English

GREEK_LETTERS = {
    "Α": "A", "Β": "V", "Γ": "G", "Δ": "D", "Ε": "E", "Ζ": "Z", "Η": "I", "Θ": "TH",
    "Ι": "I", "Κ": "K", "Λ": "L", "Μ": "M", "Ν": "N", "Ξ": "X", "Ο": "O", "Π": "P",
    "Ρ": "R", "Σ": "S", "Τ": "T", "Υ": "Y", "Φ": "F", "Χ": "CH", "Ψ": "PS", "Ω": "O",
}
GREEK_DIGRAPHS = {"ΟΥ": "OU", "ΑΥ": "AV", "ΕΥ": "EV", "ΓΓ": "NG", "ΓΚ": "GK", "ΜΠ": "MP", "ΝΤ": "NT"}


def transliterate(text: str) -> str:
    """Greek letters in Latin ones (ELOT 743, capitals), everything else unchanged."""
    upper = "".join(
        char for char in unicodedata.normalize("NFD", str(text or "").upper()) if not unicodedata.combining(char)
    )
    for greek, latin in GREEK_DIGRAPHS.items():
        upper = upper.replace(greek, latin)
    return "".join(GREEK_LETTERS.get(char, char) for char in upper)


STATUS = {
    "ΕΓΚΕΚΡΙΜΕΝΟ": "Authorised",
    "ΑΝΑΣΤΟΛΗ": "Suspended",
    "ΑΝΑΚΛΗΣΗ": "Revoked",
    "ΑΡΘ. 2,2.Α Ν4139/2013": "Authorised (Art. 2(2)(a), Law 4139/2013)",
    "ΑΡΘ.29 Ν1316": "Authorised (Art. 29, Law 1316/1983)",
}
PROCEDURE = {
    "ΚΕΝΤΡΙΚΗ": "Centralised",
    "ΑΠΟΚΕΝΤΡΩΜΕΝΗ": "Decentralised",
    "ΑΜΟΙΒΑΙΑΣ ΑΝΑΓΝΩΡΙΣΗΣ": "Mutual recognition",
    "ΕΘΝΙΚΗ": "National",
}
FORMS = {
    "F.C.TAB": "Film-coated tablet", "TAB": "Tablet", "CAPS": "Capsule", "CAPS.HARD": "Capsule, hard",
    "CAPS.SOFT": "Capsule, soft", "SOFT.CAPS": "Capsule, soft", "EF.TAB": "Effervescent tablet",
    "OR.DISP.TAB": "Orodispersible tablet", "PR.TAB": "Prolonged-release tablet",
    "MOD.R.TAB": "Modified-release tablet", "GASTR.TAB": "Gastro-resistant tablet",
    "GASTR.CAPS": "Gastro-resistant capsule", "CHEW.TAB": "Chewable tablet",
    "PD.ORA.SUS": "Powder for oral suspension", "GRAN.ORA.SUS": "Granules for oral suspension",
    "ORAL.SOL": "Oral solution", "ORAL.SUSP": "Oral suspension", "SYRUP": "Syrup",
    "INJ.SOL": "Solution for injection", "SOL.INJ": "Solution for injection",
    "PD.INJ.SOL": "Powder for solution for injection", "CON.S.INF": "Concentrate for solution for infusion",
    "SOL.INF": "Solution for infusion", "CREAM": "Cream", "OINT": "Ointment", "GEL": "Gel",
    "EY.DR.SOL": "Eye drops, solution", "SUPP": "Suppository", "PATCH": "Transdermal patch",
    "INH.PD": "Inhalation powder", "INH.SOL": "Inhalation solution",
}
ROUTES = {
    "ΑΠΟ ΤΟΥ ΣΤΟΜΑΤΟΣ": "Oral", "ΕΝΔΟΦΛΕΒΙΑ": "Intravenous", "ΕΝΔΟΜΥΙΚΗ": "Intramuscular",
    "ΥΠΟΔΟΡΙΑ": "Subcutaneous", "ΔΕΡΜΙΚΗ": "Cutaneous", "ΤΟΠΙΚΗ": "Topical",
    "ΟΦΘΑΛΜΙΚΗ": "Ocular", "ΡΙΝΙΚΗ": "Nasal", "ΠΡΩΚΤΙΚΗ": "Rectal", "ΚΟΛΠΙΚΗ": "Vaginal",
    "ΔΙΑ ΕΙΣΠΝΟΗΣ": "Inhalation", "ΥΠΟΓΛΩΣΣΙΑ": "Sublingual", "ΔΙΑΔΕΡΜΙΚΗ": "Transdermal",
}
PACK_WORDS = {
    "ΦΙΑΛΗ": "bottle", "ΦΙΑΛΙΔΙΟ": "vial", "ΦΑΚΕΛΙΣΚΟΙ": "sachets", "ΦΑΚΕΛΙΣΚΟΣ": "sachet",
    "ΚΟΥΤΙ": "box", "ΔΙΣΚΙΑ": "tablets", "ΔΙΣΚΙΟ": "tablet", "ΚΑΨΟΥΛΕΣ": "capsules",
    "ΚΥΨΕΛΗ": "blister", "ΚΥΨΕΛΕΣ": "blisters", "ΜΕ ΥΛΙΚΟ ΑΦΥΓΡΑΝΣΗΣ": "with desiccant",
    "ΜΕ ΕΞΩΤΕΡΙΚΟ ΚΟΥΤΙ": "with outer box", "ΧΩΡΙΣ ΕΞΩΤΕΡΙΚΟ ΚΟΥΤΙ": "without outer box",
    "ΜΕ ΔΟΣΙΜΕΤΡΙΚΟ ΚΟΥΤΑΛΙ": "with measuring spoon", "ΠΡΟΓΕΜΙΣΜΕΝΗ ΣΥΡΙΓΓΑ": "pre-filled syringe",
}
HOLDER_COUNTRIES = {"ΕΛΛΑΔΑ": "Greece", "ΚΥΠΡΟΣ": "Cyprus"}
LEGAL_FORMS = {"ΑΒΕΕ": "S.A.", "Α.Β.Ε.Ε.": "S.A.", "Α.Β.Ε.Ε": "S.A.", "Α.Ε.": "S.A.", "ΑΕ": "S.A.", "Ε.Π.Ε.": "Ltd"}


def _plain(text: str) -> str:
    return " ".join(
        "".join(char for char in unicodedata.normalize("NFD", str(text or "").upper()) if not unicodedata.combining(char)).split()
    )


def _english(text: str, table: dict[str, str]) -> str:
    key = _plain(text)
    return table.get(key) or transliterate(key)


def _phrases(text: str, table: dict[str, str]) -> str:
    out = _plain(text)
    for greek, english in sorted(table.items(), key=lambda item: -len(item[0])):
        out = out.replace(greek, english)
    return transliterate(out)


def english_company(name: str) -> str:
    """ "ΦΑΡΑΝ ΑΝΩΝΥΜΗ ... Δ.Τ. ΦΑΡΑΝ Α.Β.Ε.Ε." -> "FARAN S.A."; Latin names are kept."""
    text = " ".join(str(name or "").split())
    if "Δ.Τ." in text:
        # The distinctive title is the name the company trades under.
        text = text.split("Δ.Τ.", 1)[1].strip()
    words = []
    for word in text.split():
        words.append(LEGAL_FORMS.get(_plain(word), word))
    return transliterate(" ".join(words)).strip(" ,")


# ---------------------------------------------------------------- register

def _view_state(html: str) -> str:
    match = re.search(r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"', html) or re.search(
        r'ViewState[^>]*><!\[CDATA\[([^\]]+)\]\]>', html
    )
    return match.group(1) if match else ""


class Register:
    def __init__(self) -> None:
        self.session = requests.Session()
        response = self.session.get(HOME_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        self.view_state = _view_state(response.text)
        if not self.view_state:
            raise RegisterUnavailable("EOF search page has no view state")
        code_column = re.search(r'<th id="([^"]+)"[^>]*>(?:(?!</th>).)*Κωδικός', response.text, re.S)
        self.code_column = code_column.group(1) if code_column else ""

    def _ajax(self, data: list[tuple[str, str]]) -> str:
        response = self.session.post(HOME_URL, data=data, headers=AJAX_HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        self.view_state = _view_state(response.text) or self.view_state
        return response.text

    def substance_ids(self, word: str) -> list[tuple[str, str]]:
        text = self._ajax([
            ("javax.faces.partial.ajax", "true"),
            ("javax.faces.source", "frmSearch:txtdrinac"),
            ("javax.faces.partial.execute", "frmSearch:txtdrinac"),
            ("javax.faces.partial.render", "frmSearch:txtdrinac"),
            ("frmSearch:txtdrinac", "frmSearch:txtdrinac"),
            ("frmSearch:txtdrinac_query", word),
            ("frmSearch", "frmSearch"),
            ("frmSearch:txtdrinac_input", word),
            ("javax.faces.ViewState", self.view_state),
        ])
        return re.findall(r'data-item-value="([^"]+)" data-item-label="([^"]+)"', text)

    def search(self, ids: list[str]) -> int:
        data = [
            ("javax.faces.partial.ajax", "true"),
            ("javax.faces.source", "frmSearch:btnSearch"),
            ("javax.faces.partial.execute", "frmSearch"),
            ("javax.faces.partial.render", "frmMain"),
            ("frmSearch:btnSearch", "frmSearch:btnSearch"),
            ("frmSearch", "frmSearch"),
            ("frmSearch:txtDrugid", ""), ("frmSearch:txtDrname", ""), ("frmSearch:txtlicnumber", ""),
            ("frmSearch:txtapplicproc_input", ""), ("frmSearch:txtmrpcp", ""),
            ("frmSearch:txtDrstatus_input", ""), ("frmSearch:txtdratc_input", ""),
            ("frmSearch:txtdrinac_input", ""),
            ("javax.faces.ViewState", self.view_state),
        ] + [("frmSearch:txtdrinac_hinput", value) for value in ids]
        text = self._ajax(data)
        match = re.search(r"\d+ - \d+ / (\d+)", text)
        total = int(match.group(1)) if match else 0
        if total > PAGE_ROWS:
            self._sort_by_code()
        return total

    def _sort_by_code(self) -> None:
        """Order the results by product code.

        The default order is by name, and products sharing a name come back in
        a different order on each request: paging then repeats some and skips
        others. The code is unique, so paging by it is stable.
        """
        if not self.code_column:
            raise RegisterUnavailable("EOF results table has no code column to sort by")
        self._ajax([
            ("javax.faces.partial.ajax", "true"),
            ("javax.faces.source", "frmMain:tblResults"),
            ("javax.faces.partial.execute", "frmMain:tblResults"),
            ("javax.faces.partial.render", "frmMain:tblResults"),
            ("frmMain:tblResults", "frmMain:tblResults"),
            ("frmMain:tblResults_sorting", "true"),
            ("frmMain:tblResults_skipChildren", "true"),
            ("frmMain:tblResults_encodeFeature", "true"),
            ("frmMain:tblResults_sortKey", self.code_column),
            ("frmMain:tblResults_sortDir", "1"),
            ("frmMain:tblResults_first", "0"),
            ("frmMain:tblResults_rows", str(PAGE_ROWS)),
            ("frmMain", "frmMain"),
            ("javax.faces.ViewState", self.view_state),
        ])

    def page(self, first: int) -> list[dict[str, str]]:
        text = self._ajax([
            ("javax.faces.partial.ajax", "true"),
            ("javax.faces.source", "frmMain:tblResults"),
            ("javax.faces.partial.execute", "frmMain:tblResults"),
            ("javax.faces.partial.render", "frmMain:tblResults"),
            ("frmMain:tblResults", "frmMain:tblResults"),
            ("frmMain:tblResults_pagination", "true"),
            ("frmMain:tblResults_first", str(first)),
            ("frmMain:tblResults_rows", str(PAGE_ROWS)),
            ("frmMain:tblResults_skipChildren", "true"),
            ("frmMain:tblResults_encodeFeature", "true"),
            ("frmMain", "frmMain"),
            ("javax.faces.ViewState", self.view_state),
        ])
        return parse_result_rows(text)

    def product_page(self, view_id: str) -> str:
        response = self.session.get(VIEW_URL, params={"id": view_id}, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.text


def parse_result_rows(text: str) -> list[dict[str, str]]:
    rows = []
    for html in re.findall(r"<tr data-ri=.*?</tr>", text, re.S):
        cells = BeautifulSoup(html, "html.parser").find_all("td")
        link = re.search(r"view\.xhtml\?id=([0-9a-f]+)", html)
        if len(cells) < 6 or not link:
            continue

        def value(cell: Any) -> str:
            title = cell.find(class_="ui-column-title")
            if title:
                title.extract()
            return " ".join(cell.get_text(" ").split())

        rows.append({
            "code": value(cells[0]),
            "name": value(cells[1]),
            "status": value(cells[2]),
            "licence": value(cells[3]),
            "procedure": value(cells[4]),
            "procedure_number": value(cells[5]),
            "view_id": link.group(1),
        })
    return rows


def _cards(soup: BeautifulSoup) -> dict[str, list[list[Any]]]:
    """Each card title with its list items, each item as its <div> children."""
    cards: dict[str, list[list[Any]]] = {}
    for title in soup.select("div.text-2xl"):
        card = title.find_parent("div", class_="surface-section") or title.parent
        items = []
        for item in card.select("ul > li"):
            items.append(item.find_all("div", recursive=False))
        cards[" ".join(title.get_text().split())] = items
    return cards


def _text(node: Any) -> str:
    return " ".join(node.get_text(" ").split()) if node is not None else ""


def parse_product_page(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    cards = _cards(soup)
    detail: dict[str, Any] = {"parties": {}, "packs": [], "forms": [], "atc": [], "routes": [], "substances": [], "documents": {}}
    for divs in cards.get("Γενικά", []):
        if len(divs) < 2:
            continue
        label = _text(divs[0])
        if label in ("Κάτοχος Αδείας Κυκλοφορίας", "Τοπικός Αντιπρόσωπος"):
            address = divs[2].get_text("\n") if len(divs) > 2 else ""
            lines = [" ".join(line.split()) for line in address.split("\n") if line.strip()]
            detail["parties"][label] = {"name": _text(divs[1]), "address": lines[0] if lines else ""}
        else:
            detail[label] = _text(divs[1])
    for divs in cards.get("Συσκευασίες", []):
        if len(divs) < 2:
            continue
        price = next((_text(span) for span in divs[-1].select("span[id$=txtGRP]")), "")
        count = re.match(r"\s*([\d.,]+)", divs[2].get_text()) if len(divs) > 2 else None
        detail["packs"].append({
            "barcode": _text(divs[0]),
            "description": _text(divs[1]),
            "units": count.group(1) if count else "",
            "status": _text(divs[3]) if len(divs) > 3 else "",
            "retail_price": price,
        })
    for divs in cards.get("Φαρμακοτεχνική μορφή - Περιεκτικότητα", []):
        if len(divs) >= 3:
            detail["forms"].append({"code": _text(divs[0]), "greek": _text(divs[1]), "strength": _text(divs[2])})
    for divs in cards.get("Ταξινόμηση ATC", []):
        if len(divs) >= 2 and _text(divs[0]):
            detail["atc"].append((_text(divs[0]), _text(divs[1])))
    for divs in cards.get("Οδός χορήγησης", []):
        if len(divs) >= 2 and _text(divs[1]):
            detail["routes"].append(_text(divs[1]))
    for divs in cards.get("Δραστική ουσία", []):
        if len(divs) >= 2 and _text(divs[1]):
            detail["substances"].append((_text(divs[1]), _text(divs[2]) if len(divs) > 2 else ""))
    for divs in cards.get("Τεκμηρίωση", []):
        if len(divs) >= 2:
            link = divs[1].find("a", href=True)
            if link:
                detail["documents"][_text(divs[0])] = urljoin(VIEW_URL, link["href"])
    return detail


def build_row(listing: dict[str, str], detail: dict[str, Any], searched: str) -> dict[str, Any]:
    holder = detail.get("parties", {}).get("Κάτοχος Αδείας Κυκλοφορίας", {})
    agent = detail.get("parties", {}).get("Τοπικός Αντιπρόσωπος", {})
    holder_name = holder.get("name", "")
    holder_country = ""
    # Holders are written "SANOFI WINTHROP INDUSTRIE, FRANCE" or
    # "WIN MEDICA Α.Ε., ΕΛΛΑΔΑ": the country follows the last comma.
    match = re.match(r"^(.*\S),\s*([A-ZΑ-Ω][A-ZΑ-Ω .]+)$", _plain(holder_name))
    if match:
        holder_name = match.group(1)
        holder_country = HOLDER_COUNTRIES.get(match.group(2), transliterate(match.group(2)).title())
    elif holder_name and re.search(r"[Α-Ω]", _plain(holder.get("address", "") + holder_name)):
        holder_country = "Greece"
    substances = detail.get("substances") or []
    molecules = "; ".join(transliterate(name) for name, _strength in substances)
    forms = detail.get("forms") or []
    strength = "; ".join(form["strength"] for form in forms if form["strength"])
    dosage_form = "; ".join(
        FORMS.get(form["code"].upper()) or _phrases(form["greek"], {}) or form["code"] for form in forms
    )
    packs = detail.get("packs") or []
    pack_size = "; ".join(
        f"{_phrases(pack['description'], PACK_WORDS)} ({pack['units']} units)" if pack["units"] else _phrases(pack["description"], PACK_WORDS)
        for pack in packs
    )
    price = next((pack["retail_price"] for pack in packs if pack["retail_price"]), "")
    documents = detail.get("documents") or {}
    # The product page's id only opens inside the session that searched, so it
    # is no use as a link or an identity. The register's product code is both
    # stable and unique; the link leads to the search it can be entered in.
    product_url = f"{HOME_URL}#code={listing['code']}"
    return {
        "substance": molecules or searched,
        "active_substance": molecules,
        "source_substance": molecules,
        "product": transliterate(listing["name"]) if re.search(r"[Α-Ωα-ω]", listing["name"]) else listing["name"],
        "company": english_company(holder_name),
        "ma_holder_country": holder_country,
        "local_agent": english_company(agent.get("name", "")),
        "country": "Greece",
        "region": "EU",
        "status": _english(detail.get("Κατάσταση Α.Κ.") or listing["status"], STATUS),
        "authorisation_procedure": _english(detail.get("Διαδικασία") or listing["procedure"], PROCEDURE),
        "procedure_number": detail.get("Αρ. διαδικασίας") or listing["procedure_number"],
        "strength": strength,
        "dosage_form": dosage_form,
        "route": "; ".join(_english(route, ROUTES) for route in detail.get("routes") or []),
        "pack_size": pack_size,
        "price": f"EUR {price} (retail incl. VAT)" if price else "",
        "atc_code": "; ".join(code for code, _name in detail.get("atc") or []),
        "registration_number": detail.get("Αρ. άδειας") or listing["licence"],
        "product_code": listing["code"],
        "smpc_url": documents.get("Περίληψη Χαρακτηριστικών Προϊόντος", ""),
        "pil_url": documents.get("Φύλλο Οδηγιών για το Χρήστη", ""),
        "assessment_report_url": documents.get("Δημόσια Έκθεση Αξιολόγησης", ""),
        "source": SOURCE,
        "source_url": HOME_URL,
        "product_url": product_url,
        "url": product_url,
        "document_type": "EOF product record",
    }


def _molecules(substance: str) -> list[str]:
    parts = combination_parts(substance) or [substance]
    return [base_name(part).upper() for part in parts if base_name(part)]


def _open(molecules: list[str]) -> tuple[Register, int]:
    """A session with the search run; returns it and the number of products."""
    register = Register()
    # The register files one molecule under several names (ATORVASTATIN,
    # ATORVASTATIN CALCIUM, ...). All of them are searched together; a
    # combination is found through its first molecule, and the search engine
    # keeps the products whose substances name every one.
    first = molecules[0]
    ids = [value for value, label in register.substance_ids(first) if label.upper().startswith(first)]
    return register, register.search(ids) if ids else 0


def _read_pages(
    register: Register, starts: list[int], deadline: float
) -> list[tuple[dict[str, str], dict[str, Any]]]:
    """A results page, then its products' pages while that page is the one shown.

    A product page opens only for a row of the results page the session shows
    last; after paging on, the earlier rows' links lead back to the search.
    """
    read = []
    for start in starts:
        for listing in register.page(start):
            detail: dict[str, Any] = {}
            if time.monotonic() < deadline:
                try:
                    page = parse_product_page(register.product_page(listing["view_id"]))
                    # Only a page that shows this product's own code is used.
                    if page.get("Κωδικός") == listing["code"]:
                        detail = page
                    else:
                        logger.info("EOF product page for %s showed %s", listing["code"], page.get("Κωδικός"))
                except requests.RequestException as exc:
                    logger.info("EOF product page %s unavailable: %s", listing["code"], exc)
            read.append((listing, detail))
    return read


def _reader(molecules: list[str], starts: list[int], deadline: float) -> list[tuple[dict[str, str], dict[str, Any]]]:
    register, _total = _open(molecules)
    return _read_pages(register, starts, deadline)


def run_eof_greece_search(substance: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    term = " ".join(str(substance or "").split())
    if not term:
        return []
    molecules = _molecules(term)
    try:
        register, total = _open(molecules)
    except (requests.RequestException, RegisterUnavailable) as exc:
        logger.warning("EOF Greece search unavailable: %s", exc)
        return []
    starts = list(range(0, min(total, limit), PAGE_ROWS))
    if not starts:
        return []

    # One session per results page: once a session has opened product pages
    # the register stops paging it. The session that ran the search reads the
    # first page; up to SESSIONS run at once.
    deadline = time.monotonic() + DETAIL_DEADLINE_SECONDS
    executor = ThreadPoolExecutor(max_workers=min(SESSIONS, len(starts)))
    futures = [executor.submit(_read_pages, register, starts[:1], deadline)] + [
        executor.submit(_reader, molecules, [start], deadline) for start in starts[1:]
    ]
    read: list[tuple[dict[str, str], dict[str, Any]]] = []
    try:
        for future in as_completed(futures, timeout=DETAIL_DEADLINE_SECONDS + 2 * REQUEST_TIMEOUT):
            try:
                read.extend(future.result())
            except (requests.RequestException, RegisterUnavailable) as exc:
                logger.info("EOF Greece: a reading session failed: %s", exc)
    except FuturesTimeout:
        logger.info("EOF Greece: results not all read before the deadline")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    seen = set()
    rows = []
    for listing, detail in read:
        if listing["code"] in seen:
            continue
        seen.add(listing["code"])
        rows.append(build_row(listing, detail, term))
    return rows[:limit]
