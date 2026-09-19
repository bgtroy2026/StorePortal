"""The morning email: one short section per taproom, plus a company roll-up for leadership.

A dashboard only works on the people who open it. The exception digest already decides, every night, what is
worth a director's attention — this puts that list in front of them at the start of the day instead of waiting
for them to come looking, and says nothing when there is nothing to say.

Built here, sent by the Apps Script (see sendDigests in apps-script/code.gs). The division is deliberate: the
pipeline knows the numbers but has no business holding anyone's email address; the Apps Script owns the roster
but cannot compute anything. So this module renders HTML keyed by location, encrypts it, and publishes it beside
the bundles; the script decides who receives which sections.

Apps Script has no AES, so the file uses a SHA-256 counter-mode stream (the same scheme as the Sales Portal's
Monday digest) keyed with HMAC(PORTAL_SECRET, "digest:<epoch>"). Confidentiality only — the file sits on a
public Pages URL like the bundles do, and like them it is rotated out of reach by bumping the epoch.
"""
from __future__ import annotations

import gzip
import hashlib
import html
import json
import os
import struct
from datetime import date, timedelta

SITE = "https://bgtroy2026.github.io/StorePortal/"
INK, MUTED, BAD, GOOD, RULE = "#1b2340", "#6b7280", "#b4413c", "#2e7d5b", "#e5e7eb"


def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out, i = bytearray(), 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + struct.pack(">I", i)).digest(); i += 1
    return bytes(out[:n])


def stream_encrypt(obj, key: bytes) -> bytes:
    data = gzip.compress(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), 9)
    nonce = os.urandom(16)
    return nonce + bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data))))


def stream_decrypt(buf: bytes, key: bytes):
    nonce, ct = buf[:16], buf[16:]
    return json.loads(gzip.decompress(bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct))))).decode("utf-8"))


def _money(v, d=0):
    return "—" if v is None else f"${v:,.{d}f}"


def _pct(v, d=0):
    return "—" if v is None else f"{v * 100:.{d}f}%"


def _chg(cur, prev, invert=False):
    if not cur or not prev:
        return f'<span style="color:{MUTED}">no comparison</span>'
    d = (cur - prev) / abs(prev)
    good = (d < 0) if invert else (d >= 0)
    return f'<span style="color:{GOOD if good else BAD}">{"▲" if d >= 0 else "▼"} {abs(d) * 100:.0f}%</span>'


def _day(payload, lid, d: str):
    k = payload["meta"]["daily_keys"]
    for r in payload["daily"].get(lid, []):
        if r[0] == d:
            return dict(zip(k, r))
    return None


def _sum(payload, lid, a: str, b: str):
    k = payload["meta"]["daily_keys"]
    out = {"net": 0.0, "guests": 0, "lc": 0.0, "n": 0}
    for r in payload["daily"].get(lid, []):
        if a <= r[0] <= b:
            o = dict(zip(k, r))
            out["net"] += o["net"] or 0; out["guests"] += o["guests"] or 0; out["lc"] += o["lc"] or 0; out["n"] += 1
    return out


WX = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog", 51: "drizzle", 53: "drizzle", 55: "drizzle",
      61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
      77: "snow", 80: "showers", 81: "showers", 82: "heavy showers", 85: "snow showers", 86: "snow showers", 95: "thunderstorms", 96: "thunderstorms", 99: "thunderstorms"}


def section(payload: dict, lid: str) -> dict | None:
    loc = next((l for l in payload["locations"] if l["id"] == lid), None)
    thr = payload["meta"]["through"]
    y = _day(payload, lid, thr)
    if not loc or not y:
        return None
    T = date.fromisoformat(thr)
    lw = _day(payload, lid, (T - timedelta(days=7)).isoformat())
    ly = _day(payload, lid, (T - timedelta(days=364)).isoformat())
    wk0 = T - timedelta(days=(T.weekday()))                      # Monday of the week `through` falls in
    wtd = _sum(payload, lid, wk0.isoformat(), thr)
    pwtd = _sum(payload, lid, (wk0 - timedelta(days=7)).isoformat(), (T - timedelta(days=7)).isoformat())
    pac = (payload.get("pacing") or {}).get(lid) or {}
    dig = (payload.get("digest") or {}).get(lid) or []
    fc = ((payload.get("weather") or {}).get(lid) or {}).get("forecast") or []
    day_name = T.strftime("%A")
    lab = (y["lc"] / y["net"]) if y["net"] else None

    def cell(label, value, sub):
        return (f'<td style="padding:10px 14px 10px 0;vertical-align:top"><div style="font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:{MUTED}">{label}</div>'
                f'<div style="font-size:22px;font-weight:700;color:{INK};line-height:1.25">{value}</div><div style="font-size:12px;color:{MUTED}">{sub}</div></td>')

    h = [f'<div style="border-top:3px solid {INK};padding-top:12px">',
         f'<div style="font-size:18px;font-weight:700;color:{INK}">{html.escape(loc["name"])}</div>',
         f'<div style="font-size:12px;color:{MUTED};margin-bottom:6px">{day_name} {T.strftime("%b")} {T.day}</div>',
         '<table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse"><tr>',
         cell("Net sales", _money(y["net"]), f'{_chg(y["net"], lw and lw["net"])} vs last {day_name}' + (f' · {_chg(y["net"], ly["net"])} vs last year' if ly and ly["net"] else "")),
         cell("Guests", f'{int(y["guests"] or 0):,}', f'{_chg(y["guests"], lw and lw["guests"])} vs last {day_name}'),
         cell("Labor", _pct(lab, 1), (f'target {_pct((pac.get("targets") or {}).get("labor"))}' if (pac.get("targets") or {}).get("labor") else "of net sales")),
         '</tr><tr>',
         cell("Week to date", _money(wtd["net"]), f'{_chg(wtd["net"], pwtd["net"])} vs same days last week'),
         cell("Month to date", _money((pac.get("mtd") or {}).get("net")), (f'pace {_pct(pac.get("pace_pct"))} of target' if pac.get("pace_pct") else f'{pac.get("days_elapsed", "")} days in')),
         cell("Forecast", _money(pac.get("forecast")), (f'{_pct(pac.get("forecast_pct"))} of {_money(pac.get("target"))}' if pac.get("target") else "to month end")),
         '</tr></table>']
    if dig:
        h.append(f'<div style="font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:{MUTED};margin:12px 0 4px">What needs you</div>')
        h.append('<table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%">')
        for it in dig[:8]:
            col = {"warn": BAD, "good": GOOD}.get(it[0], MUTED)
            h.append(f'<tr><td style="padding:5px 8px 5px 0;border-top:1px solid {RULE};width:74px;font-size:11px;color:{col};font-weight:700;text-transform:uppercase;vertical-align:top">{html.escape(it[1])}</td>'
                     f'<td style="padding:5px 0;border-top:1px solid {RULE};font-size:14px;color:{INK}">{html.escape(it[2])}'
                     + (f'<div style="font-size:12px;color:{MUTED}">{html.escape(it[3])}</div>' if it[3] else "") + '</td></tr>')
        h.append('</table>')
    else:
        h.append(f'<div style="font-size:14px;color:{GOOD};margin:12px 0 0">Nothing crossed a threshold — sales, labor, comps, voids, cash and inventory are all in their normal range.</div>')
    if fc:
        bits = []
        for f in fc[:3]:
            dd = date.fromisoformat(f[0])
            wet = f' · {f[3]:.1f}" rain' if (f[3] or 0) >= 0.1 else ""
            temps = f'{int(f[1])}°/{int(f[2])}° ' if (f[1] is not None and f[2] is not None) else ""
            bits.append(f'{dd.strftime("%a")} {temps}{WX.get(f[4], "")}{wet}')
        h.append(f'<div style="font-size:12px;color:{MUTED};margin-top:10px">Forecast: {html.escape(" &nbsp;|&nbsp; ".join(bits), quote=False).replace("&amp;nbsp;", "&nbsp;")}</div>')
    h.append(f'<div style="margin-top:12px"><a href="{SITE}#loc={lid}&page=overview" style="font-size:13px;color:#1d4ed8">Open {html.escape(loc["name"])} in the portal →</a></div></div>')
    n_warn = sum(1 for i in dig if i[0] == "warn")
    subject = f'{loc["name"]} · {day_name} {_money(y["net"])}' + (f' · {n_warn} thing{"s" if n_warn != 1 else ""} to look at' if n_warn else " · nothing urgent")
    return {"subject": subject, "html": "".join(h), "warn": n_warn}


def rollup(payload: dict, sections: dict) -> dict:
    thr = payload["meta"]["through"]; T = date.fromisoformat(thr)
    rows, tot, tot_lw = [], 0.0, 0.0
    for l in payload["locations"]:
        y = _day(payload, l["id"], thr); lw = _day(payload, l["id"], (T - timedelta(days=7)).isoformat())
        if not y:
            continue
        pac = (payload.get("pacing") or {}).get(l["id"]) or {}
        tot += y["net"] or 0; tot_lw += (lw or {}).get("net") or 0
        rows.append(f'<tr><td style="padding:6px 10px 6px 0;border-top:1px solid {RULE};font-size:14px;color:{INK}">{html.escape(l["name"])}</td>'
                    f'<td style="padding:6px 10px;border-top:1px solid {RULE};font-size:14px;text-align:right">{_money(y["net"])}</td>'
                    f'<td style="padding:6px 10px;border-top:1px solid {RULE};font-size:13px;text-align:right">{_chg(y["net"], lw and lw["net"])}</td>'
                    f'<td style="padding:6px 10px;border-top:1px solid {RULE};font-size:13px;text-align:right">{_pct((y["lc"] / y["net"]) if y["net"] else None, 1)}</td>'
                    f'<td style="padding:6px 10px;border-top:1px solid {RULE};font-size:13px;text-align:right">{_pct(pac.get("pace_pct")) if pac.get("pace_pct") else "—"}</td>'
                    f'<td style="padding:6px 0 6px 10px;border-top:1px solid {RULE};font-size:13px;text-align:right;color:{BAD if (sections.get(l["id"]) or {}).get("warn") else MUTED}">{(sections.get(l["id"]) or {}).get("warn", 0)}</td></tr>')
    th = f'padding:0 10px 4px;font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:{MUTED};text-align:right;font-weight:600'
    h = (f'<div style="border-top:3px solid {INK};padding-top:12px"><div style="font-size:18px;font-weight:700;color:{INK}">All taprooms</div>'
         f'<div style="font-size:12px;color:{MUTED};margin-bottom:8px">{T.strftime("%A %b")} {T.day} · {_money(tot)} net, {_chg(tot, tot_lw)} vs last {T.strftime("%A")}</div>'
         f'<table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%"><tr><th style="{th};text-align:left;padding-left:0">Taproom</th><th style="{th}">Net</th>'
         f'<th style="{th}">vs last wk</th><th style="{th}">Labor</th><th style="{th}">MTD pace</th><th style="{th};padding-right:0">Flags</th></tr>{"".join(rows)}</table></div>')
    body = h + "".join(f'<div style="height:26px"></div>{s["html"]}' for s in sections.values() if s and s.get("warn"))
    n = sum((s or {}).get("warn", 0) for s in sections.values())
    return {"subject": f'Taprooms · {T.strftime("%A")} {_money(tot)} · {n} flag{"s" if n != 1 else ""}', "html": body}


def build(payload: dict) -> dict:
    secs = {}
    for l in payload["locations"]:
        try:                                          # one taproom's bad row must not cost the other five their email
            sct = section(payload, l["id"])
            if sct:
                secs[l["id"]] = sct
        except Exception as e:
            from .util import log
            log.error("digest: section for %s failed (%s: %s)", l["id"], type(e).__name__, e)
    T = date.fromisoformat(payload["meta"]["through"])
    note = ""
    if (payload["meta"].get("provisional") or {}).get("marginedge"):
        note = "Inventory, purchases and P&L come from MarginEdge, which is still being set up — treat those lines as provisional. Sales, labor and cash come from Toast and tie to it to the cent."
    return {"built": payload["meta"]["built_at"], "through": payload["meta"]["through"], "through_label": f'{T.strftime("%a %b")} {T.day}',
            "head": '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:620px;margin:0 auto;padding:8px 4px">',
            "foot": f'<div style="margin-top:22px;padding-top:10px;border-top:1px solid {RULE};font-size:11px;color:{MUTED}">{note} '
                    f'Built {payload["meta"]["built_at"]}. You get this because you are on the Store Director Portal roster.</div></div>',
            "locations": secs, "all": rollup(payload, secs)}
