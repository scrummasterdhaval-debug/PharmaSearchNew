import re
from typing import Iterable


DOSAGE_FORM_PATTERNS: list[tuple[str, str]] = [
    ("Suspension", r"\bsuspension\b"),
    ("Suspension/drops", r"\bsuspension\s*/\s*drops\b"),
    ("Prolonged-release tablet", r"\bprolonged[- ]release tablets?\b"),
    ("Modified-release tablet", r"\bmodified[- ]release tablets?\b"),
    ("Film-coated tablet", r"\bfilm[- ]coated tablets?\b"),
    ("Film-coated tablet", r"\bcomprimidos?\s+recubiertos?\s+con\s+pel[iï¿½]cula\b"),
    ("Tablet", r"\bcomprimidos?\b"),
    ("Orodispersible tablet", r"\borodispersible tablets?\b"),
    ("Tablet", r"\btablets?\b"),
    ("Capsule", r"\bcapsules?\b"),
    ("Oral solution", r"\boral solution\b"),
    ("Oral suspension", r"\boral suspension\b"),
    ("Solution for injection", r"\b(solution for )?injection\b"),
    ("Cream", r"\bcream\b"),
    ("Ointment", r"\bointment\b"),
    ("Gel", r"\bgel\b"),
    ("Patch", r"\bpatch(?:es)?\b"),
    ("Spray", r"\bspray\b"),
    ("Drops", r"\bdrops?\b"),
    ("Syrup", r"\bsyrup\b"),
    ("Powder", r"\bpowder\b"),
]

STRENGTH_PATTERN = re.compile(
    r"""
    (?:
        \d+(?:\.\d+)?\s*
        (?:mg|mcg|micrograms?|g|kg|ml|l|iu|units?|%)\b
        (?:\s*/\s*\d*(?:\.\d+)?\s*(?:mg|mcg|micrograms?|g|kg|ml|l|iu|units?))?
    )
    (?:\s*\+\s*
        \d+(?:\.\d+)?\s*
        (?:mg|mcg|micrograms?|g|kg|ml|l|iu|units?|%)\b
        (?:\s*/\s*\d*(?:\.\d+)?\s*(?:mg|mcg|micrograms?|g|kg|ml|l|iu|units?))?
    )*
    """,
    re.IGNORECASE | re.VERBOSE,
)

REGISTRATION_NUMBER_PATTERN = re.compile(
    r"\b(?:PLGB|PLNI|PL|THRGB|THRNI|THR|NRGB|NRNI|NR)\s*\d+/\d+(?:-\d+)?\b",
    re.IGNORECASE,
)

PACK_SIZE_PATTERNS: tuple[str, ...] = (
    r"\b\d+\s*(?:x\s*)?\d*\s*(?:tablets?|capsules?|caplets?|patch(?:es)?|sachets?|vials?|ampoules?|bottles?|blisters?|syringes?)\b",
    r"\b\d+(?:\.\d+)?\s*(?:ml|mL|g)\b\s*(?:in\s*)?(?:bottle|vial|tube|sachet|ampoule)?",
)

ROUTE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("Oral", r"\boral\b|\bby mouth\b"),
    ("Intravenous", r"\bintravenous\b|\biv\b"),
    ("Subcutaneous", r"\bsubcutaneous\b|\bsc\b"),
    ("Topical", r"\btopical\b|\bcutaneous\b"),
    ("Ophthalmic", r"\bophthalmic\b|\beye\b"),
    ("Nasal", r"\bnasal\b"),
    ("Inhalation", r"\binhalation\b"),
    ("Rectal", r"\brectal\b"),
)

ATC_CODE_PATTERN = re.compile(r"\b[A-Z]\d{2}[A-Z]{1,2}\d{0,2}\b", re.IGNORECASE)


def _normalized_matches(matches: Iterable[re.Match[str]]) -> list[str]:
    return [" ".join(match.group(0).split()) for match in matches]


def clean_product_name(product: object) -> str:
    cleaned = " ".join(str(product or "").split())
    if not cleaned:
        return ""

    cleaned = re.sub(r"\.(?:pdf|html?|docx?)$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"\b(?:pdf|download|view document|document)\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:summary of product characteristics|patient information leaflet|"
        r"package leaflet|public assessment report|assessment report)\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\b(?:sm?pc|spc|pil|par)\b\s*[-:]*\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.split())

    words = cleaned.split()
    for size in range(1, len(words) // 2 + 1):
        if words[:size] == words[size : size * 2]:
            cleaned = " ".join(words[size:])
            break

    cleaned = re.sub(r"\b(.{4,}?)\s+\1\b", r"\1", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" -")


def normalize_product(product: object) -> str:
    return clean_product_name(product)


def extract_strength(product: object) -> str:
    matches = _normalized_matches(STRENGTH_PATTERN.finditer(str(product or "")))
    if not matches:
        return ""
    for value in matches:
        if re.search(r"\b(?:mg|mcg|micrograms?|g|iu|units?|%)\b", value, flags=re.IGNORECASE):
            return value
    return matches[0]


def extract_dosage_form(product: object) -> str:
    value = str(product or "")
    for label, pattern in DOSAGE_FORM_PATTERNS:
        if re.search(pattern, value, flags=re.IGNORECASE):
            return label
    return ""


def extract_registration_number(product: object) -> str:
    match = REGISTRATION_NUMBER_PATTERN.search(str(product or ""))
    return " ".join(match.group(0).upper().split()) if match else ""


def extract_pack_size(text: object) -> str:
    value = str(text or "")
    for pattern in PACK_SIZE_PATTERNS:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if match:
            return " ".join(match.group(0).split())
    return ""


def extract_route(text: object) -> str:
    value = str(text or "")
    for label, pattern in ROUTE_PATTERNS:
        if re.search(pattern, value, flags=re.IGNORECASE):
            return label
    return ""


def extract_atc_code(text: object) -> str:
    value = str(text or "")
    label_match = re.search(
        r"(?:ATC\s*code|Anatomical\s+therapeutic\s+chemical\s*\(ATC\)\s*code)"
        r"\s*[:\-\n\r ]+([A-Z]\d{2}[A-Z]{1,2}\d{0,2})",
        value,
        flags=re.IGNORECASE,
    )
    if label_match:
        return label_match.group(1).upper()
    match = ATC_CODE_PATTERN.search(value)
    return match.group(0).upper() if match else ""


def extract_pl_number(product: object) -> str:
    return extract_registration_number(product)


def extract_strength_form(product: object) -> tuple[str, str]:
    return extract_strength(product), extract_dosage_form(product)
