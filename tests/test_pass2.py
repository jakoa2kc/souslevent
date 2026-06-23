"""Tests for the Pass-2 plumbing (crop, OpenFOAM case discovery, momentum flags) and the
Pass-1 hourly timeline GIF. All synthetic / filesystem-only — no WindNinja binary, no
network, no display (matplotlib forced to Agg).
"""

from __future__ import annotations

import matplotlib
import numpy as np
import pytest

matplotlib.use("Agg")  # headless, before pyplot is imported anywhere

pytest.importorskip("scipy")
from rasterio.transform import from_origin  # noqa: E402
from rasterio.crs import CRS  # noqa: E402

from sillage.terrain.dem import Dem, crop_dem  # noqa: E402
from sillage.flow.windninja import locate_openfoam_case, run_momentum, FLAG  # noqa: E402
from sillage.flow.openfoam_reader import read_terrain_stl  # noqa: E402
from sillage.viz.map2d import save_timeline_gif  # noqa: E402
from sillage.viz.volume3d import mean_flow_vector  # noqa: E402


def _synthetic_dem(n: int = 100, res_m: float = 50.0) -> Dem:
    z = np.random.default_rng(0).random((n, n)).astype("float32") * 100.0
    transform = from_origin(600000.0, 4900000.0, res_m, res_m)  # north-up
    return Dem(elevation=z, transform=transform, crs=CRS.from_epsg(32631), resolution_m=res_m)


def test_crop_dem_centered_window():
    dem = _synthetic_dem(n=100, res_m=50.0)  # 5 km square starting at (600000, 4900000)
    left, bottom, right, top = dem.bounds
    cx, cy = (left + right) / 2, (bottom + top) / 2
    crop = crop_dem(dem, cx, cy, half_width_m=1000.0)  # ~2 km window
    cl, cb, cr, ct = crop.bounds
    assert crop.resolution_m == dem.resolution_m
    assert crop.crs == dem.crs
    # window ~2 km on a side, well inside the parent
    assert 1800 <= (cr - cl) <= 2200 and 1800 <= (ct - cb) <= 2200
    assert cl >= left and ct <= top and cr <= right and cb >= bottom


def test_crop_dem_outside_extent_raises():
    dem = _synthetic_dem()
    with pytest.raises(ValueError):
        crop_dem(dem, 0.0, 0.0, half_width_m=500.0)  # far outside the DEM


def test_locate_openfoam_case_finds_ninjafoam(tmp_path):
    # NinjaFOAM writes the case next to the DEM, not in the run working dir.
    dem_parent = tmp_path / "pass2"
    work = dem_parent / "smoke_run"
    work.mkdir(parents=True)
    case = dem_parent / "NINJAFOAM_crop_123_4"
    (case / "system").mkdir(parents=True)
    (case / "constant").mkdir()
    (case / "0").mkdir()

    found = locate_openfoam_case(work, "", extra_roots=[dem_parent])
    assert found is not None
    assert found.resolve() == case.resolve()


def test_locate_openfoam_case_none_when_absent(tmp_path):
    assert locate_openfoam_case(tmp_path, "", extra_roots=[tmp_path]) is None


def test_momentum_turbulence_requires_goog_output():
    on = run_momentum(cli="WindNinja_cli", dem_path="/tmp/c.tif", working_dir="/tmp/m",
                      wind_speed_ms=8.0, wind_from_deg=320.0, turbulence_output=True,
                      dry_run=True)
    assert "--write_goog_output=true" in " ".join(on.command)
    off = run_momentum(cli="WindNinja_cli", dem_path="/tmp/c.tif", working_dir="/tmp/m",
                       wind_speed_ms=8.0, wind_from_deg=320.0, turbulence_output=False,
                       dry_run=True)
    assert "--write_goog_output=true" not in " ".join(off.command)
    assert f"--{FLAG['turbulence_out']}=false" in " ".join(off.command)


def test_save_timeline_gif(tmp_path):
    dem = _synthetic_dem(n=40, res_m=50.0)
    stack = [np.random.default_rng(i).random(dem.shape) for i in range(3)]
    labels = [f"h{i:02d}" for i in range(3)]
    out = save_timeline_gif(dem, stack, labels, tmp_path / "timeline.gif", fps=2)
    assert out.exists() and out.stat().st_size > 0


def test_mean_flow_vector_points_downwind():
    # wind FROM west (270) blows TO the east (+x)
    v = mean_flow_vector(270.0)
    assert v[0] > 0.99 and abs(v[1]) < 1e-6 and v[2] == 0.0
    # wind FROM south (180) blows TO the north (+y)
    v = mean_flow_vector(180.0)
    assert v[1] > 0.99 and abs(v[0]) < 1e-6


def test_read_terrain_stl_absent_and_present(tmp_path):
    import pyvista as pv

    assert read_terrain_stl(tmp_path) is None  # no constant/triSurface
    tri = tmp_path / "constant" / "triSurface"
    tri.mkdir(parents=True)
    pv.Plane().save(str(tri / "ground.stl"))
    surf = read_terrain_stl(tmp_path)
    assert surf is not None and surf.n_points > 0


def test_draw_indicator_on_axes():
    import matplotlib.pyplot as plt
    from sillage.viz.map2d import draw_indicator

    dem = _synthetic_dem(n=40, res_m=50.0)
    haz = np.random.default_rng(0).random(dem.shape)
    fig = plt.figure()
    ax = fig.add_subplot(111)
    im = draw_indicator(ax, dem, haz)  # same rendering the IHM canvas uses
    assert im is not None
    plt.close(fig)


def test_volume3d_public_api():
    from sillage.viz import volume3d  # refactor keeps both entry points

    assert hasattr(volume3d, "populate_plotter") and hasattr(volume3d, "build_scene")


def test_gui_module_imports():
    pytest.importorskip("PySide6")  # only when the gui extra is installed
    from sillage.app.main_window import MainWindow  # noqa: F401


def test_sample_grid_and_upstream_wind(tmp_path):
    import rasterio

    from sillage.screening.pass1 import (
        find_direction_grid,
        sample_grid_at,
        upstream_crest_wind,
    )

    transform = from_origin(600000.0, 4901000.0, 100.0, 100.0)  # 10x10 @ 100 m, north-up

    def _write(name, value):
        p = tmp_path / name
        prof = dict(driver="GTiff", height=10, width=10, count=1, dtype="float32",
                    crs=CRS.from_epsg(32631), transform=transform, nodata=-9999.0)
        with rasterio.open(p, "w", **prof) as dst:
            dst.write(np.full((10, 10), value, dtype="float32"), 1)
        return p

    vel = _write("run_vel.asc", 7.5)
    ang = _write("run_ang.asc", 315.0)
    cx, cy = 600500.0, 4900500.0
    assert sample_grid_at(vel, cx, cy) == 7.5
    assert sample_grid_at(vel, 0.0, 0.0) is None  # outside the grid
    assert find_direction_grid(tmp_path) == ang

    bc = upstream_crest_wind(vel, ang, cx, cy, from_deg=270.0, fetch_m=100.0)
    assert bc is not None
    spd, drc = bc
    assert spd == 7.5 and abs(drc - 315.0) < 1e-6


def test_zoom_for_resolution_caps():
    from sillage.terrain.acquire import zoom_for_resolution

    z_fine = zoom_for_resolution(44.5, 30.0, merc_width_m=20_000, max_px=2500)
    z_coarse = zoom_for_resolution(44.5, 200.0, merc_width_m=20_000, max_px=2500)
    assert z_fine > z_coarse  # finer target -> higher zoom
    z_capped = zoom_for_resolution(44.5, 30.0, merc_width_m=5_000_000, max_px=2500)
    assert z_capped < z_fine  # a giant AOI is capped down to a coarser DEM


def test_prepare_dem_for_bbox_decodes_and_reprojects(tmp_path, monkeypatch):
    import contextily as cx

    from sillage.terrain import acquire
    from sillage.terrain.dem import load_dem

    # terrarium encoding of 1000 m: v = 1000 + 32768 = 33768 -> R=131, G=232, B=0
    img = np.zeros((16, 16, 4), dtype="uint8")
    img[:, :, 0], img[:, :, 1], img[:, :, 2], img[:, :, 3] = 131, 232, 0, 255
    ext = (665000.0, 724000.0, 5518000.0, 5577000.0)  # web-mercator (Champsaur-ish)
    monkeypatch.setattr(cx, "bounds2img", lambda *a, **k: (img, ext))

    out = tmp_path / "aoi.tif"
    p = acquire.prepare_dem_for_bbox((44.45, 6.0, 44.70, 6.45), out, target_res_m=90.0)
    assert p.exists()
    dem = load_dem(str(p), max_domain_km=200.0)
    assert dem.crs.is_projected
    assert 950 < float(np.nanmean(dem.elevation)) < 1050  # ~1000 m preserved


def test_window_forecast_provider(monkeypatch):
    from sillage.wind import forecast, profile
    from sillage.wind.forecast import HourlyProfile, WindSample

    def fake(lat, lon, hours=24, **kw):
        return [
            HourlyProfile(time_iso=f"t{i}", samples=[
                WindSample(f"t{i}", 700.0, 2500.0, 5.0 + i, (300 + i) % 360)])
            for i in range(hours)
        ]

    monkeypatch.setattr(forecast, "fetch_open_meteo", fake)
    dem = _synthetic_dem(40, res_m=50.0)
    make = profile.window_forecast_provider(dem, 2500.0, n_hours=6, source="open_meteo")
    spd, drc = make(2)(600500.0, 4900000.0)  # hour 2 -> 5+2 m/s, (300+2)°
    assert abs(spd - 7.0) < 1e-6 and abs(drc - 302.0) < 1e-6


def test_subzone_bboxes_tiling():
    from sillage.screening.subzones import subzone_bboxes

    dem = _synthetic_dem(n=100, res_m=50.0)  # 5 km square
    left, bottom, right, top = dem.bounds
    tiles = subzone_bboxes(dem, nx=2, ny=2, overlap_frac=0.2)
    assert len(tiles) == 4
    for (lft, bot, rgt, topp), (cx, cy) in tiles:
        assert left - 1 <= lft and rgt <= right + 1  # clamped to domain
        assert bottom - 1 <= bot and topp <= top + 1
        assert lft < cx < rgt and bot < cy < topp  # centre inside its bbox


def test_assemble_mosaic_full_coverage_and_blend():
    from sillage.screening.subzones import assemble_mosaic, subzone_bboxes

    dem = _synthetic_dem(n=100, res_m=50.0)
    tiles = subzone_bboxes(dem, 2, 2, overlap_frac=0.2)
    contribs = [(bbox, np.full((20, 20), float(i))) for i, (bbox, _c) in enumerate(tiles)]
    mosaic = assemble_mosaic(dem, contribs)
    assert mosaic.shape == dem.shape
    assert np.isfinite(mosaic).all()  # 2x2 tiles cover the whole domain
    assert -0.01 <= mosaic.min() and mosaic.max() <= 3.01  # bounded by tile values


def test_crest_wind_provider(monkeypatch):
    from sillage.wind import forecast, profile
    from sillage.wind.forecast import HourlyProfile, WindSample

    def fake_fetch(lat, lon, hours=24, **kw):
        s = WindSample(time_iso="t0", pressure_hpa=700.0, altitude_m=2500.0,
                       speed_ms=10.0, from_deg=315.0)
        return [HourlyProfile(time_iso="t0", samples=[s])]

    monkeypatch.setattr(forecast, "fetch_open_meteo", fake_fetch)
    dem = _synthetic_dem(n=40, res_m=50.0)  # EPSG:32631, valid UTM
    provider = profile.crest_wind_provider(dem, crest_alt_m=2500.0, hour_index=0)
    spd, drc = provider(600500.0, 4900000.0)
    assert spd == 10.0 and abs(drc - 315.0) < 1e-6


def test_basemap_sources_and_unknown_raises():
    import matplotlib.pyplot as plt

    from sillage.viz.map2d import BASEMAP_SOURCES, add_basemap

    assert "IGN plan" in BASEMAP_SOURCES and "OpenStreetMap" in BASEMAP_SOURCES
    fig, ax = plt.subplots()
    with pytest.raises(ValueError):  # validated before any network/contextily import
        add_basemap(ax, "EPSG:32632", source="does-not-exist")
    plt.close(fig)


def test_synthetic_series():
    from sillage.screening.pass1 import synthetic_series

    s = synthetic_series(4)
    assert len(s) == 4
    assert len({label for label, _s, _d in s}) == 4  # distinct hour labels
    assert [spd for _l, spd, _d in s] == sorted(spd for _l, spd, _d in s)  # speed ramps


def test_pass2_wind_falls_back_to_controls():
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from sillage.app.main_window import MainWindow

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = MainWindow()
    # No manual wind fields: the Pass-2 fallback wind comes from the flight window's first
    # hour (synthetic_series hour 0 = 6 m/s from 300°).
    assert w._pass2_wind_at(270000.0, 4958000.0) == (6.0, 300.0, "créneau")
    w.deleteLater()


def test_pass2_mesh_presets_and_estimate():
    pytest.importorskip("PySide6")
    from sillage.app.main_window import (
        PASS2_MESH_DEFAULT,
        PASS2_MESH_PRESETS,
        _estimate_minutes,
    )

    assert PASS2_MESH_DEFAULT in PASS2_MESH_PRESETS
    for mesh_count, iters in PASS2_MESH_PRESETS.values():
        assert mesh_count > 0 and iters > 0
    assert _estimate_minutes(20_000) >= 1
    assert _estimate_minutes(400_000) > _estimate_minutes(20_000)
