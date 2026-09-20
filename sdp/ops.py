"""Operational views built on data the warehouse already holds (added 2026-09-20).

Each function answers one question a director can act on the same day, and each is called through an isolating
wrapper in `metrics`: a failure here publishes that ONE section as null and never stops the nightly publish.

Conventions shared with `insights`:
  * people appear under the short display name the employee directory already builds (first name, last initial);
  * nobody is ranked or flagged on a handful of orders -- a rate on twenty covers is noise, and a flag built on
    noise starts a conversation the data cannot support;
  * where a view depends on fields only recent pulls carry, it publishes how much of its window is covered
    rather than presenting a part as the whole.
"""
from __future__ import annotations

import re
import statistics
from datetime import date, datetime, timedelta

from .insights import channel_of
from .transform import NON_LABOR_JOBS
from .util import locations, settings, to_local

MIN_ORDERS = 40          # below this a person's void / discount rate is not reported as a rate at all


def _rows(con, sql, args=()):
    cur = con.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _r(v, nd=2):
    return None if v is None else round(float(v), nd)


def _names(con, lid: str) -> dict:
    return {r["employee_guid"]: (r["display_name"] or "").strip() or "(unnamed)"
            for r in _rows(con, "SELECT employee_guid, display_name FROM toast_employees WHERE location_id=?", (lid,))}


def _ts(s: str | None):
    """Toast timestamps are UTC ISO strings. Only differences are taken from these, so no zone is applied."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _median(v):
    return statistics.median(v) if v else None


# ------------------------------------------------------------------------------------------ comps and voids

def comps(con, lid: str, w28: date, through: date) -> dict:
    """Who gives product away, who approves it, and why things are voided.

    Loyalty redemptions are left out: a guest spending points is not a decision anyone at the taproom made.
    A person is flagged only when BOTH their rate is at least double the taproom's own rate AND the dollars are
    material -- either alone produces flags about nothing.
    """
    a, b = w28.isoformat(), through.isoformat()
    names = _names(con, lid)
    # Automatic promotions (happy hour and the like) are priced by the clock, not by a person. They are shown in
    # their own column and kept out of the rate and the flag: otherwise whoever works the happy-hour shift is
    # "discounting at twice the taproom rate" for doing their job.
    promo_words = [str(w).lower() for w in ((settings().get("comps") or {}).get("promotions") or ["happy hour"]) if str(w).strip()]

    def is_promo(nm):
        n = str(nm or "").lower()
        return any(w in n for w in promo_words)

    disc = _rows(con, """
        SELECT d.business_date bd, d.approver_guid ap, o.server_guid sv, COALESCE(d.name,'(unnamed)') nm, COALESCE(d.reason,'') rs,
               d.amount amt, COALESCE(d.captured,0) cap
        FROM toast_discounts d LEFT JOIN toast_orders o ON o.order_guid=d.order_guid
        WHERE d.location_id=? AND d.business_date>=? AND d.business_date<=?
          AND COALESCE(d.loyalty_vendor,'')='' AND COALESCE(o.voided,0)=0""", (lid, a, b))
    per = {r["g"] or "": r for r in _rows(con, """
        SELECT COALESCE(server_guid,'') g, COUNT(*) n, COALESCE(SUM(net_sales),0) net, COALESCE(SUM(gross_sales),0) gross
        FROM toast_orders WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=? GROUP BY 1""", (lid, a, b))}
    if not per:
        return None
    tot_gross = sum(float(r["gross"] or 0) for r in per.values())
    promo = [r for r in disc if is_promo(r["nm"])]
    disc = [r for r in disc if not is_promo(r["nm"])]
    tot_disc = sum(float(r["amt"] or 0) for r in disc)
    tot_promo = sum(float(r["amt"] or 0) for r in promo)
    # Days pulled by a version that reads approver and void reason. On any other day a missing reason means
    # "not looked at", which must never be shown as "staff gave no reason".
    cap_days = {r["d"] for r in _rows(con, """SELECT DISTINCT business_date d FROM toast_discounts
                                               WHERE location_id=? AND business_date>=? AND business_date<=? AND COALESCE(captured,0)=1""", (lid, a, b))}

    # Item-level voids, by whoever owned the order. Whole-order voids are already on the server table.
    voids = _rows(con, """
        SELECT i.business_date bd, COALESCE(o.server_guid,'') g, COALESCE(NULLIF(i.void_reason,''),'') rs, COALESCE(i.pre_discount_price,0) v
        FROM toast_order_items i JOIN toast_orders o ON o.order_guid=i.order_guid
        WHERE i.location_id=? AND i.business_date>=? AND i.business_date<=? AND i.voided=1 AND COALESCE(o.voided,0)=0""", (lid, a, b))
    tot_void = sum(float(r["v"] or 0) for r in voids)

    by_srv: dict[str, dict] = {}
    blank = lambda: {"disc": 0.0, "nd": 0, "void": 0.0, "nv": 0, "promo": 0.0}
    for r in disc:
        o = by_srv.setdefault(r["sv"] or "", blank())
        o["disc"] += float(r["amt"] or 0); o["nd"] += 1
    for r in promo:
        by_srv.setdefault(r["sv"] or "", blank())["promo"] += float(r["amt"] or 0)
    for r in voids:
        o = by_srv.setdefault(r["g"], blank())
        o["void"] += float(r["v"] or 0); o["nv"] += 1

    base_d = (tot_disc / tot_gross) if tot_gross else None
    base_v = (tot_void / tot_gross) if tot_gross else None
    people = []
    for g, o in by_srv.items():
        p = per.get(g)
        n = (p or {}).get("n") or 0
        gross = float((p or {}).get("gross") or 0)
        enough = n >= MIN_ORDERS and gross > 0
        dr = (o["disc"] / gross) if enough else None
        vr = (o["void"] / gross) if enough else None
        flag = []
        if dr is not None and base_d and dr >= 2 * base_d and o["disc"] >= 100:
            flag.append("discounts")
        if vr is not None and base_v and vr >= 2 * base_v and o["void"] >= 100:
            flag.append("voids")
        people.append([names.get(g) or ("(online / no server)" if not g else "(unnamed)"), n, _r(gross),
                       _r(o["disc"]), o["nd"], _r(dr, 4), _r(o["void"]), o["nv"], _r(vr, 4), flag, _r(o["promo"])])
    people.sort(key=lambda x: -((x[3] or 0) + (x[6] or 0)))

    by_ap: dict[str, list] = {}
    for r in disc:
        if not r["cap"]:
            continue
        k = r["ap"] or ""
        o = by_ap.setdefault(k, [0.0, 0]); o[0] += float(r["amt"] or 0); o[1] += 1
    cap_amt = sum(v[0] for v in by_ap.values())
    approvers = sorted(([names.get(k) or ("(no approval recorded)" if not k else "(unnamed)"), _r(v[0]), v[1],
                         _r(v[0] / cap_amt, 4) if cap_amt else None] for k, v in by_ap.items()), key=lambda x: -(x[1] or 0))

    by_name: dict[tuple, list] = {}
    for r in disc:
        o = by_name.setdefault((r["nm"], r["rs"]), [0.0, 0]); o[0] += float(r["amt"] or 0); o[1] += 1
    reasons_v: dict[str, list] = {}
    for r in voids:
        o = reasons_v.setdefault(r["rs"] or ("(no reason given)" if r["bd"] in cap_days else "(day not re-pulled yet)"), [0.0, 0]); o[0] += float(r["v"] or 0); o[1] += 1

    n_cap = sum(1 for r in disc if r["cap"])
    return {"window": [a, b],
            "base": {"gross": _r(tot_gross), "promo": _r(tot_promo), "disc": _r(tot_disc), "disc_rate": _r(base_d, 4), "void": _r(tot_void), "void_rate": _r(base_v, 4)},
            "people": people[:40],
            "approvers": approvers[:20],
            "discounts": sorted(([k[0], k[1], _r(v[0]), v[1]] for k, v in by_name.items()), key=lambda x: -(x[2] or 0))[:25],
            "void_reasons": sorted(([k, _r(v[0]), v[1]] for k, v in reasons_v.items()), key=lambda x: -(x[1] or 0))[:15],
            # how much of the window was pulled by a version that reads the approver -- the rest predates it
            "approver_coverage": _r(n_cap / len(disc), 3) if disc else None,
            "promotions": promo_words, "min_orders": MIN_ORDERS}


# ---------------------------------------------------------------------------------------------- tab duration

DAYPARTS = [("Lunch", 0, 14), ("Afternoon", 14, 17), ("Dinner", 17, 21), ("Late", 21, 24)]


def tabs(con, lid: str, w28: date, through: date, tz: str | None = None) -> dict:
    """How long a tab stays open, and how full the room gets.

    Duration is opened-to-closed per order, between 2 minutes and 8 hours: shorter is a quick counter sale that
    says nothing about dwell, longer is a tab nobody closed until the end of the night (counted separately,
    because that is its own problem). Medians, not means -- a few marathon tabs would otherwise set the figure.

    "Open at once" counts orders open at the half-hour of each local clock hour, averaged over the days in the
    window. Against the seat count it shows whether Friday at 7 is limited by the room or by demand.
    """
    rows = _rows(con, """
        SELECT business_date d, opened_at o, closed_at c, COALESCE(dining_option,'?') opt, net_sales net, guests g
        FROM toast_orders WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=?
          AND opened_at IS NOT NULL AND closed_at IS NOT NULL""", (lid, w28.isoformat(), through.isoformat()))
    if not rows:
        return None
    by_dp: dict[tuple, list] = {}
    by_opt: dict[str, list] = {}
    conc: dict[tuple, int] = {}
    days_by_dow: dict[int, set] = {}
    left_open = quick = off = 0
    for r in rows:
        if channel_of(r["opt"]) not in ("On premise", "Other"):
            off += 1                                   # delivery and takeout never sat in the room
            continue
        o, c = _ts(r["o"]), _ts(r["c"])
        if not o or not c or c < o:
            continue
        mins = (c - o).total_seconds() / 60.0
        try:
            lo = to_local(r["o"], tz)
        except Exception:
            continue
        bd = date.fromisoformat(r["d"])
        dow = (bd.weekday() + 1) % 7                 # 0 = Sunday, to match the hourly heatmap
        days_by_dow.setdefault(dow, set()).add(r["d"])
        if mins > 480:
            left_open += 1
            continue
        # occupancy: every local clock hour whose :30 falls inside the tab
        t = lo.replace(minute=30, second=0, microsecond=0)
        if t < lo:
            t += timedelta(hours=1)
        end = lo + timedelta(minutes=mins)
        while t <= end:
            conc[(dow, t.hour)] = conc.get((dow, t.hour), 0) + 1
            t += timedelta(hours=1)
        if mins < 2:
            quick += 1
            continue
        # after midnight is the end of last night, not an early lunch
        part = "Late" if lo.hour < 4 else next((nm for nm, a, b in DAYPARTS if a <= lo.hour < b), "Late")
        wk = "Fri–Sat" if bd.weekday() in (4, 5) else "Sun–Thu"
        by_dp.setdefault((part, wk), []).append(mins)
        by_opt.setdefault(r["opt"], []).append(mins)

    def pack(v):
        v = sorted(v)
        return [len(v), _r(_median(v), 0), _r(v[int(len(v) * 0.75)] if v else None, 0)]

    dayparts = [[nm, wk] + pack(by_dp.get((nm, wk), [])) for nm, _, _ in DAYPARTS for wk in ("Sun–Thu", "Fri–Sat") if by_dp.get((nm, wk))]
    occ = sorted([[dow, h, _r(n / max(len(days_by_dow.get(dow, [])), 1), 1)] for (dow, h), n in conc.items()])
    peak = max(occ, key=lambda x: x[2]) if occ else None
    seats = next((l.get("seats") for l in locations() if l["slug"] == lid), None)
    return {"dayparts": dayparts,
            "options": sorted(([k] + pack(v) for k, v in by_opt.items() if len(v) >= 30), key=lambda x: -x[1])[:8],
            "occupancy": occ, "peak": peak, "seats": seats,
            "left_open": left_open, "quick": quick, "off_premise": off, "orders": len(rows)}


# ------------------------------------------------------------------------------------------------ overtime

def overtime(con, lid: str, through: date) -> dict:
    """Who is heading for overtime THIS week, while the schedule can still be changed.

    Worked hours so far this pay week plus what is still on the schedule for the rest of it. The pay week's
    first day comes from config (`labor.week_start`, 0 = Monday); the threshold from `labor.overtime_hours`.
    Hours are per taproom: somebody who also works at another taproom is only seen here for this one, and the
    panel says so.
    """
    cfg = (settings().get("labor") or {})
    ws = int(cfg.get("week_start", 0)) % 7
    limit = float(cfg.get("overtime_hours", 40))
    today = through + timedelta(days=1)                      # the first day nobody has worked yet
    wk0 = today - timedelta(days=(today.weekday() - ws) % 7)
    wk1 = wk0 + timedelta(days=6)
    names = _names(con, lid)

    worked = {r["g"]: float(r["h"] or 0) for r in _rows(con, """
        SELECT employee_guid g, SUM(COALESCE(regular_hours,0)+COALESCE(overtime_hours,0)) h FROM toast_time_entries
        WHERE location_id=? AND business_date>=? AND business_date<=? GROUP BY 1""", (lid, wk0.isoformat(), through.isoformat()))}
    ahead = {r["g"]: float(r["h"] or 0) for r in _rows(con, """
        SELECT employee_guid g, SUM(hours) h FROM toast_shifts
        WHERE location_id=? AND deleted=0 AND business_date>? AND business_date<=?
          AND COALESCE(job_name,'') NOT IN (%s) GROUP BY 1""" % (",".join("?" * len(NON_LABOR_JOBS)) or "''"),
        (lid, through.isoformat(), wk1.isoformat(), *sorted(NON_LABOR_JOBS)))}
    people = []
    for g in set(worked) | set(ahead):
        w, s = worked.get(g, 0.0), ahead.get(g, 0.0)
        if w + s >= limit - 4:                                # within four hours of the line is worth a look
            people.append([names.get(g) or "(unnamed)", _r(w, 1), _r(s, 1), _r(w + s, 1), _r(max(0.0, w + s - limit), 1)])
    people.sort(key=lambda x: -x[3])

    # Eight weeks of what overtime has actually cost: the premium is the extra half, not the whole hour.
    hist = {}
    for r in _rows(con, """
        SELECT business_date d, SUM(COALESCE(overtime_hours,0)) oh, SUM(COALESCE(overtime_hours,0)*COALESCE(hourly_wage,0)*0.5) prem,
               SUM(COALESCE(regular_hours,0)+COALESCE(overtime_hours,0)) h
        FROM toast_time_entries WHERE location_id=? AND business_date>=? AND business_date<=? GROUP BY 1""",
                   (lid, (wk0 - timedelta(days=56)).isoformat(), through.isoformat())):
        d = date.fromisoformat(r["d"])
        k = (d - timedelta(days=(d.weekday() - ws) % 7)).isoformat()
        o = hist.setdefault(k, [0.0, 0.0, 0.0]); o[0] += float(r["oh"] or 0); o[1] += float(r["prem"] or 0); o[2] += float(r["h"] or 0)
    return {"week": [wk0.isoformat(), wk1.isoformat()], "limit": limit, "through": through.isoformat(),
            "has_schedule": bool(ahead), "people": people[:30],
            "history": [[k, _r(v[0], 1), _r(v[1]), _r(v[2], 1)] for k, v in sorted(hist.items())]}


# ------------------------------------------------------------------------ gift cards, $0 items, guest counts

_GIFT = re.compile(r"\bgift\s*(card|certificate)s?\b|\be-?gift\b", re.I)


def checks(con, lid: str, w28: date, through: date) -> dict:
    """Three small things the POS knows and nobody looks at."""
    a, b = w28.isoformat(), through.isoformat()
    sold = [r for r in _rows(con, """
        SELECT item_name nm, COALESCE(sales_category,'') sc, SUM(price) v, SUM(quantity) q FROM toast_order_items
        WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=? GROUP BY 1,2""", (lid, a, b))
            if _GIFT.search(str(r["nm"] or "")) or _GIFT.search(str(r["sc"] or ""))]
    red = con.execute("""SELECT COALESCE(SUM(amount),0), COUNT(*) FROM toast_payments
                         WHERE location_id=? AND business_date>=? AND business_date<=? AND UPPER(COALESCE(type,''))='GIFTCARD'""",
                      (lid, a, b)).fetchone()

    # Rung at nothing: no price before discounts, not voided. Plenty are legitimate (water, a modifier-priced
    # draft button); the list is for a director to recognise the ones that are not.
    zero = [[r["nm"], r["bucket"], int(r["n"] or 0)] for r in _rows(con, """
        SELECT COALESCE(item_name,'(unnamed)') nm, COALESCE(bucket,'') bucket, SUM(quantity) n FROM toast_order_items
        WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=?
          AND COALESCE(pre_discount_price,0)=0 AND COALESCE(price,0)=0 AND quantity>0
        GROUP BY 1,2 HAVING SUM(quantity)>=5 ORDER BY n DESC LIMIT 20""", (lid, a, b))]

    g = con.execute("""SELECT COUNT(*), SUM(CASE WHEN COALESCE(guests,0)=0 THEN 1 ELSE 0 END),
                              SUM(CASE WHEN guests=1 THEN 1 ELSE 0 END), SUM(CASE WHEN guests>=2 THEN 1 ELSE 0 END)
                       FROM toast_orders WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=?""",
                    (lid, a, b)).fetchone()
    n = g[0] or 0
    guests = None
    if n:
        p2 = (g[3] or 0) / n
        # Toast fills in 1 when nobody enters a number, so "1" is mostly silence. Per-guest figures are only
        # worth showing where a real share of orders carry a party size somebody typed.
        guests = {"orders": n, "zero": _r((g[1] or 0) / n, 3), "one": _r((g[2] or 0) / n, 3), "two_plus": _r(p2, 3),
                  "usable": bool(p2 >= 0.35)}
    return {"gift": {"sold": _r(sum(float(r["v"] or 0) for r in sold)), "sold_n": int(sum(float(r["q"] or 0) for r in sold)),
                     "redeemed": _r(red[0]), "redeemed_n": red[1]},
            "zero_rung": zero, "guests": guests}


# --------------------------------------------------------------------------------------------- out of stock

def stock(con, lid: str, through: date, days: int = 28) -> dict:
    since = (through - timedelta(days=days)).isoformat()
    snaps = _rows(con, "SELECT snap_date d, n_out FROM toast_stock_days WHERE location_id=? AND snap_date>=? ORDER BY 1", (lid, since))
    if not snaps:
        return None
    last = snaps[-1]["d"]
    now = [[r["name"] or "(item not on the current menu)", r["status"], _r(r["quantity"], 0)] for r in _rows(con, """
        SELECT name, status, quantity FROM toast_stock WHERE location_id=? AND snap_date=? ORDER BY status, name""", (lid, last))]
    often = [[r["name"], r["n"]] for r in _rows(con, """
        SELECT COALESCE(name,'(item not on the current menu)') name, COUNT(DISTINCT snap_date) n FROM toast_stock
        WHERE location_id=? AND snap_date>=? AND status='OUT_OF_STOCK' GROUP BY 1 HAVING n>=2 ORDER BY n DESC LIMIT 15""", (lid, since))]
    return {"as_of": last, "snapshots": len(snaps), "now": now[:40], "often": often}


# ------------------------------------------------------------------------------------ MarginEdge invoice health

def invoice_health(con, lid: str, through: date) -> dict:
    """How late invoices are entered, and which regular vendors have gone quiet.

    A vendor is "regular" with five or more invoice dates in the last ten weeks and a typical gap of ten days or
    less. It is "quiet" when the time since its last invoice date is more than twice that gap, plus three days,
    PLUS however long this taproom usually takes to enter an invoice -- without that allowance every daily
    vendor would look quiet every morning simply because last week's paper has not been keyed yet. A quiet
    regular vendor is usually a delivery whose invoice never reached MarginEdge, which understates cost of
    goods until it does.
    """
    a = (through - timedelta(days=70)).isoformat()
    inv = _rows(con, """
        SELECT COALESCE(vendor_name,'(no vendor)') v, invoice_date d, created_date c, order_total t FROM me_invoices
        WHERE location_id=? AND invoice_date>=? AND invoice_date<=? AND COALESCE(is_credit,0)=0""", (lid, a, through.isoformat()))
    if not inv:
        return None
    lags, by_v = [], {}
    for r in inv:
        try:
            d = date.fromisoformat(str(r["d"])[:10])
        except ValueError:
            continue
        by_v.setdefault(r["v"], []).append((d, float(r["t"] or 0)))
        try:
            c = date.fromisoformat(str(r["c"])[:10])
            if 0 <= (c - d).days <= 120:
                lags.append((c - d).days)
        except (ValueError, TypeError):
            pass
    quiet = []
    lags.sort()
    slack = (lags[int(len(lags) * 0.9)] if lags else 7)          # the slowest tenth of this taproom's own entry lag
    for v, xs in by_v.items():
        ds = sorted({d for d, _ in xs})
        if len(ds) < 5:
            continue
        gaps = [(ds[i + 1] - ds[i]).days for i in range(len(ds) - 1)]
        g = _median(gaps)
        since_last = (through - ds[-1]).days
        if g and g <= 10 and since_last > 2 * g + 3 + slack:
            avg = sum(t for _, t in xs) / len(xs)
            quiet.append([v, ds[-1].isoformat(), since_last, _r(g, 0), len(ds), _r(avg)])
    quiet.sort(key=lambda x: -(x[5] or 0))
    lags.sort()
    return {"slack": slack, "lag": {"n": len(lags), "median": _r(_median(lags), 0), "p90": _r(lags[int(len(lags) * 0.9)] if lags else None, 0),
                    "late_share": _r(sum(1 for x in lags if x > 7) / len(lags), 3) if lags else None},
            "quiet": quiet[:12]}


# ------------------------------------------------------------------------------------------ company-wide views

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def menu_price_consistency(con) -> list:
    """The same menu item priced differently across taprooms. Items with no base price (draft buttons priced by
    their size modifier) are skipped -- there is nothing to compare."""
    by: dict[str, dict] = {}
    for r in _rows(con, "SELECT location_id l, name, sales_category sc, price FROM toast_menu_items WHERE COALESCE(price,0)>0"):
        k = _norm(r["name"])
        if not k:
            continue
        o = by.setdefault(k, {"name": r["name"], "sc": r["sc"], "p": {}})
        # one item can sit on several menus at one taproom; keep the highest, which is the everyday price
        o["p"][r["l"]] = max(o["p"].get(r["l"], 0.0), float(r["price"]))
    out = []
    for o in by.values():
        if len(o["p"]) < 2:
            continue
        lo, hi = min(o["p"].values()), max(o["p"].values())
        if hi - lo >= 0.5:
            out.append([o["name"], o["sc"], {k: _r(v) for k, v in o["p"].items()}, _r(hi - lo), _r((hi - lo) / lo, 4)])
    out.sort(key=lambda x: -(x[4] or 0))
    return out[:80]


def purchase_price_compare(con, through: date, days: int = 90) -> list:
    """The same vendor's same item code bought at different prices by different taprooms.

    Keyed on vendor plus the vendor's own item code, never on product alone: the same product in a different
    pack has a different unit price, and comparing those manufactures a gap that does not exist.
    """
    since = (through - timedelta(days=days)).isoformat()
    by: dict[tuple, dict] = {}
    for r in _rows(con, """
        SELECT l.location_id loc, COALESCE(v.central_vendor_id, i.vendor_name) vk, i.vendor_name vn, l.vendor_item_code code,
               COALESCE(l.vendor_item_name,'(unnamed)') nm, COALESCE(l.bucket,'Other') bucket, l.unit_price up, l.quantity q
        FROM me_invoice_lines l
        JOIN me_invoices i ON i.order_id=l.order_id AND i.location_id=l.location_id
        LEFT JOIN me_vendors v ON v.vendor_id=i.vendor_id AND v.location_id=i.location_id
        WHERE l.invoice_date>=? AND l.invoice_date<=? AND COALESCE(i.is_credit,0)=0
          AND l.unit_price>0 AND l.quantity>0 AND COALESCE(l.vendor_item_code,'')!=''""", (since, through.isoformat())):
        o = by.setdefault((str(r["vk"]), str(r["code"])), {"nm": r["nm"], "vn": r["vn"], "bucket": r["bucket"], "loc": {}})
        x = o["loc"].setdefault(r["loc"], [0.0, 0.0]); x[0] += float(r["up"]) * float(r["q"]); x[1] += float(r["q"])
    out = []
    for o in by.values():
        if len(o["loc"]) < 2:
            continue
        price = {k: v[0] / v[1] for k, v in o["loc"].items() if v[1] > 0}
        lo = min(price.values())
        if lo <= 0:
            continue
        gap = (max(price.values()) - lo) / lo
        if gap < 0.03 or gap > 0.6:                  # past 60% it is almost certainly a different pack, not a different price
            continue
        over = sum((price[k] - lo) * o["loc"][k][1] for k in price)     # what paying above the best price cost
        out.append([o["nm"], o["vn"], o["bucket"], {k: [_r(price[k], 4), _r(o["loc"][k][1], 1)] for k in price}, _r(gap, 4), _r(over)])
    out.sort(key=lambda x: -(x[5] or 0))
    return out[:60]
