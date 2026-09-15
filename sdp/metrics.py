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
               "daily": {}, "hourly": {}, "top_items": {}, "labor_jobs": {}, "vendors": {}, "inventory": {}, "activations": [], "targets": {}, "payments": {}, "dining": {}, "revctr": {}, "labor_hourly": {}, "schedule": {}, "tender": {}, "voids": {}, "invoices": {}, "servers": {}, "cash": {}, "pacing": {}, "discounts": {}, "pnl": {}, "sources": {}, "events": {}, "leads": {}, "events_monthly": {}, "scorecard": {}}

    for l in locs:
        lid = l["slug"]
        rows = _rows(con, f"SELECT {','.join(DAILY_COLS)} FROM daily_summary WHERE location_id=? AND business_date>=? AND business_date<=? ORDER BY business_date", (lid, since.isoformat(), through.isoformat()))
        payload["daily"][lid] = [[_r(r[c]) for c in DAILY_COLS] for r in rows]

        payload["hourly"][lid] = [[r["dow"], r["h"], _r(r["net"]), r["n"]] for r in _rows(con, """
            SELECT CAST(strftime('%w', business_date) AS INT) dow, hour_local h, SUM(price)/COUNT(DISTINCT business_date) net, COUNT(DISTINCT business_date) n
            FROM toast_order_items WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=? AND hour_local IS NOT NULL GROUP BY 1,2""", (lid, w56.isoformat(), through.isoformat()))]

        # Labor spread across the clock, so it can be read against sales by the hour. The portal has had an
        # hourly sales heatmap and a labor-by-job table on separate pages since the start; neither answers
        # "are we staffed for the hour we are about to trade", which is the question a director schedules to.
        payload["labor_hourly"][lid] = _labor_by_hour(con, lid, w56, through)
        payload["schedule"][lid] = _schedule_vs_actual(con, lid, w28, through)
        payload["servers"][lid] = _server_performance(con, lid, w28, through)
        payload["cash"][lid] = _cash_management(con, lid, w28, through)
        payload["pacing"][lid] = _pacing(con, lid, through)

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

        # Tender detail. `payments` already gives the type split; this adds the card brand, the tip rate that
        # goes with it, and refunds — the three things a manager reconciling a drawer actually asks about.
        payload["tender"][lid] = [[r["type"], r["card"], _r(r["amt"]), _r(r["tips"]), _r(r["refunds"]), r["n"]] for r in _rows(con, """
            SELECT COALESCE(type,'OTHER') type, COALESCE(NULLIF(card_type,''),'—') card, SUM(amount) amt,
                   SUM(tip_amount) tips, SUM(refund_amount) refunds, COUNT(*) n
            FROM toast_payments WHERE location_id=? AND business_date>=? AND business_date<=?
            GROUP BY 1,2 ORDER BY amt DESC""", (lid, w28.isoformat(), through.isoformat()))]

        # Voids. Deliberately counted separately from discounts: a comp is a decision to give something away,
        # a void is a correction, and rolling them together hides both. Value is the gross that was rung and
        # then removed, which is why it comes from gross_sales rather than net.
        vd = _rows(con, """
            SELECT business_date d, COUNT(*) n, SUM(COALESCE(voided_value,0)) v,
                   SUM(CASE WHEN COALESCE(voided_value,0)>0 THEN 1 ELSE 0 END) valued
            FROM toast_orders WHERE location_id=? AND voided=1 AND business_date>=? AND business_date<=?
            GROUP BY 1 ORDER BY 1""", (lid, w28.isoformat(), through.isoformat()))
        vi = con.execute("""SELECT COUNT(*), COALESCE(SUM(pre_discount_price),0) FROM toast_order_items
                            WHERE location_id=? AND voided=1 AND business_date>=? AND business_date<=?""",
                         (lid, w28.isoformat(), through.isoformat())).fetchone()
        tot = con.execute("""SELECT COUNT(*), COALESCE(SUM(net_sales),0) FROM toast_orders
                             WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=?""",
                          (lid, w28.isoformat(), through.isoformat())).fetchone()
        # `valued` says how many of those voids actually carry a value. Days pulled before voided_value existed
        # have the count but not the money, and the page says so rather than implying the voids were free.
        payload["voids"][lid] = {"by_day": [[r["d"], r["n"], _r(r["v"])] for r in vd],
                                 "orders": sum(r["n"] for r in vd), "value": _r(sum(float(r["v"] or 0) for r in vd)),
                                 "valued": sum(r["valued"] for r in vd),
                                 "items": vi[0], "item_value": _r(vi[1]),
                                 "orders_ok": tot[0], "net_ok": _r(tot[1])}

        # Invoice hygiene: what is sitting unprocessed, what credits are outstanding, and which payment account
        # the spend ran through. All three are in the invoice header and none of them were surfaced anywhere.
        payload["invoices"][lid] = {
            "by_status": [[r["s"], r["n"], _r(r["t"])] for r in _rows(con, """
                SELECT COALESCE(NULLIF(status,''),'(none)') s, COUNT(*) n, SUM(order_total) t FROM me_invoices
                WHERE location_id=? AND invoice_date>=? GROUP BY 1 ORDER BY n DESC""", (lid, w56.isoformat(),))],
            "by_account": [[r["a"], r["n"], _r(r["t"])] for r in _rows(con, """
                SELECT COALESCE(NULLIF(payment_account,''),'(unassigned)') a, COUNT(*) n, SUM(order_total) t
                FROM me_invoices WHERE location_id=? AND invoice_date>=? GROUP BY 1 ORDER BY t DESC LIMIT 12""",
                (lid, w56.isoformat(),))],
            "credits": [[r["v"], r["n"], _r(r["t"])] for r in _rows(con, """
                SELECT COALESCE(vendor_name,'(no vendor)') v, COUNT(*) n, SUM(COALESCE(credit_amount, order_total)) t
                FROM me_invoices WHERE location_id=? AND is_credit=1 AND invoice_date>=? GROUP BY 1 ORDER BY t DESC LIMIT 12""",
                (lid, w56.isoformat(),))],
            "open": [[r["v"], r["num"], r["d"], _r(r["t"]), r["s"]] for r in _rows(con, """
                SELECT COALESCE(vendor_name,'(no vendor)') v, invoice_number num, invoice_date d, order_total t, status s
                FROM me_invoices
                WHERE location_id=? AND invoice_date>=? AND COALESCE(is_credit,0)=0
                  AND LOWER(COALESCE(status,'')) NOT IN ('exported','processed','complete','completed','closed','approved')
                ORDER BY invoice_date DESC LIMIT 25""", (lid, w56.isoformat(),))],
        }

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


def _labor_by_hour(con, lid: str, since: date, through: date) -> list:
    """Clock hours and wage cost spread across the hours actually worked.

    A shift is not an event at its clock-in time — it is four or eight hours of cost laid across the evening,
    so attributing it all to the hour someone punched in would put the labor peak an hour or two before the
    sales peak and quietly invert the answer. Each entry is therefore sliced at hour boundaries and its wages
    apportioned by the minutes falling in each slice.

    Two conventions are inherited from the sales side so the two can be divided by each other honestly:
    the weekday comes from the POS BUSINESS DATE (not the calendar date, so a 1am hour still belongs to the
    night before), and the hour comes from the local-offset timestamp exactly as `hour_local` does for items.
    A shift crossing midnight therefore lands on hours 22, 23, 0, 1 of the same business day, matching where
    the sales from those hours land.
    """
    rows = _rows(con, """
        SELECT business_date, in_at, out_at, regular_hours, overtime_hours, wages
        FROM toast_time_entries
        WHERE location_id=? AND business_date>=? AND business_date<=? AND in_at IS NOT NULL AND out_at IS NOT NULL""",
        (lid, since.isoformat(), through.isoformat()))
    if not rows:
        return []

    def parse(ts):
        # Toast sends local-offset timestamps; the offset is the restaurant's, so the wall-clock reading is
        # the local one. Take it literally rather than converting, which is what hour_local does for items.
        try:
            return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None

    buckets: dict[tuple[int, int], list] = {}
    days: dict[tuple[int, int], set] = {}
    for r in rows:
        a, b = parse(r["in_at"]), parse(r["out_at"])
        if not a or not b or b <= a:
            continue
        total = (b - a).total_seconds() / 3600.0
        if total <= 0 or total > 24:
            continue                                    # a punch never closed, or clock nonsense
        wages = float(r["wages"] or 0)
        bd = r["business_date"]
        try:
            dow = int(date.fromisoformat(bd).strftime("%w"))
        except Exception:
            continue
        cur = a
        while cur < b:
            nxt = min((cur + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0), b)
            if nxt <= cur:
                break
            frac = (nxt - cur).total_seconds() / 3600.0
            k = (dow, cur.hour)
            o = buckets.setdefault(k, [0.0, 0.0])
            o[0] += frac                                 # hours worked in this clock hour
            o[1] += wages * (frac / total)               # wage cost apportioned by time, not by headcount
            days.setdefault(k, set()).add(bd)
            cur = nxt

    return [[dow, hr, _r(v[0]), _r(v[1]), len(days[(dow, hr)])] for (dow, hr), v in sorted(buckets.items())]


def _pacing(con, lid: str, through: date) -> dict:
    """Month-to-date against target, and a forecast to month end.

    The forecast is seasonal-naive rather than a straight run-rate: each remaining day is estimated from that
    LOCATION'S OWN average for that weekday over the last eight weeks. A run-rate is badly wrong in a taproom,
    where a Saturday can be triple a Tuesday — halfway through a month with a weekend still to come, a linear
    projection understates; just after a big weekend it overstates, and a director chasing either number makes
    the wrong call about staffing and ordering.

    The pro-rata target is weighted the same way, so "ahead" and "behind" mean the same thing on both sides.
    """
    month = through.strftime("%Y-%m")
    first = through.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    dim = (nxt - first).days

    tg = con.execute("SELECT sales_target, cogs_pct_target, labor_pct_target, guests_target FROM targets WHERE location_id=? AND month=?",
                     (lid, month)).fetchone()
    mtd = con.execute("""SELECT COALESCE(SUM(net_sales),0), COALESCE(SUM(guests),0), COALESCE(SUM(labor_cost),0),
                                COALESCE(SUM(purchases),0), COUNT(*)
                         FROM daily_summary WHERE location_id=? AND business_date>=? AND business_date<=?""",
                      (lid, first.isoformat(), through.isoformat())).fetchone()
    if not mtd or not mtd[4]:
        return {}

    # Weekday profile from the last eight complete weeks, used for both the forecast and the pro-rata split.
    prof = {int(r["dow"]): float(r["avg"] or 0) for r in _rows(con, """
        SELECT CAST(strftime('%w', business_date) AS INT) dow, AVG(net_sales) avg
        FROM daily_summary WHERE location_id=? AND business_date>=? AND business_date<? AND net_sales>0
        GROUP BY 1""", (lid, (through - timedelta(days=56)).isoformat(), through.isoformat()))}
    if not prof:
        return {}
    def w(d):
        return prof.get(int(d.strftime("%w")), 0.0)

    elapsed = [first + timedelta(days=i) for i in range((through - first).days + 1)]
    remaining = [first + timedelta(days=i) for i in range((through - first).days + 1, dim)]
    w_elapsed = sum(w(d) for d in elapsed)
    w_total = w_elapsed + sum(w(d) for d in remaining)

    net, guests, labor, purch, days = float(mtd[0]), int(mtd[1] or 0), float(mtd[2]), float(mtd[3]), mtd[4]
    forecast = net + sum(w(d) for d in remaining)
    target = float(tg[0]) if tg and tg[0] else None
    share = (w_elapsed / w_total) if w_total else None

    return {
        "month": month, "days_elapsed": days, "days_in_month": dim,
        "mtd": {"net": _r(net), "guests": guests, "labor_pct": _r(labor / net, 4) if net else None,
                "purch_pct": _r(purch / net, 4) if net else None},
        "forecast": _r(forecast),
        "target": _r(target) if target else None,
        "prorata": _r(target * share) if (target and share) else None,
        "pace_pct": _r(net / (target * share), 4) if (target and share and target * share) else None,
        "forecast_pct": _r(forecast / target, 4) if target else None,
        "elapsed_share": _r(share, 4) if share else None,
        "targets": {"cogs": tg[1] if tg else None, "labor": tg[2] if tg else None, "guests": tg[3] if tg else None},
    }


def _cash_management(con, lid: str, since: date, through: date) -> dict:
    """Drawer over/short, payouts and no-sales.

    Reversals are removed on BOTH sides. Toast records a correction as a new entry whose `undoes` points at the
    original, so counting naively means a mistake that somebody already fixed still reads as a shortage — and
    the person who fixed it looks worse, not better. Every entry named by an `undoes`, and every entry doing
    the undoing, is dropped before anything is totalled.
    """
    rows = _rows(con, """
        SELECT entry_guid, business_date, type, amount, reason, payout_reason, no_sale_reason, employee_guid, undoes
        FROM toast_cash_entries WHERE location_id=? AND business_date>=? AND business_date<=?""",
        (lid, since.isoformat(), through.isoformat()))
    if not rows:
        return {}
    undone = {r["undoes"] for r in rows if r["undoes"]}
    live = [r for r in rows if not r["undoes"] and r["entry_guid"] not in undone]

    names = {r["employee_guid"]: (r["display_name"] or "").strip()
             for r in _rows(con, "SELECT employee_guid, display_name FROM toast_employees WHERE location_id=?", (lid,))}
    cfg = {r["guid"]: r["name"] for r in _rows(con, "SELECT guid, name FROM toast_config WHERE location_id=?", (lid,))}

    by_day, by_emp, payouts, nosales = {}, {}, {}, {}
    over = short = payout_total = 0.0
    for r in live:
        t = (r["type"] or "").upper()
        amt = float(r["amount"] or 0)
        d = r["business_date"]
        emp = names.get(r["employee_guid"]) or "(unattributed)"
        e = by_emp.setdefault(emp, {"over": 0.0, "short": 0.0, "payouts": 0, "payout_value": 0.0, "nosales": 0})
        day = by_day.setdefault(d, {"over": 0.0, "short": 0.0})
        if t == "CLOSE_OUT_OVERAGE":
            over += abs(amt); day["over"] += abs(amt); e["over"] += abs(amt)
        elif t == "CLOSE_OUT_SHORTAGE":
            short += abs(amt); day["short"] += abs(amt); e["short"] += abs(amt)
        elif t in ("PAY_OUT", "DRIVER_REIMBURSEMENT"):
            payout_total += abs(amt); e["payouts"] += 1; e["payout_value"] += abs(amt)
            key = cfg.get(r["payout_reason"]) or r["reason"] or "(no reason given)"
            p = payouts.setdefault(key, [0, 0.0]); p[0] += 1; p[1] += abs(amt)
        elif t == "NO_SALE":
            e["nosales"] += 1
            key = cfg.get(r["no_sale_reason"]) or r["reason"] or "(no reason given)"
            nosales[key] = nosales.get(key, 0) + 1

    deps = _rows(con, """SELECT business_date d, SUM(amount) a, COUNT(*) n FROM toast_deposits
                         WHERE location_id=? AND business_date>=? AND business_date<=? AND COALESCE(undoes,'')=''
                         GROUP BY 1 ORDER BY 1""", (lid, since.isoformat(), through.isoformat()))
    return {
        "days": [[d, _r(v["over"]), _r(v["short"])] for d, v in sorted(by_day.items())],
        "by_employee": sorted(([k, _r(v["over"]), _r(v["short"]), v["payouts"], _r(v["payout_value"]), v["nosales"]]
                               for k, v in by_emp.items()), key=lambda x: -(x[2] or 0))[:25],
        "payouts": sorted(([k, v[0], _r(v[1])] for k, v in payouts.items()), key=lambda x: -x[2])[:15],
        "nosales": sorted(([k, n] for k, n in nosales.items()), key=lambda x: -x[1])[:10],
        "deposits": [[r["d"], _r(r["a"]), r["n"]] for r in deps],
        "totals": {"over": _r(over), "short": _r(short), "net": _r(over - short), "payouts": _r(payout_total),
                   "entries": len(live), "reversed": len(rows) - len(live)},
    }


def _server_performance(con, lid: str, since: date, through: date, limit: int = 40) -> list:
    """Per-server sales performance.

    Two deliberate choices, both about not being unfair to people:

    Names are the SHORT form (first name plus last initial) that the employee directory already builds. A
    performance table does not need a full legal name to be useful, and the less identifying the published
    payload is, the better — it is a static file behind a sign-in, not a database with an audit log.

    Servers below a minimum number of orders are dropped rather than ranked. With a handful of orders the
    average check is noise, and a league table that puts a new starter bottom on four covers is worse than
    no table: it invites a conversation the data cannot support.
    """
    rows = _rows(con, """
        SELECT COALESCE(o.server_guid,'') g,
               SUM(CASE WHEN o.voided=0 THEN 1 ELSE 0 END) orders,
               SUM(CASE WHEN o.voided=0 THEN o.net_sales ELSE 0 END) net,
               SUM(CASE WHEN o.voided=0 THEN o.guests ELSE 0 END) guests,
               SUM(CASE WHEN o.voided=0 THEN o.discounts ELSE 0 END) disc,
               SUM(CASE WHEN o.voided=0 THEN o.tips ELSE 0 END) tips,
               SUM(CASE WHEN o.voided=1 THEN 1 ELSE 0 END) voids,
               SUM(COALESCE(o.voided_value,0)) void_value
        FROM toast_orders o
        WHERE o.location_id=? AND o.business_date>=? AND o.business_date<=?
        GROUP BY 1""", (lid, since.isoformat(), through.isoformat()))
    if not rows:
        return []

    names = {r["employee_guid"]: (r["display_name"] or "").strip()
             for r in _rows(con, "SELECT employee_guid, display_name FROM toast_employees WHERE location_id=?", (lid,))}
    hours = {r["employee_guid"]: float(r["h"] or 0) for r in _rows(con, """
        SELECT employee_guid, SUM(regular_hours+overtime_hours) h FROM toast_time_entries
        WHERE location_id=? AND business_date>=? AND business_date<=? GROUP BY 1""",
        (lid, since.isoformat(), through.isoformat()))}

    out = []
    for r in rows:
        n_orders = r["orders"] or 0
        if n_orders < 20:
            continue                                  # too few covers for an average to mean anything
        g = r["g"]
        net = float(r["net"] or 0)
        h = hours.get(g, 0.0)
        out.append([names.get(g) or ("(online / no server)" if not g else "(unnamed)"),
                    n_orders, _r(net), r["guests"] or 0,
                    _r(net / n_orders) if n_orders else None,                      # average check
                    _r(net / r["guests"]) if r["guests"] else None,                # per guest
                    _r(h, 1) if h else None,
                    _r(net / h) if h else None,                                    # sales per labor hour
                    _r(float(r["disc"] or 0) / net, 4) if net else None,            # discount rate
                    _r(float(r["tips"] or 0) / net, 4) if net else None,            # tip rate
                    r["voids"] or 0, _r(float(r["void_value"] or 0))])
    out.sort(key=lambda x: -(x[2] or 0))
    return out[:limit]


def _schedule_vs_actual(con, lid: str, since: date, through: date) -> dict:
    """The schedule against what actually happened.

    Matched at the level of (person, business date, job) TOTALS rather than shift-to-punch. A one-to-one
    match sounds more precise and is in practice worse: people split shifts, clock out for breaks, get moved
    between jobs mid-day, and any pairing rule invents a winner for those cases. Totals per person per day
    per job are unambiguous, and the difference is the number anyone actually acts on.

    Punctuality compares the FIRST scheduled start with the FIRST punch of that person/day/job, which is the
    only in-time comparison that survives split shifts.
    """
    def key(r):
        return (r["employee_guid"] or "", r["business_date"] or "", r["job_guid"] or "")

    sched, actual = {}, {}
    for r in _rows(con, """SELECT employee_guid, business_date, job_guid, job_name, SUM(hours) h, MIN(in_at) first_in
                           FROM toast_shifts WHERE location_id=? AND deleted=0 AND business_date>=? AND business_date<=?
                           GROUP BY 1,2,3""", (lid, since.isoformat(), through.isoformat())):
        sched[key(r)] = {"h": float(r["h"] or 0), "in": r["first_in"], "job": r["job_name"]}
    for r in _rows(con, """SELECT employee_guid, business_date, job_guid, job_name,
                                  SUM(regular_hours+overtime_hours) h, MIN(in_at) first_in
                           FROM toast_time_entries WHERE location_id=? AND business_date>=? AND business_date<=?
                           GROUP BY 1,2,3""", (lid, since.isoformat(), through.isoformat())):
        actual[key(r)] = {"h": float(r["h"] or 0), "in": r["first_in"], "job": r["job_name"]}
    if not sched:
        return {}                                   # no schedule pulled: the view hides itself rather than lying

    daily: dict[str, list] = {}
    byjob: dict[str, list] = {}
    late = []
    unscheduled = worked_off = 0.0
    for k in set(sched) | set(actual):
        sh, ac = sched.get(k), actual.get(k)
        bd = k[1]
        s_h = sh["h"] if sh else 0.0
        a_h = ac["h"] if ac else 0.0
        job = (sh or ac).get("job") or "Unknown"
        d = daily.setdefault(bd, [0.0, 0.0]); d[0] += s_h; d[1] += a_h
        b = byjob.setdefault(job, [0.0, 0.0, 0]); b[0] += s_h; b[1] += a_h; b[2] += 1
        if not sh and a_h:
            unscheduled += a_h                      # worked without being on the schedule at all
        if sh and not ac:
            worked_off += s_h                       # scheduled and never turned up (or was cut)
        if sh and ac and sh["in"] and ac["in"]:
            try:
                sm = datetime.strptime(sh["in"][:19], "%Y-%m-%dT%H:%M:%S")
                am = datetime.strptime(ac["in"][:19], "%Y-%m-%dT%H:%M:%S")
                late.append([bd, job, round((am - sm).total_seconds() / 60.0)])
            except Exception:
                pass

    mins = [x[2] for x in late]
    on_time = sum(1 for m in mins if -5 <= m <= 5)
    early = sum(1 for m in mins if m < -5)
    tardy = sorted([x for x in late if x[2] > 5], key=lambda x: -x[2])
    return {
        "daily": [[bd, _r(v[0]), _r(v[1])] for bd, v in sorted(daily.items())],
        "jobs": sorted(([j, _r(v[0]), _r(v[1]), v[2]] for j, v in byjob.items()), key=lambda x: -(x[2] or 0)),
        "punctuality": {"n": len(mins), "early": early, "on_time": on_time, "late": len(tardy),
                        "avg_late": _r(sum(x[2] for x in tardy) / len(tardy), 1) if tardy else None,
                        "worst": tardy[:12]},
        "unscheduled_hours": _r(unscheduled), "no_show_hours": _r(worked_off),
    }


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

    # P&L at the line-item level. The headline page shows category totals because `metrics` filters to
    # item_name='' — the individual lines beneath each category were collected all along and never published.
    # They ride in the detail bundle rather than the main one because there are a few hundred per location.
    out["pnl_items"] = [[r["m"], r["section"], r["cat"], r["item"], _r(r["t"])] for r in _rows(con, """
        SELECT substr(business_date,1,7) m, section, COALESCE(category_name,'(uncategorised)') cat,
               item_name item, SUM(total) t
        FROM me_pnl_daily
        WHERE location_id=? AND business_date>=? AND COALESCE(item_name,'')!=''
        GROUP BY 1,2,3,4 HAVING ABS(SUM(total))>0.005 ORDER BY 1 DESC, 2, 5 DESC""",
        (lid, (through.replace(day=1) - timedelta(days=95)).replace(day=1).isoformat()))]
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
    for k in ("daily", "hourly", "top_items", "labor_jobs", "vendors", "inventory", "targets", "payments", "dining", "revctr", "labor_hourly", "schedule", "tender", "voids", "invoices", "servers", "cash", "pacing", "discounts", "pnl", "sources", "events", "leads", "events_monthly"):
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
