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

import sys  # noqa: E402
import threading  # noqa: E402
from pathlib import Path  # noqa: E402

from sillage.terrain.dem import Dem  # noqa: E402
from sillage.screening import indicator as ind  # noqa: E402
from sillage.screening import pass1 as p1  # noqa: E402
from sillage.flow.windninja import run_mass, run_momentum, FLAG  # noqa: E402
from sillage.flow import windninja as wn  # noqa: E402


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


def test_parse_progress():
    assert wn._parse_progress("Run 0: (solver) 36% complete...") == 36
    assert wn._parse_progress("no percentage here") is None


def test_run_streams_progress_and_captures_output(tmp_path):
    code = (
        "import sys\n"
        "for p in (10, 50, 100):\n"
        "    print(f'(solver) {p}% complete', flush=True)\n"
        "print('Run number 0 done!', flush=True)\n"
    )
    seen: list[int] = []
    rc, out, err = wn._run([sys.executable, "-c", code], tmp_path, dry_run=False,
                           on_progress=lambda pct, msg: seen.append(pct))
    assert rc == 0
    assert seen[:3] == [10, 50, 100]  # phase lines (e.g. "Run number 0 done!") may re-emit 100
    assert "Run number 0 done!" in out


def test_run_cancel_terminates(tmp_path):
    code = (
        "import time\n"
        "for i in range(100000):\n"
        "    print(f'{i % 100}% complete', flush=True)\n"
        "    time.sleep(0.02)\n"
    )
    state = {"n": 0}
    rc, out, err = wn._run(
        [sys.executable, "-c", code], tmp_path, dry_run=False,
        on_progress=lambda pct, msg: state.__setitem__("n", state["n"] + 1),
        cancel=lambda: state["n"] >= 1,
    )
    assert state["n"] >= 1
    assert "[cancelled by user]" in err


def test_hourly_worker_plan_is_conservative():
    workers, threads = p1.hourly_worker_plan(8, max_workers=99)
    assert workers == 8
    assert 1 <= threads <= 4
    assert p1.hourly_worker_plan(0) == (0, 0)


def test_hourly_indicator_stack_runs_parallel_and_preserves_order(monkeypatch, tmp_path):
    dem = _synthetic_ridge(n=20, res_m=50.0)
    barrier = threading.Barrier(3, timeout=5)
    ran: set[str] = set()
    calls: list[dict] = []

    def fake_run_mass(**kw):
        calls.append(kw)
        barrier.wait()
        ran.add(str(kw["working_dir"]))
        return wn.WindNinjaRun(command=["x"], working_dir=kw["working_dir"], returncode=0)

    def fake_find_speed_grid(work):
        return Path(work) / "x_vel.asc" if str(work) in ran else None

    monkeypatch.setattr(p1, "run_mass", fake_run_mass)
    monkeypatch.setattr(p1, "find_speed_grid", fake_find_speed_grid)
    monkeypatch.setattr(p1, "find_direction_grid", lambda work: Path(work) / "x_ang.asc")
    monkeypatch.setattr(p1, "load_speed_grid", lambda path: np.ones(dem.shape))

    series = [(f"h{i}", 6.0 + i, 270.0 + i) for i in range(3)]
    results = p1.hourly_indicator_stack(
        dem=dem, cli="WindNinja_cli", dem_path="/tmp/dem.tif", series=series,
        work_dir_for=lambda i, *_args: tmp_path / f"h{i}", force_run=True, max_workers=3,
    )

    assert [r.label for r in results] == ["h0", "h1", "h2"]
    assert len(calls) == 3
    assert all(call["num_threads"] >= 1 for call in calls)
    assert all(str(call["tmp_dir"]).endswith("_tmp") for call in calls)


def test_run_timings_summary():
    from sillage.timing import RunTimings, format_seconds

    timings = RunTimings()
    timings.add("a", 0.05)
    timings.add("b", 1.25)
    assert "a 50ms" in timings.summary()
    assert "b 1.2s" in timings.summary()
    assert format_seconds(65) == "1m05s"



