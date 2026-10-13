"""The salt or ester part of a substance name, and the molecule without it.

Registers disagree on how they file a salt. Medsafe lists "Sevelamer" and names
the carbonate only in the composition; Greece files one product under
"SEVELAMER" and its twin under "SEVELAMER CARBONATE". A connector searches by
the molecule and lets the row's own ingredient text decide the salt.
"""
import re

SALT_WORDS = (
    "acetate", "anhydrous", "arginine", "benzoate", "besilate", "besylate", "bitartrate",
    "bromide", "calcium", "carbonate", "chloride", "citrate", "dihydrate", "dipropionate",
    "disodium", "erbumine", "ethyl", "fumarate", "hemifumarate", "hemihydrate", "hyclate",
    "hydrate", "hydrobromide", "hydrochloride", "hydrogen", "lactate", "lysine", "magnesium",
    "maleate", "malate", "mesilate", "mesylate", "meglumine", "monohydrate", "nitrate",
    "oxalate", "pamoate", "phosphate", "potassium", "propionate", "sodium", "succinate",
    "sulfate", "sulphate", "tartrate", "tosilate", "tosylate", "trihydrate", "trometamol",
    "valerate", "zinc",
)
_SALT = re.compile(r"\b(?:" + "|".join(SALT_WORDS) + r")\b", re.IGNORECASE)


def base_name(name: str) -> str:
    """ "sevelamer carbonate" -> "sevelamer"; "atorvastatin calcium trihydrate" -> "atorvastatin"."""
    stripped = " ".join(_SALT.sub(" ", str(name or "")).split())
    return stripped or " ".join(str(name or "").split())


def salts_in(name: str) -> list[str]:
    """The salt words a name carries, lower-case, in order."""
    return [match.group(0).lower() for match in _SALT.finditer(str(name or ""))]
