"""Build the static site: site/ (login shell + app) + site/data/<id>.bin encrypted bundles.

Output goes to _site/ (what GitHub Pages publishes). site/index.html is copied as-is; the only thing that
changes per build is the data. In --mock / --no-encrypt mode a plain site/data/dev.json is written too, and the
shell's ?dev=1 mode loads it without signing in (local preview only — never published).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import metrics
from .bundle import bundle_id, derive_key, encrypt
from .util import ROOT, SITE_DIR, env, log

OUT_DIR = ROOT / "_site"


def run(secret: str | None = None, dev_json: bool = False) -> dict:
    payload = metrics.run()
    OUT_DIR.mkdir(exist_ok=True)
    for p in OUT_DIR.iterdir():
        (shutil.rmtree if p.is_dir() else p.unlink)(p)
    for p in SITE_DIR.iterdir():
        if p.name == "data":
            continue
        (shutil.copytree if p.is_dir() else shutil.copy2)(p, OUT_DIR / p.name)
    data = OUT_DIR / "data"
    data.mkdir(exist_ok=True)
    (OUT_DIR / ".nojekyll").touch()
    written = {}
    if secret:
        labels = ["all"] + [f"loc:{l['id']}" for l in payload["locations"]]
        for label in labels:
            obj = payload if label == "all" else metrics.slice_for_location(payload, label.split(":", 1)[1])
            bid = bundle_id(label)
            buf = encrypt(obj, derive_key(secret, label))
            (data / f"{bid}.bin").write_bytes(buf)
            written[label] = {"file": f"data/{bid}.bin", "bytes": len(buf)}
        # a public manifest with only build time + data-through date (no data) so the shell can show freshness pre-login
    (data / "manifest.json").write_text(json.dumps({"built_at": payload["meta"]["built_at"], "through": payload["meta"]["through"]}))
    if dev_json:
        (data / "dev.json").write_text(json.dumps(payload, separators=(",", ":")))
        written["dev.json"] = {"file": "data/dev.json", "bytes": (data / "dev.json").stat().st_size}
    log.info("site: %s", {k: v["bytes"] for k, v in written.items()})
    return written
