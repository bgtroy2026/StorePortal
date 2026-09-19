"""Manual inputs kept as CSV in inputs/ (edited by hand or from a Google Sheet export).

  activations.csv       market activations, events, promos — overlaid on sales trends
  targets.csv           monthly targets per location (sales, COGS %, labor %, guests)
  inventory_counts.csv  inventory value per bucket per count date (until the MarginEdge count-sheet endpoint is wired)
  floats.csv            what each cash drawer should open with, per location (feeds the cash float test)
  depletions.csv        distributor depletions by brand x taproom market (built by tools/build_depletions.py)
"""
from __future__ import annotations

import csv

from .util import INPUTS_DIR


def _read(name: str) -> list[dict]:
    p = INPUTS_DIR / name
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8-sig") as f:
        rows = [{k.strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]
    return [r for r in rows if any(r.values()) and not next(iter(r.values())).startswith("#")]


def read_activations() -> list[dict]:
    return _read("activations.csv")


def read_targets() -> list[dict]:
    return _read("targets.csv")


def read_inventory_counts() -> list[dict]:
    return _read("inventory_counts.csv")


def read_floats() -> list[dict]:
    return _read("floats.csv")


def read_depletions() -> list[dict]:
    return _read("depletions.csv")
