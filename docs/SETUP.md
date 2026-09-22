# Setup

## 1. Credentials

| Secret | Where it comes from |
|---|---|
| `MARGINEDGE_API_KEY` | MarginEdge → your name (top right) → **Settings → Security → Create new API key** (you are a MarginEdge Admin). Shown once; read-only; sent as `x-api-key`. Keys made on/after 2026-08-04 include bulk-export access. |
| `TOAST_CLIENT_ID` / `TOAST_CLIENT_SECRET` | Toast API credentials for the restaurant group (Toast support / Partner Connect / "Toast API access" request). Access is scoped per restaurant GUID. |
| `PORTAL_SECRET` | Any long random string you generate: `python -c "import secrets;print(secrets.token_urlsafe(48))"`. Used for every bundle key and the warehouse state. Must match the Apps Script property of the same name. |
| `TRIPLESEAT_PUBLIC_KEY` | Tripleseat → Settings → **Tripleseat API / Webhooks** → API tab → "Tripleseat Public API key". It is the key Tripleseat embeds in website lead forms, so it is not a secret in the usual sense — but the repository is public, so it lives in a secret all the same. See "Tripleseat" below for what it can and cannot do. |

Add them as **GitHub → Settings → Secrets and variables → Actions → New repository secret**.
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

## 2a. Tripleseat (private events) — three routes, added 2026-09-21

Tripleseat is the events team's booking system (`biggrovebrewery.tripleseat.com`). The portal reaches it three ways,
each in `sdp/tripleseat.py`, and the nightly pull runs whichever are configured:

| Route | What it gives | Needs | State |
|---|---|---|---|
| **Public key** | The catalog: every location and its rooms (with capacities), the site's event types, lead sources, referral sources, billing rules per location (gratuity, taxes, fees) and lead forms. Feeds the "Rooms and capacities" and "Event charges" panels and resolves room and event-type names on events. | `TRIPLESEAT_PUBLIC_KEY` | **Working.** Probed on 2026-09-21: reads `/locations`, `/sites`, `/lead_forms` only. `/events`, `/leads`, `/bookings`, `/rooms`, `/users`, `/accounts` all answer "You don't have permission". |
| **Webhooks** | Events, leads and bookings as they are created, edited or deleted. Tripleseat POSTs the object to the Apps Script, which files it on a "Tripleseat" tab of the roster workbook; the pipeline collects the tail nightly (`{"a":"tripleseat"}`, HMAC of `PORTAL_SECRET`, like the scorecard). | The webhook added in Tripleseat (below) and a redeployed Apps Script | **Live since 2026-09-21** (proof below). Only objects touched after the webhook exists arrive on their own; the history behind it was seeded once from a report export (next section). |
| **OAuth API** | The full read API as a nightly window (`/events/search`, `/leads/search`). | `TRIPLESEAT_CLIENT_ID` / `_SECRET` / `_REFRESH_TOKEN`, `TRIPLESEAT_REDIRECT_URI` | **Blocked.** Creating the client application under Settings → Tripleseat API & Webhooks fails on Tripleseat's side; needs their support. Note the token endpoint offers only `authorization_code` (one browser consent by a Tripleseat admin) and `oauth1_exchange` — no machine-to-machine grant. |

**Location mapping** is in `config/locations.json` (`tripleseat_location_id`). Print the account's locations and rooms
with `TRIPLESEAT_PUBLIC_KEY=… python -m sdp ts-locations`. Five taprooms are Tripleseat locations of their own;
**Solon is not** — its events are booked under the Iowa City location in a room named "Solon", so Solon carries Iowa
City's location id plus `tripleseat_room_ids: ["232405"]`, and a room match wins over the location. "BlackStone"
(location 11731) is in the account but is not a taproom; its notifications are ignored and counted in the log.

**Switching the webhook on** (one time, in this order):

1. Redeploy the Apps Script (Deploy → Manage deployments → ✏️ → New version) so it carries `doTripleseatHook`.
2. In the Apps Script editor run `showTripleseatWebhookUrl()` once. It creates the `TRIPLESEAT_HOOK_TOKEN` script
   property and logs the target URL: the web app's `/exec` URL followed by `?hook=<token>`.
3. In Tripleseat: Settings → Tripleseat API / Webhooks → Webhooks tab → **Add Webhook**. Tick the Event actions
   (Create, Update, Delete Event), the Lead actions (Create Lead, Create Internal Lead, Convert Lead, Lead Turned
   Down) and the Booking actions (Create, Update, Delete, Status Change, Change Booking Dates, Convert Lead To
   Booking). Leave Contact and Account actions unticked — they carry guests' details the portal never shows. Leave
   "Include Event Payment and Line Item Information" unticked to start (it can make a delivery too large for a
   cell; the receiver trims, but nothing reads line items yet). Paste the URL as the Target URL.
4. Edit any event in Tripleseat, then check two places: the "Tripleseat" tab of the roster workbook has a new row,
   and the endpoint on the Webhooks tab in Tripleseat is still enabled. Apps Script answers every POST with
   a **302 redirect** (after recording the body); Tripleseat may count that as a failure and disable the endpoint
   after too many, and enabling it again resets the count. If that ever happens, the receiver has to move to
   something that answers 200 directly (a Cloudflare Worker relaying to the same script is the smallest such thing).

**Proven 2026-09-21 19:08 CT.** A guest-count nudge on event 61680687 (Business Lunch, Cedar Rapids) produced three
deliveries within a minute — `UPDATE_BOOKING`, `CHANGE_EVENT_GUEST_COUNTS`, `UPDATE_EVENT` — and the revert three
more; all six landed on the tab, the next `source = tripleseat` run absorbed them, and the event published under
Cedar Rapids with its room and lead source. The endpoint was still enabled afterwards, so six 302s in a row did not
trip Tripleseat's failure limit (its UI shows no counter, so "still enabled" is the only readable signal). The real
payload is `{"webhook_trigger_type": "...", "message": "...", "event" | "booking" | "lead": {...}}` with the full
object: status upper-case (`DEFINITE`), money as strings, `created_at` as `7/27/2026 11:08 PM`, `rooms` as objects
with names, `status_changes` and `selected_lead_sources` included, `event_type_id` null.

**History seeded 2026-09-22** from a Tripleseat *Event Details* report export (`tools/tripleseat_seed.js`, run in
the browser console on Reports → History; instructions at the top of the file). The export — all locations,
statuses Prospect / Tentative / Definite / Closed, 8/1/2025 through 12/31/2027, Lost left out on purpose — held
2,956 events (Cedar Rapids 810, Des Moines 753, Omaha 589, Iowa City 584, Prairie Village 220; none in the
"Solon" room). The tool reshapes each report line into the object the webhook would have delivered (rooms and
event types resolved to ids through the public-key catalog, money as strings, `status_changes` from the
Definite/Tentative/Lost/Closed dates, the lead form and lead source, `created_at`) wrapped as action
`SEED_EVENT` with `exported_at`, and POSTs them to the same Apps Script URL with `&seed=1`
(`doTripleseatSeed`, up to 500 rows a call, same token, same PII scrub, plain append). The next refresh loads them
through the webhook code path with `source = 'seed'`; a row keeps that label until a live delivery replaces it.
Rows apply newest-state-first — a delivery's time, or the export time for a seeded row — so re-running the tool
later (a gap while the webhook was off, an older year, a lost tab) can never roll a live update back. To rebuild
the whole feed from the tab, run the workflow with `source = tripleseat` and `backfill` ticked. The report does
not carry `updated_at`, contacts, or line items, so those stay empty on seeded rows.

The receiver cannot verify Tripleseat's `X-Signature` header — Apps Script never sees request headers — so the
random token in the URL is what stands between the tab and the internet. The tab is append-only, the pipeline
treats it as untrusted input, and email addresses, phone numbers and postal addresses are stripped from every
delivery before it is written.

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
