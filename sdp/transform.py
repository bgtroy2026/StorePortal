"""Transform: raw/ JSON (real or mock) -> state/warehouse.sqlite, then rebuild daily_summary.

Idempotent: every table is upserted on its natural key, so re-pulling a business day replaces it cleanly
(orders modified after close — tips, voids, refunds — are handled by re-pulling the incremental window).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import inputs
from .util import DB_PATH, ROOT, iter_raw, load_json, locations, log, settings

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
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

def load_toast(con, bk: Buckets) -> dict:
    stats = {"orders": 0, "items": 0, "payments": 0, "time_entries": 0}
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
    jobs: dict[tuple[str, str], str] = {}
    for slug, ds, p, j in iter_raw("toast", dataset="jobs"):
        rows = [{"job_guid": x["guid"], "location_id": slug, "title": x.get("title"), "wage_frequency": x.get("wageFrequency"), "default_wage": x.get("defaultWage")} for x in j.get("jobs", [])]
        for r in rows:
            jobs[(slug, r["job_guid"])] = r["title"]
        _upsert(con, "toast_jobs", rows)

    for slug, ds, p, j in iter_raw("toast", dataset="orders"):
        orders, items, pays = [], [], []
        for o in j.get("orders", []):
            bd = _bd(o.get("businessDate"))
            net = tax = tips = disc = svc = refunds = 0.0
            checks = o.get("checks") or []
            for c in checks:
                if c.get("voided") or c.get("deleted"):
                    continue
                net += float(c.get("amount") or 0); tax += float(c.get("taxAmount") or 0)
                disc += sum(float(d.get("discountAmount") or 0) for d in (c.get("appliedDiscounts") or []))
                svc += sum(float(s.get("chargeAmount") or 0) for s in (c.get("appliedServiceCharges") or []))
                for s in c.get("selections") or []:
                    disc += sum(float(d.get("discountAmount") or 0) for d in (s.get("appliedDiscounts") or []))
                    meta = item_cat.get((slug, (s.get("item") or {}).get("guid")))
                    sc = (s.get("salesCategory") or {}).get("name") or (meta[2] if meta else None)
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
                           "dining_option": (o.get("diningOption") or {}).get("behavior") or (o.get("diningOption") or {}).get("guid"), "revenue_center": (o.get("revenueCenter") or {}).get("guid"),
                           "server_guid": (o.get("server") or {}).get("guid"), "guests": int(o.get("numberOfGuests") or 0), "voided": voided, "checks_count": len(checks),
                           "net_sales": 0 if voided else round(net, 2), "tax": 0 if voided else round(tax, 2), "tips": round(tips, 2), "discounts": round(disc, 2), "service_charges": round(svc, 2),
                           "gross_sales": 0 if voided else round(net + disc, 2), "refunds": round(refunds, 2), "source_hash": None})
        # replace the whole business day for this location so deleted orders disappear
        if orders:
            con.execute("DELETE FROM toast_order_items WHERE location_id=? AND business_date=?", (slug, orders[0]["business_date"]))
            con.execute("DELETE FROM toast_payments WHERE location_id=? AND business_date=?", (slug, orders[0]["business_date"]))
            con.execute("DELETE FROM toast_orders WHERE location_id=? AND business_date=?", (slug, orders[0]["business_date"]))
        stats["orders"] += _upsert(con, "toast_orders", orders)
        stats["items"] += _upsert(con, "toast_order_items", items)
        stats["payments"] += _upsert(con, "toast_payments", pays)

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
    # inventories from the API when the endpoint is enabled (shape TBD -> stored as-is per bucket if it has categoryType/value)
    for slug, ds, p, j in iter_raw("marginedge", dataset="inventories"):
        rows = []
        for inv in j.get("inventories", []):
            for c in inv.get("categories", []) or []:
                rows.append({"location_id": slug, "count_date": inv.get("inventoryDate") or inv.get("date"), "bucket": bk.from_me(c.get("categoryType"), c.get("categoryName")), "value": float(c.get("value") or c.get("totalValue") or 0), "source": "api"})
        _upsert(con, "me_inventory_counts", rows)
    return stats


# ---------------------------------------------------------------- manual inputs

def load_inputs(con) -> dict:
    a = [{"activation_id": r["activation_id"], "location_id": r["location_id"], "start_date": r["start_date"], "end_date": r.get("end_date") or r["start_date"], "name": r["name"], "type": r.get("type"),
          "cost": float(r.get("cost") or 0), "owner": r.get("owner"), "notes": r.get("notes")} for r in inputs.read_activations()]
    con.execute("DELETE FROM activations"); _upsert(con, "activations", a)
    t = [{"location_id": r["location_id"], "month": r["month"], "sales_target": float(r.get("sales_target") or 0) or None, "cogs_pct_target": float(r.get("cogs_pct_target") or 0) or None,
          "labor_pct_target": float(r.get("labor_pct_target") or 0) or None, "guests_target": int(float(r.get("guests_target") or 0)) or None} for r in inputs.read_targets()]
    _upsert(con, "targets", t)
    inv = [{"location_id": r["location_id"], "count_date": r["count_date"], "bucket": r["bucket"], "value": float(r.get("value") or 0), "source": r.get("source") or "csv"} for r in inputs.read_inventory_counts()]
    _upsert(con, "me_inventory_counts", inv)
    return {"activations": len(a), "targets": len(t), "inventory_counts": len(inv)}


# ---------------------------------------------------------------- derived

def rebuild_daily_summary(con):
    con.execute("DELETE FROM daily_summary")
    con.execute("""
    INSERT INTO daily_summary (location_id, business_date, net_sales, gross_sales, discounts, tax, tips, refunds, orders, checks, guests,
      sales_food, sales_beer, sales_liquor, sales_wine, sales_nabev, sales_retail, sales_other, labor_hours, labor_cost,
      purchases, purch_food, purch_beer, purch_liquor, purch_wine, purch_nabev, purch_other)
    WITH days AS (
      SELECT location_id, business_date FROM toast_orders
      UNION SELECT location_id, business_date FROM toast_time_entries
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
    t AS (SELECT location_id, business_date, SUM(regular_hours+overtime_hours) hrs, SUM(wages) cost FROM toast_time_entries GROUP BY 1,2),
    p AS (SELECT location_id, invoice_date business_date,
                 SUM(CASE WHEN bucket='Food' THEN line_price ELSE 0 END) f, SUM(CASE WHEN bucket='Beer' THEN line_price ELSE 0 END) b, SUM(CASE WHEN bucket='Liquor' THEN line_price ELSE 0 END) l,
                 SUM(CASE WHEN bucket='Wine' THEN line_price ELSE 0 END) w, SUM(CASE WHEN bucket='NA Bev' THEN line_price ELSE 0 END) n,
                 SUM(CASE WHEN bucket NOT IN ('Food','Beer','Liquor','Wine','NA Bev') THEN line_price ELSE 0 END) x
          FROM me_invoice_lines GROUP BY 1,2),
    ph AS (SELECT location_id, invoice_date business_date, SUM(order_total) tot FROM me_invoices GROUP BY 1,2)
    SELECT d.location_id, d.business_date,
      COALESCE(o.net,0), COALESCE(o.gross,0), COALESCE(o.disc,0), COALESCE(o.tax,0), COALESCE(o.tips,0), COALESCE(o.ref,0), COALESCE(o.orders,0), COALESCE(o.checks,0), COALESCE(o.guests,0),
      COALESCE(i.f,0), COALESCE(i.b,0), COALESCE(i.l,0), COALESCE(i.w,0), COALESCE(i.n,0), COALESCE(i.r,0), COALESCE(i.x,0),
      COALESCE(t.hrs,0), COALESCE(t.cost,0),
      COALESCE(ph.tot,0), COALESCE(p.f,0), COALESCE(p.b,0), COALESCE(p.l,0), COALESCE(p.w,0), COALESCE(p.n,0), COALESCE(p.x,0)
    FROM days d
    LEFT JOIN o ON o.location_id=d.location_id AND o.business_date=d.business_date
    LEFT JOIN i ON i.location_id=d.location_id AND i.business_date=d.business_date
    LEFT JOIN t ON t.location_id=d.location_id AND t.business_date=d.business_date
    LEFT JOIN p ON p.location_id=d.location_id AND p.business_date=d.business_date
    LEFT JOIN ph ON ph.location_id=d.location_id AND ph.business_date=d.business_date
    """)


def run() -> dict:
    cfg = settings()
    bk = Buckets(cfg["category_map"])
    con = connect()
    _upsert(con, "locations", [{"location_id": l["slug"], "name": l["name"], "short": l.get("short"), "toast_guid": l.get("toast_guid"), "marginedge_unit_id": str(l.get("marginedge_unit_id") or ""),
                                "timezone": l.get("timezone"), "opened": l.get("opened"), "state": l.get("state")} for l in locations()])
    s = {"toast": load_toast(con, bk), "marginedge": load_marginedge(con, bk), "inputs": load_inputs(con)}
    rebuild_daily_summary(con)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('last_transform', ?)", (datetime.utcnow().isoformat(timespec="seconds") + "Z",))
    con.commit()
    n = con.execute("SELECT COUNT(*), MIN(business_date), MAX(business_date) FROM daily_summary").fetchone()
    log.info("transform: %s | daily_summary rows=%d range=%s..%s", s, *n)
    con.close()
    return s
