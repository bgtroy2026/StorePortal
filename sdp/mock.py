"""Mock mode: writes raw/ files in the *same shapes the real APIs return*, so transform/build run unchanged.

Deterministic (seeded) so re-runs produce identical output. Volumes are scaled to keep local runs fast;
the shapes follow the Toast Orders/Labor/Menus APIs and the MarginEdge Orders/Products/Categories/Vendors APIs.
"""
from __future__ import annotations

import hashlib
import math
import random
import uuid
from datetime import date, datetime, timedelta

from .util import iso, log, today_local, write_raw

BUCKETS = ["Food", "Beer", "Liquor", "Wine", "NA Bev", "Retail"]

MENU = [  # (name, menu group, sales category, price, bucket)
    ("Easy Eddy IPA", "Draft", "Draft Beer", 7.0, "Beer"), ("Boomtown Pale Ale", "Draft", "Draft Beer", 6.5, "Beer"),
    ("Citrus Surfer", "Draft", "Draft Beer", 7.0, "Beer"), ("Tigerhawk IPA", "Draft", "Draft Beer", 7.5, "Beer"),
    ("A Real Nice Surprise", "Draft", "Draft Beer", 7.5, "Beer"), ("Tigerhawk 4pk 16oz", "Packaged", "Packaged Beer", 14.0, "Beer"),
    ("Easy Eddy 6pk", "Packaged", "Packaged Beer", 12.0, "Beer"), ("Flight (4)", "Draft", "Draft Beer", 12.0, "Beer"),
    ("Smash Burger", "Entrees", "Food", 15.0, "Food"), ("Margherita Pizza", "Pizza", "Food", 16.0, "Food"),
    ("Pepperoni Pizza", "Pizza", "Food", 17.0, "Food"), ("Wings (10)", "Apps", "Food", 15.0, "Food"),
    ("Pretzel & Beer Cheese", "Apps", "Food", 11.0, "Food"), ("Caesar Salad", "Entrees", "Food", 12.0, "Food"),
    ("Fish Tacos", "Entrees", "Food", 15.0, "Food"), ("Kids Cheese Pizza", "Kids", "Food", 8.0, "Food"),
    ("Brownie Sundae", "Desserts", "Food", 8.0, "Food"), ("Fries", "Apps", "Food", 5.0, "Food"),
    ("Old Fashioned", "Cocktails", "Liquor", 12.0, "Liquor"), ("Margarita", "Cocktails", "Liquor", 11.0, "Liquor"),
    ("Vodka Soda", "Cocktails", "Liquor", 9.0, "Liquor"), ("Espresso Martini", "Cocktails", "Liquor", 13.0, "Liquor"),
    ("House Red", "Wine", "Wine", 9.0, "Wine"), ("House White", "Wine", "Wine", 9.0, "Wine"), ("Prosecco", "Wine", "Wine", 10.0, "Wine"),
    ("Fountain Soda", "NA", "NA Bev", 3.0, "NA Bev"), ("Sparkling Water", "NA", "NA Bev", 3.5, "NA Bev"), ("Root Beer", "NA", "NA Bev", 4.0, "NA Bev"),
    ("Logo Tee", "Merch", "Retail", 28.0, "Retail"), ("Trucker Hat", "Merch", "Retail", 30.0, "Retail"), ("Pint Glass", "Merch", "Retail", 8.0, "Retail"),
]
JOBS = [("Server", 7.25), ("Bartender", 8.0), ("Line Cook", 17.0), ("Prep Cook", 15.5), ("Host", 12.0), ("Manager", 26.0), ("Dish", 14.5)]
VENDORS = [("Sysco", "Food"), ("US Foods", "Food"), ("Capital City Fruit", "Food"), ("Big Grove Production", "Beer"),
           ("Johnson Brothers", "Liquor"), ("Southern Glazer's", "Wine"), ("Coca-Cola Bottling", "NA Bev"), ("Ecolab", "Other"), ("Big Grove Merch", "Retail")]
DINING = ["DINE_IN", "TAKE_OUT", "DINE_IN", "DINE_IN", "ONLINE", "DINE_IN", "BAR"]
FIRST_NAMES = ["Avery", "Brooke", "Caleb", "Dana", "Eli", "Faith", "Gus", "Hana", "Ivan", "Jules", "Kara", "Liam", "Mia", "Nora"]
LAST_NAMES = ["Alder", "Boone", "Cruz", "Diaz", "Ellis", "Ford", "Gray", "Hart", "Ingram", "Jansen", "Keller", "Lowe", "Meyer", "Nash"]


def _g(seed: str) -> str:
    """Stable pseudo-GUID from a seed string."""
    h = hashlib.md5(seed.encode()).hexdigest()
    return str(uuid.UUID(h))


def _ts(d: date, hour: float) -> str:
    base = datetime(d.year, d.month, d.day) + timedelta(hours=hour)
    return base.strftime("%Y-%m-%dT%H:%M:%S.000-0500")


def _daily_volume(loc_idx: int, d: date, rnd: random.Random) -> int:
    base = [140, 190, 120, 160, 110, 95][loc_idx % 6]
    dow = [0.75, 0.7, 0.8, 0.95, 1.35, 1.55, 1.2][d.weekday()]           # Mon..Sun
    season = 1 + 0.22 * math.sin((d.timetuple().tm_yday - 100) / 365 * 2 * math.pi)  # summer peak
    growth = 1 + 0.06 * ((d - date(2025, 1, 1)).days / 365)
    return max(15, int(base * dow * season * growth * rnd.uniform(0.85, 1.15)))


def gen_toast(loc: dict, loc_idx: int, start: date, end: date, events: dict[str, float], write: bool = True):
    """Returns (n_orders, n_time_entries, day_sales{date:{bucket:net}}, day_labor{date:cost})."""
    slug = loc["slug"]
    guid = loc.get("toast_guid") or _g(f"toast:{slug}")
    rnd = random.Random(f"toast:{slug}")
    info = {"guid": guid, "general": {"name": f"Big Grove Brewery {loc['name']}", "locationName": loc["name"], "timeZone": loc.get("timezone", "America/Chicago"), "closeoutHour": 4, "firstBusinessDate": 20130901},
            "location": {"city": loc["name"], "stateCode": loc.get("state", "IA")}}
    W = (lambda *a: write_raw(*a)) if write else (lambda *a: None)
    W("toast", slug, "restaurant", "info", info)
    jobs = [{"guid": _g(f"job:{slug}:{t}"), "title": t, "wageFrequency": "HOURLY", "defaultWage": w, "deleted": False} for t, w in JOBS]
    W("toast", slug, "jobs", "all", {"jobs": jobs})
    items = [{"guid": _g(f"item:{slug}:{n}"), "name": n, "price": p, "menuGroup": g, "salesCategory": {"name": sc}} for n, g, sc, p, b in MENU]
    menus = {"restaurantGuid": guid, "menus": [{"name": "Main", "menuGroups": [
        {"name": g, "menuItems": [{"guid": it["guid"], "name": it["name"], "price": it["price"], "salesCategory": it["salesCategory"]} for it in items if it["menuGroup"] == g]}
        for g in sorted({m[1] for m in MENU})]}]}
    W("toast", slug, "menus", "all", menus)
    # Lookup tables, mirroring config/v2 and labor/v1/employees, so mock mode exercises the same guid ->
    # name resolution the real pull depends on (the bug that made the portal show raw guids).
    W("toast", slug, "config-diningOptions", "all",
      {"diningOptions": [{"guid": _g("do:" + b), "name": b.replace("_", " ").title(), "behavior": b} for b in sorted(set(DINING))]})
    W("toast", slug, "config-revenueCenters", "all",
      {"revenueCenters": [{"guid": _g(f"rc:{slug}:{n}"), "name": n} for n in ("Bar", "Dining", "Patio")]})
    W("toast", slug, "config-salesCategories", "all",
      {"salesCategories": [{"guid": _g(f"sc:{slug}:{c}"), "name": c} for c in sorted({m[2] for m in MENU})]})
    W("toast", slug, "config-voidReasons", "all",
      {"voidReasons": [{"guid": _g(f"vr:{slug}:{n}"), "name": n} for n in ("Server error", "Kitchen error", "Guest changed mind", "Walkout")]})
    W("toast", slug, "config-discounts", "all",
      {"discounts": [{"guid": _g(f"dc:{slug}:{n}"), "name": n} for n in ("Happy Hour", "Employee Meal", "Manager Comp", "Loyalty Reward")]})
    W("toast", slug, "config-payoutReasons", "all",
      {"payoutReasons": [{"guid": _g(f"pr:{slug}:{n}"), "name": n} for n in ("Supplies", "Delivery tip", "Repair")]})
    W("toast", slug, "config-noSaleReasons", "all",
      {"noSaleReasons": [{"guid": _g(f"nsr:{slug}:{n}"), "name": n} for n in ("Change for guest", "Opened in error")]})
    W("toast", slug, "config-cashDrawers", "all",
      {"cashDrawers": [{"guid": _g(f"drawer:{slug}"), "name": "Main drawer"}]})
    W("toast", slug, "config-serviceAreas", "all",
      {"serviceAreas": [{"guid": _g(f"sa:{slug}:{n}"), "name": n} for n in ("Main Floor", "Patio", "Upstairs")]})

    # One location deliberately has NO schedule in Toast — plenty of restaurants build the rota elsewhere, and
    # the portal must hide the comparison for them rather than imply perfect adherence.
    sched = None if slug == "solon" else []
    day_sales, day_labor = {}, {}
    servers = [_g(f"emp:{slug}:{i}") for i in range(14)]
    W("toast", slug, "employees", "all", {"employees": [
        {"guid": g, "firstName": FIRST_NAMES[i % len(FIRST_NAMES)], "lastName": LAST_NAMES[i % len(LAST_NAMES)],
         "email": None, "externalEmployeeId": f"{slug}-{i:03d}", "deleted": False, "disabled": False,
         "jobReferences": [{"guid": _g(f"job:{slug}:{JOBS[i % len(JOBS)][0]}")}]}
        for i, g in enumerate(servers)]})
    n_orders_total = 0
    d = start
    while d <= end:
        n = _daily_volume(loc_idx, d, rnd)
        lift = events.get(f"{slug}:{iso(d)}", 1.0)
        n = int(n * lift)
        orders = []
        for i in range(n):
            hour = rnd.choice([11.5, 12, 12.5, 13, 14, 16, 17, 17.5, 18, 18.5, 19, 19.5, 20, 21, 22]) + rnd.uniform(0, 0.9)
            guests = rnd.choice([1, 1, 2, 2, 2, 3, 4, 4, 5, 6])
            og = _g(f"order:{slug}:{iso(d)}:{i}")
            cg = _g(f"check:{slug}:{iso(d)}:{i}")
            voided = rnd.random() < 0.012
            sels, amount, tax = [], 0.0, 0.0
            k = max(1, int(guests * rnd.uniform(1.0, 2.2)))
            for j in range(k):
                name, grp, sc, price, b = rnd.choices(MENU, weights=[9, 6, 5, 8, 4, 3, 3, 4, 7, 6, 5, 5, 5, 3, 4, 2, 2, 4, 3, 3, 3, 2, 2, 2, 1, 4, 2, 2, 0.6, 0.5, 0.8])[0]
                q = 1 if b != "Beer" else rnd.choice([1, 1, 1, 2])
                pre = round(price * q, 2)
                disc = round(pre * 0.5, 2) if (rnd.random() < 0.04) else 0.0
                line = round(pre - disc, 2)
                t = round(line * 0.07, 2)
                amount += line; tax += t
                if not voided: day_sales.setdefault(iso(d), {})[b] = day_sales.setdefault(iso(d), {}).get(b, 0) + line
                sels.append({"guid": _g(f"sel:{og}:{j}"), "entityType": "MenuItemSelection", "item": {"guid": _g(f"item:{slug}:{name}")}, "itemGroup": {"guid": _g(f"grp:{slug}:{grp}")},
                             "salesCategory": {"guid": _g(f"sc:{slug}:{sc}"), "name": sc}, "displayName": name, "quantity": q, "preDiscountPrice": pre, "price": line, "tax": t,
                             "voided": False, "createdDate": _ts(d, hour + 0.1 * j), "appliedDiscounts": ([{"guid": _g(f"disc:{og}:{j}"), "discountAmount": disc, "name": "Happy Hour", "discountType": "PERCENT"}] if disc else [])})
            # Toast includes service charges INSIDE the check amount, so net sales contains them while the
            # category split cannot. Mock used to add the charge alongside the amount instead of into it,
            # which made the mix add up perfectly here and not in production — exactly the kind of
            # well-behaved fiction that lets a real defect through. Model it the way Toast does.
            svc = round(amount * 0.18, 2) if guests >= 6 else 0.0
            # One check in fifty has an item rung, discounted and then voided. Toast excludes it from sales and
            # discounts; nothing in mock used to produce this shape, so the bug it causes could not be seen.
            if rnd.random() < 0.02:
                sels.append({"guid": _g(f"sel:{og}:void"), "entityType": "MenuItemSelection", "item": {"guid": _g(f"item:{slug}:Voided")},
                             "salesCategory": {"guid": _g(f"sc:{slug}:Food"), "name": "Food"}, "displayName": "Rung then voided",
                             "quantity": 1, "preDiscountPrice": 12.0, "price": 0.0, "tax": 0.0, "voided": True,
                             "createdDate": _ts(d, hour + 0.05), "appliedDiscounts": [{"guid": _g(f"disc:{og}:void"), "discountAmount": 12.0, "name": "Void comp", "discountType": "OPEN"}]})
            amount, tax = round(amount + svc, 2), round(tax, 2)
            total = round(amount + tax, 2)
            tip = round(total * rnd.choice([0, 0.15, 0.18, 0.2, 0.2, 0.22, 0.25]), 2)
            ptype = rnd.choices(["CREDIT", "CASH", "GIFTCARD", "OTHER"], weights=[82, 12, 4, 2])[0]
            refund = round(total, 2) if (not voided and rnd.random() < 0.004) else 0.0
            pay = {"guid": _g(f"pay:{og}"), "type": ptype, "cardType": ("VISA" if ptype == "CREDIT" else None), "amount": total, "tipAmount": tip, "paidDate": _ts(d, hour + 0.8),
                   "refundStatus": ("FULL" if refund else "NONE"), "refund": ({"refundAmount": refund, "tipRefundAmount": 0, "refundDate": _ts(d + timedelta(days=1), 10)} if refund else None)}
            orders.append({"guid": og, "entityType": "Order", "businessDate": int(d.strftime("%Y%m%d")), "openedDate": _ts(d, hour), "closedDate": _ts(d, hour + 0.9), "modifiedDate": _ts(d, hour + 1),
                           "diningOption": {"guid": _g("do:" + rnd.choice(DINING)), "behavior": rnd.choice(DINING)}, "revenueCenter": {"guid": _g(f"rc:{slug}:{'Bar' if hour > 20 else rnd.choice(['Dining', 'Dining', 'Patio'])}")},
                           "server": {"guid": rnd.choice(servers)}, "numberOfGuests": guests, "voided": voided, "voidDate": (_ts(d, hour + 0.5) if voided else None),
                           "checks": [{"guid": cg, "entityType": "Check", "amount": amount, "taxAmount": tax, "totalAmount": total, "voided": voided, "paymentStatus": "CLOSED",
                                       "selections": sels, "payments": [pay], "appliedDiscounts": [], "appliedServiceCharges": ([{"chargeAmount": svc, "gratuity": True, "name": "Auto grat"}] if svc else [])}]})
        W("toast", slug, "orders", iso(d), {"businessDate": iso(d), "orders": orders})
        n_orders_total += n
        d += timedelta(days=1)
    # time entries in 30-day windows, ~sales-scaled
    tes = []
    d = start
    while d <= end:
        vol = _daily_volume(loc_idx, d, rnd)
        shifts = max(6, int(vol / 5.2))
        for i in range(shifts):
            title, wage = rnd.choices(JOBS, weights=[8, 5, 5, 3, 2, 1.5, 3])[0]
            hrs = rnd.choice([4, 5, 6, 6.5, 7, 8, 8.5])
            ot = 0.5 if hrs > 8 else 0
            start_h = rnd.choice([9, 10, 11, 14, 15, 16, 17])
            emp = rnd.choice(servers)
            day_labor[iso(d)] = day_labor.get(iso(d), 0) + (hrs - ot) * wage + ot * wage * 1.5
            tes.append({"guid": _g(f"te:{slug}:{iso(d)}:{i}"), "employeeReference": {"guid": emp}, "jobReference": {"guid": _g(f"job:{slug}:{title}")},
                        "inDate": _ts(d, start_h), "outDate": _ts(d, start_h + hrs), "businessDate": d.strftime("%Y%m%d"),
                        "regularHours": hrs - ot, "overtimeHours": ot, "hourlyWage": wage, "declaredCashTips": (round(rnd.uniform(0, 40), 2) if title in ("Server", "Bartender") else 0),
                        "nonCashTips": (round(rnd.uniform(40, 220), 2) if title in ("Server", "Bartender") else 0), "deleted": False})
            # The matching SCHEDULED shift: mostly the same, but with the drift a real schedule has — people
            # clock in a few minutes either side of their start and stay a little past the end.
            if sched is not None and rnd.random() > 0.05:          # ~5% worked with no shift on the schedule
                s_start = start_h - rnd.choice([0, 0, 0, 0.25, -0.25, 0.5])
                s_hrs = hrs - rnd.choice([0, 0, 0, 0.5, -0.5])
                sched.append({"guid": _g(f"sh:{slug}:{iso(d)}:{i}"), "employeeReference": {"guid": emp},
                              "jobReference": {"guid": _g(f"job:{slug}:{title}")},
                              "inDate": _ts(d, s_start), "outDate": _ts(d, s_start + s_hrs), "deleted": False})
        d += timedelta(days=1)
    W("toast", slug, "timeEntries", f"{iso(start)}_{iso(end)}", {"timeEntries": tes, "window": [iso(start), iso(end)]})

    # Cash management for the recent window only, mirroring the real pull. Includes reversals on purpose: an
    # entry whose `undoes` names an earlier one must remove BOTH from the totals, and that is the easiest part
    # of this to get quietly wrong.
    cd = max(start, end - timedelta(days=34))
    while cd <= end:
        ents, drawer = [], _g(f"drawer:{slug}")
        for k in range(rnd.randint(2, 6)):
            emp = rnd.choice(servers)
            # The vocabulary a real Toast account actually returns, in roughly the real proportions. Mock used
            # to emit PAY_OUT and CASH_IN — types this account never produces — while omitting CLOSE_OUT_EXACT
            # and under-weighting TIP_OUT and CASH_COLLECTED, which between them carry 96% of real entries.
            # That is why a view built against the wrong vocabulary passed every test and published zeros.
            kind = rnd.choices(["TIP_OUT", "CASH_COLLECTED", "CLOSE_OUT_EXACT", "CLOSE_OUT_SHORTAGE",
                                "CLOSE_OUT_OVERAGE", "NO_SALE"],
                               weights=[58, 35, 2, 2, 1, 2])[0]
            amt = {"NO_SALE": 0.0, "CLOSE_OUT_EXACT": 0.0}.get(kind, round(rnd.uniform(2, 85), 2))
            e = {"guid": _g(f"cash:{slug}:{iso(cd)}:{k}"), "entityType": "CashEntry", "type": kind, "amount": amt,
                 "reason": None, "date": _ts(cd, rnd.randint(11, 23)), "cashDrawer": {"guid": drawer},
                 "employee1": {"guid": emp}, "undoes": None}

            if kind == "NO_SALE":
                e["noSaleReason"] = {"guid": _g(f"nsr:{slug}:{rnd.choice(['Change for guest', 'Opened in error'])}")}
            ents.append(e)
        if ents and rnd.random() < 0.18:                  # somebody corrected a mistake
            tgt = ents[0]
            ents.append({"guid": _g(f"cash:{slug}:{iso(cd)}:undo"), "entityType": "CashEntry", "type": "UNDO_CASH_COLLECTED",
                         "amount": tgt["amount"], "reason": "Entered twice", "date": _ts(cd, 23),
                         "cashDrawer": {"guid": drawer}, "employee1": {"guid": tgt["employee1"]["guid"]},
                         "undoes": tgt["guid"]})
        W("toast", slug, "cash", iso(cd), {"businessDate": iso(cd), "entries": ents})
        W("toast", slug, "deposits", iso(cd), {"deposits": ([{"guid": _g(f"dep:{slug}:{iso(cd)}"), "entityType": "Deposit",
            "amount": round(rnd.uniform(300, 2200), 2), "date": _ts(cd, 23), "employee": {"guid": rnd.choice(servers)},
            "undoes": None}] if rnd.random() > 0.15 else []), "businessDate": iso(cd)})
        cd += timedelta(days=1)
    if sched is not None:
        W("toast", slug, "shifts", f"{iso(start)}_{iso(end)}", {"shifts": sched, "window": [iso(start), iso(end)]})
    return n_orders_total, len(tes), day_sales, day_labor


def gen_marginedge(loc: dict, loc_idx: int, start: date, end: date, day_sales: dict, day_labor: dict):
    slug = loc["slug"]
    uid = loc.get("marginedge_unit_id") or str(1000 + loc_idx)
    rnd = random.Random(f"me:{slug}")
    cats = [{"categoryId": f"c{loc_idx}{i}", "categoryName": n, "categoryType": t, "accountingCode": 5000 + i * 10} for i, (n, t) in enumerate([
        ("Food - Protein", "Food"), ("Food - Produce", "Food"), ("Food - Dry Goods", "Food"), ("Food - Dairy", "Food"),
        ("Beer - Big Grove", "Beer"), ("Beer - Guest", "Beer"), ("Liquor", "Liquor"), ("Wine", "Wine"), ("N/A Bev", "N/A Bev"),
        ("Paper & Supplies", "Supplies"), ("Chemicals", "Supplies"), ("Retail", "Retail")])]
    write_raw("marginedge", slug, "categories", "all", {"categories": cats})
    vendors = [{"vendorId": f"v{loc_idx}{i}", "vendorName": n, "centralVendorId": f"cv{i}", "vendorAccounts": [{"vendorAccountNumber": f"BG-{loc_idx}{i:02d}"}]} for i, (n, b) in enumerate(VENDORS)]
    write_raw("marginedge", slug, "vendors", "all", {"vendors": vendors})
    cat_by_bucket = {}
    for c in cats:
        cat_by_bucket.setdefault({"N/A Bev": "NA Bev", "Supplies": "Other"}.get(c["categoryType"], c["categoryType"]), []).append(c["categoryId"])
    prods = []
    names = {"Food": ["Ground Beef 80/20", "Chicken Wings", "Mozzarella", "Romaine", "Tomatoes", "Pizza Flour", "Fry Oil", "Buns", "Pepperoni", "Butter"],
             "Beer": ["Easy Eddy 1/2 bbl", "Tigerhawk 1/2 bbl", "Citrus Surfer 1/6 bbl", "Tigerhawk 4pk", "Guest Lager 1/2 bbl"],
             "Liquor": ["Bourbon 1L", "Vodka 1L", "Tequila 1L", "Espresso Liqueur"], "Wine": ["House Red 750ml", "House White 750ml", "Prosecco 750ml"],
             "NA Bev": ["Bag-in-box Cola", "Sparkling Water cs", "Root Beer keg"], "Other": ["To-go boxes", "Napkins", "Sanitizer"], "Retail": ["Logo Tee", "Trucker Hat"]}
    for b, ns in names.items():
        for n in ns:
            cid = rnd.choice(cat_by_bucket.get(b, cat_by_bucket["Other"]))
            prods.append({"companyConceptProductId": f"p{loc_idx}-{abs(hash(n)) % 10000}", "centralProductId": f"cp-{abs(hash(n)) % 10000}", "productName": n,
                          "latestPrice": round(rnd.uniform(4, 180), 2), "reportByUnit": rnd.choice(["EACH", "POUND", "CASE", "KEG"]), "taxExempt": False, "itemCount": rnd.randint(1, 3),
                          "categories": [{"categoryId": cid, "percentAllocation": 100}]})
    write_raw("marginedge", slug, "products", "all", {"products": prods})
    prods_by_bucket = {}
    for p in prods:
        b = next((bb for bb, ids in cat_by_bucket.items() if p["categories"][0]["categoryId"] in ids), "Other")
        prods_by_bucket.setdefault(b, []).append(p)
    base_sales = [140, 190, 120, 160, 110, 95][loc_idx % 6] * 112  # ≈ weekly purchases scale (~30% of sales)
    orders, details = [], []
    d = start
    oid_n = 0
    while d <= end:
        for vname, vb in VENDORS:
            freq = {"Food": 0.42, "Beer": 0.28, "Liquor": 0.14, "Wine": 0.1, "NA Bev": 0.14, "Other": 0.08, "Retail": 0.04}[vb]
            if rnd.random() > freq:
                continue
            oid_n += 1
            oid = f"o{loc_idx}-{oid_n}"
            vid = next(v["vendorId"] for v in vendors if v["vendorName"] == vname)
            target = base_sales * {"Food": 0.19, "Beer": 0.22, "Liquor": 0.06, "Wine": 0.03, "NA Bev": 0.02, "Other": 0.03, "Retail": 0.03}[vb] / 7 / max(freq, 0.05)
            lines, total = [], 0.0
            pool = prods_by_bucket.get(vb) or prods_by_bucket["Other"]
            for j in range(rnd.randint(2, 7)):
                p = rnd.choice(pool)
                q = rnd.randint(1, 6)
                up = round(p["latestPrice"] * rnd.uniform(0.9, 1.1), 2)
                lp = round(q * up, 2)
                total += lp
                lines.append({"vendorItemCode": f"{vid}-{abs(hash(p['productName'])) % 999}", "vendorItemName": p["productName"], "companyConceptProductId": p["companyConceptProductId"],
                              "categoryId": p["categories"][0]["categoryId"], "packagingId": "pk1", "quantity": q, "unitPrice": up, "linePrice": lp})
            scale = target / max(total, 1)
            for l in lines:
                l["unitPrice"] = round(l["unitPrice"] * scale, 2); l["linePrice"] = round(l["linePrice"] * scale, 2)
            total = round(sum(l["linePrice"] for l in lines), 2)
            is_credit = rnd.random() < 0.03
            if is_credit:
                total = -round(total * 0.2, 2)
                for l in lines: l["linePrice"] = -abs(round(l["linePrice"] * 0.2, 2))
            hdr = {"orderId": oid, "invoiceNumber": f"INV{rnd.randint(100000, 999999)}", "vendorId": vid, "vendorName": vname, "customerNumber": f"BG-{loc_idx}",
                   "invoiceDate": iso(d), "createdDate": iso(d + timedelta(days=rnd.randint(0, 3))), "paymentAccount": rnd.choices(["Operating", "Operating", "Amex", "Petty cash"], weights=[70, 15, 10, 5])[0],
                   "orderTotal": total,
                   # A realistic spread: most invoices finish, a tail sits in review or unreviewed. The hygiene
                   # panel exists precisely for that tail, so mock has to produce one.
                   "status": rnd.choices(["CLOSED", "APPROVED", "REVIEW", "NEW"], weights=[70, 16, 9, 5])[0]}
            orders.append(hdr)
            details.append(dict(hdr, tax=0.0, deliveryCharges=0.0, otherCharges=0.0, creditAmount=(abs(total) if is_credit else 0.0), isCredit=is_credit, inputTaxCredits=0.0, attachments=[], lineItems=lines))
        d += timedelta(days=1)
    write_raw("marginedge", slug, "orders", f"{iso(start)}_{iso(end)}", {"orders": orders, "window": [iso(start), iso(end)]})
    for det in details:
        write_raw("marginedge", slug, "orderDetail", det["orderId"], det)

    # ---- daily sales report + daily P&L (what the Toast→MarginEdge integration produces) ----
    SALES_CATS = [("Food", "Food"), ("Beer", "Beer"), ("Liquor", "Liquor"), ("Wine", "Wine"), ("N/A Bev", "NA Bev"), ("Retail", "Retail")]
    purch_by_day = {}
    for det in details:
        for l in det["lineItems"]:
            b = next((bb for bb, ids in cat_by_bucket.items() if l["categoryId"] in ids), "Other")
            purch_by_day.setdefault(det["invoiceDate"], {})[b] = purch_by_day.setdefault(det["invoiceDate"], {}).get(b, 0) + l["linePrice"]
    hdr = {"restaurantUnitId": int(uid), "restaurantUnitName": f"Big Grove {loc['name']}", "companyId": 1, "companyName": "Big Grove Brewery", "conceptId": 1, "conceptName": "Taprooms", "currency": "USD"}
    d = start
    while d <= end:
        ds_ = day_sales.get(iso(d), {}); tot = sum(ds_.values()) or 0.0
        cats = [{"id": 900 + i, "name": n, "total": round(ds_.get(b, 0), 2), "percentOfTotalSales": round(ds_.get(b, 0) / tot, 4) if tot else 0} for i, (n, b) in enumerate(SALES_CATS)]
        write_raw("marginedge", slug, "salesReport", iso(d), {"salesReports": [dict(hdr, startDate=iso(d), endDate=iso(d), summary={"totalSales": round(tot, 2)}, categories=cats)]})
        labor = day_labor.get(iso(d), 0.0); pb = purch_by_day.get(iso(d), {})
        def sec(cats_):
            t = sum(c["total"] for c in cats_)
            return {"total": round(t, 2), "totalPercentOfSales": round(t / tot, 4) if tot else 0, "categories": cats_, "items": []}
        def cat(i, n, v, items=None):
            return {"id": i, "name": n, "total": round(v, 2), "percentOfSales": round(v / tot, 4) if tot else 0, "items": items or [{"name": n, "total": round(v, 2), "percentOfSales": round(v / tot, 4) if tot else 0}]}
        income = sec([cat(900 + i, n, ds_.get(b, 0)) for i, (n, b) in enumerate(SALES_CATS)])
        cogs = sec([cat(600 + i, {"NA Bev": "N/A Bev", "Other": "Paper & Supplies"}.get(b, b), pb.get(b, 0)) for i, b in enumerate(["Food", "Beer", "Liquor", "Wine", "NA Bev", "Other", "Retail"])])
        lab = sec([cat(801, "Hourly Labor", labor * 0.82, [{"name": "FOH Hourly", "total": round(labor * 0.45, 2), "percentOfSales": 0}, {"name": "BOH Hourly", "total": round(labor * 0.37, 2), "percentOfSales": 0}]),
                   cat(802, "Salaried Labor", labor * 0.10), cat(803, "Payroll Taxes & Benefits", labor * 0.08)])
        exp = sec([cat(701, "Occupancy", tot * 0.06), cat(702, "Utilities", tot * 0.025), cat(703, "Marketing", tot * 0.015), cat(704, "Repairs & Maintenance", tot * 0.01)])
        gp = income["total"] - cogs["total"]; prime = cogs["total"] + lab["total"]
        write_raw("marginedge", slug, "pnl", iso(d), {"profitAndLossReports": [dict(hdr, startDate=iso(d), endDate=iso(d),
            summary={"grossProfit": round(gp, 2), "grossProfitPercentOfSales": round(gp / tot, 4) if tot else 0, "primeCostTotal": round(prime, 2), "primeCostPercentOfSales": round(prime / tot, 4) if tot else 0,
                     "controllableProfit": round(gp - lab["total"] - exp["total"], 2), "controllableProfitPercentOfSales": round((gp - lab["total"] - exp["total"]) / tot, 4) if tot else 0},
            income=income, cogs=cogs, labor=lab, expenses=exp)]})
        d += timedelta(days=1)

    # ---- inventories: one count every two weeks, items valued per product ----
    invs, cnt = [], 0
    d = end
    while d >= start:
        cnt += 1
        iid = f"inv{loc_idx}-{cnt}"
        head = {"inventoryId": iid, "countsheetId": f"cs{loc_idx}", "countsheetName": "Full Store Count", "inventoryDate": iso(d), "status": "CLOSED", "closedDate": iso(d) + "T23:30:00Z",
                "firstClosedDate": iso(d) + "T23:30:00Z", "savedDate": iso(d) + "T23:30:00Z", "origin": "WEB"}
        sections, total = [], 0.0
        for si, (b, ps) in enumerate(prods_by_bucket.items()):
            items = []
            for pi, pr in enumerate(ps):
                q = rnd.uniform(0.5, 14) * (1.0 + 0.15 * math.sin(cnt))
                val = round(q * pr["latestPrice"], 2); total += val
                items.append({"itemId": f"{iid}-{si}-{pi}", "position": pi, "productId": pr["companyConceptProductId"], "productName": pr["productName"], "companyConceptProductId": pr["companyConceptProductId"],
                              "centralProductId": pr["centralProductId"], "quantity": round(q, 2), "price": pr["latestPrice"], "value": val, "unit": pr["reportByUnit"], "unitSize": 1, "productCodes": []})
            sections.append({"sectionId": f"{iid}-s{si}", "name": f"{b} storage", "position": si, "items": items})
        head["totalValue"] = round(total, 2)
        invs.append({k: v for k, v in head.items()})
        write_raw("marginedge", slug, "inventoryDetail", iid, dict(head, sections=sections))
        d -= timedelta(days=14)
    write_raw("marginedge", slug, "inventories", "list", {"inventories": invs, "window": [iso(start), iso(end)]})
    return len(orders)


EVENT_NAMES = ["Rehearsal Dinner", "Corporate Holiday Party", "Wedding Reception", "Birthday Party", "Retirement Party",
               "Company Happy Hour", "Baby Shower", "Fundraiser", "Beer Dinner", "Graduation Party", "Team Offsite", "Anniversary Dinner"]
EVENT_STATUS = ["Definite", "Definite", "Definite", "Tentative", "Prospect", "Closed/Lost"]
LEAD_STATUS = ["New", "Contacted", "Proposal Sent", "Won", "Lost"]


def gen_tripleseat(loc: dict, loc_idx: int, start: date, end: date, forward: date):
    """Private events in the Tripleseat response shape, including the booked-but-future calendar."""
    slug = loc["slug"]
    rnd = random.Random(f"ts:{slug}")
    tid = str(3000 + loc_idx)
    scale = [1.0, 1.4, 0.8, 1.1, 0.7, 0.6][loc_idx % 6]
    events, leads = [], []
    d, eid = start, 0
    while d <= forward:
        # a couple of events a week, heavier Fri/Sat and in Nov/Dec
        base = 0.30 * scale * (1.9 if d.weekday() in (4, 5) else 1.0) * (1.8 if d.month in (11, 12) else 1.0)
        for _ in range(1 if rnd.random() < base else 0):
            eid += 1
            guests = rnd.choice([12, 18, 20, 25, 30, 40, 50, 60, 80, 120])
            ppp = round(rnd.uniform(28, 68), 2)
            fb = round(guests * ppp, 2)
            rental = round(rnd.choice([0, 0, 150, 250, 500]), 2)
            total = round(fb + rental, 2)
            past = d <= end
            status = "Definite" if past else rnd.choices(EVENT_STATUS, weights=[45, 20, 15, 12, 6, 2])[0]
            events.append({"id": int(f"{loc_idx}{eid:04d}"), "name": rnd.choice(EVENT_NAMES), "status": status,
                           "location_id": int(tid), "booking_id": int(f"{loc_idx}9{eid:03d}"),
                           "event_date": d.strftime("%m/%d/%Y"), "event_date_iso8601": iso(d),
                           "event_start_iso8601": iso(d) + "T17:30:00-05:00", "event_end_iso8601": iso(d) + "T21:30:00-05:00",
                           "event_style": rnd.choice(["Buffet", "Plated", "Passed Apps", "Family Style"]),
                           "event_type_id": rnd.randint(1, 8), "guest_count": guests,
                           "guaranteed_guest_count": guests if past else 0,
                           "food_and_beverage_min": fb, "rental_fee": rental, "deposit_amount": round(total * 0.25, 2),
                           "grand_total": total,
                           "actual_amount": (round(total * rnd.uniform(0.9, 1.25), 2) if past and status == "Definite" else None),
                           "amount_due": (0.0 if past else round(total * 0.75, 2)), "price_per_person": ppp,
                           "created_at": iso(d - timedelta(days=rnd.randint(20, 120))) + "T10:00:00-05:00",
                           "updated_at": iso(min(d, end)) + "T10:00:00-05:00", "deleted_at": None})
        if rnd.random() < base * 1.6:
            leads.append({"id": int(f"{loc_idx}8{len(leads):04d}"), "first_name": rnd.choice(["Sam", "Alex", "Jordan", "Casey", "Riley", "Morgan"]),
                          "last_name": rnd.choice(["Nguyen", "Patel", "Johnson", "Garcia", "Smith", "Olson"]),
                          "company": rnd.choice(["", "Hills Bank", "ACT", "Collins Aerospace", "UIHC", "Kum & Go", "", ""]),
                          "location_id": int(tid), "event_date": iso(d), "guest_count": rnd.choice([10, 20, 30, 45, 60, 100]),
                          "status": rnd.choices(LEAD_STATUS, weights=[22, 20, 18, 28, 12])[0],
                          "lead_source": {"name": rnd.choice(["Website", "Referral", "Phone", "Walk-in", "Repeat client"])},
                          "event_description": rnd.choice(EVENT_NAMES), "created_at": iso(d - timedelta(days=rnd.randint(10, 90))) + "T09:00:00-05:00",
                          "updated_at": iso(min(d, end)) + "T09:00:00-05:00"})
        d += timedelta(days=1)
    write_raw("tripleseat", slug, "events", f"{iso(start)}_{iso(forward)}", {"events": events, "window": [iso(start), iso(forward)], "location_id": tid})
    write_raw("tripleseat", slug, "leads", f"{iso(start)}_{iso(forward)}", {"leads": leads, "window": [iso(start), iso(forward)], "location_id": tid})
    return len(events), len(leads)


def generate(locations: list[dict], days: int = 120, toast: bool = True) -> None:
    end = today_local() - timedelta(days=1)
    start = end - timedelta(days=days)
    write_raw("marginedge", "_all", "restaurantUnits", "units", {"restaurants": [{"id": int(l.get("marginedge_unit_id") or 1000 + i), "name": f"Big Grove {l['name']}"} for i, l in enumerate(locations)]})
    # sales lift on activation days so the overlay has something to show (mirrors inputs/activations.csv sample)
    events = {}
    from .inputs import read_activations
    for a in read_activations():
        s = date.fromisoformat(a["start_date"]); e = date.fromisoformat(a.get("end_date") or a["start_date"])
        for dd in (s + timedelta(n) for n in range((e - s).days + 1)):
            events[f"{a['location_id']}:{iso(dd)}"] = 1.0 + float(a.get("_mock_lift") or 0.25)
    write_raw("tripleseat", "_all", "locations", "all",
              {"locations": [{"id": 3000 + i, "name": l.get("tripleseat_name") or l["name"]} for i, l in enumerate(locations)]})
    forward = end + timedelta(days=180)
    for i, loc in enumerate(locations):
        no, nt, day_sales, day_labor = gen_toast(loc, i, start, end, events, write=toast)
        ni = gen_marginedge(loc, i, start, end, day_sales, day_labor)
        ne, nl = gen_tripleseat(loc, i, start, end, forward)
        log.info("mock %-13s toast orders=%d time entries=%d%s | marginedge invoices=%d + daily sales/P&L + inventories | tripleseat events=%d leads=%d",
                 loc["slug"], no, nt, "" if toast else " (not written: --mock-no-toast)", ni, ne, nl)
