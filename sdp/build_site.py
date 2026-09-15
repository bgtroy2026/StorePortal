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
    # The epoch salts every bundle key and file name. Bumping the PORTAL_EPOCH repo variable republishes
    # everything under new names with new keys, which is what makes a departure actually revocable.
    epoch = env("PORTAL_EPOCH", "1")
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
        # The scorecard workbook is leadership-only, so it is NOT part of the "all" bundle. A multi-location
        # director legitimately receives "all" (their roster entry scopes it client-side), which would have
        # handed them exec compensation and per-manager audit scores. Splitting it into its own label means the
        # data is absent unless the sign-in backend hands out that bundle's key, rather than merely hidden.
        scorecard = payload.pop("scorecard", None)
        # Detail is published as one bundle PER LOCATION, fetched on demand rather than at sign-in. Keeping it
        # out of "all" is what stops an admin's first paint from carrying every counted line in the company.
        detail = payload.pop("detail", None) or {}
        labels = ["all"] + [f"loc:{l['id']}" for l in payload["locations"]]
        if scorecard:
            labels.append("scorecard")
        labels += [f"loc:{lid}:detail" for lid in detail]
        for label in labels:
            if label == "scorecard":
                obj = {"meta": payload["meta"], "scorecard": scorecard}
            elif label.endswith(":detail"):
                obj = {"meta": payload["meta"], "detail": detail[label.split(":", 2)[1]]}
            else:
                obj = payload if label == "all" else metrics.slice_for_location(payload, label.split(":", 1)[1])
            bid = bundle_id(label, epoch)
            buf = encrypt(obj, derive_key(secret, label, epoch))
            (data / f"{bid}.bin").write_bytes(buf)
            written[label] = {"file": f"data/{bid}.bin", "bytes": len(buf)}
        # a public manifest with only build time + data-through date (no data) so the shell can show freshness pre-login
    # The epoch is published deliberately: it is a salt, not a secret, and the Apps Script reads it from here
    # so there is one source of truth rather than two settings that can drift apart.
    # `locations` is published alongside the epoch so the sign-in backend can mint detail-bundle keys without a
    # second copy of the location list to keep in step with this one.
    (data / "manifest.json").write_text(json.dumps({"built_at": payload["meta"]["built_at"], "through": payload["meta"]["through"], "epoch": epoch,
                                                    "locations": [l["id"] for l in payload["locations"]]}))
    if dev_json:
        # dev.json is the whole picture including detail, so ?dev=1 exercises the drilldown too. (When a secret
        # was given the detail was popped out into its own bundles above — put it back for the preview only.)
        if "detail" not in payload and "detail" in locals() and detail:
            payload["detail"] = detail
        (data / "dev.json").write_text(json.dumps(payload, separators=(",", ":")))
        written["dev.json"] = {"file": "data/dev.json", "bytes": (data / "dev.json").stat().st_size}
    log.info("site: %s", {k: v["bytes"] for k, v in written.items()})
    return written
