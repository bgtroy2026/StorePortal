"""Invariant checks against whatever warehouse is loaded.

These exist because mock data cannot be trusted to find real defects. Every check below corresponds to a bug
that actually shipped and was only caught by a human comparing the portal against the source system on
2026-09-15 — the category split that quietly lost service charges, the discounts counted on voided items, the
till-assignment job inflating labour hours, the inventory category with no ending count whose usage was
therefore "everything we started with plus everything we bought".

The point is that they run against the REAL warehouse, nightly, and say so out loud. A check that only ever
runs against generated data is a check on the generator.

Severity:
  FAIL  — the number is wrong and someone could act on it. Exits non-zero.
  WARN  — worth a human's attention, but may be legitimate (a genuinely uncounted category, say).

Each check reports the worst offenders rather than a bare count, because "17 days fail" is not actionable and
"solon 2026-09-05 is $41.66 out" is.

WHAT THESE CANNOT DO. An invariant compares the warehouse against itself, so it cannot see an error that is
consistent within the warehouse. The voided-discount bug is the case in point: it inflated discounts and gross
by the same amount, leaving gross = net + discounts perfectly satisfied, and every figure internally
consistent — it was only visible by opening Toast and reading a different number. Checks catch drift and
contradiction. Catching a shared wrong assumption still takes a person comparing against the source system.
"""
from __future__ import annotations

import sqlite3

from .transform import NON_LABOR_JOBS, connect
from .util import DB_PATH, log

MONEY_TOL = 0.05          # a nickel: below this is float noise, not a defect
HOURS_TOL = 0.05
SAMPLE = 5                # offenders to name per failing check


class Result:
    def __init__(self) -> None:
        self.fails: list[str] = []
        self.warns: list[str] = []

    def fail(self, msg: str) -> None:
        self.fails.append(msg); log.error("CHECK FAIL  %s", msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg); log.warning("CHECK WARN  %s", msg)

    def ok(self, msg: str) -> None:
        log.info("check ok    %s", msg)


def _rows(con, sql, args=()):
    con.row_factory = sqlite3.Row
    return con.execute(sql, args).fetchall()


def _report(r: Result, name: str, bad: list, fmt, severity: str = "fail") -> None:
    if not bad:
        r.ok(name)
        return
    head = ", ".join(fmt(b) for b in bad[:SAMPLE])
    more = f" (+{len(bad) - SAMPLE} more)" if len(bad) > SAMPLE else ""
    msg = f"{name}: {len(bad)} row(s) — {head}{more}"
    (r.fail if severity == "fail" else r.warn)(msg)


def run(db=None) -> int:
    con = connect(db or DB_PATH)
    r = Result()

    # 1. Gross must be net plus discounts. This is the identity that hid the voided-discount bug: counting a
    #    phantom discount inflated both sides, so net stayed right and nothing looked wrong.
    bad = _rows(con, """
        SELECT location_id, business_date, net_sales, gross_sales, discounts,
               gross_sales - (net_sales + discounts) gap
        FROM daily_summary WHERE net_sales > 0 AND ABS(gross_sales - (net_sales + discounts)) > ?
        ORDER BY ABS(gross_sales - (net_sales + discounts)) DESC""", (MONEY_TOL,))
    _report(r, "gross = net + discounts", bad, lambda b: f"{b['location_id']} {b['business_date']} off by {b['gap']:.2f}")

    # 2. The split now reconciles by construction (the unattributed slice is the residual), so the meaningful
    #    question is no longer "does it add up" but "how much could we not attribute". A day that is mostly
    #    unattributed means the category mapping has failed for it, and every mix, COGS ratio and benchmark
    #    built on the split is meaningless for that day — which is worth saying out loud rather than papering
    #    over with a slice that always makes the arithmetic work.
    bad = _rows(con, """
        SELECT location_id, business_date, net_sales, sales_unattr,
               ROUND(sales_unattr * 100.0 / net_sales, 1) pct
        FROM daily_summary
        WHERE net_sales > 100 AND sales_unattr * 100.0 / net_sales > 25
        ORDER BY sales_unattr * 100.0 / net_sales DESC""")
    _report(r, "no day is mostly unattributed sales", bad,
            lambda b: f"{b['location_id']} {b['business_date']} {b['pct']}% unattributed (${b['sales_unattr']:,.0f})",
            severity="warn")

    # 2b. The residual is clamped at zero, so a day where the categories EXCEED net sales would be silently
    #     truncated. That would mean sales counted twice somewhere, which is worth a failure, not a shrug.
    bad = _rows(con, """
        SELECT location_id, business_date, net_sales,
               (sales_food+sales_beer+sales_liquor+sales_wine+sales_nabev+sales_retail+sales_other) cats
        FROM daily_summary
        WHERE net_sales > 0
          AND (sales_food+sales_beer+sales_liquor+sales_wine+sales_nabev+sales_retail+sales_other) - net_sales > ?
        ORDER BY (sales_food+sales_beer+sales_liquor+sales_wine+sales_nabev+sales_retail+sales_other) - net_sales DESC""",
                 (MONEY_TOL,))
    _report(r, "no day's categories exceed its net sales", bad,
            lambda b: f"{b['location_id']} {b['business_date']} categories {b['cats']:.2f} > net {b['net_sales']:.2f}")

    # 3. No non-labour job may contribute hours. Labour cost stayed correct while hours ran 5% high, because
    #    the offending job carries no wage — so cost agreeing with Toast proved nothing about hours.
    if NON_LABOR_JOBS:
        q = ",".join("?" * len(NON_LABOR_JOBS))
        bad = _rows(con, f"""SELECT job_name, COUNT(*) n, ROUND(SUM(regular_hours+overtime_hours),1) hrs
                             FROM toast_time_entries WHERE job_name IN ({q}) GROUP BY 1""", tuple(NON_LABOR_JOBS))
        _report(r, "no non-labour jobs in time entries", bad,
                lambda b: f"{b['job_name']} {b['hrs']}h across {b['n']} entries")

    # 4. Labour cost should be within a rounding of hours x wage (overtime at 1.5x). Catches a wage or an
    #    overtime multiplier drifting from Toast's.
    # `wages` is rounded per time entry and then summed, so a day with forty entries can legitimately differ
    # from the recomputed total by a few cents. A nickel flagged forty days of pure rounding; a dollar still
    # catches a wrong wage or a broken overtime multiplier, which is what this is for.
    bad = _rows(con, """
        SELECT location_id, business_date, ROUND(SUM(wages),2) stored,
               ROUND(SUM(regular_hours*hourly_wage + overtime_hours*hourly_wage*1.5),2) recomputed
        FROM toast_time_entries GROUP BY 1,2
        HAVING ABS(stored - recomputed) > 1.0 ORDER BY ABS(stored - recomputed) DESC""")
    _report(r, "labour cost = hours x wage", bad,
            lambda b: f"{b['location_id']} {b['business_date']} {b['stored']:.2f} vs {b['recomputed']:.2f}")

    # 5. Nothing should be negative. Sales and hours going negative means a refund or a correction is being
    #    double-applied somewhere upstream.
    bad = _rows(con, """
        SELECT location_id, business_date, net_sales, labor_hours, guests
        FROM daily_summary WHERE net_sales < 0 OR labor_hours < 0 OR guests < 0
        ORDER BY net_sales""")
    _report(r, "no negative sales, hours or guests", bad,
            lambda b: f"{b['location_id']} {b['business_date']} net={b['net_sales']:.2f} hrs={b['labor_hours']:.1f}")

    # 6. A day with sales but no labour (or the reverse) is usually a business-date boundary that has slipped
    #    on one side only — the failure mode we specifically went looking for and did not find.
    bad = _rows(con, """
        SELECT location_id, business_date, net_sales, labor_hours FROM daily_summary
        WHERE (net_sales > 500 AND labor_hours = 0) OR (labor_hours > 20 AND net_sales = 0)
        ORDER BY business_date DESC""")
    _report(r, "sales and labour appear on the same days", bad,
            lambda b: f"{b['location_id']} {b['business_date']} net={b['net_sales']:.0f} hrs={b['labor_hours']:.1f}",
            severity="warn")

    # 7. An ending count of zero under a non-zero opening is a category left off the sheet, not a category that
    #    ran to nothing — and it yields a usage figure of "everything you had plus everything you bought",
    #    which lands at a believable cost percentage and is entirely fictional.
    bad = _rows(con, """
        WITH ranked AS (SELECT location_id, bucket, count_date, value,
                               ROW_NUMBER() OVER (PARTITION BY location_id, bucket ORDER BY count_date DESC) rn
                        FROM me_inventory_counts)
        SELECT a.location_id, a.bucket, a.count_date, b.value prev
        FROM ranked a JOIN ranked b ON b.location_id=a.location_id AND b.bucket=a.bucket AND b.rn=2
        WHERE a.rn=1 AND a.value=0 AND b.value>0 ORDER BY b.value DESC""")
    _report(r, "no inventory category counted to zero under a non-zero opening", bad,
            lambda b: f"{b['location_id']} {b['bucket']} (last counted {b['count_date']}, opened at {b['prev']:.0f})",
            severity="warn")

    # 8. Categories counted on wildly different dates cannot be read side by side, which is how a beer figure
    #    four months stale sat in a table headed with last week's date.
    bad = _rows(con, """
        WITH latest AS (SELECT location_id, bucket, MAX(count_date) d FROM me_inventory_counts GROUP BY 1,2),
             newest AS (SELECT location_id, MAX(d) nd FROM latest GROUP BY 1)
        SELECT l.location_id, l.bucket, l.d, n.nd,
               CAST(julianday(n.nd) - julianday(l.d) AS INT) days_behind
        FROM latest l JOIN newest n ON n.location_id=l.location_id
        WHERE julianday(n.nd) - julianday(l.d) > 21 ORDER BY days_behind DESC""")
    _report(r, "all inventory categories counted on a comparable date", bad,
            lambda b: f"{b['location_id']} {b['bucket']} {b['days_behind']}d behind the rest",
            severity="warn")

    # 9. The day's discount total must equal the individual discounts recorded for it. These are written by the
    #    same pass, so this catches one drifting from the other (a filter added to one and not the other) rather
    #    than both being wrong together — see the note in the module docstring about what invariants cannot do.
    bad = _rows(con, """
        SELECT d.location_id, d.business_date, d.discounts, COALESCE(SUM(t.amount),0) itemised,
               d.discounts - COALESCE(SUM(t.amount),0) gap
        FROM daily_summary d
        LEFT JOIN toast_discounts t ON t.location_id=d.location_id AND t.business_date=d.business_date
        WHERE d.discounts > 0
        GROUP BY d.location_id, d.business_date
        HAVING ABS(d.discounts - COALESCE(SUM(t.amount),0)) > ?
        ORDER BY ABS(d.discounts - COALESCE(SUM(t.amount),0)) DESC""", (MONEY_TOL,))
    # Days from before per-discount capture existed have a day total and no itemised rows. That is history, not
    # a defect being introduced now, so only recent days fail — otherwise the check is red forever and stops
    # being read, which is the failure mode that matters most for a check nobody is forced to look at.
    cutoff = con.execute("SELECT DATE(MAX(business_date), '-90 days') FROM daily_summary").fetchone()[0] or "0000-00-00"
    recent = [b for b in bad if b["business_date"] >= cutoff]
    older = [b for b in bad if b["business_date"] < cutoff]
    _report(r, "day's discounts match the discounts recorded for it (last 90 days)", recent,
            lambda b: f"{b['location_id']} {b['business_date']} {b['discounts']:.2f} vs {b['itemised']:.2f}")
    _report(r, "day's discounts match the discounts recorded for it (older than 90 days)", older,
            lambda b: f"{b['location_id']} {b['business_date']} {b['discounts']:.2f} vs {b['itemised']:.2f}",
            severity="warn")

    # 10. "Other" is the bucket for anything the category mapping did not recognise. A little is normal; most of
    #     a day's sales landing there means the mapping is not working for that location, and every mix chart,
    #     COGS ratio and benchmark built on the split is meaningless for it.
    bad = _rows(con, """
        SELECT location_id, COUNT(*) days, ROUND(AVG(sales_other * 100.0 / net_sales),1) pct
        FROM daily_summary WHERE net_sales > 100
        GROUP BY location_id HAVING pct > 25 ORDER BY pct DESC""")
    _report(r, "no location has most of its sales in the unmapped Other category", bad,
            lambda b: f"{b['location_id']} averages {b['pct']}% in Other across {b['days']} days",
            severity="warn")

    # 11. Cash entries that exist but carry no value. The aggregation keys on Toast's `type` strings, so a
    #     vocabulary we do not recognise leaves every total at zero while the entry COUNT still looks healthy —
    #     a loss-prevention view full of $0.00 that appears to be working. Report the actual types present so
    #     the mismatch is obvious rather than a mystery.
    n_cash = con.execute("SELECT COUNT(*) FROM toast_cash_entries").fetchone()[0]
    if n_cash:
        nonzero = con.execute("SELECT COUNT(*) FROM toast_cash_entries WHERE ABS(COALESCE(amount,0)) > 0.005").fetchone()[0]
        types = _rows(con, """SELECT COALESCE(type,'(null)') t, COUNT(*) n,
                                     ROUND(SUM(ABS(COALESCE(amount,0))),2) val
                              FROM toast_cash_entries GROUP BY 1 ORDER BY n DESC""")
        log.info("cash entry types present: %s",
                 "; ".join(f"{t['t']}={t['n']} (${t['val']:,.2f})" for t in types))
        if not nonzero:
            r.fail(f"cash entries carry no value: {n_cash} entries, every amount zero — "
                   f"types present: {', '.join(t['t'] for t in types)}")
        else:
            r.ok(f"cash entries carry values ({nonzero} of {n_cash} non-zero)")

        # Types the aggregation actually understands. Anything else contributes nothing to over/short.
        known = ("CLOSE_OUT_OVERAGE", "CLOSE_OUT_SHORTAGE", "PAY_OUT", "DRIVER_REIMBURSEMENT", "NO_SALE")
        unknown = [t for t in types if t["t"] not in known]
        _report(r, "cash entry types are all understood by the aggregation", unknown,
                lambda b: f"{b['t']} x{b['n']} (${b['val']:,.2f})", severity="warn")

    n_days = con.execute("SELECT COUNT(*) FROM daily_summary").fetchone()[0]
    locs = con.execute("SELECT COUNT(DISTINCT location_id) FROM daily_summary").fetchone()[0]
    log.info("checks complete over %d location-days across %d locations: %d failed, %d warned",
             n_days, locs, len(r.fails), len(r.warns))
    return 1 if r.fails else 0
