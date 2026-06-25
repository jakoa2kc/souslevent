"""Tests for the parallel sub-zone speed field (ADR-0007) and the run_mass thread flag.

WindNinja is mocked: we patch ``run_mass`` + the grid readers so the test runs without the
binary, and assert the tiles are all solved, the mosaic has the DEM shape, the per-run thread
cap is passed, and cancel propagates.
"""

from __future__ import annotations

import threading

import numpy as np
import pytest

pytest.importorskip("scipy")
from rasterio.crs import CRS  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402

from sillage.flow.windninja import FLAG, WindNinjaRun, run_mass  # noqa: E402
from sillage.screening import subzones as sz  # noqa: E402
from sillage.terrain.dem import Dem  # noqa: E402


def _dem(n: int = 60, res_m: float = 50.0) -> Dem:
    z = np.tile(np.exp(-(np.linspace(-1, 1, n) ** 2) / 0.05) * 400.0, (n, 1))
    return Dem(elevation=z.astype("float32"), transform=from_origin(600000.0, 4900000.0, res_m, res_m),
              crs=CRS.from_epsg(32631), resolution_m=res_m)


def _patch_windninja(monkeypatch, recorder):
    def fake_run_mass(**kw):
        recorder.setdefault("calls", []).append(kw)
        return WindNinjaRun(command=["x"], working_dir=kw["working_dir"], returncode=0)

    monkeypatch.setattr(sz, "run_mass", fake_run_mass)
    monkeypatch.setattr(sz, "write_dem", lambda *a, **k: None)
    monkeypatch.setattr(sz, "crop_dem", lambda *a, **k: None)
    monkeypatch.setattr(sz, "find_speed_grid", lambda work: "vel.asc")
    monkeypatch.setattr(sz, "load_speed_grid", lambda p: np.full((10, 10), 7.0, dtype="float32"))


def test_subzone_field_solves_every_tile_in_parallel(monkeypatch, tmp_path):
    rec: dict = {}
    _patch_windninja(monkeypatch, rec)
    dem = _dem()
    seen_centers = []

    def provider(x, y):
        seen_centers.append((round(x), round(y)))
        return 8.0, 270.0

    field = sz.subzone_speed_field(
        dem=dem, cli="WindNinja_cli", wind_at_center=provider, nx=2, ny=2,
        work_root=tmp_path, resolution_m=150.0, max_workers=4,
    )
    assert len(rec["calls"]) == 4              # one mass solve per tile
    assert len(set(seen_centers)) == 4         # four distinct tile centres
    assert field.shape == dem.shape
    assert np.isfinite(field).any()
    # each run is thread-capped so parallel tiles don't oversubscribe the CPU
    assert all(c["num_threads"] >= 1 for c in rec["calls"])


def test_subzone_field_runs_concurrently(monkeypatch, tmp_path):
    """With 4 workers, the 4 tile solves overlap in time (barrier proves concurrency)."""
    rec: dict = {}
    _patch_windninja(monkeypatch, rec)
    barrier = threading.Barrier(4, timeout=5)

    def fake_run_mass(**kw):
        barrier.wait()  # only returns if all 4 are in-flight at once
        return WindNinjaRun(command=["x"], working_dir=kw["working_dir"], returncode=0)

    monkeypatch.setattr(sz, "run_mass", fake_run_mass)
    sz.subzone_speed_field(
        dem=_dem(), cli="c", wind_at_center=lambda x, y: (8.0, 270.0), nx=2, ny=2,
        work_root=tmp_path, max_workers=4,
    )  # raises BrokenBarrierError if they did NOT run concurrently


def test_subzone_field_cancel_propagates(monkeypatch, tmp_path):
    rec: dict = {}
    _patch_windninja(monkeypatch, rec)
    with pytest.raises(RuntimeError):
        sz.subzone_speed_field(
            dem=_dem(), cli="c", wind_at_center=lambda x, y: (8.0, 270.0), nx=2, ny=2,
            work_root=tmp_path, cancel=lambda: True,
        )


def test_subzone_tile_retries_once_on_transient_failure(monkeypatch, tmp_path):
    rec: dict = {}
    _patch_windninja(monkeypatch, rec)
    attempts: dict = {}

    def fake_run_mass(**kw):
        wd = str(kw["working_dir"])
        n = attempts.get(wd, 0)
        attempts[wd] = n + 1
        rc = 4294967295 if n == 0 else 0  # fail first attempt per tile (HTTP 500), then ok
        return WindNinjaRun(command=["x"], working_dir=kw["working_dir"], returncode=rc,
                            stderr="ERROR 1 : HTTP error code : 500")

    monkeypatch.setattr(sz, "run_mass", fake_run_mass)
    field = sz.subzone_speed_field(
        dem=_dem(), cli="c", wind_at_center=lambda x, y: (8.0, 270.0), nx=1, ny=2,
        work_root=tmp_path, max_workers=2,
    )
    assert field.shape == _dem().shape          # recovered from the transient failure
    assert all(v == 2 for v in attempts.values())  # exactly one retry per tile


def test_windninja_env_disables_proj_network_and_isolates_tmp(tmp_path):
    from sillage.flow.windninja import _subprocess_env

    assert _subprocess_env()["PROJ_NETWORK"] == "OFF"
    # per-run temp dir isolates concurrent WindNinja/GDAL scratch files (no rc=-1 races)
    env = _subprocess_env(tmp_path / "_wn_tmp")
    for key in ("TMP", "TEMP", "TMPDIR", "CPL_TMPDIR"):
        assert env[key] == str(tmp_path / "_wn_tmp")
    assert (tmp_path / "_wn_tmp").is_dir()
    assert env["PROJ_USER_WRITABLE_DIRECTORY"] == str(tmp_path / "_wn_tmp")


def test_run_mass_num_threads_flag():
    run = run_mass(cli="WindNinja_cli", dem_path="/tmp/d.tif", working_dir="/tmp/w",
                   wind_speed_ms=10.0, wind_from_deg=270.0, num_threads=3, dry_run=True)
    assert f"--{FLAG['num_threads']}=3" in " ".join(run.command)
    # default: no thread flag (unchanged behaviour)
    run2 = run_mass(cli="WindNinja_cli", dem_path="/tmp/d.tif", working_dir="/tmp/w",
                    wind_speed_ms=10.0, wind_from_deg=270.0, dry_run=True)
    assert "--num_threads" not in " ".join(run2.command)


def test_windninja_default_env_does_not_leak_project_tmp(monkeypatch):
    from sillage.flow.windninja import _subprocess_env

    marker = "__sillage_project_tmp_marker__"
    for key in ("TMP", "TEMP", "TMPDIR", "CPL_TMPDIR", "PROJ_USER_WRITABLE_DIRECTORY"):
        monkeypatch.setenv(key, marker)
    env = _subprocess_env()
    for key in ("TMP", "TEMP", "TMPDIR", "CPL_TMPDIR", "PROJ_USER_WRITABLE_DIRECTORY"):
        assert env.get(key) != marker
