"""callbook.py — license-address positions for US calls, via callook.info.

Most contacts in the log carry no gridsquare, which leaves the map to guess at
a state or country centroid. callook.info serves the FCC license address for
any US call, free and keyless, and that address is almost always closer to the
station than the middle of its state.

Map placement only. A license address is not a worked grid, so nothing here
ever counts toward grid totals.

Results are cached in data/latest/callook.json so each call is asked about once
in a long while, not every 30 minutes. The workflows already commit that folder.
"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

URL = "https://callook.info/{}/json"
CACHE_PATH = os.path.join("data", "latest", "callook.json")

# FCC-licensed entities: USA, Alaska, Hawaii, Puerto Rico, US Virgin Is,
# Guam, American Samoa, Northern Marianas.
US_ENTITIES = frozenset({291, 6, 110, 202, 285, 103, 9, 166})

HIT_DAYS = 365        # people move, slowly
MISS_DAYS = 30        # a brand-new license may not be in the database yet
MAX_LOOKUPS = 150     # per run; the map window is far smaller than this
SLEEP = 0.25          # between requests, to be a polite free service user
TIMEOUT = 8
GIVE_UP_AFTER = 3     # consecutive failures before the run stops asking

# "CITY, ST 12345" or "CITY, ST 12345-6789"
_STATE_RE = re.compile(r",\s*([A-Z]{2})\s+\d{5}")


class Callbook:
    def __init__(self, path=CACHE_PATH, session=None):
        self.path = path
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent", "wrl-boards-collect/1.0 (K8JKU)")
        self.cache = {}
        self.lookups = 0
        self.failures = 0
        self.dirty = False
        try:
            with open(path, encoding="utf-8") as fh:
                self.cache = json.load(fh)
        except (OSError, ValueError):
            self.cache = {}

    def position(self, call, entity_code):
        """(lat, lon, state) from the license address, or None."""
        if entity_code not in US_ENTITIES or not call:
            return None
        call = str(call).strip().upper()
        # A portable or mobile suffix means the station is not at home.
        if not call or "/" in call:
            return None

        e = self.cache.get(call)
        if e is None or self._stale(e):
            fresh = self._fetch(call)
            if fresh is not None:
                e = self.cache[call] = fresh
                self.dirty = True
        if not e or e.get("lat") is None:
            return None
        return (e["lat"], e["lon"], e.get("st"))

    def _stale(self, e):
        try:
            t = datetime.fromisoformat(e["t"]).replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            return True
        days = HIT_DAYS if e.get("lat") is not None else MISS_DAYS
        return datetime.now(timezone.utc) - t > timedelta(days=days)

    def _fetch(self, call):
        """A cache entry, or None when the answer could not be had this run."""
        if self.lookups >= MAX_LOOKUPS or self.failures >= GIVE_UP_AFTER:
            return None
        if self.lookups:
            time.sleep(SLEEP)
        self.lookups += 1
        try:
            r = self.session.get(URL.format(call), timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            self.failures += 1
            return None
        self.failures = 0

        today = datetime.now(timezone.utc).date().isoformat()
        status = data.get("status")
        if status == "INVALID":
            return {"lat": None, "t": today}
        if status != "VALID":
            return None        # UPDATING and friends: ask again next run
        loc = data.get("location") or {}
        try:
            lat, lon = float(loc["latitude"]), float(loc["longitude"])
        except (KeyError, TypeError, ValueError):
            return {"lat": None, "t": today}
        m = _STATE_RE.search((data.get("address") or {}).get("line2") or "")
        return {"lat": round(lat, 3), "lon": round(lon, 3),
                "st": m.group(1) if m else None, "t": today}

    def save(self):
        if not self.dirty:
            return None
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(self.cache, fh, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
            fh.write("\n")
        os.replace(tmp, self.path)
        return os.path.getsize(self.path)
