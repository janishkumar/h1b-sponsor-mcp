"""Employer-name normalisation, shared by the build and by query matching.

Kept dependency-free so the runtime server can use it without the build extras.
"""

from __future__ import annotations

import re

LEGAL_SUFFIXES = {
    "INC", "INCORPORATED", "LLC", "LLP", "LP", "LTD", "LIMITED", "CO", "COMPANY", "CORP",
    "CORPORATION", "PC", "PLLC", "PLC", "NA", "PA", "GMBH", "AG", "SA", "BV", "LLLP",
}
_DBA = re.compile(r"\b(D\s*/?\s*B\s*/?\s*A|DOING BUSINESS AS|AKA)\b.*$")
_PUNCT = re.compile(r"[^A-Z0-9 ]+")
_SPACES = re.compile(r"\s+")

# Common names people type that differ from how the company files.
BRAND_ALIASES = {
    "FACEBOOK": "META PLATFORMS",
    "META": "META PLATFORMS",
    "ALPHABET": "GOOGLE",
    "AWS": "AMAZON WEB SERVICES",
    "JP MORGAN": "JPMORGAN CHASE",
    "J P MORGAN": "JPMORGAN CHASE",
    "CHASE": "JPMORGAN CHASE",
    "GOLDMAN": "GOLDMAN SACHS",
    "MORGAN STANLEY": "MORGAN STANLEY",
    "TCS": "TATA CONSULTANCY SERVICES",
    "EY": "ERNST AND YOUNG",
    "PWC": "PRICEWATERHOUSECOOPERS",
    "BCG": "BOSTON CONSULTING GROUP",
}


def normalise_employer(name: str | None) -> str:
    if not isinstance(name, str) or not name:
        return ""
    s = str(name).upper().replace("&", " AND ")
    s = _DBA.sub("", s)
    s = s.replace(".COM", " COM")
    s = _PUNCT.sub(" ", s)
    tokens = _SPACES.sub(" ", s).strip().split(" ")
    if tokens and tokens[0] == "THE" and len(tokens) > 1:
        tokens = tokens[1:]
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(t for t in tokens if t)


def query_key(query: str) -> str:
    """Normalise what a user typed, applying brand aliases."""
    key = normalise_employer(query)
    return BRAND_ALIASES.get(key, key)


def clean_fein(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) != 9 or len(set(digits)) == 1:
        return None
    return digits
