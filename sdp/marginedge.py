"""MarginEdge Public API pull.

API:  https://api.marginedge.com/public   read-only · header `x-api-key` · opaque `nextPage` cursor
      rate limit: 1 request / second / API key across all endpoints (429 on excess → we throttle at 1 rps)
Docs: https://developer.marginedge.com    (key: MarginEdge Admin → name → Settings → Security → Create new API key;
      keys created before 2026-08-04 cannot call the bulk-export endpoints)

Datasets pulled per restaurant unit (raw/marginedge/<loc>/<dataset>/<key>.json):
  restaurantUnits      _all/restaurantUnits/units.json
  categories, vendors, products        <dataset>/all.json          full refresh each run (small)
  orders               orders/<start>_<end>.json                    invoice headers in the window
  orderDetail          orderDetail/<orderId>.json                   line items; fetched once unless still non-final
  salesReport          salesReport/<YYYY-MM-DD>.json                GET /sales/report per business day (Toast → ME integration)
  pnl                  pnl/<YYYY-MM-DD>.json                        GET /profitAndLoss/report per business day (labor, cogs, expenses)
  inventories          inventories/list.json                        GET /inventories (window)
  inventoryDetail      inventoryDetail/<inventoryId>.json           sections + every counted item (value per product)

Because of the 1 rps limit a full 400-day backfill is ~2 400 report calls per report type; `pull()` therefore
skips any (unit, day) already in the warehouse and stops cleanly at `max_minutes`, so a long backfill can span
several nightly runs without losing work.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

from .util import Http, env, iso, log, settings, today_local, write_raw

HDR = "x-api-key"
FINAL_STATUSES = {"CLOSED"}


class MarginEdge:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        cfg = settings()["marginedge"]
        self.key = api_key or env("MARGINEDGE_API_KEY", required=True)
        self.http = Http(base_url or cfg["base_url"], headers={HDR: self.key, "Accept": "application/json"}, rps=float(cfg.get("requests_per_second", 1)))

    # ---- low level -------------------------------------------------------------------------
    def _paged(self, path: str, params: dict, list_key: str):
        params = dict(params)
        while True:
            j = self.http.get(path, params=params).json()
            for it in j.get(list_key, []) or []:
                yield it
            nxt = j.get("nextPage")
            if not nxt:
                break
            params["nextPage"] = nxt

    # ---- reference data --------------------------------------------------------------------
    def restaurant_units(self) -> list[dict]:
        return self.http.get("/restaurantUnits").json().get("restaurants", [])

    def categories(self, unit_id: str) -> list[dict]:
        return list(self._paged("/categories", {"restaurantUnitId": unit_id}, "categories"))

    def vendors(self, unit_id: str) -> list[dict]:
        return list(self._paged("/vendors", {"restaurantUnitId": unit_id}, "vendors"))

    def products(self, unit_id: str) -> list[dict]:
        return list(self._paged("/products", {"restaurantUnitId": unit_id}, "products"))

    # ---- purchasing ------------------------------------------------------------------------
    def orders(self, unit_id: str, start: date, end: date, status: str | None = None) -> list[dict]:
        p = {"restaurantUnitId": unit_id, "startDate": iso(start), "endDate": iso(end)}
        if status and status != "ALL":
            p["orderStatus"] = status
        return list(self._paged("/orders", p, "orders"))

    def order_detail(self, unit_id: str, order_id: str) -> dict:
        return self.http.get(f"/orders/{order_id}", params={"restaurantUnitId": unit_id}).json()

    # ---- reports (Toast sales + labor arrive via the ME↔Toast integration) -----------------
    def sales_report(self, unit_id: str, start: date, end: date) -> dict:
        """{salesReports:[{restaurantUnitId, startDate, endDate, summary:{totalSales}, categories:[{id,name,total,percentOfTotalSales}]}]}"""
        return self.http.get("/sales/report", params={"restaurantUnitId": unit_id, "startDate": iso(start), "endDate": iso(end)}).json()

    def pnl_report(self, unit_id: str, start: date, end: date) -> dict:
        """{profitAndLossReports:[{..., summary:{grossProfit, primeCostTotal, controllableProfit, ...%OfSales},
             income|cogs|labor|expenses: {total, totalPercentOfSales, categories:[{id,name,total,percentOfSales,items:[{name,total,percentOfSales}]}], items:[...]}}]}"""
        return self.http.get("/profitAndLoss/report", params={"restaurantUnitId": unit_id, "startDate": iso(start), "endDate": iso(end)}).json()

    # ---- inventory -------------------------------------------------------------------------
    def countsheets(self, unit_id: str) -> list[dict]:
        return list(self._paged("/countsheets", {"restaurantUnitId": unit_id}, "countsheets"))

    def inventories(self, unit_id: str, start: date, end: date) -> list[dict]:
        # max range 365 days per call
        out, s = [], start
        while s <= end:
            e = min(end, s + timedelta(days=364))
            out.extend(self._paged("/inventories", {"restaurantUnitId": unit_id, "startDate": iso(s), "endDate": iso(e)}, "inventories"))
            s = e + timedelta(days=1)
        return out

    def inventory_detail(self, unit_id: str, inventory_id: str) -> dict:
        """Inventory header + sections, with every section's items inlined under section['items']."""
        params = {"restaurantUnitId": unit_id}
        head, sections = None, []
        while True:
            j = self.http.get(f"/inventories/{inventory_id}", params=params).json()
            head = head or {k: v for k, v in j.items() if k not in ("sections", "nextPage")}
            sections.extend(j.get("sections") or [])
            if not j.get("nextPage"):
                break
            params["nextPage"] = j["nextPage"]
        for s in sections:
            s["items"] = list(self._paged(f"/inventories/{inventory_id}/sections/{s['sectionId']}/items", {"restaurantUnitId": unit_id}, "items"))
        head["sections"] = sections
        return head


# ---- orchestration ------------------------------------------------------------------------

class Budget:
    def __init__(self, max_minutes: float | None):
        self.deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
        self.exhausted = False

    def ok(self) -> bool:
        if self.deadline and time.monotonic() > self.deadline:
            if not self.exhausted:
                log.warning("MarginEdge: time budget reached — stopping cleanly; the next run continues where this left off")
            self.exhausted = True
        return not self.exhausted


def pull(locations: list[dict], days_back: int, incremental_days: int = 7, have: dict | None = None, max_minutes: float | None = None) -> dict:
    """Pull for the given locations.
    `have` = {"orders": {(slug, orderId)}, "sales_days": {(slug, date)}, "pnl_days": {(slug, date)}, "inventories": {(slug, inventoryId)}}
    — what the warehouse already holds, so only the incremental window plus gaps are fetched."""
    me = MarginEdge()
    have = have or {}
    have_orders, have_sales, have_pnl, have_inv = (have.get(k, set()) for k in ("orders", "sales_days", "pnl_days", "inventories"))
    budget = Budget(max_minutes)
    end = today_local() - timedelta(days=1)          # last complete business day
    start = end - timedelta(days=days_back)
    inc_start = end - timedelta(days=incremental_days - 1)
    summary = {}

    units = me.restaurant_units()
    write_raw("marginedge", "_all", "restaurantUnits", "units", {"restaurants": units})
    log.info("MarginEdge: %d restaurant units visible to this key: %s", len(units), ", ".join(f"{u.get('id')}={u.get('name')}" for u in units))

    by_name = {str(u.get("name", "")).strip().lower(): str(u.get("id")) for u in units}
    for loc in locations:
        uid = str(loc.get("marginedge_unit_id") or "").strip()
        slug = loc["slug"]
        if not uid and loc.get("me_name"):
            uid = by_name.get(loc["me_name"].strip().lower(), "")   # fall back to matching the unit name from config
            if uid:
                log.info("MarginEdge: %s matched unit %s by name '%s'", slug, uid, loc["me_name"])
        if not uid:
            log.warning("MarginEdge: %s has no marginedge_unit_id (and no me_name match) in config/locations.json — skipped", slug)
            continue
        if not budget.ok():
            break
        s = {"window": [iso(start), iso(end)]}
        cats = me.categories(uid); write_raw("marginedge", slug, "categories", "all", {"categories": cats}); s["categories"] = len(cats)
        vends = me.vendors(uid);   write_raw("marginedge", slug, "vendors", "all", {"vendors": vends}); s["vendors"] = len(vends)
        prods = me.products(uid);  write_raw("marginedge", slug, "products", "all", {"products": prods}); s["products"] = len(prods)

        # invoices: headers for the whole window (cheap: paginated), detail only where missing / not yet CLOSED
        orders = me.orders(uid, start, end)
        write_raw("marginedge", slug, "orders", f"{iso(start)}_{iso(end)}", {"orders": orders, "window": [iso(start), iso(end)]})
        s["orders"] = len(orders); s["order_details"] = 0
        for o in orders:
            if not budget.ok():
                break
            oid = str(o.get("orderId"))
            if (slug, oid) in have_orders and o.get("status") in FINAL_STATUSES:
                continue
            write_raw("marginedge", slug, "orderDetail", oid, me.order_detail(uid, oid)); s["order_details"] += 1

        # daily sales report + daily P&L: incremental window always, older days only if missing
        s["sales_days"] = s["pnl_days"] = 0
        d = start
        while d <= end and budget.ok():
            k = (slug, iso(d))
            if d >= inc_start or k not in have_sales:
                write_raw("marginedge", slug, "salesReport", iso(d), me.sales_report(uid, d, d)); s["sales_days"] += 1
            if d >= inc_start or k not in have_pnl:
                write_raw("marginedge", slug, "pnl", iso(d), me.pnl_report(uid, d, d)); s["pnl_days"] += 1
            d += timedelta(days=1)

        # inventories: list the window, pull detail for new/changed ones
        if budget.ok():
            invs = me.inventories(uid, start, end)
            write_raw("marginedge", slug, "inventories", "list", {"inventories": invs, "window": [iso(start), iso(end)]})
            s["inventories"] = len(invs); s["inventory_details"] = 0
            for inv in invs:
                if not budget.ok():
                    break
                iid = str(inv.get("inventoryId"))
                if (slug, iid, inv.get("savedDate") or inv.get("closedDate") or "") in have_inv:
                    continue
                write_raw("marginedge", slug, "inventoryDetail", iid, me.inventory_detail(uid, iid)); s["inventory_details"] += 1
        summary[slug] = s
        log.info("MarginEdge %s: %s", slug, s)
    return summary
