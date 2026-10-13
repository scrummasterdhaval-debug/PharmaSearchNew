from playwright.sync_api import sync_playwright

with sync_playwright() as p:

    browser = p.chromium.launch(headless=False)

    page = browser.new_page()

    page.goto(
        "https://www.ema.europa.eu/en/search?search_api_fulltext=Dapagliflozin",
        wait_until="networkidle"
    )

    page.wait_for_timeout(10000)

    print(page.locator("body").inner_text())

    input("Press Enter to close browser...")

    browser.close()
