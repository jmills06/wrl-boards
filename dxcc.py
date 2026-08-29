"""
dxcc.py — ADIF entity code lookup, backed by data/dxcc.json.

The WRL API returns `dxcc` as a bare ADIF entity code and returns no country,
continent or flag. This turns the code into the values the boards display.

    import dxcc
    dxcc.country(291)      -> 'United States'
    dxcc.continent(291)    -> 'NA'
    dxcc.center(291)       -> (37.53, -91.67)

Every lookup tolerates a missing, null or unknown code, because `dxcc` is
null on roughly 6% of contacts (enrichment has not run on them) and the
boards must render anyway. `country()` returns None rather than raising or
inventing a placeholder — the caller decides what a blank looks like.

The table is loaded once, lazily, on first lookup.
"""

import json
import os

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "dxcc.json")

# WAC (Worked All Continents) recognises six. Antarctica is a DXCC entity but
# not a WAC continent, so a "continents worked" total should normally be out
# of six. Kept here so the boards do not each invent their own denominator.
WAC_CONTINENTS = ("AF", "AS", "EU", "NA", "OC", "SA")
ALL_CONTINENTS = WAC_CONTINENTS + ("AN",)

# Slash segments that indicate operating status, not location.
CALL_SUFFIXES = {
    "P", "M", "MM", "AM", "QRP", "A", "B", "R", "LH", "J", "N", "T",
    "PORTABLE", "MOBILE", "BEACON", "SOTA", "POTA", "QRPP",
}

_TABLE = None
_PREFIXES = None


def _load():
    global _TABLE, _PREFIXES
    if _TABLE is None:
        with open(DATA_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        _TABLE = raw["entities"]
        _PREFIXES = raw.get("prefixes", {})
    return _TABLE


def _prefix_table():
    _load()
    return _PREFIXES


def entity_from_call(call):
    """Best-guess ADIF entity code from a callsign. None if no match.

    A FALLBACK ONLY. The API's `dxcc` field is authoritative when present,
    but enrichment lags, so the newest contacts — precisely the ones the
    recent board shows — routinely arrive with dxcc null.

    Slash handling: among the segments, operating-status suffixes (/P, /M,
    /QRP) are dropped, and the SHORTEST remaining segment is taken as the
    location. That resolves both conventions: DL/W1AW is Germany, and
    W1AW/VE3 is Canada.
    """
    if not call or not str(call).strip():
        return None
    table = _prefix_table()
    if not table:
        return None

    raw = str(call).strip().upper()
    # Every amateur callsign carries a digit. Without this the matcher happily
    # resolves arbitrary text, since almost any letter pair is some prefix.
    if not (3 <= len(raw) <= 16) or not any(c.isdigit() for c in raw):
        return None
    if not all(c.isalnum() or c == "/" for c in raw):
        return None

    segments = [s for s in raw.split("/") if s]
    if not segments:
        return None
    meaningful = [s for s in segments
                  if s not in CALL_SUFFIXES and not s.isdigit()]
    if not meaningful:
        meaningful = segments
    meaningful.sort(key=len)
    candidate = meaningful[0]

    # Longest matching prefix wins: VE3 must beat VE, and 3B8 must beat 3B.
    for n in range(len(candidate), 0, -1):
        code = table.get(candidate[:n])
        if code is not None:
            return code
    return None


def entity(code):
    """Full record for an ADIF entity code, or None if unknown/null."""
    if code is None or code == "":
        return None
    try:
        key = str(int(code))
    except (TypeError, ValueError):
        return None
    return _load().get(key)


def country(code, default=None):
    e = entity(code)
    return e["name"] if e else default


def country_for(code, call, default=None):
    """Country name from the entity code, falling back to the callsign."""
    name = country(code)
    if name:
        return name
    return country(entity_from_call(call), default=default)


def continent(code, default=None):
    e = entity(code)
    return e["continent"] if e and e["continent"] else default


def center(code):
    """Approximate (lat, lon) of the entity, east-positive. None if unknown.

    This is an entity centroid, not a station location. It is only good
    enough for a rough map position when a contact has no gridsquare.
    """
    e = entity(code)
    if not e or e.get("lat") is None or e.get("lon") is None:
        return None
    return (e["lat"], e["lon"])


def continents_worked(codes, include_antarctica=False):
    """Distinct continents across an iterable of entity codes.

    Nulls and unknown codes are skipped rather than counted as a continent.
    """
    valid = set(ALL_CONTINENTS if include_antarctica else WAC_CONTINENTS)
    return {c for c in (continent(x) for x in codes) if c in valid}


def count():
    return len(_load())
