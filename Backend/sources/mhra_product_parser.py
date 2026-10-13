from playwright.sync_api import sync_playwright

from core.logging_config import get_logger


logger = get_logger(__name__)


def extract_mhra_product_page(url: str) -> dict[str, str]:

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=True)

        page = browser.new_page()

        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("body", timeout=15000)
        logger.debug("MHRA product page URL: %s", page.url)
        logger.debug("MHRA product page title: %s", page.title())

        body_text = page.locator("body").inner_text()

        logger.debug("MHRA product page body preview: %s", body_text[:3000])

        try:

            checkbox = page.locator("input[type='checkbox']")

            if checkbox.count() > 0:

                checkbox.check()

                page.wait_for_timeout(1000)

                page.get_by_text("Agree", exact=True).click()

                page.wait_for_timeout(3000)

        except Exception as e:
            logger.warning("MHRA cookie acceptance failed: %s", e)
        current_url = page.url
        title = page.title()

        logger.debug("MHRA current URL after cookie flow: %s", current_url)
        logger.debug("MHRA title after cookie flow: %s", title)

        body_text = page.locator("body").inner_text()

        # Use page title instead of hardcoded FORXIGA
        product_name = title.strip()
        logger.info("Parsed MHRA product page: %s", product_name)


        active_substance = ""

        for line in body_text.split("\n"):

            if "Active substances:" in line:

                active_substance = (
                    line.replace("Active substances:", "")
                    .strip()
                )

                break

        logger.debug("MHRA active substance: %s", active_substance)

        with open(
            "mhra_output_after_agree.txt",
            "w",
            encoding="utf-8"
        ) as f:
            f.write(body_text)
            logger.debug("Saved MHRA output after agree")

        browser.close()

        return {
            "product_name": product_name,
            "active_substance": active_substance,
            "product_url": current_url
        }
