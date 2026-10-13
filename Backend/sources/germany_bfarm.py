"""Germany, read live from BfArM's AMIce public database.

AMIce - Öffentlicher Teil is the German medicines register: every authorisation
BfArM, PEI and BVL have granted since 1990, about 337,000 documents, updated
every working day, and free to search since 13 February 2025. A document names
the marketing authorisation holder and, separately, the plants that do batch
release ("Hersteller Endfreigabe") with their addresses and countries -- which
is the pair this project exists to report.

It is not downloaded and indexed like Italy's or Ireland's register, because
BfArM's own copyright terms do not allow that:

    "Kopieren der gesamten Datenbank ist aus Gründen der Aktualität nicht
     sinnvoll und deshalb nicht erlaubt."
    "Die Daten dürfen nicht weiter kopiert, verbreitet oder verkauft werden."

So this connector searches the register when somebody searches here, shows what
came back, and marks every row ``view_only``: the store does not keep it and
the exports leave it out. Each row carries BfArM's copyright line, which its
terms require to be shown with any printout. Until BfArM grants permission to
reuse the data, that is the whole of what Germany does here.

Reaching the search costs an anonymous session: the entry page hands over to
BfArM's sign-in service, which hands back a session cookie, and the register
then shows its disclaimer -- no warranty for the data, and the limits BfArM
lists -- which must be acknowledged before the search form appears. The
session is kept and reused rather than rebuilt for every molecule, because
BfArM allows only 200 at a time.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup

from core.logging_config import get_logger
from sources.salts import base_name


logger = get_logger(__name__)

BASE_URL = "https://portal.bfarm.de/amguifree"
ENTRY_URL = f"{BASE_URL}/"
TERMS_URL = f"{BASE_URL}/termsofuse.xhtml?accept=true"
SEARCH_URL = f"{BASE_URL}/am/search.xhtml"
RESULT_URL = f"{BASE_URL}/am/searchresult.xhtml"
PUBLIC_SEARCH_URL = "https://www.bfarm.de/DE/Arzneimittel/Arzneimittelinformationen/Arzneimittel-recherchieren/AMIce/_node.html"

COPYRIGHT = "© BfArM, Bonn"
# BfArM's terms allow a search result to be shown and printed, not stored or
# passed on. Every row says so, and repository/export_service obey it.
VIEW_ONLY_NOTE = (
    "Shown from BfArM's AMIce public database under its terms of use: "
    "look at and print for your own use. Not stored, and left out of exports, "
    "until BfArM grants permission to reuse the data."
)

REQUEST_TIMEOUT = 25
SESSION_MAX_AGE = 15 * 60  # BfArM allows 200 sessions at a time; reuse ours
MAX_PAGES = 2  # ten products a page
MAX_DETAILS = 20
# BfArM says itself that complex searches are slow, and a document takes five
# to eight seconds. Four at a time turns three minutes into one; more would be
# leaning on a public service that has asked for patience.
DETAIL_WORKERS = 4
DETAIL_BUDGET = 75
HEADERS = {"User-Agent": "PharmaSearch/1.0 (regulatory register search)"}

# The form field that searches the active ingredient rather than the brand.
SUBSTANCE_FIELD = "PPT.INT.SUBST.STF"
FORM_PREFIX = "searchForm:searchInputsComponent"
FILTER_PREFIX = f"{FORM_PREFIX}:searchFilterComponent"

GERMAN_COUNTRIES = {
    "Deutschland": "Germany", "Frankreich": "France", "Irland": "Ireland",
    "Italien": "Italy", "Spanien": "Spain", "Niederlande": "Netherlands",
    "Belgien": "Belgium", "Österreich": "Austria", "Schweiz": "Switzerland",
    "Polen": "Poland", "Portugal": "Portugal", "Ungarn": "Hungary",
    "Tschechien": "Czech Republic", "Tschechische Republik": "Czech Republic",
    "Slowenien": "Slovenia", "Slowakei": "Slovakia", "Kroatien": "Croatia",
    "Rumänien": "Romania", "Bulgarien": "Bulgaria", "Griechenland": "Greece",
    "Schweden": "Sweden", "Dänemark": "Denmark", "Finnland": "Finland",
    "Norwegen": "Norway", "Island": "Iceland", "Estland": "Estonia",
    "Lettland": "Latvia", "Litauen": "Lithuania", "Luxemburg": "Luxembourg",
    "Malta": "Malta", "Zypern": "Cyprus",
    "Vereinigtes Königreich": "United Kingdom", "Großbritannien": "United Kingdom",
    "Vereinigte Staaten": "United States", "Vereinigte Staaten von Amerika": "United States",
    "Indien": "India", "China": "China", "Japan": "Japan", "Israel": "Israel",
    "Kanada": "Canada", "Türkei": "Türkiye", "Brasilien": "Brazil",
    "Südkorea": "South Korea", "Korea, Republik": "South Korea",
    "Singapur": "Singapore", "Australien": "Australia", "Neuseeland": "New Zealand",
}

# EDQM standard terms as BfArM writes them. Matched on the longest prefix, so
# a form not listed keeps BfArM's own German wording rather than a guess.
GERMAN_DOSAGE_FORMS = {
    "Filmtablette": "Film-coated tablet",
    "Hartkapsel": "Hard capsule",
    "Weichkapsel": "Soft capsule",
    "Kapsel": "Capsule",
    "Retardtablette": "Prolonged-release tablet",
    "Magensaftresistente Tablette": "Gastro-resistant tablet",
    "Magensaftresistente Hartkapsel": "Gastro-resistant hard capsule",
    "Brausetablette": "Effervescent tablet",
    "Schmelztablette": "Orodispersible tablet",
    "Lutschtablette": "Lozenge",
    "Kautablette": "Chewable tablet",
    "Tablette": "Tablet",
    "Injektionslösung": "Solution for injection",
    "Infusionslösung": "Solution for infusion",
    "Konzentrat zur Herstellung einer Infusionslösung": "Concentrate for solution for infusion",
    "Pulver zur Herstellung einer Injektionslösung": "Powder for solution for injection",
    "Pulver zur Herstellung einer Infusionslösung": "Powder for solution for infusion",
    "Lösung zum Einnehmen": "Oral solution",
    "Suspension zum Einnehmen": "Oral suspension",
    "Pulver zum Einnehmen": "Powder for oral use",
    "Granulat": "Granules",
    "Sirup": "Syrup",
    "Tropfen zum Einnehmen": "Oral drops",
    "Augentropfen": "Eye drops",
    "Nasenspray": "Nasal spray",
    "Creme": "Cream",
    "Salbe": "Ointment",
    "Gel": "Gel",
    "Zäpfchen": "Suppository",
    "Transdermales Pflaster": "Transdermal patch",
    "Dosieraerosol": "Pressurised inhalation",
    "Pulver zur Inhalation": "Inhalation powder",
}

GERMAN_STATUS = {
    "zugelassen": "Authorised",
    "verlängert": "Authorised (renewed)",
    "registriert": "Registered",
    "erloschen": "Expired",
    "ruhend": "Suspended",
    "zurückgenommen": "Withdrawn",
    "widerrufen": "Revoked",
}

GERMAN_PROCEDURE = {
    "CP": "Centralised",
    "DCP": "Decentralised",
    "MRP": "Mutual recognition",
    "NAP": "National",
}

GERMAN_LEGAL_STATUS = {
    "verschreibungspflichtig": "Prescription only",
    "apothekenpflichtig": "Pharmacy only",
    "freiverkäuflich": "General sale",
    "betäubungsmittelrezeptpflichtig": "Controlled drug prescription",
    "sonderrezeptpflichtig": "Special prescription",
}

# The heading a company table sits under says what that company does.
COMPANY_ROLES = {
    "Zulassungsinhaber": "holder",
    "Hersteller Endfreigabe": "Batch release",
    "Wirkstoffherstellung": "Active substance manufacture",
    "Vertreiber": "Distributor",
    "örtlicher Vertreter": "Local representative",
}


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def company_name(value: object) -> str:
    """"Sanofi Winthrop Industrie Rechtsform: S.A." -> "Sanofi Winthrop Industrie S.A."

    AMIce keeps the legal form in its own field and the register's own text
    runs the two together; the label is not part of the company's name.
    """
    return _clean(re.sub(r"\bRechtsform:\s*", "", _clean(value)))


PACK_WORDS = {
    "Originalpackung": "Original pack",
    "Klinikpackung": "Hospital pack",
    "Anstaltspackung": "Institutional pack",
    "Bündelpackung": "Bundle pack",
    "Stück": "units",
    "Milliliter": "ml",
    "Gramm": "g",
}


def english_pack_size(value: object) -> str:
    text = _clean(value)
    for german, english in PACK_WORDS.items():
        text = re.sub(rf"\b{german}\b", english, text)
    return text


def english_country(value: object) -> str:
    text = _clean(value)
    return GERMAN_COUNTRIES.get(text, text)


def english_dosage_form(value: object) -> str:
    text = _clean(value)
    if not text:
        return ""
    for german, english in sorted(GERMAN_DOSAGE_FORMS.items(), key=lambda item: -len(item[0])):
        if text.lower().startswith(german.lower()):
            return english
    return text


def english_status(value: object) -> str:
    text = _clean(value)
    return GERMAN_STATUS.get(text.lower(), text)


def english_legal_status(value: object) -> str:
    text = _clean(value)
    for german, english in GERMAN_LEGAL_STATUS.items():
        if text.lower().startswith(german):
            return english
    return text


def english_procedure(value: object) -> str:
    """"CP - europäisches zentralisiertes Verfahren" -> "Centralised"."""
    text = _clean(value)
    code = text.split("-", 1)[0].strip().upper()
    return GERMAN_PROCEDURE.get(code, text)


def german_date(value: object) -> str:
    text = _clean(value)
    match = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    return f"{match.group(3)}-{match.group(2)}-{match.group(1)}" if match else text


# ---------------------------------------------------------------- the session

_session_lock = threading.Lock()
# One search at a time. The result list, its further pages and the documents
# all hang off the session's state at BfArM's end, so two searches sharing it
# would read each other's results.
_search_lock = threading.Lock()
_session: requests.Session | None = None
_session_opened = 0.0


def _follow_handover(session: requests.Session, response: requests.Response, hops: int = 6) -> requests.Response:
    """Walk the sign-in handover, which a browser walks by submitting each form on load."""
    for _ in range(hops):
        if "SAML" not in response.text:
            return response
        form = BeautifulSoup(response.text, "html.parser").find("form")
        if not form or not form.get("action"):
            return response
        data = {
            field.get("name"): field.get("value", "")
            for field in form.find_all("input")
            if field.get("name")
        }
        response = session.post(form["action"], data=data, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    return response


def open_session() -> requests.Session:
    """An anonymous session with BfArM's disclaimer acknowledged."""
    session = requests.Session()
    session.headers.update(HEADERS)
    _follow_handover(session, session.get(ENTRY_URL, timeout=REQUEST_TIMEOUT))
    session.get(TERMS_URL, timeout=REQUEST_TIMEOUT)
    return session


def _current_session(reopen: bool = False) -> requests.Session:
    global _session, _session_opened
    with _session_lock:
        expired = time.time() - _session_opened > SESSION_MAX_AGE
        if reopen or expired or _session is None:
            _session = open_session()
            _session_opened = time.time()
        return _session


# ---------------------------------------------------------------- the search

def form_fields(html: str, form_id: str) -> dict[str, str]:
    """Every value a browser would post back, so only the search terms change.

    JSF rejects a post that leaves out its view state or any field it drew, so
    the form is read back rather than assembled from scratch.
    """
    form = BeautifulSoup(html, "html.parser").find("form", id=form_id)
    if form is None:
        return {}
    data: dict[str, str] = {}
    for field in form.find_all(["input", "select", "textarea"]):
        name = field.get("name")
        if not name:
            continue
        if field.name == "select":
            chosen = field.find("option", selected=True) or field.find("option")
            data[name] = chosen.get("value", "") if chosen else ""
        elif field.get("type") in {"checkbox", "radio"}:
            if field.has_attr("checked"):
                data[name] = field.get("value", "on")
        elif field.get("type") != "submit":
            data[name] = field.get("value", "")
    return data


def search_payload(page_html: str, substance: str) -> dict[str, str]:
    data = form_fields(page_html, "searchForm")
    data[f"{FORM_PREFIX}:searchRows:0:searchTerm"] = substance
    data[f"{FORM_PREFIX}:searchRows:0:searchField"] = SUBSTANCE_FIELD
    # AND: only medicines that may currently be sold. MENSCH: human medicines.
    data[f"{FILTER_PREFIX}:verkehrsfaehige:verkehrsfaehige"] = "AND"
    data[f"{FILTER_PREFIX}:humanarzneimittel:humanarzneimittel"] = "MENSCH"
    data[f"{FORM_PREFIX}:suchestarten"] = "Suche starten"
    return data


def found_documents(html: str) -> int:
    match = re.search(r"Gefundene Dokumente:\s*([\d.]+)", BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
    return int(match.group(1).replace(".", "")) if match else 0


def parse_result_rows(html: str) -> list[dict[str, str]]:
    """One row per product in the result list, with the link to its document."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id=lambda value: bool(value) and value.endswith("searchResultsComponent:titles"))
    if table is None:
        return []
    rows = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 6:
            continue
        link = row.find("a", href=lambda href: bool(href) and "jpadocdisplay" in href)
        rows.append({
            "product": _clean(cells[1].get_text(" ", strip=True)),
            "dosage_form": _clean(cells[2].get_text(" ", strip=True)),
            "company": _clean(cells[3].get_text(" ", strip=True)),
            "entry_number": _clean(cells[4].get_text(" ", strip=True)),
            "registration_number": _clean(cells[5].get_text(" ", strip=True)),
            "document_url": requests.compat.urljoin(BASE_URL, link["href"]) if link else "",
        })
    return rows


def _next_page_payloads(html: str) -> list[dict[str, str]]:
    """What to post to reach each further page of the result list."""
    soup = BeautifulSoup(html, "html.parser")
    payloads = []
    for button in soup.find_all("input", attrs={"type": "submit"}):
        name = button.get("name") or ""
        if "goToPageButton" not in name:
            continue
        data = form_fields(html, "searchResultsForm")
        data[name] = button.get("value", "")
        payloads.append(data)
    return payloads


# ------------------------------------------------------------- the document

def _labelled_values(soup: BeautifulSoup) -> dict[str, str]:
    """Every two-cell row of the document read as label -> value."""
    values: dict[str, str] = {}
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) != 2:
            continue
        label = _clean(cells[0].get_text(" ", strip=True))
        value = _clean(cells[1].get_text(" ", strip=True))
        if label and label not in values:
            values[label] = value
    return values


def _companies(soup: BeautifulSoup) -> list[dict[str, str]]:
    """The pharmaceutical companies, each under the heading that gives its role.

    Several tables can share one heading -- a product often has two release
    sites -- so each table takes the nearest heading above it.
    """
    companies = []
    for table in soup.find_all("table"):
        cells = [_clean(cell.get_text(" ", strip=True)) for cell in table.find_all(["td", "th"])]
        if not cells or cells[0] != "PU-Nummer":
            continue
        fields = _labelled_values(table)
        heading = table.find_previous(["h3", "h4", "h5"])
        role_text = _clean(heading.get_text(" ", strip=True)).rstrip("*") if heading else ""
        role = COMPANY_ROLES.get(role_text, role_text)
        name = fields.get("Name", "")
        if not name:
            continue
        address = ", ".join(
            part for part in (fields.get("Straße Hausnummer", "").rstrip(" -"), fields.get("PLZ Ort", "")) if part
        )
        companies.append({
            "role": role,
            "name": company_name(name),
            "address": address,
            "country": english_country(fields.get("Land", "")),
        })
    return companies


def _active_substances(soup: BeautifulSoup) -> list[dict[str, str]]:
    """The composition table: the ingredient, how much of it, and the unit."""
    substances = []
    for table in soup.find_all("table"):
        header = [_clean(cell.get_text(" ", strip=True)) for cell in table.find_all("tr")[0].find_all(["th", "td"])]
        if "Stoffname" not in header or "Maßeinheit" not in header:
            continue
        caption = table.find_previous(["caption", "h3", "h4", "h5", "span"])
        if caption and "Hilfsstoffe" in _clean(caption.get_text(" ", strip=True)):
            continue  # excipients, not the active ingredient
        name_at, amount_at, unit_at = header.index("Stoffname"), header.index("Stoffmenge"), header.index("Maßeinheit")
        for row in table.find_all("tr")[1:]:
            cells = [_clean(cell.get_text(" ", strip=True)) for cell in row.find_all("td")]
            if len(cells) <= unit_at or not cells[name_at]:
                continue
            substances.append({
                "name": cells[name_at],
                "amount": "" if cells[amount_at] in {"-", "n/a"} else cells[amount_at],
                "unit": "" if cells[unit_at] in {"-", "n/a"} else cells[unit_at],
            })
        break
    return substances


UNITS = {"Milligramm": "mg", "Gramm": "g", "Mikrogramm": "microgram", "Milliliter": "ml", "Liter": "l",
         "Internationale Einheiten": "IU", "Mikroliter": "microlitre"}


def parse_document(html: str) -> dict[str, Any]:
    """The fields this project reports, read from one AMIce document."""
    soup = BeautifulSoup(html, "html.parser")
    values = _labelled_values(soup)
    companies = _companies(soup)
    substances = _active_substances(soup)
    strength = "; ".join(
        f"{item['amount']} {UNITS.get(item['unit'], item['unit'])}".strip()
        for item in substances
        if item["amount"]
    )
    return {
        "product": values.get("Arzneimittelbezeichnung", ""),
        "dosage_form": english_dosage_form(values.get("Darreichungsform", "")),
        "active_substance": "; ".join(item["name"] for item in substances),
        "strength": strength,
        "atc_code": values.get("Indikation/ATC-Code", ""),
        "registration_number": values.get("Zulassungsnummer/ Registrierungsnummer", ""),
        "registration_date": german_date(values.get("Datum der Zulassung/Registrierung (Wirksamkeitsdatum)", "")),
        "status": english_status(values.get("Status", "")),
        "authorisation_scope": english_procedure(values.get("Verfahrenstyp", "")),
        "pack_size": english_pack_size(values.get("Packungsgröße", "")),
        "classification": english_legal_status(values.get("Verkaufsabgrenzung", "")),
        "marketable": values.get("Verkehrsfähigkeit", ""),
        "companies": companies,
    }


def build_row(
    listed: dict[str, str],
    detail: dict[str, Any],
    substance: str,
    document_url: str,
    checked_at: str,
) -> dict[str, Any]:
    """One German product, with the holder and the release sites kept apart."""
    companies = detail.get("companies") or []
    holder = next((item for item in companies if item["role"] == "holder"), None)
    makers = [item for item in companies if item["role"] != "holder"]
    active = detail.get("active_substance") or substance
    return {
        "substance": active,
        "active_substance": active,
        "source_substance": active,
        "product": detail.get("product") or listed.get("product", ""),
        "company": (holder or {}).get("name") or company_name(listed.get("company", "")),
        "country": "Germany",
        "region": "EU",
        "status": detail.get("status", ""),
        "authorisation_scope": detail.get("authorisation_scope", ""),
        "strength": detail.get("strength", ""),
        "dosage_form": detail.get("dosage_form") or english_dosage_form(listed.get("dosage_form", "")),
        "pack_size": detail.get("pack_size", ""),
        "atc_code": detail.get("atc_code", ""),
        "classification": detail.get("classification", ""),
        "registration_number": detail.get("registration_number") or listed.get("registration_number", ""),
        "registration_date": detail.get("registration_date", ""),
        "manufacturer_name": "; ".join(item["name"] for item in makers),
        "manufacturer_country": "; ".join(dict.fromkeys(item["country"] for item in makers if item["country"])),
        "manufacturer_address": "; ".join(item["address"] for item in makers if item["address"]),
        "manufacturer_role": "; ".join(dict.fromkeys(item["role"] for item in makers if item["role"])),
        "manufacturer_source": document_url,
        "manufacturers": makers,
        "source": "BfArM Germany",
        "source_url": document_url or PUBLIC_SEARCH_URL,
        "product_url": document_url or PUBLIC_SEARCH_URL,
        "document_type": "AMIce public medicines database entry",
        "copyright": COPYRIGHT,
        "view_only": True,
        "view_only_reason": VIEW_ONLY_NOTE,
        "last_checked": checked_at,
    }


def _result_page_htmls(session: requests.Session, first_html: str, pages: int) -> Iterable[str]:
    yield first_html
    for payload in _next_page_payloads(first_html)[1:pages]:
        try:
            response = session.post(RESULT_URL, data=payload, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as error:
            logger.info("BfArM: a further page of results did not load (%s)", error)
            return
        yield response.text


def _search_once(session: requests.Session, term: str) -> str:
    page = session.get(SEARCH_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    page.raise_for_status()
    if "searchForm" not in page.text:
        raise requests.RequestException("BfArM did not show the search form")
    result = session.post(SEARCH_URL, data=search_payload(page.text, term), headers=HEADERS, timeout=REQUEST_TIMEOUT)
    result.raise_for_status()
    return result.text


def run_germany_bfarm_search(substance: str) -> list[dict[str, Any]]:
    """Search AMIce for a molecule and read back what Germany has authorised."""
    term = _clean(substance)
    if not term:
        return []
    with _search_lock:
        return _search(term)


def _search(term: str) -> list[dict[str, Any]]:
    checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    html = ""
    for attempt, query in enumerate((term, base_name(term))):
        if attempt and query.lower() == term.lower():
            break  # the salt was the whole name; no second question to ask
        try:
            session = _current_session(reopen=attempt > 0)
            html = _search_once(session, query)
        except requests.RequestException as error:
            logger.info("BfArM search for %s failed (%s)", query, error)
            html = ""
            continue
        if found_documents(html):
            break
    if not found_documents(html):
        return []

    total = found_documents(html)
    session = _current_session()
    listed_rows: list[dict[str, str]] = []
    for page_html in _result_page_htmls(session, html, MAX_PAGES):
        listed_rows.extend(parse_result_rows(page_html))
        if len(listed_rows) >= MAX_DETAILS:
            break

    wanted = listed_rows[:MAX_DETAILS]
    details = _documents(session, wanted)
    rows = []
    for listed in wanted:
        row = build_row(listed, details.get(listed["document_url"], {}), term, listed["document_url"], checked_at)
        if total > len(wanted):
            row["available_total"] = total
        rows.append(row)
    return rows


def _document(session: requests.Session, listed: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """One product's document, and nothing if it turns out to be another product.

    The check matters: the document is fetched by its own identifier, so a
    session that has moved on must not be allowed to attach one product's
    manufacturers to another's name.
    """
    url = listed["document_url"]
    if not url:
        return url, {}
    try:
        response = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as error:
        logger.info("BfArM document %s did not load (%s)", url, error)
        return url, {}
    detail = parse_document(response.text)
    if detail.get("product") and listed.get("product") and detail["product"] != listed["product"]:
        logger.info("BfArM returned %r for %r; ignoring it", detail["product"], listed["product"])
        return url, {}
    return url, detail


def _documents(session: requests.Session, listed_rows: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    """Every listed product's document, a few at a time, inside a time budget."""
    details: dict[str, dict[str, Any]] = {}
    deadline = time.monotonic() + DETAIL_BUDGET
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        futures = {pool.submit(_document, session, listed): listed for listed in listed_rows}
        for future in as_completed(futures):
            url, detail = future.result()
            details[url] = detail
            if time.monotonic() > deadline:
                # Whatever has arrived is reported; the rest keep the values
                # the result list already gave.
                for pending in futures:
                    pending.cancel()
                break
    return details
