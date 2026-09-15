"""Transform: raw/ JSON (real or mock) -> state/warehouse.sqlite, then rebuild daily_summary.

Idempotent: every table is upserted on its natural key, so re-pulling a business day replaces it cleanly
(orders modified after close — tips, voids, refunds — are handled by re-pulling the incremental window).
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import inputs
from .toast import CONFIG_RESOURCES
from .util import DB_PATH, ROOT, iter_raw, load_json, locations, log, settings

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


def _bd(v) -> str:
    """Toast businessDate int yyyymmdd -> ISO."""
    s = str(v)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


def _hour(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        return int(ts[11:13])  # local-offset timestamps from Toast carry the restaurant's offset
    except Exception:
        return None


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
            "amount": round(float(d.get("discountAmount") or 0), 2)}


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
                guid, name = d.get("guid"), d.get("name") or d.get("behavior")
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

    for slug, ds, p, j in iter_raw("toast", dataset="orders"):
        orders, items, pays, discs = [], [], [], []
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
                for s in c.get("selections") or []:
                    for d in (s.get("appliedDiscounts") or []):
                        disc += float(d.get("discountAmount") or 0)
                        discs.append(_disc_row(d, o, c, slug, bd, "item"))
                    meta = item_cat.get((slug, (s.get("item") or {}).get("guid")))
                    sc = ((s.get("salesCategory") or {}).get("name")
                          or salescat.get((slug, (s.get("salesCategory") or {}).get("guid")))
                          or (meta[2] if meta else None))
                    items.append({"selection_guid": s["guid"], "order_guid": o["guid"], "check_guid": c["guid"], "location_id": slug, "business_date": bd,
                                  "item_guid": (s.get("item") or {}).get("guid"), "item_name": s.get("displayName") or (meta[0] if meta else None),
                                  "item_group_guid": (s.get("itemGroup") or {}).get("guid"), "sales_category": sc, "bucket": bk.from_toast(sc),
                                  "quantity": float(s.get("quantity") or 0), "pre_discount_price": float(s.get("preDiscountPrice") or 0), "price": float(s.get("price") or 0),
                                  "tax": float(s.get("tax") or 0), "voided": 1 if (s.get("voided") or o.get("voided")) else 0, "hour_local": _hour(s.get("createdDate") or o.get("openedDate"))})
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
                           "net_sales": 0 if voided else round(net, 2), "tax": 0 if voided else round(tax, 2), "tips": round(tips, 2), "discounts": round(disc, 2), "service_charges": round(svc, 2),
                           "gross_sales": 0 if voided else round(net + disc, 2), "voided_value": voided_value, "refunds": round(refunds, 2), "source_hash": None})
        # replace the whole business day for this location so deleted orders disappear
        if orders:
            con.execute("DELETE FROM toast_order_items WHERE location_id=? AND business_date=?", (slug, orders[0]["business_date"]))
            con.execute("DELETE FROM toast_payments WHERE location_id=? AND business_date=?", (slug, orders[0]["business_date"]))
            con.execute("DELETE FROM toast_orders WHERE location_id=? AND business_date=?", (slug, orders[0]["business_date"]))
            con.execute("DELETE FROM toast_discounts WHERE location_id=? AND business_date=?", (slug, orders[0]["business_date"]))
        stats["orders"] += _upsert(con, "toast_orders", orders)
        stats["items"] += _upsert(con, "toast_order_items", items)
        stats["payments"] += _upsert(con, "toast_payments", pays)
        stats["discounts"] = stats.get("discounts", 0) + _upsert(con, "toast_discounts", [d for d in discs if d])

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
    sh_rows = []
    for slug, ds, p, j in iter_raw("toast", dataset="shifts"):
        co = closeout.get(slug, 4)
        for x in j.get("shifts", []):
            if x.get("deleted"):
                continue
            a, b = x.get("inDate"), x.get("outDate")
            if not a:
                continue
            try:
                start = datetime.strptime(a[:19], "%Y-%m-%dT%H:%M:%S")
                bd = (start - timedelta(days=1)).date().isoformat() if start.hour < co else start.date().isoformat()
                hrs = ((datetime.strptime(b[:19], "%Y-%m-%dT%H:%M:%S") - start).total_seconds() / 3600.0) if b else 0.0
            except Exception:
                continue
            jg = (x.get("jobReference") or {}).get("guid")
            sh_rows.append({"shift_guid": x["guid"], "location_id": slug, "business_date": bd,
                            "employee_guid": (x.get("employeeReference") or {}).get("guid"), "job_guid": jg,
                            "job_name": jobs.get((slug, jg)), "in_at": a, "out_at": b,
                            "hours": round(hrs, 2) if 0 < hrs <= 24 else 0.0, "deleted": 0})
    stats["shifts"] = _upsert(con, "toast_shifts", sh_rows)

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

    for slug, ds, p, j in iter_raw("toast", dataset="timeEntries"):
        rows = []
        for t in j.get("timeEntries", []):
            if t.get("deleted"):
                continue
            reg, ot, wage = float(t.get("regularHours") or 0), float(t.get("overtimeHours") or 0), float(t.get("hourlyWage") or 0)
            bd = _bd(t.get("businessDate")) if t.get("businessDate") else (t.get("inDate") or "")[:10]
            jg = (t.get("jobReference") or {}).get("guid")
            rows.append({"entry_guid": t["guid"], "location_id": slug, "business_date": bd, "employee_guid": (t.get("employeeReference") or {}).get("guid"), "job_guid": jg,
                         "job_name": jobs.get((slug, jg)), "in_at": t.get("inDate"), "out_at": t.get("outDate"), "regular_hours": reg, "overtime_hours": ot, "hourly_wage": wage,
                         "wages": round(reg * wage + ot * wage * 1.5, 2), "declared_cash_tips": float(t.get("declaredCashTips") or 0), "non_cash_tips": float(t.get("nonCashTips") or 0)})
        stats["time_entries"] += _upsert(con, "toast_time_entries", rows)
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


def load_tripleseat(con) -> dict:
    """Private events and the lead pipeline behind them. Events are replaced per location on every run —
    Tripleseat rows are edited long after the event date (final billing, guest counts), so a full refresh of
    the window is both cheaper and more correct than trying to detect changes."""
    n_ev = n_ld = 0
    for slug, ds, p, j in iter_raw("tripleseat", dataset="events"):
        tid = str(j.get("location_id") or "")
        rows = []
        for e in j.get("events") or []:
            if e.get("deleted_at"):
                continue
            rows.append({"event_id": str(e.get("id")), "location_id": slug, "ts_location_id": str(e.get("location_id") or tid),
                         "booking_id": str(e.get("booking_id") or ""), "name": e.get("name"), "status": e.get("status"),
                         "event_type": str(e.get("event_type_id") or ""), "event_style": e.get("event_style"),
                         "event_date": (e.get("event_date_iso8601") or e.get("event_date") or "")[:10],
                         "start_at": e.get("event_start_iso8601") or e.get("event_start"),
                         "end_at": e.get("event_end_iso8601") or e.get("event_end"),
                         "guest_count": int(e.get("guest_count") or 0), "guaranteed_guest_count": int(e.get("guaranteed_guest_count") or 0),
                         "fb_minimum": _num(e.get("food_and_beverage_min")), "rental_fee": _num(e.get("rental_fee")),
                         "deposit": _num(e.get("deposit_amount")), "grand_total": _num(e.get("grand_total")),
                         "actual_amount": _num(e.get("actual_amount")), "amount_due": _num(e.get("amount_due")),
                         "price_per_person": _num(e.get("price_per_person")),
                         "created_at": e.get("created_at"), "updated_at": e.get("updated_at")})
        con.execute("DELETE FROM ts_events WHERE location_id=?", (slug,))
        n_ev += _upsert(con, "ts_events", rows)
    for slug, ds, p, j in iter_raw("tripleseat", dataset="leads"):
        tid = str(j.get("location_id") or "")
        rows = []
        for l in j.get("leads") or []:
            nm = " ".join(x for x in [l.get("first_name"), l.get("last_name")] if x).strip()
            rows.append({"lead_id": str(l.get("id")), "location_id": slug, "ts_location_id": str(l.get("location_id") or tid),
                         "company": l.get("company"), "contact_name": nm, "status": l.get("status") or l.get("state"),
                         "source": (l.get("lead_source") or {}).get("name") if isinstance(l.get("lead_source"), dict) else l.get("lead_source"),
                         "event_date": (l.get("event_date") or "")[:10], "guest_count": int(l.get("guest_count") or 0),
                         "description": (l.get("event_description") or "")[:500],
                         "created_at": l.get("created_at"), "updated_at": l.get("updated_at")})
        con.execute("DELETE FROM ts_leads WHERE location_id=?", (slug,))
        n_ld += _upsert(con, "ts_leads", rows)
    return {"events": n_ev, "leads": n_ld}


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
    return {"activations": len(a), "targets": len(t), "inventory_counts": len(inv)}


# ---------------------------------------------------------------- derived

def rebuild_daily_summary(con):
    """location × business day. Sales: Toast orders when that day was pulled from Toast, else the MarginEdge sales
    report (which is Toast data arriving via the ME integration). Labor: Toast time entries, else P&L labor total."""
    con.execute("DELETE FROM daily_summary")
    con.execute("""
    INSERT INTO daily_summary (location_id, business_date, net_sales, gross_sales, discounts, tax, tips, refunds, orders, checks, guests,
      sales_food, sales_beer, sales_liquor, sales_wine, sales_nabev, sales_retail, sales_other, labor_hours, labor_cost,
      purchases, purch_food, purch_beer, purch_liquor, purch_wine, purch_nabev, purch_retail, purch_other)
    WITH days AS (
      SELECT location_id, business_date FROM toast_orders
      UNION SELECT location_id, business_date FROM toast_time_entries
      UNION SELECT location_id, business_date FROM me_sales_daily
      UNION SELECT location_id, business_date FROM me_pnl_summary
      UNION SELECT location_id, invoice_date FROM me_invoices WHERE invoice_date IS NOT NULL
    ),
    o AS (SELECT location_id, business_date, SUM(net_sales) net, SUM(gross_sales) gross, SUM(discounts) disc, SUM(tax) tax, SUM(tips) tips, SUM(refunds) ref,
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


def run() -> dict:
    cfg = settings()
    bk = Buckets(cfg["category_map"])
    con = connect()
    _upsert(con, "locations", [{"location_id": l["slug"], "name": l["name"], "short": l.get("short"), "toast_guid": l.get("toast_guid"), "marginedge_unit_id": str(l.get("marginedge_unit_id") or ""),
                                "timezone": l.get("timezone"), "opened": l.get("opened"), "state": l.get("state")} for l in locations()])
    s = {"toast": load_toast(con, bk), "marginedge": load_marginedge(con, bk), "tripleseat": load_tripleseat(con),
         "scorecard": load_scorecard(con), "inputs": load_inputs(con)}
    rebuild_daily_summary(con)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('last_transform', ?)", (datetime.utcnow().isoformat(timespec="seconds") + "Z",))
    con.commit()
    n = con.execute("SELECT COUNT(*), MIN(business_date), MAX(business_date) FROM daily_summary").fetchone()
    log.info("transform: %s | daily_summary rows=%d range=%s..%s", s, *n)
    con.close()
    return s
