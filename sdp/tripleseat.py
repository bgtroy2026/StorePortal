"""Tripleseat API pull — private events, bookings and leads.

API:  https://api.tripleseat.com/v1/       OAuth 2.0 Bearer
Rate: 10 req/s (429 past that) — far looser than Toast or MarginEdge, so we run at 5 req/s.
Docs: Settings > Tripleseat API > Documentation in the account; OpenAPI at
      https://api.tripleseat.com/api-docs/v1/openapi.yaml

AUTH — authorization code, not client credentials
-------------------------------------------------
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

import sqlite3
from datetime import date, timedelta
from urllib.parse import urlencode

from .util import DB_PATH, Http, env, iso, log, settings, today_local, write_raw

AUTH_URL = "https://login.tripleseat.com/oauth2/authorize"
TOKEN_URL = "https://api.tripleseat.com/oauth2/token"
PAGE_SIZE = 50
META_KEY = "tripleseat_refresh_token"
DEFAULT_SCOPE = "read"


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
