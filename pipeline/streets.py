"""Street names, so the same street matches across permits, agendas, and 311.

The city writes "33 MAPLEWOOD AV, GLOUCESTER", agendas write "33 Maplewood
Avenue", and SeeClickFix writes "33 Maplewood Ave Gloucester MA 01930". All of
them become the key "MAPLEWOOD AVE", shown as "Maplewood Avenue".
"""

from __future__ import annotations

import re

from pipeline.seeclickfix import short_address

# Street-type words as written in city records, agendas, and 311, mapped to one
# key form. The site's street lookup reads this same table.
SUFFIXES = {
    "ST": "ST", "STREET": "ST", "RD": "RD", "ROAD": "RD", "AV": "AVE", "AVE": "AVE", "AVENUE": "AVE",
    "LN": "LN", "LANE": "LN", "DR": "DR", "DRIVE": "DR", "CT": "CT", "COURT": "CT", "WY": "WAY", "WAY": "WAY",
    "HT": "HTS", "HTS": "HTS", "HEIGHTS": "HTS", "SQ": "SQ", "SQUARE": "SQ", "TR": "TER", "TER": "TER",
    "TERRACE": "TER", "BV": "BLVD", "BLVD": "BLVD", "BOULEVARD": "BLVD", "CR": "CIR", "CIR": "CIR",
    "CIRCLE": "CIR", "PL": "PL", "PLACE": "PL", "LP": "LOOP", "LOOP": "LOOP", "PT": "PT", "POINT": "PT",
    "HWY": "HWY", "HIGHWAY": "HWY", "PKWY": "PKWY", "PARKWAY": "PKWY", "CV": "COVE", "COVE": "COVE",
    "RDG": "RDG", "RIDGE": "RDG",
}
FULL_NAMES = {
    "ST": "Street", "RD": "Road", "AVE": "Avenue", "LN": "Lane", "DR": "Drive", "CT": "Court", "WAY": "Way",
    "HTS": "Heights", "SQ": "Square", "TER": "Terrace", "BLVD": "Boulevard", "CIR": "Circle", "PL": "Place",
    "LOOP": "Loop", "PT": "Point", "HWY": "Highway", "PKWY": "Parkway", "COVE": "Cove", "RDG": "Ridge",
}
# House numbers at the start: "33", "105R", "62-66", "62 62R 64".
HOUSE_NUMBER = re.compile(r"^\s*#?\d+[A-Z]?(?:\s*[-–/&]?\s*\d+[A-Z]?)*\s+")
MONTHS = {"January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"}
# A street address in running text, such as an agenda: "38 Pleasant Street".
ADDRESS_IN_TEXT = re.compile(
    r"\b\d{1,5}[A-Za-z]?(?:\s*[-–&]\s*\d{1,5}[A-Za-z]?)?\s+(?:[A-Z][A-Za-z'’]+\.?\s+){1,3}"
    r"(?:" + "|".join(sorted({s.title() for s in SUFFIXES} | {s for s in SUFFIXES if len(s) > 2}, key=len, reverse=True))
    + r")\b"
)


def street_key(part: str) -> str | None:
    """'33 MAPLEWOOD AV' -> 'MAPLEWOOD AVE'. None if it doesn't end in a street type."""
    text = re.sub(r"[.,’']", "", part.upper())
    text = re.sub(r"\s+(?:UNIT|APT|#)\s*\S+$", "", text)
    words = HOUSE_NUMBER.sub("", text).split()
    if len(words) < 2 or words[-1] not in SUFFIXES:
        return None
    return " ".join(words[:-1] + [SUFFIXES[words[-1]]])


def street_keys(address: str, town: str) -> list[str]:
    """Every street in an address; an intersection has two. town is the town's
    name, which addresses from the city and SeeClickFix end with."""
    short = short_address(address or "", town)
    parts = re.split(r"\s+(?:&|and|at)\s+|/", short, flags=re.I)
    return list(dict.fromkeys(k for p in parts if (k := street_key(p))))


def street_name(key: str) -> str:
    """'MAPLEWOOD AVE' -> 'Maplewood Avenue'."""
    *name, suffix = key.split()
    return " ".join([w.capitalize() for w in name] + [FULL_NAMES.get(suffix, suffix.capitalize())])


def addresses_in(text: str) -> list[str]:
    """Street addresses mentioned in a document, in order, without repeats."""
    found = (m.group(0) for m in ADDRESS_IN_TEXT.finditer(text or ""))
    # "15 September Road" in "2026 15 September Road..." is a date, not an address.
    return list(dict.fromkeys(a for a in found if a.split()[1] not in MONTHS))
