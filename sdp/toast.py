"""Toast API pull.

Auth:  POST {host}/authentication/v1/authentication/login
       {"clientId","clientSecret","userAccessType":"TOAST_MACHINE_CLIENT"} -> token.accessToken (Bearer)
Every other call needs `Toast-Restaurant-External-ID: <restaurant guid>`.
Production host is https://ws-api.toasttab.com (sandbox: https://ws-sandbox-api.eng.toasttab.com) — set in
config/settings.json or override with TOAST_HOST.

Datasets pulled per restaurant:
  restaurants/v1/restaurants/{guid}         -> raw/toast/<loc>/restaurant/info.json   (closeoutHour, tz)
  orders/v2/ordersBulk?businessDate=yyyymmdd -> raw/toast/<loc>/orders/<YYYY-MM-DD>.json  (one file per business day)
  labor/v1/timeEntries?startDate&endDate    -> raw/toast/<loc>/timeEntries/<start>_<end>.json (≤30-day windows)
  labor/v1/shifts?startDate&endDate         -> raw/toast/<loc>/shifts/<start>_<end>.json (SCHEDULED shifts)
  labor/v1/jobs                             -> raw/toast/<loc>/jobs/all.json
  menus/v2/menus                            -> raw/toast/<loc>/menus/all.json  (item -> sales category names)
  config/v2/<resource>                      -> raw/toast/<loc>/config-<resource>/all.json  (guid -> name lookups)
  labor/v1/employees                        -> raw/toast/<loc>/employees/all.json      (guid -> person)
  cashmgmt/v1/entries?businessDate=yyyymmdd -> raw/toast/<loc>/cash/<YYYY-MM-DD>.json   (drawer movements)
  cashmgmt/v1/deposits?businessDate=...     -> raw/toast/<loc>/deposits/<YYYY-MM-DD>.json

Rate limits: ordersBulk is capped at 5 req/s per location; we run at 4.
Incremental strategy: re-pull the last `incremental_days` business days every night (orders get modified after
close: tips adjusted, voids, refunds), plus any day with no raw file yet inside the backfill window.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import time

from .util import Http, RAW_DIR, env, iso, log, settings, today_local, write_raw


# Guid -> name lookups worth having. diningOptions and revenueCenters rename columns we already store;
# the rest are landed in toast_config so the views that need them do not each need a new pull.
CONFIG_RESOURCES = ["diningOptions", "revenueCenters", "salesCategories", "voidReasons", "discounts", "serviceAreas",
                    "payoutReasons", "noSaleReasons", "cashDrawers"]

# Cash management takes ONE business date per call — no ranges, no paging — so a 400-day backfill would be
# 800 requests per location. It is only ever pulled for a recent window; the drawer questions it answers
# ("who was short last Friday") are about the last few weeks, not last year.
CASH_DAYS = 35


class Toast:
    def __init__(self):
        cfg = settings()["toast"]
        self.host = env("TOAST_HOST", cfg["host"]).rstrip("/")
        self.page_size = int(cfg.get("page_size", 100))
        self.labor_window = int(cfg.get("labor_window_days", 30))
        self.http = Http(self.host, headers={"Accept": "application/json"}, rps=float(cfg.get("requests_per_second_per_location", 4)))
        self._token = None
        self._token_exp = datetime.min
        self.client_id = env("TOAST_CLIENT_ID", required=True)
        self.client_secret = env("TOAST_CLIENT_SECRET", required=True)

    # ---- auth ------------------------------------------------------------------------------
    def token(self) -> str:
        if self._token and datetime.utcnow() < self._token_exp:
            return self._token
        r = self.http.post("/authentication/v1/authentication/login",
                           {"clientId": self.client_id, "clientSecret": self.client_secret, "userAccessType": "TOAST_MACHINE_CLIENT"})
        j = r.json()
        tok = j["token"]
        self._token = tok["accessToken"]
        self._token_exp = datetime.utcnow() + timedelta(seconds=int(tok.get("expiresIn", 3600)) - 120)
        self.http.s.headers["Authorization"] = f"Bearer {self._token}"
        return self._token

    def _h(self, guid: str) -> dict:
        self.token()
        return {"Toast-Restaurant-External-ID": guid}

    # ---- datasets --------------------------------------------------------------------------
    def restaurant(self, guid: str) -> dict:
        return self.http.get(f"/restaurants/v1/restaurants/{guid}", headers=self._h(guid)).json()

    def accessible_restaurants(self) -> list[dict]:
        """Partner-scoped list of restaurants this client can see (useful to find GUIDs)."""
        self.token()
        return self.http.get("/partners/v1/restaurants").json()

    def orders_for_business_date(self, guid: str, d: date) -> list[dict]:
        out, page = [], 1
        bd = d.strftime("%Y%m%d")
        while True:
            r = self.http.get("/orders/v2/ordersBulk", params={"businessDate": bd, "pageSize": self.page_size, "page": page}, headers=self._h(guid))
            batch = r.json() or []
            out.extend(batch)
            if len(batch) < self.page_size:
                break
            page += 1
        return out

    def time_entries(self, guid: str, start: date, end: date) -> list[dict]:
        # API accepts up to 30 days per call; startDate/endDate are ISO-8601 datetimes (UTC ok)
        out = []
        s = start
        while s <= end:
            e = min(end, s + timedelta(days=self.labor_window - 1))
            r = self.http.get("/labor/v1/timeEntries",
                              params={"startDate": f"{iso(s)}T00:00:00.000-0600", "endDate": f"{iso(e)}T23:59:59.999-0600", "includeArchived": "true"},
                              headers=self._h(guid))
            out.extend(r.json() or [])
            s = e + timedelta(days=1)
        return out

    def shifts(self, guid: str, start: date, end: date) -> list[dict]:
        """SCHEDULED shifts — what the schedule said, as opposed to timeEntries, which is what people actually
        punched. Same ≤1-month windowing as time entries."""
        out = []
        s = start
        while s <= end:
            e = min(end, s + timedelta(days=self.labor_window - 1))
            r = self.http.get("/labor/v1/shifts",
                              params={"startDate": f"{iso(s)}T00:00:00.000-0600", "endDate": f"{iso(e)}T23:59:59.999-0600"},
                              headers=self._h(guid))
            out.extend(r.json() or [])
            s = e + timedelta(days=1)
        return out

    def cash_entries(self, guid: str, d: date) -> list[dict]:
        """Drawer movements for one business day. Returns a bare JSON array."""
        r = self.http.get("/cashmgmt/v1/entries", params={"businessDate": d.strftime("%Y%m%d")}, headers=self._h(guid))
        j = r.json()
        return j if isinstance(j, list) else []

    def cash_deposits(self, guid: str, d: date) -> list[dict]:
        r = self.http.get("/cashmgmt/v1/deposits", params={"businessDate": d.strftime("%Y%m%d")}, headers=self._h(guid))
        j = r.json()
        return j if isinstance(j, list) else []

    def jobs(self, guid: str) -> list[dict]:
        return self.http.get("/labor/v1/jobs", headers=self._h(guid)).json() or []

    def menus(self, guid: str) -> dict:
        return self.http.get("/menus/v2/menus", headers=self._h(guid)).json() or {}

    def _paged(self, path: str, guid: str, list_key: str | None = None) -> list[dict]:
        """A config/labor lookup list. Toast pages these with a `pageToken` query parameter and returns the
        next token in the Toast-Next-Page-Token header; absence of the header means this was the last page."""
        out: list[dict] = []
        token, pages = None, 0
        while pages < 100:
            params = {"pageSize": 200}
            if token:
                params["pageToken"] = token
            r = self.http.get(path, params=params, headers=self._h(guid))
            j = r.json() or []
            out.extend(j if isinstance(j, list) else (j.get(list_key or "", []) or []))
            token = r.headers.get("Toast-Next-Page-Token")
            pages += 1
            if not token:
                break
        return out

    def stock(self, guid: str) -> list[dict]:
        """Menu items whose stock status is not plain IN_STOCK, as of this moment."""
        out = []
        for status in ("OUT_OF_STOCK", "QUANTITY"):
            r = self.http.get("/stock/v1/inventory", params={"status": status}, headers=self._h(guid), retries=2)   # optional: fail fast
            j = r.json()
            out.extend(j if isinstance(j, list) else [])
        return out

    def config_list(self, guid: str, resource: str) -> list[dict]:
        """One config/v2 lookup table. Orders and their lines reference these by guid only — the names that
        make a report readable (revenue centre, dining option, void reason) live here and nowhere else."""
        return self._paged(f"/config/v2/{resource}", guid, resource)

    def employees(self, guid: str) -> list[dict]:
        return self._paged("/labor/v1/employees", guid, "employees")


# ---- orchestration ------------------------------------------------------------------------

def _existing_days(slug: str) -> set[str]:
    d = RAW_DIR / "toast" / slug / "orders"
    return {p.stem for p in d.glob("*.json")} if d.exists() else set()


def pull(locations: list[dict], days_back: int, incremental_days: int, warehouse_days: set[tuple[str, str]] | None = None,
         max_minutes: float | None = None, newest_first: bool = False) -> dict:
    """Pull Toast for each configured restaurant. `warehouse_days` = (slug, business_date) already loaded in the DB,
    so a fresh runner (no raw/ cache) still only re-pulls the incremental window plus missing days.

    Stops cleanly at `max_minutes` so a long first backfill spans several nightly runs instead of being lost to a
    job timeout — whatever was pulled is transformed and persisted, and the next run picks up the missing days."""
    t = Toast()
    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    stopped = False
    end = today_local() - timedelta(days=1)          # last complete business day
    start = end - timedelta(days=days_back)
    warehouse_days = warehouse_days or set()
    summary = {}
    for loc in locations:
        guid = (loc.get("toast_guid") or "").strip()
        slug = loc["slug"]
        if not guid:
            log.warning("Toast: %s has no toast_guid in config/locations.json — skipped", slug)
            continue
        if stopped:
            break
        info = t.restaurant(guid); write_raw("toast", slug, "restaurant", "info", info)
        write_raw("toast", slug, "jobs", "all", {"jobs": t.jobs(guid)})
        write_raw("toast", slug, "menus", "all", t.menus(guid))

        # Lookup tables. Each is one small request and each is optional: a credential without the config or
        # labor scope should cost us readable names, never the orders pull that everything else depends on.
        for res in CONFIG_RESOURCES:
            try:
                write_raw("toast", slug, f"config-{res}", "all", {res: t.config_list(guid, res)})
            except Exception as e:
                log.warning("Toast: %s config/%s not pulled (%s) — related fields will show guids", slug, res, e)
        try:
            write_raw("toast", slug, "employees", "all", {"employees": t.employees(guid)})
        except Exception as e:
            log.warning("Toast: %s employees not pulled (%s) — server/shift names unavailable", slug, e)

        have = _existing_days(slug) | {d for s, d in warehouse_days if s == slug}
        days = []
        d = start
        while d <= end:
            if d > end - timedelta(days=incremental_days) or iso(d) not in have:
                days.append(d)
            d += timedelta(days=1)
        loc_deadline = deadline
        if newest_first and deadline:
            # Share what is left of the budget evenly across the taprooms still to come, so the first one in the
            # list cannot spend the whole run healing its own history while the other five get nothing.
            left = len(locations) - locations.index(loc)
            loc_deadline = time.monotonic() + max(0.0, deadline - time.monotonic()) / max(left, 1)
        if newest_first:
            # When healing history under a time budget, the recent past is worth more than last autumn: whatever
            # the budget reaches should be the days people actually look at.
            days.sort(reverse=True)
        n_orders = 0
        t0, last_note = time.monotonic(), time.monotonic()
        if days:
            log.info("Toast %s: %d business days to pull (%s..%s)", slug, len(days), iso(min(days)), iso(max(days)))
        for n, d in enumerate(days, 1):
            # progress every ~2 minutes, with a rate-based estimate — a silent hour-long backfill is unreviewable
            if time.monotonic() - last_note > 120:
                rate = n / max(time.monotonic() - t0, 1)
                log.info("Toast %s: %d/%d days (%.1f%%), %d orders so far, ~%.0f min left for this location",
                         slug, n, len(days), 100.0 * n / len(days), n_orders, (len(days) - n) / rate / 60)
                last_note = time.monotonic()
            if deadline and time.monotonic() > deadline:
                if not stopped:
                    log.warning("Toast: time budget reached — stopping cleanly; the next run continues where this left off")
                stopped = True
                break
            if newest_first and loc_deadline and time.monotonic() > loc_deadline:
                log.info("Toast %s: this taproom's share of the heal budget is spent (%d of %d days) — moving on", slug, n - 1, len(days))
                break
            orders = t.orders_for_business_date(guid, d)
            n_orders += len(orders)
            write_raw("toast", slug, "orders", iso(d), {"businessDate": iso(d), "orders": orders})
        # labor: incremental window + any backfill gap, in ≤30-day chunks
        lab_start = min(days) if days else end - timedelta(days=incremental_days)
        te = t.time_entries(guid, lab_start, end)
        write_raw("toast", slug, "timeEntries", f"{iso(lab_start)}_{iso(end)}", {"timeEntries": te, "window": [iso(lab_start), iso(end)]})
        # Scheduled shifts. Optional in the same way the config lookups are: a credential without the schedule
        # scope should cost us the schedule-vs-actual view, never the labor data everything else depends on.
        n_sh = 0
        try:
            # A week past `end`: the overtime view needs what is still scheduled this week, not only what was.
            sh_end = end + timedelta(days=8)
            sh = t.shifts(guid, lab_start, sh_end)
            write_raw("toast", slug, "shifts", f"{iso(lab_start)}_{iso(sh_end)}", {"shifts": sh, "window": [iso(lab_start), iso(sh_end)]})
            n_sh = len(sh)
        except Exception as e:
            log.warning("Toast: %s shifts not pulled (%s) — schedule-vs-actual unavailable", slug, e)
        # Cash management, recent window only (see CASH_DAYS). Optional like the other lookups: a credential
        # without the cash scope should cost the drawer view and nothing else.
        n_cash = 0
        try:
            c_start = max(start, end - timedelta(days=CASH_DAYS - 1))
            d = c_start
            while d <= end:
                if not (RAW_DIR / "toast" / slug / "cash" / f"{iso(d)}.json").exists() or d > end - timedelta(days=incremental_days):
                    write_raw("toast", slug, "cash", iso(d), {"businessDate": iso(d), "entries": t.cash_entries(guid, d)})
                    write_raw("toast", slug, "deposits", iso(d), {"businessDate": iso(d), "deposits": t.cash_deposits(guid, d)})
                    n_cash += 1
                d += timedelta(days=1)
        except Exception as e:
            log.warning("Toast: %s cash management not pulled (%s) — the drawer view will be unavailable", slug, e)

        # What is 86'd right now. Optional like every other lookup: no stock scope costs this one view.
        try:
            st = t.stock(guid)
            write_raw("toast", slug, "stock", iso(today_local()), {"snap_date": iso(today_local()), "items": st})
        except Exception as e:
            log.warning("Toast: %s stock not pulled (%s) — the out-of-stock view will be unavailable", slug, e)

        summary[slug] = {"days_pulled": len(days), "orders": n_orders, "time_entries": len(te), "shifts": n_sh,
                         "cash_days": n_cash, "window": [iso(start), iso(end)], "stopped_early": stopped}
        log.info("Toast %s: %s", slug, summary[slug])
    return summary
