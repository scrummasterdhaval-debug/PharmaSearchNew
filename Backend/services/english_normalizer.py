from __future__ import annotations

import re
import unicodedata
from typing import Any


CYRILLIC_TRANSLITERATION = str.maketrans(
    {
        "А": "A",
        "Б": "B",
        "В": "V",
        "Г": "G",
        "Д": "D",
        "Е": "E",
        "Ё": "E",
        "Ж": "Zh",
        "З": "Z",
        "И": "I",
        "Й": "Y",
        "К": "K",
        "Л": "L",
        "М": "M",
        "Н": "N",
        "О": "O",
        "П": "P",
        "Р": "R",
        "С": "S",
        "Т": "T",
        "У": "U",
        "Ф": "F",
        "Х": "Kh",
        "Ц": "Ts",
        "Ч": "Ch",
        "Ш": "Sh",
        "Щ": "Shch",
        "Ъ": "",
        "Ы": "Y",
        "Ь": "",
        "Э": "E",
        "Ю": "Yu",
        "Я": "Ya",
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "kh",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "shch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
)

EXACT_TRANSLATIONS = {
    "Д": "Active",
    "д": "Active",
    "Н": "Inactive",
    "н": "Inactive",
    "Мирабегрон": "Mirabegron",
    "Бетмига": "Betmiga",
    "Астеллас Фарма Юроп Б.В.": "Astellas Pharma Europe B.V.",
    "Нидерланды": "Netherlands",
    "Германия": "Germany",
    "Индия": "India",
    "Италия": "Italy",
    "Испания": "Spain",
    "Франция": "France",
    "Венгрия": "Hungary",
    "Ирландия": "Ireland",
    "Россия": "Russia",
    "Китай": "China",
    "Япония": "Japan",
}

PHRASE_TRANSLATIONS = {
    "таблетки": "tablets",
    "таблетка": "tablet",
    "капсулы": "capsules",
    "капсула": "capsule",
    "раствор": "solution",
    "суспензия": "suspension",
    "крем": "cream",
    "мазь": "ointment",
    "капли": "drops",
    "порошок": "powder",
    "инъекций": "injection",
    "инъекции": "injection",
    "пленочной оболочкой": "film-coated",
    "пролонгированного высвобождения": "prolonged-release",
    "зарегистрирован": "registered",
    "зарегистрировано": "registered",
    "действующий": "active",
    "истек": "expired",
}


def _has_cyrillic(text: str) -> bool:
    return bool(re.search(r"[\u0400-\u04FF]", text))


def _has_untranslated_non_latin(text: str) -> bool:
    """Letters of another script; symbols such as ®, µ or € are not a language."""
    if _has_cyrillic(text):
        return False
    return any(
        ord(char) > 0x7F
        and unicodedata.category(char).startswith("L")
        and "LATIN" not in unicodedata.name(char, "")
        # The micro sign, which NFKD turns into Greek mu: "5 µg".
        and char not in "µμ"
        for char in text
    )


def _strip_latin_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _translate_known_phrases(text: str) -> str:
    translated = text
    for source, target in sorted(PHRASE_TRANSLATIONS.items(), key=lambda item: len(item[0]), reverse=True):
        translated = re.sub(re.escape(source), target, translated, flags=re.IGNORECASE)
    return translated


def english_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text in EXACT_TRANSLATIONS:
        return EXACT_TRANSLATIONS[text]

    translated = _translate_known_phrases(text)
    if _has_cyrillic(translated):
        translated = translated.translate(CYRILLIC_TRANSLITERATION)
    translated = _strip_latin_accents(translated)
    translated = re.sub(r"\bB\.V\b(?!\.)", "B.V.", translated)
    translated = re.sub(r"\bFarma\b", "Pharma", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\bYurop\b", "Europe", translated, flags=re.IGNORECASE)
    if _has_untranslated_non_latin(translated):
        translated = f"Local-language registry value: {translated}"
    return " ".join(translated.split())


def english_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    for field in (
        "substance",
        "active_substance",
        "active_substances",
        "searched_substance",
        "source_substance",
        "product",
        "company",
        "commercial_company",
        "brand_owner",
        "labeler_name",
        "sponsor",
        "applicant",
        "mah",
        "ma_holder",
        "manufacturer_name",
        "manufacturer_country",
        "status",
        "dosage_form",
        "pack_size",
        "therapeutic_category",
        "document_type",
    ):
        if field in normalized:
            normalized[field] = english_text(normalized[field])
    # Imported here: vendor_display is presentation, and english_text is also
    # used on its own where no row is being shown.
    from services.vendor_display import vendor_row

    return vendor_row(normalized)
