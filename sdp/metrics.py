"""Metrics: warehouse -> compact JSON payload(s) for the dashboard.

The dashboard computes flexible-window KPIs (WTD/MTD/YTD/L7/L28, vs prior year) client-side from the daily
rows; item/labor/vendor/hourly detail is pre-aggregated here for fixed windows to keep bundles small.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from .transform import connect
from .util import locations, log, settings

DAILY_COLS = ["business_date", "net_sales", "gross_sales", "discounts", "tax", "tips", "refunds", "orders", "checks", "guests",
              "sales_food", "sales_beer", "sales_liquor", "sales_wine", "sales_nabev", "sales_retail", "sales_other",
              "labor_hours", "labor_cost", "purchases", "purch_food", "purch_beer", "purch_liquor", "purch_wine", "purch_nabev", "purch_retail", "purch_other"]
DAILY_KEYS = ["d", "net", "gross", "disc", "tax", "tips", "ref", "orders", "checks", "guests", "f", "b", "l", "w", "n", "r", "x", "lh", "lc", "p", "pf", "pb", "pl", "pw", "pn", "pr", "po"]


def _rows(con, sql, args=()):
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def _r(v, nd=2):
    return round(v, nd) if isinstance(v, float) else v


def build_payload(con, through: date | None = None) -> dict:
    cfg = settings()
    locs = locations()
    days_in_bundle = int(cfg["site"].get("history_days_in_bundle", 400))
    through = through or date.fromisoformat(con.execute("SELECT MAX(business_date) FROM daily_summary WHERE net_sales>0").fetchone()[0] or date.today().isoformat())
    since = through - timedelta(days=days_in_bundle)
    w28 = through - timedelta(days=27)
    w56 = through - timedelta(days=55)
    payload = {"meta": {"built_at": datetime.utcnow().isoformat(timespec="seconds") + "Z", "through": through.isoformat(), "since": since.isoformat(),
                        "title": cfg["site"]["title"], "buckets": cfg["category_map"]["buckets"]},
               "locations": [{"id": l["slug"], "name": l["name"], "short": l.get("short"), "opened": l.get("opened"),
                              # first_date is the earliest business day the POS actually has sales for, taken from the
                              # data rather than the hand-typed `opened` in config/locations.json. A location that opened
                              # partway through the window (Prairie Village, 2026) has no comparable prior period, so the
                              # site suppresses its change-vs-prior figures instead of dividing by a partial baseline.
                              "first_date": (con.execute("SELECT MIN(business_date) FROM daily_summary WHERE location_id=? AND net_sales>0", (l["slug"],)).fetchone() or [None])[0]}
                             for l in locs],
               "daily": {}, "hourly": {}, "top_items": {}, "labor_jobs": {}, "vendors": {}, "inventory": {}, "activations": [], "targets": {}, "payments": {}, "dining": {}, "revctr": {}, "discounts": {}, "pnl": {}, "sources": {}, "events": {}, "leads": {}, "events_monthly": {}, "scorecard": {}}

    for l in locs:
        lid = l["slug"]
        rows = _rows(con, f"SELECT {','.join(DAILY_COLS)} FROM daily_summary WHERE location_id=? AND business_date>=? AND business_date<=? ORDER BY business_date", (lid, since.isoformat(), through.isoformat()))
        payload["daily"][lid] = [[_r(r[c]) for c in DAILY_COLS] for r in rows]

        payload["hourly"][lid] = [[r["dow"], r["h"], _r(r["net"]), r["n"]] for r in _rows(con, """
            SELECT CAST(strftime('%w', business_date) AS INT) dow, hour_local h, SUM(price)/COUNT(DISTINCT business_date) net, COUNT(DISTINCT business_date) n
            FROM toast_order_items WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=? AND hour_local IS NOT NULL GROUP BY 1,2""", (lid, w56.isoformat(), through.isoformat()))]

        payload["top_items"][lid] = [[r["item_name"], r["bucket"], _r(r["qty"]), _r(r["net"])] for r in _rows(con, """
            SELECT item_name, bucket, SUM(quantity) qty, SUM(price) net FROM toast_order_items WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=?
            GROUP BY 1,2 ORDER BY net DESC LIMIT 40""", (lid, w28.isoformat(), through.isoformat()))]

        payload["labor_jobs"][lid] = [[r["job"], _r(r["hrs"]), _r(r["cost"]), r["shifts"]] for r in _rows(con, """
            SELECT COALESCE(job_name, 'Unknown') job, SUM(regular_hours+overtime_hours) hrs, SUM(wages) cost, COUNT(*) shifts FROM toast_time_entries
            WHERE location_id=? AND business_date>=? AND business_date<=? GROUP BY 1 ORDER BY cost DESC""", (lid, w28.isoformat(), through.isoformat()))]

        payload["vendors"][lid] = [[r["vendor_name"], _r(r["tot"]), r["n"], r["bucket"]] for r in _rows(con, """
            SELECT i.vendor_name, SUM(i.order_total) tot, COUNT(*) n,
                   (SELECT bucket FROM me_invoice_lines l WHERE l.order_id=i.order_id AND l.location_id=i.location_id GROUP BY bucket ORDER BY SUM(line_price) DESC LIMIT 1) bucket
            FROM me_invoices i WHERE i.location_id=? AND i.invoice_date>=? AND i.invoice_date<=? GROUP BY 1 ORDER BY tot DESC LIMIT 25""", (lid, w28.isoformat(), through.isoformat()))]

        payload["payments"][lid] = [[r["type"], _r(r["amt"]), _r(r["tips"]), r["n"]] for r in _rows(con, """
            SELECT COALESCE(type,'OTHER') type, SUM(amount) amt, SUM(tip_amount) tips, COUNT(*) n FROM toast_payments WHERE location_id=? AND business_date>=? AND business_date<=? GROUP BY 1 ORDER BY amt DESC""",
            (lid, w28.isoformat(), through.isoformat()))]

        # Discounts broken out by name and loyalty provider. Loyalty redemptions arrive through Toast as
        # discounts, so this is the loyalty picture without a second integration; `vendor` is non-null only
        # where Toast attributed the discount to a loyalty provider.
        payload["discounts"][lid] = [[r["name"], r["vendor"], _r(r["amt"]), r["n"]] for r in _rows(con, """
            SELECT COALESCE(name,'(unnamed)') name, loyalty_vendor vendor, SUM(amount) amt, COUNT(*) n
            FROM toast_discounts WHERE location_id=? AND business_date>=? AND business_date<=?
            GROUP BY 1,2 ORDER BY amt DESC LIMIT 30""", (lid, w28.isoformat(), through.isoformat()))]

        # Where in the building the sale happened. Same shape as the dining split and published next to it;
        # a bar-versus-patio mix is one of the few cuts a director can act on the same week.
        payload["revctr"][lid] = [[r["revenue_center"], _r(r["net"]), r["n"]] for r in _rows(con, """
            SELECT COALESCE(revenue_center,'(unassigned)') revenue_center, SUM(net_sales) net, COUNT(*) n FROM toast_orders
            WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=? GROUP BY 1 ORDER BY net DESC""",
            (lid, w28.isoformat(), through.isoformat()))]

        payload["dining"][lid] = [[r["dining_option"], _r(r["net"]), r["n"]] for r in _rows(con, """
            SELECT COALESCE(dining_option,'?') dining_option, SUM(net_sales) net, COUNT(*) n FROM toast_orders WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=? GROUP BY 1 ORDER BY net DESC""",
            (lid, w28.isoformat(), through.isoformat()))]

        # inventory: latest two counts per bucket + purchases between them -> usage & COGS actual
        counts = _rows(con, "SELECT count_date, bucket, value FROM me_inventory_counts WHERE location_id=? ORDER BY count_date", (lid,))
        by_bucket: dict[str, list] = {}
        for c in counts:
            by_bucket.setdefault(c["bucket"], []).append(c)
        inv = []
        for b, cs in by_bucket.items():
            last = cs[-1]; prev = cs[-2] if len(cs) > 1 else None
            purch = usage = sales = None
            if prev:
                col = {"Food": "purch_food", "Beer": "purch_beer", "Liquor": "purch_liquor", "Wine": "purch_wine", "NA Bev": "purch_nabev", "Retail": "purch_retail"}.get(b, "purch_other")
                scol = {"Food": "sales_food", "Beer": "sales_beer", "Liquor": "sales_liquor", "Wine": "sales_wine", "NA Bev": "sales_nabev", "Retail": "sales_retail"}.get(b, "sales_other")
                r = con.execute(f"SELECT COALESCE(SUM({col}),0), COALESCE(SUM({scol}),0) FROM daily_summary WHERE location_id=? AND business_date>? AND business_date<=?", (lid, prev["count_date"], last["count_date"])).fetchone()
                purch, sales = r[0], r[1]
                usage = prev["value"] + purch - last["value"]
            inv.append({"bucket": b, "date": last["count_date"], "value": _r(last["value"]), "prev_date": prev["count_date"] if prev else None, "prev_value": _r(prev["value"]) if prev else None,
                        "purchases": _r(purch) if purch is not None else None, "usage": _r(usage) if usage is not None else None, "sales": _r(sales) if sales is not None else None})
        payload["inventory"][lid] = inv

        # which source feeds this location (drives the dashboard's "needs Toast" states)
        t_days = con.execute("SELECT COUNT(DISTINCT business_date) FROM toast_orders WHERE location_id=? AND business_date>=?", (lid, w28.isoformat())).fetchone()[0]
        m_days = con.execute("SELECT COUNT(DISTINCT business_date) FROM me_sales_daily WHERE location_id=? AND business_date>=?", (lid, w28.isoformat())).fetchone()[0]
        payload["sources"][lid] = {"toast": t_days > 0, "marginedge": m_days > 0, "toast_days_28": t_days, "me_days_28": m_days}

        # P&L by month (MarginEdge): last 4 months incl. current, section totals + COGS/labor/expense categories
        pnl = {}
        for r in _rows(con, """SELECT substr(business_date,1,7) m, SUM(income_total) inc, SUM(cogs_total) cogs, SUM(labor_total) lab, SUM(expenses_total) exp, SUM(gross_profit) gp, SUM(prime_cost) pc,
                               SUM(controllable_profit) cp, COUNT(*) days FROM me_pnl_summary WHERE location_id=? AND business_date>=? GROUP BY 1 ORDER BY 1""",
                       (lid, (through.replace(day=1) - timedelta(days=95)).replace(day=1).isoformat())):
            pnl[r["m"]] = {"income": _r(r["inc"]), "cogs": _r(r["cogs"]), "labor": _r(r["lab"]), "expenses": _r(r["exp"]), "gross_profit": _r(r["gp"]), "prime_cost": _r(r["pc"]), "controllable_profit": _r(r["cp"]), "days": r["days"], "cats": {}}
        for r in _rows(con, """SELECT substr(business_date,1,7) m, section, category_name, bucket, SUM(total) t FROM me_pnl_daily WHERE location_id=? AND business_date>=? AND category_id!='' AND item_name=''
                               GROUP BY 1,2,3,4 ORDER BY 1,2,5 DESC""", (lid, (through.replace(day=1) - timedelta(days=95)).replace(day=1).isoformat())):
            if r["m"] in pnl:
                pnl[r["m"]]["cats"].setdefault(r["section"], []).append([r["category_name"], r["bucket"], _r(r["t"])])
        payload["pnl"][lid] = pnl

        # Tripleseat: events in the bundle window plus everything still ahead of us (the booked calendar)
        payload["events"][lid] = [[r["event_id"], r["name"], r["event_date"], r["status"], r["guest_count"],
                                   _r(r["grand_total"]), _r(r["actual_amount"]), _r(r["fb_minimum"]), _r(r["deposit"]), r["event_style"]]
                                  for r in _rows(con, """SELECT event_id, name, event_date, status, guest_count, grand_total, actual_amount, fb_minimum, deposit, event_style
                                                         FROM ts_events WHERE location_id=? AND event_date>=? ORDER BY event_date""", (lid, since.isoformat()))]
        payload["events_monthly"][lid] = {r["m"]: [r["n"], _r(r["booked"]), _r(r["actual"]), r["guests"]] for r in _rows(con, """
            SELECT substr(event_date,1,7) m, COUNT(*) n, SUM(COALESCE(grand_total,0)) booked, SUM(COALESCE(actual_amount,0)) actual, SUM(COALESCE(guest_count,0)) guests
            FROM ts_events WHERE location_id=? AND event_date>=? GROUP BY 1 ORDER BY 1""", (lid, since.isoformat()))}
        payload["leads"][lid] = [[r["status"] or "?", r["n"], r["guests"]] for r in _rows(con, """
            SELECT COALESCE(status,'?') status, COUNT(*) n, SUM(COALESCE(guest_count,0)) guests FROM ts_leads
            WHERE location_id=? AND event_date>=? GROUP BY 1 ORDER BY n DESC""", (lid, since.isoformat()))]

        payload["targets"][lid] = {r["month"]: [r["sales_target"], r["cogs_pct_target"], r["labor_pct_target"], r["guests_target"]] for r in _rows(con, "SELECT * FROM targets WHERE location_id=?", (lid,))}

    payload["activations"] = _rows(con, "SELECT activation_id id, location_id loc, start_date s, end_date e, name, type, cost, owner, notes FROM activations ORDER BY start_date")
    payload["meta"]["daily_keys"] = DAILY_KEYS

    # ---- Leadership Scorecard -------------------------------------------------------------------------
    # Company-wide, not per-location, so it rides in the payload once rather than under each location. Weeks
    # are ordered newest-first in the sheet; the page reverses them for charting.
    sc_tabs = _rows(con, "SELECT tab, rows, cols, truncated, grid FROM scorecard_tabs ORDER BY seq")
    sc_rows = _rows(con, "SELECT metric, owner, seq FROM scorecard GROUP BY metric ORDER BY MIN(seq)")
    if sc_tabs:
        import json as _json
        payload["scorecard"]["tabs"] = [{"name": t["tab"], "rows": t["rows"], "cols": t["cols"],
                                         "truncated": bool(t["truncated"]), "grid": _json.loads(t["grid"] or "[]")} for t in sc_tabs]
    if sc_rows:
        goals = {r["metric"]: [r["value"], r["display"]] for r in _rows(con, "SELECT metric, value, display FROM scorecard_goals")}
        weeks = [r["week"] for r in _rows(con, "SELECT week, MAX(seq) s FROM scorecard GROUP BY week ORDER BY MIN(rowid)")]
        cells = {}
        for r in _rows(con, "SELECT metric, week, value, display FROM scorecard"):
            cells.setdefault(r["metric"], {})[r["week"]] = [_r(r["value"]) if r["value"] is not None else None, r["display"]]
        payload["scorecard"]["weeks"] = weeks
        payload["scorecard"]["metrics"] = [{"metric": r["metric"], "owner": r["owner"], "goal": goals.get(r["metric"]),
                                            "cells": cells.get(r["metric"], {})} for r in sc_rows]
    if payload["scorecard"].get("tabs"):
        n = sum(len(_t["grid"]) for _t in payload["scorecard"]["tabs"])
        log.info("scorecard: %d tabs, %d rows total", len(payload["scorecard"]["tabs"]), n)

    return payload


def detail_for_location(con, lid: str, through: date, sheets: int = 12) -> dict:
    """The count-sheet level view behind the Inventory page's bucket rollups.

    Published as a SEPARATE per-location bundle, fetched only when somebody actually drills in. The headline
    page is 30-140 KB precisely because everything in it is pre-aggregated; putting every counted line in there
    would make every visitor pay for a page most of them never open.

    Variance is computed on VALUE, not quantity, and deliberately so: a product is counted in its report unit
    (a keg, a case) but purchased in whatever the vendor ships, and reconciling those two unit systems per
    product is exactly the kind of silent wrongness that makes a variance report worse than none. Value is the
    same currency on both sides.
    """
    dates = [r["d"] for r in _rows(con, """
        SELECT DISTINCT inventory_date d FROM me_inventories
        WHERE location_id=? AND inventory_date IS NOT NULL AND inventory_date!='' ORDER BY d DESC LIMIT ?""", (lid, sheets))]
    out = {"dates": dates, "events": [], "sheet": [], "variance": [], "window": None, "coverage": None}
    if not dates:
        return out

    out["events"] = [[r["inventory_date"], r["countsheet_name"], r["status"], _r(r["total_value"]), r["n"]] for r in _rows(con, """
        SELECT inventory_date, GROUP_CONCAT(DISTINCT countsheet_name) countsheet_name, GROUP_CONCAT(DISTINCT status) status,
               SUM(total_value) total_value, COUNT(*) n
        FROM me_inventories WHERE location_id=? AND inventory_date IN (%s) GROUP BY 1 ORDER BY 1 DESC""" % ",".join("?" * len(dates)),
        (lid, *dates))]

    cur = dates[0]
    prev = dates[1] if len(dates) > 1 else None

    # The latest count sheet as it was actually taken: by storage section, because that is how somebody
    # standing in the walk-in with a clipboard will read it back.
    out["sheet"] = [[r["section_name"], r["product_name"], r["bucket"], _r(r["qty"]), r["unit"], _r(r["price"]), _r(r["value"])] for r in _rows(con, """
        SELECT ii.section_name, ii.product_name, ii.bucket, SUM(ii.quantity) qty, MAX(ii.unit) unit,
               MAX(ii.price) price, SUM(ii.value) value
        FROM me_inventory_items ii JOIN me_inventories iv ON iv.inventory_id=ii.inventory_id AND iv.location_id=ii.location_id
        WHERE ii.location_id=? AND iv.inventory_date=?
        GROUP BY ii.section_name, ii.product_id ORDER BY ii.section_name, value DESC""", (lid, cur))]

    if prev:
        days = (date.fromisoformat(cur) - date.fromisoformat(prev)).days or 1
        out["window"] = [prev, cur, days]
        by_prod: dict[str, dict] = {}
        for tag, d in (("prev", prev), ("cur", cur)):
            for r in _rows(con, """
                SELECT ii.product_id pid, MAX(ii.product_name) nm, MAX(ii.bucket) bucket, SUM(ii.value) v, SUM(ii.quantity) q, MAX(ii.unit) unit
                FROM me_inventory_items ii JOIN me_inventories iv ON iv.inventory_id=ii.inventory_id AND iv.location_id=ii.location_id
                WHERE ii.location_id=? AND iv.inventory_date=? GROUP BY ii.product_id""", (lid, d)):
                o = by_prod.setdefault(r["pid"], {"name": r["nm"], "bucket": r["bucket"], "prev": 0.0, "cur": 0.0, "qty": 0.0, "unit": None, "purch": 0.0})
                o[tag] = float(r["v"] or 0)
                o["name"] = o["name"] or r["nm"]
                if tag == "cur":
                    o["qty"], o["unit"], o["bucket"] = float(r["q"] or 0), r["unit"], r["bucket"]
        attributed = 0.0
        for r in _rows(con, """
            SELECT product_id pid, SUM(line_price) v FROM me_invoice_lines
            WHERE location_id=? AND invoice_date>? AND invoice_date<=? AND product_id IS NOT NULL AND product_id!='' GROUP BY 1""", (lid, prev, cur)):
            if r["pid"] in by_prod:
                by_prod[r["pid"]]["purch"] = float(r["v"] or 0)
                attributed += float(r["v"] or 0)
        # Purchases of things nobody counts (paper, chemicals, a one-off) cannot appear in a per-product
        # variance, so the table would quietly cover less than the bucket totals it sits under. Publish the
        # coverage rather than let the two disagree without explanation.
        total_purch = (con.execute("SELECT COALESCE(SUM(line_price),0) FROM me_invoice_lines WHERE location_id=? AND invoice_date>? AND invoice_date<=?",
                                   (lid, prev, cur)).fetchone() or [0])[0] or 0.0
        out["coverage"] = [_r(attributed), _r(float(total_purch))]

        rows = []
        for pid, o in by_prod.items():
            usage = o["prev"] + o["purch"] - o["cur"]
            weekly = (usage / days * 7) if usage > 0 else None
            woh = (o["cur"] / weekly) if weekly else None
            # Stock sitting on a shelf with nothing leaving it. Worth its own flag rather than a low sort
            # position: dead stock is a decision (stop buying it, run it off), not just a small number.
            dead = 1 if (o["cur"] > 0 and usage <= 0) else 0
            rows.append([o["name"], o["bucket"], _r(o["prev"]), _r(o["purch"]), _r(o["cur"]), _r(usage),
                         _r(woh, 1) if woh is not None else None, dead, _r(o["qty"]), o["unit"]])
        rows.sort(key=lambda x: -(x[5] or 0))
        out["variance"] = rows

    out.update(_price_tracking(con, lid, through))
    return out


def _price_tracking(con, lid: str, through: date, days: int = 180) -> dict:
    """What we are paying per unit, and what has changed.

    Grouped by product AND PACKAGING, never product alone. The same product bought in a different pack size
    has a different unit price, so pooling them would manufacture price "movement" out of a packaging switch —
    the most plausible-looking wrong answer this data can give. Credits and zero/negative lines are excluded
    for the same reason: a credit is not a purchase at a negative price.

    A mover is ranked by IMPACT (price change x recent volume), not by percentage. A 40% jump on something
    bought twice a year is trivia; a 4% drift on a staple is the money.
    """
    since = (through - timedelta(days=days)).isoformat()
    rows = _rows(con, """
        SELECT l.product_id pid, COALESCE(l.packaging_id,'') pkg,
               COALESCE(pr.name, l.vendor_item_name, '(unnamed)') nm, COALESCE(l.bucket,'Other') bucket,
               COALESCE(i.vendor_name,'(no vendor)') vendor, l.invoice_date d,
               l.unit_price up, l.quantity qty, l.line_price lp
        FROM me_invoice_lines l
        JOIN me_invoices i ON i.order_id=l.order_id AND i.location_id=l.location_id
        LEFT JOIN me_products pr ON pr.product_id=l.product_id AND pr.location_id=l.location_id
        WHERE l.location_id=? AND l.invoice_date>=? AND l.invoice_date<=?
          AND COALESCE(i.is_credit,0)=0 AND l.unit_price>0 AND l.quantity>0
        ORDER BY l.invoice_date""", (lid, since, through.isoformat()))
    if not rows:
        return {"price_movers": [], "price_series": [], "vendor_compare": [], "price_window": None}

    mid = (through - timedelta(days=days // 2)).isoformat()
    by_key: dict[tuple, dict] = {}
    by_prod: dict[str, dict] = {}
    for r in rows:
        k = (r["pid"], r["pkg"])
        o = by_key.setdefault(k, {"nm": r["nm"], "bucket": r["bucket"], "vendors": {}, "series": [], "spend": 0.0,
                                  "old": [0.0, 0.0], "new": [0.0, 0.0]})
        o["spend"] += float(r["lp"] or 0)
        o["series"].append([r["d"], round(float(r["up"]), 4)])
        o["vendors"][r["vendor"]] = o["vendors"].get(r["vendor"], 0) + 1
        half = "new" if r["d"] > mid else "old"
        o[half][0] += float(r["up"]) * float(r["qty"])          # volume-weighted, so one odd small order
        o[half][1] += float(r["qty"])                            # cannot swing the average
        pv = by_prod.setdefault(r["pid"], {"nm": r["nm"], "bucket": r["bucket"], "v": {}})
        vv = pv["v"].setdefault(r["vendor"], [0.0, 0.0])
        vv[0] += float(r["up"]) * float(r["qty"]); vv[1] += float(r["qty"])

    movers = []
    for (pid, pkg), o in by_key.items():
        if o["old"][1] <= 0 or o["new"][1] <= 0:
            continue                                             # no before-and-after: nothing to compare
        old_up, new_up = o["old"][0] / o["old"][1], o["new"][0] / o["new"][1]
        if old_up <= 0:
            continue
        pct = (new_up - old_up) / old_up
        impact = (new_up - old_up) * o["new"][1]                  # dollars the change cost over the recent half
        if abs(pct) < 0.02:
            continue                                             # sub-2% is rounding and vendor noise
        vendor = max(o["vendors"], key=lambda v: o["vendors"][v])
        movers.append([o["nm"], vendor, o["bucket"], _r(old_up, 4), _r(new_up, 4), _r(pct, 4), _r(impact),
                       len(o["series"]), _r(o["spend"])])
    movers.sort(key=lambda x: -abs(x[6] or 0))

    top = sorted(by_key.values(), key=lambda o: -o["spend"])[:24]
    series = [[o["nm"], o["bucket"], o["series"][-40:]] for o in top if len(o["series"]) > 1]

    compare = []
    for pid, pv in by_prod.items():
        if len(pv["v"]) < 2:
            continue
        vs = sorted(([v, _r(t[0] / t[1], 4), _r(t[1], 2)] for v, t in pv["v"].items() if t[1] > 0), key=lambda x: x[1])
        if len(vs) > 1 and vs[0][1] > 0:
            compare.append([pv["nm"], pv["bucket"], vs, _r((vs[-1][1] - vs[0][1]) / vs[0][1], 4)])
    compare.sort(key=lambda x: -(x[3] or 0))

    return {"price_movers": movers[:40], "price_series": series, "vendor_compare": compare[:25],
            "price_window": [since, through.isoformat(), mid]}


def slice_for_location(payload: dict, lid: str) -> dict:
    """A director's bundle: only their location (other locations are not merely hidden — they are absent)."""
    out = {"meta": dict(payload["meta"]), "locations": [l for l in payload["locations"] if l["id"] == lid], "activations": [a for a in payload["activations"] if a["loc"] == lid]}
    for k in ("daily", "hourly", "top_items", "labor_jobs", "vendors", "inventory", "targets", "payments", "dining", "revctr", "discounts", "pnl", "sources", "events", "leads", "events_monthly"):
        out[k] = {lid: payload[k].get(lid)} if lid in payload[k] else {}
    return out


def run(through: date | None = None) -> dict:
    con = connect()
    p = build_payload(con, through)
    # Detail rides under its own key and is split out into per-location bundles by build_site, never published
    # inside the headline payload.
    through = date.fromisoformat(p["meta"]["through"])
    p["detail"] = {l["id"]: detail_for_location(con, l["id"], through) for l in p["locations"]}
    con.close()
    n = sum(len(v) for v in p["daily"].values())
    nd = sum(len(v.get("variance") or []) for v in p["detail"].values())
    log.info("metrics: %d locations, %d daily rows, %d detail variance rows, through %s", len(p["locations"]), n, nd, p["meta"]["through"])
    return p
