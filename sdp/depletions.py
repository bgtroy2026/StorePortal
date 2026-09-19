"""Distributor depletions by brand x taproom market, collected from the roster workbook.

The roll-up is built on the Mac (tools/build_depletions.py) and uploaded from the portal by an Admin. It is never
committed: this repository is public, and even a coarse roll-up of company sales has no business in it. The Apps
Script holds it in a "Depletions" tab and hands it to this pipeline on proof of PORTAL_SECRET, the same way the
scorecard travels (see sdp/scorecard.py for why that route exists).
"""
from __future__ import annotations

import base64
import hashlib
import hmac

from .util import Http, env, log, write_raw


def pull() -> dict:
    url = env("APPS_SCRIPT_URL", required=True)
    secret = env("PORTAL_SECRET", required=True)
    key = base64.b64encode(hmac.new(secret.encode("utf-8"), b"depletions", hashlib.sha256).digest()).decode()
    j = Http(url, headers={"Content-Type": "application/json"}).post(url, {"a": "depletions", "k": key}).json()
    if not j.get("ok"):
        raise RuntimeError(f"depletions fetch refused: {str(j.get('error'))[:200]}")
    rows = j.get("rows") or []
    write_raw("depletions", "_all", "depletions", "current", {"rows": rows})
    log.info("Depletions: %d brand x market rows%s", len(rows), f" (as of {rows[0][6]})" if rows else " — nothing uploaded yet")
    return {"rows": len(rows)}
