"""Daily weather per taproom, from Open-Meteo (https://open-meteo.com — free, no key, no account).

A taproom with a patio does not trade the same on a wet Saturday as a dry one, and without the weather beside
it a director reading "down 18% on last Saturday" has to remember what the sky was doing. Two endpoints:

  archive-api.open-meteo.com/v1/archive   observed history, but it lags real time by several days
  api.open-meteo.com/v1/forecast          the last few days (past_days) plus the coming week

The forecast endpoint fills the gap the archive leaves, and its future days are stored with kind='forecast' so
they can never be mistaken for something that happened. Coordinates come from config/locations.json (lat/lon);
a location without them is skipped, and the portal simply shows no weather for it.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from .util import DB_PATH, Http, iso, log, today_local, write_raw

DAILY = "temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code"
COMMON = {"daily": DAILY, "temperature_unit": "fahrenheit", "precipitation_unit": "inch"}


def _have() -> dict[str, int]:
    if not DB_PATH.exists():
        return {}
    con = sqlite3.connect(DB_PATH)
    try:
        return dict(con.execute("SELECT location_id, COUNT(*) FROM weather_daily WHERE kind='observed' GROUP BY 1").fetchall())
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()


def _days(j: dict, kind_after: str | None = None) -> list[dict]:
    d = j.get("daily") or {}
    out = []
    today = iso(today_local())
    for i, day in enumerate(d.get("time") or []):
        def g(k):
            v = (d.get(k) or [None] * (i + 1))[i]
            return v
        if g("temperature_2m_max") is None and g("precipitation_sum") is None:
            continue
        out.append({"date": day, "tmax_f": g("temperature_2m_max"), "tmin_f": g("temperature_2m_min"),
                    "precip_in": g("precipitation_sum"), "code": g("weather_code"),
                    "kind": "forecast" if day >= today else "observed"})
    return out


def _pull_one(arch, fc, lat, lon, tz, have_n: int, end, days_back: int) -> list[dict]:
    days = []
    # History once; after that the nightly forecast call (past_days) keeps it topped up.
    if have_n < days_back * 0.8:
        r = arch.get("/archive", params=dict(COMMON, latitude=lat, longitude=lon, timezone=tz,
                                             start_date=iso(end - timedelta(days=days_back)), end_date=iso(end - timedelta(days=5))))
        days += _days(r.json())
    r = fc.get("/forecast", params=dict(COMMON, latitude=lat, longitude=lon, timezone=tz, past_days=14, forecast_days=8))
    recent = _days(r.json())
    seen = {d["date"] for d in recent}
    return [d for d in days if d["date"] not in seen] + recent


def pull(locations: list[dict], days_back: int = 400) -> dict:
    have = _have()
    arch = Http("https://archive-api.open-meteo.com/v1", rps=2)
    fc = Http("https://api.open-meteo.com/v1", rps=2)
    end = today_local() - timedelta(days=1)
    summary = {}
    for loc in locations:
        slug, lat, lon = loc["slug"], loc.get("lat"), loc.get("lon")
        if lat is None or lon is None:
            continue
        try:
            days = _pull_one(arch, fc, lat, lon, loc.get("timezone") or "America/Chicago", have.get(slug, 0), end, days_back)
        except Exception as e:                       # one taproom's weather must not cost the other five theirs
            log.warning("weather: %s not pulled (%s: %s)", slug, type(e).__name__, e)
            continue
        write_raw("weather", slug, "daily", "latest", {"days": days})
        summary[slug] = len(days)
    log.info("weather: %s", summary)
    return summary
