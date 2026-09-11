"""MarginEdge connectivity diagnostic — prints request outcomes, NEVER the key.

    python -m sdp me-diag

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
          f"fingerprint={hashlib.sha256(key.encode()).hexdigest()[:8]} charset={'alnum/-_' if all(c.isalnum() or c in '-_' for c in key) else 'has other chars'}")
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
