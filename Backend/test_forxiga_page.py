from sources.ema_product_parser import extract_product_page

result = extract_product_page(
    "https://www.ema.europa.eu/en/medicines/human/EPAR/forxiga"
)

print(result)
