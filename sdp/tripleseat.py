"""Tripleseat API pull — private events, bookings and leads.

API:  https://api.tripleseat.com/v1/       OAuth 2.0 bearer (client-credentials grant)
Auth: POST https://api.tripleseat.com/oauth2/token  {grant_type, client_id, client_secret} -> access_token (2h)
Rate: 10 req/s, 1 200/min, 18 000/hour — far looser than Toast or MarginEdge, so we run at 5 req/s.
Docs: https://api.tripleseat.com/api-docs/v1/openapi.yaml (public), Settings > API in the Tripleseat account.

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

from datetime import date, timedelta

from .util import Http, env, iso, log, settings, today_local, write_raw

TOKEN_URL = "https://api.tripleseat.com/oauth2/token"
PAGE_SIZE = 50


class Tripleseat:
    def __init__(self):
        cfg = settings().get("tripleseat", {})
        self.base = cfg.get("base_url", "https://api.tripleseat.com/v1")
        self.http = Http(self.base, headers={"Accept": "application/json"}, rps=float(cfg.get("requests_per_second", 5)))
        self.client_id = env("TRIPLESEAT_CLIENT_ID", required=True)
        self.client_secret = env("TRIPLESEAT_CLIENT_SECRET", required=True)
        self._token = None

    def token(self) -> str:
        if self._token:
            return self._token
        r = self.http.post(TOKEN_URL, {"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret})
        j = r.json()
        self._token = j.get("access_token") or j.get("token")
        if not self._token:
            raise RuntimeError(f"no access_token in Tripleseat token response: {str(j)[:200]}")
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
