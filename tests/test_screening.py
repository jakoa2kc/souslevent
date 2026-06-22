"""Tests for the screening indicator and the WindNinja wrapper command builder.

The WindNinja wrapper is tested via dry_run (no binary needed): we assert the built
command carries the right flags. The indicator is tested on a synthetic ridge.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("scipy")
from rasterio.transform import from_origin  # noqa: E402
from rasterio.crs import CRS  # noqa: E402

from sillage.terrain.dem import Dem  # noqa: E402
from sillage.screening import indicator as ind  # noqa: E402
from sillage.flow.windninja import run_mass, run_momentum, FLAG  # noqa: E402


def _synthetic_ridge(n: int = 64, res_m: float = 30.0) -> Dem:
    x = np.linspace(-1, 1, n)
    ridge = np.exp(-(x**2) / 0.05) * 500.0
    z = np.tile(ridge, (n, 1))
    transform = from_origin(600000.0, 4900000.0, res_m, res_m)
    return Dem(elevation=z.astype("float32"), transform=transform,
               crs=CRS.from_epsg(32631), resolution_m=res_m)


def test_indicator_in_unit_range_and_nonzero():
    dem = _synthetic_ridge()
    h = ind.hazard_indicator(dem, wind_from_deg=270.0, speed_grid=None)
    assert h.shape == dem.shape
    assert np.all((h >= 0) & (h <= 1))
    assert h.max() > 0  # geometry alone should flag the lee of the ridge


def test_velocity_deficit_monotonic():
    speed = np.array([[10.0, 5.0, 1.0]])
    d = ind.velocity_deficit(speed, reference=10.0)
    assert d[0, 0] < d[0, 1] < d[0, 2]  # slower wind => larger deficit


def test_find_candidates_returns_sorted_separated():
    dem = _synthetic_ridge()
    h = ind.hazard_indicator(dem, wind_from_deg=270.0)
    cands = ind.find_candidates(dem, h, n=5, min_separation_m=120.0)
    assert len(cands) >= 1
    scores = [c.score for c in cands]
    assert scores == sorted(scores, reverse=True)  # descending


def test_run_mass_dry_run_builds_mass_command():
    run = run_mass(cli="WindNinja_cli", dem_path="/tmp/dem.tif",
                   working_dir="/tmp/wnd_mass", wind_speed_ms=12.0, wind_from_deg=270.0,
                   dry_run=True)
    cmd = " ".join(run.command)
    assert f"--{FLAG['momentum']}=false" in cmd
    assert "domainAverageInitialization" in cmd
    assert f"--{FLAG['ascii_uv']}=true" in cmd
    assert "--output_speed_units=mps" in cmd
    assert "--vegetation=grass" in cmd
    assert "--mesh_resolution=50.0" in cmd
    assert run.returncode is None  # not executed


def test_run_momentum_dry_run_sets_momentum_and_turbulence():
    run = run_momentum(cli="WindNinja_cli", dem_path="/tmp/crop.tif",
                       working_dir="/tmp/wnd_mom", wind_speed_ms=12.0, wind_from_deg=270.0,
                       mesh_count=250_000, iterations=200, dry_run=True)
    cmd = " ".join(run.command)
    assert f"--{FLAG['momentum']}=true" in cmd
    assert f"--{FLAG['turbulence_out']}=true" in cmd
    assert "--output_speed_units=mps" in cmd
    assert f"--{FLAG['mesh_count']}=250000" in cmd
    assert "--vegetation=grass" in cmd
    # momentum solver must use domain-average init (no weather-model/point init)
    assert "domainAverageInitialization" in cmd



