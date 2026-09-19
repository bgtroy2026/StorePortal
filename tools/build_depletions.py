#!/usr/bin/env python3
"""Roll the VIP distributor exports up to brand x taproom market, for the portal's "taproom vs market" view.

Runs on the Mac (standard library only) against files the Sales Portal's daily refresh already downloads:

    python3 tools/build_depletions.py \\
        "../Big Grove Sales Portal/Yearly Distributor Data" \\
        "../Big Grove Sales Portal/_build/geocache.json" \\
        inputs/depletions.csv

What goes in:  account x item rows, year-to-date case equivalents this year and the same span last year.
What comes out: one row per taproom market x brand x premise (ON/OFF) — case equivalents and a count of buying
accounts. No account names, no addresses, no prices. That coarseness is deliberate: the output is committed to the
repository that builds the portal, and nothing finer than "Easy Eddy, on-premise, around Cedar Rapids" is needed
to compare a taproom with the market around it.

A market is every account within `radius_miles` of the taproom (config/markets.json), using the Sales Portal's
geocode cache. An account the cache has never seen falls back to a city-name match, and the summary printed at the
end says how many were placed each way and how many could not be placed at all.
"""
from __future__ import annotations

import csv
import glob
import json
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def brand_of(item: str) -> tuple[str, str] | None:
    """('Easy Eddy', 'draft'|'package') from 'Easy Eddy 1/2 Barrel Keg'. None for VIP's generic volume rows."""
    s = (item or "").strip()
    if not s or s.upper().startswith("99Z"):
        return None
    brand = re.split(r"\s+\d+(?:\.\d+)?\s*/\s*\d", s)[0]
    brand = re.sub(r"\s+\d+(?:\.\d+)?\s*(?:FIRKIN|oz).*$", "", brand, flags=re.I).strip()
    kind = "draft" if re.search(r"barrel|bbl|keg|firkin", s, re.I) else "package"
    return (brand, kind) if brand else None


def miles(a, b, c, d):
    p = math.pi / 180
    h = 0.5 - math.cos((c - a) * p) / 2 + math.cos(a * p) * math.cos(c * p) * (1 - math.cos((d - b) * p)) / 2
    return 7917.5 * math.asin(math.sqrt(h))


def main(src: str, geocache: str, out: str) -> int:
    locs = json.load(open(os.path.join(ROOT, "config", "locations.json")))["locations"]
    mk = json.load(open(os.path.join(ROOT, "config", "markets.json")))
    radius = float(mk.get("radius_miles", 12))
    per_loc = mk.get("radius_by_location") or {}
    cities = {k: {c.upper() for c in v} for k, v in (mk.get("cities") or {}).items()}
    cache = {}
    if geocache and os.path.exists(geocache):
        cache = (json.load(open(geocache)).get("cache") or {})
    agg: dict[tuple, list] = {}
    placed = {"geo": 0, "outside": 0, "city": 0, "none": 0}
    as_of = None
    files = sorted(glob.glob(os.path.join(src, "Yearly Distributor Data (*).csv")))
    if not files:
        print("no 'Yearly Distributor Data (*).csv' files in", src); return 1
    for f in files:
        rd = csv.reader(open(f, encoding="utf-8-sig", newline=""))
        next(rd, None)
        h = next(rd, None) or []
        ce = [i for i, c in enumerate(h) if c.strip().endswith("Case Equivs") and "thru" in c]
        if len(ce) < 2:
            print("skipped (no two 'Case Equivs' spans):", os.path.basename(f)); continue
        m = re.search(r"thru\s+(\d+)/(\d+)/(\d{4})", h[ce[0]])
        if m:
            d = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
            as_of = max(as_of or d, d)
        col = {n: h.index(n) for n in ("OnOff Premises", "Address", "City", "State", "Zip Code", "VIP Outlet ID", "Item Names") if n in h}
        if len(col) < 7:
            print("skipped (columns missing):", os.path.basename(f)); continue
        for r in rd:
            if len(r) <= ce[1]:
                continue
            b = brand_of(r[col["Item Names"]])
            if not b:
                continue
            try:
                ty, ly = float(r[ce[0]] or 0), float(r[ce[1]] or 0)
            except ValueError:
                continue
            if not ty and not ly:
                continue
            addr, city, st, z = (r[col[k]].strip().upper() for k in ("Address", "City", "State", "Zip Code"))
            hit = cache.get(f"{addr}|{city}|{st}|{z[:5]}") or cache.get(f"{addr}|{city}|{st}|{z}")
            markets = []
            if hit and isinstance(hit, (list, tuple)) and len(hit) >= 2 and hit[0] is not None and hit[1] is not None:
                markets = [l["slug"] for l in locs if l.get("lat") is not None
                           and miles(hit[0], hit[1], l["lat"], l["lon"]) <= float(per_loc.get(l["slug"], radius))]
                placed["geo" if markets else "outside"] += 1
            else:
                markets = [k for k, cs in cities.items() if city in cs and (not mk.get("states") or st in (mk["states"].get(k) or [st]))]
                placed["city" if markets else "none"] += 1
            prem = "ON" if r[col["OnOff Premises"]].strip().upper().startswith("ON") else "OFF"
            for slug in markets:
                o = agg.setdefault((slug, b[0], prem), [0.0, 0.0, set()])
                o[0] += ty; o[1] += ly
                if ty > 0:
                    o[2].add(r[col["VIP Outlet ID"]])
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["location_id", "brand", "premise", "ce_ty", "ce_ly", "accounts", "as_of"])
        for (slug, brand, prem), v in sorted(agg.items()):
            w.writerow([slug, brand, prem, round(v[0], 2), round(v[1], 2), len(v[2]), as_of or ""])
    by = {}
    for (slug, _, _), v in agg.items():
        by[slug] = by.get(slug, 0.0) + v[0]
    print(f"as of {as_of}: {len(agg)} rows -> {out}")
    print(f"account-item rows: {placed['geo']} inside a market by geocode, {placed['city']} by city name, "
          f"{placed['outside']} geocoded but outside every market, {placed['none']} with no geocode and no city match")
    for slug, ce_ in sorted(by.items()):
        print(f"  {slug:16} {ce_:>12,.0f} CE year to date within {float(per_loc.get(slug, radius)):g} miles")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__); sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
