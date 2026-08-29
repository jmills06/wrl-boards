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
    PowerShell:  $env:WRL_API_KEY = "wrl_live_..."
    cmd.exe:     set WRL_API_KEY=wrl_live_...
    bash:        export WRL_API_KEY=wrl_live_...

    then, from the repo directory:  python discover.py

Note for Windows: `set` is an alias for Set-Variable in PowerShell and will
NOT set an environment variable there — use $env: as shown. The `py` launcher
is absent from Microsoft Store installs of Python; `python` always works.

Requires: pip install requests truststore
"""

import argparse
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

# Bumped whenever the report changes, and printed in the header. A stale
# download is otherwise indistinguishable from a run that found nothing.
VERSION = "5 (reach + distance outliers)"

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


# ---------------------------------------------------------------- auth

def describe_key(key):
    """Report the SHAPE of the key without revealing it.

    Catches the usual copy-paste damage: placeholder text, smart quotes from a
    web page, stray whitespace, invisible characters.
    """
    print(f"key  : {key[:9]}...{key[-4:]} ({len(key)} chars)")

    problems = []
    if not key.startswith("wrl_live_"):
        problems.append(f"does not start with 'wrl_live_' (starts with {key[:9]!r})")
    elif key.count("wrl_live_") > 1:
        problems.append("the 'wrl_live_' prefix appears more than once — the key "
                        "was probably pasted into a template that already had it")
    if len(key) < 30:
        problems.append(f"only {len(key)} chars — real keys are considerably longer")
    if any(c in key for c in "<>"):
        problems.append("contains < or > — looks like placeholder text was pasted literally")
    if any(c.isspace() for c in key):
        problems.append("contains whitespace inside the key")
    non_ascii = sorted({c for c in key if not (32 <= ord(c) < 127)})
    if non_ascii:
        problems.append(f"contains non-ASCII characters: {non_ascii!r} "
                        "(smart quotes or a hidden character from copy-paste?)")

    if problems:
        print("\n  ! the key does not look right:")
        for p_ in problems:
            print(f"    - {p_}")
        print()
    return not problems


def apply_auth(session, key, style):
    """WRL accepts either header. Set exactly one so the failure is unambiguous."""
    session.headers.pop("Authorization", None)
    session.headers.pop("X-API-Key", None)
    if style == "bearer":
        session.headers["Authorization"] = f"Bearer {key}"
    else:
        session.headers["X-API-Key"] = key


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

def fetch_sample(session, limit=None):
    """Pull contacts via cursor paging. `limit=None` pulls the entire log.

    Returns (contacts, first_meta).
    """
    contacts = []
    cursor = None
    first_meta = None
    page = 0

    while limit is None or len(contacts) < limit:
        page += 1
        want = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(contacts))
        params = {"limit": want}
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

        if limit is None:
            # Full pulls are long; one line per page would be pages of noise.
            if page % 10 == 1 or not rows:
                print(f"  page {page}: {len(rows)} rows, {len(contacts) + len(rows)} so far")
        else:
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


def step_sample(session, logbooks, full=False):
    if full:
        rule("STEP 3 — GET /v1/contacts (ENTIRE LOG)")
        print("  paging until nextCursor is null; this may take a minute\n")
        contacts, meta = fetch_sample(session, None)
    else:
        rule(f"STEP 3 — GET /v1/contacts (first {SAMPLE_SIZE})")
        contacts, meta = fetch_sample(session, SAMPLE_SIZE)
    n = len(contacts)
    print(f"\n  pulled {n} contacts")

    if meta:
        print()
        dump("raw meta of first page", meta)
        if isinstance(meta, dict) and "count" in meta and "total" not in meta:
            print("\n  note: meta.count is the size of THIS PAGE, not the log total.")
            print("        This API reports no log total; run with --full to count it.")

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
        if not full:
            print(f"\n    (newest-first, so this is the trailing edge of the log)")
    else:
        print("  ! no parseable timestamps in the sample")
    if unparsed:
        print(f"    ! {unparsed} contacts had a missing or unparseable timestamp")

    # Year distribution is only meaningful over the whole log.
    if full and stamps:
        rule()
        years = Counter(d.year for d in stamps)
        print(f"  CONTACTS BY YEAR\n")
        for y in sorted(years):
            print(f"    {y}  {years[y]:6}")

    if full:
        report_reach(contacts)
        report_distance_outliers(contacts)
    report_logbooks(contacts, logbooks)
    return contacts, meta


def report_reach(contacts):
    """The headline numbers the career and grid boards actually display."""
    rule()
    grids4 = {str(c.get("gridsquare")).strip().upper()[:4]
              for c in contacts if not is_absent(c.get("gridsquare"))
              and len(str(c.get("gridsquare")).strip()) >= 4}
    fields = {g[:2] for g in grids4}
    entities = {c.get("dxcc") for c in contacts if c.get("dxcc") is not None}

    lens = Counter(len(str(c.get("gridsquare")).strip())
                   for c in contacts if not is_absent(c.get("gridsquare")))

    print("  REACH — what the boards would display\n")
    print(f"    unique grids (4-char)  : {len(grids4)}")
    print(f"    unique fields (2-char) : {len(fields)} of 324")
    print(f"    unique dxcc entities   : {len(entities)}")
    print(f"\n    gridsquare precision actually stored:")
    for ln, cnt in sorted(lens.items()):
        print(f"      {ln} chars  {cnt:6}")
    return grids4, entities


def report_distance_outliers(contacts):
    """How trustworthy is `distance` per-contact, not just in aggregate?

    The median says kilometres beyond doubt, but records like "furthest
    contact" are decided by a SINGLE row, so a handful of bad rows matters
    far more there than it does to a median.
    """
    rule()
    rows = []
    for c in contacts:
        d, g = c.get("distance"), c.get("gridsquare")
        center = grid_center(g)
        if d is None or center is None:
            continue
        try:
            d = float(d)
        except (TypeError, ValueError):
            continue
        mi = great_circle(QTH_LAT, QTH_LON, center[0], center[1], EARTH_RADIUS_MI)
        if mi < 50:
            continue
        rows.append((d / mi, c, d, mi))

    if not rows:
        print("  DISTANCE OUTLIERS — nothing comparable")
        return

    KM = 1.0 / MI_PER_KM
    bad = [r for r in rows if not (0.8 * KM <= r[0] <= 1.25 * KM)]
    print(f"  DISTANCE OUTLIERS — rows more than 20-25% off the km ratio\n")
    print(f"    {len(bad)} of {len(rows)} comparable rows ({pct(len(bad), len(rows)):.2f}%)")

    # Does operating portable explain them? WRL computes from the QSO's own
    # myGridsquare; we compare against a fixed home QTH.
    def home(c):
        mg = str(c.get("myGridsquare") or "").strip().upper()
        return mg.startswith("EN82")

    bad_away = sum(1 for r in bad if not home(r[1]))
    all_away = sum(1 for r in rows if not home(r[1]))
    print(f"    of those, {bad_away} were logged from a myGridsquare outside EN82")
    print(f"    (across the whole set, {all_away} of {len(rows)} were outside EN82)")
    if all_away:
        print(f"    outlier rate away from EN82 : {pct(bad_away, all_away):.1f}%")
    if len(rows) - all_away:
        print(f"    outlier rate at home EN82   : "
              f"{pct(len(bad) - bad_away, len(rows) - all_away):.1f}%")

    print(f"\n    worst offenders:")
    for ratio, c, d, mi in sorted(rows, key=lambda r: -abs(r[0] - KM))[:8]:
        print(f"      {str(c.get('call')):10} their={str(c.get('gridsquare')):9} "
              f"mine={str(c.get('myGridsquare') or '-'):9} "
              f"api={d:10,.1f}  calc={mi:9,.1f}mi  ratio={ratio:8.2f}")

    dists = [float(c["distance"]) for c in contacts
             if c.get("distance") is not None]
    if dists:
        mx = max(dists)
        print(f"\n    largest distance in log : {mx:,.1f} "
              f"({mx * MI_PER_KM:,.1f} mi if km)")
        if mx * MI_PER_KM > 12500:
            print(f"      ! exceeds the ~12,450 mi antipodal maximum — that row is bad")


def report_logbooks(contacts, logbooks):
    """Which logbooks did an UNFILTERED /v1/contacts actually return?

    This is not cosmetic. If the unfiltered read spans every logbook, then
    "the default logbook" is not what the boards would be counting.
    """
    rule()
    names = {}
    for b in logbooks or []:
        if isinstance(b, dict):
            names[b.get("id")] = b.get("name")

    dist = Counter(c.get("logbookId") for c in contacts)
    n = len(contacts)
    print(f"  LOGBOOKS PRESENT IN THIS UNFILTERED READ ({len(dist)} distinct)\n")
    for lid, cnt in dist.most_common():
        print(f"    {cnt:6}  {pct(cnt, n):5.1f}%  {names.get(lid, '(name unknown)')}")
        print(f"            {lid}")
    return dist


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

    # A 4-character grid is a ~100 mi cell, so the grid center can sit far from
    # the real station. That error is a large FRACTION of a short path and a
    # negligible one of a long path. If the unit is consistent, ratios must
    # therefore converge as distance grows — which is a much stronger test than
    # any single contact.
    buckets = [(0, 500), (500, 1500), (1500, 3000), (3000, 99999)]
    print(f"\n    ratio by path length (rounding error shrinks as distance grows)")
    print(f"      {'range (mi)':>14}  {'n':>4}  {'median':>7}  {'spread':>7}")
    for lo_mi, hi_mi in buckets:
        grp = sorted(u["ratio"] for u in usable if lo_mi <= u["calc_mi"] < hi_mi)
        if not grp:
            continue
        m = grp[len(grp) // 2] if len(grp) % 2 else (grp[len(grp)//2 - 1] + grp[len(grp)//2]) / 2
        label = f"{lo_mi}-{hi_mi}" if hi_mi < 99999 else f"{lo_mi}+"
        print(f"      {label:>14}  {len(grp):4}  {m:7.4f}  {grp[-1]-grp[0]:7.4f}")

    # The longest paths are the most trustworthy evidence, so judge on those.
    far_ratios = sorted(u["ratio"] for u in usable if u["calc_mi"] >= 1500)
    if far_ratios:
        fm = (far_ratios[len(far_ratios)//2] if len(far_ratios) % 2
              else (far_ratios[len(far_ratios)//2 - 1] + far_ratios[len(far_ratios)//2]) / 2)
        print(f"\n    median over paths >1500 mi: {fm:.4f}  ({len(far_ratios)} contacts)")
        median = fm  # decide the verdict on the least noisy evidence

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

    spread = (far_ratios[-1] - far_ratios[0]) if far_ratios else (ratios[-1] - ratios[0])
    print(f"\n  >> VERDICT: {verdict}")
    print(f"     {note}")
    if spread > 0.15:
        print(f"     ! spread among LONG paths is {spread:.3f}, where grid-center")
        print(f"       rounding should be small. Some distances may use a different")
        print(f"       origin than the QTH proxy (check myGridsquare).")

    return verdict


# ---------------------------------------------------------------- summary

def summarize(me, resolution, logbooks, contacts, sample_meta, verdict, full=False):
    rule("SUMMARY — the open questions from the handoff")

    n = len(contacts)
    grids = sum(1 for c in contacts if not is_absent(c.get("gridsquare")))
    dxccs = sum(1 for c in contacts if c.get("dxcc") is not None)
    dists = sum(1 for c in contacts if c.get("distance") is not None)
    modes = Counter(c.get("mode") for c in contacts if not is_absent(c.get("mode")))
    top_mode, top_mode_n = modes.most_common(1)[0] if modes else (None, 0)

    print(f"\n  1. Is distance miles or kilometres?")
    print(f"     {verdict or 'UNRESOLVED — no contact had both distance and gridsquare'}")

    scope = "the WHOLE LOG" if full else f"the trailing {n} contacts only"
    print(f"\n  2. How complete is gridsquare? (decides the map board)")
    print(f"     Measured over {scope}.")
    print(f"     {grids}/{n} present ({pct(grids, n):.1f}%). "
          f"dxcc {pct(dxccs, n):.1f}%, distance {pct(dists, n):.1f}%.")
    if not full:
        print(f"     >> a trailing sample is a biased view of grid completeness.")
        print(f"        Re-run with --full before deciding the map board's fate.")
    if pct(grids, n) < 60:
        print(f"     >> sparse. The grid map will be thin as designed — consider")
        print(f"        deriving approximate positions from dxcc, or dropping the board.")

    print(f"\n  3. Logbooks and default resolution")
    print(f"     resolution={resolution}, {len(logbooks)} logbook(s) visible.")

    default_id = None
    if isinstance(me, dict) and isinstance(me.get("defaultLogbook"), dict):
        default_id = (me["defaultLogbook"].get("logbookId")
                      or me["defaultLogbook"].get("id"))
    seen = Counter(c.get("logbookId") for c in contacts)

    if len(seen) > 1:
        print(f"     >> the UNFILTERED read spans {len(seen)} logbooks, not just the")
        print(f"        default. Boards built on it would count every logbook.")
        print(f"        Decide deliberately: aggregate (pass no logbookId) or")
        print(f"        filter (pass logbookId={default_id}).")
    elif seen and default_id and default_id not in seen:
        print(f"     >> the unfiltered read returned a logbook that is NOT the")
        print(f"        default ({default_id}). Do not assume default scoping.")
    elif resolution in ("configured", "sole"):
        print(f"     >> unfiltered reads matched the default logbook in this sample.")

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
        # NOT "count" — this API uses that for the page size, not the log total.
        for k in ("total", "totalCount", "totalItems"):
            if isinstance(sample_meta.get(k), int):
                total, total_key = sample_meta[k], k
                break
    if full:
        pages = math.ceil(n / PAGE_SIZE)
        print(f"     COUNTED DIRECTLY: {n:,} contacts across {pages} pages.")
        print(f"     A nightly full pull is {pages} requests, ~{pages * PAGE_SLEEP:.0f}s "
              f"of sleep plus request time.")
        if pages > 100:
            print(f"     >> over 100 requests. Inside 20,000 reads/day, and "
                  f"PAGE_SLEEP={PAGE_SLEEP}s keeps it under 120 reads/min.")
        total = None
    if total is not None:
        pages = math.ceil(total / PAGE_SIZE)
        print(f"     meta.{total_key} = {total:,} contacts -> {pages} pages of {PAGE_SIZE},")
        print(f"     ~{pages * PAGE_SLEEP:.0f}s of sleep plus request time for a full pull.")
        if pages > 100:
            print(f"     >> {pages} requests in one run. Still inside 20,000 reads/day, but")
            print(f"        keep PAGE_SLEEP at {PAGE_SLEEP}s to stay under 120 reads/min.")
    elif not full:
        print(f"     No total in meta — meta.count is the page size, not the log size.")
        print(f"     Re-run with --full to count the log directly.")

    print(f"\n  This script wrote no files. Nothing to clean up.")
    print("=" * W)


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Read-only discovery against the World Radio League API.")
    ap.add_argument("--full", action="store_true",
                    help="page the ENTIRE log instead of the first "
                         f"{SAMPLE_SIZE} contacts. Answers the questions the "
                         "trailing sample cannot: true log size, true field "
                         "completeness, every mode and band ever used. Still "
                         "writes nothing.")
    args = ap.parse_args(argv)

    if args.full:
        print(f"(discover.py version {VERSION} — a --full run must print REACH "
              f"and DISTANCE OUTLIERS sections; if it does not, the download "
              f"was stale)")

    key = os.environ.get("WRL_API_KEY", "").strip()
    if not key:
        print("ERROR: WRL_API_KEY is not set.", file=sys.stderr)
        print('  PowerShell: $env:WRL_API_KEY = "wrl_live_..."', file=sys.stderr)
        print("  cmd.exe:    set WRL_API_KEY=wrl_live_...", file=sys.stderr)
        print("  bash:       export WRL_API_KEY=wrl_live_...", file=sys.stderr)
        print("", file=sys.stderr)
        print("  In PowerShell, `set` is an alias for Set-Variable and does not",
              file=sys.stderr)
        print("  set environment variables. Use the $env: form above.", file=sys.stderr)
        return 2

    print("=" * W)
    print("WRL API DISCOVERY — read-only, writes no files")
    print(f"ver  : {VERSION}")
    print(f"base : {BASE_URL}")
    describe_key(key)
    print(f"run  : {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print("=" * W)

    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": "wrl-boards-discover/1.0 (K8JKU)",
    })

    # The API documents both header styles. Try Bearer, fall back to X-API-Key,
    # so a 401 tells us the key is wrong rather than the header being wrong.
    me = resolution = None
    last = None
    for style in ("bearer", "x-api-key"):
        apply_auth(session, key, style)
        try:
            me, resolution = step_me(session)
            print(f"\n  (authenticated with {'Authorization: Bearer' if style == 'bearer' else 'X-API-Key'})")
            break
        except ApiError as exc:
            last = exc
            if exc.status in (401, 403) and style == "bearer":
                print(f"  ! Bearer rejected ({exc.code}); retrying with the "
                      f"X-API-Key header ...\n")
                continue
            break

    if me is None:
        exc = last
        print(f"\nFATAL: {exc}", file=sys.stderr)
        if exc.status in (401, 403):
            print("  Both Authorization: Bearer and X-API-Key were rejected.",
                  file=sys.stderr)
            print("  That points at the key itself, not the request. Check:",
                  file=sys.stderr)
            print("    - the key is the full string from WRL Integrations > "
                  "Developer API", file=sys.stderr)
            print("    - it has not been regenerated since (that revokes the old one)",
                  file=sys.stderr)
            print("    - the WRL membership is current — the API needs a paid tier",
                  file=sys.stderr)
            print("    - nothing was truncated or auto-corrected when pasting",
                  file=sys.stderr)
        if exc.request_id:
            print(f"  X-Request-Id: {exc.request_id}  <- quote this to WRL support",
                  file=sys.stderr)
        return 1

    logbooks = step_logbooks(session)

    try:
        contacts, sample_meta = step_sample(session, logbooks, full=args.full)
    except ApiError as exc:
        print(f"\nFATAL: {exc}", file=sys.stderr)
        if exc.request_id:
            print(f"  X-Request-Id: {exc.request_id}", file=sys.stderr)
        return 1

    verdict = step_distance(contacts) if contacts else None
    summarize(me, resolution, logbooks, contacts, sample_meta, verdict,
              full=args.full)
    return 0


if __name__ == "__main__":
    sys.exit(main())
