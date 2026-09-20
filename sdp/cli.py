"""Store Director Portal pipeline CLI.

  python -m sdp pull        [--mock [--mock-no-toast]] [--source toast|marginedge|all] [--backfill] [--max-minutes N]
  python -m sdp transform
  python -m sdp check                               invariant checks against the loaded warehouse (exit 1 on failure)
  python -m sdp pull --incremental-days N            widen the forced re-pull window for one run (heals recent history)
  python -m sdp pull --source toast --heal           re-pull days that predate modifier / loyalty capture, newest first
  python -m sdp build       [--dev-json] [--no-encrypt]
  python -m sdp all         [--mock] ...            pull -> transform -> build
  python -m sdp restore | persist                   warehouse state <-> GitHub release asset
  python -m sdp me-units                            list MarginEdge restaurant units visible to the key
  python -m sdp me-diag                             MarginEdge connectivity diagnostic (never prints the key)
  python -m sdp toast-diag                          Toast connectivity diagnostic + restaurant GUIDs
  python -m sdp sc-test                             fetch the Leadership Scorecard once and print its shape
  python -m sdp ts-locations                        list Tripleseat locations (ids for config/locations.json)
  python -m sdp ts-auth-url                         print the one-time Tripleseat consent URL
  python -m sdp ts-exchange --code CODE             trade the consent code for a refresh token (run locally)
  python -m sdp toast-restaurants                   list Toast restaurants visible to the client

Env: MARGINEDGE_API_KEY, TOAST_CLIENT_ID, TOAST_CLIENT_SECRET, PORTAL_SECRET,
     TRIPLESEAT_CLIENT_ID, TRIPLESEAT_CLIENT_SECRET, TRIPLESEAT_REDIRECT_URI, TRIPLESEAT_REFRESH_TOKEN
     (see docs/SETUP.md)
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


def _unhealed_days() -> set[tuple[str, str]]:
    """Business days loaded BEFORE the warehouse kept Toast modifiers, loyalty identification and order source.

    Those fields are read out of the order JSON at transform time, and raw JSON is not kept between runs — so the
    only way a past day gains them is to be pulled again. `pull --heal` treats these days as missing. It is a
    deliberate, manual mode rather than part of the nightly run: a 400-day re-pull is hours of API calls, and the
    nightly run's job is to have yesterday's numbers published before anyone is at work.
    """
    if not DB_PATH.exists():
        return set()
    con = sqlite3.connect(DB_PATH)
    try:
        out = set(con.execute("""
            SELECT o.location_id, o.business_date FROM (SELECT DISTINCT location_id, business_date FROM toast_orders) o
            WHERE EXISTS (SELECT 1 FROM toast_order_items i WHERE i.location_id=o.location_id AND i.business_date=o.business_date)
              AND NOT EXISTS (SELECT 1 FROM toast_order_items i WHERE i.location_id=o.location_id AND i.business_date=o.business_date AND i.modifiers IS NOT NULL)""").fetchall())
    except sqlite3.OperationalError:
        return set()
    try:
        # Discount approver and void reason (2026-09-20). Only the last 60 days: nothing reads them further back
        # than the 28-day comps view, and a full second re-pull would be hours of API calls for no reader. In its
        # own try because `captured` does not exist until the first transform after the upgrade has migrated the
        # warehouse -- and a missing column here must not switch the whole heal off.
        out |= set(con.execute("""
            SELECT location_id, business_date FROM toast_discounts
            WHERE business_date >= date('now','-60 day')
            GROUP BY 1,2 HAVING MAX(COALESCE(captured,0))=0""").fetchall())
    except sqlite3.OperationalError:
        pass
    finally:
        con.close()
    return out


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
    # A widened window re-pulls days the warehouse already has. That is the point: a transform fix only reaches
    # a day when that day's raw JSON is read again, so healing history means deliberately re-fetching it.
    inc_days = a.incremental_days if getattr(a, "incremental_days", None) else cfg["incremental_days"]
    if inc_days != cfg["incremental_days"]:
        log.info("incremental window widened to %d days for this run (settings say %d)", inc_days, cfg["incremental_days"])

    # Each source is isolated: if one API is down or unauthorized the other still populates the warehouse,
    # and the build goes ahead with whatever arrived. The run fails only if every configured source failed.
    attempted, failed = [], []
    if a.source in ("all", "marginedge"):
        if env("MARGINEDGE_API_KEY"):
            attempted.append("marginedge")
            try:
                from . import marginedge
                me_cfg = cfg["marginedge"]
                marginedge.pull(locs, days_back=cfg["backfill_days"], incremental_days=inc_days, have=({} if a.backfill else _me_have()),
                                max_minutes=a.max_minutes or me_cfg.get("max_minutes_per_run"), phase=a.phase)
            except Exception as e:
                failed.append("marginedge"); log.error("MarginEdge pull failed (%s: %s) — continuing with other sources", type(e).__name__, e)
        else:
            log.warning("MARGINEDGE_API_KEY not set — skipping MarginEdge")
    if a.source in ("all", "toast"):
        if env("TOAST_CLIENT_ID") and env("TOAST_CLIENT_SECRET"):
            attempted.append("toast")
            try:
                from . import toast
                t_cfg = cfg["toast"]
                have_days = set() if a.backfill else _warehouse_days()
                if getattr(a, "heal", False) and not a.backfill:
                    stale = _unhealed_days()
                    log.info("heal: %d location-days predate modifier/loyalty capture and will be re-pulled, newest first", len(stale))
                    have_days -= stale
                toast.pull(locs, days_back=cfg["backfill_days"], incremental_days=inc_days,
                           warehouse_days=have_days, newest_first=bool(getattr(a, "heal", False)),
                           max_minutes=a.max_minutes or t_cfg.get("max_minutes_per_run"))
            except Exception as e:
                failed.append("toast"); log.error("Toast pull failed (%s: %s) — continuing with other sources", type(e).__name__, e)
        else:
            log.warning("TOAST_CLIENT_ID / TOAST_CLIENT_SECRET not set — skipping Toast")
    if a.source in ("all", "scorecard"):
        # Needs no API credential of its own — the Apps Script web app already deployed for sign-in serves it,
        # authenticated with an HMAC of PORTAL_SECRET.
        if env("APPS_SCRIPT_URL") and env("PORTAL_SECRET"):
            attempted.append("scorecard")
            try:
                from . import scorecard
                scorecard.pull()
            except Exception as e:
                failed.append("scorecard"); log.error("Scorecard pull failed (%s: %s) — continuing with other sources", type(e).__name__, e)
        else:
            log.warning("APPS_SCRIPT_URL / PORTAL_SECRET not set — skipping the Leadership Scorecard")
    if a.source in ("all", "scorecard"):
        # Rides with the scorecard: same backend, same proof. Optional in the same way weather is.
        if env("APPS_SCRIPT_URL") and env("PORTAL_SECRET"):
            try:
                from . import depletions
                depletions.pull()
            except Exception as e:
                log.warning("depletions not pulled (%s: %s) — the taproom-vs-market view keeps its last load", type(e).__name__, e)
    if a.source in ("all", "tripleseat"):
        # The refresh token is the piece that makes this unattended; without it the consent step has not been
        # done yet and there is nothing to run. It may live in the warehouse (after a rotation) or the secret.
        if env("TRIPLESEAT_CLIENT_ID") and env("TRIPLESEAT_CLIENT_SECRET"):
            attempted.append("tripleseat")
            try:
                from . import tripleseat
                ts_cfg = cfg.get("tripleseat", {})
                tripleseat.pull(locs, days_back=cfg["backfill_days"], days_forward=int(ts_cfg.get("days_forward", 180)))
            except Exception as e:
                failed.append("tripleseat"); log.error("Tripleseat pull failed (%s: %s) — continuing with other sources", type(e).__name__, e)
        else:
            log.warning("TRIPLESEAT_CLIENT_ID / TRIPLESEAT_CLIENT_SECRET not set — skipping Tripleseat")
    if a.source in ("all", "weather"):
        # Keyless and optional: weather is context, never a reason to fail a run. It does not count towards
        # `attempted`, so a weather outage on its own can never trip the "every source failed" exit below.
        try:
            from . import weather
            weather.pull(locs, days_back=cfg["backfill_days"])
        except Exception as e:
            log.warning("weather not pulled (%s: %s) — the portal will show sales without it", type(e).__name__, e)
    if attempted and len(failed) == len(attempted):
        raise SystemExit("every configured source failed: " + ", ".join(failed))
    if failed:
        log.warning("finished with %s unavailable; the dashboard will show whatever the other sources provided", ", ".join(failed))


def cmd_transform(a):
    from . import transform
    transform.run()


def cmd_check(a):
    from . import check
    return check.run()


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


def cmd_me_diag(a):
    from . import diag
    diag.main()


def cmd_toast_diag(a):
    from . import diag
    diag.toast()


def cmd_sc_test(a):
    from . import scorecard
    j = scorecard.fetch()
    print(f"workbook: {j.get('workbook')}  tab: {j.get('sheet')}  fetched: {j.get('fetched_at')}")
    print(f"weeks ({len(j.get('header') or [])}): {', '.join((j.get('header') or [])[:8])} ...")
    for r in (j.get("rows") or []):
        latest = next(iter(r["cells"].values()), {}).get("d", "")
        print(f"  {r.get('owner',''):8} {r['metric'][:38]:40} goal={r['goal']['d'][:12]:14} latest={latest}")


def cmd_ts_auth_url(a):
    from .tripleseat import authorize_url
    print(authorize_url())
    print("\nOpen that URL, approve, then copy the ?code=... value out of the address bar and run:\n"
          "  python -m sdp ts-exchange --code <code>")


def cmd_ts_exchange(a):
    """One-time consent exchange. Prints the refresh token so it can be pasted into a repo secret —
    run this locally, never in CI, where the output would land in a build log."""
    if not a.code:
        raise SystemExit("--code is required (the ?code=... value Tripleseat redirected to)")
    from .tripleseat import exchange_code
    j = exchange_code(a.code)
    print("access_token expires in:", j.get("expires_in"))
    print("\nSet this as the repo secret TRIPLESEAT_REFRESH_TOKEN:\n")
    print(j["refresh_token"])


def cmd_ts_locations(a):
    from .tripleseat import Tripleseat
    for l in Tripleseat().locations():
        print(f"{l.get('id')}\t{l.get('name')}")


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
    for name, fn in [("pull", cmd_pull), ("transform", cmd_transform), ("check", cmd_check), ("build", cmd_build), ("all", cmd_all), ("restore", cmd_restore), ("persist", cmd_persist),
                     ("me-units", cmd_me_units), ("me-diag", cmd_me_diag), ("toast-diag", cmd_toast_diag), ("ts-locations", cmd_ts_locations), ("sc-test", cmd_sc_test), ("ts-auth-url", cmd_ts_auth_url), ("ts-exchange", cmd_ts_exchange), ("toast-restaurants", cmd_toast_restaurants)]:
        p = sub.add_parser(name); p.set_defaults(fn=fn)
        p.add_argument("--mock", action="store_true", help="generate sample raw data instead of calling APIs")
        p.add_argument("--mock-days", type=int, default=120)
        p.add_argument("--mock-no-toast", action="store_true", help="mock the MarginEdge-only phase (no Toast raw data)")
        p.add_argument("--max-minutes", type=float, default=None, help="stop the MarginEdge pull cleanly after N minutes (default from settings)")
        p.add_argument("--source", choices=["all", "toast", "marginedge", "tripleseat", "scorecard", "weather"], default="all")
        p.add_argument("--phase", choices=["all", "recent", "history"], default="all",
                       help="MarginEdge only: 'recent' fetches the last few weeks so the site can publish, 'history' walks backwards")
        p.add_argument("--code", default=None, help="Tripleseat authorization code (ts-exchange)")
        p.add_argument("--backfill", action="store_true", help="pull the full backfill window even if a warehouse exists")
        p.add_argument("--dev-json", action="store_true", help="also write site/data/dev.json (unencrypted, local preview)")
        p.add_argument("--no-encrypt", action="store_true", help="skip bundles; write dev.json only")
        p.add_argument("--heal", action="store_true",
                       help="Toast: re-pull days loaded before modifiers/loyalty/source were captured, newest first, within the "
                            "time budget. Run by hand after the morning refresh; repeat until it reports nothing left.")
        p.add_argument("--incremental-days", type=int, default=None,
                       help="override settings.incremental_days for this run — re-pulls that many days even where the "
                            "warehouse already has them. Use to heal history after a transform fix, then drop back.")
    a = ap.parse_args(argv)
    # A command that returns a non-zero code must fail the process, or a failing check is a green workflow.
    rc = a.fn(a)
    if rc:
        raise SystemExit(rc)


if __name__ == "__main__":
    main()
