from sources.mhra_product_parser import extract_mhra_product_page

result = extract_mhra_product_page(
    "https://products.mhra.gov.uk/search/?search=Forxiga&page=1"
)

print(result)
