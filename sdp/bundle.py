"""Encrypted bundles — same wire format as the Big Grove Sales Portal so the login shell code is shared:

    bundle = 12-byte IV || AES-256-GCM( gzip(payload) )

Keys are derived from one secret (PORTAL_SECRET, a GitHub Actions secret and an Apps Script property) and
an EPOCH:
    key(label) = HMAC-SHA256(secret, "<label>:<epoch>")   -> 32 raw bytes, base64 on the wire
    id(label)  = sha256("<label>:<epoch>").hexdigest()[:16]  -> the file name  data/<id>.bin
labels: "all" (leadership/admin: every location), "loc:<slug>" (one taproom) and "scorecard".

WHY THE EPOCH EXISTS. Bundles are public files and a key, once a browser has held it, works forever. Without
an epoch, removing somebody from the roster stops them signing in but does nothing about the key they already
have — and tomorrow's build publishes fresh data to the same file name, which that old key still opens. That
is a silent failure: you take the action, see them gone from the roster, and reasonably believe it is handled.

Bumping PORTAL_EPOCH changes every key AND every file name, so previously issued keys open nothing and the
files they point at no longer exist. It is the revoke button. The epoch is not a secret — it is a salt, and it
is published in manifest.json precisely so the Apps Script can read the current value rather than being
configured separately and drifting out of step.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _salted(label: str, epoch: str | None) -> str:
    """The warehouse key deliberately passes epoch=None: rotating the epoch must not strand the persisted
    state, which is our own backup rather than anything a browser ever holds."""
    return label if not epoch else f"{label}:{epoch}"


def derive_key(secret: str, label: str, epoch: str | None = None) -> bytes:
    return hmac.new(secret.encode("utf-8"), _salted(label, epoch).encode("utf-8"), hashlib.sha256).digest()


def bundle_id(label: str, epoch: str | None = None) -> str:
    return hashlib.sha256(_salted(label, epoch).encode("utf-8")).hexdigest()[:16]


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
