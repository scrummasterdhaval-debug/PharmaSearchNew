from sources.ema_product_parser import extract_product_page
from sources.product_details import save_product_details

result = extract_product_page(
    "https://www.ema.europa.eu/en/medicines/human/EPAR/jardiance"
)

save_product_details(
    result["active_substance"],
    result["product_name"],
    result["mah"],
    "EU",
    result["status"],
    result["product_url"],
    result["atc_code"],
    result["authorisation_date"]
)

print("Jardiance saved successfully")
