"""Local upstream wind for each auto sub-domain — real **AROME 1.5 km**.

Each sub-domain's momentum BC is ONE upstream wind at its centre for the chosen hour. With
``source="arome"`` we read **AROME France HD (1.5 km)** height-above-ground wind from Open-Meteo's
``arome_france_hd`` model (keyless JSON — the Météo-France GRIB API only exposes 2.5 km wind at
10–100 m and GRIB-only, so this is both finer and simpler). We take the **highest available
height** (≈120 m AGL) as the near-free-stream wind feeding the lee. Distinct sub-zone centres land
in distinct AROME cells → the valley-scale spatial variation we want. Falls back to the Open-Meteo
crest blend (~11 km) per point if AROME HD is unavailable. ``source="open_meteo"`` keeps the blend.
"""

from __future__ import annotations

import math

# Open-Meteo arome_france_hd height-above-ground wind levels (m), highest first (180 m is null
# for HD; we pick the highest non-null per hour).
AROME_HD_HEIGHTS = (120, 80, 10)


def local_wind_provider(dem, crest_alt_m: float, n_hours: int, source: str = "open_meteo"):
    """Return ``make(hour) -> provider(x, y) -> (speed_ms, from_deg)`` for the flight window."""
    from ..wind.profile import window_forecast_provider

    if source != "arome":
        return window_forecast_provider(dem, crest_alt_m, n_hours=n_hours, source="open_meteo")

    from rasterio.crs import CRS
    from rasterio.warp import transform as warp_xy

    from ..wind.forecast import fetch_open_meteo
    from ..wind.profile import crest_height_series

    cache: dict = {}

    def series_at(x: float, y: float):
        lon, lat = warp_xy(dem.crs, CRS.from_epsg(4326), [x], [y])
        lon, lat = float(lon[0]), float(lat[0])
        key = (round(lat, 3), round(lon, 3))
        if key not in cache:
            s = _fetch_arome_hd(lat, lon, n_hours)
            if not s:  # AROME HD empty -> Open-Meteo crest blend fallback for this point
                s = crest_height_series(fetch_open_meteo(lat, lon, hours=n_hours), crest_alt_m)
            cache[key] = s
        return cache[key]

    def make(absolute_hour: int):
        def wind_at_center(x: float, y: float) -> tuple[float, float]:
            s = series_at(x, y)
            if not s:
                raise RuntimeError("vent local (AROME HD / Open-Meteo) indisponible à ce point")
            _t, spd, drc = s[min(int(absolute_hour), len(s) - 1)]
            return spd, drc

        return wind_at_center

    return make


def _fetch_arome_hd(lat: float, lon: float, n_hours: int, heights=AROME_HD_HEIGHTS):
    """Per-hour ``(time_iso, speed_ms, from_deg)`` from Open-Meteo AROME France HD, at the highest
    available height-above-ground. Hours run from today 00:00 Europe/Paris (so the index matches
    the pipeline's clock-hour offsets). Returns ``[]`` on any failure/empty."""
    import requests

    hourly = []
    for h in heights:
        hourly += [f"wind_speed_{h}m", f"wind_direction_{h}m"]
    try:
        r = requests.get("https://api.open-meteo.com/v1/meteofrance", params={
            "latitude": lat, "longitude": lon, "hourly": ",".join(hourly),
            "models": "arome_france_hd", "windspeed_unit": "ms", "timezone": "Europe/Paris",
            "forecast_days": min(7, max(1, math.ceil(n_hours / 24) + 1)),
        }, timeout=30)
        r.raise_for_status()
        hj = r.json().get("hourly", {})
    except Exception:
        return []
    return _parse_arome_hd(hj, heights)


def _parse_arome_hd(hourly: dict, heights=AROME_HD_HEIGHTS):
    """Pick, per hour, the wind at the highest height with a non-null value. Pure (testable)."""
    times = hourly.get("time", [])
    out = []
    for i, t in enumerate(times):
        for h in heights:  # highest first
            spd = hourly.get(f"wind_speed_{h}m", [])
            drc = hourly.get(f"wind_direction_{h}m", [])
            if i < len(spd) and spd[i] is not None and i < len(drc) and drc[i] is not None:
                out.append((t, float(spd[i]), float(drc[i])))
                break
    return out
