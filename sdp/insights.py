"""Second-generation views: beer volume, menu behaviour, channels, loyalty, counting discipline, weather,
market depletions, and the daily-grain data behind the date-range picker.

Kept apart from metrics.py because these share a different temperament. metrics.py reports what the source
systems already state (sales, labour, cash). Most of what is here is DERIVED — a pour size read out of a modifier,
a channel read out of a dining-option name, a brand matched across three systems by its name — and every one of
those derivations can fail quietly. So the rule throughout this module is the one the first verification pass
taught: publish the coverage next to the number. Every section says how much of the underlying data it managed to
interpret, and lists what it could not, so a wrong-looking figure can be traced to a specific unparsed string
rather than argued about.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

from .pours import brand_key, brand_label, keg_ounces
from .util import settings


def _rows(con, sql, args=()):
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def _r(v, nd=2):
    return round(v, nd) if isinstance(v, float) else v


# ------------------------------------------------------------------------------------------ beer volume

def beer_volume(con, lid: str, through: date, max_days: int = 56) -> dict:
    """Ounces poured against ounces received — beer variance with no dollar value attached.

    The window is "as far back as pour sizes were captured, up to eight weeks". Sizes live in Toast modifiers,
    which the warehouse only began keeping on the day this shipped, so for the first weeks the window is short and
    the view says so. Days before capture are not estimated; they are left out.
    """
    since = through - timedelta(days=max_days - 1)
    cap = con.execute("""SELECT MIN(business_date), COUNT(DISTINCT business_date) FROM toast_order_items
                         WHERE location_id=? AND bucket='Beer' AND modifiers IS NOT NULL AND business_date>=? AND business_date<=?""",
                      (lid, since.isoformat(), through.isoformat())).fetchone()
    out = {"window": None, "days": 0, "brands": [], "sizes": [], "unsized": [], "unsized_pkg": [], "totals": None,
           "supply": None, "count_yield": None}
    if not cap or not cap[0]:
        return out
    start = cap[0]
    out["window"] = [start, through.isoformat()]
    out["days"] = cap[1]

    rows = _rows(con, """
        SELECT COALESCE(item_name,'(unnamed)') nm, COALESCE(pour,'draft') pour, SUM(quantity) q,
               SUM(CASE WHEN size_oz IS NOT NULL THEN quantity ELSE 0 END) sized_q,
               SUM(CASE WHEN size_oz IS NOT NULL THEN quantity*size_oz ELSE 0 END) oz, SUM(price) net
        FROM toast_order_items
        WHERE location_id=? AND bucket='Beer' AND voided=0 AND modifiers IS NOT NULL AND business_date>=? AND business_date<=?
        GROUP BY 1,2""", (lid, start, through.isoformat()))
    brands: dict[str, dict] = {}
    T = {"draft_q": 0.0, "draft_sized_q": 0.0, "draft_oz": 0.0, "pkg_q": 0.0, "pkg_oz": 0.0, "net": 0.0}
    for r in rows:
        # "Easy Eddy" and "Easy Eddy 16oz" are one beer rung two ways; the size is already in size_oz.
        b = brands.setdefault(brand_label(r["nm"]) or "(unnamed)", {"draft_oz": 0.0, "draft_q": 0.0, "unsized_q": 0.0, "pkg_q": 0.0, "net": 0.0})
        q, sq, oz = float(r["q"] or 0), float(r["sized_q"] or 0), float(r["oz"] or 0)
        b["net"] += float(r["net"] or 0); T["net"] += float(r["net"] or 0)
        if r["pour"] == "package":
            b["pkg_q"] += q; T["pkg_q"] += q; T["pkg_oz"] += oz
        else:
            b["draft_q"] += q; b["draft_oz"] += oz; b["unsized_q"] += q - sq
            T["draft_q"] += q; T["draft_sized_q"] += sq; T["draft_oz"] += oz
    out["brands"] = sorted(([k, _r(v["draft_oz"], 0), _r(v["draft_q"], 0), _r(v["unsized_q"], 0), _r(v["pkg_q"], 0), _r(v["net"])]
                            for k, v in brands.items()), key=lambda x: -(x[1] or 0))[:40]
    out["sizes"] = [[_r(r["oz"], 1), _r(r["q"], 0)] for r in _rows(con, """
        SELECT size_oz oz, SUM(quantity) q FROM toast_order_items
        WHERE location_id=? AND bucket='Beer' AND voided=0 AND COALESCE(pour,'draft')='draft' AND size_oz IS NOT NULL
          AND modifiers IS NOT NULL AND business_date>=? AND business_date<=? GROUP BY 1 ORDER BY q DESC LIMIT 12""",
        (lid, start, through.isoformat()))]
    # The exact text that would not parse, most frequent first. This list is the to-do list for config.
    for key, pour in (("unsized", "draft"), ("unsized_pkg", "package")):
        out[key] = [[r["t"], _r(r["q"], 0)] for r in _rows(con, """
            SELECT TRIM(COALESCE(item_name,'(unnamed)') || CASE WHEN COALESCE(modifiers,'')!='' THEN '  ['||modifiers||']' ELSE '' END) t, SUM(quantity) q
            FROM toast_order_items
            WHERE location_id=? AND bucket='Beer' AND voided=0 AND size_oz IS NULL AND COALESCE(pour,'draft')=?
              AND modifiers IS NOT NULL AND business_date>=? AND business_date<=? GROUP BY 1 ORDER BY q DESC LIMIT 15""",
            (lid, pour, start, through.isoformat()))]
    out["totals"] = {"draft_qty": _r(T["draft_q"], 0), "draft_sized_qty": _r(T["draft_sized_q"], 0), "draft_oz": _r(T["draft_oz"], 0),
                     "coverage": _r(T["draft_sized_q"] / T["draft_q"], 4) if T["draft_q"] else None,
                     "pkg_qty": _r(T["pkg_q"], 0), "net": _r(T["net"]),
                     "bbl": _r(T["draft_oz"] / 3968.0, 1)}                   # 31 US gal x 128 oz

    # ---- supply side: keg ounces received (MarginEdge invoices), same window
    lines = _rows(con, """
        SELECT COALESCE(pr.name, l.vendor_item_name, '(unnamed)') nm, l.vendor_item_name vin, l.packaging_id pkg, pr.report_unit ru,
               COALESCE(i.vendor_name,'') vendor, SUM(l.quantity) q, SUM(l.line_price) v
        FROM me_invoice_lines l
        JOIN me_invoices i ON i.order_id=l.order_id AND i.location_id=l.location_id
        LEFT JOIN me_products pr ON pr.product_id=l.product_id AND pr.location_id=l.location_id
        WHERE l.location_id=? AND l.bucket='Beer' AND l.invoice_date>=? AND l.invoice_date<=? AND COALESCE(i.is_credit,0)=0 AND l.quantity>0
        GROUP BY 1,2,3,4,5""", (lid, start, through.isoformat()))
    rec_by_brand: dict[str, float] = {}
    rec_names: dict[str, str] = {}
    rec_oz = 0.0; kegs = 0.0; unparsed = {}
    for l in lines:
        oz = keg_ounces(l["nm"], l["vin"], l["pkg"])
        if oz:
            rec_oz += oz * float(l["q"] or 0); kegs += float(l["q"] or 0)
            k = brand_key(l["nm"])
            rec_by_brand[k] = rec_by_brand.get(k, 0.0) + oz * float(l["q"] or 0)
            rec_names.setdefault(k, l["nm"])
        else:
            unparsed[l["nm"]] = unparsed.get(l["nm"], 0.0) + float(l["q"] or 0)
    sold_by_brand: dict[str, list] = {}
    for nm, v in brands.items():
        k = brand_key(nm)
        o = sold_by_brand.setdefault(k, [nm, 0.0, -1.0])
        o[1] += v["draft_oz"]
        if v["draft_oz"] > o[2]:                       # label the brand by its biggest DRAFT seller, not a six-pack
            o[0], o[2] = nm, v["draft_oz"]
    match = []
    for k in set(rec_by_brand) | {k for k, v in sold_by_brand.items() if v[1] > 0}:
        s = sold_by_brand.get(k, [rec_names.get(k, k) + " (received, not rung under this name)", 0.0]); rcv = rec_by_brand.get(k, 0.0)
        match.append([s[0] if s[0] else k, _r(s[1], 0), _r(rcv, 0), _r(s[1] / rcv, 4) if rcv else None])
    match.sort(key=lambda x: -((x[1] or 0) + (x[2] or 0)))
    out["supply"] = {"received_oz": _r(rec_oz, 0), "kegs": _r(kegs, 1), "invoice_lines": len(lines),
                     "pour_through": _r(T["draft_oz"] / rec_oz, 4) if rec_oz else None,
                     "by_brand": match[:30],
                     "unparsed": sorted(([k, _r(v, 1)] for k, v in unparsed.items()), key=lambda x: -x[1])[:15]}

    # ---- count-based yield, when two beer counts both fall inside the captured window.
    # Only products that appear on BOTH sheets are used. A partial sheet on the closing date would otherwise read
    # as kegs that vanished — the same trap as the uncounted inventory category, in ounces instead of dollars.
    cnt = _rows(con, """
        SELECT iv.inventory_date d, COALESCE(ii.product_id, ii.product_name) pid, MAX(ii.product_name) nm, MAX(ii.unit) u, SUM(ii.quantity) q
        FROM me_inventory_items ii JOIN me_inventories iv ON iv.inventory_id=ii.inventory_id AND iv.location_id=ii.location_id
        WHERE ii.location_id=? AND ii.bucket='Beer' AND iv.inventory_date>=? AND iv.inventory_date<=? GROUP BY 1,2""",
        (lid, start, through.isoformat()))
    by_date: dict[str, dict] = {}
    for c in cnt:
        oz = keg_ounces(c["nm"], c["u"])
        if oz and c["d"]:
            by_date.setdefault(c["d"], {})[c["pid"]] = oz * float(c["q"] or 0)
    ds = sorted(by_date)
    if len(ds) >= 2:
        a, b = ds[0], ds[-1]
        both = set(by_date[a]) & set(by_date[b])
        if both:
            rcv = 0.0
            for l in _rows(con, """SELECT l.product_id pid, COALESCE(pr.name, l.vendor_item_name,'') nm, l.vendor_item_name vin, l.packaging_id pkg, SUM(l.quantity) q
                                   FROM me_invoice_lines l
                                   JOIN me_invoices i ON i.order_id=l.order_id AND i.location_id=l.location_id
                                   LEFT JOIN me_products pr ON pr.product_id=l.product_id AND pr.location_id=l.location_id
                                   WHERE l.location_id=? AND l.bucket='Beer' AND l.invoice_date>? AND l.invoice_date<=? AND l.quantity>0
                                     AND COALESCE(i.is_credit,0)=0 GROUP BY 1,2,3,4""", (lid, a, b)):
                oz = keg_ounces(l["nm"], l["vin"], l["pkg"])
                if oz and (l["pid"] in both or l["nm"] in both):
                    rcv += oz * float(l["q"] or 0)
            keys = {brand_key(c["nm"]) for c in cnt if c["pid"] in both}
            sold = 0.0
            for r in _rows(con, """SELECT COALESCE(item_name,'') nm, SUM(quantity*size_oz) oz FROM toast_order_items WHERE location_id=? AND bucket='Beer' AND voided=0
                                   AND COALESCE(pour,'draft')='draft' AND size_oz IS NOT NULL AND business_date>? AND business_date<=? GROUP BY 1""", (lid, a, b)):
                if brand_key(r["nm"]) in keys:
                    sold += float(r["oz"] or 0)
            begin = sum(by_date[a][k] for k in both); end_ = sum(by_date[b][k] for k in both)
            depleted = begin + rcv - end_
            out["count_yield"] = {"from": a, "to": b, "begin_oz": _r(begin, 0), "received_oz": _r(rcv, 0), "end_oz": _r(end_, 0),
                                  "depleted_oz": _r(depleted, 0), "sold_oz": _r(sold, 0), "products": len(both),
                                  "dropped": len(set(by_date[a]) ^ set(by_date[b])),
                                  "yield": _r(sold / depleted, 4) if depleted > 0 else None}
    return out


# ------------------------------------------------------------------------------------------ channels

CHANNEL_RULES = [
    (r"door\s*dash", "DoorDash"), (r"uber", "Uber Eats"), (r"grub\s*hub", "Grubhub"),
    (r"toast delivery", "Toast Delivery"), (r"thanx", "Thanx ordering"), (r"online", "Online ordering"),
    (r"curbside|take\s*-?\s*out|to\s*-?\s*go|pick\s*-?\s*up", "Takeout"), (r"gift", "Gift cards"),
    (r"dine|bar|patio|table|taproom|no make", "On premise"),
]
THIRD_PARTY = {"DoorDash", "Uber Eats", "Grubhub", "Toast Delivery"}


def channel_of(dining_option: str | None) -> str:
    s = (dining_option or "").lower()
    for rx, name in CHANNEL_RULES:
        if re.search(rx, s):
            return name
    return "Other"


def channels(con, lid: str, w28: date, through: date) -> dict:
    """Sales by ordering channel, with what is left after the marketplace's commission.

    Read from the dining-option NAME, because that is how this Toast account distinguishes them ("Doordash -
    Delivery"). The commission rates come from config and default to unset: a delivery order at 25% commission and
    one at 15% are different businesses, and a made-up rate would produce a confident margin that means nothing.
    Until a rate is entered the view shows the sales and says the margin is unknown.
    """
    rates = ((settings().get("channels") or {}).get("commission") or {})
    prior0 = (w28 - timedelta(days=28)).isoformat()
    agg: dict[str, dict] = {}
    for r in _rows(con, """SELECT COALESCE(dining_option,'?') d, CASE WHEN business_date>=? THEN 1 ELSE 0 END cur,
                                  COUNT(*) n, SUM(net_sales) net, SUM(discounts) disc
                           FROM toast_orders WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=? GROUP BY 1,2""",
                   (w28.isoformat(), lid, prior0, through.isoformat())):
        o = agg.setdefault(channel_of(r["d"]), {"n": 0, "net": 0.0, "pn": 0, "pnet": 0.0, "disc": 0.0, "names": set()})
        if r["cur"]:
            o["n"] += r["n"]; o["net"] += float(r["net"] or 0); o["disc"] += float(r["disc"] or 0); o["names"].add(r["d"])
        else:
            o["pn"] += r["n"]; o["pnet"] += float(r["net"] or 0)
    total = sum(o["net"] for o in agg.values()) or 0.0
    rows = []
    for ch, o in agg.items():
        rate = rates.get(ch)
        rate = float(rate) if isinstance(rate, (int, float)) else None
        rows.append([ch, o["n"], _r(o["net"]), _r(o["net"] / o["n"]) if o["n"] else None, _r(o["pnet"]),
                     rate, _r(o["net"] * (1 - rate)) if rate is not None else None, ch in THIRD_PARTY,
                     sorted(o["names"])[:6]])
    rows.sort(key=lambda x: -(x[2] or 0))
    tp = sum(r[2] for r in rows if r[7])
    return {"rows": rows, "total": _r(total), "third_party_share": _r(tp / total, 4) if total else None,
            "rates_set": any(r[5] is not None for r in rows if r[7])}


# ------------------------------------------------------------------------------------------ loyalty

def loyalty(con, lid: str, w28: date, through: date) -> dict:
    a, b = w28.isoformat(), through.isoformat()
    net = (con.execute("SELECT COALESCE(SUM(net_sales),0), COUNT(*) FROM toast_orders WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=?", (lid, a, b)).fetchone())
    red = _rows(con, """
        SELECT COALESCE(d.name,'(unnamed)') name, SUM(d.amount) amt, COUNT(*) n, COUNT(DISTINCT d.check_guid) checks
        FROM toast_discounts d WHERE d.location_id=? AND d.business_date>=? AND d.business_date<=?
          AND (COALESCE(d.loyalty_vendor,'')!='' OR d.name LIKE '%Cheers%' OR d.name LIKE 'Thanx%')
        GROUP BY 1 ORDER BY amt DESC LIMIT 15""", (lid, a, b))
    red_net = (con.execute("""
        SELECT COALESCE(SUM(o.net_sales),0), COUNT(*) FROM toast_orders o WHERE o.location_id=? AND o.voided=0 AND o.business_date>=? AND o.business_date<=?
          AND o.order_guid IN (SELECT order_guid FROM toast_discounts d WHERE d.location_id=? AND d.business_date>=? AND d.business_date<=?
                               AND (COALESCE(d.loyalty_vendor,'')!='' OR d.name LIKE '%Cheers%' OR d.name LIKE 'Thanx%'))""",
        (lid, a, b, lid, a, b)).fetchone())
    out = {"net": _r(float(net[0])), "orders": net[1],
           "redemptions": [[r["name"], _r(r["amt"]), r["n"]] for r in red],
           "redeemed": {"amount": _r(sum(float(r["amt"] or 0) for r in red)), "orders": red_net[1], "net": _r(float(red_net[0])),
                        "share": _r(float(red_net[0]) / float(net[0]), 4) if net[0] else None},
           "members": None}
    # Identified members: only over days where identification was captured at all, or the share is diluted by
    # every earlier day on which the question was never asked.
    cap = con.execute("SELECT MIN(business_date), COUNT(DISTINCT business_date) FROM toast_loyalty WHERE location_id=? AND business_date>=? AND business_date<=?", (lid, a, b)).fetchone()
    if cap and cap[0]:
        m = con.execute("""SELECT COUNT(*), COALESCE(SUM(net),0), COUNT(DISTINCT member) FROM toast_loyalty WHERE location_id=? AND business_date>=? AND business_date<=?""", (lid, cap[0], b)).fetchone()
        base = con.execute("SELECT COALESCE(SUM(net_sales),0), COALESCE(SUM(checks_count),0) FROM toast_orders WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=?", (lid, cap[0], b)).fetchone()
        # Repeat is measured over the longest captured span available (up to 91 days), not the 28-day window: a
        # monthly regular is a repeat guest, and a four-week window would call most of them one-time visitors.
        span0 = max((through - timedelta(days=90)).isoformat(), (con.execute("SELECT MIN(business_date) FROM toast_loyalty WHERE location_id=?", (lid,)).fetchone() or [a])[0] or a)
        visits = _rows(con, """SELECT member, COUNT(DISTINCT business_date) v FROM toast_loyalty WHERE location_id=? AND business_date>=? AND business_date<=? AND member IS NOT NULL GROUP BY 1""", (lid, span0, b))
        rep = sum(1 for v in visits if v["v"] >= 2)
        ident_checks, ident_net = m[0], float(m[1])
        other_checks = max((base[1] or 0) - ident_checks, 0); other_net = float(base[0]) - ident_net
        out["members"] = {"since": cap[0], "days": cap[1], "checks": ident_checks, "net": _r(ident_net),
                          "share": _r(ident_net / float(base[0]), 4) if base[0] else None,
                          "members": m[2], "avg_check": _r(ident_net / ident_checks) if ident_checks else None,
                          "avg_check_other": _r(other_net / other_checks) if other_checks else None,
                          "repeat_span": [span0, b], "span_members": len(visits), "repeat_members": rep,
                          "repeat_rate": _r(rep / len(visits), 4) if visits else None,
                          "visits_per_member": _r(sum(v["v"] for v in visits) / len(visits), 2) if visits else None}
    return out


# ------------------------------------------------------------------------------------------ menu behaviour

DAYPARTS = [("Lunch", 0, 14), ("Afternoon", 14, 17), ("Dinner", 17, 21), ("Late", 21, 24)]


def menu_analysis(con, lid: str, w28: date, through: date, first_date: str | None) -> dict:
    a, b = w28.isoformat(), through.isoformat()
    p0 = (w28 - timedelta(days=28)).isoformat()
    # One pass per check: what was on it and when it started. Hours before the 4am closeout belong to "Late".
    checks = _rows(con, """
        SELECT check_guid, MAX(CASE WHEN bucket='Beer' THEN 1 ELSE 0 END) beer, MAX(CASE WHEN bucket='Food' THEN 1 ELSE 0 END) food,
               MAX(CASE WHEN bucket IN ('Liquor','Wine') THEN 1 ELSE 0 END) other_alc, MIN(hour_local) h, SUM(price) net, SUM(CASE WHEN bucket='Beer' THEN quantity ELSE 0 END) beers
        FROM toast_order_items WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=? AND bucket NOT IN ('Other','Retail')
        GROUP BY 1""", (lid, a, b))
    def part(h):
        if h is None:
            return None
        if h < 5:
            return "Late"
        for nm, lo, hi in DAYPARTS:
            if lo <= h < hi:
                return nm
        return None
    dp = {nm: {"n": 0, "net": 0.0, "beer": 0, "food": 0, "both": 0} for nm, _, _ in DAYPARTS}
    tot = {"n": 0, "net": 0.0, "beer": 0, "food": 0, "both": 0, "beers": 0.0}
    for c in checks:
        for o in (tot, dp.get(part(c["h"]))):
            if o is None:
                continue
            o["n"] += 1; o["net"] += float(c["net"] or 0)
            o["beer"] += c["beer"]; o["food"] += c["food"]; o["both"] += 1 if (c["beer"] and c["food"]) else 0
        tot["beers"] += float(c["beers"] or 0)
    def pack(o):
        # Counts, not rates: the portal adds taprooms together, and a rate cannot be added.
        return [o["n"], _r(o["net"]), o["beer"], o["food"], o["both"]]
    out = {"attach": {"checks": tot["n"], "beer_checks": tot["beer"], "food_checks": tot["food"], "both": tot["both"],
                      "food_on_beer": _r(tot["both"] / tot["beer"], 4) if tot["beer"] else None,
                      "beer_on_food": _r(tot["both"] / tot["food"], 4) if tot["food"] else None,
                      "beers_per_beer_check": _r(tot["beers"] / tot["beer"], 2) if tot["beer"] else None},
           "dayparts": [[nm] + pack(dp[nm]) for nm, _, _ in DAYPARTS]}

    # Velocity: this 28 days against the 28 before, per item. Wider than the headline top-40 and carrying the
    # comparison, because "what is selling" matters less week to week than "what stopped".
    vel = {}
    for r in _rows(con, """SELECT COALESCE(item_name,'(unnamed)') nm, bucket, CASE WHEN business_date>=? THEN 1 ELSE 0 END cur, SUM(quantity) q, SUM(price) net, COUNT(DISTINCT business_date) days
                           FROM toast_order_items WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=? AND bucket NOT IN ('Other')
                           GROUP BY 1,2,3""", (a, lid, p0, b)):
        # Beer is folded to the brand so a re-keyed button ("Easy Eddy" -> "Easy Eddy 16oz") is not reported as one
        # item collapsing and another taking off on the same day.
        o = vel.setdefault((brand_label(r["nm"]) if r["bucket"] == "Beer" else r["nm"], r["bucket"]), [0.0, 0.0, 0.0, 0.0, 0])
        if r["cur"]:
            o[0] += float(r["q"] or 0); o[1] += float(r["net"] or 0); o[4] = max(o[4], r["days"] or 0)
        else:
            o[2] += float(r["q"] or 0); o[3] += float(r["net"] or 0)
    rows = [[k[0], k[1], _r(v[0], 0), _r(v[1]), _r(v[2], 0), _r(v[3]), v[4]] for k, v in vel.items() if v[0] or v[2]]
    rows.sort(key=lambda x: -(x[3] or 0))
    out["velocity"] = rows[:80]
    # Fallers and risers by absolute dollars, so a slow seller halving does not outrank a staple slipping 15%.
    movers = sorted(rows, key=lambda x: (x[3] or 0) - (x[5] or 0))
    out["fallers"] = [m for m in movers[:8] if (m[3] or 0) - (m[5] or 0) < 0]
    out["risers"] = [m for m in reversed(movers[-8:]) if (m[3] or 0) - (m[5] or 0) > 0]

    # New beer releases: first sold here within the last 120 days, and at least 45 days after the data begins —
    # otherwise every item on the opening-day menu would look like a launch.
    out["new_beers"] = []
    if first_date:
        floor = (date.fromisoformat(first_date) + timedelta(days=45)).isoformat()
        recent = (through - timedelta(days=120)).isoformat()
        # A release is a new BEER, not a new button. Taprooms re-key their menus — Solon turned "Easy Eddy" into
        # "Easy Eddy 16oz" on 7 Sep 2026 — and by item name that reads as eight launches in one day. So first-sale
        # dates are taken per BRAND (sizes and pack words stripped), and a brand only counts as new if no spelling
        # of it was sold here before.
        items = _rows(con, """SELECT item_name nm, MIN(business_date) f, SUM(quantity) q, SUM(price) net FROM toast_order_items
                              WHERE location_id=? AND voided=0 AND bucket='Beer' AND item_name IS NOT NULL
                                AND business_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' GROUP BY 1""", (lid,))
        brands: dict[str, dict] = {}
        for it in items:
            k = brand_key(it["nm"])
            o = brands.setdefault(k, {"names": [], "f": it["f"], "q": 0.0, "net": 0.0, "top": (it["nm"], -1.0)})
            o["names"].append(it["nm"]); o["f"] = min(o["f"], it["f"]); o["q"] += float(it["q"] or 0); o["net"] += float(it["net"] or 0)
            if float(it["q"] or 0) > o["top"][1]:
                o["top"] = (it["nm"], float(it["q"] or 0))
        for k, o in brands.items():
            if not (o["f"] >= floor and o["f"] >= recent and o["q"] >= 20):
                continue
            try:
                f0 = date.fromisoformat(o["f"])
            except ValueError:
                continue
            marks = ",".join("?" * len(o["names"]))
            wk = []
            for i in range(4):
                s_, e_ = f0 + timedelta(days=7 * i), f0 + timedelta(days=7 * i + 6)
                if e_ > through:
                    wk.append(None); continue               # an unfinished week is not a week
                v = con.execute(f"SELECT COALESCE(SUM(quantity),0) FROM toast_order_items WHERE location_id=? AND voided=0 AND item_name IN ({marks}) AND business_date>=? AND business_date<=?",
                                (lid, *o["names"], s_.isoformat(), e_.isoformat())).fetchone()[0]
                wk.append(_r(float(v or 0), 0))
            out["new_beers"].append([brand_label(o["top"][0]), o["f"], wk, _r(o["q"], 0), _r(o["net"])])
        out["new_beers"].sort(key=lambda x: x[1], reverse=True)
    return out


def beer_mix(con, lid: str, w28: date, through: date) -> list:
    """Each beer's share of this taproom's beer units — the column the cross-location brand matrix is built from.
    Folded to the brand, so a taproom that keys "Easy Eddy 16oz" lines up with one that keys "Easy Eddy"."""
    acc: dict[str, float] = {}
    for r in _rows(con, """SELECT COALESCE(item_name,'(unnamed)') nm, COALESCE(SUM(quantity),0) q FROM toast_order_items WHERE location_id=? AND voided=0 AND bucket='Beer'
                           AND business_date>=? AND business_date<=? GROUP BY 1""", (lid, w28.isoformat(), through.isoformat())):
        k = brand_label(r["nm"])
        acc[k] = acc.get(k, 0.0) + float(r["q"] or 0)
    tot = sum(acc.values()) or 0.0
    rows = sorted(acc.items(), key=lambda kv: -kv[1])[:40]
    return [[k, _r(v, 0), _r(v / tot, 4) if tot else None] for k, v in rows]


# ------------------------------------------------------------------------------------------ counting discipline

def count_compliance(con, lid: str, through: date, weeks: int = 8) -> dict:
    """How reliably each inventory category is being counted — the discipline, not the dollars.

    Inventory variance is only as good as the counts under it, and the first look at real data found categories
    counted weekly sitting beside ones last counted in April. That is not a finding about food cost; it is a
    finding about counting, and it is the part a director controls. So it gets its own score: for each category
    this taproom has ever counted, how many of the counts it should have made in the last eight weeks it did make.
    Cadence per category comes from config (weekly unless stated), because a retail shelf counted monthly is not
    a lapse.
    """
    cad = ((settings().get("inventory") or {}).get("expected_count_days") or {})
    start = through - timedelta(days=weeks * 7 - 1)

    def _d(v):
        try:
            return date.fromisoformat(str(v)[:10])
        except (TypeError, ValueError):
            return None
    rows = [r for r in _rows(con, "SELECT bucket, count_date d, value v FROM me_inventory_counts WHERE location_id=? ORDER BY count_date", (lid,)) if _d(r["d"])]
    # A taproom cannot be behind on counts it could not have made. The window opens at the later of eight weeks
    # ago and this taproom's FIRST EVER count, so a location three weeks into MarginEdge is scored on three weeks.
    firsts = [_d(r["d"]) for r in rows if (r["v"] or 0) > 0]
    if firsts and min(firsts) > start:
        start = min(firsts)
    span = (through - start).days + 1
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["bucket"] or "Other", []).append(r)
    out_rows, got, want = [], 0.0, 0.0
    for b, cs in by.items():
        if not any((c["v"] or 0) > 0 for c in cs):
            continue                                  # never actually counted: not a category this taproom stocks
        try:
            days = max(1, int(cad.get(b, 7)))
        except (TypeError, ValueError):
            days = 7
        expected = max(1, round(span / days))
        real = [c for c in cs if (c["v"] or 0) > 0]
        in_win = {c["d"][:10] for c in real if _d(c["d"]) >= start}
        # one count per cadence period is what is asked for; several in a week do not earn extra credit
        periods = {(_d(d) - start).days // days for d in in_win}
        made = min(len(periods), expected)
        last = real[-1]["d"][:10] if real else None
        zero = bool(cs and not (cs[-1]["v"] or 0) and len(real) > 0)
        since = (through - _d(last)).days if last else None
        out_rows.append([b, last, since, made, expected, days, zero])
        got += made; want += expected
    out_rows.sort(key=lambda x: (x[3] / x[4] if x[4] else 0, -(x[2] or 0)))
    return {"score": _r(got / want, 4) if want else None, "weeks": max(1, round(span / 7)), "rows": out_rows,
            "since": start.isoformat()}


# ------------------------------------------------------------------------------------------ weather

def weather(con, lid: str, since: date, through: date) -> dict:
    rows = _rows(con, "SELECT date, tmax_f, tmin_f, precip_in, code, kind FROM weather_daily WHERE location_id=? AND date>=? ORDER BY date", (lid, since.isoformat()))
    return {"days": [[r["date"], _r(r["tmax_f"], 0), _r(r["precip_in"], 2), r["code"]] for r in rows if r["kind"] != "forecast" and r["date"] <= through.isoformat()],
            "forecast": [[r["date"], _r(r["tmax_f"], 0), _r(r["tmin_f"], 0), _r(r["precip_in"], 2), r["code"]] for r in rows if r["date"] > through.isoformat()][:8]}


# ------------------------------------------------------------------------------------------ market depletions

def market(con, lid: str, w28: date, through: date) -> dict:
    """Taproom brand velocity beside distributor depletions in the same market.

    The taproom is the one place Big Grove sees a beer meet a drinker without a distributor, a retailer and a
    shelf set in between — so if a brand is accelerating at the bar before it accelerates in the market around it,
    that is worth knowing, and if it never does, that is worth knowing too. This puts the two side by side by
    brand: taproom units this 28 days against the same 28 days last year, and market case-equivalents this year
    to date against the same span last year.

    It is a comparison, not yet a test. One year-to-date figure per brand cannot show which moved first; that needs
    the depletion history to accumulate, which it now does each time the file is rebuilt.
    """
    dep = _rows(con, "SELECT brand, premise, ce_ty, ce_ly, accounts, as_of FROM depletions WHERE location_id=?", (lid,))
    if not dep:
        return {}
    ly0, ly1 = (w28 - timedelta(days=364)).isoformat(), (through - timedelta(days=364)).isoformat()
    tap: dict[str, list] = {}
    for r in _rows(con, """SELECT COALESCE(item_name,'(unnamed)') nm, CASE WHEN business_date>=? THEN 1 ELSE 0 END cur, SUM(quantity) q FROM toast_order_items
                           WHERE location_id=? AND voided=0 AND bucket='Beer' AND ((business_date>=? AND business_date<=?) OR (business_date>=? AND business_date<=?))
                           GROUP BY 1,2""", (w28.isoformat(), lid, w28.isoformat(), through.isoformat(), ly0, ly1)):
        o = tap.setdefault(brand_key(r["nm"]), [r["nm"], 0.0, 0.0])
        o[1 if r["cur"] else 2] += float(r["q"] or 0)
    # The taproom must have traded through the WHOLE of last year's window, or the comparison is against a part-month.
    has_ly = bool(con.execute("SELECT 1 FROM toast_orders WHERE location_id=? AND business_date<=? LIMIT 1", (lid, ly0)).fetchone())
    mk: dict[str, dict] = {}
    for d in dep:
        o = mk.setdefault(brand_key(d["brand"]), {"name": d["brand"], "ty": 0.0, "ly": 0.0, "on_ty": 0.0, "on_ly": 0.0, "acc": 0})
        o["ty"] += d["ce_ty"] or 0; o["ly"] += d["ce_ly"] or 0; o["acc"] += d["accounts"] or 0      # ON and OFF outlets are disjoint
        if d["premise"] == "ON":
            o["on_ty"] += d["ce_ty"] or 0; o["on_ly"] += d["ce_ly"] or 0
    t_tot = sum(v[1] for v in tap.values()) or 0.0
    m_tot = sum(v["ty"] for v in mk.values()) or 0.0
    rows = []
    for k in set(tap) | set(mk):
        t, m = tap.get(k), mk.get(k)
        rows.append([(m or {}).get("name") or (t[0] if t else k),
                     _r(t[1], 0) if t else None, _r(t[1] / t_tot, 4) if (t and t_tot) else None,
                     _r((t[1] - t[2]) / t[2], 4) if (t and has_ly and t[2]) else None,
                     _r(m["ty"], 1) if m else None, _r(m["ty"] / m_tot, 4) if (m and m_tot) else None,
                     _r((m["ty"] - m["ly"]) / m["ly"], 4) if (m and m["ly"]) else None,
                     _r(m["on_ty"], 1) if m else None, (m or {}).get("acc")])
    rows.sort(key=lambda x: -((x[4] or 0) + (x[1] or 0) / 10.0))
    return {"as_of": dep[0]["as_of"], "rows": rows[:40], "taproom_has_ly": has_ly,
            "matched": sum(1 for r in rows if r[1] is not None and r[4] is not None)}


# ------------------------------------------------------------------------------------------ daily grain for the date picker

def range_detail(con, lid: str, since: date, through: date, item_days: int = 200) -> dict:
    """Daily-grain versions of the panels that used to be fixed at 28 or 56 days.

    The headline bundle pre-aggregates these to keep first paint small, which is why nobody could look at last
    month. The answer is not to bloat the bundle everyone downloads but to publish the daily grain in the
    per-location DETAIL bundle, which is fetched only when somebody actually moves the date range. Each table is
    [day index, dimension index, measures...] against shared lookup lists, which is about a fifth the size of
    repeating names and dates on every row.
    """
    a, b = since.isoformat(), through.isoformat()
    # Every calendar day, not just trading days: invoices are dated on days the taproom was shut.
    days = [(since + timedelta(days=i)).isoformat() for i in range((through - since).days + 1)]
    di = {d: i for i, d in enumerate(days)}
    out = {"days": days}

    def table(sql, args, dim_cols, measures):
        names, idx, rows = [], {}, []
        for r in _rows(con, sql, args):
            if r["d"] not in di:
                continue
            key = tuple(r[c] for c in dim_cols)
            if key not in idx:
                idx[key] = len(names); names.append(list(key) if len(key) > 1 else key[0])
            rows.append([di[r["d"]], idx[key]] + [_r(float(r[m] or 0)) if m != "n" else int(r[m] or 0) for m in measures])
        return {"names": names, "rows": rows}

    ia = max(a, (through - timedelta(days=item_days - 1)).isoformat())
    out["items_since"] = ia
    out["items"] = table("""SELECT business_date d, COALESCE(item_name,'(unnamed)') nm, bucket bk, SUM(quantity) q, SUM(price) net FROM toast_order_items
                            WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=? GROUP BY 1,2,3""", (lid, ia, b), ["nm", "bk"], ["q", "net"])
    out["dining"] = table("""SELECT business_date d, COALESCE(dining_option,'?') nm, SUM(net_sales) net, COUNT(*) n FROM toast_orders
                             WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=? GROUP BY 1,2""", (lid, a, b), ["nm"], ["net", "n"])
    out["revctr"] = table("""SELECT business_date d, COALESCE(revenue_center,'(unassigned)') nm, SUM(net_sales) net, COUNT(*) n FROM toast_orders
                             WHERE location_id=? AND voided=0 AND business_date>=? AND business_date<=? GROUP BY 1,2""", (lid, a, b), ["nm"], ["net", "n"])
    out["payments"] = table("""SELECT business_date d, COALESCE(type,'OTHER') nm, SUM(amount) amt, SUM(tip_amount) tips, COUNT(*) n FROM toast_payments
                               WHERE location_id=? AND business_date>=? AND business_date<=? GROUP BY 1,2""", (lid, a, b), ["nm"], ["amt", "tips", "n"])
    out["jobs"] = table("""SELECT business_date d, COALESCE(job_name,'Unknown') nm, SUM(COALESCE(regular_hours,0)+COALESCE(overtime_hours,0)) hrs, SUM(wages) cost, COUNT(*) n FROM toast_time_entries
                           WHERE location_id=? AND business_date>=? AND business_date<=? GROUP BY 1,2""", (lid, a, b), ["nm"], ["hrs", "cost", "n"])
    out["discounts"] = table("""SELECT business_date d, COALESCE(name,'(unnamed)') nm, COALESCE(loyalty_vendor,'') v, SUM(amount) amt, COUNT(*) n FROM toast_discounts
                                WHERE location_id=? AND business_date>=? AND business_date<=? GROUP BY 1,2,3""", (lid, a, b), ["nm", "v"], ["amt", "n"])
    out["vendors"] = table("""SELECT invoice_date d, COALESCE(vendor_name,'(no vendor)') nm, SUM(order_total) tot, COUNT(*) n FROM me_invoices
                              WHERE location_id=? AND invoice_date>=? AND invoice_date<=? GROUP BY 1,2""", (lid, a, b), ["nm"], ["tot", "n"])
    # hour-of-day sales: [day index, hour, net]
    out["hourly"] = [[di[r["d"]], r["h"], _r(float(r["net"] or 0))] for r in _rows(con, """
        SELECT business_date d, hour_local h, SUM(price) net FROM toast_order_items
        WHERE location_id=? AND voided=0 AND hour_local IS NOT NULL AND business_date>=? AND business_date<=? GROUP BY 1,2""", (lid, a, b)) if r["d"] in di]
    return out
