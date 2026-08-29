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

_TABLE = None


def _load():
    global _TABLE
    if _TABLE is None:
        with open(DATA_PATH, encoding="utf-8") as fh:
            _TABLE = json.load(fh)["entities"]
    return _TABLE


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
