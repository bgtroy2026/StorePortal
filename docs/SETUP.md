# Setup

## 1. Credentials

| Secret | Where it comes from |
|---|---|
| `MARGINEDGE_API_KEY` | MarginEdge → your name (top right) → **Settings → Security → Create new API key** (you are a MarginEdge Admin). Shown once; read-only; sent as `x-api-key`. Keys made on/after 2026-08-04 include bulk-export access. |
| `TOAST_CLIENT_ID` / `TOAST_CLIENT_SECRET` | Toast API credentials for the restaurant group (Toast support / Partner Connect / "Toast API access" request). Access is scoped per restaurant GUID. |
| `PORTAL_SECRET` | Any long random string you generate: `python -c "import secrets;print(secrets.token_urlsafe(48))"`. Used for every bundle key and the warehouse state. Must match the Apps Script property of the same name. |

Add the four as **GitHub → Settings → Secrets and variables → Actions → New repository secret**.
Optional repository *variable* `TOAST_HOST` (defaults to `https://ws-api.toasttab.com`; sandbox is
`https://ws-sandbox-api.eng.toasttab.com`).

Until Toast credentials arrive the workflow simply skips Toast (it logs a warning); the dashboard runs on
MarginEdge alone and marks the order-level panels "needs Toast".

**Backfill timing.** MarginEdge allows 1 request/second per key. The first run pulls a daily sales report and a
daily P&L for every unit for `backfill_days` (400) — about 80 minutes for six units — plus invoice detail and
inventories. The pull stops cleanly at `max_minutes_per_run` (300) and the next nightly run continues from
where it left off, so a full backfill may take two or three nights. Nightly runs afterwards take a few minutes.

## 2. Locations

Edit `config/locations.json`. Find the ids with:

```bash
python -m sdp me-units            # MarginEdge restaurantUnit id + name
python -m sdp toast-restaurants   # Toast restaurant GUID + name (partner scope)
```

If `marginedge_unit_id` is left blank the pull matches units by `me_name` (the exact name in MarginEdge),
which is pre-filled for all six taprooms. Toast GUIDs are visible in Toast Web → Restaurant admin → Restaurant
info once your Toast user has that access.

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

`inputs/activations.csv` (events/promos/launches with cost) and `inputs/targets.csv` (monthly sales, COGS %,
labor %, guests per location). `inputs/inventory_counts.csv` is an optional fallback — inventory values come
from the MarginEdge `/inventories` API (every counted item, rolled up by category). Edit in GitHub or keep them
as a Google Sheet and export; commit → picked up next run.

## 6. Rotating keys

Change `PORTAL_SECRET` in both places (GitHub secret + Apps Script property), delete the `warehouse-state`
release asset (it was encrypted with the old secret) and run the workflow with *backfill* ticked.

## Morning automation (added 2026-09-19)

The refresh, the digest email and the "nothing landed" alarm all run from the Apps Script project that already
handles sign-in, on a 15-minute time trigger. Nothing depends on a computer being awake.

One-time setup, in the Apps Script editor for **Store Director Login**:

1. Paste the current `apps-script/code.gs` over the project's code and save.
2. Project Settings → Script properties, add:
   - `GH_TOKEN` — a fine-grained GitHub token for `bgtroy2026/StorePortal` with **Actions: Read and write**. This is
     what starts the 5:15 AM refresh. (Without it the digest and the alarm still work; the refresh falls back to
     GitHub's own late schedule.)
   - `GH_TOKEN_EXPIRES` — its expiry date as `YYYY-MM-DD`. A warning is emailed weekly from 14 days out.
   - `DIGEST_MODE` — leave unset (or `preview`) to begin with: every digest is sent to the first Admin on the
     roster, with the intended recipient in the subject. Set to `live` when the content has been judged.
   - `ALERT_TO` — optional; defaults to the first Admin on the roster.
3. Run `installTriggers` once from the editor and approve the permissions it asks for (send mail, call GitHub).
4. Deploy → Manage deployments → edit → **New version**, so the web app also serves the new `view`, `acks` and
   `ack` actions. Until this is done the portal simply hides acknowledgements and page-view counts.
5. Optional: `previewDigestToMe` sends today's digest to the alert address immediately.

A roster row can opt out of the digest by putting `no` in a fifth column.

## Inputs added 2026-09-19

- `inputs/floats.csv` — what each cash drawer should open with: what Toast expects and what actually goes in.
- Depletions — brand × taproom-market case equivalents. **Never committed** (this repo is public). They travel
  through the roster workbook's "Depletions" tab and the pipeline fetches them with the scorecard.
  - Automatic: `tools/push_depletions.py` runs at the end of every Sales Portal deploy on the Mac, rebuilds the
    roll-up, and uploads it if it changed. One-time setup: double-click `tools/Set up depletions upload.command`
    and paste the clipboard into the Apps Script as script property `DEPLETIONS_PUT_KEY`.
  - By hand: an admin can upload a CSV from the Activations page.
  Market radius and fallback city lists are in `config/markets.json`.
- `config/settings.json` gained `pours` (pour-size keywords), `channels.commission` (marketplace rates),
  `inventory.expected_count_days` (count cadence) and `marginedge.provisional` (the provisional badge switch).
- `config/locations.json` gained `lat`/`lon` (weather) and optional `seats`.

## Healing history after a schema addition

Toast modifiers (pour sizes), loyalty identification and order source are read from the order JSON, which is not
kept between runs — so past days gain them only by being pulled again. Run the workflow by hand with
`source = toast` and `heal = true`, after the morning refresh. It works newest-first inside the normal Toast time
budget, shares the budget across taprooms, and the check step logs how many location-days remain.
