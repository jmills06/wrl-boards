#!/usr/bin/env python3
"""
discover.py — read-only diagnostic for the World Radio League API.

Answers the open questions in the handoff before we design the collector:
  1. Who are we, which logbook is default, what are the rate limits?
  2. What logbooks exist, and are any locked?
  3. How complete are the fields we want to build boards on?
  4. Is `distance` miles or kilometres?

This script WRITES NOTHING. It only GETs and prints.

Usage:
    set WRL_API_KEY=wrl_live_...      (Windows)
    export WRL_API_KEY=wrl_live_...   (bash)
    py discover.py
"""

import json
import math
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import truststore

truststore.inject_into_ssl()  # must happen before requests builds its SSL context

import requests  # noqa: E402  (deliberately after inject_into_ssl)

# ---------------------------------------------------------------- constants

BASE_URL = "https://api.worldradioleague.com"
TIMEOUT = (10, 60)          # (connect, read) — never a bare float
PAGE_SIZE = 100
SAMPLE_SIZE = 200           # first N contacts to profile
PAGE_SLEEP = 0.5            # courtesy pause between pages
MAX_RETRIES = 3

# Clarkston MI QTH proxy, from the handoff.
QTH_LAT = 42.72285220808688
QTH_LON = -83.41970398420766

EARTH_RADIUS_MI = 3958.7613     # mean radius, miles
EARTH_RADIUS_KM = 6371.0088     # mean radius, kilometres
MI_PER_KM = 0.621371

# Fields whose completeness decides which boards are buildable.
PROFILE_FIELDS = ["gridsquare", "state", "dxcc", "distance", "name", "mode", "band"]

# A "far" grid for the distance check means at least this many miles away.
FAR_MILES = 3000

W = 78  # console rule width


# ---------------------------------------------------------------- output helpers

def rule(title=""):
    if title:
        print("\n" + "=" * W)
        print(title)
        print("=" * W)
    else:
        print("-" * W)


def dump(label, obj):
    print(f"{label}:")
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def pct(n, total):
    return 0.0 if not total else 100.0 * n / total


# ---------------------------------------------------------------- maidenhead

def grid_center(grid):
    """Center lat/lon of a 2, 4, 6 or 8 character Maidenhead locator.

    Returns (lat, lon) or None if the locator is unusable.
    """
    if not grid:
        return None
    g = str(grid).strip()
    n = len(g)
    if n < 2 or n % 2 or n > 8:
        return None

    try:
        lon = -180.0
        lat = -90.0
        lon_cell = 20.0
        lat_cell = 10.0

        # Field: A-R
        f_lon = ord(g[0].upper()) - ord("A")
        f_lat = ord(g[1].upper()) - ord("A")
        if not (0 <= f_lon <= 17 and 0 <= f_lat <= 17):
            return None
        lon += f_lon * 20.0
        lat += f_lat * 10.0

        # Square: 0-9
        if n >= 4:
            if not (g[2].isdigit() and g[3].isdigit()):
                return None
            lon += int(g[2]) * 2.0
            lat += int(g[3]) * 1.0
            lon_cell, lat_cell = 2.0, 1.0

        # Subsquare: a-x
        if n >= 6:
            s_lon = ord(g[4].lower()) - ord("a")
            s_lat = ord(g[5].lower()) - ord("a")
            if not (0 <= s_lon <= 23 and 0 <= s_lat <= 23):
                return None
            lon += s_lon * (2.0 / 24.0)
            lat += s_lat * (1.0 / 24.0)
            lon_cell, lat_cell = 2.0 / 24.0, 1.0 / 24.0

        # Extended square: 0-9
        if n >= 8:
            if not (g[6].isdigit() and g[7].isdigit()):
                return None
            lon += int(g[6]) * (2.0 / 240.0)
            lat += int(g[7]) * (1.0 / 240.0)
            lon_cell, lat_cell = 2.0 / 240.0, 1.0 / 240.0

        return (lat + lat_cell / 2.0, lon + lon_cell / 2.0)
    except (TypeError, ValueError, IndexError):
        return None


def great_circle(lat1, lon1, lat2, lon2, radius):
    """Haversine great-circle distance in the units of `radius`."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- http

class ApiError(Exception):
    def __init__(self, message, status=None, code=None, request_id=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id


def get(session, path, params=None):
    """GET an endpoint and return (data, meta, response). Honours Retry-After on 429."""
    url = BASE_URL + path
    attempt = 0
    while True:
        attempt += 1
        try:
            r = session.get(url, params=params, timeout=TIMEOUT)
        except requests.exceptions.RequestException as exc:
            if attempt >= MAX_RETRIES:
                raise ApiError(f"network failure calling {path}: {exc}") from exc
            wait = 2 ** attempt
            print(f"  ! {type(exc).__name__} on {path}, retry {attempt}/{MAX_RETRIES} in {wait}s")
            time.sleep(wait)
            continue

        req_id = r.headers.get("X-Request-Id")

        if r.status_code == 429:
            # Retry-After is computed from the window that actually blocked — trust it.
            wait = int(r.headers.get("Retry-After") or 5)
            print(f"  ! 429 rate limited on {path}, sleeping {wait}s (X-Request-Id: {req_id})")
            time.sleep(wait)
            if attempt >= MAX_RETRIES:
                raise ApiError("rate limited repeatedly", 429, "rate_limited", req_id)
            continue

        try:
            body = r.json()
        except ValueError:
            raise ApiError(
                f"non-JSON response from {path} (HTTP {r.status_code}): {r.text[:200]!r}",
                r.status_code, None, req_id,
            )

        if not isinstance(body, dict):
            raise ApiError(f"unexpected top-level JSON type from {path}: {type(body).__name__}",
                           r.status_code, None, req_id)

        err = body.get("error")
        if r.status_code >= 400 or err:
            code = (err or {}).get("code") if isinstance(err, dict) else None
            msg = (err or {}).get("message") if isinstance(err, dict) else str(err)
            raise ApiError(f"HTTP {r.status_code} on {path}: code={code} message={msg}",
                           r.status_code, code, req_id)

        return body.get("data"), body.get("meta"), r


# ---------------------------------------------------------------- step 1: /v1/me

def step_me(session):
    rule("STEP 1 — GET /v1/me")
    data, meta, resp = get(session, "/v1/me")
    print(f"X-Request-Id: {resp.headers.get('X-Request-Id')}\n")

    dump("raw data", data)
    if meta:
        dump("\nraw meta", meta)

    print()
    rule()
    d = data if isinstance(data, dict) else {}
    default_logbook = d.get("defaultLogbook")
    resolution = None
    if isinstance(default_logbook, dict):
        resolution = default_logbook.get("resolution")
    resolution = resolution or d.get("resolution")

    print(f"  defaultLogbook : {json.dumps(default_logbook, default=str)}")
    print(f"  resolution     : {resolution}")
    print(f"  membershipTier : {d.get('membershipTier')}")
    print(f"  limits         : {json.dumps(d.get('limits'), default=str)}")

    if resolution == "ambiguous":
        print("\n  >> resolution is AMBIGUOUS. The collector must pass an explicit")
        print("     logbookId, or deliberately aggregate across all logbooks.")
    elif resolution == "none":
        print("\n  >> resolution is NONE. No default logbook — an explicit logbookId "
              "is required.")

    return d, resolution


# ---------------------------------------------------------------- step 2: /v1/logbooks

def step_logbooks(session):
    rule("STEP 2 — GET /v1/logbooks")
    try:
        data, meta, resp = get(session, "/v1/logbooks")
    except ApiError as exc:
        # Not fatal: the rest of discovery is still worth having.
        print(f"  ! could not read /v1/logbooks: {exc}")
        if exc.status == 404:
            print("    (endpoint may not exist on this API version)")
        return []

    print(f"X-Request-Id: {resp.headers.get('X-Request-Id')}\n")

    if isinstance(data, dict):
        books = data.get("logbooks") or data.get("items") or []
    elif isinstance(data, list):
        books = data
    else:
        books = []

    if not books:
        print("  (no logbooks returned)")
        dump("raw data", data)
        return []

    print(f"  {len(books)} logbook(s):\n")
    print(f"  {'id':38}  {'locked':7}  name")
    print(f"  {'-'*38}  {'-'*7}  {'-'*24}")
    for b in books:
        if not isinstance(b, dict):
            print(f"  {b!r}")
            continue
        locked = b.get("locked", b.get("isLocked"))
        print(f"  {str(b.get('id')):38}  {str(locked):7}  {b.get('name')}")

    print()
    dump("raw data (first logbook, for field shape)", books[0])
    return books


# ---------------------------------------------------------------- step 3: sample

def fetch_sample(session, limit):
    """Pull up to `limit` contacts via cursor paging. Returns (contacts, first_meta)."""
    contacts = []
    cursor = None
    first_meta = None
    page = 0

    while len(contacts) < limit:
        page += 1
        params = {"limit": min(PAGE_SIZE, limit - len(contacts))}
        if cursor:
            params["cursor"] = cursor  # opaque — pass back verbatim, never parse

        data, meta, resp = get(session, "/v1/contacts", params)
        if first_meta is None:
            first_meta = meta

        if isinstance(data, dict):
            rows = data.get("contacts") or data.get("items") or []
        elif isinstance(data, list):
            rows = data
        else:
            rows = []

        print(f"  page {page}: {len(rows)} rows "
              f"(X-Request-Id: {resp.headers.get('X-Request-Id')})")
        contacts.extend(rows)

        cursor = (meta or {}).get("nextCursor")
        if not cursor or not rows:
            break
        time.sleep(PAGE_SLEEP)

    return contacts, first_meta


def band_label(band):
    """`band` is metres as a number: 20 -> '20m', 0.7 -> '70cm'."""
    if band is None:
        return "?"
    try:
        b = float(band)
    except (TypeError, ValueError):
        return str(band)
    if b < 1:
        return f"{round(b * 100):g}cm"
    return f"{b:g}m"


def is_absent(v):
    return v is None or (isinstance(v, str) and not v.strip())


def step_sample(session):
    rule(f"STEP 3 — GET /v1/contacts (first {SAMPLE_SIZE})")
    contacts, meta = fetch_sample(session, SAMPLE_SIZE)
    n = len(contacts)
    print(f"\n  pulled {n} contacts")

    if meta:
        print()
        dump("raw meta of first page (look for a total count here)", meta)

    if not contacts:
        print("  ! no contacts returned — nothing to profile")
        return contacts, meta

    print()
    dump("raw data (first contact, for field shape)", contacts[0])

    # --- null rates -------------------------------------------------------
    rule()
    print(f"  NULL RATES over {n} contacts\n")
    print(f"  {'field':12}  {'present':>7}  {'null':>6}  {'empty':>6}  {'missing %':>9}")
    print(f"  {'-'*12}  {'-'*7}  {'-'*6}  {'-'*6}  {'-'*9}")
    for f in PROFILE_FIELDS:
        nulls = empties = present = 0
        for c in contacts:
            v = c.get(f)
            if v is None:
                nulls += 1
            elif isinstance(v, str) and not v.strip():
                empties += 1
            else:
                present += 1
        missing = nulls + empties
        flag = "   <-- sparse" if pct(missing, n) >= 25 else ""
        print(f"  {f:12}  {present:7}  {nulls:6}  {empties:6}  {pct(missing, n):8.1f}%{flag}")

    # --- distinct modes and bands ----------------------------------------
    rule()
    modes = Counter(c.get("mode") for c in contacts if not is_absent(c.get("mode")))
    print(f"  DISTINCT MODES ({len(modes)} present in sample)\n")
    for mode, cnt in modes.most_common():
        print(f"    {str(mode):10}  {cnt:5}  {pct(cnt, n):5.1f}%")

    bands = Counter(c.get("band") for c in contacts if c.get("band") is not None)
    print(f"\n  DISTINCT BANDS ({len(bands)} present in sample)\n")
    for band, cnt in sorted(bands.items(), key=lambda kv: -kv[1]):
        print(f"    {band!r:10} -> {band_label(band):7}  {cnt:5}  {pct(cnt, n):5.1f}%")

    # --- date range -------------------------------------------------------
    rule()
    stamps = []
    unparsed = 0
    for c in contacts:
        ts = c.get("timestamp")
        if is_absent(ts):
            unparsed += 1
            continue
        try:
            stamps.append(datetime.fromisoformat(str(ts).replace("Z", "+00:00")))
        except ValueError:
            unparsed += 1
    if stamps:
        lo, hi = min(stamps), max(stamps)
        span = (hi - lo).days
        print(f"  DATE RANGE OF SAMPLE\n")
        print(f"    newest : {hi.isoformat()}")
        print(f"    oldest : {lo.isoformat()}")
        print(f"    span   : {span} days for {n} contacts "
              f"({n / max(span, 1):.1f} QSO/day average)")
        print(f"\n    (results are newest-first, so this is the trailing edge of the log)")
    else:
        print("  ! no parseable timestamps in the sample")
    if unparsed:
        print(f"    ! {unparsed} contacts had a missing or unparseable timestamp")

    return contacts, meta


# ---------------------------------------------------------------- step 4: distance unit

def step_distance(contacts):
    rule("STEP 4 — VERIFY THE distance UNIT")
    print(f"  reference QTH: {QTH_LAT}, {QTH_LON} (Clarkston MI proxy)\n")

    # Every contact where we can independently compute the distance ourselves.
    usable = []
    for c in contacts:
        d = c.get("distance")
        center = grid_center(c.get("gridsquare"))
        if d is None or center is None:
            continue
        try:
            d = float(d)
        except (TypeError, ValueError):
            continue
        if d <= 0:
            continue
        mi = great_circle(QTH_LAT, QTH_LON, center[0], center[1], EARTH_RADIUS_MI)
        km = great_circle(QTH_LAT, QTH_LON, center[0], center[1], EARTH_RADIUS_KM)
        if mi < 1:
            continue  # same-grid QSO, ratio is meaningless
        usable.append({
            "call": c.get("call"),
            "grid": c.get("gridsquare"),
            "my_grid": c.get("myGridsquare"),
            "reported": d,
            "calc_mi": mi,
            "calc_km": km,
            "ratio": d / mi,
        })

    if not usable:
        print("  ! no contact in the sample has BOTH a distance and a usable gridsquare.")
        print("    Cannot verify the unit from this sample. Options: pull a larger")
        print("    sample, or check a contact whose grid you know by hand.")
        return None

    # The headline check the handoff asks for: one far contact, shown in full.
    far = [u for u in usable if u["calc_mi"] >= FAR_MILES]
    pick = max(far, key=lambda u: u["calc_mi"]) if far else max(usable, key=lambda u: u["calc_mi"])
    if not far:
        print(f"  ! nothing in the sample is more than {FAR_MILES} mi out; using the")
        print(f"    furthest available instead. The verdict is weaker at short range.\n")

    print(f"  furthest usable contact in sample")
    print(f"    call            : {pick['call']}")
    print(f"    their grid      : {pick['grid']}")
    print(f"    our grid on QSO : {pick['my_grid']}")
    print(f"    computed        : {pick['calc_mi']:>10,.1f} mi   /  {pick['calc_km']:>10,.1f} km")
    print(f"    API distance    : {pick['reported']:>10,.1f}")
    print(f"    ratio (api/mi)  : {pick['ratio']:.4f}")

    # Corroborate across the whole sample so one bad grid can't decide this.
    ratios = sorted(u["ratio"] for u in usable)
    mid = len(ratios) // 2
    median = ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2

    print(f"\n  corroboration across all {len(usable)} usable contacts in the sample")
    print(f"    median ratio    : {median:.4f}")
    print(f"    min / max ratio : {ratios[0]:.4f} / {ratios[-1]:.4f}")

    if abs(median - 1.0) <= 0.05:
        verdict = "MILES"
        note = "distance is already in miles — use it as-is."
    elif abs(median - (1.0 / MI_PER_KM)) <= 0.08:   # 1.609
        verdict = "KILOMETRES"
        note = f"multiply every distance by {MI_PER_KM} to get miles."
    elif abs(median - MI_PER_KM) <= 0.05:           # 0.621
        verdict = "unexpected — looks like miles-per-km inverted"
        note = "investigate before trusting the field."
    else:
        verdict = "INCONCLUSIVE"
        note = ("ratio matches neither 1.0 (miles) nor 1.609 (km). Do not trust "
                "`distance`; consider computing it ourselves from gridsquare.")

    spread = ratios[-1] - ratios[0]
    print(f"\n  >> VERDICT: {verdict}")
    print(f"     {note}")
    if spread > 0.15:
        print(f"     ! ratio spread is {spread:.3f} — wider than grid-center rounding")
        print(f"       explains. Some distances may be computed from a different")
        print(f"       origin than the QTH proxy (check myGridsquare).")

    return verdict


# ---------------------------------------------------------------- summary

def summarize(me, resolution, logbooks, contacts, sample_meta, verdict):
    rule("SUMMARY — the open questions from the handoff")

    n = len(contacts)
    grids = sum(1 for c in contacts if not is_absent(c.get("gridsquare")))
    dxccs = sum(1 for c in contacts if c.get("dxcc") is not None)
    dists = sum(1 for c in contacts if c.get("distance") is not None)
    modes = Counter(c.get("mode") for c in contacts if not is_absent(c.get("mode")))
    top_mode, top_mode_n = modes.most_common(1)[0] if modes else (None, 0)

    print(f"\n  1. Is distance miles or kilometres?")
    print(f"     {verdict or 'UNRESOLVED — no contact had both distance and gridsquare'}")

    print(f"\n  2. How complete is gridsquare? (decides the map board)")
    print(f"     {grids}/{n} present ({pct(grids, n):.1f}%). "
          f"dxcc {pct(dxccs, n):.1f}%, distance {pct(dists, n):.1f}%.")
    if pct(grids, n) < 60:
        print(f"     >> sparse. The grid map will be thin as designed — consider")
        print(f"        deriving approximate positions from dxcc, or dropping the board.")

    print(f"\n  3. Logbooks and default resolution")
    print(f"     resolution={resolution}, {len(logbooks)} logbook(s) visible.")
    if resolution == "ambiguous":
        print(f"     >> must decide: filter to one logbookId, or aggregate all.")
    elif resolution in ("configured", "sole"):
        print(f"     >> safe to rely on the default logbook; no logbookId needed.")

    print(f"\n  4. Enough distinct modes for the four-colour split?")
    print(f"     {len(modes)} distinct mode(s) in the sample.")
    if top_mode:
        print(f"     most common: {top_mode} at {pct(top_mode_n, n):.1f}% of the sample.")
    if len(modes) <= 1:
        print(f"     >> single-mode sample. The mode panel may not be worth four colours.")
    elif pct(top_mode_n, n) > 90:
        print(f"     >> overwhelmingly one mode. Check a full-log pull before "
              f"committing to the split.")

    print(f"\n  5. Total QSO count / expected page count")
    total = None
    if isinstance(sample_meta, dict):
        for k in ("total", "totalCount", "count", "totalItems"):
            if isinstance(sample_meta.get(k), int):
                total, total_key = sample_meta[k], k
                break
    if total is not None:
        pages = math.ceil(total / PAGE_SIZE)
        print(f"     meta.{total_key} = {total:,} contacts -> {pages} pages of {PAGE_SIZE},")
        print(f"     ~{pages * PAGE_SLEEP:.0f}s of sleep plus request time for a full pull.")
        if pages > 100:
            print(f"     >> {pages} requests in one run. Still inside 20,000 reads/day, but")
            print(f"        keep PAGE_SLEEP at {PAGE_SLEEP}s to stay under 120 reads/min.")
    else:
        print(f"     No total in meta. The full pull pages until nextCursor is null.")
        print(f"     At {PAGE_SIZE} rows/page and {PAGE_SLEEP}s between pages, a 10k log is")
        print(f"     ~100 requests and ~1 minute — well inside the 120/min read budget.")

    print(f"\n  This script wrote no files. Nothing to clean up.")
    print("=" * W)


# ---------------------------------------------------------------- main

def main():
    key = os.environ.get("WRL_API_KEY", "").strip()
    if not key:
        print("ERROR: WRL_API_KEY is not set.", file=sys.stderr)
        print("  bash:    export WRL_API_KEY=wrl_live_...", file=sys.stderr)
        print("  Windows: set WRL_API_KEY=wrl_live_...", file=sys.stderr)
        return 2

    print("=" * W)
    print("WRL API DISCOVERY — read-only, writes no files")
    print(f"base : {BASE_URL}")
    print(f"key  : {key[:9]}...{key[-4:]} ({len(key)} chars)")
    print(f"run  : {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print("=" * W)

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "wrl-boards-discover/1.0 (K8JKU)",
    })

    try:
        me, resolution = step_me(session)
    except ApiError as exc:
        print(f"\nFATAL: {exc}", file=sys.stderr)
        if exc.status in (401, 403):
            print("  Authentication failed. Check WRL_API_KEY and that the "
                  "membership is current.", file=sys.stderr)
        if exc.request_id:
            print(f"  X-Request-Id: {exc.request_id}", file=sys.stderr)
        return 1

    logbooks = step_logbooks(session)

    try:
        contacts, sample_meta = step_sample(session)
    except ApiError as exc:
        print(f"\nFATAL: {exc}", file=sys.stderr)
        if exc.request_id:
            print(f"  X-Request-Id: {exc.request_id}", file=sys.stderr)
        return 1

    verdict = step_distance(contacts) if contacts else None
    summarize(me, resolution, logbooks, contacts, sample_meta, verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
