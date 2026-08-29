# World Radio League Boards — Build Handoff

Handoff for Claude Code. Everything needed to build three DakBoard display boards
driven by the World Radio League logbook API.

**Operator:** James Mills, K8JKU, Clarkston MI, grid EN82, CQ Zone 4
**Proposed repo:** `jmills06/wrl-boards`
**Reference mockups:** `mockups/wrl-career-dashboard.html`, `mockups/wrl-recent-activity.html`, `mockups/wrl-grid-map.html`

The three mockup files are complete, self-contained HTML with hardcoded sample data.
They are the visual target. Build the real boards by replacing the sample data with
fetches against `data/latest/*.json`, keeping the layout and styling as-is.

---

## 1. What we are building

Three non-interactive portrait boards, 1080x1920, hosted on GitHub Pages, rendered
on a DakBoard CPU v5 (Pi 5B).

| Board | File | Purpose | Refresh |
|---|---|---|---|
| Career dashboard | `career.html` | Lifetime totals, bands, modes, when-you-operate heatmap, contacts by year | Nightly |
| Recent activity | `recent.html` | Today / week / 30 days, 7-day chart, 24-hour strip, last 14 contacts | Every 30 min |
| Grid map | `grids.html` | World map of grids worked, median distance by band, newest grids | Nightly |

---

## 2. The API

Base URL: `https://api.worldradioleague.com`
Spec: `https://api.worldradioleague.com/v1/openapi.json`
Plain-text summary: `https://api.worldradioleague.com/v1/llms.txt`

### Auth

API key generated in WRL under **Integrations > Developer API**. Shown once, stored
hashed. One key covers the whole account. Requires a paid membership.

Send as `Authorization: Bearer wrl_live_...` or `X-API-Key`.

Store as GitHub Actions secret **`WRL_API_KEY`**.

### Critical constraint: no CORS

The endpoints deliberately send no CORS headers. A browser cannot call them. An API
key in front-end code is a leaked key. **The boards never touch the WRL API.** The
collector runs in a GitHub Action, holds the key, and writes static JSON. This is the
same pattern as the POTA Achievement Board and the school menu board.

### Endpoints we use

Only one matters for reading:

```
GET /v1/contacts?limit=100&cursor=<opaque>
```

Optional filters: `logbookId`, `call`, `mode`, `band`, `since`, `until`.
Returns newest first, ordered by timestamp descending, tiebroken by id, so paging
never skips or repeats a row.

Also call once at setup:

```
GET /v1/me
```

Reports `defaultLogbook` (with a `resolution` of `configured` / `sole` / `ambiguous` /
`none`), `membershipTier`, and rate limits.

### Response envelope

Every response is `{ "data": ..., "meta": ..., "error": ... }`. On failure
`error.code` is a stable string — branch on `code`, never on `message`. Every
response carries `X-Request-Id`.

Pagination: `meta.nextCursor`. Pass it back verbatim as `cursor`. Null means done.
**Cursors are opaque — never parse, construct, or modify them.**

### Rate limits

120 reads/min, 20,000 reads/day. Reads and writes have separate budgets.
`429` carries `Retry-After` in seconds, computed from the window that actually
blocked. Honour it rather than retrying on a fixed timer.

At 100 rows per page a 10,000-QSO log is 100 requests. Well inside budget. Add a
small sleep (~0.5s) between pages anyway so we never approach the per-minute ceiling.

### Contact fields returned

```
id                uuid
logbookId         uuid
call              string    station worked
stationCallsign   string    our callsign for that QSO
operator          string    who was operating
timestamp         ISO-8601 UTC
freq              number    MHz
band              number    METRES AS A NUMBER: 20, 0.7 — not "20m"
mode              string    single field, submode folded in (FT8, JS8 — not MFSK)
txPwr             string    watts, but a string
rstSent           string
rstRcvd           string
name              string    contacted operator's name
gridsquare        string    theirs, 2-8 chars
myGridsquare      string    ours
qth               string
state             string    abbreviation preferred ("TX")
notes             string    up to 2000 chars
dxcc              integer   ENRICHED — ADIF entity code
distance          number    ENRICHED — great-circle, UNITS UNDOCUMENTED
isDuplicate       bool
createdAt         ISO-8601
updatedAt         ISO-8601
```

### Field gotchas

- **`band` is a number in metres.** Format it for display: `20` renders as "20m",
  `0.7` renders as "70cm". Write one helper and use it everywhere.
- **Country, continent and flag are NOT returned.** All three are derivable from
  `dxcc`, so WRL does not duplicate them. We need a local DXCC entity lookup table
  committed to the repo: entity code to country name and continent. Build this once
  as `data/dxcc.json` from the ADIF country list.
- **`distance` units are not documented anywhere in the spec.** It just says
  great-circle. Verify empirically before trusting it (see discovery step below).
- **`distance` and `dxcc` are null until enrichment runs.** Fine for us, since we
  only ever read historical contacts, but do not assume non-null.
- **`programId` is required on write and never returned on read.** We never write,
  so it does not apply, but do not expect it in responses.
- **No POTA/SOTA fields.** No `sig`, `sigInfo`, `potaRef`. Park references live in
  `notes` if anywhere. Do not build POTA features on this API.
- **No QSL or confirmation status.** WRL tracks confirmation internally but does not
  expose it. Everything on these boards is worked, not confirmed.

---

## 3. MANDATORY discovery step before building anything

Same rule as the Nutrislice menu slugs: do not guess, inspect first.

Write `discover.py` as a throwaway script. It should:

1. `GET /v1/me`, print `defaultLogbook`, `resolution`, `membershipTier`, `limits`.
   If `resolution` is `ambiguous`, we need to pick a logbook or pass `logbookId`.
2. `GET /v1/logbooks`, print all logbooks with id, name, and whether locked.
3. Pull the first 200 contacts and report:
   - Null rate for `gridsquare`, `state`, `dxcc`, `distance`, `name`, `mode`, `band`
   - Distinct values of `mode` and `band` actually present
   - Date range of the sample
4. **Verify the `distance` unit.** Find a contact with a known far grid (anything in
   Europe or Asia), compute great-circle miles from the Clarkston QTH proxy
   (`42.72285220808688, -83.41970398420766`) to that grid's center, and compare
   against the returned `distance`. If the ratio is close to 1.0 it is miles; close
   to 1.609 it is kilometres. Print the ratio and the verdict.

Print a summary and stop. Do not build boards until this has run and we have looked
at the output together.

**Why this matters:** if `gridsquare` is sparse, the map is thin and we should derive
grids from `dxcc` where possible or reconsider that board. If `state` is sparse it
does not matter much, since we already removed the WAS panel. If `distance` turns out
to be kilometres, every number on all three boards is wrong by 60 percent.

---

## 4. Architecture

Standard pattern for this ecosystem.

```
cron-job.org  →  workflow_dispatch  →  GitHub Action  →  collect.py  →  data/latest/*.json  →  commit  →  Pages  →  board fetches JSON
```

**Do not use GitHub's native `schedule:` cron.** It is unreliable. Use cron-job.org
hitting `workflow_dispatch` with a fine-grained PAT scoped to this repo. Headers in
cron-job.org go in separate Key/Value fields.

### Two workflows, one collector

The recent board needs a 30-minute cadence; the career and grid boards only need
nightly. But all three derive from the same full-log pull, so:

- `collect.py --mode=recent` — pulls only the trailing ~45 days using `since`,
  writes `recent.json`. Cheap, runs every 30 minutes.
- `collect.py --mode=full` — pulls the entire log, writes all three files including
  a fresh `recent.json`. Runs nightly.

Two workflow files, `collect-recent.yml` and `collect-full.yml`, both triggered by
`workflow_dispatch`, both calling the same script with a different flag.

### Why full resync rather than incremental

The `since` filter works on **QSO timestamp, not `createdAt`**. There is no
`createdAt` or `updatedAt` filter. So an incremental sync would silently miss:

- Any backdated ADIF import
- Any edit to an older QSO
- Any deletion

The nightly full pull is cheap and always correct. Do not build incremental sync
logic for the full mode. The recent mode's `since` window is fine because those
contacts are new by definition, and the nightly full run corrects any drift.

### Repo layout

```
wrl-boards/
├── career.html
├── recent.html
├── grids.html
├── collect.py
├── discover.py                (throwaway, can be deleted after)
├── requirements.txt
├── data/
│   ├── dxcc.json              committed lookup: entity code → country, continent
│   └── latest/
│       ├── career.json
│       ├── recent.json
│       └── grids.json
├── mockups/                   reference only, not served
│   ├── wrl-career-dashboard.html
│   ├── wrl-recent-activity.html
│   └── wrl-grid-map.html
└── .github/workflows/
    ├── collect-recent.yml
    └── collect-full.yml
```

---

## 5. Collector requirements

`collect.py`, Python, run with the `py` launcher on Windows for local testing.

### Non-negotiables (learned the hard way on other projects)

- **Write JSON with `json.dump` only.** Never PowerShell `Set-Content` — it writes a
  BOM that breaks the fetch.
- **Never open a file for write then immediately read it.** That truncates it.
- **Use `truststore`** for HTTPS. James's corporate Windows workstation does TLS
  inspection and requests will fail without it.
- **HTTP timeout as a tuple**, `(10, 60)` — connect 10s, read 60s.
- **Abort before changes.** Validate the full pull succeeded and the contact count is
  sane before writing any file. If the API returns an error partway through paging,
  exit non-zero and leave the existing JSON untouched. A stale board is fine; a
  wiped board is not.
- **`fetch-depth: 0`** on the checkout step.
- **5-attempt rebase-retry push loop:**
  `git pull --rebase --autostash -X theirs origin main`

### Single pass over the log

The heatmap and the median-distance panel both need every QSO grouped and sorted, not
just counted. Median in particular means holding a sorted distance list per band. Do
one pass over the full log building all aggregates simultaneously, then write.
Do not recompute per board.

### Aggregates to compute

**career.json**
- `total_qsos`, `qsos_this_year`, `first_qso_date`
- `dxcc_count`, `grid_count`, `continent_count` (derived from `dxcc` via lookup)
- `total_distance` (sum of all non-null `distance`, in miles)
- `bands`: list of `{band, count}` sorted by count desc
- `modes`: list of `{mode, count}` sorted by count desc, with a small mapping so
  FT8/FT4/JS8/PSK etc. roll into the Digital color, and CW/SSB/FM keep their own
- `records`: furthest (distance + call), best day (count + date), longest daily streak
- `heatmap`: `{band: [24 ints]}` — QSO count per UTC hour per band, top 8 bands only
- `by_year`: list of `{year, count}`

**recent.json**
- `today`, `week`, `month` counts (UTC day boundaries)
- `daily`: last 7 days as `{label, date, count}`, oldest first
- `hourly`: trailing 24 hours as 24 ints, oldest first, plus `now_hour`
- `contacts`: last 14, each with `time` (HH:MM UTC), `call`, `country` (from dxcc),
  `grid`, `name`, `band`, `mode`, `distance`, and two booleans `new_dxcc` and
  `new_grid` (true if this QSO was the first ever for that entity or grid)

The `new_dxcc` / `new_grid` flags require the full log to determine, so the recent
mode needs the previous run's set of known entities and grids. Simplest approach:
the nightly full run writes `data/latest/known.json` containing the sets, and the
30-minute recent run reads it, compares, and updates it. That way the recent run
still only pulls 45 days.

**grids.json**
- `grid_count`, `field_count` (unique 2-char fields, out of 324), `dxcc_count`
- `new_30d` count
- `points`: list of `{grid, lat, lon, fresh}` — one per unique grid, `lat`/`lon` are
  the grid center, `fresh` true if worked in last 30 days
- `median_distance`: list of `{band, miles}` sorted by miles desc
- `newest_grids`: last 8 unique grids as `{grid, call, country, date}`

Grid center from a Maidenhead locator is a small pure function. Write it once,
handle 2, 4, 6 and 8 character locators.

### Tuning constants at top of file

`FRESH_DAYS = 30`, `RECENT_CONTACTS = 14`, `RECENT_WINDOW_DAYS = 45`,
`HEATMAP_BANDS = 8`, `PAGE_SIZE = 100`, `PAGE_SLEEP = 0.5`.

---

## 6. Board requirements

Start from the mockup files. Keep the CSS exactly as written — it is already on
brand and already fits 1080x1920. Replace only the hardcoded markup with JS that
renders from the fetched JSON.

### Conventions to preserve

- **Non-interactive.** No hover effects, no tooltips, no clickable elements.
- **Single master tick.** One interval drives the clock, the relative ages, and the
  fetch trigger. Never two competing intervals for the same concern.
- **Signature check before DOM rebuild.** Hash the fetched payload; only rebuild if
  it changed. Prevents animation and scroll restarts.
- **Graceful stale handling.** Preserve last-known values on fetch failure. Never
  wipe the display on a network error. Show a stale badge in the footer after
  several consecutive failed cycles.
- **Animation restricted to `transform` and `opacity`.** No `backdrop-filter`, no
  `shadowBlur`, no animating CSS filters. The Pi 5B has headroom but the discipline
  is still worth keeping.
- **Fetch with cache busting:** same-origin relative path, `?v=Date.now()`, and
  `cache: 'no-store'`.
- **`AUTO_FIT` flag** stays in each file, defaulting to `false` for production. The
  mockups ship with it `true` so they can be viewed in a browser window.
- **All tuning constants at the top of the script block.**

### Brand system (already implemented in the mockups)

```
Navy dark    #0d1620      background gradient start
Navy         #152333      background gradient mid
Navy light   #1e3347      background gradient end
Orange       #ff7030      accent, section headers, active states
Cream        #fdf8e5      primary text
Cream 80%    rgba(253,248,229,.80)
Cream 50%    rgba(253,248,229,.50)
Cream 30%    rgba(253,248,229,.30)
Gold         #E0A52E      records and personal bests only
Card fill    rgba(255,255,255,.05)
Card border  rgba(255,255,255,.10)

Mode colors: CW #4a90e2, SSB #ff7030, FM #9b59b6, Digital #2ecc71

Fonts: Oswald (headings, numbers), Source Sans Pro (body)
```

Orange is reserved for brand UI accents and the fresh tier. Gold is reserved for
records and personal bests.

### Grid map specifics

Uses D3 v7 plus topojson-client, both from jsdelivr, with
`https://cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json` for the land layer
and `d3.geoNaturalEarth1()` for the projection.

Consider vendoring the topojson file into the repo rather than fetching from a CDN
on every board load. It is small, it never changes, and it removes an external
dependency from a display that needs to survive a flaky network. Same argument as
self-hosting the fonts.

Great-circle arcs are drawn by passing a two-point GeoJSON `LineString` to
`d3.geoPath` — D3's adaptive resampling follows the great circle automatically.
Draw arcs from the Clarkston QTH proxy to a handful of the furthest recent contacts.

---

## 7. Build order

1. `discover.py`, run it, review output together. **Stop here for review.**
2. `data/dxcc.json` lookup table, plus the entity-to-country/continent helper.
3. `collect.py` full mode only. Run locally, inspect the three JSON files by hand.
4. `career.html` wired to real data. Verify against the mockup side by side.
5. `grids.html` wired to real data.
6. `collect.py` recent mode plus the `known.json` mechanism.
7. `recent.html` wired to real data.
8. Both workflow files, `WRL_API_KEY` secret, fine-grained PAT, cron-job.org entries.
9. Point DakBoard at the Pages URLs.

---

## 8. Open questions to resolve during discovery

- Is `distance` miles or kilometres?
- How complete is `gridsquare` across the log? This decides whether the map board is
  worth building as designed.
- Are there multiple logbooks, and is one set as default? If `resolution` is
  `ambiguous` we need to decide whether to aggregate all logbooks or filter to one.
- Does `mode` contain enough distinct values to justify the four-color mode split,
  or is the log overwhelmingly FT8?
- Total QSO count, which sets the expected page count and run time.
