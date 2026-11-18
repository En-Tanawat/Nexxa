# Vendored front-end libraries

Never loaded from a CDN at runtime (the CSP only allows `'self'` scripts). Pinned exact versions; to upgrade, download the
new version into a new `<name>-<version>/` folder, update the page templates and the pinned-version test, delete the old folder.

## Chart.js 4.5.1

- Used by: `static/js/trends.js` (public trends page).
- File: `chartjs-4.5.1/chart.umd.min.js` (UMD build, defines the global `Chart`; contains no `eval` / `new Function`).
- Source: https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js (npm package `chart.js@4.5.1`, https://www.chartjs.org)
- Licence: MIT, kept in `chartjs-4.5.1/LICENSE.md` (https://cdn.jsdelivr.net/npm/chart.js@4.5.1/LICENSE.md)
- Downloaded: 2026-11-16 with `curl`.
- SHA-256 of `chart.umd.min.js`: `48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a` (also in the table below)

## SHA-256 of every vendored file

`.gitattributes` marks these files `-text` so git never converts their line endings (that would change the hash).

| File (under `static/vendor/`) | SHA-256 |
| --- | --- |
| `chartjs-4.5.1/LICENSE.md` | `41a84aa2caba645f966a18d9c2056b73e6d3a81d80bc0046bc0011a2634d4cce` |
| `chartjs-4.5.1/chart.umd.min.js` | `48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a` |
