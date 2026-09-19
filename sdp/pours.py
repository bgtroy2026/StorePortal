"""Pour sizes: turning "Easy Eddy" + a modifier into fluid ounces.

Why this exists. Big Grove brews its own beer, so house beer reaches a taproom as a transfer rather than a
purchase, and a dollar variance on it means nothing until somebody decides what a keg is worth. VOLUME needs no
such decision: ounces left the keg, ounces were rung, and the gap between the two is the loss. That only works if
every beer sale can be converted to ounces, and Toast does not make that easy — draft is sold under the brand
name alone and the size rides along as a modifier ("16oz", "Crowler"), in the item name ("Tigerhawk 4pk 16oz"),
or not at all.

Two rules this module holds itself to:

1. NEVER guess silently. A sale whose size cannot be read gets size_oz = NULL and is reported as unsized, with
   the exact text that failed to parse, so the fix is a one-line addition to config/settings.json rather than a
   number that is quietly wrong. The keyword sizes below that ARE assumptions (a "pint" is 16 oz; a crowler is
   32) are declared in settings under `pours` precisely so they can be corrected by someone who has stood behind
   the bar.

2. Packaged beer is not draft. A four-pack sold to go did not come out of a keg, so it is sized (for total
   volume) but tagged 'package' and kept out of every keg-yield calculation.
"""
from __future__ import annotations

import re

from .util import settings

# Defaults, overridable in config/settings.json -> "pours". Every entry here is an ASSUMPTION about how the
# taprooms ring things, and is surfaced in the portal as such.
DEFAULTS = {
    # keyword (lower-case, matched as a whole word) -> ounces
    "keywords": {
        "pint": 16, "half pint": 8, "half pour": 8, "half": 8, "taster": 5, "sample": 5, "sampler": 5,
        "crowler": 32, "growler": 64, "howler": 32, "pitcher": 60, "stein": 33.8, "liter": 33.8, "litre": 33.8,
        "tulip": 12, "snifter": 10, "goblet": 12,
    },
    # a flight is several small pours; total ounces for the whole flight
    "flight_oz": 20,
    # words that mark a sale as packaged (to-go cans/bottles), not draft
    "package_words": ["pk", "pack", "4pk", "6pk", "12pk", "can", "cans", "bottle", "bottles", "case", "to-go cans", "togo"],
    # Draft sold with NO readable size. None = leave unsized and report it. Set a number only once the unsized
    # list in the portal has been read and the remaining items really are a standard pour.
    "default_draft_oz": None,
}

_OZ = re.compile(r"(\d+(?:\.\d+)?)\s*(?:oz|ounce|ounces)\b", re.I)
_ML = re.compile(r"(\d+(?:\.\d+)?)\s*ml\b", re.I)
_MULTI = re.compile(r"(\d+)\s*(?:pk|pack|-pack|x)\b", re.I)                 # 4pk, 6 pack, 4x
_MULTI2 = re.compile(r"\b(\d+)\s*/\s*(\d+(?:\.\d+)?)\s*(?:oz)\b", re.I)      # 4/16oz

# Keg sizes, for the supply side (MarginEdge product / packaging names). US fluid ounces.
KEG_OZ = [
    (re.compile(r"\b1\s*/\s*2\s*(?:bbl|barrel|bl)\b|\bhalf\s*(?:bbl|barrel)\b|\b15\.5\s*gal", re.I), 1984.0),
    (re.compile(r"\b1\s*/\s*4\s*(?:bbl|barrel)\b|\bquarter\s*(?:bbl|barrel)\b|\bpony\b|\b7\.75\s*gal", re.I), 992.0),
    (re.compile(r"\b1\s*/\s*6\s*(?:bbl|barrel)\b|\bsixtel\b|\bsixth\s*(?:bbl|barrel)\b|\b5\.16\s*gal|\b20\s*l\b", re.I), 661.0),
    (re.compile(r"\b50\s*l\b|\b50\s*liter", re.I), 1690.7),
    (re.compile(r"\b30\s*l\b|\b30\s*liter", re.I), 1014.4),
]


def _cfg() -> dict:
    try:
        user = settings().get("pours") or {}
    except Exception:
        user = {}
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for k, v in user.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update({kk.lower(): vv for kk, vv in v.items() if not str(kk).startswith("_")})
        else:
            out[k] = v
    return out


class PourParser:
    def __init__(self, cfg: dict | None = None):
        c = cfg or _cfg()

        def num(v):
            # Config is hand-edited. A value that is not a number ("20 oz", a stray comment) must cost that one
            # setting, never the transform — this runs inside the nightly publish for six taprooms.
            try:
                f = float(v)
                return f if f > 0 else None
            except (TypeError, ValueError):
                return None
        kws = [(str(k).lower(), num(v)) for k, v in (c.get("keywords") or {}).items() if not str(k).startswith("_")]
        # longest keyword first, so "half pint" wins over "pint"
        self.keywords = sorted(((k, v) for k, v in kws if v), key=lambda kv: -len(kv[0]))
        self.flight_oz = num(c.get("flight_oz"))
        self.package_words = [str(w).lower() for w in (c.get("package_words") or []) if w]
        self.default_draft_oz = num(c.get("default_draft_oz"))
        self._kw_re = [(re.compile(r"\b" + re.escape(k) + r"s?\b", re.I), v) for k, v in self.keywords]
        self._pkg_re = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in self.package_words) + r")\b", re.I) if self.package_words else None

    def parse(self, item_name: str | None, modifiers: str | None, sales_category: str | None = None) -> tuple[float | None, str]:
        """(ounces per unit or None, 'draft' | 'package'). Only call this for beer."""
        name = (item_name or "")
        mods = (modifiers or "")
        text = f"{name} | {mods}"
        # Whole words only. Unanchored, "can" matches "AmeriCAN IPA" and every pint in that category silently
        # becomes a can. The pack words are looked for in the MODIFIERS and the sales category, never the brand
        # name, for the same reason: "Bottle Rocket IPA" is a draft beer.
        packaged = (bool(re.search(r"\b(?:packaged?|cans?|bottles?|to-?go)\b", sales_category or "", re.I))
                    or bool(self._pkg_re and self._pkg_re.search(mods))
                    or bool(re.search(r"\b\d+\s*-?\s*(?:pk|pack)\b|\b\d+\s*/\s*\d+(?:\.\d+)?\s*oz\b", name, re.I)))
        # A crowler or growler is filled from the tap: it is draft volume however it leaves the building.
        if re.search(r"\b(?:crowler|growler|howler)s?\b", text, re.I):
            packaged = False

        # A flight is sized as a flight. Its modifiers list the beers in it, often with their own small pour
        # sizes ("Easy Eddy 5oz"), and reading the first of those would size the whole flight at five ounces.
        if re.search(r"\bflights?\b", text, re.I):
            return (self.flight_oz, "draft") if self.flight_oz else (None, "draft")
        # Modifiers beat the item name: the name is the brand, the modifier is what the guest actually got.
        for src in (mods, name):
            m2 = _MULTI2.search(src)
            if m2:
                return float(m2.group(1)) * float(m2.group(2)), "package"
            m = _OZ.search(src)
            if m:
                oz = float(m.group(1))
                if 0 < oz <= 128:
                    mult = _MULTI.search(src) if packaged else None
                    return (oz * int(mult.group(1)) if mult else oz), ("package" if packaged else "draft")
            ml = _ML.search(src)
            if ml:
                return round(float(ml.group(1)) / 29.5735, 1), ("package" if packaged else "draft")
        # Size WORDS are read from modifiers only. In a brand name they are just words: "Better Half Stout" is not
        # an eight-ounce pour. "1/2 pint" style fractions are handled before the bare keyword.
        frac = re.search(r"\b(?:1\s*/\s*2|half)\s+(pint|pour|liter|litre)\b", mods, re.I)
        if frac:
            base = {"pint": 16.0, "pour": 16.0, "liter": 33.8, "litre": 33.8}[frac.group(1).lower()]
            return base / 2, ("package" if packaged else "draft")
        for rx, oz in self._kw_re:
            if rx.search(mods):
                return oz, ("package" if packaged else "draft")
        # A crowler or growler sold as its own menu item carries the word in the name.
        m = re.search(r"\b(crowler|growler|howler)s?\b", name, re.I)
        if m:
            kw = dict(self.keywords).get(m.group(1).lower())
            if kw:
                return kw, "draft"
        if packaged:
            return None, "package"
        return (float(self.default_draft_oz) if self.default_draft_oz else None), "draft"


def keg_ounces(*texts: str | None) -> float | None:
    """Ounces in one unit of a purchased/counted beer product, read from its name, packaging or unit text.
    None when it is not recognisably a keg — cans and bottles are left out of keg yield on purpose."""
    blob = " ".join(t for t in texts if t)
    if not blob:
        return None
    for rx, oz in KEG_OZ:
        if rx.search(blob):
            return oz
    if re.search(r"\bkeg\b", blob, re.I):
        return None                                  # a keg, but of unstated size: report, do not assume
    return None


_BRAND_STRIP = re.compile(r"\b(?:\d+\s*/\s*\d+(?:\s*/\s*[\d.]+)?|\d+(?:\.\d+)?\s*(?:oz|ml|l|gal)|1\s*/\s*[246]\s*(?:bbl|barrel|bl)?|half|quarter|sixth|sixtel|bbl|barrel|keg|kegs|draft|draught|"
                          r"\d+\s*(?:pk|pack)|can|cans|bottle|bottles|case|ipa|pale|ale|lager|pils|pilsner|big grove|bgb|brewery|historical)\b", re.I)


def brand_key(name: str | None) -> str:
    """A loose key for matching a Toast item to a MarginEdge product or a VIP item: lower-case, sizes and pack
    words removed, punctuation collapsed. "Easy Eddy 1/2 BBL Keg" and "Easy Eddy" both become "easy eddy"."""
    s = _BRAND_STRIP.sub(" ", (name or "").lower())
    s = re.sub(r"[^a-z0-9]+", " ", s)
    # Word order is not stable between systems ("Lime Citrus Surfer" at the bar, "Citrus Surfer Lime" at the
    # distributor), so the key is the sorted set of words. Two different beers sharing every word is not a case
    # this menu has.
    key = " ".join(sorted(w for w in s.split() if w not in ("the", "a")))
    if key:
        return key
    # A name made ONLY of the words stripped above ("Big Grove IPA", "Pale Ale") would otherwise collapse to the
    # empty string, and every such beer would merge into one row. Fall back to the whole name.
    return " ".join(sorted(re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split()))


def brand_label(name: str | None) -> str:
    """The name with its size taken off, for display: "Easy Eddy 16oz" -> "Easy Eddy"."""
    s = re.sub(r"\s*\b\d+(?:\.\d+)?\s*(?:oz|ml)\b\.?", "", name or "", flags=re.I)
    return re.sub(r"\s{2,}", " ", s).strip(" -") or (name or "")
