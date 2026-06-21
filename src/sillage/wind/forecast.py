"""Wind forecast acquisition (Open-Meteo / AROME), network-isolated for mockability.

Returns wind by altitude (pressure level), hour by hour, for an area. The reduction to
the single quantity the solvers need (crest-height wind) lives in wind/profile.py.

Open-Meteo provides wind by pressure level hourly and needs no key for typical use; AROME
(Meteo-France, ~1.3 km) needs an API key for finer local structure. See docs/04.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


@dataclass
class WindSample:
    """Wind at one pressure level / altitude for one hour."""

    time_iso: str
    pressure_hpa: float | None
    altitude_m: float | None
    speed_ms: float
    from_deg: float  # meteorological (from-direction)


@dataclass
class HourlyProfile:
    """A vertical stack of WindSample for one hour at one location."""

    time_iso: str
    samples: list[WindSample]


def fetch_open_meteo(
    lat: float,
    lon: float,
    pressure_levels_hpa: tuple[int, ...] = (1000, 925, 850, 700, 600, 500),
    hours: int = 24,
    timeout_s: float = 30.0,
) -> list[HourlyProfile]:
    """Fetch hourly wind by pressure level from Open-Meteo for a point.

    Returns one HourlyProfile per hour. Cache the raw response upstream for reproducible,
    offline-replayable debugging (docs/04 caching). This builds the variable list for
    wind speed/direction at each requested pressure level.
    """
    speed_vars = [f"windspeed_{p}hPa" for p in pressure_levels_hpa]
    dir_vars = [f"winddirection_{p}hPa" for p in pressure_levels_hpa]
    height_vars = [f"geopotential_height_{p}hPa" for p in pressure_levels_hpa]
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(speed_vars + dir_vars + height_vars),
        "windspeed_unit": "ms",
        "forecast_days": max(1, (hours + 23) // 24),
    }
    resp = requests.get(OPEN_METEO_URL, params=params, timeout=timeout_s)
    resp.raise_for_status()
    data = resp.json()["hourly"]

    times = data["time"][:hours]
    profiles: list[HourlyProfile] = []
    for i, t in enumerate(times):
        samples: list[WindSample] = []
        for p in pressure_levels_hpa:
            spd = data.get(f"windspeed_{p}hPa", [None] * len(times))[i]
            drc = data.get(f"winddirection_{p}hPa", [None] * len(times))[i]
            hgt = data.get(f"geopotential_height_{p}hPa", [None] * len(times))[i]
            if spd is None or drc is None:
                continue
            samples.append(
                WindSample(
                    time_iso=t, pressure_hpa=float(p), altitude_m=hgt,
                    speed_ms=float(spd), from_deg=float(drc),
                )
            )
        profiles.append(HourlyProfile(time_iso=t, samples=samples))
    return profiles


# AROME high-resolution client: add when wiring finer local forecasts (docs/04, roadmap).
def fetch_arome(*args, **kwargs):  # pragma: no cover - stub
    raise NotImplementedError(
        "AROME (Meteo-France) client not implemented yet. Needs METEOFRANCE_API_KEY. "
        "See docs/04_data_sources.md and roadmap M5."
    )
