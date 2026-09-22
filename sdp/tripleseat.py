"""Tripleseat — private events, bookings and leads, three ways in.

API:  https://api.tripleseat.com/v1/
Rate: 10 req/s (429 past that) — far looser than Toast or MarginEdge, so we run at 5 req/s.
Docs: Settings > Tripleseat API > Documentation in the account; OpenAPI at
      https://api.tripleseat.com/api-docs/v1/openapi.yaml

THREE ROUTES, in the order they became available (2026-09-21)
-------------------------------------------------------------
1. PUBLIC KEY  (TRIPLESEAT_PUBLIC_KEY, works today). The key under Settings > Tripleseat API is the one Tripleseat
   embeds in website lead forms. Probed against every endpoint on 2026-09-21: it READS exactly three things —
   /locations (with rooms, capacities, addresses), /sites (event types, lead sources, referral sources, billing
   rules per location, line-item categories) and /lead_forms — and can POST /leads/create. Everything else
   (/events, /events/search, /leads, /bookings, /rooms, /users, /accounts) answers "You don't have permission".
   So the key gives the portal the CATALOG: which Tripleseat location and rooms are which taproom, what an
   event can be called, and what each taproom charges on top of an event. It cannot give it a single event.

2. WEBHOOKS  (Settings > Tripleseat API & Webhooks > Webhooks). Tripleseat POSTs the full event / lead /
   booking object to a URL whenever one is created, changed or deleted. The portal's Apps Script receives them
   into a "Tripleseat" tab of the roster workbook and this pipeline collects that tab nightly on proof of
   PORTAL_SECRET, the same route the scorecard and depletions already travel (sdp/scorecard.py). Only objects
   touched AFTER the webhook exists arrive this way, so the calendar fills in as the events team works. The
   history behind it was SEEDED once (2026-09-22) from a Tripleseat "Event Details" report export, reshaped
   into webhook-style rows in the browser and posted to the same Apps Script (`tools/tripleseat_seed.js`,
   action SEED_EVENT, source='seed' in the warehouse until a live delivery replaces the row). The same tool
   re-seeds if the tab is ever lost or a gap opens.

3. OAUTH 2.0  (TRIPLESEAT_CLIENT_ID / _SECRET / _REFRESH_TOKEN). The full read API, pulled as a window every
   night. Blocked as of 2026-09-21: creating the client application in Tripleseat fails on their side, and the
   token endpoint offers only authorization_code and oauth1_exchange — no client-credentials grant — so it will
   always need one browser consent by a Tripleseat administrator once the application exists.

`pull_all()` runs whichever of the three are configured, each isolated from the others.

AUTH FOR ROUTE 3 — authorization code, not client credentials
--------------------------------------------------------------
Tripleseat has no machine-to-machine grant: an application always acts on behalf of a Tripleseat *user*, who
logs in and consents once in a browser. That is a poor fit for a nightly unattended job, so we do the consent
once by hand and keep the refresh token:

  1. `python -m sdp ts-auth-url`      prints the consent URL; open it, approve.
  2. Tripleseat redirects to TRIPLESEAT_REDIRECT_URI with ?code=... in the address bar.
  3. `python -m sdp ts-exchange --code <code>`  trades it for an access + refresh token and prints the
     refresh token. Put that in the repo secret TRIPLESEAT_REFRESH_TOKEN.
  4. Every run afterwards exchanges the refresh token for a 2-hour access token. No human involved.

Refresh tokens may ROTATE: an exchange can return a new refresh_token, and the old one then stops working.
A GitHub Actions job cannot write back to its own repo secrets, so a rotated token would strand the pipeline
after a single night. We therefore keep the current refresh token in the warehouse `meta` table, which is
already encrypted and persisted between runs as a release asset; the repo secret is only the bootstrap value
used when meta holds nothing. Precedence is meta -> secret, and a rotation is written back immediately.

Datasets (raw/tripleseat/<loc>/<dataset>/<key>.json):
  locations   _all/locations/all.json                 GET /v1/locations   — maps Tripleseat location_id -> our slug
  events      <loc>/events/<start>_<end>.json         GET /v1/events/search?location_ids&event_start_date&event_end_date
                                                       with show_financial=true for grand_total / actual_amount
  leads       <loc>/leads/<start>_<end>.json          GET /v1/leads/search — the pipeline behind future events

Everything is paginated 50 per page. Events are re-pulled for the whole window every run: unlike POS orders they
are edited for weeks before and after the date (guest counts, menus, final billing), and the volume is small
enough (hundreds, not hundreds of thousands) that a full refresh costs seconds.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import sqlite3
import time
from datetime import date, timedelta
from urllib.parse import urlencode

from .util import DB_PATH, Http, env, iso, log, settings, today_local, write_raw

AUTH_URL = "https://login.tripleseat.com/oauth2/authorize"
TOKEN_URL = "https://api.tripleseat.com/oauth2/token"
PAGE_SIZE = 50
META_KEY = "tripleseat_refresh_token"
CURSOR_KEY = "tripleseat_webhook_cursor"          # how many rows of the Tripleseat tab the warehouse has absorbed
DEFAULT_SCOPE = "read"
HOOK_PAGE = 300                                    # rows per Apps Script call (its own cap is HOOK_PAGE_MAX = 300); payloads are a few KB each


def _meta_get(key: str) -> str | None:
    if not DB_PATH.exists():
        return None
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def _meta_set(key: str, value: str) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        con.commit()
    finally:
        con.close()


def authorize_url() -> str:
    """The one-time consent URL. Scope is configurable because the docs give no enumerated list."""
    cfg = settings().get("tripleseat", {})
    return AUTH_URL + "?" + urlencode({"client_id": env("TRIPLESEAT_CLIENT_ID", required=True),
                                       "redirect_uri": env("TRIPLESEAT_REDIRECT_URI", required=True),
                                       "response_type": "code",
                                       "scope": cfg.get("scope", DEFAULT_SCOPE)})


def exchange_code(code: str) -> dict:
    """One-time: authorization code -> access + refresh token. Run locally, never in CI."""
    r = Http(TOKEN_URL).post(TOKEN_URL, {"grant_type": "authorization_code", "code": code,
                                         "client_id": env("TRIPLESEAT_CLIENT_ID", required=True),
                                         "client_secret": env("TRIPLESEAT_CLIENT_SECRET", required=True),
                                         "redirect_uri": env("TRIPLESEAT_REDIRECT_URI", required=True)})
    j = r.json()
    if not j.get("refresh_token"):
        raise RuntimeError(f"no refresh_token in Tripleseat response: {str(j)[:200]}")
    _meta_set(META_KEY, j["refresh_token"])
    return j


class Tripleseat:
    def __init__(self):
        cfg = settings().get("tripleseat", {})
        self.base = cfg.get("base_url", "https://api.tripleseat.com/v1")
        self.http = Http(self.base, headers={"Accept": "application/json"}, rps=float(cfg.get("requests_per_second", 5)))
        self.client_id = env("TRIPLESEAT_CLIENT_ID", required=True)
        self.client_secret = env("TRIPLESEAT_CLIENT_SECRET", required=True)
        self._token = None

    def token(self) -> str:
        """Exchange the stored refresh token for a 2-hour access token, honouring rotation."""
        if self._token:
            return self._token
        rt = _meta_get(META_KEY) or env("TRIPLESEAT_REFRESH_TOKEN")
        if not rt:
            raise RuntimeError("no Tripleseat refresh token: run `sdp ts-auth-url` then `sdp ts-exchange --code ...` "
                               "and set TRIPLESEAT_REFRESH_TOKEN")
        r = self.http.post(TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": rt,
                                       "client_id": self.client_id, "client_secret": self.client_secret})
        j = r.json()
        self._token = j.get("access_token")
        if not self._token:
            raise RuntimeError(f"no access_token in Tripleseat token response: {str(j)[:200]}")
        new_rt = j.get("refresh_token")
        if new_rt and new_rt != rt:
            # Rotation: the token we just used is now dead. Persist the replacement before any API call can
            # fail, so an interrupted run still leaves a usable token behind.
            _meta_set(META_KEY, new_rt)
            log.info("Tripleseat: refresh token rotated — stored in the warehouse")
        elif not _meta_get(META_KEY):
            _meta_set(META_KEY, rt)
        self.http.s.headers["Authorization"] = f"Bearer {self._token}"
        log.info("Tripleseat: authenticated (expires in %ss)", j.get("expires_in"))
        return self._token

    def _paged(self, path: str, params: dict, list_key: str) -> list[dict]:
        """Tripleseat returns {<list_key>: [...]} 50 at a time; walk until a short page."""
        self.token()
        out, page = [], 1
        while True:
            j = self.http.get(path, params=dict(params, page=page)).json()
            rows = j.get(list_key) if isinstance(j, dict) else j
            rows = rows or []
            out.extend(rows)
            if len(rows) < PAGE_SIZE:
                break
            page += 1
            if page > 400:                      # 20 000 rows — a runaway guard, never hit in practice
                log.warning("Tripleseat: %s stopped at page 400", path)
                break
        return out

    # ---- datasets --------------------------------------------------------------------------
    def locations(self) -> list[dict]:
        return self._paged("/locations", {}, "locations")

    def events(self, location_id: str | None, start: date, end: date) -> list[dict]:
        p = {"event_start_date": iso(start), "event_end_date": iso(end), "show_financial": "true"}
        if location_id:
            p["location_ids"] = location_id
        return self._paged("/events/search", p, "events")

    def leads(self, location_id: str | None, start: date, end: date) -> list[dict]:
        p = {"event_start_date": iso(start), "event_end_date": iso(end)}
        if location_id:
            p["location_ids"] = location_id
        return self._paged("/leads/search", p, "leads")


# ---- orchestration ------------------------------------------------------------------------

def pull(locations_cfg: list[dict], days_back: int, days_forward: int = 180) -> dict:
    """Pull events and leads for every location that has a tripleseat_location_id.

    The window runs FORWARD as well as back: the value of this source is partly the booked-but-not-yet-happened
    calendar, which is what a store director actually plans against."""
    ts = Tripleseat()
    end = today_local() + timedelta(days=days_forward)
    start = today_local() - timedelta(days=days_back)
    summary = {}

    locs = ts.locations()
    write_raw("tripleseat", "_all", "locations", "all", {"locations": locs})
    log.info("Tripleseat: %d locations visible: %s", len(locs),
             ", ".join(f"{l.get('id')}={l.get('name')}" for l in locs[:12]))

    for loc in locations_cfg:
        tid = str(loc.get("tripleseat_location_id") or "").strip()
        slug = loc["slug"]
        if not tid:
            # fall back to matching the Tripleseat location name against ours
            want = (loc.get("tripleseat_name") or loc.get("name") or "").strip().lower()
            hit = next((l for l in locs if str(l.get("name", "")).strip().lower() == want), None)
            if hit:
                tid = str(hit.get("id"))
                log.info("Tripleseat: %s matched location %s by name '%s'", slug, tid, hit.get("name"))
        if not tid:
            log.warning("Tripleseat: %s has no tripleseat_location_id (and no name match) — skipped", slug)
            continue
        ev = ts.events(tid, start, end)
        write_raw("tripleseat", slug, "events", f"{iso(start)}_{iso(end)}", {"events": ev, "window": [iso(start), iso(end)], "location_id": tid})
        ld = ts.leads(tid, start, end)
        write_raw("tripleseat", slug, "leads", f"{iso(start)}_{iso(end)}", {"leads": ld, "window": [iso(start), iso(end)], "location_id": tid})
        summary[slug] = {"events": len(ev), "leads": len(ld), "window": [iso(start), iso(end)]}
        log.info("Tripleseat %s: %s", slug, summary[slug])
    return summary


# ---- route 1: the public key ---------------------------------------------------------------

class PublicCatalog:
    """The three read endpoints the lead-form public key opens. Nothing here is paginated or dated: each call
    returns the whole thing, so the pull is three requests."""

    def __init__(self, key: str | None = None):
        cfg = settings().get("tripleseat", {})
        self.key = key or env("TRIPLESEAT_PUBLIC_KEY", required=True)
        self.http = Http(cfg.get("base_url", "https://api.tripleseat.com/v1"), headers={"Accept": "application/json"},
                         rps=float(cfg.get("requests_per_second", 5)))

    def _get(self, path: str) -> list | dict:
        try:
            r = self.http.get(path, params={"public_key": self.key})
        except PermissionError:
            raise PermissionError(f"Tripleseat rejected the public key on {path} (401/403)") from None
        except Exception as e:
            # requests quotes the full URL in its errors, and the URL carries the key. The workflow log is public.
            raise RuntimeError(f"Tripleseat {path}: {type(e).__name__}: {str(e).replace(self.key, '<public_key>')}") from None
        j = r.json()
        # The public endpoints wrap each record: [{"location": {...}}, ...]. Unwrap to plain records.
        if isinstance(j, list):
            return [next(iter(x.values())) if isinstance(x, dict) and len(x) == 1 else x for x in j]
        if isinstance(j, dict) and len(j) == 1 and isinstance(next(iter(j.values())), dict):
            return next(iter(j.values()))
        return j

    def locations(self) -> list[dict]:
        return self._get("/locations.json")

    def sites(self) -> list[dict]:
        return self._get("/sites.json")

    def lead_forms(self) -> list[dict]:
        return self._get("/lead_forms.json")


def match_location(loc: dict, catalog: list[dict]) -> str | None:
    """The Tripleseat location id for one of ours: configured id first, then an exact or 'ends with' name match
    ("Big Grove Brewery & Taproom - Omaha" matches tripleseat_name "Omaha")."""
    tid = str(loc.get("tripleseat_location_id") or "").strip()
    if tid:
        return tid
    want = (loc.get("tripleseat_name") or loc.get("name") or "").strip().lower()
    if not want:
        return None
    for l in catalog:
        name = str(l.get("name", "")).strip().lower()
        if name == want or name.endswith("- " + want) or name.endswith("– " + want):
            return str(l.get("id"))
    return None


def pull_catalog(locations_cfg: list[dict]) -> dict:
    """Locations + rooms, the site's picklists and billing rules, and the lead forms.

    Written under raw/tripleseat/_all/catalog/ and loaded by transform into ts_rooms / ts_catalog. Also the
    place the location mapping is checked every night: a taproom whose tripleseat_location_id matches nothing
    in the account is named in the log rather than silently attributed nowhere."""
    pc = PublicCatalog()
    locs = pc.locations()
    write_raw("tripleseat", "_all", "catalog", "locations", {"locations": locs})
    sites = pc.sites()
    write_raw("tripleseat", "_all", "catalog", "sites", {"sites": sites})
    forms = pc.lead_forms()
    write_raw("tripleseat", "_all", "catalog", "lead_forms", {"lead_forms": forms})

    by_id = {str(l.get("id")): l for l in locs}
    mapped, unmapped = {}, []
    for loc in locations_cfg:
        tid = match_location(loc, locs)
        if tid and tid in by_id:
            mapped[loc["slug"]] = tid
        else:
            unmapped.append(loc["slug"])
    n_rooms = sum(len(l.get("rooms") or []) for l in locs)
    log.info("Tripleseat catalog: %d locations, %d rooms, %d sites, %d lead forms; mapped %s%s",
             len(locs), n_rooms, len(sites), len(forms),
             ", ".join(f"{s}={t}" for s, t in mapped.items()) or "none",
             f"; UNMAPPED: {', '.join(unmapped)}" if unmapped else "")
    extra = [f"{l.get('id')}={l.get('name')}" for l in locs if str(l.get("id")) not in mapped.values()]
    if extra:
        log.info("Tripleseat catalog: locations in the account that are not a taproom: %s", ", ".join(extra))
    return {"locations": len(locs), "rooms": n_rooms, "sites": len(sites), "lead_forms": len(forms), "mapped": mapped, "unmapped": unmapped}


# ---- route 2: webhook rows collected by the Apps Script -------------------------------------

def _script_json(http: Http, url: str, body: dict, tries: int = 3) -> dict:
    """POST to the Apps Script and insist on a JSON answer, walking the redirect by hand.

    Apps Script answers a POST with a 302 to a one-time script.googleusercontent.com URL that serves the output.
    Seen on 2026-09-21 from the GitHub runner, three times out of four: that hop came back as an HTML page or a
    404 while the script itself had completed — the content was not yet readable where the runner's GET landed,
    and requests' automatic redirect gives it exactly one immediate try. So: POST without following, then fetch
    the Location ourselves with a fresh GET (no JSON content-type, a short pause, up to five tries), and only when
    the key really is dead re-POST. The POST is idempotent (it only reads rows), so asking again costs nothing."""
    s = http.s
    last = ""
    for attempt in range(tries):
        if attempt:
            time.sleep(5 * attempt)
        r = s.post(url, json=body, timeout=http.timeout, allow_redirects=False)
        candidates = [r] if r.status_code == 200 else []
        loc = r.headers.get("Location") if r.status_code in (301, 302, 303, 307, 308) else None
        if not candidates and not loc:
            last = f"HTTP {r.status_code} {(r.text or '')[:120]!r}"
            log.warning("Apps Script: %s on attempt %d — retrying", last, attempt + 1)
            continue
        for g in range(5 if loc else 1):
            if loc:
                if g:
                    time.sleep(1.5 * g)
                r2 = s.get(loc, timeout=http.timeout, headers={"Content-Type": None, "Accept": "application/json,text/plain,*/*"})
            else:
                r2 = candidates[0]
            if r2.status_code == 200:
                try:
                    return r2.json()
                except ValueError:
                    last = f"200 but not JSON: {(r2.text or '')[:100]!r}"
            elif r2.status_code == 404:
                last = "404 from the output URL"
            else:
                last = f"HTTP {r2.status_code} from the output URL"
                break
            log.warning("Apps Script output not ready (%s), try %d/%d", last, g + 1, 5 if loc else 1)
    raise RuntimeError(f"Apps Script never answered with JSON: {last}")


def _cursor() -> int:
    v = _meta_get(CURSOR_KEY)
    try:
        return int(v or 0)
    except ValueError:
        return 0


def pull_webhooks(from_start: bool = False) -> dict:
    """Collect the rows Tripleseat has posted to the Apps Script since the warehouse last absorbed any.

    The tab is append-only and the warehouse persists between runs, so only the tail is fetched: `since` is the
    number of data rows already loaded (kept in warehouse meta by transform, AFTER a successful load, so a run
    that dies between pull and transform simply fetches the same rows again next time). The Apps Script pages
    the answer; each page is written to raw/ and transform reads them in order.

    `from_start` (the workflow's `backfill` input) re-reads the whole tab — it is the complete record of the
    route, so transform rebuilds every webhook/seed row from it (newest state per object wins, whatever the
    order on the tab). A few thousand rows is a dozen calls."""
    url = env("APPS_SCRIPT_URL", required=True)
    secret = env("PORTAL_SECRET", required=True)
    key = base64.b64encode(hmac.new(secret.encode("utf-8"), b"tripleseat", hashlib.sha256).digest()).decode()
    http = Http(url, headers={"Content-Type": "application/json"}, rps=2)
    since = 0 if from_start else _cursor()
    if from_start:
        log.info("Tripleseat webhooks: re-reading the whole tab (backfill) — the warehouse had absorbed %d rows", _cursor())
    start, total, page, got = since, None, 0, 0
    while True:
        j = _script_json(http, url, {"a": "tripleseat", "k": key, "since": since, "limit": HOOK_PAGE})
        if not j.get("ok"):
            raise RuntimeError(f"tripleseat webhook fetch refused: {str(j.get('error'))[:200]}")
        rows = j.get("rows") or []
        total = j.get("total")
        if rows:
            write_raw("tripleseat", "_all", "webhook", f"{since:08d}", {"since": since, "header": j.get("header"), "rows": rows,
                                                                        "fetched_at": j.get("fetched_at")})
            got += len(rows)
            since += len(rows)
            page += 1
        # Stop on the script's word (total), not on a short page: the script may cap a page below HOOK_PAGE.
        if not rows or (total is not None and since >= int(total)):
            break
        if page > 200:                          # 80 000 rows in one night is not a webhook feed, it is a bug
            log.warning("Tripleseat webhooks: stopped after 200 pages")
            break
    log.info("Tripleseat webhooks: %d new rows (tab holds %s; warehouse had absorbed %d)", got, total, start)
    return {"rows": got, "total": total, "cursor": start}


# ---- all three, each on its own ---------------------------------------------------------------

def pull_all(locations_cfg: list[dict], days_back: int, days_forward: int = 180, backfill: bool = False) -> dict:
    """Run every configured route. A failure in one is logged and the others still run; the caller decides
    whether 'nothing configured' is a warning (it is). `backfill` makes the webhook route re-read its whole tab."""
    out, ran = {}, []
    if env("TRIPLESEAT_PUBLIC_KEY"):
        ran.append("catalog")
        try:
            out["catalog"] = pull_catalog(locations_cfg)
        except Exception as e:
            log.error("Tripleseat catalog pull failed (%s: %s)", type(e).__name__, e)
            out["catalog"] = {"error": str(e)}
    if env("APPS_SCRIPT_URL") and env("PORTAL_SECRET"):
        ran.append("webhooks")
        try:
            out["webhooks"] = pull_webhooks(from_start=backfill)
        except Exception as e:
            log.error("Tripleseat webhook collection failed (%s: %s)", type(e).__name__, e)
            out["webhooks"] = {"error": str(e)}
    if env("TRIPLESEAT_CLIENT_ID") and env("TRIPLESEAT_CLIENT_SECRET"):
        ran.append("api")
        try:
            out["api"] = pull(locations_cfg, days_back=days_back, days_forward=days_forward)
        except Exception as e:
            log.error("Tripleseat API pull failed (%s: %s)", type(e).__name__, e)
            out["api"] = {"error": str(e)}
    if not ran:
        log.warning("Tripleseat: nothing configured (TRIPLESEAT_PUBLIC_KEY, APPS_SCRIPT_URL+PORTAL_SECRET, or the OAuth trio) — skipped")
    elif all(isinstance(v, dict) and "error" in v for v in out.values()):
        raise RuntimeError("every Tripleseat route failed: " + ", ".join(ran))
    return out
