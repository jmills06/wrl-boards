#!/usr/bin/env python3
"""
build_dxcc.py — generate data/dxcc.json, the ADIF entity lookup table.

The WRL API returns `dxcc` as an ADIF entity code and deliberately does not
return country, continent or flag, because all three are derivable from it.
The boards need country names (recent activity panel) and continents (career
stat tile), so we commit a local lookup table.

Run this once. The output is committed; the boards and collector read the
JSON, never this script. Only the derived table is committed — the sources
are third-party files of unclear licence and are not redistributed here.

    pip download --no-deps --dest /tmp/src hamtools pyhamtools
    tar xzf /tmp/src/hamtools-0.3.tar.gz -C /tmp/src
    tar xzf /tmp/src/pyhamtools-0.13.0.tar.gz -C /tmp/src
    python build_dxcc.py \
        --cty /tmp/src/hamtools-0.3/hamtools/ctydat/cty.dat \
        --mapping /tmp/src/pyhamtools-0.13.0/pyhamtools/countryfilemapping.json

Two sources are combined, because neither alone is sufficient:

  cty.dat (bundled in hamtools) gives country name -> continent, lat, lon,
      but carries no ADIF entity codes.
  countryfilemapping.json (bundled in pyhamtools) gives country name ->
      ADIF entity code, but no continent.

They join on country name. Generated from hamtools 0.3 and pyhamtools 0.13.0.

The bundled cty.dat is dated December 2013. That is old but adequate here:
we use only entity codes, continents and approximate centres, and no DXCC
entity has been added since Kosovo in 2018. The two post-2013 renames
(Eswatini, North Macedonia) are corrected below. Prefix assignments in that
file ARE stale, which is why nothing downstream uses them for identification
— the API gives us the entity code directly.

Three source defects are corrected here rather than in the collector:

  1. cty.dat stores longitude WEST-POSITIVE. Every longitude is negated to
     the normal east-positive convention. Getting this wrong mirrors the
     entire world map.
  2. cty.dat files Antarctica under continent SA. It is overridden to AN.
  3. pyhamtools splits names containing "&" and maps both halves to a code.
     Usually harmless, but 'Trindade ' is mapped to 90 (Trinidad & Tobago)
     when Trindade & Martim Vaz is 273. We therefore iterate over cty.dat
     entities and look UP their code, never the reverse, so the truncated
     halves are never consulted.
"""

import argparse
import json
import os
import sys
from collections import defaultdict

OUT_PATH = os.path.join("data", "dxcc.json")

# cty.dat names with no exact counterpart in the ADIF mapping.
CTY_TO_ADIF = {
    "Swaziland": 468,                 # renamed Eswatini
    "Macedonia": 502,                 # renamed North Macedonia
    "Kosovo": 522,
    "Auckland & Campbell Is.": 16,    # listed as N.Z. Subantarctic Is.
    "Tristan da Cunha & Gough": 274,
}

# cty.dat entries that are not DXCC entities in their own right.
CTY_SKIP = {"Vienna Intl Ctr"}

# Where several cty.dat names share one code, or where cty.dat is out of date,
# pick the name a display board should show.
NAME_OVERRIDE = {
    248: "Italy",             # cty.dat also lists Sicily, African Italy
    279: "Scotland",          # cty.dat also lists Shetland and Fair Isle
    390: "Turkey",            # cty.dat splits Asiatic / European Turkey
    266: "Norway",            # cty.dat also lists Bear Island
    468: "Eswatini",
    502: "North Macedonia",
    # Display name. These boards are read at a glance from across a room;
    # the formal ADIF name is not what a reader is scanning for. Kept to
    # entities common enough for the difference to be noticed.
    230: "Germany",
}

CONTINENT_OVERRIDE = {
    13: "AN",                 # Antarctica; cty.dat files it under SA
}

VALID_CONTINENTS = {"AF", "AN", "AS", "EU", "NA", "OC", "SA"}


def find(explicit, *relative):
    """Use the given path, else look for the file inside an installed package."""
    if explicit:
        if not os.path.exists(explicit):
            raise SystemExit(f"no such file: {explicit}")
        return explicit
    for base in sys.path:
        if base:
            p = os.path.join(base, *relative)
            if os.path.exists(p):
                return p
    raise SystemExit(
        f"could not find {'/'.join(relative)}.\n"
        f"Pass --cty and --mapping explicitly; see the module docstring for\n"
        f"how to obtain the source files.")


def parse_cty(path):
    """cty.dat: 'Name: CQ: ITU: Cont: Lat: Lon: GMT: Prefix:' then aliases."""
    out = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    for block in text.split(";"):
        block = block.strip()
        if not block or ":" not in block:
            continue
        head = block.split("\n")[0]
        parts = [p.strip() for p in head.split(":")]
        if len(parts) < 8:
            continue
        name, cq, itu, cont, lat, lon, gmt, prefix = parts[:8]
        # A leading '*' marks a row cty.dat does not treat as its own DXCC
        # entity. It is a marker, not part of the name or prefix.
        name = name.lstrip("*").strip()
        prefix = prefix.lstrip("*").strip()
        try:
            lat, lon = float(lat), float(lon)
        except ValueError:
            continue
        out[name] = {
            "continent": cont,
            "lat": round(lat, 2) + 0.0,
            "lon": round(-lon, 2) + 0.0,   # cty.dat is west-positive; normalise
            "prefix": prefix,
        }
    return out


def main():
    ap = argparse.ArgumentParser(description="Generate data/dxcc.json.")
    ap.add_argument("--cty", help="path to cty.dat")
    ap.add_argument("--mapping", help="path to countryfilemapping.json")
    args = ap.parse_args()

    cty_path = find(args.cty, "hamtools", "ctydat", "cty.dat")
    map_path = find(args.mapping, "pyhamtools", "countryfilemapping.json")
    print(f"cty.dat              : {cty_path}")
    print(f"mapping              : {map_path}")

    cty = parse_cty(cty_path)
    with open(map_path, encoding="utf-8") as fh:
        name_to_code = json.load(fh)

    print(f"cty.dat entities     : {len(cty)}")
    print(f"ADIF name -> code    : {len(name_to_code)}")

    entities = {}
    claimed = defaultdict(list)
    unresolved = []

    for name, rec in sorted(cty.items()):
        if name in CTY_SKIP:
            continue
        code = name_to_code.get(name, CTY_TO_ADIF.get(name))
        if code is None or code == 0:
            unresolved.append(name)
            continue
        claimed[code].append(name)

        cont = CONTINENT_OVERRIDE.get(code, rec["continent"])
        display = NAME_OVERRIDE.get(code, name)

        prev = entities.get(code)
        if prev and NAME_OVERRIDE.get(code) != name and prev["name"] != display:
            # Several cty.dat rows share a code (Italy/Sicily, Turkey halves).
            # Keep the shortest name unless an override already decided it.
            if code in NAME_OVERRIDE or len(prev["name"]) <= len(display):
                continue
        entities[code] = {
            "name": display,
            "continent": cont,
            "lat": rec["lat"],
            "lon": rec["lon"],
            "prefix": rec["prefix"],
        }

    # Codes the ADIF mapping knows but cty.dat has no row for: deleted or
    # very rare entities. Record the name so a lookup never returns nothing.
    for name, code in sorted(name_to_code.items()):
        if code and code not in entities and not name.endswith(" "):
            entities[code] = {"name": name, "continent": None,
                              "lat": None, "lon": None, "prefix": None}
            unresolved.append(f"{name} (code {code}, no cty.dat row)")

    # ---- validation -----------------------------------------------------
    problems = []
    for code, e in entities.items():
        if e["continent"] is not None and e["continent"] not in VALID_CONTINENTS:
            problems.append(f"{code} {e['name']}: bad continent {e['continent']!r}")
        if e["lat"] is not None and not (-90 <= e["lat"] <= 90):
            problems.append(f"{code} {e['name']}: lat out of range {e['lat']}")
        if e["lon"] is not None and not (-180 <= e["lon"] <= 180):
            problems.append(f"{code} {e['name']}: lon out of range {e['lon']}")
        if not e["name"]:
            problems.append(f"{code}: empty name")

    collisions = {c: ns for c, ns in claimed.items() if len(ns) > 1}

    print(f"\nentities written     : {len(entities)}")
    print(f"with continent       : {sum(1 for e in entities.values() if e['continent'])}")
    print(f"with coordinates     : {sum(1 for e in entities.values() if e['lat'] is not None)}")
    print(f"continents present   : {sorted({e['continent'] for e in entities.values() if e['continent']})}")

    if collisions:
        print(f"\nshared codes (expected — same entity, several cty.dat rows):")
        for c, ns in sorted(collisions.items()):
            print(f"  {c:4} {entities[c]['name']:24} <- {ns}")

    if unresolved:
        print(f"\nno cty.dat geography ({len(unresolved)}):")
        for u in unresolved[:12]:
            print(f"  - {u}")
        if len(unresolved) > 12:
            print(f"  ... and {len(unresolved) - 12} more")

    if problems:
        print("\nVALIDATION FAILED:")
        for p in problems:
            print(f"  ! {p}")
        return 1

    payload = {
        "meta": {
            "description": "ADIF DXCC entity code -> country, continent, "
                           "approximate entity centre",
            "sources": ["cty.dat (hamtools 0.3)",
                        "countryfilemapping.json (pyhamtools 0.13.0)"],
            "generated_by": "build_dxcc.py",
            "longitude": "east-positive (cty.dat's west-positive values negated)",
            "entity_count": len(entities),
        },
        "entities": {str(k): entities[k] for k in sorted(entities)},
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=False, ensure_ascii=False)
        fh.write("\n")
    print(f"\nwrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
