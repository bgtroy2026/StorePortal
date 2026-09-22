"""Warehouse persistence between GitHub Actions runs.

The SQLite warehouse is gzip'd and encrypted (same AES-GCM format as bundles, label "warehouse") and stored as a
single asset on a GitHub Release tagged `warehouse-state`, overwritten each night. That keeps the item-level
history without bloating git history, and a fresh runner restores it in seconds. If the asset is missing the
pipeline simply starts from an empty warehouse and backfills.
"""
from __future__ import annotations

import os
import subprocess
import zlib
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .bundle import derive_key
from .util import DB_PATH, STATE_DIR, env, log

TAG = "warehouse-state"
ASSET = "warehouse.sqlite.enc"
CHUNK = 8 << 20          # 8 MB: the most of the warehouse that is ever in memory at once, in either direction
GZIP_WBITS = 16 + zlib.MAX_WBITS   # a gzip container from zlib's streaming API — byte-compatible with gzip.compress

# The wire format is unchanged from the first version: 12-byte IV, then AES-GCM ciphertext, then the 16-byte tag
# — exactly what AESGCM(key).encrypt(iv, gzip.compress(db), None) produced. What changed on 2026-09-22 is that
# both directions STREAM. The one-shot version read the whole SQLite file into memory, gzipped it into a second
# copy and encrypted that into a third; at the 2 GB the warehouse had reached that was already ~3 GB of RAM on a
# 7 GB runner, and extending Toast history to 26 months roughly doubles the warehouse. Streaming in 8 MB pieces
# keeps the save and the restore at a few tens of MB whatever the file grows to. The other ceiling to know about
# is GitHub's 2 GB limit per release asset; the encrypted file is about 30% of the warehouse.


def encrypt_db(secret: str, db: Path = DB_PATH, out: Path | None = None) -> Path:
    out = out or STATE_DIR / ASSET
    key = derive_key(secret, "warehouse")
    iv = os.urandom(12)
    enc = Cipher(algorithms.AES(key), modes.GCM(iv)).encryptor()
    comp = zlib.compressobj(6, zlib.DEFLATED, GZIP_WBITS)
    out.parent.mkdir(parents=True, exist_ok=True)
    with db.open("rb") as f, out.open("wb") as o:
        o.write(iv)
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            c = comp.compress(chunk)
            if c:
                o.write(enc.update(c))
        tail = comp.flush()
        if tail:
            o.write(enc.update(tail))
        o.write(enc.finalize())
        o.write(enc.tag)
    log.info("state: encrypted %s (%.1f MB -> %.1f MB)", db.name, db.stat().st_size / 1e6, out.stat().st_size / 1e6)
    return out


def decrypt_db(secret: str, enc: Path | None = None, db: Path = DB_PATH) -> Path:
    enc = enc or STATE_DIR / ASSET
    key = derive_key(secret, "warehouse")
    size = enc.stat().st_size
    if size < 12 + 16:
        raise ValueError(f"{enc.name} is too small to be an encrypted warehouse ({size} bytes)")
    db.parent.mkdir(parents=True, exist_ok=True)
    tmp = db.with_suffix(db.suffix + ".restoring")
    try:
        with enc.open("rb") as f, tmp.open("wb") as o:
            iv = f.read(12)
            dec = Cipher(algorithms.AES(key), modes.GCM(iv)).decryptor()
            decomp = zlib.decompressobj(GZIP_WBITS)
            remaining = size - 12 - 16           # ciphertext only; the tag is the last 16 bytes
            while remaining > 0:
                chunk = f.read(min(CHUNK, remaining))
                if not chunk:
                    raise ValueError(f"{enc.name} ended early")
                remaining -= len(chunk)
                o.write(decomp.decompress(dec.update(chunk)))
            # Nothing above is trusted until the tag checks out: a tampered or truncated asset (which gzip usually
            # notices first, as a stream error) must leave no half-written warehouse behind to be mistaken for a
            # real one.
            dec.finalize_with_tag(f.read(16))
            o.write(decomp.flush())
    except (InvalidTag, zlib.error, ValueError) as e:
        tmp.unlink(missing_ok=True)
        why = "wrong PORTAL_SECRET or a damaged asset" if isinstance(e, (InvalidTag, zlib.error)) else str(e)
        raise ValueError(f"{enc.name} could not be restored — {why}") from None
    os.replace(tmp, db)
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
