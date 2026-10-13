from playwright.sync_api import sync_playwright

def extract_product_page(url):

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=False)

        page = browser.new_page()

        page.goto(url)

        page.wait_for_timeout(5000)

        title = page.title()

        product_name = title.split("|")[0].strip()

        page_text = page.locator("body").inner_text()

        active_substance = ""
        status = ""
        mah = ""
        atc_code = ""
        authorisation_date = ""

        # Active Substance
        if "contains the active substance" in page_text:

            start = page_text.find(
                "contains the active substance"
            )

            text = page_text[start:start+200]

            active_substance = text.split(".")[0]

            active_substance = active_substance.replace(
                "contains the active substance",
                ""
            ).strip()

        # Status
        if "This medicine is authorised for use in the European Union" in page_text:
            status = "Authorised"

        # Marketing Authorisation Holder
        if "Marketing authorisation holder" in page_text:

            start = page_text.find(
                "Marketing authorisation holder"
            )

            text = page_text[start:start+300]

            lines = text.split("\n")

            if len(lines) > 1:
                mah = lines[1].strip()

        # ATC Code
        if "Anatomical therapeutic chemical (ATC) code" in page_text:

            start = page_text.find(
                "Anatomical therapeutic chemical (ATC) code"
            )

            text = page_text[start:start+200]

            lines = text.split("\n")

            if len(lines) > 1:
                atc_code = lines[1].strip()

        # Authorisation Date
        if "Marketing authorisation issued" in page_text:

            start = page_text.find(
                "Marketing authorisation issued"
            )

            text = page_text[start:start+100]

            lines = text.split("\n")

            if len(lines) > 1:
                authorisation_date = lines[1].strip()
            links = page.locator("a").evaluate_all(
                "(elements) => elements.map(e => e.href)"
            )

            for link in links:
                print(link)

        browser.close()

        return {
            "page_title": title,
            "product_name": product_name,
            "product_url": url,
            "active_substance": active_substance,
            "status": status,
            "mah": mah,
            "atc_code": atc_code,
            "authorisation_date": authorisation_date
        }
