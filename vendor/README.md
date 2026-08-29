# Vendored assets

Committed rather than fetched from a CDN. These boards run unattended on a
Pi behind a home network; a display that blanks because jsdelivr is briefly
unreachable is worse than 150 KB in the repo.

| File | Version | Licence | Source |
|---|---|---|---|
| `d3-array.min.js` | 3.2.4 | ISC | npm `d3-array` (peer dependency of d3-geo) |
| `d3-geo.min.js` | 3.1.1 | ISC | npm `d3-geo` |
| `topojson-client.min.js` | 3.1.0 | ISC | npm `topojson-client` |
| `countries-110m.json` | 2.0.2 | ISC | npm `world-atlas`; underlying data is Natural Earth, public domain |

Only `d3-geo` is vendored, not all of D3 — the boards use `geoNaturalEarth1`,
`geoPath` and `geoGraticule10` and nothing else, which is 36 KB instead of
roughly 280 KB.

Load order matters: `d3-array` before `d3-geo`, because the UMD build of
d3-geo expects the `d3` global to already carry the d3-array functions.

To refresh:

    npm install d3-array d3-geo topojson-client world-atlas
    cp node_modules/d3-array/dist/d3-array.min.js vendor/
    cp node_modules/d3-geo/dist/d3-geo.min.js vendor/
    cp node_modules/topojson-client/dist/topojson-client.min.js vendor/
    cp node_modules/world-atlas/countries-110m.json vendor/
