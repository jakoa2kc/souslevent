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


def test_locate_openfoam_case_dem_stem_disambiguates_parallel(tmp_path):
    # Parallel auto solves drop several NINJAFOAM_* in the same root; the dem stem must select
    # this task's case even if a sibling's case was written more recently (higher mtime).
    root = tmp_path / "auto"
    mine = root / "NINJAFOAM_z01_h09_111_0"
    other = root / "NINJAFOAM_z02_h09_222_0"
    for case in (mine, other):
        (case / "system").mkdir(parents=True)
        (case / "constant").mkdir()
    import os

    os.utime(other, (other.stat().st_atime, mine.stat().st_mtime + 100))  # other is "newer"
    found = locate_openfoam_case(root, "", extra_roots=[root], dem_stem="z01_h09")
    assert found.resolve() == mine.resolve()  # picked by stem, not by newest mtime


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


def test_terrain_mesh_uses_pixel_centres():
    pytest.importorskip("pyvista")
    from sillage.viz import volume3d as v3

    dem = Dem(elevation=np.zeros((3, 4), dtype="float32"),
              transform=from_origin(100.0, 200.0, 10.0, 10.0),
              crs=CRS.from_epsg(32631), resolution_m=10.0)
    terrain = v3._terrain_mesh(dem)

    assert terrain.bounds[0] == pytest.approx(105.0)
    assert terrain.bounds[1] == pytest.approx(135.0)
    assert terrain.bounds[2] == pytest.approx(175.0)
    assert terrain.bounds[3] == pytest.approx(195.0)


def test_drape_basemap_reprojects_tiles_to_terrain_crs(monkeypatch):
    pytest.importorskip("pyvista")
    from rasterio.warp import transform_bounds

    import rasterio.warp as rwarp

    from sillage.viz import volume3d as v3
    from sillage.viz.map2d import import_contextily

    cx = import_contextily()

    dem = Dem(elevation=np.zeros((8, 8), dtype="float32"),
              transform=from_origin(600000.0, 4900000.0, 50.0, 50.0),
              crs=CRS.from_epsg(32631), resolution_m=50.0)
    terrain = v3._terrain_mesh(dem)
    xmin, xmax, ymin, ymax = terrain.bounds[:4]
    webm = transform_bounds(dem.crs, CRS.from_epsg(3857), xmin, ymin, xmax, ymax)
    ext3857 = (webm[0], webm[2], webm[1], webm[3])
    img = np.zeros((6, 6, 4), dtype="uint8")
    img[:, :, 0], img[:, :, 3] = 255, 255

    def fake_bounds2img(*args, **kwargs):
        return img, ext3857

    calls = []
    real_reproject = rwarp.reproject

    def spy_reproject(*args, **kwargs):
        calls.append(kwargs.get("dst_crs"))
        return real_reproject(*args, **kwargs)

    monkeypatch.setattr(cx, "bounds2img", fake_bounds2img)
    monkeypatch.setattr(rwarp, "reproject", spy_reproject)

    class _FakePlotter:
        def __init__(self):
            self.meshes = 0

        def add_mesh(self, *a, **k):
            self.meshes += 1

    p = _FakePlotter()
    assert v3._drape_basemap(p, terrain, dem.crs, "OpenStreetMap")
    assert p.meshes == 1
    assert len(calls) == 3
    assert all(crs.to_epsg() == 32631 for crs in calls)


def test_add_horizontal_scale_bar_draws_line_and_label():
    pv = pytest.importorskip("pyvista")
    from sillage.viz import volume3d as v3

    xx, yy = np.meshgrid(np.linspace(0.0, 2000.0, 5), np.linspace(0.0, 1000.0, 5))
    terrain = pv.StructuredGrid(xx, yy, np.zeros_like(xx))

    class _FakePlotter:
        def __init__(self):
            self.meshes = 0
            self.labels = []

        def add_mesh(self, *a, **k):
            self.meshes += 1

        def add_point_labels(self, pts, labels, **k):
            self.labels.extend(labels)

    p = _FakePlotter()
    v3._add_horizontal_scale_bar(p, terrain)
    assert p.meshes >= 3  # bar + two ticks
    assert p.labels and ("m" in p.labels[0] or "km" in p.labels[0])


def test_lee_source_can_be_rethresholded_without_openfoam_case():
    pv = pytest.importorskip("pyvista")
    from sillage.viz import volume3d as v3

    grid = pv.ImageData(dimensions=(11, 11, 6), spacing=(1.0, 1.0, 1.0))
    centers = grid.cell_centers().points
    u = np.zeros((grid.n_cells, 3), dtype="float32")
    u[:, 0] = np.where(centers[:, 0] < 5.0, -2.0, 1.0)  # reversed on the west half
    u[:, 2] = np.select([centers[:, 1] < 3.0, centers[:, 1] > 5.0], [-1.5, 1.2], default=0.1)
    grid.cell_data["U"] = u
    grid.cell_data["k"] = np.full(grid.n_cells, 1.5, dtype="float32")

    source = v3.extract_lee_source(
        grid, np.array([1.0, 0.0, 0.0]), ref_speed_ms=2.0, aoi_bounds=(1.0, 9.0, 1.0, 9.0))
    assert source is not None and source.n_cells
    assert "U" not in source.cell_data
    assert {"along_flow", "along_pct", "w_ms", "w_abs", "turb_rms"}.issubset(source.cell_data)

    rotor = v3.threshold_lee_source(source, metric="rotor")
    vertical = v3.threshold_lee_source(source, metric="vertical", vol_floor=1.0)
    horizontal_range = v3.threshold_lee_source(
        source, metric="horizontal", metric_range={"min": -80.0, "max": 40.0})
    rotor_range = v3.threshold_lee_source(
        source, metric="rotor", metric_range={"min": 1.5, "max": 3.0})
    vertical_range = v3.threshold_lee_source(
        source, metric="vertical",
        metric_range={"sink_min": -2.0, "sink_max": -1.0, "lift_min": 1.0, "lift_max": 2.0})
    turbulence_range = v3.threshold_lee_source(
        source, metric="turbulence", metric_range={"min": 1.0, "max": 2.0})
    assert rotor is not None and rotor.n_cells
    assert vertical is not None and vertical.n_cells
    assert horizontal_range is not None and horizontal_range.n_cells
    assert rotor_range is not None and rotor_range.n_cells
    assert vertical_range is not None and vertical_range.n_cells
    assert turbulence_range is not None and turbulence_range.n_cells
    assert np.all(np.asarray(rotor.cell_data["along_flow"]) < 0.0)
    assert np.all(np.asarray(vertical.cell_data["w_abs"]) >= 1.0)
    assert np.all(np.asarray(horizontal_range.cell_data["along_pct"]) <= 40.0)
    assert np.all(-np.asarray(rotor_range.cell_data["along_flow"]) >= 1.5)
    vw = np.asarray(vertical_range.cell_data["w_ms"])
    assert np.all((vw <= -1.0) | (vw >= 1.0))
    assert np.all(np.asarray(turbulence_range.cell_data["turb_rms"]) >= 1.0)


def test_gui_module_imports():
    pytest.importorskip("PySide6")  # only when the gui extra is installed
    from sillage.app.main_window import MainWindow  # noqa: F401
    from sillage.souslevent.window import SousLeVentWindow  # noqa: F401


def test_souslevent_window_builds_offscreen():
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from sillage.souslevent.window import SousLeVentWindow

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = SousLeVentWindow()
    assert w.windowTitle() == "SousLeVent"
    assert w.selection_combo.count() == 2
    assert w.calc_combo.count() == 2  # Pass-1→sélection, Pass-2 partout (features-auto dropped)
    assert w.render_basemap_combo.currentText() == "IGN plan"
    assert w.candidate_basemap_combo.currentText() == "IGN plan"
    assert w.btn_apply_scale.text() == "Recalculer la vue 3D"
    assert "#2d7d2d" in w.btn_apply_scale.styleSheet()
    settings = w._render_settings_layout

    def settings_index(item):
        for idx in range(settings.count()):
            layout_item = settings.itemAt(idx)
            if layout_item.widget() is item or layout_item.layout() is item:
                return idx
        return -1

    assert settings_index(w._basemap_form_layout) == 0
    assert settings_index(w._metric_choice_layout) < settings_index(w.legend_label)
    assert settings_index(w.legend_label) < settings_index(w._metric_slider_layout)
    assert settings_index(w.btn_apply_scale) < settings_index(w.wind_legend_label)
    assert not w.wind_forecast_widget.isHidden()
    assert w.wind_manual_widget.isHidden()
    assert not w.features_row_widget.isHidden()
    assert w.step_row_widget.isHidden()
    assert w.btn_validate.minimumWidth() >= 360
    assert w.tabs.count() == 3
    w.deleteLater()


def test_souslevent_manual_candidate_rectangle_offscreen():
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from sillage.auto.pipeline import AutoConfig, ScreeningResult
    from sillage.souslevent.window import SousLeVentWindow

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = SousLeVentWindow()
    dem = Dem(
        elevation=np.arange(400, dtype="float32").reshape(20, 20),
        transform=from_origin(600000.0, 4900000.0, 50.0, 50.0),
        crs=CRS.from_epsg(32631),
        resolution_m=50.0,
    )
    w._candidate_dem = dem
    w._screening_cfg = AutoConfig(
        bbox_latlon=(44.0, 6.0, 44.1, 6.1), hours=(9,), target_res_m=50.0)
    w._screening_result = ScreeningResult(
        dem_path="", crs=dem.crs, partition=[], hours=[9], hazard=np.ones(dem.shape))
    w._candidate_auto_count = 0
    w.candidate_basemap_combo.setCurrentText("Aucun")

    w._finish_manual_candidate_rect(600100.0, 4899300.0, 600700.0, 4899800.0)

    assert len(w._screening_result.partition) == 1
    assert w.candidate_list.count() == 1
    assert w.candidate_list.item(0).isSelected()
    assert w._screening_result.partition[0].est_cells > 0
    w.deleteLater()


def test_souslevent_pass1_candidate_map_uses_basemap(monkeypatch):
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from sillage.auto.pipeline import AutoConfig, ScreeningResult
    from sillage.souslevent import window as slv_window
    from sillage.souslevent.window import SousLeVentWindow

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = SousLeVentWindow()
    dem = Dem(
        elevation=np.arange(400, dtype="float32").reshape(20, 20),
        transform=from_origin(600000.0, 4900000.0, 50.0, 50.0),
        crs=CRS.from_epsg(32631),
        resolution_m=50.0,
    )
    w._candidate_dem = dem
    w._screening_cfg = AutoConfig(
        bbox_latlon=(44.0, 6.0, 44.1, 6.1), hours=(9,), target_res_m=50.0)
    w._screening_result = ScreeningResult(
        dem_path="", crs=dem.crs, partition=[], hours=[9], hazard=np.ones(dem.shape))

    calls = []

    def fake_add_basemap(ax, crs, source, **kwargs):
        calls.append((crs, source, kwargs))

    monkeypatch.setattr(slv_window.map2d, "add_basemap", fake_add_basemap)
    w._draw_candidate_map()

    assert calls and calls[0][1] == "IGN plan"
    w.deleteLater()


def test_souslevent_manual_wind_grid_config_offscreen():
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from sillage.auto.pipeline import WIND_MODE_MANUAL_GRID
    from sillage.souslevent.window import SousLeVentWindow

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = SousLeVentWindow()
    w._selection_mode = "rectangle"
    w._on_rectangle(44.0, 6.0, 44.1, 6.1)
    w.wind_mode_combo.setCurrentIndex(1)
    w.speed_slider.setValue((10, 20))
    w.dir_slider.setValue((270, 315))

    cfg = w._build_cfg(domain_mode="corridor")

    assert w.wind_forecast_widget.isHidden()
    assert not w.wind_manual_widget.isHidden()
    assert cfg.wind_mode == WIND_MODE_MANUAL_GRID
    assert cfg.wind_source == "manual"
    assert cfg.hours == tuple(range(6))
    assert cfg.manual_wind_speeds_kmh == (10, 15, 20)
    assert cfg.manual_wind_dirs_deg == (270, 315)
    assert w.dir_label.text() == "Ouest → Nord-Ouest"
    assert w._labels_for_cfg(cfg, cfg.hours)[0] == "10 km/h · Ouest"
    assert w._labels_for_cfg(cfg, cfg.hours)[5] == "20 km/h · Nord-Ouest"
    w.deleteLater()


def test_souslevent_mesh_preset_feeds_cfg():
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from sillage.souslevent.window import SousLeVentWindow

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = SousLeVentWindow()
    w._selection_mode = "rectangle"
    w._on_rectangle(44.0, 6.0, 44.1, 6.1)
    w.mesh_combo.setCurrentText("Fin — lent")          # (150_000, 300)
    cfg = w._build_cfg(domain_mode="corridor")
    assert cfg.mesh_count == 150_000 and cfg.iterations == 300  # Pass-2 mesh knob (ADR-0008/0035)
    w.deleteLater()


def test_souslevent_candidate_hour_slider_browses_hazard_stack(monkeypatch):
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from sillage.auto.pipeline import AutoConfig, ScreeningResult, WIND_MODE_MANUAL_GRID
    from sillage.souslevent import window as slv_window
    from sillage.souslevent.window import SousLeVentWindow

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = SousLeVentWindow()
    dem = Dem(elevation=np.arange(400, dtype="float32").reshape(20, 20),
              transform=from_origin(600000.0, 4900000.0, 50.0, 50.0),
              crs=CRS.from_epsg(32631), resolution_m=50.0)
    monkeypatch.setattr(slv_window, "load_dem", lambda *a, **k: dem)
    w._screening_cfg = AutoConfig(
        bbox_latlon=(44.0, 6.0, 44.1, 6.1), hours=(0, 1, 2), wind_mode=WIND_MODE_MANUAL_GRID,
        manual_wind_speeds_kmh=(10,), manual_wind_dirs_deg=(270, 315, 0))
    stack = [np.full(dem.shape, v) for v in (0.1, 0.5, 0.9)]
    result = ScreeningResult(dem_path="x.tif", crs=dem.crs, partition=[], hours=[0, 1, 2],
                             hazard=np.maximum.reduce(stack), hazard_stack=stack)
    w._screening_result = result  # _on_finished normally sets this before _populate_candidates
    drawn = []
    monkeypatch.setattr(w, "_draw_candidate_map", lambda: drawn.append(w._candidate_hour))
    w._populate_candidates(result)
    assert not w.candidate_hour_row.isHidden()         # 3-map stack → slider shown
    assert w.candidate_hour_slider.maximum() == 2
    w.candidate_hour_slider.setValue(2)                # browse to the last scenario
    assert w._candidate_hour == 2 and drawn[-1] == 2
    assert w.candidate_hour_label.text() == "10 km/h · Nord"  # hours[2] → dir 0° = Nord
    w.deleteLater()


def test_souslevent_wind_arrow_sliders_update_state():
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from sillage.souslevent.window import SousLeVentWindow

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = SousLeVentWindow()
    w.wind_size_slider.setValue(250)      # 2.5× reference size
    w.wind_alt_slider.setValue(120)       # 120 m above ground
    assert w._wind_size_factor == 2.5
    assert w._wind_altitude_m == 120.0
    assert "250 %" in w.wind_style_label.text() and "120 m" in w.wind_style_label.text()
    w.deleteLater()


def test_set_wind_arrow_style_records_without_arrows():
    # No arrows on the plotter yet: the call must still record size/altitude for the next build.
    from sillage.viz.volume3d import set_wind_arrow_style

    class _P:
        pass

    p = _P()
    set_wind_arrow_style(p, size_factor=2.0, altitude_m=50.0)
    assert p._wind_size_factor == 2.0 and p._wind_altitude_m == 50.0


def test_souslevent_manual_wind_result_uses_two_render_sliders():
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from sillage.auto.pipeline import AutoConfig, AutoResult, CaseResult, WIND_MODE_MANUAL_GRID
    from sillage.souslevent.window import SousLeVentWindow

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = SousLeVentWindow()
    cfg = AutoConfig(
        bbox_latlon=(44.0, 6.0, 44.1, 6.1), hours=tuple(range(6)),
        wind_mode=WIND_MODE_MANUAL_GRID,
        manual_wind_speeds_kmh=(10, 15, 20), manual_wind_dirs_deg=(270, 315))
    cases = [
        CaseResult(
            zone_index=0, hour=h, case_dir="", wind_speed_ms=10.0, wind_from_deg=270.0,
            crs=CRS.from_epsg(32631), aoi_bounds=(0.0, 1.0, 0.0, 1.0), elapsed_s=1.0)
        for h in cfg.hours
    ]
    w._last_cfg = cfg
    w._result = AutoResult(dem_path="", crs=CRS.from_epsg(32631), partition=[], cases=cases)
    rendered = []
    busy_states = []

    def fake_render(idx):
        busy_states.append((
            w.btn_apply_scale.isEnabled(),
            w.btn_apply_scale.text(),
            w.statusBar().currentMessage(),
        ))
        rendered.append(idx)

    w._render_hour = fake_render

    w._show_result_hours(w._result.hours)
    assert rendered == [0]
    w.render_speed_slider.setValue(2)
    w.render_dir_slider.setValue(1)

    assert w.hour_slider.isHidden()
    assert not w.manual_render_widget.isHidden()
    assert w.render_speed_label.text() == "20 km/h"
    assert w.render_dir_label.text() == "Nord-Ouest"
    assert w.hour_label.text() == "20 km/h · Nord-Ouest"
    assert rendered == [0]
    w._on_apply_scale()
    assert busy_states[-1] == (False, "Calcul en cours...", "Calcul en cours...")
    assert w.btn_apply_scale.isEnabled()
    assert w.btn_apply_scale.text() == "Recalculer la vue 3D"
    assert w.statusBar().currentMessage() == "Vue 3D recalculée"
    assert rendered[-1] == 5
    w.deleteLater()


def test_souslevent_forecast_hour_slider_waits_for_apply():
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from sillage.auto.pipeline import AutoConfig, AutoResult, CaseResult
    from sillage.souslevent.window import SousLeVentWindow

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = SousLeVentWindow()
    cfg = AutoConfig(bbox_latlon=(44.0, 6.0, 44.1, 6.1), hours=(0, 1, 2))
    cases = [
        CaseResult(
            zone_index=0, hour=h, case_dir="", wind_speed_ms=10.0, wind_from_deg=270.0,
            crs=CRS.from_epsg(32631), aoi_bounds=(0.0, 1.0, 0.0, 1.0), elapsed_s=1.0)
        for h in cfg.hours
    ]
    w._last_cfg = cfg
    w._result = AutoResult(dem_path="", crs=CRS.from_epsg(32631), partition=[], cases=cases)
    rendered = []
    w._render_hour = lambda idx: rendered.append(idx)

    w._show_result_hours(w._result.hours)
    assert rendered == [0]
    w.hour_slider.setValue(2)

    assert not w.hour_slider.isHidden()
    assert w.manual_render_widget.isHidden()
    assert w.hour_label.text()
    w.metric_combo.setCurrentIndex(1)
    w.render_basemap_combo.setCurrentIndex(0)
    assert rendered == [0]
    w._on_apply_scale()
    assert rendered == [0, 2]
    w.deleteLater()


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


def test_in_france():
    from sillage.terrain.acquire import in_france

    assert in_france((44.4, 6.0, 44.7, 6.4))            # French Alps
    assert not in_france((40.0, -74.0, 40.1, -73.9))    # New York


def test_prepare_dem_ign_decodes(tmp_path, monkeypatch):
    import requests

    from sillage.terrain import acquire
    from sillage.terrain.dem import load_dem

    class _Resp:
        def __init__(self, content):
            self.content = content
            self.headers = {"Content-Type": "image/x-bil;bits=32"}

        def raise_for_status(self):
            pass

    def fake_get(url, params=None, timeout=None):
        w, h = int(params["WIDTH"]), int(params["HEIGHT"])
        return _Resp(np.full((h, w), 1200.0, dtype="<f4").tobytes())

    monkeypatch.setattr(requests, "get", fake_get)
    p = acquire.prepare_dem_ign((44.55, 6.15, 44.70, 6.35), tmp_path / "ign.tif", target_res_m=60.0)
    dem = load_dem(str(p), max_domain_km=200.0)
    assert dem.crs.is_projected
    assert 1150 < float(np.nanmean(dem.elevation)) < 1250  # ~1200 m preserved


def test_prepare_dem_ign_one_meter_keeps_native_fetch(tmp_path, monkeypatch):
    import requests

    from sillage.terrain import acquire
    from sillage.terrain.dem import load_dem

    class _Resp:
        def __init__(self, content):
            self.content = content
            self.headers = {"Content-Type": "image/x-bil;bits=32"}

        def raise_for_status(self):
            pass

    def fake_get(url, params=None, timeout=None):
        w, h = int(params["WIDTH"]), int(params["HEIGHT"])
        return _Resp(np.full((h, w), 1234.0, dtype="<f4").tobytes())

    def fail_average(arr, factor):
        raise AssertionError(f"1 m target should not average native IGN fetch (factor={factor})")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(acquire, "_block_average", fail_average)
    p = acquire.prepare_dem_ign((44.550, 6.150, 44.551, 6.151), tmp_path / "ign_1m.tif",
                                target_res_m=1.0)
    dem = load_dem(str(p), max_domain_km=200.0)
    assert dem.crs.is_projected
    assert dem.resolution_m <= 2.0


def test_zoom_for_resolution_caps():
    from sillage.terrain.acquire import zoom_for_resolution

    z_fine = zoom_for_resolution(44.5, 30.0, merc_width_m=20_000, max_px=2500)
    z_coarse = zoom_for_resolution(44.5, 200.0, merc_width_m=20_000, max_px=2500)
    assert z_fine > z_coarse  # finer target -> higher zoom
    z_capped = zoom_for_resolution(44.5, 30.0, merc_width_m=5_000_000, max_px=2500)
    assert z_capped < z_fine  # a giant AOI is capped down to a coarser DEM


def test_prepare_dem_for_bbox_decodes_and_reprojects(tmp_path, monkeypatch):
    from sillage.terrain import acquire
    from sillage.terrain.dem import load_dem
    from sillage.viz.map2d import import_contextily

    cx = import_contextily()

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


def test_bbox_latlon_from_utm_window():
    from rasterio.warp import transform

    from sillage.terrain.acquire import bbox_latlon_from_utm_window

    crs = CRS.from_epsg(32631)
    x, y, half = 500000.0, 4958344.0, 2500.0  # central meridian, ~44.7°N; 5 km window
    s, w, n, e = bbox_latlon_from_utm_window(crs, x, y, half)
    assert s < n and w < e
    # the bbox centre round-trips back to (x, y)
    bx, by = transform(CRS.from_epsg(4326), crs, [(w + e) / 2], [(s + n) / 2])
    assert abs(bx[0] - x) < 50 and abs(by[0] - y) < 50
    assert 0.03 < (n - s) < 0.06  # ~5 km of latitude is ~0.045°


def test_mean_flow_vector_blows_to():
    v = mean_flow_vector(270.0)            # FROM west -> blows toward east (+X)
    assert v[0] > 0.99 and abs(v[1]) < 1e-6 and v[2] == 0.0
    n = mean_flow_vector(180.0)            # FROM south -> blows toward north (+Y)
    assert n[1] > 0.99 and abs(n[0]) < 1e-6


def test_clip_domain_boundary_drops_lateral_frame_and_lid():
    pv = pytest.importorskip("pyvista")
    from sillage.viz.volume3d import _clip_domain_boundary

    grid = pv.ImageData(dimensions=(21, 21, 21))  # 20×20×20 cells over 0..20
    grid.cell_data["v"] = np.ones(grid.n_cells)
    vol = grid.threshold(0.5, scalars="v")
    clipped = _clip_domain_boundary(vol, grid, lateral_frac=0.1, lid_frac=0.1)
    c = clipped.cell_centers().points
    assert clipped.n_cells < vol.n_cells
    assert c[:, 0].min() >= 2 and c[:, 0].max() <= 18  # E/W frame removed (0.1×20 = 2)
    assert c[:, 1].min() >= 2 and c[:, 1].max() <= 18  # N/S frame removed
    assert c[:, 2].max() <= 18                          # lid removed

    # explicit AOI bounds clip the rotor back to the drawn zone (here 5..15 in x/y)
    aoi = _clip_domain_boundary(vol, grid, aoi_bounds=(5, 15, 5, 15), lid_frac=0.0)
    ca = aoi.cell_centers().points
    assert ca[:, 0].min() >= 5 and ca[:, 0].max() <= 15
    assert ca[:, 1].min() >= 5 and ca[:, 1].max() <= 15

    # Manual view keeps the original if a too-tight clip would blank it; auto compaction can ask
    # for the truthful empty mesh so boundary-only artifacts are not persisted.
    kept = _clip_domain_boundary(vol, grid, aoi_bounds=(100, 110, 100, 110), lid_frac=0.0)
    empty = _clip_domain_boundary(
        vol, grid, aoi_bounds=(100, 110, 100, 110), lid_frac=0.0, keep_if_empty=False)
    assert kept.n_cells == vol.n_cells
    assert empty.n_cells == 0


def test_populate_pass1_3d_builds_scene():
    pytest.importorskip("pyvista")
    from sillage.viz import volume3d as v3

    ridge = np.exp(-(np.linspace(-1, 1, 40) ** 2) / 0.05) * 400.0
    dem = Dem(elevation=np.tile(ridge, (40, 1)).astype("float32"),
              transform=from_origin(600000.0, 4900000.0, 50.0, 50.0),
              crs=CRS.from_epsg(32631), resolution_m=50.0)
    hazard = np.zeros((40, 40))
    hazard[:20, :] = 0.8  # north half flagged

    class _FakePlotter:
        def __init__(self):
            self.meshes = self.texts = self.labels = 0

        def add_mesh(self, *a, **k):
            self.meshes += 1

        def add_text(self, *a, **k):
            self.texts += 1

        def add_point_labels(self, *a, **k):
            self.labels += 1

        def show_axes(self):
            pass

        def view_isometric(self):
            pass

    p = _FakePlotter()
    # crs=None -> no basemap fetch (offline): falls back to the elevation colormap
    v3.populate_pass1_3d(p, dem, hazard, winds=[(600000.0 + 1000, 4900000.0 - 1000, 10.0, 270.0)],
                         crs=None)
    assert p.meshes >= 3  # terrain + hazard overlay + wind arrow + north arrow
    assert p.texts >= 1 and p.labels >= 1


def test_add_compass_adds_north_and_wind_arrows():
    pv = pytest.importorskip("pyvista")
    from sillage.viz import volume3d as v3

    xx, yy = np.meshgrid(np.linspace(0, 2000, 12), np.linspace(0, 1500, 12))
    terrain = pv.StructuredGrid(xx, yy, 50.0 * np.sin(xx / 400.0))

    class _FakePlotter:
        def __init__(self):
            self.meshes = 0
            self.labels = None

        def add_mesh(self, *a, **k):
            self.meshes += 1

        def add_point_labels(self, pts, labels, **k):
            self.labels = list(labels)

    p = _FakePlotter()
    v3._add_compass(p, terrain, mean_flow_vector(270.0), wind_speed_ms=12.0, wind_from_deg=270.0)
    assert p.meshes == 2  # north + wind arrows
    assert p.labels[0] == "N"
    assert "43 km/h" in p.labels[1] and "Ouest" in p.labels[1]  # 12 m/s -> 43 km/h (display only)
