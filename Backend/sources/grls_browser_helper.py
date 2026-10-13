import json
import sys

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


def clean_text(value):
    return " ".join(str(value or "").split()).strip()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    substance = sys.argv[1] if len(sys.argv) > 1 else ""
    search_term = sys.argv[2] if len(sys.argv) > 2 else substance
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 50
    rows = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://grls.rosminzdrav.ru/grls.aspx", wait_until="domcontentloaded", timeout=30000)
        page.fill("#ctl00_plate_txtMNN", search_term)
        page.click("#ctl00_plate_bSeek")
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        html = page.content()
        source_url = page.url
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    for table_row in soup.select("tr"):
        cells = [clean_text(cell.get_text(" ", strip=True)) for cell in table_row.find_all("td", recursive=False)]
        if len(cells) < 11 or not cells[0].isdigit():
            continue
        product = cells[1]
        active = cells[2]
        if search_term.lower() not in f"{product} {active}".lower():
            continue
        rows.append(
            {
                "substance": substance,
                "active_substance": active,
                "product": product,
                "company": cells[4],
                "country": "Russia",
                "region": "RU",
                "status": cells[10] or "Registered in GRLS",
                "dosage_form": cells[3],
                "registration_number": cells[6],
                "registration_date": cells[7],
                "expiry_date": cells[8],
                "manufacturer_name": cells[4],
                "manufacturer_country": cells[5],
                "source": "GRLS Russia",
                "source_url": source_url,
                "product_url": source_url,
                "url": source_url,
            }
        )
        if len(rows) >= limit:
            break
    print(json.dumps(rows, ensure_ascii=False))


if __name__ == "__main__":
    main()
