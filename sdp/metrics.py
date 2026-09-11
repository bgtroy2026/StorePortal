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
              "labor_hours", "labor_cost", "purchases", "purch_food", "purch_beer", "purch_liquor", "purch_wine", "purch_nabev", "purch_other"]
DAILY_KEYS = ["d", "net", "gross", "disc", "tax", "tips", "ref", "orders", "checks", "guests", "f", "b", "l", "w", "n", "r", "x", "lh", "lc", "p", "pf", "pb", "pl", "pw", "pn", "po"]


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
               "locations": [{"id": l["slug"], "name": l["name"], "short": l.get("short"), "opened": l.get("opened")} for l in locs],
               "daily": {}, "hourly": {}, "top_items": {}, "labor_jobs": {}, "vendors": {}, "inventory": {}, "activations": [], "targets": {}, "payments": {}, "dining": {}, "pnl": {}, "sources": {}}

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
                col = {"Food": "purch_food", "Beer": "purch_beer", "Liquor": "purch_liquor", "Wine": "purch_wine", "NA Bev": "purch_nabev"}.get(b, "purch_other")
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

        payload["targets"][lid] = {r["month"]: [r["sales_target"], r["cogs_pct_target"], r["labor_pct_target"], r["guests_target"]] for r in _rows(con, "SELECT * FROM targets WHERE location_id=?", (lid,))}

    payload["activations"] = _rows(con, "SELECT activation_id id, location_id loc, start_date s, end_date e, name, type, cost, owner, notes FROM activations ORDER BY start_date")
    payload["meta"]["daily_keys"] = DAILY_KEYS
    return payload


def slice_for_location(payload: dict, lid: str) -> dict:
    """A director's bundle: only their location (other locations are not merely hidden — they are absent)."""
    out = {"meta": dict(payload["meta"]), "locations": [l for l in payload["locations"] if l["id"] == lid], "activations": [a for a in payload["activations"] if a["loc"] == lid]}
    for k in ("daily", "hourly", "top_items", "labor_jobs", "vendors", "inventory", "targets", "payments", "dining", "pnl", "sources"):
        out[k] = {lid: payload[k].get(lid)} if lid in payload[k] else {}
    return out


def run(through: date | None = None) -> dict:
    con = connect()
    p = build_payload(con, through)
    con.close()
    n = sum(len(v) for v in p["daily"].values())
    log.info("metrics: %d locations, %d daily rows, through %s", len(p["locations"]), n, p["meta"]["through"])
    return p
