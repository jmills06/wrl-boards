#!/usr/bin/env python3
"""
collect.py — pull the WRL log and write the static JSON the boards read.

    python collect.py --mode=full        entire log -> all board files
    python collect.py --mode=full --dry-run     compute, print, write nothing

The boards never call the WRL API: the endpoints send no CORS headers, and an
API key in front-end code is a leaked key. This runs in a GitHub Action, holds
the key, and commits static JSON.

Why a full resync every night rather than incremental: the `since` filter works
on QSO timestamp, not `createdAt`, and there is no createdAt/updatedAt filter.
An incremental sync would silently miss backdated ADIF imports, edits to older
QSOs, and deletions. At ~86 pages the full pull is cheap and always correct.

Field handling forced by what discovery actually found:

  distance is KILOMETRES. Undocumented, verified empirically against 1,300
      contacts over 1,500 miles (median ratio 1.6094 vs the 1.6093 expected).
      Everything downstream is miles, converted once, here.
  band is a number in metres. 20 -> "20m", 0.7 -> "70cm".
  Submodes are NOT folded in, despite the API docs. LSB/USB arrive separately
      from SSB, and MFSK arrives raw. Normalised here.
  gridsquare is empty string, not null, when absent.
  dxcc is null on ~6% of contacts, distance on ~4%. Aggregates skip rather
      than assume.
  An unfiltered read spans logbooks. Contest logs are excluded per decision.
"""

import argparse
import json
import math
import os
import random
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import truststore

truststore.inject_into_ssl()   # before requests builds its SSL context

import requests  # noqa: E402

import dxcc

# ---------------------------------------------------------------- tuning

FRESH_DAYS = 30
RECENT_CONTACTS = 14
RECENT_WINDOW_DAYS = 45
# Widen to this if the window is too quiet to fill the contact list.
RECENT_FALLBACK_DAYS = 400
PAGE_SIZE = 100
PAGE_SLEEP = 0.5

# A band needs this many measured contacts before its median is meaningful.
MEDIAN_MIN_SAMPLES = 5
# Unique grids listed in the "newest grids" panel.
NEWEST_GRIDS = 8
# Window plotted on the recent board's maps, and its third tile.
MAP_WINDOW_DAYS = 15
# Contact rows listed under the maps. The maps are the board now; the list is
# there for the callsigns, not to be the board itself.
MAP_LIST_ROWS = 8
# Bands named in the map legend. Anything rarer is drawn in the "other" colour.
MAP_BANDS = 6
# The regional map's default frame: lon/lat of the SW and NE corners. Sized to
# the lower 48 and southern Canada, which is where the log actually is. Going
# wider to catch the occasional Alaskan costs real estate on every single
# render; those contacts land on the world map instead, which is what it is
# for. Its aspect is ~1.8:1, matched by the map box below it.
NA_FRAME = [[-126.0, 23.0], [-62.0, 51.0]]
# Padding around the operating site when the regional map has to leave North
# America, in degrees of lon/lat.
FRAME_PAD = [24.0, 14.0]
# Stations listed in the career board's furthest-contacts ladder.
DX_LADDER = 8
# Great-circle arcs drawn on the map.
ARC_COUNT = 6

BASE_URL = "https://api.worldradioleague.com"
TIMEOUT = (10, 60)            # (connect, read) — never a bare float
MAX_RETRIES = 5
BACKOFF_CAP = 30              # seconds; the curve never sleeps longer than this
# Statuses worth a second look. The gateway in front of the API returns 502 for
# a few seconds when it restarts, which is the whole reason this set exists.
RETRY_STATUS = frozenset({408, 425, 500, 502, 503, 504})

OUT_DIR = os.path.join("data", "latest")

KM_TO_MILES = 0.621371

# Logbooks excluded from every total. Contest logs only — see discovery.
# Fallback position for a contact that logged a state or province but no
# gridsquare. US values are the area-weighted centroid of each state's
# largest landmass, derived from vendor/us-states-10m.json (so Michigan
# lands in the Lower Peninsula, not a lake). Canadian values are
# population centres: a province centroid would put every Ontario
# contact several hundred miles north of every Ontario operator.
STATE_CENTERS = {
    "AB": (52.5, -113.7), "AK": (64.499, -152.695), "AL": (32.79, -86.828),
    "AR": (34.9, -92.44), "AS": (-14.294, -170.705), "AZ": (34.293, -111.665),
    "BC": (49.7, -123.0), "CA": (37.254, -119.612), "CO": (38.998, -105.548),
    "CT": (41.62, -72.726), "DC": (38.904, -77.015), "DE": (38.993, -75.501),
    "FL": (28.646, -82.503), "GA": (32.649, -83.446), "GU": (13.444, 144.775),
    "HI": (19.602, -155.521), "IA": (42.074, -93.5), "ID": (44.389, -114.659),
    "IL": (40.065, -89.199), "IN": (39.908, -86.276), "KS": (38.484, -98.38),
    "KY": (37.527, -85.288), "LA": (31.054, -91.978), "MA": (42.27, -71.823),
    "MB": (49.9, -97.1), "MD": (39.035, -76.77), "ME": (45.364, -69.225),
    "MI": (43.483, -84.622), "MN": (46.316, -94.309), "MO": (38.367, -92.477),
    "MP": (15.19, 145.75), "MS": (32.751, -89.665), "MT": (47.033, -109.645),
    "NB": (46.2, -66.0), "NC": (35.54, -79.365), "ND": (47.446, -100.47),
    "NE": (41.527, -99.81), "NH": (43.685, -71.578), "NJ": (40.183, -74.662),
    "NL": (48.0, -56.0), "NM": (34.421, -106.108), "NS": (44.8, -63.3),
    "NT": (62.5, -114.4), "NU": (63.7, -68.5), "NV": (39.355, -116.655),
    "NY": (42.943, -75.505), "OH": (40.293, -82.79), "OK": (35.583, -97.508),
    "ON": (44.0, -79.5), "OR": (43.936, -120.556), "PA": (40.874, -77.8),
    "PE": (46.3, -63.2), "PR": (18.224, -66.475), "QC": (46.3, -72.5),
    "RI": (41.694, -71.589), "SC": (33.907, -80.896), "SD": (44.436, -100.231),
    "SK": (51.5, -105.8), "TN": (35.843, -86.344), "TX": (31.482, -99.349),
    "UT": (39.323, -111.678), "VA": (37.513, -78.881), "VI": (17.73, -64.8),
    "VT": (44.075, -72.663), "WA": (47.375, -120.433), "WI": (44.634, -90.012),
    "WV": (38.641, -80.615), "WY": (42.999, -107.551), "YT": (60.7, -135.1),
}

EXCLUDED_LOGBOOKS = {
    "d0f88526-bf85-4114-bff1-390f8b55996c",   # K8JKU - CQ Worldwide DX, SSB
}

# Refuse to overwrite good data with a suspiciously small pull.
MIN_SANE_CONTACTS = 100
MAX_SHRINK = 0.10             # abort if the log shrank more than this

# ---------------------------------------------------------------- modes

# The API does not fold submodes in, so we do. Maps raw mode -> display mode.
MODE_ALIASES = {
    "LSB": "SSB", "USB": "SSB",
}

# Display mode -> colour category. The board has four colours.
MODE_CATEGORY = {
    "CW": "cw",
    "SSB": "ssb",
    "FM": "fm",
}
# Everything not named above is digital. Listed explicitly so an unexpected
# mode is visible in review rather than silently absorbed.
KNOWN_DIGITAL = {
    "FT8", "FT4", "FT2", "JS8", "MFSK", "PSK31", "PSK63", "PSK",
    "RTTY", "VARA HF", "VARAC", "OLIVIA", "JT65", "JT9", "DSTAR",
    "DMR", "C4FM", "SSTV", "HELL", "CONTESTIA", "MT63", "Q65",
}

# ---------------------------------------------------------------- places

# The recent board bolds the state for North American contacts and the country
# for everyone else, so abbreviations need expanding.
STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
    "VI": "Virgin Islands", "GU": "Guam", "AS": "American Samoa",
    "MP": "N. Mariana Is.",
    # Canadian provinces and territories
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba",
    "NB": "New Brunswick", "NL": "Newfoundland", "NS": "Nova Scotia",
    "NT": "NW Territories", "NU": "Nunavut", "ON": "Ontario",
    "PE": "Prince Edward I.", "QC": "Quebec", "SK": "Saskatchewan",
    "YT": "Yukon",
}
# Entities whose contacts show a state/province instead of the country.
STATE_ENTITIES = {291, 1, 6, 110}    # USA, Canada, Alaska, Hawaii


# ---------------------------------------------------------------- helpers

def band_label(band):
    """`band` is metres as a number: 20 -> '20m', 0.7 -> '70cm'."""
    if band is None:
        return None
    try:
        b = float(band)
    except (TypeError, ValueError):
        return str(band)
    if b < 1:
        return f"{round(b * 100):g}cm"
    return f"{b:g}m"


def band_sort_key(label):
    """Sort band labels by wavelength descending (160m first, 70cm last)."""
    if not label:
        return -1.0
    try:
        if label.endswith("cm"):
            return float(label[:-2]) / 100.0
        return float(label[:-1])
    except ValueError:
        return -1.0


def normalise_mode(raw):
    if not raw or not str(raw).strip():
        return None
    m = str(raw).strip().upper()
    return MODE_ALIASES.get(m, m)


def mode_category(mode):
    if mode in MODE_CATEGORY:
        return MODE_CATEGORY[mode]
    return "dig"


def is_absent(v):
    return v is None or (isinstance(v, str) and not v.strip())


def grid_center(grid):
    """Centre (lat, lon) of a 2/4/6/8 character Maidenhead locator.

    Returns None for anything malformed. The log contains one 7-character
    locator, which is not valid Maidenhead — odd lengths are rejected.
    """
    if is_absent(grid):
        return None
    g = str(grid).strip()
    n = len(g)
    if n < 2 or n % 2 or n > 8:
        return None
    try:
        lon, lat = -180.0, -90.0
        lon_cell, lat_cell = 20.0, 10.0

        f_lon = ord(g[0].upper()) - ord("A")
        f_lat = ord(g[1].upper()) - ord("A")
        if not (0 <= f_lon <= 17 and 0 <= f_lat <= 17):
            return None
        lon += f_lon * 20.0
        lat += f_lat * 10.0

        if n >= 4:
            if not (g[2].isdigit() and g[3].isdigit()):
                return None
            lon += int(g[2]) * 2.0
            lat += int(g[3]) * 1.0
            lon_cell, lat_cell = 2.0, 1.0
        if n >= 6:
            s_lon = ord(g[4].lower()) - ord("a")
            s_lat = ord(g[5].lower()) - ord("a")
            if not (0 <= s_lon <= 23 and 0 <= s_lat <= 23):
                return None
            lon += s_lon * (2.0 / 24.0)
            lat += s_lat * (1.0 / 24.0)
            lon_cell, lat_cell = 2.0 / 24.0, 1.0 / 24.0
        if n >= 8:
            if not (g[6].isdigit() and g[7].isdigit()):
                return None
            lon += int(g[6]) * (2.0 / 240.0)
            lat += int(g[7]) * (1.0 / 240.0)
            lon_cell, lat_cell = 2.0 / 240.0, 1.0 / 240.0

        return (round(lat + lat_cell / 2.0, 4), round(lon + lon_cell / 2.0, 4))
    except (TypeError, ValueError, IndexError):
        return None


def grid4(grid):
    """Normalise a locator to its 4-character square, or None."""
    if is_absent(grid):
        return None
    g = str(grid).strip().upper()
    return g[:4] if len(g) >= 4 else None


def parse_ts(value):
    if is_absent(value):
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def median(values):
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def resolve_entity(entity_code, call):
    """The API's dxcc when present, else inferred from the callsign.

    Enrichment lags: every one of the 14 most recent contacts in the live log
    had dxcc null, so the recent board would show a blank country on every
    row without this fallback.
    """
    if entity_code is not None:
        return entity_code
    return dxcc.entity_from_call(call)


def place_name(entity_code, state, call=None):
    """What the recent board bolds: state for NA entities, country otherwise."""
    code = resolve_entity(entity_code, call)
    abbr = str(state).strip().upper() if not is_absent(state) else None

    if code in STATE_ENTITIES and abbr:
        return STATES.get(abbr, abbr)
    # No entity at all, but a recognised state abbreviation is itself strong
    # evidence: WRL only populates `state` for entities that have states.
    if code is None and abbr in STATES:
        return STATES[abbr]
    return dxcc.country(code)


# ---------------------------------------------------------------- fetch

class CollectError(Exception):
    pass


def build_session(key):
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "wrl-boards-collect/1.0 (K8JKU)",
    })
    return s


def backoff(response, attempt):
    """Sleep before the next attempt.

    A server that sends Retry-After knows when it will be ready, so that wins
    over any curve we could invent. Otherwise exponential, with jitter so that
    a retry never lands on a round second alongside everyone else's, and
    capped: the job has a timeout and a long sleep helps nobody.
    """
    raw = response.headers.get("Retry-After") if response is not None else None
    if raw:
        try:
            wait = max(0, min(int(raw), BACKOFF_CAP))
        except ValueError:
            wait = None       # HTTP-date form; not worth parsing, use the curve
        if wait is not None:
            time.sleep(wait)
            return
    time.sleep(min(2 ** attempt, BACKOFF_CAP) + random.random())


def get_page(session, params):
    """One GET with retry. Honours Retry-After; never guesses a fixed timer.

    Retried, because none of it means anything is actually wrong:

      a dropped connection,
      a 429,
      a 5xx from the gateway in front of the API,
      a body that will not parse as JSON.

    That last one is the same event as the third. The gateway answers with an
    HTML error page, so the 502 arrives as a decode failure rather than as a
    status the old code looked at, and one blip mid-pagination killed the whole
    run. A JSON API that answers with non-JSON is infrastructure talking, not
    the API, and infrastructure recovers.

    Not retried: 4xx other than 429. A revoked key or a malformed request fails
    identically every time, so retrying only delays the report.
    """
    url = BASE_URL + "/v1/contacts"
    problem = "no attempt made"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=TIMEOUT)
        except requests.exceptions.RequestException as exc:
            problem = f"network failure: {exc}"
            if attempt < MAX_RETRIES:
                print(f"    {problem} — retrying ({attempt}/{MAX_RETRIES - 1})")
                backoff(None, attempt)
            continue

        try:
            body = r.json()
        except ValueError:
            body = None

        err = body.get("error") if isinstance(body, dict) else None
        if r.status_code < 400 and body is not None and not err:
            return body

        rid = r.headers.get("X-Request-Id")
        if r.status_code == 429:
            problem = f"rate limited (HTTP 429) X-Request-Id={rid}"
        elif body is None:
            problem = (f"non-JSON response (HTTP {r.status_code}) "
                       f"X-Request-Id={rid}")
        else:
            code = err.get("code") if isinstance(err, dict) else None
            problem = f"HTTP {r.status_code} code={code} X-Request-Id={rid}"

        transient = (r.status_code == 429 or r.status_code in RETRY_STATUS
                     or body is None)
        if not transient:
            raise CollectError(problem)

        if attempt < MAX_RETRIES:
            print(f"    {problem} — retrying ({attempt}/{MAX_RETRIES - 1})")
            backoff(r, attempt)

    raise CollectError(f"{problem} — gave up after {MAX_RETRIES} attempts")


def fetch_all(session, since=None, stop_before=None):
    """Page the log newest-first until nextCursor is null. Returns all rows.

    `since` is sent to the API. `stop_before` additionally stops paging once
    rows older than it appear, so a recent pull stays cheap even if the
    server ignores or misreads the filter. Results are newest-first, which
    makes that safe.

    Any failure raises. A partial pull must never reach the aggregation step:
    a stale board is fine, a wiped board is not.
    """
    rows, cursor, page = [], None, 0
    while True:
        page += 1
        params = {"limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor       # opaque — pass back verbatim
        if since:
            params["since"] = since

        body = get_page(session, params)
        data = body.get("data")
        batch = data if isinstance(data, list) else (
            (data or {}).get("contacts") or (data or {}).get("items") or [])
        rows.extend(batch)

        if page % 10 == 1 or not batch:
            print(f"    page {page}: {len(rows)} contacts so far")

        if stop_before and batch:
            oldest = parse_ts(batch[-1].get("timestamp"))
            if oldest and oldest < stop_before:
                break

        cursor = (body.get("meta") or {}).get("nextCursor")
        if not cursor or not batch:
            break
        time.sleep(PAGE_SLEEP)
    return rows


# ---------------------------------------------------------------- aggregate

class Aggregate:
    """One pass over the log, building everything all three boards need.

    The heatmap and the median-distance panel both need QSOs grouped rather
    than merely counted, so nothing here can be reduced to a running total.
    """

    def __init__(self, contacts, now):
        self.now = now
        self.today = now.date()
        self.fresh_cutoff = now - timedelta(days=FRESH_DAYS)

        self.total = 0
        self.excluded = 0
        self.duplicates = 0
        self.no_timestamp = 0
        self.inferred_entities = 0

        self.bands = Counter()
        self.modes = Counter()
        self.categories = Counter()
        self.by_year = Counter()
        self.by_date = Counter()
        self.entities = set()
        self.grids = set()

        self.total_miles = 0.0
        self.with_distance = 0
        # Hour-by-band counts. No board renders these since the career
        # heatmap came out; kept because it is cheap and the recent
        # board is the obvious next home for it.
        self.heat = defaultdict(lambda: [0] * 24)
        self.band_distances = defaultdict(list)

        self.furthest = None          # (miles, call, date)
        self.first_qso = None
        self.grid_first_seen = {}     # grid4 -> (datetime, call, entity)
        self.entity_first_seen = {}   # code  -> datetime
        self.fresh_grids = set()
        self.home_grids = Counter()

        self.rows = []                # normalised, chronological

        self._run(contacts)

    def _run(self, contacts):
        prepared = []
        for c in contacts:
            if c.get("logbookId") in EXCLUDED_LOGBOOKS:
                self.excluded += 1
                continue
            ts = parse_ts(c.get("timestamp"))
            if ts is None:
                self.no_timestamp += 1
                continue
            prepared.append((ts, c))

        # "First ever worked" is only meaningful in chronological order, and
        # the API returns newest-first.
        prepared.sort(key=lambda p: (p[0], str(p[1].get("id"))))

        for ts, c in prepared:
            self.total += 1
            if c.get("isDuplicate"):
                self.duplicates += 1

            if self.first_qso is None or ts < self.first_qso:
                self.first_qso = ts

            day = ts.date()
            self.by_date[day] += 1
            self.by_year[day.year] += 1

            blabel = band_label(c.get("band"))
            if blabel:
                self.bands[blabel] += 1

            mode = normalise_mode(c.get("mode"))
            if mode:
                self.modes[mode] += 1
                self.categories[mode_category(mode)] += 1

            miles = None
            d = c.get("distance")
            if d is not None:
                try:
                    miles = float(d) * KM_TO_MILES
                except (TypeError, ValueError):
                    miles = None
            if miles is not None and miles >= 0:
                self.total_miles += miles
                self.with_distance += 1
                if blabel:
                    self.band_distances[blabel].append(miles)
                if self.furthest is None or miles > self.furthest[0]:
                    self.furthest = (miles, c.get("call"), day)

            # Count the resolved entity, so a contact the API has not enriched
            # yet still counts toward DXCC and continents.
            code = c.get("dxcc")
            if code is None:
                code = dxcc.entity_from_call(c.get("call"))
                if code is not None:
                    self.inferred_entities += 1
            if code is not None:
                self.entities.add(code)
                self.entity_first_seen.setdefault(code, ts)

            g4 = grid4(c.get("gridsquare"))
            if g4 and grid_center(g4):
                self.grids.add(g4)
                if g4 not in self.grid_first_seen:
                    self.grid_first_seen[g4] = (ts, c.get("call"), code)
                if ts >= self.fresh_cutoff:
                    self.fresh_grids.add(g4)

            mg = grid4(c.get("myGridsquare"))
            if mg:
                self.home_grids[mg] += 1

            if blabel:
                self.heat[blabel][ts.hour] += 1

            self.rows.append({
                "ts": ts, "call": c.get("call"), "band": blabel, "mode": mode,
                "miles": miles, "dxcc": code, "grid": c.get("gridsquare"),
                "grid4": g4, "name": c.get("name"), "state": c.get("state"),
                "my_grid": c.get("myGridsquare"),
            })

    # -- derived ------------------------------------------------------

    def best_day(self):
        if not self.by_date:
            return None
        day, n = max(self.by_date.items(), key=lambda kv: (kv[1], kv[0]))
        return {"count": n, "date": day.isoformat()}

    def longest_streak(self):
        """Longest run of consecutive UTC days with at least one QSO."""
        if not self.by_date:
            return 0
        days = sorted(self.by_date)
        best = run = 1
        for prev, cur in zip(days, days[1:]):
            run = run + 1 if (cur - prev).days == 1 else 1
            best = max(best, run)
        return best

    def home_grid(self):
        return self.home_grids.most_common(1)[0][0] if self.home_grids else None


# ---------------------------------------------------------------- builders

def month_series(by_date, first, now):
    """Contiguous months from the first QSO to now, empty ones included.

    A quiet month has to render as a short bar, not a missing one. Dropping
    empty months would slide the remaining bars together and quietly redraw a
    summer off the air as continuous activity.
    """
    if not first:
        return []
    counts = Counter()
    for day, n in by_date.items():
        counts[(day.year, day.month)] += n

    out, y, m = [], first.year, first.month
    while (y, m) <= (now.year, now.month):
        out.append({"month": f"{y:04d}-{m:02d}", "count": counts.get((y, m), 0)})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def build_dx_ladder(rows, limit=DX_LADDER):
    """The furthest contacts, one row per station.

    Deduplicated by callsign, keeping each station's best. Working the same
    Australian eight times is one achievement, and eight identical rows is a
    list, not a ladder.

    Row 0 is by definition the single furthest contact ever made, which is why
    `records` no longer carries a separate `furthest`.
    """
    best = {}
    for r in rows:
        call = (r.get("call") or "").strip().upper()
        if not call or r.get("miles") is None:
            continue
        if call not in best or r["miles"] > best[call]["miles"]:
            best[call] = r

    top = sorted(best.values(), key=lambda r: -r["miles"])[:limit]
    return [{
        "call": r["call"],
        "country": dxcc.country_for(r["dxcc"], r["call"], default=""),
        "miles": round(r["miles"]),
        "date": r["ts"].date().isoformat(),
        "band": r["band"],
        "mode": r["mode"],
    } for r in top]


def build_career(agg):
    months = month_series(agg.by_date, agg.first_qso, agg.now)
    busiest = max(months, key=lambda m: m["count"], default=None)
    return {
        "generated": agg.now.isoformat(timespec="seconds"),
        "total_qsos": agg.total,
        "qsos_this_year": agg.by_year.get(agg.now.year, 0),
        "first_qso_date": agg.first_qso.date().isoformat() if agg.first_qso else None,
        "dxcc_count": len(agg.entities),
        "grid_count": len(agg.grids),
        "continent_count": len(dxcc.continents_worked(agg.entities)),
        "continent_total": len(dxcc.WAC_CONTINENTS),
        "total_distance": round(agg.total_miles),
        "bands": [{"band": b, "count": n} for b, n in agg.bands.most_common()],
        "modes": [{"mode": m, "count": n, "category": mode_category(m)}
                  for m, n in agg.modes.most_common()],
        "mode_groups": [{"category": c, "count": n}
                        for c, n in agg.categories.most_common()],
        "records": {
            # No "furthest" here any more: it is dx_ladder[0], and one fact
            # with two sources in the same file drifts the moment one changes.
            "best_day": agg.best_day(),
            "longest_streak": agg.longest_streak(),
            "active_days": len(agg.by_date),
            "busiest_month": (busiest if busiest and busiest["count"] else None),
        },
        "dx_ladder": build_dx_ladder(agg.rows),
        "by_month": months,
        "by_year": [{"year": y, "count": agg.by_year[y]}
                    for y in sorted(agg.by_year)],
    }


def build_grids(agg):
    points = []
    for g in sorted(agg.grids):
        c = grid_center(g)
        if not c:
            continue
        points.append({"grid": g, "lat": c[0], "lon": c[1],
                       "fresh": g in agg.fresh_grids})

    new_30d = sum(1 for g, (ts, _, _) in agg.grid_first_seen.items()
                  if ts >= agg.fresh_cutoff)

    medians = []
    for b, ds in agg.band_distances.items():
        if len(ds) >= MEDIAN_MIN_SAMPLES:
            medians.append({"band": b, "miles": round(median(ds)),
                            "samples": len(ds)})
    medians.sort(key=lambda m: -m["miles"])

    newest = sorted(agg.grid_first_seen.items(), key=lambda kv: kv[1][0],
                    reverse=True)[:NEWEST_GRIDS]

    home = agg.home_grid()
    home_c = grid_center(home) if home else None

    # Arcs originate at the QSO's OWN myGridsquare, not a fixed home point.
    # 7.8% of the log was worked portable, and an arc drawn from Michigan for
    # a contact made from Florida is simply a wrong line on a map.
    def arc_for(r):
        a = grid_center(r["my_grid"])
        b = grid_center(r["grid"]) or grid_center(r["grid4"])
        if not a or not b or r["miles"] is None:
            return None
        return {"from": [a[1], a[0]], "to": [b[1], b[0]],
                "call": r["call"], "miles": round(r["miles"])}

    recent = [r for r in agg.rows if r["ts"] >= agg.fresh_cutoff]
    recent.sort(key=lambda r: -(r["miles"] or 0))
    arcs, seen_calls = [], set()
    for r in recent:
        a = arc_for(r)
        if a and a["call"] not in seen_calls:
            arcs.append(a)
            seen_calls.add(a["call"])
        if len(arcs) >= ARC_COUNT:
            break
    if len(arcs) < ARC_COUNT:
        # A quiet month should still draw a map with arcs on it.
        rest = sorted(agg.rows, key=lambda r: -(r["miles"] or 0))
        for r in rest:
            a = arc_for(r)
            if a and a["call"] not in seen_calls:
                arcs.append(a)
                seen_calls.add(a["call"])
            if len(arcs) >= ARC_COUNT:
                break

    return {
        "generated": agg.now.isoformat(timespec="seconds"),
        "grid_count": len(agg.grids),
        "field_count": len({g[:2] for g in agg.grids}),
        "field_total": 324,
        "dxcc_count": len(agg.entities),
        "new_30d": new_30d,
        "home": ({"grid": home, "lat": home_c[0], "lon": home_c[1]}
                 if home_c else None),
        "points": points,
        "arcs": arcs,
        "median_distance": medians,
        "newest_grids": [
            {"grid": g, "call": call, "country": dxcc.country_for(code, call),
             "date": ts.date().isoformat()}
            for g, (ts, call, code) in newest
        ],
    }


def locate(row):
    """Best known position for a contact, as (lat, lon, precision).

    Three sources, in descending order of what they actually claim:

      grid   the other station's own locator, good to a few miles
      state  a state or province centroid: "somewhere in Colorado"
      dxcc   an entity centroid: "somewhere in Japan"

    The precision travels with the point so the board can draw an honest mark.
    Only a grid earns an arc; a centroid gets a bubble, because an arc drawn to
    a centroid is a line to a place nobody was standing.
    """
    c = grid_center(row.get("grid")) or grid_center(row.get("grid4"))
    if c:
        return (c[0], c[1], "grid")

    ab = (row.get("state") or "").strip().upper()
    if ab in STATE_CENTERS:
        lat, lon = STATE_CENTERS[ab]
        return (lat, lon, "state")

    c = dxcc.center(row.get("dxcc"))
    if c:
        return (c[0], c[1], "dxcc")
    return None


def inside(bbox, lon, lat):
    (w, s), (e, n) = bbox
    return w <= lon <= e and s <= lat <= n


def region_frame(origins):
    """Frame for the regional map.

    North America by default, and pixel-identical day to day, which matters on
    a wall: a map that reframes itself every half hour is a map you stop being
    able to read. It only moves when the site most of the window's QSOs came
    from is somewhere else entirely, which is the trip-abroad case.
    """
    if not origins:
        return {"mode": "na", "bbox": NA_FRAME}

    main = max(origins, key=lambda o: o["count"])
    if inside(NA_FRAME, main["lon"], main["lat"]):
        return {"mode": "na", "bbox": NA_FRAME}

    px, py = FRAME_PAD
    return {"mode": "fit", "bbox": [
        [round(main["lon"] - px, 2), round(max(-85.0, main["lat"] - py), 2)],
        [round(main["lon"] + px, 2), round(min(85.0, main["lat"] + py), 2)]]}


def build_map(agg, rows, bands_order):
    """Arcs, bubbles and operating sites for the two maps.

    Everything carries a `region` flag: true items belong on the regional map,
    false ones on the world map, which shows nothing but the DX so it is not a
    smaller, worse copy of the map beside it.
    """
    # Operating sites, most-used first. A contact with no myGridsquare is
    # assumed to be from the busiest site rather than dropped.
    sites = {}
    for r in rows:
        c = grid_center(r.get("my_grid"))
        if not c:
            continue
        key = str(r["my_grid"]).strip().upper()
        o = sites.setdefault(key, {"grid": key, "lat": round(c[0], 3),
                                   "lon": round(c[1], 3), "count": 0})
        o["count"] += 1
    origins = sorted(sites.values(), key=lambda o: -o["count"])
    index = {o["grid"]: i for i, o in enumerate(origins)}
    frame = region_frame(origins)

    band_rank = {b: i for i, b in enumerate(bands_order)}
    arcs, bubbles = [], {}
    for r in rows:
        pos = locate(r)
        if not pos:
            continue
        lat, lon, prec = pos
        home = str(r.get("my_grid") or "").strip().upper()
        oi = index.get(home, 0 if origins else None)

        if prec == "grid" and oi is not None:
            arcs.append({
                "o": oi,
                "lat": round(lat, 2), "lon": round(lon, 2),
                "band": r["band"],
                "region": inside(frame["bbox"], lon, lat),
            })
        else:
            # Many contacts share one centroid, so they are counted into a
            # single bubble rather than stacked as identical invisible dots.
            key = (round(lat, 2), round(lon, 2))
            b = bubbles.setdefault(key, {
                "lat": key[0], "lon": key[1], "count": 0,
                "region": inside(frame["bbox"], lon, lat),
            })
            b["count"] += 1

    arcs.sort(key=lambda a: band_rank.get(a["band"], len(band_rank)))
    return {
        "window_days": MAP_WINDOW_DAYS,
        "frame": frame,
        "origins": origins,
        "arcs": arcs,
        "bubbles": sorted(bubbles.values(), key=lambda b: -b["count"]),
        "bands": bands_order,
    }


def build_recent(agg, known=None):
    """known: {"entities": set, "grids": set} from the last full run.

    In full mode the whole log is present, so "first ever worked" is decided
    directly. In recent mode only a 45-day window was pulled, and a first
    must be judged against what the nightly run already knew.
    """
    now = agg.now
    today = now.date()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    map_start = now - timedelta(days=MAP_WINDOW_DAYS)
    counts = {
        "today": sum(1 for r in agg.rows if r["ts"] >= day_start),
        "week": sum(1 for r in agg.rows if r["ts"] >= now - timedelta(days=7)),
        "window": sum(1 for r in agg.rows if r["ts"] >= map_start),
        "month": sum(1 for r in agg.rows if r["ts"] >= now - timedelta(days=30)),
    }

    # The maps plot the whole window, not just the rows the list shows.
    map_rows = [r for r in agg.rows if r["ts"] >= map_start]
    band_counts = Counter(r["band"] for r in map_rows if r["band"])
    bands_order = [b for b, _ in band_counts.most_common()]

    # One entry per day across the map window, so the strip under the tiles
    # covers the same span the maps do.
    daily = []
    for i in range(MAP_WINDOW_DAYS - 1, -1, -1):
        d = today - timedelta(days=i)
        daily.append({
            "label": "Today" if i == 0 else d.strftime("%a"),
            "dom": d.day,
            "date": d.isoformat(),
            "count": agg.by_date.get(d, 0),
        })

    # 24 buckets ending at the current hour, oldest first.
    hourly = [0] * 24
    window_start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
    for r in agg.rows:
        if r["ts"] >= window_start:
            idx = int((r["ts"] - window_start).total_seconds() // 3600)
            if 0 <= idx < 24:
                hourly[idx] += 1

    known_e = (known or {}).get("entities")
    known_g = (known or {}).get("grids")

    recent_rows = agg.rows[-RECENT_CONTACTS:][::-1]
    today_iso = today.isoformat()
    contacts = []
    for r in recent_rows:
        code = r["dxcc"]
        first_e = agg.entity_first_seen.get(code)
        first_g = agg.grid_first_seen.get(r["grid4"], (None,))[0]

        if known_e is None:
            new_dxcc = bool(code is not None and first_e == r["ts"])
            new_grid = bool(r["grid4"] and first_g == r["ts"])
        else:
            # First in this window AND absent from what the full run knew.
            new_dxcc = bool(code is not None and first_e == r["ts"]
                            and code not in known_e)
            new_grid = bool(r["grid4"] and first_g == r["ts"]
                            and r["grid4"] not in known_g)

        d = r["ts"].date()
        contacts.append({
            "time": r["ts"].strftime("%H:%M"),
            "date": d.isoformat(),
            # Empty for today, so only rows that need explaining carry a day.
            "day": "" if d.isoformat() == today_iso else d.strftime("%a"),
            "call": r["call"],
            "country": dxcc.country_for(code, r["call"]),
            "place": place_name(code, r["state"], r["call"]),
            "state": (r["state"] or "").strip().upper() or None,
            "grid": (r["grid"] or "").strip() or None,
            "name": (r["name"] or "").strip() or None,
            "band": r["band"],
            "mode": r["mode"],
            "category": mode_category(r["mode"]) if r["mode"] else None,
            "distance": round(r["miles"]) if r["miles"] is not None else None,
            "new_dxcc": new_dxcc,
            "new_grid": new_grid,
        })

    last = agg.rows[-1]["ts"] if agg.rows else None
    return {
        "generated": now.isoformat(timespec="seconds"),
        "today": counts["today"],
        "week": counts["week"],
        "window": counts["window"],
        "month": counts["month"],
        "window_days": MAP_WINDOW_DAYS,
        # Minutes since the last QSO, so the board can say how cold the log is
        # instead of leaving a reader to work it out from a bare timestamp.
        "since_last_min": (int((now - last).total_seconds() // 60) if last else None),
        "daily": daily,
        "hourly": hourly,
        "now_hour": now.hour,
        "contacts": contacts,
        "map": build_map(agg, map_rows, bands_order),
    }


KNOWN_PATH = os.path.join(OUT_DIR, "known.json")


def build_known(agg, previous=None, source="full"):
    """Entity and grid sets, so the 30-minute recent run can flag firsts
    without pulling the whole log.

    A recent run only saw 45 days, so it must MERGE into what it was given
    rather than replace it. Replacing would forget everything older and make
    every entity look new again on the following run.
    """
    entities = {e for e in agg.entities if e is not None}
    grids = set(agg.grids)
    if previous:
        entities |= set(previous.get("entities") or ())
        grids |= set(previous.get("grids") or ())
    return {
        "generated": agg.now.isoformat(timespec="seconds"),
        "source": source,
        "entities": sorted(entities),
        "grids": sorted(grids),
    }


def read_known():
    """Previous known sets, or None if absent/unreadable."""
    try:
        with open(KNOWN_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    ents = d.get("entities")
    grids = d.get("grids")
    if not isinstance(ents, list) or not isinstance(grids, list):
        return None
    return {"entities": set(ents), "grids": set(grids),
            "generated": d.get("generated"), "source": d.get("source"),
            "raw": d}


# ---------------------------------------------------------------- validate

def previous_total():
    path = os.path.join(OUT_DIR, "career.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("total_qsos")
    except (OSError, ValueError):
        return None


def validate(agg, force):
    """Abort before touching any file. A stale board beats a wiped one."""
    problems = []
    if agg.total < MIN_SANE_CONTACTS:
        problems.append(f"only {agg.total} contacts after filtering "
                        f"(minimum {MIN_SANE_CONTACTS})")
    if agg.first_qso is None:
        problems.append("no contact carried a usable timestamp")

    prev = previous_total()
    if prev and agg.total < prev * (1 - MAX_SHRINK):
        problems.append(
            f"log shrank from {prev:,} to {agg.total:,} "
            f"({100 * (prev - agg.total) / prev:.1f}%). Pass --force if a "
            f"deletion really happened.")

    if problems and force:
        print("\n  validation problems overridden by --force:")
        for p in problems:
            print(f"    ! {p}")
        return True
    if problems:
        print("\nVALIDATION FAILED — nothing written:", file=sys.stderr)
        for p in problems:
            print(f"  ! {p}", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------- write

def write_json(path, payload):
    """json.dump only, UTF-8, no BOM. Written to a temp file and moved into
    place so a crash mid-write cannot leave a truncated board file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
    os.replace(tmp, path)
    return os.path.getsize(path)


# ---------------------------------------------------------------- report

def report(agg, career, grids, recent):
    print("\n" + "=" * 70)
    print("COLLECTED")
    print("=" * 70)
    print(f"  contacts kept       : {agg.total:,}")
    print(f"  excluded (logbook)  : {agg.excluded:,}")
    print(f"  no usable timestamp : {agg.no_timestamp:,}")
    print(f"  flagged isDuplicate : {agg.duplicates:,}  (counted, not dropped)")
    print(f"  first QSO           : {career['first_qso_date']}")
    print(f"  entities / grids    : {career['dxcc_count']} / {career['grid_count']}"
          f"   ({agg.inferred_entities:,} entities inferred from callsign, "
          f"API had not enriched them)")
    print(f"  continents          : {career['continent_count']}/{career['continent_total']}")
    print(f"  distance total      : {career['total_distance']:,} mi "
          f"(from {agg.with_distance:,} contacts)")
    ladder = career["dx_ladder"]
    if ladder:
        f = ladder[0]
        print(f"  furthest            : {f['miles']:,} mi  {f['call']}  {f['date']}")
        print(f"  dx ladder           : {len(ladder)} stations, "
              f"down to {ladder[-1]['miles']:,} mi")
    b = career["records"]["best_day"]
    if b:
        print(f"  best day            : {b['count']} on {b['date']}")
    print(f"  longest streak      : {career['records']['longest_streak']} days")
    print(f"  active days         : {career['records']['active_days']:,}")
    bm = career["records"]["busiest_month"]
    if bm:
        print(f"  busiest month       : {bm['month']}  {bm['count']:,}")
    print(f"  months tracked      : {len(career['by_month'])}")
    print(f"  bands / modes       : {len(career['bands'])} / {len(career['modes'])}")
    print(f"  map points          : {len(grids['points']):,}  "
          f"fresh {sum(1 for p in grids['points'] if p['fresh'])}  "
          f"new in {FRESH_DAYS}d {grids['new_30d']}")
    print(f"  map arcs            : {len(grids['arcs'])}")
    print(f"  recent today/week   : {recent['today']} / {recent['week']}")

    unknown = sorted({m["mode"] for m in career["modes"]
                      if m["category"] == "dig" and m["mode"] not in KNOWN_DIGITAL})
    if unknown:
        print(f"\n  ! modes defaulted to Digital without being listed as digital:")
        print(f"    {', '.join(unknown)}")
        print(f"    Check these are really digital, then add them to "
              f"KNOWN_DIGITAL.")

    missing = sorted({r["dxcc"] for r in agg.rows
                      if r["dxcc"] is not None and dxcc.country(r["dxcc"]) is None})
    if missing:
        print(f"\n  ! entity codes absent from data/dxcc.json: {missing}")


# ---------------------------------------------------------------- main

def run_recent(session, now, args):
    """Pull the trailing window only and refresh recent.json + known.json.

    Never touches career.json or grids.json: those describe the whole log and
    a 45-day window cannot produce them.
    """
    window_start = now - timedelta(days=RECENT_WINDOW_DAYS)
    since = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"  pulling contacts since {since} ...")

    try:
        raw = fetch_all(session, since=since, stop_before=window_start)
    except CollectError as exc:
        print(f"\nFETCH FAILED — nothing written: {exc}", file=sys.stderr)
        return 1
    print(f"  pulled {len(raw):,} contacts")

    agg = Aggregate(raw, now)

    # The panel shows the last 14 contacts. A quiet window would leave it
    # nearly empty, which reads as a broken board rather than a quiet month,
    # so widen once and try again.
    if agg.total < RECENT_CONTACTS:
        wide_start = now - timedelta(days=RECENT_FALLBACK_DAYS)
        print(f"  only {agg.total} contacts in {RECENT_WINDOW_DAYS} days; "
              f"widening to {RECENT_FALLBACK_DAYS} to fill the contact list")
        try:
            raw = fetch_all(session,
                            since=wide_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            stop_before=wide_start)
            agg = Aggregate(raw, now)
        except CollectError as exc:
            print(f"\nFETCH FAILED — nothing written: {exc}", file=sys.stderr)
            return 1

    if raw:
        oldest = min((parse_ts(c.get("timestamp")) for c in raw
                      if parse_ts(c.get("timestamp"))), default=None)
        if oldest and oldest < now - timedelta(days=RECENT_FALLBACK_DAYS * 2):
            print(f"  ! the API returned contacts from {oldest.date()}, far "
                  f"outside the requested window — the `since` filter may be "
                  f"ignored. Results are still correct, just more expensive.")

    known = read_known()
    if known is None:
        print("  ! no readable data/latest/known.json. New DXCC and new grid "
              "flags will be suppressed this run; the nightly full run "
              "rebuilds the sets.")
    elif known.get("generated"):
        print(f"  known sets from {known['generated']} "
              f"({len(known['entities'])} entities, {len(known['grids'])} grids)")

    recent = build_recent(agg, known)
    merged = build_known(agg, previous=known.get("raw") if known else None,
                         source="recent")

    print("\n" + "=" * 70)
    print("COLLECTED (recent window)")
    print("=" * 70)
    print(f"  contacts in window  : {agg.total:,}")
    print(f"  excluded (logbook)  : {agg.excluded:,}")
    print(f"  today / week / month: {recent['today']} / {recent['week']} / {recent['month']}")
    print(f"  contacts listed     : {len(recent['contacts'])}")
    print(f"  new dxcc flagged    : {sum(1 for c in recent['contacts'] if c['new_dxcc'])}")
    print(f"  new grid flagged    : {sum(1 for c in recent['contacts'] if c['new_grid'])}")
    print(f"  known sets now      : {len(merged['entities'])} entities, "
          f"{len(merged['grids'])} grids")

    if agg.total == 0:
        print("\n  ! the window is empty. Writing zeroes is correct if you "
              "have not operated; pass --force if that is genuinely the case.")
        if not args.force:
            print("\nVALIDATION FAILED — nothing written.", file=sys.stderr)
            return 1

    if args.dry_run:
        print("\n  --dry-run: nothing written")
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    print()
    for name, payload in (("recent.json", recent), ("known.json", merged)):
        size = write_json(os.path.join(OUT_DIR, name), payload)
        print(f"  wrote {OUT_DIR}/{name:12} {size:>9,} bytes")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Collect WRL log into board JSON.")
    ap.add_argument("--mode", choices=["full", "recent"], default="full")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="write even if validation objects")
    args = ap.parse_args()

    # In CI stdout is a pipe and therefore block-buffered, while stderr is not.
    # The failure line then lands above the progress it followed, which reads
    # as if the run failed before it started. Line buffering keeps the log in
    # the order the run actually happened.
    sys.stdout.reconfigure(line_buffering=True)

    key = os.environ.get("WRL_API_KEY", "").strip()
    if not key:
        print("ERROR: WRL_API_KEY is not set.", file=sys.stderr)
        print('  PowerShell: $env:WRL_API_KEY = "wrl_live_..."', file=sys.stderr)
        print("  bash:       export WRL_API_KEY=wrl_live_...", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    print(f"WRL collect — mode={args.mode}  {now.isoformat(timespec='seconds')}")
    print(f"  dxcc table: {dxcc.count()} entities")

    session = build_session(key)

    if args.mode == "recent":
        return run_recent(session, now, args)

    print("  pulling entire log ...")
    try:
        raw = fetch_all(session)
    except CollectError as exc:
        print(f"\nFETCH FAILED — nothing written: {exc}", file=sys.stderr)
        return 1

    print(f"  pulled {len(raw):,} contacts")

    agg = Aggregate(raw, now)
    career = build_career(agg)
    grids = build_grids(agg)
    recent = build_recent(agg)
    known = build_known(agg, source="full")

    report(agg, career, grids, recent)

    ok = validate(agg, args.force)

    # A preview must always complete. Reporting what validation WOULD reject
    # is the main reason to run one.
    if args.dry_run:
        print("\n  --dry-run: nothing written"
              + ("" if ok else "  (validation would have blocked this run)"))
        return 0

    if not ok:
        return 1

    os.makedirs(OUT_DIR, exist_ok=True)
    print()
    for name, payload in (("career.json", career), ("grids.json", grids),
                          ("recent.json", recent), ("known.json", known)):
        size = write_json(os.path.join(OUT_DIR, name), payload)
        print(f"  wrote {OUT_DIR}/{name:12} {size:>9,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
