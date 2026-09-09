# Big Grove Brewery — Store Director Portal

A nightly pipeline + static dashboard that joins **Toast** (POS: sales, guests, labor) and **MarginEdge**
(purchasing, COGS, inventory) for the six taprooms, gated by Google sign-in and published on GitHub Pages.

```
Toast API ─┐                                    ┌─ data/<id>.bin  (AES-256-GCM, one per audience)
MarginEdge ┼─ pull ─► raw/ ─► transform ─► SQLite ─► metrics ─► build ─┤
inputs/*.csv┘                        (warehouse)                     └─ index.html (login shell + dashboard)
                                          │
                                          └── persisted between runs as an encrypted GitHub Release asset
```

Everything runs in **GitHub Actions** on a schedule (`.github/workflows/refresh.yml`, 5:15 AM Central) and
deploys to **GitHub Pages**. Nothing runs on a laptop; no plaintext data is ever committed or published.

## Repo layout

| Path | What it is |
|---|---|
| `sdp/marginedge.py`, `sdp/toast.py` | API clients + pull orchestration (incremental, throttled, retried) |
| `sdp/mock.py` | Sample-data generator in the exact API shapes — `python -m sdp pull --mock` |
| `sdp/schema.sql` | Warehouse schema (SQLite) — see `docs/DATA_MODEL.md` |
| `sdp/transform.py` | raw JSON → warehouse tables → `daily_summary` |
| `sdp/metrics.py` | warehouse → compact JSON payload for the dashboard (+ per-location slices) |
| `sdp/bundle.py`, `sdp/state.py` | Encryption: dashboard bundles, warehouse persistence |
| `sdp/build_site.py` | Assembles `_site/` (what Pages serves) |
| `site/index.html` | Login shell + dashboard (single file, Chart.js vendored in `site/vendor/`) |
| `apps-script/code.gs` | Google Apps Script sign-in backend (roster sheet → bundle keys) |
| `config/locations.json` | The six taprooms: slugs, Toast GUIDs, MarginEdge unit IDs |
| `config/settings.json` | Windows, hosts, category → bucket mapping |
| `inputs/*.csv` | Manual inputs: activations, monthly targets, inventory counts |
| `docs/SETUP.md` | One-time setup: secrets, Google OAuth, Apps Script, Pages |
| `docs/DATA_MODEL.md` | Tables, grains, KPI definitions |

## Quick start (local, no credentials)

```bash
pip install -r requirements.txt
python -m sdp pull --mock          # sample data for 6 taprooms, 120 days  (raw/)
python -m sdp transform            # → state/warehouse.sqlite
python -m sdp build --no-encrypt   # → _site/ with data/dev.json
cd _site && python -m http.server 8000
# open http://localhost:8000/?dev=1
```

## With real credentials

```bash
export MARGINEDGE_API_KEY=...  TOAST_CLIENT_ID=...  TOAST_CLIENT_SECRET=...  PORTAL_SECRET=...
python -m sdp me-units             # find MarginEdge restaurantUnit ids → config/locations.json
python -m sdp toast-restaurants    # find Toast restaurant GUIDs      → config/locations.json
python -m sdp all                  # pull (backfill on first run) → transform → build (encrypted)
```

Set the same four values as **Actions secrets** and the workflow does this nightly. See `docs/SETUP.md`.

## Dashboard pages

Overview (KPI tiles vs prior period / last year / target, sales trend with activations, mix, weekly labor % and
purchases %, day-of-week) · Sales (category stack, hourly heatmap, top items, dining options, tenders) · Labor
(labor % by day, hours, by job) · COGS & Purchasing (purchases by category/week, category cost %, vendors) ·
Inventory (begin + purchases − end = usage, actual cost %, weeks on hand) · Activations (cost, sales lift vs
same-weekday baseline, lift ÷ cost) · Locations (scorecard, weekly comparisons).

## Access model

Google sign-in → Apps Script checks the **Store Director Roster** sheet → returns the bundle id + key for that
person. Admin/Leadership get `all`; a Director gets `loc:<slug>` — a file that only contains their taproom.
Keys are derived from `PORTAL_SECRET` (HMAC), so nothing secret is stored in the repo or on the site.
