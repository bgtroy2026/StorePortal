"""Encrypted bundles — same wire format as the Big Grove Sales Portal so the login shell code is shared:

    bundle = 12-byte IV || AES-256-GCM( gzip(payload) )

Keys are derived from one secret (PORTAL_SECRET, a GitHub Actions secret and an Apps Script property):
    key(label) = HMAC-SHA256(secret, label)            -> 32 raw bytes, base64 on the wire
    id(label)  = sha256(label).hexdigest()[:16]        -> the file name  data/<id>.bin
labels: "all" (leadership/admin: every location) and "loc:<slug>" (a director's own taproom).
The Apps Script derives the same key/id for whoever signs in (see apps-script/code.gs::portalAccess), so
keys never live in the repo and rotating PORTAL_SECRET rotates everything.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def derive_key(secret: str, label: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), label.encode("utf-8"), hashlib.sha256).digest()


def bundle_id(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:16]


def encrypt(obj, key: bytes) -> bytes:
    text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    gz = gzip.compress(text.encode("utf-8"), 9)
    iv = os.urandom(12)
    return iv + AESGCM(key).encrypt(iv, gz, None)


def decrypt(buf: bytes, key: bytes):
    pt = AESGCM(key).decrypt(buf[:12], buf[12:], None)
    return json.loads(gzip.decompress(pt).decode("utf-8"))


def key_b64(secret: str, label: str) -> str:
    return base64.b64encode(derive_key(secret, label)).decode()
