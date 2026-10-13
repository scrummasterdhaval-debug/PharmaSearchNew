from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from core.logging_config import get_logger


logger = get_logger(__name__)

EMA_HOSTS = ("ema.europa.eu",)


class NotAnEmaPage(ValueError):
    """Raised for an address that is not a page on the EMA website."""


def is_ema_page_url(url: object) -> bool:
    """True only for an https address on the EMA website.

    Whatever this parser reads is saved as an EMA record, so it must never be
    pointed anywhere else: not at an internal address the server can reach and
    a caller cannot, and not at a look-alike host. The hostname is taken from
    the parsed URL rather than matched as text, so "ema.europa.eu@elsewhere"
    and "ema.europa.eu.elsewhere" are both refused.
    """
    try:
        parsed = urlparse(str(url or "").strip())
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or port not in (None, 443):
        return False
    return any(host == allowed or host.endswith("." + allowed) for allowed in EMA_HOSTS)


def extract_product_page(url: str) -> dict[str, str]:

    if not is_ema_page_url(url):
        raise NotAnEmaPage(url)

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=True)

        page = browser.new_page()

        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # A redirect can carry the browser off the EMA site after the address
        # itself passed; what gets read must still be on it.
        if not is_ema_page_url(page.url):
            browser.close()
            raise NotAnEmaPage(page.url)
        page.wait_for_selector("body", timeout=15000)

        title = page.title()

        product_name = title.split("|")[0].strip()

        page_text = page.locator("body").inner_text()

        links = page.locator("a").evaluate_all(
            "(elements) => elements.map(e => e.href)"
        )

        smpc_url = ""
        pil_url = ""
        assessment_report_url = ""

        for link in links:

            if not link:
                continue

            link = str(link)

            if (
                "product-information" in link
                and "_en.pdf" in link
            ):
                smpc_url = link

            if (
                "package-leaflet" in link
                and "_en.pdf" in link
            ):
                pil_url = link

            if (
                "assessment-report" in link
                and "_en.pdf" in link
            ):
                assessment_report_url = link

        active_substance = ""
        status = ""
        mah = ""
        atc_code = ""
        authorisation_date = ""

        if "contains the active substance" in page_text:

            start = page_text.find(
                "contains the active substance"
            )

            text = page_text[start:start + 200]

            active_substance = text.split(".")[0]

            active_substance = active_substance.replace(
                "contains the active substance",
                ""
            ).strip()

        if "This medicine is authorised for use in the European Union" in page_text:
            status = "Authorised"

        if "Marketing authorisation holder" in page_text:

            start = page_text.find(
                "Marketing authorisation holder"
            )

            text = page_text[start:start + 300]

            lines = text.split("\n")

            if len(lines) > 1:
                mah = lines[1].strip()

        if "Anatomical therapeutic chemical (ATC) code" in page_text:

            start = page_text.find(
                "Anatomical therapeutic chemical (ATC) code"
            )

            text = page_text[start:start + 200]

            lines = text.split("\n")

            if len(lines) > 1:
                atc_code = lines[1].strip()

        if "Marketing authorisation issued" in page_text:

            start = page_text.find(
                "Marketing authorisation issued"
            )

            text = page_text[start:start + 100]

            lines = text.split("\n")

            if len(lines) > 1:
                authorisation_date = lines[1].strip()

        logger.debug("EMA product page SMPC URL: %s", smpc_url)
        logger.debug("EMA product page PIL URL: %s", pil_url)
        logger.debug("EMA product page assessment URL: %s", assessment_report_url)

        browser.close()
        logger.info("Parsed EMA product page: %s", product_name)
        logger.debug("EMA product page MAH: %s", mah)
        logger.debug("EMA product page active substance: %s", active_substance)

        return {
            "page_title": title,
            "product_name": product_name,
            "product_url": url,
            "active_substance": active_substance,
            "status": status,
            "mah": mah,
            "atc_code": atc_code,
            "authorisation_date": authorisation_date,
            "smpc_url": smpc_url,
            "pil_url": pil_url,
            "assessment_report_url": assessment_report_url
        }
