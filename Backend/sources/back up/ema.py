from playwright.sync_api import sync_playwright

def run_ema_search(substance):

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=False)

        page = browser.new_page()

        url = f"https://www.ema.europa.eu/en/search?search_api_fulltext={substance}"

        page.goto(url)

        page.wait_for_timeout(10000)

        links = page.locator("a").evaluate_all(
            "(elements) => elements.map(e => e.href)"
        )

        browser.close()

        product_links = []

        for link in links:

            if link and "/medicines/human/EPAR/" in str(link):

                if link not in product_links:
                    product_links.append(link)

        return product_links

def find_product_url(substance):

    results = run_ema_search(substance)

    if not results:
        return None

    return results[0]
