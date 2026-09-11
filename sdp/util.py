"""Shared helpers: paths, config, env, HTTP with retry, logging."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
RAW_DIR = Path(os.environ.get("SDP_RAW_DIR", ROOT / "raw"))
STATE_DIR = ROOT / "state"
INPUTS_DIR = ROOT / "inputs"
SITE_DIR = ROOT / "site"
DB_PATH = Path(os.environ.get("SDP_DB", STATE_DIR / "warehouse.sqlite"))

log = logging.getLogger("sdp")
if not log.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    log.addHandler(h)
    log.setLevel(os.environ.get("SDP_LOGLEVEL", "INFO"))


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def settings() -> dict:
    return load_json(CONFIG_DIR / "settings.json")


def locations() -> list[dict]:
    return load_json(CONFIG_DIR / "locations.json")["locations"]


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    """Read an environment variable, treating empty/whitespace as ABSENT.

    GitHub Actions substitutes an unset `vars.X` / `secrets.X` as an empty string rather than
    omitting the variable, so `os.environ.get(name, default)` would return "" and silently defeat
    the default (e.g. an empty TOAST_HOST produced a schemeless URL). Values are stripped, so a
    secret pasted with a trailing newline still works."""
    v = os.environ.get(name)
    v = v.strip() if isinstance(v, str) else v
    if not v:
        v = default
    if required and not v:
        raise SystemExit(f"Missing required environment variable {name}")
    return v


def today_local() -> date:
    # Business dates are Central; GitHub runners are UTC. Nightly run at ~04:00 CT sees 'yesterday' as complete.
    return (datetime.now(timezone.utc) - timedelta(hours=5)).date()


def iso(d: date) -> str:
    return d.isoformat()


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def write_raw(source: str, location_id: str, dataset: str, key: str, payload) -> Path:
    """Persist a raw API response so transform is replayable and pulls are debuggable."""
    out = RAW_DIR / source / location_id / dataset
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{key}.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    return p


def iter_raw(source: str, location_id: str | None = None, dataset: str | None = None):
    base = RAW_DIR / source
    if not base.exists():
        return
    for loc in sorted(base.iterdir()):
        if location_id and loc.name != location_id:
            continue
        for ds in sorted(loc.iterdir()):
            if dataset and ds.name != dataset:
                continue
            for p in sorted(ds.glob("*.json")):
                yield loc.name, ds.name, p, load_json(p)


class Http:
    """Tiny requests wrapper with retry/backoff and a per-second throttle."""

    def __init__(self, base_url: str, headers: dict | None = None, rps: float = 4.0, timeout: int = 60):
        import requests  # local import keeps mock mode dependency-free

        self.s = requests.Session()
        self.s.headers.update(headers or {})
        self.base = base_url.rstrip("/")
        self.min_gap = 1.0 / rps if rps else 0
        self.timeout = timeout
        self._last = 0.0

    def _throttle(self):
        gap = time.monotonic() - self._last
        if gap < self.min_gap:
            time.sleep(self.min_gap - gap)
        self._last = time.monotonic()

    def get(self, path: str, params: dict | None = None, headers: dict | None = None, retries: int = 5):
        url = path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"
        for attempt in range(retries):
            self._throttle()
            r = self.s.get(url, params=params, headers=headers, timeout=self.timeout)
            if r.status_code == 429 or r.status_code >= 500:
                wait = min(60, 2 ** attempt + 1)
                log.warning("HTTP %s on %s — retry in %ss", r.status_code, url, wait)
                time.sleep(wait)
                continue
            if r.status_code == 401 or r.status_code == 403:
                raise PermissionError(f"{r.status_code} from {url}: {r.text[:300]}")
            r.raise_for_status()
            return r
        raise RuntimeError(f"gave up on {url} after {retries} attempts")

    def post(self, path: str, json_body: dict, headers: dict | None = None):
        url = path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"
        self._throttle()
        r = self.s.post(url, json=json_body, headers=headers, timeout=self.timeout)
        r.raise_for_status()
        return r
