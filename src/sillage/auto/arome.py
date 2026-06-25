"""AROME availability for the auto pipeline (Météo-France, key in .env — ADR-0016).

Drives the auto window's **time axis**: the slider graduations are real **absolute dates** over
the window AROME actually covers (its run + ~48 h horizon), and we label whether the run will use
AROME (valid key) or fall back to Open-Meteo. The wind *values* still come from Open-Meteo until
the GRIB ingest lands (`auto.wind`); this connects the key + the available window first.

``forecast_window`` is offline (validates the JWT, computes the schedule) so it never blocks the
UI; the heavy GRIB fetch is a separate, worker-thread step.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..wind.meteofrance import check_arome_key

AROME_HORIZON_H = 48  # AROME-France practical forecast horizon (h)
_TZ = "Europe/Paris"


@dataclass(frozen=True)
class ForecastWindow:
    """The available forecast window in absolute time + offsets for the hourly provider."""

    start: datetime          # earliest available hour (aware, Europe/Paris)
    end: datetime            # latest available hour
    base_midnight: datetime  # today 00:00 Europe/Paris = the provider's hour-0
    source: str              # "AROME" | "Open-Meteo"
    note: str                # human status (key expiry / fallback reason)

    @property
    def start_offset_h(self) -> int:
        return int(round((self.start - self.base_midnight).total_seconds() / 3600))

    @property
    def end_offset_h(self) -> int:
        return int(round((self.end - self.base_midnight).total_seconds() / 3600))

    def at(self, offset_h: int) -> datetime:
        return self.base_midnight + timedelta(hours=int(offset_h))

    def label_at(self, offset_h: int) -> str:
        """Absolute date/hour label for a slider graduation, e.g. ``mer. 25/06 14h``."""
        from ..screening.pass1 import _FR_DAYS

        t = self.at(offset_h)
        return f"{_FR_DAYS[t.weekday()]} {t:%d/%m %Hh}"


def forecast_window(api_key, *, now: datetime | None = None,
                    horizon_h: int = AROME_HORIZON_H) -> ForecastWindow:
    """The AROME availability window (now → now + horizon) in absolute dates, tagged with whether
    a valid AROME key is present (else Open-Meteo). Offline: validates the JWT + computes the
    schedule, no network."""
    tz = ZoneInfo(_TZ)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = now.replace(minute=0, second=0, microsecond=0)

    status = check_arome_key(api_key)
    if status.ok and status.reason in ("ok", "expiring_soon"):
        source, note = "AROME", status.message
    elif not api_key:
        source, note = "Open-Meteo", "Pas de clé AROME — repli Open-Meteo (~11 km)."
    else:
        source, note = "Open-Meteo", f"Clé AROME inutilisable ({status.reason}) — repli Open-Meteo."

    return ForecastWindow(
        start=start, end=start + timedelta(hours=int(horizon_h)),
        base_midnight=midnight, source=source, note=note)
