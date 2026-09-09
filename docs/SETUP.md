# Setup

## 1. Credentials

| Secret | Where it comes from |
|---|---|
| `MARGINEDGE_API_KEY` | MarginEdge → an **Admin** user generates an API key (one key per user). Read-only. Sent as `x-api-key`. |
| `TOAST_CLIENT_ID` / `TOAST_CLIENT_SECRET` | Toast API credentials for the restaurant group (Toast support / Partner Connect / "Toast API access" request). Access is scoped per restaurant GUID. |
| `PORTAL_SECRET` | Any long random string you generate: `python -c "import secrets;print(secrets.token_urlsafe(48))"`. Used for every bundle key and the warehouse state. Must match the Apps Script property of the same name. |

Add the four as **GitHub → Settings → Secrets and variables → Actions → New repository secret**.
Optional repository *variable* `TOAST_HOST` (defaults to `https://ws-api.toasttab.com`; sandbox is
`https://ws-sandbox-api.eng.toasttab.com`).

Until Toast credentials arrive the workflow simply skips Toast (it logs a warning) and the dashboard shows
MarginEdge-only pages; the Sales/Labor pages fill in once Toast is connected.

## 2. Locations

Edit `config/locations.json`. Find the ids with:

```bash
python -m sdp me-units            # MarginEdge restaurantUnit id + name
python -m sdp toast-restaurants   # Toast restaurant GUID + name (partner scope)
```

Toast GUIDs are also visible in Toast Web → Restaurant admin → Restaurant info.

## 3. GitHub Pages + Actions

1. Repo → **Settings → Pages → Source: GitHub Actions**.
2. Repo → **Settings → Actions → General → Workflow permissions: Read and write** (needed for the encrypted
   warehouse release asset).
3. Run the workflow once by hand: **Actions → Nightly refresh → Run workflow** (tick *mock* for a demo build
   with sample data). The first real run backfills `backfill_days` (400) and takes a while because of Toast's
   5 req/s per-location limit; later runs re-pull only the last 7 days.

The published URL is `https://<org>.github.io/<repo>/`.

## 4. Google sign-in (one-time, owner account: troy@biggrovebrewery.com)

Mirrors the Sales Portal setup — same Google Cloud project can be reused.

**A. OAuth client** — console.cloud.google.com → APIs & Services → Credentials → the existing "Big Grove
Portal" web client → add the new Pages origin (`https://<org>.github.io`) to *Authorized JavaScript origins*
(or create a second web client). Copy the Client ID.

**B. Roster sheet** — new Google Sheet **Store Director Roster**, row 1: `email | name | role | locations`.
Roles: `Admin`, `Leadership`, `Director`. Locations: slug(s) from `config/locations.json`, e.g. `solon` or
`solon,iowa-city`; blank/`all` for leadership. A `Logins` tab is created automatically for usage logging.

**C. Apps Script** — script.google.com → new project **Store Director Login** → paste `apps-script/code.gs`.
Project Settings → Script properties: `CLIENT_ID`, `SHEET_ID`, `PORTAL_SECRET` (identical to the GitHub
secret). Deploy → New deployment → Web app → Execute as **Me**, access **Anyone** → copy the `/exec` URL.
Run `testDerivation()` once and compare with
`python -c "from sdp.bundle import *; print(bundle_id('all'), key_b64('<secret>','all'))"` — they must match.

**D. Wire the shell** — paste the Client ID and `/exec` URL into the `CFG` block at the top of
`site/index.html`, commit, and the next workflow run publishes it.

## 5. Manual inputs

`inputs/activations.csv` (events/promos/launches with cost), `inputs/targets.csv` (monthly sales, COGS %,
labor %, guests per location) and `inputs/inventory_counts.csv` (value per bucket per count date — until the
MarginEdge count-sheet endpoint is wired; set `MARGINEDGE_INVENTORY_PATH` once its path is confirmed in the
developer portal). Edit in GitHub or keep them as a Google Sheet and export; commit → picked up next run.

## 6. Rotating keys

Change `PORTAL_SECRET` in both places (GitHub secret + Apps Script property), delete the `warehouse-state`
release asset (it was encrypted with the old secret) and run the workflow with *backfill* ticked.
