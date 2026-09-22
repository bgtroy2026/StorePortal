"""Transform: raw/ JSON (real or mock) -> state/warehouse.sqlite, then rebuild daily_summary.

Idempotent: every table is upserted on its natural key, so re-pulling a business day replaces it cleanly
(orders modified after close — tips, voids, refunds — are handled by re-pulling the incremental window).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import inputs
from .pours import PourParser
from .toast import CONFIG_RESOURCES
from .util import DB_PATH, ROOT, iter_raw, load_json, locations, log, settings, to_local

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


_CREATE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\)\s*;", re.S)


def _migrate(con) -> list[str]:
    """Add columns that schema.sql has gained since the persisted warehouse was written.

    The warehouse survives between nightly runs as an encrypted release asset, and CREATE TABLE IF NOT EXISTS
    silently leaves an older table shaped exactly as it was. So adding a column to schema.sql works perfectly
    on a fresh database and then fails on every real run with "table X has no column named Y" — which is a
    deploy-time break rather than a test-time one, and therefore worth handling once, here, rather than
    remembering to hand-write a migration each time.

    SQLite does the parsing: the new definition is built as a throwaway probe table and its columns compared
    against the live one, so this never has to understand column syntax itself.
    """
    added = []
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for name, body in _CREATE_RE.findall(SCHEMA):
        if name not in have:
            continue                                    # brand-new table: the schema script just created it
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({name})")}
        probe = f"__probe_{name}"
        con.execute(f"DROP TABLE IF EXISTS {probe}")
        try:
            con.execute(f"CREATE TABLE {probe} ({body})")
            want = [(r[1], r[2] or "") for r in con.execute(f"PRAGMA table_info({probe})")]
        except sqlite3.Error:
            continue                                    # unparseable for any reason: leave the table alone
        finally:
            con.execute(f"DROP TABLE IF EXISTS {probe}")
        for col, typ in want:
            if col not in cols:
                con.execute(f"ALTER TABLE {name} ADD COLUMN {col} {typ}")
                added.append(f"{name}.{col}")
    if added:
        con.commit()
        log.info("warehouse migration: added %s", ", ".join(added))
    return added


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _upsert(con, table: str, rows: list[dict]):
    if not rows:
        return 0
    cols = list(rows[0].keys())
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    con.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
    return len(rows)


# Job titles that record hours but are not labour. Kept as a name list because Toast guids differ per location.
NON_LABOR_JOBS = {"Bar Drawers"}


def _bd(v) -> str:
    """Toast businessDate int yyyymmdd -> ISO."""
    s = str(v)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


def _hour(ts: str | None, tz: str | None = None) -> int | None:
    d = to_local(ts, tz)                # Toast timestamps are UTC; the hour that matters is the restaurant's
    return d.hour if d else None


def fix_item_hours(con) -> dict:
    """One-time repair: `hour_local` was stored as the UTC hour. Shift history onto the restaurant's clock.

    The timestamp itself was never kept for items, only the hour, so history is repaired arithmetically: for each
    taproom and business date, add that date's UTC offset (it differs either side of a DST change). Guarded twice,
    because shifting hours that are ALREADY local would be worse than the bug: a meta flag makes it run once, and
    it only runs at all if the data looks like UTC — more trade recorded between midnight and 4am than between
    11am and 3pm, which is true of no taproom on earth in local time and of every one in UTC.
    """
    if con.execute("SELECT 1 FROM meta WHERE key='item_hours_local'").fetchone():
        return {"skipped": "already done"}
    late, lunch = con.execute("""SELECT COALESCE(SUM(CASE WHEN hour_local BETWEEN 0 AND 3 THEN 1 ELSE 0 END),0),
                                        COALESCE(SUM(CASE WHEN hour_local BETWEEN 11 AND 14 THEN 1 ELSE 0 END),0)
                                 FROM toast_order_items WHERE hour_local IS NOT NULL""").fetchone()
    out = {"late": late, "lunch": lunch, "shifted": 0}
    if late > lunch and late > 1000:
        from zoneinfo import ZoneInfo
        for loc in locations():
            tz = ZoneInfo(loc.get("timezone") or "America/Chicago")
            by_off: dict[int, list] = {}
            for (bd,) in con.execute("SELECT DISTINCT business_date FROM toast_order_items WHERE location_id=?", (loc["slug"],)):
                try:
                    y, m, d = (int(x) for x in bd.split("-"))
                    off = int(datetime(y, m, d, 12, tzinfo=tz).utcoffset().total_seconds() // 3600)
                except Exception:
                    continue
                by_off.setdefault(off, []).append(bd)
            for off, dates in by_off.items():
                for i in range(0, len(dates), 400):
                    chunk = dates[i:i + 400]
                    cur = con.execute(f"""UPDATE toast_order_items SET hour_local=(hour_local + 24 + ?) % 24
                                          WHERE location_id=? AND hour_local IS NOT NULL AND business_date IN ({','.join('?' * len(chunk))})""",
                                      (off, loc["slug"], *chunk))
                    out["shifted"] += cur.rowcount
        log.info("item hours: shifted %d rows from UTC onto the restaurant's clock (one-time)", out["shifted"])
    else:
        log.info("item hours: already on the restaurant's clock (midnight-4am rows %d vs 11am-3pm rows %d) — nothing shifted", late, lunch)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('item_hours_local', ?)", (datetime.utcnow().isoformat(timespec="seconds") + "Z",))
    con.commit()
    return out


class Buckets:
    def __init__(self, cfg: dict):
        self.toast = {k.lower(): v for k, v in cfg["toast_sales_category_to_bucket"].items()}
        self.me = {k.lower(): v for k, v in cfg["marginedge_category_type_to_bucket"].items()}

    def from_toast(self, name: str | None) -> str:
        if not name:
            return "Other"
        n = name.lower()
        if n in self.toast:
            return self.toast[n]
        for k, v in self.toast.items():
            if k in n:
                return v
        return "Other"

    def from_me(self, ctype: str | None, cname: str | None = None) -> str:
        n = (ctype or "").lower()
        if n in self.me:
            return self.me[n]
        return self.from_toast(cname) if cname else "Other"


# ---------------------------------------------------------------- Toast

def _disc_row(d: dict, o: dict, c: dict, slug: str, bd: str, scope: str) -> dict | None:
    """One applied discount. Toast puts the loyalty provider in loyaltyDetails, which is what lets a Thanx
    redemption be told apart from a manager comp without integrating Thanx."""
    g = d.get("guid") or d.get("discountGuid")
    if not g:
        return None
    ld = d.get("loyaltyDetails") or {}
    return {"discount_guid": str(g), "order_guid": o.get("guid"), "check_guid": c.get("guid"), "location_id": slug,
            "business_date": bd, "name": d.get("name") or (d.get("discount") or {}).get("name"),
            "discount_type": d.get("discountType") or d.get("processingState"), "scope": scope,
            "loyalty_vendor": ld.get("vendor") or ld.get("vendorId"),
            "amount": round(float(d.get("discountAmount") or 0), 2),
            "approver_guid": (d.get("approver") or {}).get("guid"),
            "reason": ((d.get("appliedDiscountReason") or {}).get("name") or None),
            "captured": 1}


def load_toast(con, bk: Buckets) -> dict:
    stats = {"orders": 0, "items": 0, "payments": 0, "time_entries": 0, "config": 0, "employees": 0, "shifts": 0, "cash_entries": 0, "deposits": 0}
    # menus first: item guid -> (name, group, sales category)
    item_cat: dict[tuple[str, str], tuple[str, str, str, float]] = {}
    for slug, ds, p, j in iter_raw("toast", dataset="menus"):
        rows = []
        for menu in j.get("menus", []):
            for grp in menu.get("menuGroups", []):
                for it in grp.get("menuItems", []):
                    sc = (it.get("salesCategory") or {}).get("name")
                    item_cat[(slug, it["guid"])] = (it.get("name"), grp.get("name"), sc, it.get("price"))
                    rows.append({"item_guid": it["guid"], "location_id": slug, "name": it.get("name"), "menu_group": grp.get("name"), "sales_category": sc, "price": it.get("price")})
        _upsert(con, "toast_menu_items", rows)
    # Guid -> name lookups. Orders reference dining options, revenue centres and sales categories by guid
    # only, so without these the portal shows "5c2cc414-baf5-..." where it should say "Dine In". Every lookup
    # is landed in toast_config for later use, and the two that rename columns we already store also update
    # rows already in the warehouse — so history heals on the first run that has the lookup rather than only
    # days pulled from here on.
    lookups: dict[str, dict[tuple[str, str], str]] = {}
    cfg_rows = []
    for res in CONFIG_RESOURCES:
        for slug, ds, p, j in iter_raw("toast", dataset=f"config-{res}"):
            for d in (j.get(res) or []):
                # Not every config resource calls its label `name` — cash drawers, for one, came through with
                # no name and left raw guids in the cash view, which is the same failure the dining options had.
                # Take the first label-ish field that is actually present rather than assuming one.
                guid = d.get("guid")
                name = (d.get("name") or d.get("displayName") or d.get("title")
                        or d.get("label") or d.get("behavior"))
                if not guid or not name:
                    continue
                lookups.setdefault(res, {})[(slug, guid)] = name
                extra = {k: v for k, v in d.items() if k not in ("guid", "name", "entityType")}
                cfg_rows.append({"location_id": slug, "resource": res, "guid": guid, "name": name,
                                 "extra": json.dumps(extra) if extra else None})
    stats["config"] = _upsert(con, "toast_config", cfg_rows)
    for res, col in (("diningOptions", "dining_option"), ("revenueCenters", "revenue_center")):
        for (slug, guid), name in lookups.get(res, {}).items():
            con.execute(f"UPDATE toast_orders SET {col}=? WHERE location_id=? AND {col}=?", (name, slug, guid))
    dining = lookups.get("diningOptions", {})
    revctr = lookups.get("revenueCenters", {})
    salescat = lookups.get("salesCategories", {})
    voidreason = lookups.get("voidReasons", {})

    emp_rows = []
    for slug, ds, p, j in iter_raw("toast", dataset="employees"):
        for e in j.get("employees", []):
            first, last = e.get("firstName") or "", e.get("lastName") or ""
            chosen = e.get("chosenName") or ""
            disp = (chosen or first) + ((" " + last[:1] + ".") if last else "")
            emp_rows.append({"employee_guid": e["guid"], "location_id": slug, "first_name": first, "last_name": last,
                             "chosen_name": chosen or None, "display_name": disp.strip() or None, "email": e.get("email"),
                             "external_id": e.get("externalEmployeeId"),
                             "deleted": 1 if e.get("deleted") else 0, "disabled": 1 if e.get("disabled") else 0,
                             "job_guids": json.dumps([(r or {}).get("guid") for r in (e.get("jobReferences") or [])])})
    stats["employees"] = _upsert(con, "toast_employees", emp_rows)
    jobs: dict[tuple[str, str], str] = {}
    for slug, ds, p, j in iter_raw("toast", dataset="jobs"):
        rows = [{"job_guid": x["guid"], "location_id": slug, "title": x.get("title"), "wage_frequency": x.get("wageFrequency"), "default_wage": x.get("defaultWage")} for x in j.get("jobs", [])]
        for r in rows:
            jobs[(slug, r["job_guid"])] = r["title"]
        _upsert(con, "toast_jobs", rows)

    pours = PourParser()
    tzs = {l["slug"]: l.get("timezone") or "America/Chicago" for l in locations()}
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    for slug, ds, p, j in iter_raw("toast", dataset="orders"):
        orders, items, pays, discs, loy = [], [], [], [], []
        # Remember the ask itself, orders or not, so an empty day is never fetched again (see toast_pull_days).
        pulled_day = _bd(j.get("businessDate") or p.stem)
        con.execute("INSERT OR REPLACE INTO toast_pull_days VALUES (?,?,?,?)", (slug, pulled_day, len(j.get("orders", [])), now))
        for o in j.get("orders", []):
            bd = _bd(o.get("businessDate"))
            net = tax = tips = disc = svc = refunds = 0.0
            checks = o.get("checks") or []
            # Rung-then-removed. Computed across every check including the voided ones, because that is the
            # whole point of the number, and kept in its own column so it can never leak into sales.
            voided_value = round(sum(float(c.get("amount") or 0) for c in checks), 2) if o.get("voided") else 0.0
            for c in checks:
                if c.get("voided") or c.get("deleted"):
                    continue
                net += float(c.get("amount") or 0); tax += float(c.get("taxAmount") or 0)
                for d in (c.get("appliedDiscounts") or []):
                    disc += float(d.get("discountAmount") or 0)
                    discs.append(_disc_row(d, o, c, slug, bd, "check"))
                svc += sum(float(s.get("chargeAmount") or 0) for s in (c.get("appliedServiceCharges") or []))
                # Loyalty identification, independent of whether anything was redeemed. The identifier is hashed
                # on the way in and the original is never stored.
                li = c.get("appliedLoyaltyInfo") or {}
                ident = li.get("loyaltyIdentifier") or li.get("maskedLoyaltyIdentifier")
                if ident and not o.get("voided"):
                    loy.append({"check_guid": c["guid"], "order_guid": o["guid"], "location_id": slug, "business_date": bd,
                                "vendor": li.get("vendor"), "member": hashlib.sha256(("bg-loyalty:" + str(ident)).encode()).hexdigest()[:16],
                                "net": round(float(c.get("amount") or 0), 2)})
                for s in c.get("selections") or []:
                    # An item rung, discounted, then voided keeps its appliedDiscounts. Toast excludes those
                    # from Sales discounts and we must too: counting them inflates discounts AND gross by the
                    # same amount (gross is net + discounts), which nets out — so the error hides behind a
                    # correct net-sales figure. Verified at Solon on 2026-09-05: $41.66 on both lines.
                    s_void = bool(s.get("voided") or s.get("deleted"))
                    if not s_void:
                        for d in (s.get("appliedDiscounts") or []):
                            disc += float(d.get("discountAmount") or 0)
                            discs.append(_disc_row(d, o, c, slug, bd, "item"))
                    meta = item_cat.get((slug, (s.get("item") or {}).get("guid")))
                    sc = ((s.get("salesCategory") or {}).get("name")
                          or salescat.get((slug, (s.get("salesCategory") or {}).get("guid")))
                          or (meta[2] if meta else None))
                    # Modifier names verbatim. Draft is rung as the brand alone and the pour size is a modifier,
                    # so this is the only place a pint can be told from a crowler.
                    mods = " | ".join(str(m.get("displayName") or "").strip() for m in (s.get("modifiers") or []) if m.get("displayName"))[:240]
                    bucket = bk.from_toast(sc)
                    size_oz, pour = None, None
                    if bucket == "Beer":
                        try:
                            size_oz, pour = pours.parse(s.get("displayName") or (meta[0] if meta else None), mods, sc)
                        except Exception:             # a pour size is never worth losing the day's sales over
                            size_oz, pour = None, None
                    items.append({"selection_guid": s["guid"], "order_guid": o["guid"], "check_guid": c["guid"], "location_id": slug, "business_date": bd,
                                  "item_guid": (s.get("item") or {}).get("guid"), "item_name": s.get("displayName") or (meta[0] if meta else None),
                                  "item_group_guid": (s.get("itemGroup") or {}).get("guid"), "sales_category": sc, "bucket": bucket,
                                  "quantity": float(s.get("quantity") or 0), "pre_discount_price": float(s.get("preDiscountPrice") or 0), "price": float(s.get("price") or 0),
                                  "tax": float(s.get("tax") or 0), "voided": 1 if (s.get("voided") or o.get("voided")) else 0, "hour_local": _hour(s.get("createdDate") or o.get("openedDate"), tzs.get(slug)),
                                  "modifiers": mods, "size_oz": size_oz, "pour": pour,
                                  "void_reason": (voidreason.get((slug, (s.get("voidReason") or {}).get("guid")))
                                                  or (s.get("voidReason") or {}).get("name")) if s_void else None})
                for pm in c.get("payments") or []:
                    rf = (pm.get("refund") or {})
                    ra = float(rf.get("refundAmount") or 0)
                    refunds += ra; tips += float(pm.get("tipAmount") or 0)
                    pays.append({"payment_guid": pm["guid"], "order_guid": o["guid"], "check_guid": c["guid"], "location_id": slug, "business_date": bd, "type": pm.get("type"),
                                 "card_type": pm.get("cardType"), "amount": float(pm.get("amount") or 0), "tip_amount": float(pm.get("tipAmount") or 0), "refund_amount": ra, "paid_at": pm.get("paidDate")})
            voided = 1 if o.get("voided") else 0
            orders.append({"order_guid": o["guid"], "location_id": slug, "business_date": bd, "opened_at": o.get("openedDate"), "closed_at": o.get("closedDate"), "modified_at": o.get("modifiedDate"),
                           "dining_option": dining.get((slug, (o.get("diningOption") or {}).get("guid"))) or (o.get("diningOption") or {}).get("behavior") or (o.get("diningOption") or {}).get("guid"), "revenue_center": revctr.get((slug, (o.get("revenueCenter") or {}).get("guid"))) or (o.get("revenueCenter") or {}).get("guid"),
                           "server_guid": (o.get("server") or {}).get("guid"), "guests": int(o.get("numberOfGuests") or 0), "voided": voided, "checks_count": len(checks),
                           "net_sales": 0 if voided else round(net, 2), "tax": 0 if voided else round(tax, 2), "tips": round(tips, 2),
                           # Zeroed on a void for the same reason as net and gross — otherwise a voided order's
                           # discounts survive into the daily totals with no sales to sit against.
                           "discounts": 0 if voided else round(disc, 2), "service_charges": 0 if voided else round(svc, 2),
                           "gross_sales": 0 if voided else round(net + disc, 2), "voided_value": voided_value, "refunds": round(refunds, 2), "source_hash": None,
                           "source": o.get("source")})
        # replace the whole business day for this location so deleted orders disappear
        if orders:
            con.execute("DELETE FROM toast_order_items WHERE location_id=? AND business_date=?", (slug, orders[0]["business_date"]))
            con.execute("DELETE FROM toast_payments WHERE location_id=? AND business_date=?", (slug, orders[0]["business_date"]))
            con.execute("DELETE FROM toast_orders WHERE location_id=? AND business_date=?", (slug, orders[0]["business_date"]))
            con.execute("DELETE FROM toast_discounts WHERE location_id=? AND business_date=?", (slug, orders[0]["business_date"]))
            con.execute("DELETE FROM toast_loyalty WHERE location_id=? AND business_date=?", (slug, orders[0]["business_date"]))
        stats["orders"] += _upsert(con, "toast_orders", orders)
        stats["items"] += _upsert(con, "toast_order_items", items)
        stats["payments"] += _upsert(con, "toast_payments", pays)
        stats["discounts"] = stats.get("discounts", 0) + _upsert(con, "toast_discounts", [d for d in discs if d])
        stats["loyalty"] = stats.get("loyalty", 0) + _upsert(con, "toast_loyalty", loy)

    # Scheduled shifts. /labor/v1/shifts carries no businessDate (unlike time entries), so it is derived the
    # way Toast defines a business day: anything before the restaurant's closeout hour belongs to the night
    # before. Without that, a shift scheduled 10pm-2am would be split across two business dates and the
    # schedule would never line up with the hours actually worked.
    closeout: dict[str, int] = {}
    for slug, ds, p, j in iter_raw("toast", dataset="restaurant"):
        try:
            closeout[slug] = int(((j.get("general") or {}).get("closeoutHour")) or 4)
        except Exception:
            closeout[slug] = 4
    sh_rows, gone, seen, windows = [], [], {}, {}
    for slug, ds, p, j in iter_raw("toast", dataset="shifts"):
        co = closeout.get(slug, 4)
        w = j.get("window") or []
        if len(w) == 2:
            windows[slug] = (min(w[0], windows.get(slug, (w[0], w[1]))[0]), max(w[1], windows.get(slug, (w[0], w[1]))[1]))
        for x in j.get("shifts", []):
            if x.get("guid"):
                seen.setdefault(slug, set()).add(x["guid"])
            if x.get("deleted"):
                # The schedule is now pulled a week ahead, so a shift can be stored and THEN deleted by the
                # manager. It has to be marked, not skipped, or it would sit in the warehouse as hours forever.
                if x.get("guid"):
                    gone.append((x["guid"], slug))
                continue
            a, b = x.get("inDate"), x.get("outDate")
            if not a:
                continue
            try:
                start = to_local(a, tzs.get(slug))      # the closeout hour is a LOCAL hour, so the start must be too
                end_ = to_local(b, tzs.get(slug)) if b else None
                bd = (start - timedelta(days=1)).date().isoformat() if start.hour < co else start.date().isoformat()
                hrs = ((end_ - start).total_seconds() / 3600.0) if end_ else 0.0
            except Exception:
                continue
            jg = (x.get("jobReference") or {}).get("guid")
            sh_rows.append({"shift_guid": x["guid"], "location_id": slug, "business_date": bd,
                            "employee_guid": (x.get("employeeReference") or {}).get("guid"), "job_guid": jg,
                            "job_name": jobs.get((slug, jg)), "in_at": a, "out_at": b,
                            "hours": round(hrs, 2) if 0 < hrs <= 24 else 0.0, "deleted": 0})
    stats["shifts"] = _upsert(con, "toast_shifts", sh_rows)
    try:
        con.executemany("UPDATE toast_shifts SET deleted=1 WHERE shift_guid=? AND location_id=?", gone)
        # A shift Toast no longer returns at all was removed outright. Only strictly inside the pulled window:
        # at its edges a shift can belong to a business date the request did not cover.
        n_gone = 0
        for slug, (w0, w1) in windows.items():
            have = [r[0] for r in con.execute("SELECT shift_guid FROM toast_shifts WHERE location_id=? AND deleted=0 AND business_date>? AND business_date<?", (slug, w0, w1))]
            missing = [(g, slug) for g in have if g not in seen.get(slug, set())]
            con.executemany("UPDATE toast_shifts SET deleted=1 WHERE shift_guid=? AND location_id=?", missing)
            n_gone += len(missing)
        if gone or n_gone:
            log.info("shifts: %d marked deleted by Toast, %d no longer returned", len(gone), n_gone)
    except Exception as e:
        log.error("transform: deleted-shift reconciliation skipped (%s: %s)", type(e).__name__, e)

    cash_rows, dep_rows = [], []
    for slug, ds, p, j in iter_raw("toast", dataset="cash"):
        bd = j.get("businessDate") or ""
        for e in j.get("entries", []):
            if not e.get("guid"):
                continue
            cash_rows.append({"entry_guid": e["guid"], "location_id": slug, "business_date": bd,
                              "type": e.get("type"), "amount": float(e.get("amount") or 0), "reason": e.get("reason"),
                              "payout_reason": (e.get("payoutReason") or {}).get("guid"),
                              "no_sale_reason": (e.get("noSaleReason") or {}).get("guid"),
                              "employee_guid": (e.get("employee1") or {}).get("guid"),
                              "drawer_guid": (e.get("cashDrawer") or {}).get("guid"),
                              "undoes": e.get("undoes"), "entry_at": e.get("date")})
    stats["cash_entries"] = _upsert(con, "toast_cash_entries", cash_rows)
    for slug, ds, p, j in iter_raw("toast", dataset="deposits"):
        bd = j.get("businessDate") or ""
        for e in j.get("deposits", []):
            if not e.get("guid"):
                continue
            dep_rows.append({"deposit_guid": e["guid"], "location_id": slug, "business_date": bd,
                             "amount": float(e.get("amount") or 0),
                             "employee_guid": (e.get("employee") or {}).get("guid"),
                             "undoes": e.get("undoes"), "deposit_at": e.get("date")})
    stats["deposits"] = _upsert(con, "toast_deposits", dep_rows)

    # Out-of-stock snapshot. Isolated: a surprise in this payload is never worth the orders loaded above.
    try:
        st_rows, st_days = [], []
        for slug, ds, p, j in iter_raw("toast", dataset="stock"):
            snap = str(j.get("snap_date") or "")[:10]
            if not snap:
                continue
            n_out = 0
            for x in (j.get("items") or []):
                g = x.get("guid") or x.get("multiLocationId")
                if not g:
                    continue
                meta = item_cat.get((slug, g))
                status = str(x.get("status") or "")
                n_out += 1 if status == "OUT_OF_STOCK" else 0
                q = x.get("quantity")
                st_rows.append({"location_id": slug, "snap_date": snap, "item_guid": str(g), "name": meta[0] if meta else None,
                                "status": status, "quantity": float(q) if isinstance(q, (int, float)) else None})
            st_days.append({"location_id": slug, "snap_date": snap, "n_out": n_out})
            con.execute("DELETE FROM toast_stock WHERE location_id=? AND snap_date=?", (slug, snap))
        stats["stock"] = _upsert(con, "toast_stock", st_rows)
        _upsert(con, "toast_stock_days", st_days)
    except Exception as e:
        log.error("transform: stock snapshot skipped (%s: %s)", type(e).__name__, e)

    for slug, ds, p, j in iter_raw("toast", dataset="timeEntries"):
        rows = []
        for t in j.get("timeEntries", []):
            if t.get("deleted"):
                continue
            # Some job titles are not labour at all — "Bar Drawers" is a till assignment, carrying hours at a
            # zero wage. Toast's own labour reporting excludes it; we did not, which left labour COST correct
            # (no wage attached) while hours ran ~5% high and sales-per-labour-hour ~5% low. Verified at Solon
            # on 2026-09-05: 265.0 h against Toast's 252.7.
            if (jobs.get((slug, (t.get("jobReference") or {}).get("guid"))) or "") in NON_LABOR_JOBS:
                continue
            reg, ot, wage = float(t.get("regularHours") or 0), float(t.get("overtimeHours") or 0), float(t.get("hourlyWage") or 0)
            bd = _bd(t.get("businessDate")) if t.get("businessDate") else (t.get("inDate") or "")[:10]
            jg = (t.get("jobReference") or {}).get("guid")
            rows.append({"entry_guid": t["guid"], "location_id": slug, "business_date": bd, "employee_guid": (t.get("employeeReference") or {}).get("guid"), "job_guid": jg,
                         "job_name": jobs.get((slug, jg)), "in_at": t.get("inDate"), "out_at": t.get("outDate"), "regular_hours": reg, "overtime_hours": ot, "hourly_wage": wage,
                         "wages": round(reg * wage + ot * wage * 1.5, 2), "declared_cash_tips": float(t.get("declaredCashTips") or 0), "non_cash_tips": float(t.get("nonCashTips") or 0)})
        stats["time_entries"] += _upsert(con, "toast_time_entries", rows)

    # The filter above only keeps NEW non-labour entries out. Entries loaded before this rule existed are
    # already in the warehouse and would keep inflating labour hours for every historical day, which no
    # re-pull would reach — the pull only refreshes a recent window. Clear them out here, every run, so the
    # rule applies to the whole history rather than only to days pulled after it shipped.
    if NON_LABOR_JOBS:
        q = ",".join("?" * len(NON_LABOR_JOBS))
        n = con.execute(f"DELETE FROM toast_time_entries WHERE job_name IN ({q})", tuple(NON_LABOR_JOBS)).rowcount
        if n:
            log.info("time entries: removed %d rows for non-labour jobs (%s)", n, ", ".join(sorted(NON_LABOR_JOBS)))
    return stats


# ---------------------------------------------------------------- MarginEdge

def load_marginedge(con, bk: Buckets) -> dict:
    stats = {"categories": 0, "vendors": 0, "products": 0, "invoices": 0, "lines": 0}
    cat_bucket: dict[tuple[str, str], str] = {}
    for slug, ds, p, j in iter_raw("marginedge", dataset="categories"):
        rows = []
        for c in j.get("categories", []):
            b = bk.from_me(c.get("categoryType"), c.get("categoryName"))
            cat_bucket[(slug, str(c["categoryId"]))] = b
            rows.append({"category_id": str(c["categoryId"]), "location_id": slug, "name": c.get("categoryName"), "category_type": c.get("categoryType"), "accounting_code": str(c.get("accountingCode") or ""), "bucket": b})
        stats["categories"] += _upsert(con, "me_categories", rows)
    for slug, ds, p, j in iter_raw("marginedge", dataset="vendors"):
        stats["vendors"] += _upsert(con, "me_vendors", [{"vendor_id": str(v["vendorId"]), "location_id": slug, "name": v.get("vendorName"), "central_vendor_id": v.get("centralVendorId")} for v in j.get("vendors", [])])
    prod_cat: dict[tuple[str, str], str] = {}
    for slug, ds, p, j in iter_raw("marginedge", dataset="products"):
        rows = []
        for pr in j.get("products", []):
            cats = pr.get("categories") or []
            primary = str(max(cats, key=lambda c: c.get("percentAllocation", 0))["categoryId"]) if cats else None
            prod_cat[(slug, str(pr.get("companyConceptProductId")))] = primary
            rows.append({"product_id": str(pr.get("companyConceptProductId")), "location_id": slug, "name": pr.get("productName"), "central_product_id": pr.get("centralProductId"),
                         "latest_price": pr.get("latestPrice"), "report_unit": pr.get("reportByUnit"), "tax_exempt": 1 if pr.get("taxExempt") else 0, "categories_json": json.dumps(cats), "primary_category_id": primary})
        stats["products"] += _upsert(con, "me_products", rows)
    for slug, ds, p, j in iter_raw("marginedge", dataset="orderDetail"):
        o = j
        oid = str(o.get("orderId"))
        inv = {"order_id": oid, "location_id": slug, "vendor_id": str(o.get("vendorId")), "vendor_name": o.get("vendorName"), "invoice_number": o.get("invoiceNumber"),
               "invoice_date": o.get("invoiceDate"), "created_date": o.get("createdDate"), "order_total": float(o.get("orderTotal") or 0), "tax": float(o.get("tax") or 0),
               "delivery_charges": float(o.get("deliveryCharges") or 0), "other_charges": float(o.get("otherCharges") or 0), "credit_amount": float(o.get("creditAmount") or 0),
               "is_credit": 1 if o.get("isCredit") else 0, "status": o.get("status"), "payment_account": o.get("paymentAccount")}
        lines = []
        for n, l in enumerate(o.get("lineItems") or []):
            cid = str(l.get("categoryId") or prod_cat.get((slug, str(l.get("companyConceptProductId")))) or "")
            lines.append({"order_id": oid, "location_id": slug, "line_no": n, "invoice_date": o.get("invoiceDate"), "vendor_item_code": l.get("vendorItemCode"), "vendor_item_name": l.get("vendorItemName"),
                          "product_id": str(l.get("companyConceptProductId") or ""), "category_id": cid, "packaging_id": l.get("packagingId"), "bucket": cat_bucket.get((slug, cid), "Other"),
                          "quantity": float(l.get("quantity") or 0), "unit_price": float(l.get("unitPrice") or 0), "line_price": float(l.get("linePrice") or 0)})
        con.execute("DELETE FROM me_invoice_lines WHERE order_id=? AND location_id=?", (oid, slug))
        stats["invoices"] += _upsert(con, "me_invoices", [inv])
        stats["lines"] += _upsert(con, "me_invoice_lines", lines)
    # header-only orders (list endpoint) that have no detail yet still count toward purchases
    for slug, ds, p, j in iter_raw("marginedge", dataset="orders"):
        rows = []
        for o in j.get("orders", []):
            oid = str(o.get("orderId"))
            if con.execute("SELECT 1 FROM me_invoices WHERE order_id=? AND location_id=?", (oid, slug)).fetchone():
                continue
            rows.append({"order_id": oid, "location_id": slug, "vendor_id": str(o.get("vendorId")), "vendor_name": o.get("vendorName"), "invoice_number": o.get("invoiceNumber"), "invoice_date": o.get("invoiceDate"),
                         "created_date": o.get("createdDate"), "order_total": float(o.get("orderTotal") or 0), "tax": 0, "delivery_charges": 0, "other_charges": 0, "credit_amount": 0, "is_credit": 0,
                         "status": o.get("status"), "payment_account": o.get("paymentAccount")})
        stats["invoices"] += _upsert(con, "me_invoices", rows)
    stats.update(load_me_reports(con, bk, cat_bucket))
    stats.update(load_me_inventories(con, bk, cat_bucket, prod_cat))
    return stats


def load_me_reports(con, bk: Buckets, cat_bucket: dict) -> dict:
    """GET /sales/report and /profitAndLoss/report pulled one business day at a time."""
    n_sales = n_pnl = 0
    for slug, ds, p, j in iter_raw("marginedge", dataset="salesReport"):
        bd = p.stem
        rows = []
        for rep in j.get("salesReports") or []:
            for c in rep.get("categories") or []:
                cid = str(c.get("id"))
                rows.append({"location_id": slug, "business_date": bd, "category_id": cid, "category_name": c.get("name"),
                             "bucket": cat_bucket.get((slug, cid)) or bk.from_toast(c.get("name")), "total": float(c.get("total") or 0)})
        con.execute("DELETE FROM me_sales_daily WHERE location_id=? AND business_date=?", (slug, bd))
        n_sales += _upsert(con, "me_sales_daily", rows)
    for slug, ds, p, j in iter_raw("marginedge", dataset="pnl"):
        bd = p.stem
        rows, summ = [], None
        for rep in j.get("profitAndLossReports") or []:
            sm = rep.get("summary") or {}
            summ = {"location_id": slug, "business_date": bd, "gross_profit": sm.get("grossProfit"), "prime_cost": sm.get("primeCostTotal"), "controllable_profit": sm.get("controllableProfit")}
            for section in ("income", "cogs", "labor", "expenses"):
                sec = rep.get(section) or {}
                summ[f"{section}_total"] = float(sec.get("total") or 0)
                rows.append({"location_id": slug, "business_date": bd, "section": section, "category_id": "", "category_name": None, "item_name": "", "total": float(sec.get("total") or 0), "pct_of_sales": sec.get("totalPercentOfSales"), "bucket": None})
                for c in sec.get("categories") or []:
                    cid = str(c.get("id") or c.get("name"))
                    b = cat_bucket.get((slug, cid)) or bk.from_me(None, c.get("name"))
                    rows.append({"location_id": slug, "business_date": bd, "section": section, "category_id": cid, "category_name": c.get("name"), "item_name": "", "total": float(c.get("total") or 0), "pct_of_sales": c.get("percentOfSales"), "bucket": b})
                    for it in c.get("items") or []:
                        rows.append({"location_id": slug, "business_date": bd, "section": section, "category_id": cid, "category_name": c.get("name"), "item_name": it.get("name") or "?", "total": float(it.get("total") or 0), "pct_of_sales": it.get("percentOfSales"), "bucket": b})
                for it in sec.get("items") or []:  # uncategorized lines
                    rows.append({"location_id": slug, "business_date": bd, "section": section, "category_id": "", "category_name": None, "item_name": it.get("name") or "?", "total": float(it.get("total") or 0), "pct_of_sales": it.get("percentOfSales"), "bucket": bk.from_me(None, it.get("name"))})
        con.execute("DELETE FROM me_pnl_daily WHERE location_id=? AND business_date=?", (slug, bd))
        n_pnl += _upsert(con, "me_pnl_daily", rows)
        if summ:
            _upsert(con, "me_pnl_summary", [summ])
    return {"sales_days": n_sales, "pnl_rows": n_pnl}


def load_me_inventories(con, bk: Buckets, cat_bucket: dict, prod_cat: dict) -> dict:
    n_inv = n_items = 0
    for slug, ds, p, j in iter_raw("marginedge", dataset="inventories"):
        rows = [{"inventory_id": str(i["inventoryId"]), "location_id": slug, "countsheet_id": str(i.get("countsheetId") or ""), "countsheet_name": i.get("countsheetName"), "inventory_date": (i.get("inventoryDate") or "")[:10],
                 "status": i.get("status"), "total_value": i.get("totalValue"), "closed_date": i.get("closedDate"), "saved_date": i.get("savedDate"), "origin": i.get("origin")} for i in j.get("inventories") or []]
        n_inv += _upsert(con, "me_inventories", rows)
    for slug, ds, p, j in iter_raw("marginedge", dataset="inventoryDetail"):
        iid = str(j.get("inventoryId") or p.stem)
        _upsert(con, "me_inventories", [{"inventory_id": iid, "location_id": slug, "countsheet_id": str(j.get("countsheetId") or ""), "countsheet_name": j.get("countsheetName"), "inventory_date": (j.get("inventoryDate") or "")[:10],
                                         "status": j.get("status"), "total_value": j.get("totalValue"), "closed_date": j.get("closedDate"), "saved_date": j.get("savedDate"), "origin": j.get("origin")}])
        rows = []
        for sec in j.get("sections") or []:
            for it in sec.get("items") or []:
                pid = str(it.get("companyConceptProductId") or it.get("productId") or "")
                cid = prod_cat.get((slug, pid))
                rows.append({"inventory_id": iid, "location_id": slug, "item_id": str(it.get("itemId") or f"{sec.get('sectionId')}:{it.get('position')}"), "section_name": sec.get("name"), "product_id": pid,
                             "product_name": it.get("productName"), "central_product_id": it.get("centralProductId"), "quantity": it.get("quantity"), "price": it.get("price"), "value": float(it.get("value") or 0),
                             "unit": it.get("unit"), "unit_size": it.get("unitSize"), "bucket": cat_bucket.get((slug, str(cid)), "Other") if cid else "Other"})
        con.execute("DELETE FROM me_inventory_items WHERE inventory_id=? AND location_id=?", (iid, slug))
        n_items += _upsert(con, "me_inventory_items", rows)
    # roll counted items up to bucket values per inventory date (api rows win over csv rows for the same date)
    con.execute("DELETE FROM me_inventory_counts WHERE source='api'")
    con.execute("""
      INSERT OR REPLACE INTO me_inventory_counts (location_id, count_date, bucket, value, source)
      SELECT i.location_id, i.inventory_date, x.bucket, SUM(x.value), 'api'
      FROM me_inventory_items x JOIN me_inventories i ON i.inventory_id=x.inventory_id AND i.location_id=x.location_id
      WHERE i.status IS NULL OR UPPER(i.status) NOT IN ('DELETED','IN_PROGRESS','DRAFT')
      GROUP BY 1,2,3""")
    return {"inventories": n_inv, "inventory_items": n_items}


# ---------------------------------------------------------------- Tripleseat

def _num(v):
    try:
        return float(str(v).replace("$", "").replace(",", "")) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _ts_mapping() -> tuple[dict, dict]:
    """Two maps from config/locations.json: Tripleseat location id -> our slug, and Tripleseat ROOM id -> our
    slug for taprooms that live as a room of another location (Solon is room 232405 of Iowa City). A room
    override wins over the location, so an Iowa City event in the Solon room is Solon's."""
    by_loc, by_room = {}, {}
    # A taproom that lives as a room claims the ROOM; the location itself belongs to the taproom that has no
    # room override (Iowa City), whatever order config lists them in. It falls back to the location only if
    # nobody else claims it.
    ours = sorted(locations(), key=lambda l: bool(l.get("tripleseat_room_ids")))
    for l in ours:
        tid = str(l.get("tripleseat_location_id") or "").strip()
        rooms = [str(r) for r in (l.get("tripleseat_room_ids") or [])]
        for rid in rooms:
            by_room[rid] = l["slug"]
        if tid and (not rooms or tid not in by_loc):
            by_loc.setdefault(tid, l["slug"])
    return by_loc, by_room


def _ts_slug(ts_location_id, room_ids, by_loc: dict, by_room: dict) -> str | None:
    for rid in room_ids or []:
        if str(rid) in by_room:
            return by_room[str(rid)]
    return by_loc.get(str(ts_location_id or ""))


def _ts_ids(v) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [str(x.get("id") if isinstance(x, dict) else x) for x in v if x not in (None, "")]
    return [x.strip() for x in str(v).split(",") if x.strip()]


def _ts_event_row(e: dict, slug: str, by_room_name: dict, type_names: dict, origin: str, seen_at: str | None) -> dict:
    """One Tripleseat Event object -> a ts_events row.

    The webhook delivers the same object the API would, plus a few things the API search does not: `rooms` as
    objects with names, `status_changes` (when it went definite), `selected_lead_sources`. Read 2026-09-21 off
    the first real delivery (CHANGE_EVENT_GUEST_COUNTS on event 61680687): status is upper-case ("DEFINITE"),
    money comes as strings ("1225.5"), created/updated as "7/27/2026 11:08 PM", event_type_id is null."""
    rids = _ts_ids(e.get("room_ids") if e.get("room_ids") is not None else e.get("rooms"))
    names = {}
    for r in e.get("rooms") or []:
        if isinstance(r, dict) and r.get("id") is not None and r.get("name"):
            names[str(r["id"])] = str(r["name"]).strip()
    et = e.get("event_type_id")
    if et in (None, "") and isinstance(e.get("event_type"), dict):
        et = e["event_type"].get("id")
    et = str(et or "")
    src = None
    for x in e.get("selected_lead_sources") or []:
        if isinstance(x, dict):
            src = x.get("lead_source_name") or x.get("name") or src
        elif x:
            src = str(x)
        if src:
            break
    definite_at = None
    for ch in e.get("status_changes") or []:                # newest first as delivered; keep the latest DEFINITE
        if isinstance(ch, dict) and str(ch.get("status") or "").upper() == "DEFINITE":
            definite_at = _ts_datetime(ch.get("created_at"))
            break
    return {"event_id": str(e.get("id")), "location_id": slug, "ts_location_id": str(_ts_loc_id(e) or ""),
            "booking_id": str(e.get("booking_id") or ""), "name": e.get("name"), "status": e.get("status"),
            "event_type": et, "event_style": e.get("event_style"),
            "event_date": (e.get("event_date_iso8601") or _ts_date(e.get("event_date")) or "")[:10],
            "start_at": e.get("event_start_iso8601") or e.get("event_start"),
            "end_at": e.get("event_end_iso8601") or e.get("event_end"),
            "guest_count": int(e.get("guest_count") or 0), "guaranteed_guest_count": int(e.get("guaranteed_guest_count") or 0),
            "fb_minimum": _num(e.get("food_and_beverage_min")), "rental_fee": _num(e.get("rental_fee")),
            "deposit": _num(e.get("deposit_amount")), "grand_total": _num(e.get("grand_total")),
            "actual_amount": _num(e.get("actual_amount")), "amount_due": _num(e.get("amount_due")),
            "price_per_person": _num(e.get("price_per_person")),
            "created_at": _ts_datetime(e.get("created_at")) or e.get("created_at"),
            "updated_at": _ts_datetime(e.get("updated_at")) or e.get("updated_at"),
            "room_ids": ",".join(rids),
            "rooms": ", ".join(names.get(r) or by_room_name[r] for r in rids if names.get(r) or r in by_room_name),
            "event_type_name": type_names.get(et), "source": origin,
            "deleted": 1 if e.get("deleted_at") else 0, "seen_at": seen_at,
            "lead_source": src, "definite_at": definite_at}


def _ts_loc_id(o: dict):
    """location_id as the API sends it, or the id of a nested {"location": {...}} as some objects carry it."""
    v = o.get("location_id")
    if v in (None, "") and isinstance(o.get("location"), dict):
        v = o["location"].get("id")
    return v


def _ts_datetime(v) -> str | None:
    """"7/27/2026 11:08 PM" (Tripleseat's created_at/updated_at) -> "2026-07-27T23:08:00"; ISO passes through."""
    if not v:
        return None
    sv = str(v).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", sv):
        return sv
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            return datetime.strptime(sv, fmt).isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def _ts_date(v) -> str | None:
    """Tripleseat's non-ISO dates are m/d/yyyy ("9/11/2026 4:54 PM"); ISO strings pass through."""
    if not v:
        return None
    s = str(v).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def _ts_lead_row(l: dict, slug: str, origin: str, seen_at: str | None) -> dict:
    nm = " ".join(x for x in [l.get("first_name"), l.get("last_name")] if x).strip()
    src = l.get("lead_source")
    if not src and isinstance(l.get("selected_lead_sources"), list) and l["selected_lead_sources"]:
        s0 = l["selected_lead_sources"][0]
        src = (s0.get("lead_source_name") or s0.get("name")) if isinstance(s0, dict) else s0
    loc = l.get("location")
    tid = str(l.get("location_id") or (loc.get("id") if isinstance(loc, dict) else "") or "")
    status = l.get("status") or l.get("state")
    if not status:                                      # the webhook object carries the lifecycle as timestamps
        status = "Converted" if l.get("converted_at") else "Turned down" if l.get("turned_down_at") else "Open"
    form = l.get("lead_form")
    return {"lead_id": str(l.get("id")), "location_id": slug, "ts_location_id": tid,
            "company": l.get("company"), "contact_name": nm, "status": status,
            "source": (src.get("name") if isinstance(src, dict) else src),
            "event_date": (_ts_date(l.get("event_date")) or ""), "guest_count": int(l.get("guest_count") or 0),
            "description": (l.get("event_description") or "")[:500],
            "created_at": _ts_datetime(l.get("created_at")) or l.get("created_at"),
            "updated_at": _ts_datetime(l.get("updated_at")) or l.get("updated_at"),
            "lead_form": (form.get("name") if isinstance(form, dict) else form),
            "converted_at": _ts_datetime(l.get("converted_at")) or l.get("converted_at"),
            "turned_down_at": _ts_datetime(l.get("turned_down_at")) or l.get("turned_down_at"),
            "origin": origin, "seen_at": seen_at}


def load_tripleseat_catalog(con) -> dict:
    """Route 1: what the public key can read. Replaced whole; it is small and has no history."""
    by_loc, by_room = _ts_mapping()
    # A taproom that lives as a room of another location (Solon in Iowa City) shares that location's billing
    # rules and lead forms, so those catalog rows are filed under both.
    sharers = {}
    for l in locations():
        if l.get("tripleseat_room_ids") and l.get("tripleseat_location_id"):
            sharers.setdefault(str(l["tripleseat_location_id"]), []).append(l["slug"])
    rooms, cat = [], []
    for slug, ds, p, j in iter_raw("tripleseat", location_id="_all", dataset="catalog"):
        for l in j.get("locations") or []:
            tid = str(l.get("id"))
            cat.append({"kind": "location", "id": tid, "name": l.get("name"), "location_id": by_loc.get(tid, ""), "value": (l.get("site_name") or "")})
            for r in l.get("rooms") or []:
                rid = str(r.get("id"))
                kids = [str(k.get("id")) for grp in (r.get("descendants") or []) for k in (grp or []) if isinstance(k, dict)]
                rooms.append({"room_id": rid, "ts_location_id": tid, "location_id": by_room.get(rid) or by_loc.get(tid),
                              "name": (r.get("name") or "").strip(), "capacity": r.get("capacity"),
                              "parent_room_id": None, "is_unassigned": 1 if r.get("is_unassigned") else 0, "_kids": kids})
        for s in j.get("sites") or []:
            for kind, key in (("event_type", "event_types"), ("lead_source", "lead_sources"), ("referral_source", "referral_sources"),
                              ("line_item_category", "line_item_categories")):
                for x in s.get(key) or []:
                    cat.append({"kind": kind, "id": str(x.get("id")), "name": (x.get("name") or "").strip(), "location_id": "", "value": ""})
            for b in s.get("billings") or []:
                for bl in b.get("billing_locations") or []:
                    tid = str(bl.get("location_id"))
                    for slug in _ts_slugs_of(tid, by_loc, sharers):
                        cat.append({"kind": "billing", "id": str(b.get("id")), "name": (b.get("name") or "").strip(), "location_id": slug,
                                    "value": str(bl.get("value") or "")})
        for f in j.get("lead_forms") or []:
            for fl in f.get("locations") or [{}]:
                tid = str(fl.get("id") or "")
                for slug in _ts_slugs_of(tid, by_loc, sharers):
                    cat.append({"kind": "lead_form", "id": str(f.get("id")), "name": (f.get("name") or "").strip(), "location_id": slug, "value": ""})
    # Parents: a room lists its descendants, so invert that to give each child its parent.
    parent = {}
    for r in rooms:
        for k in r.pop("_kids"):
            parent.setdefault(k, r["room_id"])
    for r in rooms:
        r["parent_room_id"] = parent.get(r["room_id"])
    if rooms or cat:
        con.execute("DELETE FROM ts_rooms"); con.execute("DELETE FROM ts_catalog")
        _upsert(con, "ts_rooms", rooms); _upsert(con, "ts_catalog", cat)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('tripleseat_catalog_at', ?)", (datetime.utcnow().isoformat(timespec="seconds") + "Z",))
    return {"rooms": len(rooms), "catalog": len(cat)}


def _ts_slugs_of(tid: str, by_loc: dict, sharers: dict) -> list[str]:
    """Our slugs a Tripleseat location's per-location settings apply to: its owner plus any taproom sharing it.
    An id that is no taproom of ours keeps the raw id, so the catalog still shows it (BlackStone)."""
    out = [by_loc[tid]] if tid in by_loc else [tid]
    return out + [s for s in sharers.get(tid, []) if s not in out]


def _ts_lookups(con) -> tuple[dict, dict]:
    room_names = {r[0]: r[1] for r in con.execute("SELECT room_id, name FROM ts_rooms WHERE is_unassigned=0")}
    type_names = {r[0]: r[1] for r in con.execute("SELECT id, name FROM ts_catalog WHERE kind='event_type'")}
    return room_names, type_names


def load_tripleseat_api(con) -> dict:
    """Route 3: events and leads pulled as a window. Replaced per location on every run — Tripleseat rows are
    edited long after the event date (final billing, guest counts), so a full refresh of the window is both
    cheaper and more correct than trying to detect changes. Webhook-sourced rows for the same location are
    left alone: they may be newer than the window."""
    by_loc, by_room = _ts_mapping()
    room_names, type_names = _ts_lookups(con)
    n_ev = n_ld = 0
    for slug, ds, p, j in iter_raw("tripleseat", dataset="events"):
        tid = str(j.get("location_id") or "")
        rows = []
        for e in j.get("events") or []:
            if e.get("deleted_at"):
                continue
            e = dict(e); e.setdefault("location_id", tid)
            rows.append(_ts_event_row(e, slug, room_names, type_names, "api", None))
        con.execute("DELETE FROM ts_events WHERE location_id=? AND COALESCE(source,'api')='api'", (slug,))
        n_ev += _upsert(con, "ts_events", rows)
    for slug, ds, p, j in iter_raw("tripleseat", dataset="leads"):
        tid = str(j.get("location_id") or "")
        rows = []
        for l in j.get("leads") or []:
            l = dict(l); l.setdefault("location_id", tid)
            rows.append(_ts_lead_row(l, slug, "api", None))
        con.execute("DELETE FROM ts_leads WHERE location_id=? AND COALESCE(origin,'api')='api'", (slug,))
        n_ld += _upsert(con, "ts_leads", rows)
    return {"events": n_ev, "leads": n_ld}


# What a webhook row looks like once the Apps Script has filed it: see apps-script/code.gs doTripleseatHook.
HOOK_HEADER = ["when", "action", "kind", "object_id", "ts_location_id", "event_date", "status", "json"]


def _unwrap_hook(payload: dict) -> tuple[str, dict]:
    """Tripleseat's webhook body is not documented beyond 'a JSON payload describing the change'. Accept the
    shapes it could reasonably take: {"event": {...}}, {"lead": {...}}, {"booking": {...}}, {"data": {...}},
    or the bare object. Returns (kind, object)."""
    if not isinstance(payload, dict):
        return "", {}
    bare = payload.get("id") is not None              # a wrapper has no id of its own; an event carries a nested "contact"
    for k in ("event", "lead", "booking", "contact", "account"):
        if not bare and isinstance(payload.get(k), dict):
            return k, payload[k]
    inner = None if bare else payload.get("data") if isinstance(payload.get("data"), dict) else payload.get("object") if isinstance(payload.get("object"), dict) else None
    if inner:
        for k in ("event", "lead", "booking"):
            if isinstance(inner.get(k), dict):
                return k, inner[k]
        return str(payload.get("object_type") or payload.get("type") or inner.get("type") or "").lower(), inner
    kind = str(payload.get("object_type") or payload.get("type") or payload.get("kind") or "").lower()
    if not kind:
        # Guess from the fields the object carries.
        if "event_date_iso8601" in payload or ("grand_total" in payload and "event_date" in payload):
            kind = "event"
        elif "lead_form" in payload or "turned_down_at" in payload or "event_description" in payload:
            kind = "lead"
        elif "start_date" in payload and "end_date" in payload:
            kind = "booking"
    return kind, payload


def _hook_effective(action: str, wrapper: dict, when: str | None) -> str | None:
    """When the state a tab row carries was true, as an ISO UTC string.

    A live delivery is true the moment it arrives (`when`). A seeded row (action SEED_*, see
    tools/tripleseat_seed.js) carries a snapshot from a report export, true when the report was exported — the
    payload says so in `exported_at`, or the message names the day ("... export of 2026-09-21"), which is taken
    as the end of that day in UTC. Rows are applied newest-state-first regardless of their order on the tab,
    so a seed appended after the live deliveries of the same night cannot roll an event back to the export."""
    if action.startswith("SEED") and isinstance(wrapper, dict):
        v = wrapper.get("exported_at")
        if v:
            sv = str(v).strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}$", sv):
                return sv + "T23:59:59Z"
            return sv
        m = re.search(r"(\d{4}-\d{2}-\d{2})", str(wrapper.get("message") or ""))
        if m:
            return m.group(1) + "T23:59:59Z"
    return when


def _hook_newer_exists(con, table: str, key: str, ident: str, eff: str | None) -> bool:
    """True when the warehouse already holds a state of this object that is newer than `eff` (the row would
    roll it back). Equal times apply again, so a run that re-reads the whole tab is idempotent."""
    if not eff:
        return False
    prev = con.execute(f"SELECT MAX(seen_at) FROM {table} WHERE {key}=?", (ident,)).fetchone()[0]
    return bool(prev) and str(prev)[:19] > str(eff)[:19]


def load_tripleseat_webhooks(con) -> dict:
    """Route 2: rows the Apps Script collected. Each row is one notification; the NEWEST state per object wins
    (by the time it was true, see _hook_effective — for live deliveries that is the order they arrived), and
    the warehouse keeps that state between runs. Nothing is deleted from the warehouse here: a DELETE
    notification (or deleted_at) marks the row deleted, so a late re-send of an older UPDATE cannot bring it
    back. Rows seeded from a report export (action SEED_*) load like deliveries but are labelled source='seed'
    until a live delivery replaces them."""
    by_loc, by_room = _ts_mapping()
    room_names, type_names = _ts_lookups(con)
    files = sorted(iter_raw("tripleseat", location_id="_all", dataset="webhook"), key=lambda t: t[2].name)
    if not files:
        return {"rows": 0}
    n_rows = n_ev = n_ld = n_bk = n_unmapped = n_seed = n_stale = 0
    cursor, first_seen, seeded_at = None, None, None
    # A pull that started from row 0 (`--backfill`, or a first run) re-reads the whole tab, which is the complete
    # record of everything this route ever delivered: rebuild its rows from scratch rather than merging, so the
    # newest-state rule below sees the tab alone and not what an earlier reading of it left behind.
    rebuild = int(files[0][3].get("since") or 0) == 0
    if rebuild:
        n_old = con.execute("SELECT COUNT(*) FROM ts_events WHERE source IN ('webhook','seed')").fetchone()[0]
        con.execute("DELETE FROM ts_events WHERE source IN ('webhook','seed')")
        con.execute("DELETE FROM ts_leads WHERE origin IN ('webhook','seed')")
        con.execute("DELETE FROM meta WHERE key IN ('tripleseat_webhook_first','tripleseat_seeded_at','tripleseat_seed_rows')")
        if n_old:
            log.info("Tripleseat webhooks: the pull re-read the whole tab — rebuilding %d webhook/seed events from it", n_old)
    for slug_, ds, p, j in files:
        hdr = j.get("header") or HOOK_HEADER
        idx = {h: i for i, h in enumerate(hdr)}
        for r in j.get("rows") or []:
            n_rows += 1
            try:
                raw = r[idx["json"]] if "json" in idx else r[-1]
                obj = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
            except (ValueError, IndexError):
                obj = {}
            action = str(r[idx["action"]] if "action" in idx else "").upper()
            if not action and isinstance(obj, dict):
                action = str(obj.get("webhook_trigger_type") or obj.get("action") or obj.get("trigger") or "").upper()
            seeded = action.startswith("SEED")
            if seeded:
                n_seed += 1
            elif first_seen is None and "when" in idx and r[idx["when"]]:
                first_seen = r[idx["when"]]                 # the first LIVE delivery — when the webhook started
            kind, o = _unwrap_hook(obj)
            kind = (str(r[idx["kind"]]).lower() if "kind" in idx and r[idx["kind"]] else kind) or kind
            seen = str(r[idx["when"]]) if "when" in idx else None
            eff = _hook_effective(action, obj, seen)
            if seeded and eff and (seeded_at is None or eff > seeded_at):
                seeded_at = eff
            origin = "seed" if seeded else "webhook"
            deleted = "DELETE" in action or bool(o.get("deleted_at"))
            if kind == "event" and o.get("id") is not None:
                slug = _ts_slug(_ts_loc_id(o) or (r[idx["ts_location_id"]] if "ts_location_id" in idx else None),
                                _ts_ids(o.get("room_ids") if o.get("room_ids") is not None else o.get("rooms")), by_loc, by_room)
                if not slug:
                    n_unmapped += 1; continue
                if _hook_newer_exists(con, "ts_events", "event_id", str(o.get("id")), eff):
                    n_stale += 1; continue
                row = _ts_event_row(o, slug, room_names, type_names, origin, eff)
                row["deleted"] = 1 if deleted else 0
                con.execute("DELETE FROM ts_events WHERE event_id=? AND location_id<>?", (row["event_id"], slug))   # moved between taprooms
                _upsert(con, "ts_events", [row]); n_ev += 1
            elif kind == "lead" and o.get("id") is not None:
                tid = _ts_loc_id(o) or (r[idx["ts_location_id"]] if "ts_location_id" in idx else None)
                slug = _ts_slug(tid, [], by_loc, by_room)
                if not slug:
                    n_unmapped += 1; continue
                if _hook_newer_exists(con, "ts_leads", "lead_id", str(o.get("id")), eff):
                    n_stale += 1; continue
                row = _ts_lead_row(o, slug, origin, eff)
                if "CONVERT" in action:
                    row["status"] = "Converted"
                elif "TURNED_DOWN" in action or "TURN_DOWN" in action:
                    row["status"] = "Turned down"
                con.execute("DELETE FROM ts_leads WHERE lead_id=? AND location_id<>?", (row["lead_id"], slug))
                _upsert(con, "ts_leads", [row]); n_ld += 1
            elif kind == "booking":
                # A booking is the folder the events sit in; the events themselves arrive as events. Nothing to
                # store yet — counted so the log shows the feed is alive.
                n_bk += 1
        cursor = int(j.get("since") or 0) + len(j.get("rows") or [])
    if cursor is not None:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('tripleseat_webhook_cursor', ?)", (str(cursor),))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('tripleseat_webhook_at', ?)", (datetime.utcnow().isoformat(timespec="seconds") + "Z",))
        if first_seen and not con.execute("SELECT 1 FROM meta WHERE key='tripleseat_webhook_first'").fetchone():
            con.execute("INSERT INTO meta VALUES ('tripleseat_webhook_first', ?)", (str(first_seen),))
        if seeded_at:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('tripleseat_seeded_at', ?)", (str(seeded_at),))
            con.execute("INSERT OR REPLACE INTO meta VALUES ('tripleseat_seed_rows', ?)", (str(n_seed),))
    if n_unmapped:
        log.warning("Tripleseat webhooks: %d notifications for a location that is no taproom of ours (BlackStone?) — ignored", n_unmapped)
    if n_seed or n_stale:
        log.info("Tripleseat webhooks: %d of %d rows were seeded from a report export (state as of %s); %d row(s) skipped because the warehouse already held a newer state",
                 n_seed, n_rows, seeded_at, n_stale)
    return {"rows": n_rows, "events": n_ev, "leads": n_ld, "bookings": n_bk, "seeded": n_seed, "stale": n_stale, "cursor": cursor}


def load_tripleseat(con) -> dict:
    """All three routes (sdp/tripleseat.py). The catalog goes first because the other two resolve room and
    event-type names through it."""
    out = {}
    for name, fn in (("catalog", load_tripleseat_catalog), ("api", load_tripleseat_api), ("webhooks", load_tripleseat_webhooks)):
        try:
            out[name] = fn(con)
        except Exception as e:
            log.error("transform: tripleseat %s failed (%s: %s) — continuing", name, type(e).__name__, e)
            out[name] = {"error": str(e)}
    return out


# ---------------------------------------------------------------- manual inputs

def load_inputs(con) -> dict:
    a = [{"activation_id": r["activation_id"], "location_id": r["location_id"], "start_date": r["start_date"], "end_date": r.get("end_date") or r["start_date"], "name": r["name"], "type": r.get("type"),
          "cost": float(r.get("cost") or 0), "owner": r.get("owner"), "notes": r.get("notes")} for r in inputs.read_activations()]
    con.execute("DELETE FROM activations"); _upsert(con, "activations", a)
    t = [{"location_id": r["location_id"], "month": r["month"], "sales_target": float(r.get("sales_target") or 0) or None, "cogs_pct_target": float(r.get("cogs_pct_target") or 0) or None,
          "labor_pct_target": float(r.get("labor_pct_target") or 0) or None, "guests_target": int(float(r.get("guests_target") or 0)) or None} for r in inputs.read_targets()]
    _upsert(con, "targets", t)
    inv = [{"location_id": r["location_id"], "count_date": r["count_date"], "bucket": r["bucket"], "value": float(r.get("value") or 0), "source": "csv"} for r in inputs.read_inventory_counts()]
    con.execute("DELETE FROM me_inventory_counts WHERE source='csv'")
    if inv:  # CSV rows never override API-derived counts for the same location/date/bucket
        con.executemany("INSERT OR IGNORE INTO me_inventory_counts (location_id, count_date, bucket, value, source) VALUES (?,?,?,?,?)",
                        [(r["location_id"], r["count_date"], r["bucket"], r["value"], r["source"]) for r in inv])
    def _f(v):
        try:
            return float(str(v).replace(",", "")) if str(v).strip() != "" else None
        except ValueError:
            return None
    fl = [{"location_id": r["location_id"], "drawer": r.get("drawer") or "*", "toast_expected": _f(r.get("toast_expected")),
           "actual_float": _f(r.get("actual_float")), "notes": r.get("notes")} for r in inputs.read_floats() if r.get("location_id")]
    con.execute("DELETE FROM drawer_floats"); _upsert(con, "drawer_floats", fl)
    dep = [{"location_id": r["location_id"], "brand": r["brand"], "premise": (r.get("premise") or "ALL").upper(), "ce_ty": _f(r.get("ce_ty")) or 0.0,
            "ce_ly": _f(r.get("ce_ly")) or 0.0, "accounts": int(_f(r.get("accounts")) or 0), "as_of": r.get("as_of")} for r in inputs.read_depletions() if r.get("location_id") and r.get("brand")]
    # The uploaded roll-up (via the roster workbook) wins over a local CSV; the CSV remains for local development.
    for slug, ds, p_, j in iter_raw("depletions"):
        up = [{"location_id": r[0], "brand": r[1].lstrip("'"), "premise": (r[2] or "ALL").upper(), "ce_ty": _f(r[3]) or 0.0, "ce_ly": _f(r[4]) or 0.0,
               "accounts": int(_f(r[5]) or 0), "as_of": str(r[6]).lstrip("'")} for r in (j.get("rows") or []) if len(r) >= 7 and r[0] and r[1]]
        if up:
            dep = up
    if dep:                                  # an absent file leaves the last loaded depletions in place
        con.execute("DELETE FROM depletions"); _upsert(con, "depletions", dep)
    return {"activations": len(a), "targets": len(t), "inventory_counts": len(inv), "floats": len(fl), "depletions": len(dep)}


# ---------------------------------------------------------------- derived

def rebuild_daily_summary(con):
    """location × business day. Sales: Toast orders when that day was pulled from Toast, else the MarginEdge sales
    report (which is Toast data arriving via the ME integration). Labor: Toast time entries, else P&L labor total."""
    con.execute("DELETE FROM daily_summary")
    con.execute("""
    INSERT INTO daily_summary (location_id, business_date, net_sales, gross_sales, discounts, tax, tips, refunds, orders, checks, guests,
      sales_food, sales_beer, sales_liquor, sales_wine, sales_nabev, sales_retail, sales_other, sales_svc, sales_unattr, labor_hours, labor_cost,
      purchases, purch_food, purch_beer, purch_liquor, purch_wine, purch_nabev, purch_retail, purch_other)
    WITH days AS (
      SELECT location_id, business_date FROM toast_orders
      UNION SELECT location_id, business_date FROM toast_time_entries
      UNION SELECT location_id, business_date FROM me_sales_daily
      UNION SELECT location_id, business_date FROM me_pnl_summary
      UNION SELECT location_id, invoice_date FROM me_invoices WHERE invoice_date IS NOT NULL
    ),
    o AS (SELECT location_id, business_date, SUM(net_sales) net, SUM(gross_sales) gross, SUM(discounts) disc, SUM(tax) tax, SUM(tips) tips, SUM(refunds) ref, SUM(service_charges) svc,
                 SUM(CASE WHEN voided=0 THEN 1 ELSE 0 END) orders, SUM(CASE WHEN voided=0 THEN checks_count ELSE 0 END) checks, SUM(CASE WHEN voided=0 THEN guests ELSE 0 END) guests
          FROM toast_orders GROUP BY 1,2),
    i AS (SELECT location_id, business_date,
                 SUM(CASE WHEN bucket='Food' THEN price ELSE 0 END) f, SUM(CASE WHEN bucket='Beer' THEN price ELSE 0 END) b, SUM(CASE WHEN bucket='Liquor' THEN price ELSE 0 END) l,
                 SUM(CASE WHEN bucket='Wine' THEN price ELSE 0 END) w, SUM(CASE WHEN bucket='NA Bev' THEN price ELSE 0 END) n, SUM(CASE WHEN bucket='Retail' THEN price ELSE 0 END) r,
                 SUM(CASE WHEN bucket NOT IN ('Food','Beer','Liquor','Wine','NA Bev','Retail') THEN price ELSE 0 END) x
          FROM toast_order_items WHERE voided=0 GROUP BY 1,2),
    ms AS (SELECT location_id, business_date, SUM(total) net,
                 SUM(CASE WHEN bucket='Food' THEN total ELSE 0 END) f, SUM(CASE WHEN bucket='Beer' THEN total ELSE 0 END) b, SUM(CASE WHEN bucket='Liquor' THEN total ELSE 0 END) l,
                 SUM(CASE WHEN bucket='Wine' THEN total ELSE 0 END) w, SUM(CASE WHEN bucket='NA Bev' THEN total ELSE 0 END) n, SUM(CASE WHEN bucket='Retail' THEN total ELSE 0 END) r,
                 SUM(CASE WHEN bucket NOT IN ('Food','Beer','Liquor','Wine','NA Bev','Retail') THEN total ELSE 0 END) x
          FROM me_sales_daily GROUP BY 1,2),
    t AS (SELECT location_id, business_date, SUM(regular_hours+overtime_hours) hrs, SUM(wages) cost FROM toast_time_entries GROUP BY 1,2),
    ml AS (SELECT location_id, business_date, labor_total cost FROM me_pnl_summary),
    p AS (SELECT location_id, invoice_date business_date,
                 SUM(CASE WHEN bucket='Food' THEN line_price ELSE 0 END) f, SUM(CASE WHEN bucket='Beer' THEN line_price ELSE 0 END) b, SUM(CASE WHEN bucket='Liquor' THEN line_price ELSE 0 END) l,
                 SUM(CASE WHEN bucket='Wine' THEN line_price ELSE 0 END) w, SUM(CASE WHEN bucket='NA Bev' THEN line_price ELSE 0 END) n,
                 SUM(CASE WHEN bucket='Retail' THEN line_price ELSE 0 END) r,
                 SUM(CASE WHEN bucket NOT IN ('Food','Beer','Liquor','Wine','NA Bev','Retail') THEN line_price ELSE 0 END) x
          FROM me_invoice_lines GROUP BY 1,2),
    ph AS (SELECT location_id, invoice_date business_date, SUM(order_total) tot FROM me_invoices GROUP BY 1,2)
    SELECT d.location_id, d.business_date,
      COALESCE(o.net, ms.net, 0), COALESCE(o.gross, ms.net, 0), COALESCE(o.disc,0), COALESCE(o.tax,0), COALESCE(o.tips,0), COALESCE(o.ref,0), COALESCE(o.orders,0), COALESCE(o.checks,0), COALESCE(o.guests,0),
      COALESCE(i.f, ms.f, 0), COALESCE(i.b, ms.b, 0), COALESCE(i.l, ms.l, 0), COALESCE(i.w, ms.w, 0), COALESCE(i.n, ms.n, 0), COALESCE(i.r, ms.r, 0), COALESCE(i.x, ms.x, 0),
      COALESCE(o.svc,0),
      -- The unattributed remainder, as a residual. Sourcing this from Toast's service-charge field was wrong:
      -- it matched the real gap on only 608 of 2,088 location-days and overshot by $7,085 on the worst. A
      -- residual is right on every day by construction. Clamped at zero so a day where the categories somehow
      -- exceed net shows nothing rather than a negative slice; check 2 reports that case instead of hiding it.
      MAX(0, COALESCE(o.net, ms.net, 0)
             - (COALESCE(i.f, ms.f, 0) + COALESCE(i.b, ms.b, 0) + COALESCE(i.l, ms.l, 0) + COALESCE(i.w, ms.w, 0)
                + COALESCE(i.n, ms.n, 0) + COALESCE(i.r, ms.r, 0) + COALESCE(i.x, ms.x, 0))),
      COALESCE(t.hrs,0), COALESCE(t.cost, ml.cost, 0),
      COALESCE(ph.tot,0), COALESCE(p.f,0), COALESCE(p.b,0), COALESCE(p.l,0), COALESCE(p.w,0), COALESCE(p.n,0), COALESCE(p.r,0), COALESCE(p.x,0)
    FROM days d
    LEFT JOIN o  ON o.location_id=d.location_id  AND o.business_date=d.business_date
    LEFT JOIN i  ON i.location_id=d.location_id  AND i.business_date=d.business_date
    LEFT JOIN ms ON ms.location_id=d.location_id AND ms.business_date=d.business_date
    LEFT JOIN t  ON t.location_id=d.location_id  AND t.business_date=d.business_date
    LEFT JOIN ml ON ml.location_id=d.location_id AND ml.business_date=d.business_date
    LEFT JOIN p  ON p.location_id=d.location_id  AND p.business_date=d.business_date
    LEFT JOIN ph ON ph.location_id=d.location_id AND ph.business_date=d.business_date
    """)


def load_scorecard(con) -> dict:
    """The Leadership Scorecard tab, stored as-is. Replaced wholesale each run: the sheet is the source of
    truth and cells are revised in place (a week's figure is corrected days later), so merging would preserve
    numbers leadership has since changed their mind about."""
    n = 0
    for slug, ds, p, j in iter_raw("scorecard", dataset="scorecard"):
        if not j.get("rows"):
            continue
        con.execute("DELETE FROM scorecard")
        con.execute("DELETE FROM scorecard_goals")
        con.execute("DELETE FROM scorecard_tabs")
        _upsert(con, "scorecard_tabs", [{"tab": t["name"], "seq": i, "rows": t.get("rows"), "cols": t.get("cols"),
                                         "truncated": 1 if t.get("truncated") else 0, "grid": json.dumps(t.get("grid") or [])}
                                        for i, t in enumerate(j.get("tabs") or [])])
        cells, goals = [], []
        for i, r in enumerate(j["rows"]):
            metric, owner = r.get("metric"), r.get("owner") or None
            g = r.get("goal") or {}
            if g.get("d"):
                goals.append({"metric": metric, "owner": owner, "value": g.get("v"), "display": g.get("d"), "seq": i})
            for week, c in (r.get("cells") or {}).items():
                cells.append({"metric": metric, "week": week, "owner": owner, "value": c.get("v"), "display": c.get("d"), "seq": i})
        _upsert(con, "scorecard", cells)
        _upsert(con, "scorecard_goals", goals)
        n = len(cells)
    return {"cells": n}


def resize_beer(con) -> dict:
    """Re-read the pour size of every beer sale whose modifiers were captured.

    Sizes are parsed when a day is loaded, but the parser's keyword table lives in config and WILL be corrected
    once real modifier text has been read ("Imperial" turns out to be a 20 oz pour, say). Because the modifier
    text is stored verbatim, that correction can reach the whole history here in a few seconds, instead of
    needing every day re-pulled from Toast. Rows loaded before modifiers were captured (modifiers IS NULL) are
    left alone: their size is unknowable, and the portal reports them as such.
    """
    pp = PourParser()
    cur = con.execute("SELECT selection_guid, item_name, modifiers, sales_category, size_oz, pour FROM toast_order_items WHERE bucket='Beer' AND modifiers IS NOT NULL")
    changed, n, memo = [], 0, {}
    for g, name, mods, sc, oz, pour in cur:
        n += 1
        k = (name, mods, sc)                          # a few hundred distinct strings across a million rows
        if k not in memo:
            try:
                memo[k] = pp.parse(name, mods, sc)
            except Exception:
                memo[k] = (None, None)
        noz, npour = memo[k]
        if noz != oz or npour != pour:
            changed.append((noz, npour, g))
    if changed:
        con.executemany("UPDATE toast_order_items SET size_oz=?, pour=? WHERE selection_guid=?", changed)
    return {"beer_rows": n, "resized": len(changed)}


def load_weather(con) -> dict:
    rows = []
    for slug, ds, p, j in iter_raw("weather"):
        for r in j.get("days", []):
            rows.append({"location_id": slug, "date": r["date"], "tmax_f": r.get("tmax_f"), "tmin_f": r.get("tmin_f"),
                         "precip_in": r.get("precip_in"), "code": r.get("code"), "kind": r.get("kind") or "observed"})
    # A forecast row must never outlive the day it was forecasting: once that day has passed, either an
    # observation replaces it (same primary key) or it is removed, so nothing reads a prediction as history.
    n = _upsert(con, "weather_daily", rows)
    if rows:
        con.execute("DELETE FROM weather_daily WHERE kind='forecast' AND date < ?", (min(r["date"] for r in rows if r["kind"] == "forecast") if any(r["kind"] == "forecast" for r in rows) else "0000",))
    return {"days": n}


def run() -> dict:
    cfg = settings()
    bk = Buckets(cfg["category_map"])
    con = connect()
    _upsert(con, "locations", [{"location_id": l["slug"], "name": l["name"], "short": l.get("short"), "toast_guid": l.get("toast_guid"), "marginedge_unit_id": str(l.get("marginedge_unit_id") or ""),
                                "timezone": l.get("timezone"), "opened": l.get("opened"), "state": l.get("state")} for l in locations()])
    # Before anything is loaded: rows written from here on are on the restaurant's clock, so history has to be
    # moved onto it first, or the two would be indistinguishable afterwards.
    hours_fix = fix_item_hours(con)
    s = {"toast": load_toast(con, bk), "marginedge": load_marginedge(con, bk), "tripleseat": load_tripleseat(con),
         "scorecard": load_scorecard(con), "inputs": load_inputs(con)}
    # Derived, optional steps. Each is isolated: they add context to the portal, and none of them is worth the
    # nightly publish. A failure is logged loudly and the run carries on without that one thing.
    s["item_hours"] = hours_fix
    for name, fn in (("pours", resize_beer), ("weather", load_weather)):
        try:
            s[name] = fn(con)
        except Exception as e:
            log.error("transform: optional step '%s' failed (%s: %s) — continuing without it", name, type(e).__name__, e)
            s[name] = {"error": str(e)}
    rebuild_daily_summary(con)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('last_transform', ?)", (datetime.utcnow().isoformat(timespec="seconds") + "Z",))
    con.commit()
    n = con.execute("SELECT COUNT(*), MIN(business_date), MAX(business_date) FROM daily_summary").fetchone()
    log.info("transform: %s | daily_summary rows=%d range=%s..%s", s, *n)
    con.close()
    return s
