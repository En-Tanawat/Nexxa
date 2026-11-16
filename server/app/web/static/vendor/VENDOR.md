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

## three.js 0.185.1

- Used by: `static/js/twin.js` (3D greenhouse twin on the public dashboard). The page loads only `twin.js`; twin.js imports
  three lazily (`import()`), so if anything here fails to load the dashboard falls back to the 2D SVG schematic.
- Files (same layout as the npm package `three@0.185.1`, so relative imports between them work unchanged):
  - `three-0.185.1/build/three.module.min.js`: the minified ES module build (imports `./three.core.min.js`).
  - `three-0.185.1/build/three.core.min.js`: the core the module build re-exports (the 0.185 release is split in two files).
  - `three-0.185.1/examples/jsm/controls/OrbitControls.js`: the camera controls add-on (**patched**, see below).
  - `three-0.185.1/LICENSE`: the MIT licence (Copyright 2010-2026 three.js authors).
- Sources (npm package `three@0.185.1`, https://threejs.org):
  - https://cdn.jsdelivr.net/npm/three@0.185.1/build/three.module.min.js
  - https://cdn.jsdelivr.net/npm/three@0.185.1/build/three.core.min.js
  - https://cdn.jsdelivr.net/npm/three@0.185.1/examples/jsm/controls/OrbitControls.js
  - https://cdn.jsdelivr.net/npm/three@0.185.1/LICENSE
  - The bytes were also fetched from https://unpkg.com/three@0.185.1/ and are identical (same SHA-256).
- Licence: MIT. Neither build uses `eval` / `new Function`, so they run under the CSP (`script-src 'self'`, no `unsafe-inline`/`unsafe-eval`).
- Downloaded: 2026-11-16 with `curl`. Never loaded from a CDN at runtime.

### The one patch (`OrbitControls.js`)

OrbitControls imports three by its bare name (`from 'three'`). A bare name needs an import map, and an import map must be an
inline `<script type="importmap">` (import maps cannot be external), which our CSP forbids (no `'unsafe-inline'`). So the
single import line was rewritten to the relative path of the vendored build:

```diff
-} from 'three';
+} from '../../../build/three.module.min.js';
```

- Upstream (unpatched) SHA-256 of `OrbitControls.js`: `faabb4e8dfd9235ee4a9fd7c9a3d75f90f1689dbd4944bd6fd32117dacec5f93`
- Patched (vendored) SHA-256 of `OrbitControls.js`: `55c021516ef740073db295552b8fc6dfc989d272cdf2987f3b4a9773feaffdf2`
- `tests/test_web_twin.py` re-applies the reverse edit and checks that the upstream hash comes back, so nothing else was changed.
- The patched file imports the same module instance as `twin.js` (same URL), so `Vector3`, `Spherical` etc. are the same classes.
- To upgrade: download the new version into a new `three-<version>/` folder, repeat this one-line patch, update the hashes below and the
  pinned-version constants in `tests/test_web_twin.py`, delete the old folder.

## SHA-256 of every vendored file

`tests/test_web_twin.py` compares each row with the bytes on disk (and fails if a vendored file is missing from this table).
`.gitattributes` marks these files `-text` so git never converts their line endings (that would change the hash).

| File (under `static/vendor/`) | SHA-256 |
| --- | --- |
| `chartjs-4.5.1/LICENSE.md` | `41a84aa2caba645f966a18d9c2056b73e6d3a81d80bc0046bc0011a2634d4cce` |
| `chartjs-4.5.1/chart.umd.min.js` | `48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a` |
| `three-0.185.1/LICENSE` | `8b378ebe60e2fe500158cb0ac71cb5e8b7d92953c2abcc63a0eb90499653b5bc` |
| `three-0.185.1/build/three.core.min.js` | `05b2609338c76cd65daf74f3ac515bc9a5045e1b3b33edc07d8c9bd55250fa90` |
| `three-0.185.1/build/three.module.min.js` | `86bcee248b64f44bcfc23c331ae74619061957d59cab040171dcb6fb5900beb6` |
| `three-0.185.1/examples/jsm/controls/OrbitControls.js` | `55c021516ef740073db295552b8fc6dfc989d272cdf2987f3b4a9773feaffdf2` |
