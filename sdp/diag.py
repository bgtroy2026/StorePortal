"""Connectivity diagnostics — print request outcomes, NEVER the credentials.

    python -m sdp me-diag        MarginEdge
    python -m sdp toast-diag     Toast (also lists restaurant GUIDs for config/locations.json)

Tries /restaurantUnits with a few header/whitespace variants so a 403 can be classified:
  {"message":"Forbidden"}                       -> key not recognized at all
  "explicit deny in an identity-based policy"   -> key recognized by the authorizer but denied
  200                                           -> that variant works; the pull should use it
"""
from __future__ import annotations

import hashlib
import json
import os

import requests

BASE = "https://api.marginedge.com/public"


def main():
    raw = os.environ.get("MARGINEDGE_API_KEY")
    if raw is None:
        print("MARGINEDGE_API_KEY is not set"); return
    key = raw.strip()
    print(f"key: length={len(raw)} stripped_length={len(key)} leading/trailing whitespace={'YES' if raw != key else 'no'} "
          f"contains_newline={'YES' if chr(10) in raw or chr(13) in raw else 'no'} looks_like_name={'YES' if raw.strip().upper() in ('MARGINEDGE_API_KEY','PORTAL_SECRET') else 'no'} "
          f"fingerprint={hashlib.sha256(key.encode()).hexdigest()[:8]} charset={'alnum/-_' if all(c.isalnum() or c in '-_' for c in key) else 'has other chars'} "
          # MarginEdge's own settings screen masks the key except its last four characters. Echoing the same
          # four is what makes "is the secret in GitHub the key I am looking at in MarginEdge?" answerable at
          # all; four characters of a 40-character key is not enough to be worth anything on its own.
          f"last4={key[-4:] if len(key) >= 8 else '(too short)'}")
    variants = [
        ("x-api-key, stripped, python UA", {"x-api-key": key}),
        ("X-Api-Key, stripped, curl-like UA", {"X-Api-Key": key, "User-Agent": "curl/8.4.0", "Accept": "*/*"}),
        ("x-api-key, stripped, browser UA", {"x-api-key": key, "User-Agent": "Mozilla/5.0", "Accept": "application/json"}),
        ("Authorization: Bearer (in case docs are wrong)", {"Authorization": f"Bearer {key}"}),
    ]
    for label, headers in variants:
        try:
            r = requests.get(f"{BASE}/restaurantUnits", headers=headers, timeout=30)
            body = r.text[:160].replace("\n", " ")
            if r.status_code == 200:
                try:
                    n = len(r.json().get("restaurants", []))
                    body = f"OK — {n} restaurant units visible"
                except Exception:
                    pass
            print(f"[{r.status_code}] {label}: {body}")
        except Exception as e:
            print(f"[ERR] {label}: {e}")
    # a second endpoint, in case /restaurantUnits specifically is gated
    try:
        r = requests.get(f"{BASE}/restaurantUnits/groups", headers={"x-api-key": key}, timeout=30)
        print(f"[{r.status_code}] /restaurantUnits/groups: {r.text[:160]}")
    except Exception as e:
        print(f"[ERR] /restaurantUnits/groups: {e}")


if __name__ == "__main__":
    main()


# ----------------------------------------------------------------- Toast

TOAST_HOSTS = ["https://ws-api.toasttab.com"]


def toast():
    """Authenticate with the Toast standard-API credentials and list every restaurant they reach.

    The GUIDs printed here are what go into config/locations.json as `toast_guid` — they are
    identifiers, not secrets, so printing them is fine. The client secret is never printed."""
    cid = (os.environ.get("TOAST_CLIENT_ID") or "").strip()
    csec = (os.environ.get("TOAST_CLIENT_SECRET") or "").strip()
    host = (os.environ.get("TOAST_HOST") or TOAST_HOSTS[0]).strip().rstrip("/")
    if not cid or not csec:
        print("TOAST_CLIENT_ID / TOAST_CLIENT_SECRET not set"); return
    print(f"client id: length={len(cid)} fingerprint={hashlib.sha256(cid.encode()).hexdigest()[:8]}")
    print(f"client secret: length={len(csec)} fingerprint={hashlib.sha256(csec.encode()).hexdigest()[:8]}")
    print(f"host: {host}")

    body = {"clientId": cid, "clientSecret": csec, "userAccessType": "TOAST_MACHINE_CLIENT"}
    try:
        r = requests.post(f"{host}/authentication/v1/authentication/login", json=body, timeout=45)
    except Exception as e:
        print(f"[ERR] auth request failed: {e}"); return
    if r.status_code != 200:
        print(f"[{r.status_code}] auth failed: {r.text[:300]}"); return
    tok = (r.json().get("token") or {})
    at = tok.get("accessToken") or ""
    print(f"[200] auth OK — token type={tok.get('tokenType')} expires_in={tok.get('expiresIn')}s length={len(at)}")
    auth = {"Authorization": f"Bearer {at}", "Accept": "application/json"}

    # restaurants this client can reach (GUIDs are not secret)
    found = []
    for path in ("/partners/v1/restaurants", "/partners/v1/connectedRestaurants"):
        try:
            rr = requests.get(f"{host}{path}", headers=auth, timeout=45)
        except Exception as e:
            print(f"[ERR] {path}: {e}"); continue
        if rr.status_code != 200:
            print(f"[{rr.status_code}] {path}: {rr.text[:200]}"); continue
        data = rr.json()
        rows = data if isinstance(data, list) else (data.get("results") or data.get("restaurants") or [])
        print(f"[200] {path}: {len(rows)} restaurants")
        for x in rows:
            g = x.get("restaurantGuid") or x.get("guid") or (x.get("restaurant") or {}).get("guid")
            nm = x.get("restaurantName") or x.get("name") or ""
            loc = x.get("locationName") or (x.get("location") or {}).get("name") or ""
            ext = x.get("managementGroupGuid") or ""
            print(f"    GUID {g}   {nm} | {loc}   mgmtGroup={ext}")
            if g:
                found.append((g, f"{nm} {loc}".strip()))
        if rows:
            break
    if not found:
        print("    (no restaurant list — standard-API credentials sometimes cannot call the Partners API;")
        print("     use the GUIDs from Toast's credential confirmation email, or Toast Web > Restaurant admin > Restaurant info)")
        return

    # prove the token works per-restaurant and show closeout hour / timezone for business-date handling
    for g, label in found:
        try:
            rr = requests.get(f"{host}/restaurants/v1/restaurants/{g}", headers=dict(auth, **{"Toast-Restaurant-External-ID": g}), timeout=45)
            if rr.status_code == 200:
                gen = (rr.json().get("general") or {})
                print(f"    [200] {label or g}: name={gen.get('name')} tz={gen.get('timeZone')} closeoutHour={gen.get('closeoutHour')}")
            else:
                print(f"    [{rr.status_code}] {label or g}: {rr.text[:140]}")
        except Exception as e:
            print(f"    [ERR] {label or g}: {e}")
