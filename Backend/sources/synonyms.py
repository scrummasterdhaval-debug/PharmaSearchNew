"""The other names a registry may have filed the same molecule under.

A search reaches the US registers under the name the US adopted, and the rest
of the world under the INN. They differ for a long tail of very ordinary
molecules -- salbutamol is albuterol, adrenaline is epinephrine, glibenclamide
is glyburide -- and a search that sends only what was typed comes back from
openFDA empty, which reads as a broken connector rather than a naming
difference.

Two shapes of alias, kept apart because they do not behave the same way:

* ``EQUIVALENT_NAMES`` are names for one substance, so membership is mutual.
  Searching any of them should search all of them.
* ``COMBINATION_PARTS`` map a combination to the molecules in it, which is a
  one-way relation. A search for a combination may fall back to its
  components, but a search for paracetamol must not drag in every combination
  that happens to contain it.
"""


# Names for the same substance. INN first where they differ, then the US
# adopted name, then any spelling a register still files under. Every group is
# expanded in both directions below, so each pair is written once.
EQUIVALENT_NAMES = [
    ("paracetamol", "acetaminophen"),
    ("salbutamol", "albuterol"),
    ("adrenaline", "epinephrine"),
    ("noradrenaline", "norepinephrine"),
    ("glibenclamide", "glyburide"),
    ("rifampicin", "rifampin"),
    ("ciclosporin", "cyclosporine", "cyclosporin"),
    ("pethidine", "meperidine"),
    ("lidocaine", "lignocaine"),
    ("furosemide", "frusemide"),
    ("beclometasone", "beclomethasone"),
    ("chlorphenamine", "chlorpheniramine"),
    ("indometacin", "indomethacin"),
    ("hydroxycarbamide", "hydroxyurea"),
    ("isoprenaline", "isoproterenol"),
    ("colecalciferol", "cholecalciferol"),
    ("phytomenadione", "phytonadione"),
    ("clomifene", "clomiphene"),
    ("chlortalidone", "chlorthalidone"),
    ("glyceryl trinitrate", "nitroglycerin"),
    ("sodium cromoglicate", "cromolyn sodium"),
    ("methylthioninium chloride", "methylene blue"),
    ("dexamfetamine", "dextroamphetamine"),
    ("amfetamine", "amphetamine"),
    ("cefalexin", "cephalexin"),
    ("cefradine", "cephradine"),
    ("benzylpenicillin", "penicillin g"),
    ("phenoxymethylpenicillin", "penicillin v"),
    ("phenobarbital", "phenobarbitone"),
    ("thiopental", "thiopentone"),
    ("bendroflumethiazide", "bendrofluazide"),
    ("pentoxifylline", "oxpentifylline"),
    ("guaifenesin", "guaiphenesin"),
    ("dosulepin", "dothiepin"),
    ("trihexyphenidyl", "benzhexol"),
    ("levomepromazine", "methotrimeprazine"),
    ("procaine benzylpenicillin", "penicillin g procaine"),
    ("aciclovir", "acyclovir"),
    ("amoxicillin", "amoxycillin"),
    ("cefuroxime axetil", "cefuroxime"),
    # The British "ph"/"sulph" spellings several registers still file under.
    ("sulfasalazine", "sulphasalazine"),
    ("sulfamethoxazole", "sulphamethoxazole"),
    ("sulfadiazine", "sulphadiazine"),
    ("oestradiol", "estradiol"),
    ("oestriol", "estriol"),
    ("oestrone", "estrone"),
    ("oestrogens", "estrogens"),
]

# A combination and the molecules it is made of. One-way: the combination may
# fall back to its parts, never the reverse.
COMBINATION_PARTS = {
    "lidocaine prilocaine": ["lidocaine", "prilocaine"],
    "amoxicillin clavulanic acid": ["amoxicillin", "clavulanic acid"],
    "co-amoxiclav": ["amoxicillin clavulanic acid", "amoxicillin", "clavulanic acid"],
    "co-trimoxazole": ["sulfamethoxazole trimethoprim", "sulfamethoxazole", "trimethoprim"],
    "sulfamethoxazole trimethoprim": ["sulfamethoxazole", "trimethoprim"],
}


def _orderings(name: str) -> list[str]:
    """The ways a register may have written a two-part combination."""
    parts = name.split()
    if len(parts) != 2:
        return [name]
    first, second = parts
    return [
        f"{first} {second}", f"{second} {first}",
        f"{first}+{second}", f"{second}+{first}",
        f"{first} and {second}", f"{second} and {first}",
    ]


def _build() -> dict[str, list[str]]:
    table: dict[str, list[str]] = {}

    def add(key: str, values: list[str]) -> None:
        bucket = table.setdefault(key, [])
        for value in values:
            if value != key and value not in bucket:
                bucket.append(value)

    for group in EQUIVALENT_NAMES:
        for name in group:
            add(name, [other for other in group if other != name])

    for combination, parts in COMBINATION_PARTS.items():
        # Every spelling of the combination reaches the same components, and
        # the other spellings too, since registers disagree on the order.
        spellings = _orderings(combination)
        for spelling in spellings:
            add(spelling, [s for s in spellings if s != spelling] + list(parts))

    return table


SUBSTANCE_SYNONYMS = _build()


def _strip_latin_accents(text):
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


COMBINATION_SEPARATOR = r"\s*(?:\+|/|,|;|&|\band\b|\bwith\b)\s*"


def combination_parts(substance):
    """The molecules a typed combination names: "paracetamol +Caffeine" -> two.

    One name is not a combination, and neither is a name the table below
    already expands ("co-amoxiclav").
    """
    import re

    parts = [
        " ".join(part.split()).lower()
        for part in re.split(COMBINATION_SEPARATOR, str(substance or ""), flags=re.IGNORECASE)
        if part.strip()
    ]
    return list(dict.fromkeys(parts)) if len(parts) > 1 else []


def names_for(molecule):
    """A molecule and the other names a register may file it under."""
    names = [molecule]
    for synonym in SUBSTANCE_SYNONYMS.get(molecule.lower(), []):
        if synonym.lower() not in {name.lower() for name in names}:
            names.append(synonym)
    return names


def get_substance_search_terms(substance):
    normalized_parts = [
        part
        for part in substance.strip().lower().replace("+", " ").split()
        if part not in {"and", "&"}
    ]
    normalized = " ".join(normalized_parts)
    terms = [substance]
    # A name typed with accents -- "céfalexine", "ibuprofène" -- is the French
    # INN. Registries index the plain spelling, and the English INN drops the
    # final "e", so both are searched as well.
    folded = _strip_latin_accents(substance.strip())
    if folded != substance.strip():
        terms.append(folded)
        if len(folded) > 5 and folded.lower().endswith("e"):
            terms.append(folded[:-1])
        normalized = _strip_latin_accents(normalized)
    synonyms = list(SUBSTANCE_SYNONYMS.get(normalized, []))
    synonyms.extend(SUBSTANCE_SYNONYMS.get(normalized.rstrip("e"), []))
    synonyms.extend(SUBSTANCE_SYNONYMS.get(substance.strip().lower(), []))
    for synonym in synonyms:
        if synonym.lower() not in {term.lower() for term in terms}:
            terms.append(synonym)

    # A typed combination -- "paracetamol +Caffeine" -- is sent as each
    # register understands it. Registers that match every word of a query
    # (FDA, Health Canada, Italy, Brazil) find the combination under the other
    # names: "acetaminophen caffeine". Registers that search one molecule at a
    # time need the molecules separately. Results are kept only if they
    # contain every molecule, so single-molecule products do not come back.
    parts = combination_parts(folded)
    if parts:
        import itertools

        spellings = [names_for(part) for part in parts]
        for combination in itertools.islice(itertools.product(*spellings), 6):
            joined = " ".join(combination)
            if joined.lower() not in {term.lower() for term in terms}:
                terms.append(joined)
        for names in spellings:
            for name in names:
                if name.lower() not in {term.lower() for term in terms}:
                    terms.append(name)
    return terms
