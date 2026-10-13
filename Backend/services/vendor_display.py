"""What a vendor reads: registry values made presentable without changing them.

Registries publish in their own language, markup and vocabulary. Indonesia's
BPOM sends HTML entities and <br> separators, France files "comprime pellicule
secable", Belgium's status is "available, not_commercialised", and dates come
in eight shapes. Shown side by side, a result table reads as broken even where
every value is right.

Everything here is applied as rows are displayed or exported, so a row found
live is presented the same way as one stored. Stored values keep the registry's
own wording, which is the evidence; only markup and placeholders are removed
from the store itself.
"""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any

from services.regulatory_dates import parse_regulatory_date


# Values registries use for "nothing here". Shown as a value they read as data.
PLACEHOLDERS = {
    "-", "--", "---", ".", "?", "n/a", "na", "n.a.", "none", "null", "nan", "nil",
    "not yet assigned", "not assigned", "tbd", "unknown", "0",
}

TAG_BREAK = re.compile(r"<\s*(?:br|/p|p)\s*/?\s*>", re.IGNORECASE)
TAG_ANY = re.compile(r"<[^>]{1,40}>")


def clean_text(value: object) -> str:
    """Markup decoded, separators kept, placeholders dropped, spacing collapsed."""
    text = str(value or "")
    if not text.strip():
        return ""
    # Twice: BPOM encodes some values twice ("&amp;lt;br&amp;gt;").
    text = html.unescape(html.unescape(text))
    # Ligatures a PDF leaves behind ("Sanoﬁ") and full-width letters.
    text = unicodedata.normalize("NFKC", text)
    text = TAG_BREAK.sub("; ", text)
    text = TAG_ANY.sub(" ", text)
    text = " ".join(text.split()).strip(" ;,")
    text = re.sub(r"\s*,?\s*;\s*(?:;\s*)*", "; ", text)
    if text.lower() in PLACEHOLDERS:
        return ""
    return text


ATC_PATTERN = re.compile(r"[A-Z](?:\d{2}(?:[A-Z](?:[A-Z](?:\d{2})?)?)?)?")


def clean_atc_code(value: object) -> str:
    """An ATC code, or nothing: "Not yet assigned", "A02BC011" and "K01B3" are not codes."""
    text = clean_text(value).upper().replace(" ", "")
    return text if ATC_PATTERN.fullmatch(text) else ""


# --- Company and manufacturer --------------------------------------------------

PAGE_FOOTER = re.compile(r"\s*\bPage \d+ of \d+\b\s*", re.IGNORECASE)
SPLIT_WORD_FIXES = (
    (re.compile(r"\bCompan y\b"), "Company"),
    (re.compile(r"\bLimit ed\b"), "Limited"),
    (re.compile(r"\bLaborator ies\b"), "Laboratories"),
)
# Text that came out of a leaflet rather than a company field.
PROSE_MARKERS = re.compile(
    r"https?://|www\.|\bleaflet\b|\bprospecto\b|\brevisad[oa]\b|\brevised\b|\bplease\b"
    r"|\byour\b|\bphysician\b|\bdoctor\b|\bpharmacist\b|\bread (?:all|this)\b"
    r"|\bproof no\b|\bactual size\b|\bla informaci[oó]n\b|\bfecha de la\b"
    r"|\bmarketing authori[sz]ation holder and\b|\btitular de la autorizaci[oó]n\b"
    r"|\bthis medicine\b|\besta informaci[oó]n\b|\bdestinada\b|\bprofesionales\b"
    r"|�",
    re.IGNORECASE,
)


# What a company name is made of, in any of the registers' languages.
COMPANY_WORDS = re.compile(
    r"\b(?:ltd|limited|plc|inc|llc|gmbh|s\.?a\.?|b\.?v\.?|n\.?v\.?|a/s|ab|oy|s\.?p\.?a|s\.?r\.?l"
    r"|pharma\w*|laborator\w*|healthcare|industri\w*|company|group|corp\w*|kg|sas|srl|d\.?d"
    r"|pte|sdn|bhd|co|cie|farmac\w*|obshchestvo|predpriyatie|cong ty|tnhh|holding\w*)\b",
    re.IGNORECASE,
)
# Words that belong to a leaflet's text, not to a company's name.
LEAFLET_WORDS = re.compile(
    r"\byou\b|\byour\b|\btake\b|\bdose\b|\bmedicine\b|\btablets? if\b|\bif you\b|\bproblems\b"
    r"|\bfeeling sick\b|\bpregnan\w*|\bside effects?\b|•",
    re.IGNORECASE,
)


def clean_company(value: object) -> str:
    """A company name as a vendor expects to read it.

    Values parsed from leaflets carry page footers, words split by the PDF and
    whole sentences. A value made of several parts keeps the parts that are
    names and addresses; one that is only prose is dropped rather than shown.
    """
    text = clean_text(value)
    if not text:
        return ""
    text = PAGE_FOOTER.sub(" ", text)
    for pattern, replacement in SPLIT_WORD_FIXES:
        text = pattern.sub(replacement, text)
    parts = [part.strip() for part in re.split(r";\s*", text) if part.strip()]
    kept = [
        part for part in parts
        if not PROSE_MARKERS.search(part)
        and len(part) <= 160
        # A sentence lifted out of a leaflet: no company word anywhere in it,
        # and words a leaflet uses. "MEDIFARMA LABORATORIES" keeps its place;
        # "breathing problems. There can also alcohol and" does not.
        and not (LEAFLET_WORDS.search(part) and not COMPANY_WORDS.search(part))
    ]
    return "; ".join(dict.fromkeys(kept))


ADDRESS_WORDS = re.compile(r"\b(?:park|estate|zone|road|street|avenue|block|floor|building|box|area|prefecture)\b", re.IGNORECASE)
LEGAL_FORMS = re.compile(r"\b(?:ltd|limited|plc|inc|llc|gmbh|s\.?\s?a|b\.?v|n\.?v|s\.?p\.?a|s\.?r\.?l|ag|kg|sas)\b\.?", re.IGNORECASE)
MANUFACTURED_BY = re.compile(
    r"manufactured by\s*:?\s*(?P<names>.+?)(?=\bprocured\b|\brepackaged\b|\bdistributed\b|\bmarketed\b|\bPL\s*\d|\bthis leaflet\b|$)",
    re.IGNORECASE,
)


def clean_manufacturer(value: object) -> str:
    """The manufacturers' names, without the addresses leaflets write after them.

        "Sanofi Winthrop Industrie; 1 rue de la Vierge; Ambares et Lagrave; France"
            -> "Sanofi Winthrop Industrie"
        "... Manufactured by: Sanofi Winthrop Industrie, 1 rue de la Vierge, ...
         OR Genzyme Ireland Limited, IDA Industrial Park, ... Procured from ..."
            -> "Sanofi Winthrop Industrie; Genzyme Ireland Limited"
    """
    # The sentence is looked for before company cleaning, which drops a value
    # that long as prose.
    sentence = MANUFACTURED_BY.search(clean_text(value))
    text = clean_company(value)
    if sentence:
        names = [
            part.split(",")[0].strip(" .;")
            for part in re.split(r"\s+OR\s+|;\s*", sentence.group("names"))
        ]
        names = [name for name in names if COMPANY_WORDS.search(name) or len(name.split()) <= 5]
        return "; ".join(dict.fromkeys(name for name in names if name))
    if not text:
        return ""
    parts = [part.strip() for part in text.split(";") if part.strip()]
    if len(parts) < 2:
        return text
    # An address line is not a name: keep the parts that read as companies,
    # or the first part when none does. "Industrial Park Sapes" is a place.
    names = [
        part for part in parts
        if COMPANY_WORDS.search(part) and not (ADDRESS_WORDS.search(part) and not LEGAL_FORMS.search(part))
    ] or parts[:1]
    return "; ".join(dict.fromkeys(names))


# --- Status ----------------------------------------------------------------------

MARKETED = "Marketed"
AUTHORISED = "Authorised"
NOT_MARKETED = "Authorised, not marketed"
WITHDRAWN = "Withdrawn"
EXPIRED = "Expired"
REFUSED = "Refused"
EXPORT_ONLY = "Authorised for export only"
API_ONLY = "Approved as active ingredient (API)"

STATUS_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), label)
    for pattern, label in (
        (r"^application withdrawn$", "Application withdrawn"),
        (r"refused", REFUSED),
        (r"khusus ekspor|export only", EXPORT_ONLY),
        (r"mencabut produk", WITHDRAWN),
        (r"cancel|withdrawn|retired|discontinued|revoked|suspended|annul", WITHDRAWN),
        (r"expired|lapsed|istek", EXPIRED),
        (r"bulk drug / api", API_ONLY),
        (r"dormant", NOT_MARKETED),
        # Belgium lists pack states: any pack available means the product is sold.
        (r"^available\b", MARKETED),
        (r"not_commerciali|non commerciali|unavailable|not marketed", NOT_MARKETED),
        (r"commerciali|^marketed|^available", MARKETED),
        (r"authori[sz]ed|approved|registered|berlaku|included on artg|listed|"
         r"label available|active|^д$|^new$|^updated$|^no recent update$", AUTHORISED),
    )
)


def vendor_status(value: object) -> str:
    """One vocabulary for every registry's status, in English.

    The registry's own wording stays in the store; this is what is shown.
    Ireland's medicines.ie states when a label was last updated ("New",
    "No Recent Update"), which for a listed medicine means it is authorised.
    """
    text = clean_text(value)
    if not text:
        return ""
    for pattern, label in STATUS_RULES:
        if pattern.search(text):
            return label
    return text


# --- Dosage form ------------------------------------------------------------------

FRENCH_NOUNS = (
    ("poudre et solvant pour preparation injectable", "powder and solvent for solution for injection"),
    ("microgranule en comprime", "tablet"),
    ("gomme a macher", "chewing gum"),
    ("systeme de diffusion", "delivery system"),
    ("pate a sucer", "pastille"),
    ("pate pour application", "paste"),
    ("pate pour usage dentaire", "dental paste"),
    ("pansement adhesif", "plaster"),
    ("compresse impregne", "impregnated pad"),
    ("comprime a sucer", "lozenge"),
    ("comprime", "tablet"),
    ("gelule", "capsule"),
    ("dispositif", "device"),
    ("creme", "cream"),
    ("pommade", "ointment"),
    ("sirop", "syrup"),
    ("suppositoire", "suppository"),
    ("collyre", "eye drops"),
    ("collutoire", "oromucosal spray"),
    ("lyophilisat", "lyophilisate"),
    ("emplatre", "plaster"),
    ("ovule", "pessary"),
    ("mousse", "foam"),
    ("poudre", "powder"),
    ("pate", "paste"),
    ("gaz", "gas"),
)
FRENCH_MODIFIERS = (
    ("a liberation prolongee", "prolonged-release"),
    ("a liberation modifiee", "modified-release"),
    ("gastro-resistant", "gastro-resistant"),
    ("orodispersible", "orodispersible"),
    ("effervescent", "effervescent"),
    ("a croquer", "chewable"),
    ("dispersible", "dispersible"),
    ("pellicule", "film-coated"),
    ("enrobe", "coated"),
    ("medicamenteux", "medicated"),
)

FRENCH_FORMS = {
    "solution injectable": "solution for injection",
    "solution pour perfusion": "solution for infusion",
    "solution injectable ou pour perfusion": "solution for injection or infusion",
    "solution injectable pour perfusion": "solution for injection or infusion",
    "solution injectable pour perfusion ou": "solution for injection or infusion",
    "solution a diluer pour perfusion": "concentrate for solution for infusion",
    "solution injectable a liberation prolongee": "prolonged-release solution for injection",
    "solution buvable": "oral solution",
    "solution buvable en gouttes": "oral drops, solution",
    "solution pour application": "cutaneous solution",
    "solution pour pulverisation": "spray, solution",
    "solution pour inhalation": "inhalation solution",
    "solution pour inhalation par nebuliseur": "nebuliser solution",
    "solution pour bain de bouche": "mouthwash",
    "solution pour instillation": "solution for instillation",
    "solution pour dialyse peritoneale": "peritoneal dialysis solution",
    "solution et solution pour dialyse peritoneale": "peritoneal dialysis solution",
    "solution et solution pour perfusion": "solution for infusion",
    "solution et solution et emulsion pour perfusion": "emulsion and solution for infusion",
    "suspension injectable": "suspension for injection",
    "suspension buvable": "oral suspension",
    "suspension pour pulverisation": "spray, suspension",
    "suspension pour inhalation": "inhalation suspension",
    "suspension pour inhalation par nebuliseur": "nebuliser suspension",
    "poudre pour solution injectable": "powder for solution for injection",
    "poudre pour solution injectable ou pour perfusion": "powder for solution for injection or infusion",
    "poudre et solvant pour solution injectable": "powder and solvent for solution for injection",
    "poudre et solvant pour suspension injectable a liberation prolongee":
        "powder and solvent for prolonged-release suspension for injection",
    "poudre pour solution buvable": "powder for oral solution",
    "poudre pour suspension buvable": "powder for oral suspension",
    "emulsion pour perfusion": "emulsion for infusion",
    "capsule molle": "soft capsule",
    "gelule gastro-resistante": "gastro-resistant capsule, hard",
    "gel pour application": "gel",
    "granules": "granules",
    "granules enrobe": "coated granules",
    "granules pour suspension buvable": "granules for oral suspension",
    "granules pour solution buvable": "granules for oral solution",
    "film orodispersible": "orodispersible film",
    "solution": "solution",
    "suspension": "suspension",
    "gel": "gel",
    "pastille": "pastille",
    "capsule": "capsule",
}

OTHER_FORMS = {
    # Russia (GRLS), as english_text transliterates it
    "tablets": "tablet", "capsules": "capsule", "tablets prolonged-release": "prolonged-release tablet",
    "tablets, pokrytye film-coated": "film-coated tablet",
    "tablets, pokrytye film-coated i tablets": "film-coated tablet and tablet",
    "tablets prolongirovannogo deystviya, pokrytye film-coated": "prolonged-release film-coated tablet",
    "tablets s prolongirovannym vysvobozhdeniem, pokrytye film-coated": "prolonged-release film-coated tablet",
    "tablets, pokrytye sakharnoy obolochkoy": "sugar-coated tablet", "tablets dispergiruemye": "dispersible tablet",
    "tablets solutionimye": "soluble tablet", "solution dlya vnutrivennogo vvedeniya": "solution for intravenous use",
    "solution dlya infuziy": "solution for infusion", "solution dlya priema vnutr": "oral solution",
    "suspension dlya priema vnutr": "oral suspension", "gel dlya naruzhnogo primeneniya": "gel for external use",
    "powder dlya prigotovleniya solutiona dlya priema vnutr": "powder for oral solution",
    # Romania (ANMDMR), further abbreviations
    "pulb. de inhal. unidoza": "inhalation powder, single-dose", "pulb. de inhal.": "inhalation powder",
    "pulb. inhal.": "inhalation powder", "susp. de inhalat presurizata": "pressurised inhalation, suspension",
    "spray bucofaringian, sol.": "oromucosal spray, solution", "spray naz.,susp.": "nasal spray, suspension",
    "spray naz.,sol.": "nasal spray, solution",
    # Spain (CIMA), further
    "suspension para inhalacion en envase a presion": "pressurised inhalation, suspension",
    "capsula dura gastrorresistente": "gastro-resistant capsule, hard",
    # Indonesia (BPOM)
    "cairan kental": "viscous liquid", "cair": "liquid", "cairan oral": "oral liquid",
    "cairan obat luar": "liquid for external use", "krim": "cream", "kapsul": "capsule",
    "kapsul lunak": "soft capsule", "kapsul lepas tunda": "delayed-release capsule",
    "kaplet": "caplet", "kaplet salut selaput": "film-coated caplet", "kaplet kunyah": "chewable caplet",
    "tablet salut selaput": "film-coated tablet", "tablet salut enterik": "gastro-resistant tablet",
    "tablet salut gula": "sugar-coated tablet", "tablet kunyah": "chewable tablet",
    "tablet lepas lambat": "prolonged-release tablet", "infus": "infusion", "injeksi": "injection",
    "serbuk injeksi": "powder for injection", "serbuk injeksi liofilisasi": "lyophilised powder for injection",
    "serbuk": "powder", "serbuk tabur": "dusting powder", "serbuk kompak": "compact powder",
    "sirup": "syrup", "sirup kering": "powder for oral suspension", "suspensi": "suspension",
    "salep": "ointment", "tetes mata": "eye drops", "larutan": "solution",
    "larutan dialisa peritoneal": "peritoneal dialysis solution", "emulsi": "emulsion",
    "padat": "solid", "padatan": "solid", "setengah padat": "semi-solid", "pasta": "paste",
    # Spain (CIMA)
    "pastilla para chupar": "lozenge", "chicle medicamentoso": "medicated chewing gum",
    "aposito adhesivo medicamentoso": "medicated plaster",
    "concentrado para solucion para perfusion": "concentrate for solution for infusion",
    "liofilizado oral": "oral lyophilisate", "crema": "cream", "pomada": "ointment",
    "gotas oticas en solucion": "ear drops, solution", "gotas orales en solucion": "oral drops, solution",
    "gotas nasales en solucion": "nasal drops, solution", "espuma cutanea": "cutaneous foam",
    # Romania (ANMDMR)
    "liof. oral": "oral lyophilisate", "sol. cut.": "cutaneous solution",
    "guma medicamentoasa mast.": "medicated chewing gum", "emplastru medicamentos": "medicated plaster",
    "plasture transdermic": "transdermal patch", "pic. oft., sol.": "eye drops, solution",
    "pic. oft., susp.": "eye drops, suspension", "pic. nazale, sol.": "nasal drops, solution",
    "pic. oft., sol in flac. unidoza": "eye drops, solution in single-dose container",
    "spuma cut.": "cutaneous foam", "ovule": "pessary",
    # Vietnam (DAV)
    "vien nen bao phim": "film-coated tablet", "vien nen": "tablet", "vien nang cung": "hard capsule",
    "vien nang": "capsule", "vien nen sui bot": "effervescent tablet", "siro": "syrup",
    "thuoc bot sui bot": "effervescent powder",
}


def _fold(text: str) -> str:
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower().strip()


def _french_form(text: str) -> str:
    if text in FRENCH_FORMS:
        return FRENCH_FORMS[text]
    nouns = "|".join(re.escape(fr) for fr, _ in FRENCH_NOUNS)
    segments = []
    # Split only where another form starts: "comprime et comprime", not the
    # "ou" inside "a sucer ou a croquer".
    for segment in re.split(rf"\s+(?:et|ou)\s+(?=(?:{nouns}))", text):
        segment = re.sub(r"\((?:s|e|se|ve)\)", "", segment).strip()
        noun = next(((fr, en) for fr, en in FRENCH_NOUNS if segment.startswith(fr)), None)
        if noun is None:
            return ""
        rest = segment[len(noun[0]):]
        scored = bool(re.search(r"\b(?:quadri)?secable\b", rest))
        modifiers = [
            en for fr, en in FRENCH_MODIFIERS if re.search(rf"(?<![a-z-]){re.escape(fr)}", rest)
        ]
        phrase = " ".join([*modifiers, noun[1]])
        if scored:
            phrase += " (scored)"
        segments.append(phrase)
    unique = list(dict.fromkeys(segments))
    return " or ".join(unique) if " ou " in text else " and ".join(unique)


def english_dosage_form(value: object) -> str:
    """A dosage form in English, where the registry filed it in its own language."""
    text = clean_text(value)
    if not text:
        return ""
    # BPOM appends the strength: "KAPSUL; 75 mg".
    head = text.split(";")[0].strip()
    folded = _fold(head)
    folded = re.sub(r"^local-language registry value:\s*", "", folded)
    # GRLS lists one flavour per variant: "..., s aromatom limona", "[so vkusom ...]".
    folded = re.sub(r"\s*\[[^\]]*\]|,\s*(?:s|so) (?:aromatom|vkusom)\b.*$|,\s*[a-z]+(?:yy|aya)$", "", folded)
    translated = OTHER_FORMS.get(folded) or _french_form(folded) or head or text
    return translated[0].upper() + translated[1:]


# --- Dates ------------------------------------------------------------------------

def display_date(value: object) -> str:
    """ISO 8601 (YYYY-MM-DD) wherever the registry's format can be read."""
    text = clean_text(value)
    parsed = parse_regulatory_date(text)
    return parsed.isoformat() if parsed else text


# --- Row --------------------------------------------------------------------------

TEXT_FIELDS = (
    "substance", "active_substance", "active_substances", "source_substance", "product",
    "strength", "pack_size", "therapeutic_category", "registration_number", "route",
)
COMPANY_FIELDS = (
    "company", "commercial_company", "brand_owner", "labeler_name", "sponsor", "applicant",
    "mah", "ma_holder", "manufacturer_name",
)
DATE_FIELDS = ("registration_date", "expiry_date", "marketing_start_date")


def vendor_row(row: dict[str, Any]) -> dict[str, Any]:
    """A row as it is shown: clean text, English forms, one status vocabulary, ISO dates."""
    shown = dict(row)
    for field in TEXT_FIELDS:
        if field in shown and isinstance(shown[field], str):
            shown[field] = clean_text(shown[field])
    for field in COMPANY_FIELDS:
        if field in shown and isinstance(shown[field], str):
            shown[field] = (clean_manufacturer if field == "manufacturer_name" else clean_company)(shown[field])
    if "atc_code" in shown:
        shown["atc_code"] = clean_atc_code(shown["atc_code"])
    if "dosage_form" in shown:
        shown["dosage_form"] = english_dosage_form(shown["dosage_form"])
    if "status" in shown:
        shown["status"] = vendor_status(shown["status"])
    for field in DATE_FIELDS:
        if field in shown and isinstance(shown[field], str):
            shown[field] = display_date(shown[field])
    return shown
