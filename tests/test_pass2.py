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
