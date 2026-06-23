"""Reduce a vertical wind profile to the quantity the solvers need: crest-height wind.

Pass 1 (mass) can be initialized with a domain-average wind per hour; Pass 2 (momentum)
needs a single homogeneous wind. Both want the wind at roughly ridge-crest elevation.
This module interpolates a HourlyProfile to a target altitude.

Wind is interpolated in vector (u, v) space to avoid direction-averaging artifacts, then
converted back to (speed, from-direction). Meteorological convention throughout.
"""

from __future__ import annotations

import numpy as np

from .forecast import HourlyProfile


def _to_uv(speed_ms: float, from_deg: float) -> tuple[float, float]:
    # meteorological 'from' direction -> math vector pointing where wind goes
    rad = np.deg2rad(from_deg)
    u = -speed_ms * np.sin(rad)  # east-west
    v = -speed_ms * np.cos(rad)  # north-south
    return u, v


def _from_uv(u: float, v: float) -> tuple[float, float]:
    speed = float(np.hypot(u, v))
    from_deg = float(np.mod(np.degrees(np.arctan2(-u, -v)), 360.0))
    return speed, from_deg


def wind_at_altitude(profile: HourlyProfile, target_alt_m: float) -> tuple[float, float]:
    """Interpolate (speed_ms, from_deg) at `target_alt_m` from a vertical profile.

    Requires samples with altitude_m. Falls back to the nearest level if outside range.
    """
    samples = [s for s in profile.samples if s.altitude_m is not None]
    if not samples:
        raise ValueError(
            f"No altitude-tagged samples for {profile.time_iso}; cannot interpolate. "
            "Ensure geopotential height was fetched (wind/forecast.py)."
        )
    samples.sort(key=lambda s: s.altitude_m)
    alts = np.array([s.altitude_m for s in samples])
    us, vs = zip(*[_to_uv(s.speed_ms, s.from_deg) for s in samples])
    u = float(np.interp(target_alt_m, alts, us))
    v = float(np.interp(target_alt_m, alts, vs))
    return _from_uv(u, v)


def crest_height_series(
    profiles: list[HourlyProfile], crest_alt_m: float
) -> list[tuple[str, float, float]]:
    """Per-hour (time_iso, speed_ms, from_deg) at crest altitude for the whole window."""
    out = []
    for p in profiles:
        try:
            spd, drc = wind_at_altitude(p, crest_alt_m)
        except ValueError:
            continue
        out.append((p.time_iso, spd, drc))
    return out


def crest_wind_provider(dem, crest_alt_m: float, hour_index: int = 0,
                        source: str = "open_meteo", cache: dict | None = None):
    """Build ``wind_at_center(x, y) -> (speed_ms, from_deg)`` for sub-zone Pass-1 (ADR-0007).

    Samples the forecast at each point's lon/lat, reduced to ``crest_alt_m``, for the given
    ``hour_index``. ``source`` is "open_meteo" or "arome". Network per distinct point;
    results are memoized in ``cache`` (a dict) keyed by rounded lon/lat to avoid refetching
    nearby tile centres.
    """
    from rasterio.crs import CRS
    from rasterio.warp import transform as warp_xy

    from .forecast import fetch_arome, fetch_open_meteo

    fetch = fetch_arome if source == "arome" else fetch_open_meteo
    store = {} if cache is None else cache

    def wind_at_center(x: float, y: float) -> tuple[float, float]:
        lon, lat = warp_xy(dem.crs, CRS.from_epsg(4326), [x], [y])
        lon, lat = float(lon[0]), float(lat[0])
        key = (round(lat, 3), round(lon, 3))
        if key not in store:
            store[key] = crest_height_series(fetch(lat, lon, hours=hour_index + 1), crest_alt_m)
        series = store[key]
        if not series or hour_index >= len(series):
            raise RuntimeError(f"no forecast crest wind at ({lat:.3f}, {lon:.3f})")
        _t, spd, drc = series[hour_index]
        return spd, drc

    return wind_at_center
