import re

DOSAGE_FORMS = [
    "ORAL SOLUTION",
    "ORAL SUSPENSION",
    "FILM-COATED TABLET",
    "TABLETS",
    "TABLET",
    "CAPSULES",
    "CAPSULE",
    "INJECTION",
    "CREAM",
    "OINTMENT",
    "GEL",
    "PATCH",
    "SPRAY",
    "DROPS",
    "SYRUP",
    "POWDER"
]

def extract_dosage_form(product):

    product_upper = product.upper()

    for form in DOSAGE_FORMS:

        if form in product_upper:
            return form

    return ""


def extract_strength(product):

    match = re.search(
        r'(\d+\s*(MG|MCG|G|ML)(?:/\d+\s*(MG|MCG|G|ML))?)',
        product,
        re.IGNORECASE
    )

    if match:
        return match.group(1)

    return ""


def extract_pl_number(product):

    match = re.search(
        r'PL\s*\d+/\d+',
        product
    )

    if match:
        return match.group(0)

    return ""
