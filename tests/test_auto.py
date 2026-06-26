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


def test_feature_domains_places_one_domain_per_feature():
    from sillage.auto.partition import feature_domains

    n, res = 80, 50.0
    yy, xx = np.mgrid[0:n, 0:n]

    def cone(r0, c0, h, w):
        return np.clip(h * (1.0 - np.hypot(yy - r0, xx - c0) / w), 0.0, None)

    elev = 1000.0 + cone(20, 20, 500.0, 18) + cone(60, 60, 420.0, 18)  # two relief features
    dem = _dem(elev, res_m=res)
    hazard = np.clip(cone(20, 20, 1.0, 10) + cone(60, 60, 0.85, 10), 0.0, 1.0)  # two hazard peaks

    zones = feature_domains(dem, hazard, max_features=5, min_separation_m=800.0,
                            target_res_m=res, min_half_m=600.0)
    assert 2 <= len(zones) <= 5
    centres = {(round(z.center[0], -2), round(z.center[1], -2)) for z in zones}
    assert len(centres) >= 2  # the two distinct features
    for z in zones:                                  # square domains, sized to >= 2*min_half
        width = z.bbox[2] - z.bbox[0]
        assert width >= 2 * 600.0 - 1
        assert abs((z.bbox[3] - z.bbox[1]) - width) < 1.0


def test_corridor_mask_keeps_band_around_route():
    from sillage.auto.partition import corridor_mask

    dem = _dem(np.zeros((100, 100)), res_m=50.0)  # 5 x 5 km
    left, _b, right, top = dem.bounds
    xmid = (left + right) / 2.0
    route_xy = [(xmid, top - 1000.0), (xmid, top - 4000.0)]  # vertical segment, mid-domain
    mask = corridor_mask(dem, route_xy, margin_m=300.0)

    def px(x, y):
        return int(round((top - y) / 50.0)), int(round((x - left) / 50.0))

    on = px(xmid, top - 2500.0)
    far = px(xmid + 1000.0, top - 2500.0)
    assert mask[on] and not mask[far]          # within 300 m kept, 1 km away dropped
    assert 0 < mask.sum() < mask.size


def test_bbox_from_route():
    from sillage.auto.pipeline import bbox_from_route

    s, w, n, e = bbox_from_route([(44.6, 6.1), (44.7, 6.3)], margin_km=2.0)
    assert s < 44.6 and n > 44.7 and w < 6.1 and e > 6.3
    assert (44.6 - s) == pytest.approx(2.0 / 111.0, rel=1e-3)
    with pytest.raises(ValueError, match="route vide"):
        bbox_from_route([], margin_km=2.0)


def test_sample_route_keeps_endpoints_and_densifies():
    from sillage.auto.wind import _sample_route

    assert _sample_route([], spacing_km=1.5) == []
    start, end = (44.0, 6.0), (44.05, 6.0)  # ~5.5 km north along a meridian
    pts = _sample_route([start, end], spacing_km=1.5)
    assert pts[0] == start and pts[-1] == end          # endpoints preserved
    assert all(abs(p[1] - 6.0) < 1e-9 for p in pts)    # all on the meridian
    assert [p[0] for p in pts] == sorted(p[0] for p in pts)  # ordered along the route
    assert len(_sample_route([start, end], spacing_km=0.5)) > len(pts)  # finer -> more samples


def test_arrows_at_hour_indexes_and_clamps():
    from sillage.auto.wind import arrows_at_hour

    cells = [(44.0, 6.0, [("t0", 3.0, 90.0), ("t1", 5.0, 100.0)]),
             (44.1, 6.1, [])]                       # empty series -> skipped
    assert arrows_at_hour(cells, -5) == [(44.0, 6.0, 3.0, 90.0)]  # clamps to the first hour
    assert arrows_at_hour(cells, 1) == [(44.0, 6.0, 5.0, 100.0)]
    assert arrows_at_hour(cells, 9) == [(44.0, 6.0, 5.0, 100.0)]  # clamps to the last hour


def test_cleanup_auto_artifacts_drops_cases_keeps_dem_and_screening(tmp_path):
    from sillage.auto.pipeline import cleanup_auto_artifacts

    work = tmp_path / "cache" / "auto"
    (work / "NINJAFOAM_z00_h09_1_0" / "system").mkdir(parents=True)
    (work / "z00_h09_run").mkdir()
    (work / "z00_h09.tif").write_bytes(b"crop")
    (work / "z00_h09_rotor.vtu").write_bytes(b"rotor")
    (work / "dem_keep.tif").write_bytes(b"dem")      # the reusable fine DEM
    (work / "screening").mkdir()                      # the reusable Pass-1 cache

    cleanup_auto_artifacts(tmp_path / "cache")

    assert not (work / "NINJAFOAM_z00_h09_1_0").exists()
    assert not (work / "z00_h09_run").exists()
    assert not (work / "z00_h09.tif").exists()
    assert not (work / "z00_h09_rotor.vtu").exists()
    assert (work / "dem_keep.tif").exists()           # kept (expensive to refetch)
    assert (work / "screening").exists()


def test_screening_work_dir_is_keyed_by_dem_and_wind(tmp_path):
    from sillage.auto.pipeline import _screening_work_dir

    a = _screening_work_dir(tmp_path, tmp_path / "dem_a.tif", 8.0, 270.0, 150.0)
    b = _screening_work_dir(tmp_path, tmp_path / "dem_b.tif", 8.0, 270.0, 150.0)
    c = _screening_work_dir(tmp_path, tmp_path / "dem_a.tif", 10.0, 300.0, 150.0)
    assert a != b != c
    assert "dem_a" in str(a) and "270_8_150m" in str(a)


def test_compact_case_keeps_case_when_unreadable(tmp_path):
    # If the OpenFOAM case can't be read, _compact_case must NOT delete it or lose the result.
    from sillage.auto.pipeline import CaseResult, _compact_case

    case = CaseResult(zone_index=0, hour=9, case_dir=str(tmp_path / "nope"),
                      wind_speed_ms=8.0, wind_from_deg=270.0, crs=None,
                      aoi_bounds=(0.0, 1.0, 0.0, 1.0), elapsed_s=1.0)
    out = _compact_case(case, tmp_path)
    assert out.case_dir == case.case_dir and out.rotor_path == ""


def test_compact_case_deletes_empty_rotor_case(monkeypatch, tmp_path):
    from sillage.auto import scene
    from sillage.auto.pipeline import CaseResult, _compact_case

    class EmptyRotor:
        n_cells = 0

    monkeypatch.setattr(scene, "extract_rotor", lambda *a, **k: EmptyRotor())
    case_dir = tmp_path / "NINJAFOAM_z00_h09"
    run_dir = tmp_path / "z00_h09_run"
    crop = tmp_path / "z00_h09.tif"
    (case_dir / "system").mkdir(parents=True)
    run_dir.mkdir()
    crop.write_bytes(b"crop")

    case = CaseResult(zone_index=0, hour=9, case_dir=str(case_dir),
                      wind_speed_ms=8.0, wind_from_deg=270.0, crs=None,
                      aoi_bounds=(0.0, 1.0, 0.0, 1.0), elapsed_s=1.0)
    out = _compact_case(case, tmp_path)
    assert out.case_dir == "" and out.rotor_path == ""
    assert not case_dir.exists() and not run_dir.exists() and not crop.exists()


def test_free_gb_reads_volume(tmp_path):
    from sillage.auto.pipeline import _free_gb

    assert _free_gb(tmp_path) > 0.0                    # a real volume reports positive free space
    assert _free_gb("\x00 not a path") == float("inf") # unreadable -> never blocks the run


def test_default_momentum_workers_uses_all_detected_cores(monkeypatch):
    from sillage.auto import pipeline

    monkeypatch.setattr(pipeline, "detect_cores", lambda: 14)
    assert pipeline.default_momentum_workers() == 14
    monkeypatch.setattr(pipeline, "detect_cores", lambda: 2)
    assert pipeline.default_momentum_workers() == 2


def test_momentum_parallel_plan_integer_division():
    from sillage.auto.pipeline import momentum_parallel_plan

    plan = momentum_parallel_plan(4, cores=14)
    assert plan.workers == 4
    assert plan.threads_per_worker == 3
    assert plan.used_cores == 12
    assert plan.idle_cores == 2
    assert plan.perfect_workers == (1, 2, 7, 14)

    capped = momentum_parallel_plan(14, cores=14, task_count=6)
    assert capped.workers == 6
    assert capped.threads_per_worker == 2
    assert capped.used_cores == 12
    assert capped.idle_cores == 2


def test_progress_tracker_wave_eta_and_summary():
    clk = {"t": 0.0}
    t = ProgressTracker(total=4, workers=2, clock=lambda: clk["t"])
    assert t.eta_seconds is None          # nothing done yet
    assert t.fraction == 0.0
    t.start()

    clk["t"] = 10.0                       # wave 1 (2 parallel solves) finishes at t=10 s
    t.record(10.0)
    t.record(10.0)
    assert t.done == 2 and abs(t.fraction - 0.5) < 1e-9
    # total estimate = mean 10 s × ceil(4/2)=2 waves = 20 s; elapsed 10 → reste ~10 s
    assert abs(t.eta_seconds - 10.0) < 1e-9
    s = t.summary("Pass-2 auto")
    assert "2/4" in s and "50%" in s and "reste" in s

    clk["t"] = 20.0
    t.record(10.0)
    t.record(10.0)
    assert t.eta_seconds == 0.0
    assert "100%" in t.summary()


def test_progress_tracker_eta_collapses_with_parallel_workers():
    # The reported bug: 5 solves on 5 workers = ONE wave. When the first completes near the end,
    # the ETA must be ~0 (not mean × 4 remaining = the old worker-count over-estimate).
    clk = {"t": 0.0}
    t = ProgressTracker(total=5, workers=5, clock=lambda: clk["t"])
    t.start()
    clk["t"] = 300.0
    t.record(300.0)                       # 1/5 done, but the single wave is essentially finished
    assert t.eta_seconds is not None and t.eta_seconds < 1.0   # NOT ~1200 s
    assert t.display_percent >= 99        # reflects reality: ~done, not 20%
