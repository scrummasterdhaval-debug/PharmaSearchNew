"""Read the dates registries publish, in whichever form each one writes them.

The store keeps every date as the registry wrote it, and the registries do not
agree. Sorting those strings as text put 31/12/2009 at the top of a
newest-first list, because "3" outranks "2". These are the shapes found across
every stored row:

    2024-08-23                  DAV Vietnam and most ISO sources
    2025-09-15T10:56:55Z        MHRA
    2018-04-03T09:47:15+00:00   Ireland medicines.ie
    11/11/2012                  France BDPM, EMA -- day first*
    17 Dec, 2013                Hong Kong Drug Office
    22-DEC-2020                 CDSCO India
    2025-Apr-07                 CDSCO India
    22.12.2023                  GRLS Russia -- day first
    20261231                    FDA expiry dates

* Day first, not month first: 6,126 of these rows have a first part above 12
  and none has a second part above 12.
"""

from __future__ import annotations

import re
from datetime import date

_MONTHS = {
    name: number
    for number, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for name in names
}

_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ].*)?$")
_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DAY_FIRST = re.compile(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{4})$")
_DAY_MONTH_NAME = re.compile(r"^(\d{1,2})[ \-]([A-Za-z]{3,9})\.?,?[ \-](\d{4})$")
_YEAR_MONTH_NAME = re.compile(r"^(\d{4})-([A-Za-z]{3,9})-(\d{1,2})$")


def _make(year: str, month: object, day: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        return None


def parse_regulatory_date(value: object) -> date | None:
    """The calendar date a registry's date string names, or None."""
    text = str(value or "").strip()
    if not text:
        return None
    match = _ISO.match(text)
    if match:
        return _make(*match.groups())
    match = _COMPACT.match(text)
    if match:
        return _make(*match.groups())
    match = _DAY_FIRST.match(text)
    if match:
        day, month, year = match.groups()
        return _make(year, month, day)
    match = _DAY_MONTH_NAME.match(text)
    if match:
        day, month_name, year = match.groups()
        return _make(year, _MONTHS.get(month_name.lower()), day)
    match = _YEAR_MONTH_NAME.match(text)
    if match:
        year, month_name, day = match.groups()
        return _make(year, _MONTHS.get(month_name.lower()), day)
    return None
