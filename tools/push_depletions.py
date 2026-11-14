#!/usr/bin/env python3
"""Rebuild the brand x market depletion roll-up and send it to the portal's roster workbook.

Runs on the Mac at the end of the Sales Portal deploy. Nothing here touches either git repo: the roll-up is
built in a temp file, posted to the Apps Script, and deleted. The upload key lives outside both project
folders (~/Library/Application Support/BigGroveDeploy/depletions_put.key) and is never sent — only a
signature of "as-of date : row count" made with it.

Skips quietly when the roll-up has not changed since the last successful upload.
Exit code is always 0 unless --strict: a failed upload must never fail the Sales Portal deploy.
"""
from __future__ import annotations
import base64, csv, hashlib, hmac, json, os, re, subprocess, sys, tempfile, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SALES = os.path.join(os.path.dirname(REPO), "Big Grove Sales Portal")
CFG = os.path.expanduser("~/Library/Application Support/BigGroveDeploy")
KEYFILE = os.path.join(CFG, "depletions_put.key")
STATE = os.path.join(CFG, "depletions_last.json")


URLCACHE = os.path.join(REPO, "config", "api_url.txt")
URLPAT = r"API_URL:\s*'(https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec)'"


def api_url() -> str:
    """Endpoint from site/index.html, with config/api_url.txt as a fallback cache.

    site/index.html can be evicted by iCloud ("Optimize Mac Storage"); opening a dataless
    placeholder raises OSError 11 (EDEADLK on macOS) instead of returning data, which silently
    broke this upload for 8 deploys in Oct 2026. index.html stays the source of truth and
    refreshes the cache whenever it is readable; the cache only covers an eviction.
    """
    url = None
    try:
        html = open(os.path.join(REPO, "site", "index.html"), encoding="utf-8").read()
        m = re.search(URLPAT, html)
        if m:
            url = m.group(1)
        else:
            print("depletions: API_URL pattern not found in site/index.html")
    except OSError as e:
        extra = ""
        if e.errno == 11:
            extra = " - looks like an un-downloaded iCloud placeholder"
        print(f"depletions: site/index.html unreadable ({e.strerror or e}){extra}; trying cached API_URL")
    if url:
        try:
            cur = open(URLCACHE, encoding="utf-8").read().strip() if os.path.exists(URLCACHE) else None
            if cur != url:
                os.makedirs(os.path.dirname(URLCACHE), exist_ok=True)
                with open(URLCACHE, "w", encoding="utf-8") as fh:
                    fh.write(url + "\n")
        except OSError:
            pass
        return url
    try:
        cached = open(URLCACHE, encoding="utf-8").read().strip()
        if cached.startswith("https://script.google.com/macros/s/"):
            print("depletions: using cached API_URL from config/api_url.txt")
            return cached
    except OSError:
        pass
    raise RuntimeError("API_URL unavailable: site/index.html unreadable and no cache at config/api_url.txt")


def main() -> int:
    if not os.path.exists(KEYFILE):
        print("depletions: no upload key on this Mac yet (run 'Set up depletions upload.command') - skipped")
        return 0
    key = open(KEYFILE, encoding="utf-8").read().strip()
    if len(key) < 24:
        print("depletions: upload key looks wrong - skipped"); return 0
    fd, tmp = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "build_depletions.py"),
                            os.path.join(SALES, "Yearly Distributor Data"),
                            os.path.join(SALES, "_build", "geocache.json"), tmp],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            print("depletions: roll-up failed:", (r.stderr or r.stdout)[-400:]); return 1
        raw = open(tmp, "rb").read()
        rows = []
        for d in csv.DictReader(raw.decode("utf-8").splitlines()):
            rows.append([d["location_id"], d["brand"], d["premise"], float(d["ce_ty"] or 0),
                         float(d["ce_ly"] or 0), int(float(d["accounts"] or 0)), d["as_of"]])
    finally:
        try: os.remove(tmp)
        except OSError: pass
    if not rows or len(rows) > 6000:
        print(f"depletions: {len(rows)} rows - refusing to upload"); return 1
    digest = hashlib.sha256(raw).hexdigest()
    try:
        if json.load(open(STATE)).get("sha") == digest:
            print("depletions: unchanged since last upload - skipped"); return 0
    except (OSError, ValueError):
        pass
    msg = f"depletions_put:{rows[0][6]}:{len(rows)}"
    k = base64.b64encode(hmac.new(key.encode(), msg.encode(), hashlib.sha256).digest()).decode()
    body = json.dumps({"a": "depletions_put", "k": k, "rows": rows}).encode()
    req = urllib.request.Request(api_url(), data=body, headers={"Content-Type": "text/plain;charset=utf-8"})
    out = json.loads(urllib.request.urlopen(req, timeout=90).read().decode("utf-8"))
    if not out.get("ok"):
        print("depletions: upload refused:", out.get("error")); return 1
    os.makedirs(CFG, exist_ok=True)
    json.dump({"sha": digest, "rows": out.get("rows"), "as_of": out.get("as_of")}, open(STATE, "w"))
    print(f"depletions: uploaded {out.get('rows')} rows, as of {out.get('as_of')}")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:  # never break the deploy that called us
        import traceback
        tb = traceback.extract_tb(sys.exc_info()[2])
        where = f"{os.path.basename(tb[-1].filename)}:{tb[-1].lineno} in {tb[-1].name}()" if tb else "?"
        hint = ""
        if isinstance(e, OSError) and e.errno == 11:
            hint = ("\n  hint: errno 11 is EDEADLK on macOS and here almost always means the file is an"
                    "\n        iCloud placeholder that is not downloaded. Fix: brctl download <path>,"
                    "\n        or Finder right-click the folder -> Download Now / Keep Downloaded.")
        print(f"depletions: error: {type(e).__name__}: {e} (at {where}){hint}")
        rc = 1
    sys.exit(rc if "--strict" in sys.argv else 0)
