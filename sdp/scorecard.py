"""Leadership Scorecard pull — the weekly company scorecard kept in Google Sheets.

Not everything a store director is measured on comes out of a POS. The scorecard workbook carries
brewery-side lines (distribution revenue, production labor, order fulfilment) and manual inputs
(mystery shops, audit scores) that exist nowhere else, and it carries the company's own agreed
definitions and goals for the lines that do. So this source is read as-is rather than recomputed.

AUTH — why this goes through Apps Script rather than the Sheets API
------------------------------------------------------------------
Reading a private Google Sheet from CI normally means a service account, a new key file and another
secret to rotate. The portal already has an Apps Script web app deployed under the owner's account for
sign-in, and it already reads a spreadsheet. Adding one action to it reuses that authorization: the
script opens the workbook as the owner, and the pipeline proves itself with an HMAC of the shared
PORTAL_SECRET rather than any Google credential. No new credentials exist to leak.

Request:  POST <APPS_SCRIPT_URL>  {"a":"scorecard","k":<base64 HMAC-SHA256(PORTAL_SECRET,"scorecard")>}
Response: {ok, sheet, header, rows:[{owner, metric, goal, cells:{<week>: {v, d}}}], fetched_at}
"""
from __future__ import annotations

import base64
import hashlib
import hmac

from .util import Http, env, log, write_raw

ACTION = "scorecard"


def _key(secret: str) -> str:
    """Must match bundleFor()'s derivation in apps-script/code.gs: base64(HMAC-SHA256(secret, label))."""
    return base64.b64encode(hmac.new(secret.encode("utf-8"), ACTION.encode("utf-8"), hashlib.sha256).digest()).decode()


def fetch() -> dict:
    url = env("APPS_SCRIPT_URL", required=True)
    secret = env("PORTAL_SECRET", required=True)
    # Apps Script answers an exec POST with a 302 to googleusercontent; requests follows it and returns the JSON.
    r = Http(url, headers={"Content-Type": "application/json"}).post(url, {"a": ACTION, "k": _key(secret)})
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"scorecard fetch refused: {str(j.get('error'))[:200]}")
    return j


def pull() -> dict:
    j = fetch()
    write_raw("scorecard", "_all", "scorecard", "current", j)
    n = len(j.get("rows") or [])
    log.info("Scorecard: %s — %d rows, %d weeks (as of %s)", j.get("sheet"), n, len(j.get("header") or []), j.get("fetched_at"))
    return {"rows": n, "weeks": len(j.get("header") or [])}
