from __future__ import annotations

import re


THERAPEUTIC_CATEGORY_BY_ATC_PREFIX = {
    "A10": "Antidiabetic",
    "A02": "Acid disorder medicine",
    "B01": "Antithrombotic",
    "C": "Cardiovascular",
    "J01": "Antibiotic",
    "L": "Oncology / immunology",
    "M01": "Anti-inflammatory",
    "N02": "Analgesic",
    "N05": "Antipsychotic / sedative",
    "N06": "Psychoanaleptic",
    "R03": "Respiratory medicine",
}

THERAPEUTIC_CATEGORY_BY_SUBSTANCE = {
    "metformin": "Antidiabetic",
    "ibuprofen": "Anti-inflammatory / analgesic",
    "paracetamol": "Analgesic / antipyretic",
    "acetaminophen": "Analgesic / antipyretic",
    "enalapril": "Antihypertensive",
    "amlodipine": "Antihypertensive",
    "atorvastatin": "Lipid-lowering",
    "mirabegron": "Overactive bladder medicine",
    "solifenacin": "Overactive bladder medicine",
    "quetiapine": "Antipsychotic",
    "tafluprost": "Glaucoma medicine",
    "prilocaine": "Local anaesthetic",
    "lidocaine": "Local anaesthetic",
    "lidocaine prilocaine": "Local anaesthetic",
    "prilocaine lidocaine": "Local anaesthetic",
}

KEYWORD_CATEGORIES = [
    (r"\bdiabet", "Antidiabetic"),
    (r"\bhypertension|\bhigh blood pressure|\bantihypertensive", "Antihypertensive"),
    (r"\bcholesterol|\blipid|\bstatin", "Lipid-lowering"),
    (r"\bpain|\banalgesi|\bantipyretic|\bfever", "Analgesic / antipyretic"),
    (r"\binflamm", "Anti-inflammatory"),
    (r"\bantibacterial|\bantibiotic", "Antibiotic"),
    (r"\bpsychosis|\bschizophrenia|\bbipolar|\bantipsychotic", "Antipsychotic"),
    (r"\boveractive bladder|\burinary urgency|\burolog", "Overactive bladder medicine"),
    (r"\bglaucoma|\bintraocular pressure", "Glaucoma medicine"),
    (r"\blocal anaesthetic|\blocal anesthetic|\banaesthesia|\banesthesia", "Local anaesthetic"),
    (r"\bcancer|\boncology|\bneoplasm", "Oncology medicine"),
    (r"\basthma|\bcopd|\bbronchodilator|\brespiratory", "Respiratory medicine"),
    (r"\bthromb|\bcoagul|\bplatelet", "Antithrombotic"),
]


def therapeutic_category_from_atc(atc_code: object) -> str:
    code = str(atc_code or "").strip().upper()
    if not code:
        return ""
    for prefix in sorted(THERAPEUTIC_CATEGORY_BY_ATC_PREFIX, key=len, reverse=True):
        if code.startswith(prefix):
            return THERAPEUTIC_CATEGORY_BY_ATC_PREFIX[prefix]
    return ""


def therapeutic_category_from_substance(substance: object) -> str:
    text = " ".join(str(substance or "").lower().replace("+", " ").split())
    for key, value in THERAPEUTIC_CATEGORY_BY_SUBSTANCE.items():
        if key in text:
            return value
    return ""


def short_therapeutic_category(
    category: object = "",
    substance: object = "",
    atc_code: object = "",
) -> str:
    atc_category = therapeutic_category_from_atc(atc_code)
    if atc_category:
        return atc_category

    # The molecule's name is not consulted: a category guessed from it is not
    # something this regulator published.
    text = " ".join(str(category or "").split())
    if not text:
        return ""
    if len(text) <= 48 and not re.search(r"[.;:]", text):
        return text
    lower_text = text.lower()
    for pattern, value in KEYWORD_CATEGORIES:
        if re.search(pattern, lower_text):
            return value
    first_sentence = re.split(r"[.;:]", text, maxsplit=1)[0].strip()
    words = first_sentence.split()
    if len(words) > 6:
        first_sentence = " ".join(words[:6])
    return first_sentence[:48].strip()
