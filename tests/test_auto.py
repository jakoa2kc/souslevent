"""Tests for the auto pipeline's pure logic: relief-adaptive partition + progress/ETA.

No WindNinja, no network, no display — the heavy orchestration (run_auto) is integration-only.
"""

from __future__ import annotations

from pathlib import Path

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


def test_parse_arome_hd_keeps_null_hour_placeholder():
    # A fully-null hour must keep a placeholder (aligned to `time`), not be dropped — dropping it
    # shifted every later hour, feeding the wrong hour's wind to the solver BC / arrows.
    from sillage.auto.wind import _parse_arome_hd, _series_at

    hourly = {
        "time": ["t0", "t1", "t2"],
        "wind_speed_120m": [5.0, None, 7.0], "wind_direction_120m": [90.0, None, 100.0],
        "wind_speed_80m": [None, None, None], "wind_direction_80m": [None, None, None],
        "wind_speed_10m": [None, None, None], "wind_direction_10m": [None, None, None],
    }
    out = _parse_arome_hd(hourly)
    assert len(out) == 3 and out[1] == ("t1", None, None)   # index still matches the clock hour
    assert _series_at(out, 2) == ("t2", 7.0, 100.0)         # hour 2 is itself
    assert _series_at(out, 1)[0] in ("t0", "t2")            # null hour falls back to a neighbour


def test_load_result_reads_legacy_rotor_turb_files(tmp_path):
    import json
    import zipfile

    from sillage.auto.store import _FORMAT, load_result

    zpath = tmp_path / "old.sillage"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("r.vtu", b"<VTKFile/>")
        z.writestr("manifest.json", json.dumps({
            "format": _FORMAT, "version": 1, "crs": "",
            "cases": [{"zone_index": 0, "hour": 9, "wind_speed_ms": 8.0, "wind_from_deg": 270.0,
                       "aoi_bounds": [0, 1, 0, 1], "elapsed_s": 1.0,
                       "rotor_file": "r.vtu", "turb_file": ""}],
            "config": {}, "route_cells": [], "hour_labels": {}}))
    loaded = load_result(zpath, tmp_path / "open")               # legacy keys -> vtu_paths
    assert loaded.result.cases[0].vtu_paths.get("rotor", "").endswith("r.vtu")


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


def test_resample_polyline_spaces_and_keeps_endpoints():
    from sillage.auto.partition import _resample_polyline

    assert _resample_polyline([], 1000.0) == []
    pts = _resample_polyline([(0.0, 0.0), (3000.0, 0.0)], 1000.0)
    assert pts[0] == (0.0, 0.0) and pts[-1] == (3000.0, 0.0)  # both endpoints kept
    assert len(pts) >= 4                                       # ~every 1 km over 3 km


def test_corridor_tiles_paves_route_without_gaps():
    from sillage.auto.partition import corridor_tiles

    dem = _dem(np.zeros((200, 200)), res_m=50.0)  # 10 x 10 km
    left, _b, _r, top = dem.bounds
    ymid = top - 5000.0
    route = [(left + 2000.0, ymid), (left + 8000.0, ymid)]  # straight ~6 km W->E
    tiles = corridor_tiles(dem, route, step_m=1500.0, half_m=1000.0, target_res_m=10.0)
    assert len(tiles) >= 4
    xs = sorted(t.center[0] for t in tiles)
    assert max(b - a for a, b in zip(xs, xs[1:])) <= 1500.0 + 1.0   # no gap larger than the step
    assert all(abs((t.bbox[2] - t.bbox[0]) - 2000.0) < 1e-6 for t in tiles)  # square, 2*half


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
    assert (44.6 - s) == pytest.approx(2000.0 / 111_320.0, rel=1e-3)  # unified km/deg (accurate)
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
    assert out.case_dir == case.case_dir and out.vtu_paths == {}


def test_compact_case_deletes_case_with_no_volumes(monkeypatch, tmp_path):
    from sillage.auto import scene
    from sillage.auto.pipeline import CaseResult, _compact_case

    # case readable but no lee volume (empty dict) -> delete the heavy files, keep the result
    monkeypatch.setattr(scene, "extract_case_source", lambda *a, **k: None)
    monkeypatch.setattr(scene, "extract_case_volumes", lambda *a, **k: {})
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
    assert out.case_dir == "" and out.vtu_paths == {}
    assert not case_dir.exists() and not run_dir.exists() and not crop.exists()


def test_compact_case_prefers_source_over_metric_volumes(monkeypatch, tmp_path):
    from sillage.auto import scene
    from sillage.auto.pipeline import CaseResult, _compact_case

    class Source:
        n_cells = 1

        def save(self, path):
            Path(path).write_bytes(b"source")

    monkeypatch.setattr(scene, "extract_case_source", lambda *a, **k: Source())
    monkeypatch.setattr(scene, "extract_case_volumes",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unused")))
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

    assert out.case_dir == "" and out.vtu_paths == {}
    assert out.source_path.endswith("z00_h09_source.vtu")
    assert Path(out.source_path).read_bytes() == b"source"
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


def test_store_save_load_roundtrip(tmp_path):
    from sillage.auto.pipeline import AutoConfig, AutoResult, CaseResult
    from sillage.auto.store import load_result, save_result

    rotor = tmp_path / "r.vtu"
    rotor.write_bytes(b"<VTKFile/>")  # dummy; the round-trip only references the path
    crs = CRS.from_epsg(32631)
    cases = [
        CaseResult(zone_index=0, hour=9, case_dir="", wind_speed_ms=8.0, wind_from_deg=270.0,
                   crs=crs, aoi_bounds=(0.0, 1.0, 0.0, 1.0), elapsed_s=1.0,
                   vtu_paths={"rotor": str(rotor), "vertical": str(rotor)}),
        CaseResult(zone_index=1, hour=10, case_dir="", wind_speed_ms=6.0, wind_from_deg=300.0,
                   crs=crs, aoi_bounds=(1.0, 2.0, 1.0, 2.0), elapsed_s=2.0,
                   vtu_paths={"rotor": str(rotor)}),
    ]
    result = AutoResult(dem_path="", crs=crs, partition=[], cases=cases, timings_summary="t")
    cfg = AutoConfig(bbox_latlon=(44.0, 6.0, 44.5, 6.5), hours=(9, 10),
                     route_segments=(((44.1, 6.1), (44.2, 6.2)),), domain_mode="corridor",
                     target_res_m=5.0, tile_step_m=1500.0)

    save_tmp = tmp_path / "save_tmp"
    out = save_result(tmp_path / "res", result, cfg=cfg,
                      hour_labels={9: "ven 9h", 10: "ven 10h"}, temp_dir=save_tmp)
    assert out.endswith(".sillage")
    assert save_tmp.exists() and not any(save_tmp.iterdir())

    loaded = load_result(out, tmp_path / "open")
    assert [c.hour for c in loaded.result.cases] == [9, 10]
    assert loaded.hours == [9, 10]
    assert loaded.hour_labels[9] == "ven 9h"
    assert loaded.storage_mode == "compact"
    assert loaded.config["domain_mode"] == "corridor" and loaded.config["target_res_m"] == 5.0
    assert len(loaded.route_segments) == 1 and len(loaded.route_segments[0]) == 2
    assert loaded.result.cases[0].vtu_paths["rotor"].endswith(".vtu")   # per-metric persisted mesh
    assert loaded.result.cases[0].vtu_paths["vertical"].endswith(".vtu")
    assert loaded.result.cases[0].wind_from_deg == 270.0


def test_store_reanalyzable_roundtrip_keeps_source(tmp_path):
    from sillage.auto.pipeline import AutoConfig, AutoResult, CaseResult
    from sillage.auto.store import load_result, save_result

    source = tmp_path / "source.vtu"
    source.write_bytes(b"<VTKFile/>")  # copied only; rendering tests cover readable meshes
    crs = CRS.from_epsg(32631)
    case = CaseResult(zone_index=0, hour=9, case_dir="", wind_speed_ms=8.0,
                      wind_from_deg=270.0, crs=crs, aoi_bounds=(0.0, 1.0, 0.0, 1.0),
                      elapsed_s=1.0, source_path=str(source))
    result = AutoResult(dem_path="", crs=crs, partition=[], cases=[case])
    cfg = AutoConfig(bbox_latlon=(44.0, 6.0, 44.5, 6.5), hours=(9,))

    save_tmp = tmp_path / "save_src_tmp"
    out = save_result(tmp_path / "res_src", result, cfg=cfg, hour_labels={9: "ven 9h"},
                      include_sources=True, temp_dir=save_tmp)
    loaded = load_result(out, tmp_path / "open_src")

    assert loaded.storage_mode == "reanalyzable"
    assert save_tmp.exists() and not any(save_tmp.iterdir())
    assert loaded.result.cases[0].source_path.endswith("source_000.vtu")
    assert loaded.result.cases[0].vtu_paths == {}


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
