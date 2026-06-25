"""Tests for the auto pipeline's pure logic: relief-adaptive partition + progress/ETA.

No WindNinja, no network, no display — the heavy orchestration (run_auto) is integration-only.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("scipy")
from rasterio.crs import CRS  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402

from sillage.auto.partition import estimate_cells, partition_zone  # noqa: E402
from sillage.auto.progress import ProgressTracker  # noqa: E402
from sillage.terrain.dem import Dem  # noqa: E402


def _dem(z: np.ndarray, res_m: float = 50.0) -> Dem:
    return Dem(elevation=z.astype("float32"),
               transform=from_origin(600000.0, 4900000.0, res_m, res_m),
               crs=CRS.from_epsg(32631), resolution_m=res_m)


def test_estimate_cells():
    assert estimate_cells(1000.0, 1000.0, 10.0) == 10_000  # 100 x 100
    assert estimate_cells(0, 0, 0) == 0


def test_partition_flat_zone_is_one_subdomain():
    dem = _dem(np.full((40, 40), 1000.0))  # 2 x 2 km, no relief
    zones = partition_zone(dem, target_res_m=50.0, max_cells=600_000, max_relief_m=400.0)
    assert len(zones) == 1
    z = zones[0]
    assert z.relief_m == 0.0
    assert z.est_cells == 40 * 40  # (2000/50)^2
    assert z.pixel_window == (0, 40, 0, 40)


def test_partition_splits_on_relief_and_covers_the_grid():
    n = 64
    grad = np.linspace(0.0, 1000.0, n)[:, None] * np.ones((1, n))  # 1000 m N-S relief
    dem = _dem(grad, res_m=50.0)
    # cells budget huge -> only the relief cap drives splitting; min tile small enough to meet it
    zones = partition_zone(dem, target_res_m=50.0, max_cells=10_000_000,
                           max_relief_m=300.0, min_tile_m=400.0)

    assert len(zones) > 1  # the relief cap forced subdivision
    # leaves tile the grid completely and without overlap
    covered = sum((r1 - r0) * (c1 - c0) for r0, r1, c0, c1 in (z.pixel_window for z in zones))
    assert covered == n * n
    # every leaf now respects the relief cap (the min tile is small enough to reach it)
    assert all(z.relief_m <= 300.0 + 1.0 for z in zones)


def test_partition_splits_on_cell_budget():
    # Flat but large: 6.4 x 6.4 km at 10 m target = ~410k cells > 100k budget -> must split.
    dem = _dem(np.full((128, 128), 500.0), res_m=50.0)
    zones = partition_zone(dem, target_res_m=10.0, max_cells=100_000, max_relief_m=1000.0)
    assert len(zones) >= 4
    assert all(z.est_cells <= 100_000 for z in zones)


def test_parse_arome_hd_picks_highest_available_height():
    from sillage.auto.wind import _parse_arome_hd

    hourly = {
        "time": ["2026-06-26T00:00", "2026-06-26T01:00"],
        "wind_speed_120m": [None, 12.0], "wind_direction_120m": [None, 270.0],
        "wind_speed_80m": [8.0, 9.0], "wind_direction_80m": [260.0, 265.0],
        "wind_speed_10m": [3.0, 4.0], "wind_direction_10m": [250.0, 255.0],
    }
    out = _parse_arome_hd(hourly)
    assert out[0] == ("2026-06-26T00:00", 8.0, 260.0)   # 120 m null -> falls to 80 m
    assert out[1] == ("2026-06-26T01:00", 12.0, 270.0)  # 120 m present -> highest


def test_forecast_window_fallback_and_absolute_labels():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from sillage.auto.arome import forecast_window

    now = datetime(2026, 6, 25, 14, 30, tzinfo=ZoneInfo("Europe/Paris"))
    fc = forecast_window(None, now=now, horizon_h=48)  # no key -> Open-Meteo
    assert fc.source == "Open-Meteo"
    assert fc.start_offset_h == 14            # 14:00 -> 14 h from today midnight
    assert fc.end_offset_h - fc.start_offset_h == 48
    assert "14h" in fc.label_at(fc.start_offset_h)   # absolute date/hour graduation
    assert fc.at(fc.start_offset_h).hour == 14


def test_progress_tracker_eta_and_summary():
    t = ProgressTracker(total=4)
    assert t.eta_seconds is None          # nothing done yet
    assert t.fraction == 0.0

    t.record(2.0)
    t.record(2.0)
    assert t.done == 2 and abs(t.fraction - 0.5) < 1e-9
    assert abs(t.eta_seconds - 4.0) < 1e-9  # mean 2.0 s × 2 remaining
    s = t.summary("Pass-2 auto")
    assert "2/4" in s and "50%" in s and "reste" in s

    t.record(2.0)
    t.record(2.0)
    assert t.eta_seconds == 0.0
    assert "100%" in t.summary()
