"""Store Director Portal pipeline CLI.

  python -m sdp pull        [--mock [--mock-no-toast]] [--source toast|marginedge|all] [--backfill] [--max-minutes N]
  python -m sdp transform
  python -m sdp build       [--dev-json] [--no-encrypt]
  python -m sdp all         [--mock] ...            pull -> transform -> build
  python -m sdp restore | persist                   warehouse state <-> GitHub release asset
  python -m sdp me-units                            list MarginEdge restaurant units visible to the key
  python -m sdp toast-restaurants                   list Toast restaurants visible to the client

Env: MARGINEDGE_API_KEY, TOAST_CLIENT_ID, TOAST_CLIENT_SECRET, PORTAL_SECRET (see docs/SETUP.md)
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

from .util import DB_PATH, env, locations, log, settings


def _warehouse_days() -> set[tuple[str, str]]:
    if not DB_PATH.exists():
        return set()
    con = sqlite3.connect(DB_PATH)
    try:
        return set(con.execute("SELECT DISTINCT location_id, business_date FROM toast_orders").fetchall())
    except sqlite3.OperationalError:
        return set()
    finally:
        con.close()


def _me_have() -> dict:
    """What the warehouse already holds from MarginEdge, so pulls only fetch the incremental window + gaps."""
    if not DB_PATH.exists():
        return {}
    con = sqlite3.connect(DB_PATH)
    q = lambda sql: set(con.execute(sql).fetchall())
    try:
        return {"orders": q("SELECT DISTINCT location_id, order_id FROM me_invoice_lines"),
                "sales_days": q("SELECT DISTINCT location_id, business_date FROM me_sales_daily"),
                "pnl_days": q("SELECT DISTINCT location_id, business_date FROM me_pnl_summary"),
                "inventories": q("SELECT location_id, inventory_id, COALESCE(saved_date, closed_date, '') FROM me_inventories WHERE inventory_id IN (SELECT DISTINCT inventory_id FROM me_inventory_items)")}
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()


def cmd_pull(a):
    locs = locations()
    cfg = settings()
    if a.mock:
        from . import mock
        mock.generate(locs, days=a.mock_days, toast=not a.mock_no_toast)
        return
    if a.source in ("all", "marginedge"):
        if env("MARGINEDGE_API_KEY"):
            from . import marginedge
            me_cfg = cfg["marginedge"]
            marginedge.pull(locs, days_back=cfg["backfill_days"], incremental_days=cfg["incremental_days"], have=({} if a.backfill else _me_have()),
                            max_minutes=a.max_minutes or me_cfg.get("max_minutes_per_run"))
        else:
            log.warning("MARGINEDGE_API_KEY not set — skipping MarginEdge")
    if a.source in ("all", "toast"):
        if env("TOAST_CLIENT_ID") and env("TOAST_CLIENT_SECRET"):
            from . import toast
            toast.pull(locs, days_back=cfg["backfill_days"], incremental_days=cfg["incremental_days"], warehouse_days=(set() if a.backfill else _warehouse_days()))
        else:
            log.warning("TOAST_CLIENT_ID / TOAST_CLIENT_SECRET not set — skipping Toast")


def cmd_transform(a):
    from . import transform
    transform.run()


def cmd_build(a):
    from . import build_site
    secret = None if a.no_encrypt else env("PORTAL_SECRET")
    if not secret and not a.no_encrypt:
        log.warning("PORTAL_SECRET not set — building without encrypted bundles (dev only)")
    build_site.run(secret=secret, dev_json=a.dev_json or a.no_encrypt)


def cmd_all(a):
    cmd_pull(a); cmd_transform(a); cmd_build(a)


def cmd_restore(a):
    from . import state
    state.restore()


def cmd_persist(a):
    from . import state
    state.persist()


def cmd_me_units(a):
    from .marginedge import MarginEdge
    for u in MarginEdge().restaurant_units():
        print(f"{u.get('id')}\t{u.get('name')}")


def cmd_toast_restaurants(a):
    from .toast import Toast
    for r in Toast().accessible_restaurants():
        print(f"{r.get('restaurantGuid') or r.get('guid')}\t{r.get('restaurantName') or r.get('name')}\t{r.get('locationName','')}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sdp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("pull", cmd_pull), ("transform", cmd_transform), ("build", cmd_build), ("all", cmd_all), ("restore", cmd_restore), ("persist", cmd_persist),
                     ("me-units", cmd_me_units), ("toast-restaurants", cmd_toast_restaurants)]:
        p = sub.add_parser(name); p.set_defaults(fn=fn)
        p.add_argument("--mock", action="store_true", help="generate sample raw data instead of calling APIs")
        p.add_argument("--mock-days", type=int, default=120)
        p.add_argument("--mock-no-toast", action="store_true", help="mock the MarginEdge-only phase (no Toast raw data)")
        p.add_argument("--max-minutes", type=float, default=None, help="stop the MarginEdge pull cleanly after N minutes (default from settings)")
        p.add_argument("--source", choices=["all", "toast", "marginedge"], default="all")
        p.add_argument("--backfill", action="store_true", help="pull the full backfill window even if a warehouse exists")
        p.add_argument("--dev-json", action="store_true", help="also write site/data/dev.json (unencrypted, local preview)")
        p.add_argument("--no-encrypt", action="store_true", help="skip bundles; write dev.json only")
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
