"""Warehouse persistence between GitHub Actions runs.

The SQLite warehouse is gzip'd and encrypted (same AES-GCM format as bundles, label "warehouse") and stored as a
single asset on a GitHub Release tagged `warehouse-state`, overwritten each night. That keeps the item-level
history without bloating git history, and a fresh runner restores it in seconds. If the asset is missing the
pipeline simply starts from an empty warehouse and backfills.
"""
from __future__ import annotations

import gzip
import os
import subprocess
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .bundle import derive_key
from .util import DB_PATH, STATE_DIR, env, log

TAG = "warehouse-state"
ASSET = "warehouse.sqlite.enc"


def encrypt_db(secret: str, db: Path = DB_PATH, out: Path | None = None) -> Path:
    out = out or STATE_DIR / ASSET
    key = derive_key(secret, "warehouse")
    data = gzip.compress(db.read_bytes(), 6)
    iv = os.urandom(12)
    out.write_bytes(iv + AESGCM(key).encrypt(iv, data, None))
    log.info("state: encrypted %s (%.1f MB -> %.1f MB)", db.name, db.stat().st_size / 1e6, out.stat().st_size / 1e6)
    return out


def decrypt_db(secret: str, enc: Path | None = None, db: Path = DB_PATH) -> Path:
    enc = enc or STATE_DIR / ASSET
    key = derive_key(secret, "warehouse")
    buf = enc.read_bytes()
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(gzip.decompress(AESGCM(key).decrypt(buf[:12], buf[12:], None)))
    log.info("state: restored %s (%.1f MB)", db.name, db.stat().st_size / 1e6)
    return db


def _gh(*args, check=True):
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=check)


def restore() -> bool:
    """Download + decrypt the warehouse from the release asset. Returns False if none exists yet."""
    secret = env("PORTAL_SECRET", required=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    r = _gh("release", "download", TAG, "--pattern", ASSET, "--dir", str(STATE_DIR), "--clobber", check=False)
    if r.returncode != 0:
        log.warning("state: no release asset yet (%s) — starting with an empty warehouse", r.stderr.strip()[:200])
        return False
    decrypt_db(secret)
    return True


def persist():
    secret = env("PORTAL_SECRET", required=True)
    out = encrypt_db(secret)
    if _gh("release", "view", TAG, check=False).returncode != 0:
        _gh("release", "create", TAG, "--title", "Warehouse state (encrypted)", "--notes", "Encrypted SQLite warehouse used by the nightly refresh. Not for humans.", "--prerelease")
    _gh("release", "upload", TAG, str(out), "--clobber")
    log.info("state: uploaded %s to release %s", ASSET, TAG)
