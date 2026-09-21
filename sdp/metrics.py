"""Metrics: warehouse -> compact JSON payload(s) for the dashboard.

The dashboard computes flexible-window KPIs (WTD/MTD/YTD/L7/L28, vs prior year) client-side from the daily
rows; item/labor/vendor/hourly detail is pre-aggregated here for fixed windows to keep bundles small.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta

from . import insights, ops
from .transform import connect
from .util import locations, log, settings, to_local

DAILY_COLS = ["business_date", "net_sales", "gross_sales", "discounts", "tax", "tips", "refunds", "orders", "checks", "guests",
              "sales_food", "sales_beer", "sales_liquor", "sales_wine", "sales_nabev", "sales_retail", "sales_other", "sales_svc", "sales_unattr",
              "labor_hours", "labor_cost", "purchases", "purch_food", "purch_beer", "purch_liquor", "purch_wine", "purch_nabev", "purch_retail", "purch_other"]
DAILY_KEYS = ["d", "net", "gross", "disc", "tax", "tips", "ref", "orders", "checks", "guests", "f", "b", "l", "w", "n", "r", "x", "svc", "sv", "lh", "lc", "p", "pf", "pb", "pl", "pw", "pn", "pr", "po"]


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
               "locations": [{"id": l["slug"], "name": l["name"], "short": l.get("short"), "opened": l.get("opened"), "seats": l.get("seats"),
                              # first_date is the earliest business day the POS actually has sales for, taken from the
                              # data rather than the hand-typed `opened` in config/locations.json. A location that opened
                              # partway through the window (Prairie Village, 2026) has no comparable prior period, so the
                              # site suppresses its change-vs-prior figures instead of dividing by a partial baseline.
                              "first_date": (con.execute("SELECT MIN(business_date) FROM daily_summary WHERE location_id=? AND net_sales>0", (l["slug"],)).fetchone() or [None])[0]}
                             for l in locs],
               "daily": {}, "hourly": {}, "top_items": {}, "labor_jobs": {}, "vendors": {}, "inventory": {}, "activations": [], "targets": {}, "payments": {}, "dining": {}, "revctr": {}, "labor_hourly": {}, "schedule": {}, "tender": {}, "voids": {}, "invoices": {}, "servers": {}, "cash": {}, "pacing": {}, "digest": {}, "discounts": {}, "pnl": {}, "sources": {}, "events": {}, "leads": {}, "events_monthly": {}, "scorecard": {},
               "ops": {}, "menu_prices": [], "price_compare": [],
               "beer": {}, "channels": {}, "loyalty": {}, "menu": {}, "beer_mix": {}, "compliance": {}, "weather": {}, "market": {}}
    # MarginEdge onboarding is still settling, so everything sourced from it is badged provisional in the portal.
    # One switch in config turns the badges off when the data is trusted; nothing else has to change.
    payload["meta"]["provisional"] = {"marginedge": bool((cfg.get("marginedge") or {}).get("provisional", True)),
                                      "note": (cfg.get("marginedge") or {}).get("provisional_note") or
                                              "MarginEdge onboarding is still in progress — treat inventory, purchases and P&L as provisional."}

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
            SELECT COALESCE(job_name, 'Unknown') job, SUM(COALESCE(regular_hours,0)+COALESCE(overtime_hours,0)) hrs, SUM(wages) cost, COUNT(*) shifts FROM toast_time_entries
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

        first_date = next((x["first_date"] for x in payload["locations"] if x["id"] == lid), None)
        # Every derived view is isolated. They add to the portal; none of them is worth the nightly publish. A
        # failure logs loudly and that ONE section is published as null, which the page treats as "not available".
        def _safe(name, fn, *a):
            try:
                return fn(con, lid, *a)
            except Exception as e:
                log.error("metrics: %s failed for %s (%s: %s) — published without it", name, lid, type(e).__name__, e)
                return None
        payload["beer"][lid] = _safe("beer", insights.beer_volume, through)
        payload["channels"][lid] = _safe("channels", insights.channels, w28, through)
        payload["loyalty"][lid] = _safe("loyalty", insights.loyalty, w28, through)
        payload["menu"][lid] = _safe("menu", insights.menu_analysis, w28, through, first_date)
        payload["beer_mix"][lid] = _safe("beer_mix", insights.beer_mix, w28, through) or []
        payload["compliance"][lid] = _safe("compliance", insights.count_compliance, through)
        payload["weather"][lid] = _safe("weather", insights.weather, since, through)
        payload["market"][lid] = _safe("market", insights.market, w28, through)

        # Operational views (sdp/ops.py). Each sub-section fails alone, exactly like the ones above.
        op = {"comps": _safe("ops.comps", ops.comps, w28, through),
              "tabs": _safe("ops.tabs", ops.tabs, w28, through, l.get("timezone")),
              "overtime": _safe("ops.overtime", ops.overtime, through),
              "checks": _safe("ops.checks", ops.checks, w28, through),
              "stock": _safe("ops.stock", ops.stock, through),
              "invoice_health": _safe("ops.invoice_health", ops.invoice_health, through)}
        # The price movers live in the on-demand detail bundle; the few that cost real money are lifted here so
        # the digest can mention them without every visitor downloading the detail.
        try:
            mv = _price_tracking(con, lid, through).get("price_movers") or []
            op["price_alerts"] = [m for m in mv if (m[6] or 0) >= DIGEST_RULES["price_creep"]][:5]
        except Exception as e:
            log.error("metrics: price alerts failed for %s (%s: %s)", lid, type(e).__name__, e)
            op["price_alerts"] = None
        payload["ops"][lid] = op

        # The digest reads back sections of the payload rather than re-querying, so it can never disagree with
        # the page a director opens to check it. That means it has to run LAST: it previously sat above, where
        # inventory, invoices, voids and discounts were all still empty for this location, and its rules over
        # those sections could never fire at all.
        payload["digest"][lid] = _digest(con, lid, payload, w28, through)

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

        # Tripleseat: events in the bundle window plus everything still ahead of us (the booked calendar).
        # Deleted events stay in the warehouse (so a late re-send cannot resurrect them) but never reach a page.
        payload["events"][lid] = [[r["event_id"], r["name"], r["event_date"], r["status"], r["guest_count"],
                                   _r(r["grand_total"]), _r(r["actual_amount"]), _r(r["fb_minimum"]), _r(r["deposit"]), r["event_style"],
                                   r["rooms"], r["event_type_name"], r["source"]]
                                  for r in _rows(con, """SELECT event_id, name, event_date, status, guest_count, grand_total, actual_amount, fb_minimum, deposit, event_style,
                                                                rooms, event_type_name, source
                                                         FROM ts_events WHERE location_id=? AND event_date>=? AND COALESCE(deleted,0)=0 ORDER BY event_date""", (lid, since.isoformat()))]
        payload["events_monthly"][lid] = {r["m"]: [r["n"], _r(r["booked"]), _r(r["actual"]), r["guests"]] for r in _rows(con, """
            SELECT substr(event_date,1,7) m, COUNT(*) n, SUM(COALESCE(grand_total,0)) booked, SUM(COALESCE(actual_amount,0)) actual, SUM(COALESCE(guest_count,0)) guests
            FROM ts_events WHERE location_id=? AND event_date>=? AND COALESCE(deleted,0)=0 GROUP BY 1 ORDER BY 1""", (lid, since.isoformat()))}
        payload["leads"][lid] = [[r["status"] or "?", r["n"], r["guests"]] for r in _rows(con, """
            SELECT COALESCE(status,'?') status, COUNT(*) n, SUM(COALESCE(guest_count,0)) guests FROM ts_leads
            WHERE location_id=? AND event_date>=? GROUP BY 1 ORDER BY n DESC""", (lid, since.isoformat()))]

        payload["targets"][lid] = {r["month"]: [r["sales_target"], r["cogs_pct_target"], r["labor_pct_target"], r["guests_target"]] for r in _rows(con, "SELECT * FROM targets WHERE location_id=?", (lid,))}

    for key, fn, args in (("menu_prices", ops.menu_price_consistency, ()), ("price_compare", ops.purchase_price_compare, (through,))):
        try:
            payload[key] = fn(con, *args)
        except Exception as e:
            log.error("metrics: %s failed (%s: %s) — published without it", key, type(e).__name__, e)
            payload[key] = []

    payload["activations"] = _rows(con, "SELECT activation_id id, location_id loc, start_date s, end_date e, name, type, cost, owner, notes FROM activations ORDER BY start_date")
    payload["meta"]["daily_keys"] = DAILY_KEYS

    # ---- Tripleseat: what is connected, and the catalog the public key reads -------------------------------
    try:
        payload["tripleseat"] = tripleseat_section(con, [l["slug"] for l in locs])
    except Exception as e:
        log.error("metrics: tripleseat section failed (%s: %s) — published without it", type(e).__name__, e)
        payload["tripleseat"] = None

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
    night before), and the hour is the restaurant's local hour, converted from Toast's UTC exactly as `hour_local` is.
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

    tz = next((l.get("timezone") for l in locations() if l["slug"] == lid), None) or "America/Chicago"

    def parse(ts):
        # Toast timestamps are UTC. They are converted to the restaurant's wall clock here, exactly as
        # `hour_local` is for items, so that the two can be divided by each other hour for hour.
        return to_local(ts, tz)

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


# An exception is something worth ACTING on, not everything that moved. Every rule below carries a threshold,
# and a rule that does not clear its threshold produces nothing at all — a digest that lists every change is
# just the dashboard again, and one that cries wolf gets ignored within a week.
DIGEST_RULES = {
    "sales_change": 0.05,        # net sales vs the prior equal-length period
    "pct_points": 0.02,          # labor % / purchase % moves, in percentage points
    "pace": 0.05,                # forecast vs target
    "bucket_change": 0.12,       # a category's own movement...
    "bucket_material": 0.05,     # ...but only if the category is at least this share of net
    "comp_points": 0.01,
    "void_rate": 0.02,
    "cash_net": 100.0,
    "weeks_on_hand": 8.0,
    "dead_stock": 3,
    "open_invoices": 10,
    "schedule_gap": 0.05,
    "count_compliance": 0.75,    # share of expected inventory counts actually made
    "price_creep": 150.0,        # dollars a unit-price rise has cost over the recent half of the price window
    "quiet_vendor": 250.0,       # average invoice of a regular vendor that has stopped invoicing
}


def _digest(con, lid: str, payload: dict, w28: date, through: date) -> list:
    """A short "what changed" list for one location.

    Ordered worst-first and capped, because the value is in being readable at a glance. Everything here is
    derived from figures already computed for the other views, so the digest can never disagree with the page
    a director opens to check it.
    """
    R = DIGEST_RULES
    out = []
    # The fifth element is a STABLE KEY for the rule that fired ("cash_net", "inv_uncounted:Beer"). The wording of
    # a line changes every night as the numbers move; the key does not, and it is what an acknowledgement is
    # filed against — so "I'm on it" survives tomorrow's rebuild instead of vanishing with yesterday's sentence.
    def add(sev, area, head, detail="", key=None):
        out.append([sev, area, head, detail, key or re.sub(r"[^a-z0-9]+", "_", f"{area}:{head}".lower())[:60]])

    prior_start = (w28 - timedelta(days=28)).isoformat()
    cur = con.execute("""SELECT COALESCE(SUM(net_sales),0), COALESCE(SUM(labor_cost),0), COALESCE(SUM(purchases),0),
                                COALESCE(SUM(sales_food),0), COALESCE(SUM(sales_beer),0), COALESCE(SUM(sales_liquor),0),
                                COALESCE(SUM(sales_wine),0), COALESCE(SUM(sales_nabev),0), COALESCE(SUM(discounts),0)
                         FROM daily_summary WHERE location_id=? AND business_date>=? AND business_date<=?""",
                      (lid, w28.isoformat(), through.isoformat())).fetchone()
    prv = con.execute("""SELECT COALESCE(SUM(net_sales),0), COALESCE(SUM(labor_cost),0), COALESCE(SUM(purchases),0),
                                COALESCE(SUM(sales_food),0), COALESCE(SUM(sales_beer),0), COALESCE(SUM(sales_liquor),0),
                                COALESCE(SUM(sales_wine),0), COALESCE(SUM(sales_nabev),0), COALESCE(SUM(discounts),0)
                         FROM daily_summary WHERE location_id=? AND business_date>=? AND business_date<?""",
                      (lid, prior_start, w28.isoformat())).fetchone()
    if not cur or not cur[0]:
        return []
    net, labor, purch, disc = float(cur[0]), float(cur[1]), float(cur[2]), float(cur[8])
    pnet = float(prv[0]) if prv else 0.0

    if pnet:
        ch = (net - pnet) / pnet
        if abs(ch) >= R["sales_change"]:
            add("warn" if ch < 0 else "good", "Sales",
                f"Net sales {'down' if ch < 0 else 'up'} {abs(ch) * 100:.0f}% on the previous 28 days",
                f"{_r(net):,.0f} vs {_r(pnet):,.0f}", key="sales_change")
        # Category movers, but only ones big enough to matter.
        for name, i in (("Food", 3), ("Beer", 4), ("Liquor", 5), ("Wine", 6), ("NA Bev", 7)):
            c, p = float(cur[i]), float(prv[i])
            if p and c / net >= R["bucket_material"]:
                d = (c - p) / p
                if abs(d) >= R["bucket_change"]:
                    add("warn" if d < 0 else "good", "Mix",
                        f"{name} sales {'down' if d < 0 else 'up'} {abs(d) * 100:.0f}%",
                        f"{_r(c):,.0f} vs {_r(p):,.0f}", key=f"mix:{name}")

    lab_pct = labor / net if net else None
    if lab_pct is not None and prv and prv[0] and float(prv[1]):
        d = lab_pct - (float(prv[1]) / float(prv[0]))
        if d >= R["pct_points"]:
            add("warn", "Labor", f"Labor up {d * 100:.1f} points to {lab_pct * 100:.1f}% of sales", key="labor_up")
    tg = payload["targets"].get(lid, {}).get(through.strftime("%Y-%m"))
    if tg and tg[2] and lab_pct and lab_pct - tg[2] >= R["pct_points"]:
        add("warn", "Labor", f"Labor {(lab_pct - tg[2]) * 100:.1f} points over the {tg[2] * 100:.0f}% target", key="labor_target")
    if tg and tg[1] and net and (purch / net) - tg[1] >= R["pct_points"]:
        add("warn", "COGS", f"Purchases {((purch / net) - tg[1]) * 100:.1f} points over the {tg[1] * 100:.0f}% target", key="cogs_target")

    pac = payload["pacing"].get(lid) or {}
    if pac.get("forecast_pct") is not None:
        d = pac["forecast_pct"] - 1
        if abs(d) >= R["pace"]:
            add("warn" if d < 0 else "good", "Pacing",
                f"Forecast to land {abs(d) * 100:.0f}% {'under' if d < 0 else 'over'} the month's target",
                f"{_r(pac['forecast']):,.0f} vs {_r(pac['target']):,.0f}", key="pacing")

    if net and pnet and float(prv[8]):
        d = (disc / net) - (float(prv[8]) / pnet)
        if d >= R["comp_points"]:
            add("warn", "Comps", f"Discounts up {d * 100:.1f} points to {disc / net * 100:.1f}% of sales", key="comps_up")

    v = payload["voids"].get(lid) or {}
    if v.get("orders") and v.get("orders_ok"):
        rate = v["orders"] / (v["orders"] + v["orders_ok"])
        if rate >= R["void_rate"]:
            add("warn", "Voids", f"{rate * 100:.1f}% of orders voided",
                f"{v['orders']} orders" + (f", {_r(v['value']):,.0f}" if v.get("valued") else ""), key="void_rate")

    c = payload["cash"].get(lid) or {}
    if c.get("totals") and abs(c["totals"].get("net") or 0) >= R["cash_net"]:
        n = c["totals"]["net"]
        add("warn" if n < 0 else "info", "Cash",
            f"Drawers net {'short' if n < 0 else 'over'} {abs(n):,.0f} across 28 days", key="cash_net")

    # A bucket missing from the latest count sheet is a data gap, not a finding about the food. It stays silent
    # in every other view by design, which is exactly why the digest has to say it out loud — an uncounted
    # category is the one thing here a director can actually fix before the next count.
    _inv = payload["inventory"].get(lid) or []
    _newest = max((r["date"] for r in _inv if r.get("date")), default=None)
    for r in _inv:
        if r.get("prev_value") and not r.get("value"):
            add("warn", "Inventory", f"{r['bucket']} was not on the count sheet dated {r['date']}",
                "usage and cost cannot be calculated for it", key=f"inv_uncounted:{r['bucket']}")
        elif _newest and r.get("date") and r["date"] != _newest:
            add("info", "Inventory", f"{r['bucket']} last counted {r['date']}",
                f"other categories were counted {_newest}", key=f"inv_stale:{r['bucket']}")

    for r in _inv:
        if r.get("usage") and r.get("value") and r.get("prev_date"):
            days = (date.fromisoformat(r["date"]) - date.fromisoformat(r["prev_date"])).days or 1
            weekly = r["usage"] / days * 7
            if weekly > 0:
                woh = r["value"] / weekly
                if woh >= R["weeks_on_hand"]:
                    add("info", "Inventory", f"{r['bucket']} at {woh:.1f} weeks on hand",
                        f"{_r(r['value']):,.0f} counted", key=f"inv_woh:{r['bucket']}")

    inv = payload["invoices"].get(lid) or {}
    if len(inv.get("open") or []) >= R["open_invoices"]:
        add("info", "Invoices", f"{len(inv['open'])} invoices not in a finished state", key="open_invoices")

    sc = payload["schedule"].get(lid) or {}
    if sc.get("daily"):
        ts = sum(r[1] for r in sc["daily"]); ta = sum(r[2] for r in sc["daily"])
        if ts and abs(ta - ts) / ts >= R["schedule_gap"]:
            d = (ta - ts) / ts
            add("warn" if d > 0 else "info", "Schedule",
                f"{abs(d) * 100:.0f}% {'more' if d > 0 else 'fewer'} hours worked than scheduled",
                f"{ta:,.0f}h vs {ts:,.0f}h", key="schedule_gap")

    comp = payload.get("compliance", {}).get(lid) or {}
    if comp.get("score") is not None and comp["score"] < R["count_compliance"]:
        missed = [r[0] for r in comp["rows"] if r[3] < r[4]]
        add("warn", "Counting", f"{comp['score'] * 100:.0f}% of expected inventory counts made in the last {comp['weeks']} weeks",
            ("behind: " + ", ".join(missed[:5])) if missed else "", key="count_compliance")

    fc = (c.get("float_check") or {}) if c else {}
    if fc.get("verdict") == "explained":
        add("info", "Cash", f"Drawer shortages match a float setting: Toast expects {fc['toast_expected']:,.0f}, drawers open with {fc['actual_float']:,.0f}",
            "fix the expected starting cash in Toast, not the people", key="cash_float")

    op = payload.get("ops", {}).get(lid) or {}
    for m in (op.get("price_alerts") or [])[:2]:
        add("warn", "Prices", f"{m[0]} up {m[5] * 100:.0f}% from {m[1]}",
            f"about {m[6]:,.0f} more over the recent period; {m[3]:,.2f} to {m[4]:,.2f} a unit", key=f"price:{m[0]}"[:60])
    ot = op.get("overtime") or {}
    over = [x for x in (ot.get("people") or []) if (x[4] or 0) > 0]
    if over:
        # A count, never names: the portal sits behind a sign-in and an inbox does not.
        already = sum(1 for x in over if (x[1] or 0) > (ot.get("limit") or 40))
        add("warn", "Labor", f"{len(over)} {'person is' if len(over) == 1 else 'people are'} on course to pass {ot['limit']:.0f} hours this week",
            (f"{already} already over on hours worked; " if already else "") + "see Labor, overtime this week", key="overtime_week")
    for q in ((op.get("invoice_health") or {}).get("quiet") or [])[:2]:
        if (q[5] or 0) >= R["quiet_vendor"]:
            add("info", "Invoices", f"Nothing entered from {q[0]} dated after {q[1]}",
                f"usually invoices every {q[3]:.0f} days, about {q[5]:,.0f} each; if a delivery came, its invoice has not reached MarginEdge", key=f"quiet:{q[0]}"[:60])
    flagged = [x for x in ((op.get("comps") or {}).get("people") or []) if x[9]]
    if flagged:
        add("info", "Comps", f"{len(flagged)} {'person' if len(flagged) == 1 else 'people'} discounting or voiding at twice the taproom rate",
            "promotions such as happy hour are not counted; see Labor, comps and voids by person", key="comps_outlier")

    # Keg yield is deliberately NOT a digest rule yet: it has not been checked against a real pair of counts, and
    # house beer arrives as a transfer that MarginEdge may not record as an invoice. It stays on the Beer page,
    # with its caveats, until it has earned a place in an email.

    order = {"warn": 0, "info": 1, "good": 2}
    out.sort(key=lambda x: order.get(x[0], 3))
    return out[:12]


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
        SELECT entry_guid, business_date, type, amount, reason, payout_reason, no_sale_reason, employee_guid,
               drawer_guid, undoes
        FROM toast_cash_entries WHERE location_id=? AND business_date>=? AND business_date<=?""",
        (lid, since.isoformat(), through.isoformat()))
    if not rows:
        return {}
    undone = {r["undoes"] for r in rows if r["undoes"]}
    live = [r for r in rows if not r["undoes"] and r["entry_guid"] not in undone]

    names = {r["employee_guid"]: (r["display_name"] or "").strip()
             for r in _rows(con, "SELECT employee_guid, display_name FROM toast_employees WHERE location_id=?", (lid,))}
    cfg = {r["guid"]: r["name"] for r in _rows(con, "SELECT guid, name FROM toast_config WHERE location_id=?", (lid,))}

    # Toast's entry vocabulary varies by account. This one records TIP_OUT, CASH_COLLECTED, CLOSE_OUT_EXACT,
    # CLOSE_OUT_SHORTAGE, CLOSE_OUT_OVERAGE and NO_SALE — and does NOT use PAY_OUT or DRIVER_REIMBURSEMENT,
    # which an earlier version of this function handled while ignoring the two types carrying 96% of the
    # entries and $914k of movement. The result was a loss-prevention view of $0.00 that looked populated
    # because the entry COUNT was right. Anything unrecognised is now counted into `other` rather than
    # silently dropped, so the same mistake announces itself instead of reading as "nothing happened".
    #
    # TIP_OUT is deliberately excluded from the loss-prevention totals: it is the mechanics of tipping out
    # servers, not cash at risk, and at $715k it would dominate every chart it appeared in.
    # Grouped by DRAWER first, person second. Toast records the employee who PERFORMED the close-out, which at
    # a bar is the closing manager doing every drawer in the same minute — so a per-person table makes one
    # manager look responsible for every till in the building. Verified at Omaha on 2026-08-21: four drawers,
    # four shortages, one name, all timestamped 23:18. The drawer is the unit that means something; the person
    # is "who counted it", and the view says so in those words.
    by_day, by_emp, by_drawer, payouts, nosales = {}, {}, {}, {}, {}
    over = short = payout_total = collected = tipped_out = 0.0
    exact_n = over_n = short_n = nosale_n = other_n = 0
    other_types: dict[str, int] = {}
    for r in live:
        t = (r["type"] or "").upper()
        amt = float(r["amount"] or 0)
        d = r["business_date"]
        emp = names.get(r["employee_guid"]) or "(unattributed)"
        e = by_emp.setdefault(emp, {"over": 0.0, "short": 0.0, "exact": 0, "collected": 0.0, "nosales": 0})
        dw = cfg.get(r["drawer_guid"]) or r["drawer_guid"] or "(no drawer)"
        k = by_drawer.setdefault(dw, {"over": 0.0, "short": 0.0, "exact": 0, "over_n": 0, "short_n": 0, "shorts": []})
        day = by_day.setdefault(d, {"over": 0.0, "short": 0.0, "exact": 0})
        if t == "CLOSE_OUT_OVERAGE":
            over += abs(amt); over_n += 1; day["over"] += abs(amt); e["over"] += abs(amt)
            k["over"] += abs(amt); k["over_n"] += 1
        elif t == "CLOSE_OUT_SHORTAGE":
            short += abs(amt); short_n += 1; day["short"] += abs(amt); e["short"] += abs(amt)
            k["short"] += abs(amt); k["short_n"] += 1; k["shorts"].append(abs(amt))
        elif t == "CLOSE_OUT_EXACT":
            # A drawer that balanced. Without it there is no denominator, and a perfect night is
            # indistinguishable from a night nobody counted.
            exact_n += 1; day["exact"] += 1; e["exact"] += 1; k["exact"] += 1
        elif t == "CASH_COLLECTED":
            collected += abs(amt); e["collected"] += abs(amt)
        elif t == "TIP_OUT":
            tipped_out += abs(amt)
        elif t == "NO_SALE":
            nosale_n += 1; e["nosales"] += 1
            key = cfg.get(r["no_sale_reason"]) or r["reason"] or "(no reason given)"
            nosales[key] = nosales.get(key, 0) + 1
        elif t in ("PAY_OUT", "DRIVER_REIMBURSEMENT"):
            payout_total += abs(amt)
            key = cfg.get(r["payout_reason"]) or r["reason"] or "(no reason given)"
            p = payouts.setdefault(key, [0, 0.0]); p[0] += 1; p[1] += abs(amt)
        else:
            other_n += 1
            other_types[t or "(no type)"] = other_types.get(t or "(no type)", 0) + 1

    deps = _rows(con, """SELECT business_date d, SUM(amount) a, COUNT(*) n FROM toast_deposits
                         WHERE location_id=? AND business_date>=? AND business_date<=? AND COALESCE(undoes,'')=''
                         GROUP BY 1 ORDER BY 1""", (lid, since.isoformat(), through.isoformat()))
    closeouts = exact_n + over_n + short_n

    # Is this a float problem or a people problem? A drawer that is short because cash went missing is short by
    # a different amount every time. A drawer that is short because its expected starting cash is set wrong is
    # short by roughly the SAME amount every time, and so is every other drawer at that location. That is a
    # testable difference, and it is the difference between checking a setting and doubting a person — so the
    # portal tests it rather than leaving a director to infer it from a table.
    #
    # The test: at least three drawers, each short most times they were counted, with their typical shortages
    # clustered together. `typical` is the median, so one unusual night cannot manufacture the pattern.
    def _median(v):
        v = sorted(v)
        if not v:
            return None
        m = len(v) // 2
        return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2

    consistent = None
    # Three shortages minimum, not two. At Omaha a drawer with exactly two events had a median twenty times the
    # others and single-handedly hid a genuine pattern across three nightly drawers whose typical shortages were
    # within $10 of each other. Two events is not a typical anything.
    med_by_drawer = {d: _median(v["shorts"]) for d, v in by_drawer.items() if v["short_n"] >= 3}
    if len(med_by_drawer) >= 3:
        meds = sorted(med_by_drawer.values())
        lo, hi = meds[0], meds[-1]
        # Every drawer's typical shortage within 40% of the largest: tight enough that a shared cause is far
        # more likely than several independent ones.
        if hi and lo / hi >= 0.6:
            consistent = {"drawers": len(med_by_drawer), "low": _r(lo), "high": _r(hi),
                          "typical": _r(_median(meds))}

    # What the drawers are SUPPOSED to open with. The pattern test above can say "this looks like a setting";
    # only the intended float can say which setting, and by how much. inputs/floats.csv carries, per location
    # (drawer "*") or per drawer, the starting cash Toast is configured to expect and the cash actually put in.
    floats = _rows(con, "SELECT drawer, toast_expected, actual_float, notes FROM drawer_floats WHERE location_id=?", (lid,))
    float_check = None
    if floats:
        f0 = next((f for f in floats if f["drawer"] in ("*", "")), floats[0])
        exp_, act_ = f0["toast_expected"], f0["actual_float"]
        gap = (exp_ - act_) if (exp_ is not None and act_ is not None) else None
        verdict = None
        if gap is not None and consistent:
            # within $15 or 10% of the typical shortage: the float explains it
            verdict = "explained" if abs(gap - consistent["typical"]) <= max(15.0, 0.1 * consistent["typical"]) else "unexplained"
        elif gap is not None and abs(gap) >= 1:
            verdict = "mismatch"                        # floats differ, though no shortage pattern has shown up (yet)
        elif gap is not None:
            verdict = "aligned"
        float_check = {"toast_expected": exp_, "actual_float": act_, "gap": _r(gap) if gap is not None else None,
                       "verdict": verdict, "rows": [[f["drawer"], f["toast_expected"], f["actual_float"], f["notes"]] for f in floats]}

    return {
        "float_check": float_check,
        "days": [[d, _r(v["over"]), _r(v["short"]), v["exact"]] for d, v in sorted(by_day.items())],
        "by_employee": sorted(([k, _r(v["over"]), _r(v["short"]), v["exact"], _r(v["collected"]), v["nosales"]]
                               for k, v in by_emp.items()), key=lambda x: -(x[2] or 0))[:25],
        "payouts": sorted(([k, v[0], _r(v[1])] for k, v in payouts.items()), key=lambda x: -x[2])[:15],
        "nosales": sorted(([k, n] for k, n in nosales.items()), key=lambda x: -x[1])[:10],
        "deposits": [[r["d"], _r(r["a"]), r["n"]] for r in deps],
        "by_drawer": sorted(([d, v["exact"], v["over_n"], v["short_n"], _r(v["over"]), _r(v["short"]),
                              _r(_median(v["shorts"]))] for d, v in by_drawer.items()
                             if v["exact"] or v["over_n"] or v["short_n"]),
                            key=lambda x: -(x[5] or 0))[:25],
        "consistent_shortfall": consistent,
        "totals": {
            "over": _r(over), "short": _r(short), "net": _r(over - short), "payouts": _r(payout_total),
            "collected": _r(collected), "tipped_out": _r(tipped_out),
            "closeouts": closeouts, "exact": exact_n, "over_n": over_n, "short_n": short_n,
            # The share of counted drawers that balanced exactly. None when nothing was counted — which is a
            # different statement from 0% and must not render as one.
            "accuracy": _r(exact_n / closeouts, 4) if closeouts else None,
            "nosales": nosale_n,
            "entries": len(live), "reversed": len(rows) - len(live),
            "unrecognised": other_n,
            "unrecognised_types": sorted(other_types.items(), key=lambda x: -x[1])[:6],
        },
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


def tripleseat_section(con, slugs: list[str]) -> dict:
    """Connection status plus the catalog, so the Events page can say what it has before a single event exists.

    status: 'events' once webhook or API rows have produced events, 'catalog' when only the public key has
    spoken, 'none' otherwise. rooms/fees are per taproom (sliced into a director's bundle); event types and lead
    sources are company-wide picklists."""
    meta = {r[0]: r[1] for r in con.execute("SELECT key, value FROM meta WHERE key LIKE 'tripleseat_%'")}
    n_ev = con.execute("SELECT COUNT(*) FROM ts_events WHERE COALESCE(deleted,0)=0").fetchone()[0]
    n_hook = con.execute("SELECT COUNT(*) FROM ts_events WHERE source='webhook' AND COALESCE(deleted,0)=0").fetchone()[0]
    n_api = con.execute("SELECT COUNT(*) FROM ts_events WHERE COALESCE(source,'api')='api' AND COALESCE(deleted,0)=0").fetchone()[0]
    n_ld = con.execute("SELECT COUNT(*) FROM ts_leads").fetchone()[0]
    first_hook = meta.get("tripleseat_webhook_first") or con.execute("SELECT MIN(seen_at) FROM ts_events WHERE source='webhook'").fetchone()[0]
    cat = {"rooms": {}, "fees": {}, "event_types": [], "lead_sources": [], "lead_forms": {}, "ts_locations": {}}
    names = {r["room_id"]: r["name"] for r in _rows(con, "SELECT room_id, name FROM ts_rooms")}
    for r in _rows(con, "SELECT room_id, location_id, name, capacity, parent_room_id FROM ts_rooms WHERE is_unassigned=0 AND location_id IS NOT NULL ORDER BY location_id, name"):
        if r["location_id"] in slugs:
            cat["rooms"].setdefault(r["location_id"], []).append([r["room_id"], r["name"], r["capacity"], names.get(r["parent_room_id"])])
    for r in _rows(con, "SELECT location_id, name, value FROM ts_catalog WHERE kind='billing' ORDER BY location_id, name"):
        if r["location_id"] in slugs:
            cat["fees"].setdefault(r["location_id"], []).append([r["name"], r["value"]])
    cat["event_types"] = [r["name"] for r in _rows(con, "SELECT name FROM ts_catalog WHERE kind='event_type' ORDER BY name")]
    cat["lead_sources"] = [r["name"] for r in _rows(con, "SELECT name FROM ts_catalog WHERE kind='lead_source' ORDER BY name")]
    for r in _rows(con, "SELECT location_id, name FROM ts_catalog WHERE kind='lead_form' ORDER BY name"):
        if r["location_id"] in slugs:
            cat["lead_forms"].setdefault(r["location_id"], []).append(r["name"])
    for r in _rows(con, "SELECT id, name, location_id FROM ts_catalog WHERE kind='location'"):
        if r["location_id"] in slugs:
            cat["ts_locations"][r["location_id"]] = [r["id"], r["name"]]
    has_catalog = bool(cat["rooms"] or cat["event_types"])
    status = "events" if n_ev else ("catalog" if has_catalog else "none")
    return {"status": status, "catalog_at": meta.get("tripleseat_catalog_at"), "webhook_at": meta.get("tripleseat_webhook_at"),
            "webhook_rows": int(meta.get("tripleseat_webhook_cursor") or 0), "webhook_events": n_hook, "api_events": n_api, "webhook_since": first_hook,
            "events": n_ev, "leads": n_ld, "tenant": (settings().get("tripleseat") or {}).get("tenant"), **cat}


def slice_for_location(payload: dict, lid: str) -> dict:
    """A director's bundle: only their location (other locations are not merely hidden — they are absent)."""
    out = {"meta": dict(payload["meta"]), "locations": [l for l in payload["locations"] if l["id"] == lid], "activations": [a for a in payload["activations"] if a["loc"] == lid]}
    ts = payload.get("tripleseat")
    if ts:
        out["tripleseat"] = dict(ts, rooms={lid: ts["rooms"].get(lid)} if lid in ts["rooms"] else {},
                                 fees={lid: ts["fees"].get(lid)} if lid in ts["fees"] else {},
                                 lead_forms={lid: ts["lead_forms"].get(lid)} if lid in ts["lead_forms"] else {},
                                 ts_locations={lid: ts["ts_locations"].get(lid)} if lid in ts["ts_locations"] else {})
    else:
        out["tripleseat"] = ts
    # Company-wide comparisons, cut down to the rows this taproom is in. The other taprooms are not named:
    # a director sees their own price against the lowest and highest elsewhere, which is all the row is for.
    def _cut(rows, idx, val=lambda v: v):
        keep = []
        for r in rows or []:
            m = r[idx]
            if lid not in m:
                continue
            others = [val(v) for k, v in m.items() if k != lid]
            if not others:
                continue
            r2 = list(r)
            own, lo_, hi_ = val(m[lid]), min(others), max(others)
            r2[idx] = {lid: m[lid], "(lowest elsewhere)": lo_ if idx == 2 else [lo_, None], "(highest elsewhere)": hi_ if idx == 2 else [hi_, None]}
            # The gap and its cost are restated for THIS taproom. The company-wide figures are mostly other
            # taprooms' money and would read here as this director's own overspend.
            if idx == 2:
                if own == lo_ == hi_:
                    continue                                # this taproom is not part of the difference
                ref = lo_ if own > lo_ else hi_             # above the cheapest: by how much; otherwise how far under the dearest
                r2[3], r2[4] = _r(own - ref), (_r((own - ref) / ref, 4) if ref else None)
            else:
                best = min(own, lo_)
                r2[4] = _r((own - best) / best, 4) if best else None
                r2[5] = _r(max(0.0, own - best) * float(m[lid][1] or 0))
            keep.append(r2)
        keep.sort(key=lambda x: -abs((x[4] if idx == 2 else x[5]) or 0))
        return keep
    out["menu_prices"] = _cut(payload.get("menu_prices"), 2)
    out["price_compare"] = _cut(payload.get("price_compare"), 3, lambda v: v[0])
    for k in ("ops", "beer", "channels", "loyalty", "menu", "beer_mix", "compliance", "weather", "market", "daily", "hourly", "top_items", "labor_jobs", "vendors", "inventory", "targets", "payments", "dining", "revctr", "labor_hourly", "schedule", "tender", "voids", "invoices", "servers", "cash", "pacing", "digest", "discounts", "pnl", "sources", "events", "leads", "events_monthly"):
        out[k] = {lid: payload[k].get(lid)} if lid in payload[k] else {}
    return out


def run(through: date | None = None) -> dict:
    con = connect()
    p = build_payload(con, through)
    # Detail rides under its own key and is split out into per-location bundles by build_site, never published
    # inside the headline payload.
    through = date.fromisoformat(p["meta"]["through"])
    p["detail"] = {l["id"]: detail_for_location(con, l["id"], through) for l in p["locations"]}
    since = date.fromisoformat(p["meta"]["since"])
    for l in p["locations"]:
        try:
            p["detail"][l["id"]]["range"] = insights.range_detail(con, l["id"], since, through)
        except Exception as e:                        # the date picker degrades to fixed windows; the publish goes on
            log.error("metrics: range detail failed for %s (%s: %s)", l["id"], type(e).__name__, e)
    con.close()
    n = sum(len(v) for v in p["daily"].values())
    nd = sum(len(v.get("variance") or []) for v in p["detail"].values())
    log.info("metrics: %d locations, %d daily rows, %d detail variance rows, through %s", len(p["locations"]), n, nd, p["meta"]["through"])
    return p
