"""Pass-1 driver helpers: turn one hour's wind into a masked hazard indicator.

Wraps the recurring chain used by the screening scripts and the hourly loop:
  WindNinja mass run (or reuse) -> read *_vel.asc speed grid -> hazard indicator ->
  edge-buffer mask. Network-free and binary-free parts stay importable for tests; the
  actual WindNinja call is delegated to flow.windninja (mockable).

See docs/05 (WindNinja CLI) and roadmap M1/M4 (hourly loop).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

import numpy as np

from ..flow.windninja import format_run_failure, run_mass
from ..terrain.dem import Dem
from . import indicator as ind


_FR_DAYS = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")


@dataclass(frozen=True)
class HourlyIndicatorResult:
    """One Pass-1 hourly result, kept in original time-window order."""

    label: str
    hazard: np.ndarray
    speed_path: Path
    direction_path: Path | None
    winds: list[tuple[float, float, float, float]]
    elapsed_s: float


def parallel_run_plan(
    count: int, max_workers: int | None = None, hard_cap: int = 4
) -> tuple[int, int]:
    """Return ``(workers, num_threads_per_windninja_run)`` for a batch of independent WindNinja
    solves (parallel hourly Pass-1 *and* the spatial sub-zones — one shared policy).

    Several mass solves run at once; each is capped to a small thread count to avoid CPU
    oversubscription. The default deliberately caps workers at ``hard_cap`` (4): enough to cut a
    batch sharply, still conservative for WindNinja/GDAL on Windows (the source of the
    intermittent ``rc=-1`` under heavy concurrency). Pass ``max_workers`` to override the cap.
    """
    import os

    count = max(0, int(count))
    if count <= 0:
        return 0, 0
    cpu = os.cpu_count() or 4
    if max_workers is None:
        workers = min(count, cpu, hard_cap)
    else:
        workers = min(count, max(1, int(max_workers)))
    per_run_threads = max(1, min(4, cpu // max(1, workers)))
    return workers, per_run_threads


# Backwards-compatible alias (the planner is no longer hourly-specific).
hourly_worker_plan = parallel_run_plan


def synthetic_series(
    hours: int, start=None, tz: str = "Europe/Paris"
) -> list[tuple[str, float, float]]:
    """Deterministic NW-sweep hourly wind series, labelled with ABSOLUTE local clock hours.

    The wind values are synthetic (NOT a forecast), but the labels are real wall-clock hours
    in ``tz`` (default Europe/Paris) starting at ``start`` (default: the current hour), so the
    slider reads "mar. 14h", "mar. 15h", ... Real winds come from Open-Meteo / AROME sub-zones
    (ADR-0007). Returns (label, speed_ms, from_deg) per hour.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)
    if start is None:
        start = datetime.now(zone).replace(minute=0, second=0, microsecond=0)
    out = []
    for i in range(hours):
        t = start + timedelta(hours=i)
        out.append((f"{_FR_DAYS[t.weekday()]} {t:%Hh}", 6.0 + 1.0 * i, (300.0 + 10.0 * i) % 360.0))
    return out


def find_speed_grid(work_dir: Path) -> Path | None:
    """Return the WindNinja speed-magnitude ASCII grid (``*_vel.asc``), if present."""
    work_dir = Path(work_dir)
    if not work_dir.exists():
        return None
    matches = sorted(work_dir.glob("*_vel.asc"), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def find_direction_grid(work_dir: Path) -> Path | None:
    """Return the WindNinja wind-direction ASCII grid (``*_ang.asc``), if present."""
    work_dir = Path(work_dir)
    if not work_dir.exists():
        return None
    matches = sorted(work_dir.glob("*_ang.asc"), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def sample_grid_at(path: Path, x: float, y: float) -> float | None:
    """Sample a raster value at CRS coords (x, y). Returns the value, or None if the point
    is outside the grid or hits nodata."""
    import rasterio

    with rasterio.open(path) as src:
        b = src.bounds
        if not (b.left <= x <= b.right and b.bottom <= y <= b.top):
            return None
        row, col = src.index(x, y)
        arr = src.read(1)
        if not (0 <= row < arr.shape[0] and 0 <= col < arr.shape[1]):
            return None
        val = float(arr[row, col])
    if not np.isfinite(val):
        return None
    return val


def upstream_crest_wind(
    vel_path: Path, ang_path: Path, x: float, y: float, from_deg: float,
    fetch_m: float = 1500.0,
) -> tuple[float, float] | None:
    """Sample the Pass-1 surface wind a short ``fetch_m`` UPSTREAM of (x, y).

    Used to drive the Pass-2 momentum boundary condition from the local upstream flow
    instead of a global domain wind (docs/05, ADR-0003). The upstream point lies toward the
    wind's source bearing (``from_deg``). Returns (speed_ms, from_deg) or None if the sample
    falls outside the field. WindNinja ``*_ang.asc`` is the meteorological from-direction,
    consistent with the momentum solver's ``wind_from_deg`` input.
    """
    rad = np.deg2rad(from_deg)
    ux = x + fetch_m * np.sin(rad)  # toward the source bearing = upwind
    uy = y + fetch_m * np.cos(rad)
    spd = sample_grid_at(vel_path, ux, uy)
    ang = sample_grid_at(ang_path, ux, uy)
    if spd is None or ang is None or spd <= 0:
        return None
    return float(spd), float(ang % 360.0)


def load_speed_grid(path: Path) -> np.ndarray:
    """Load a WindNinja ``*_vel.asc`` raster as a float array with nodata as NaN."""
    import rasterio

    with rasterio.open(path) as src:
        arr = src.read(1).astype("float64")
        if src.nodata is not None:
            arr[arr == src.nodata] = np.nan
    return arr


def mask_edge_buffer(indicator: np.ndarray, resolution_m: float, edge_buffer_m: float) -> np.ndarray:
    """Zero a border around the indicator to suppress DEM crop-edge artifacts."""
    out = np.array(indicator, copy=True)
    px = int(np.ceil(edge_buffer_m / resolution_m))
    if px <= 0:
        return out
    px = min(px, out.shape[0] // 2, out.shape[1] // 2)
    if px <= 0:
        return out
    out[:px, :] = 0.0
    out[-px:, :] = 0.0
    out[:, :px] = 0.0
    out[:, -px:] = 0.0
    return out


def hourly_indicator(
    *,
    dem: Dem,
    cli: str,
    dem_path: str,
    work_dir: Path,
    wind_speed_ms: float,
    wind_from_deg: float,
    resolution_m: float = 100.0,
    vegetation: str = "grass",
    edge_buffer_m: float = 1500.0,
    force_run: bool = False,
    num_threads: int | None = None,
    on_progress=None,
    cancel=None,
) -> tuple[np.ndarray, Path]:
    """Compute the masked Pass-1 hazard indicator for ONE hour's domain-average wind.

    Reuses an existing ``*_vel.asc`` in ``work_dir`` unless ``force_run`` is set; otherwise
    runs the WindNinja mass solver. ``on_progress``/``cancel`` are forwarded to the solver
    so the IHM worker thread can report progress and cancel (ADR-0009). Returns
    (indicator_on_dem_grid, speed_grid_path).
    """
    work_dir = Path(work_dir)
    speed_path = find_speed_grid(work_dir)
    if force_run or speed_path is None:
        run = run_mass(
            cli=cli,
            dem_path=dem_path,
            working_dir=str(work_dir),
            wind_speed_ms=wind_speed_ms,
            wind_from_deg=wind_from_deg,
            output_resolution_m=resolution_m,
            vegetation=vegetation,
            num_threads=num_threads,
            tmp_dir=work_dir / "_tmp",
            on_progress=on_progress,
            cancel=cancel,
        )
        if run.returncode not in (0, None):
            raise RuntimeError(format_run_failure(run, "WindNinja mass"))
        speed_path = find_speed_grid(work_dir)
        if speed_path is None:
            raise RuntimeError(f"WindNinja succeeded but no *_vel.asc in {work_dir}")

    speed_grid = load_speed_grid(speed_path)
    hazard = ind.hazard_indicator(dem, wind_from_deg, speed_grid=speed_grid)
    hazard = mask_edge_buffer(hazard, dem.resolution_m, edge_buffer_m)
    return hazard, speed_path


def hourly_indicator_stack(
    *,
    dem: Dem,
    cli: str,
    dem_path: str,
    series: Sequence[tuple[str, float, float]],
    work_dir_for: Callable[[int, str, float, float], Path],
    resolution_m: float = 100.0,
    vegetation: str = "grass",
    edge_buffer_m: float = 1500.0,
    force_run: bool = False,
    max_workers: int | None = None,
    on_progress=None,
    cancel=None,
) -> list[HourlyIndicatorResult]:
    """Compute several independent Pass-1 hours concurrently.

    ``series`` is ``[(label, speed_ms, from_deg), ...]``. The returned list preserves that order.
    Each hour gets its own work directory and isolated WindNinja temp dir through
    :func:`hourly_indicator`. Existing ``*_vel.asc`` files are reused unless ``force_run`` is
    true, so a second launch remains cheap.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from threading import Lock

    items = list(series)
    n = len(items)
    if n == 0:
        return []
    import os

    workers, per_run_threads = parallel_run_plan(n, max_workers=max_workers)
    cpu = os.cpu_count() or 4  # full threads for the lone sequential retries below
    left, bottom, right, top = dem.bounds
    cx, cy = (left + right) / 2.0, (bottom + top) / 2.0
    progress = [0.0] * n
    results: list[HourlyIndicatorResult | None] = [None] * n
    lock = Lock()

    def _emit(i: int, pct: float, msg: str) -> None:
        if on_progress is None:
            return
        with lock:
            progress[i] = max(progress[i], max(0.0, min(100.0, float(pct))))
            agg = int(round(sum(progress) / n))
        on_progress(agg, msg)

    def _solve(i: int, threads: int) -> HourlyIndicatorResult:
        if cancel is not None and cancel():
            raise RuntimeError("cancelled")
        label, spd, drc = items[i]
        work = Path(work_dir_for(i, label, spd, drc))
        start = perf_counter()

        def hp(pct, msg):
            _emit(i, pct, f"{i + 1}/{n} {label}: {msg}")

        hazard, vel = hourly_indicator(
            dem=dem, cli=cli, dem_path=dem_path, work_dir=work,
            wind_speed_ms=spd, wind_from_deg=drc, resolution_m=resolution_m,
            vegetation=vegetation, edge_buffer_m=edge_buffer_m, force_run=force_run,
            num_threads=threads, on_progress=hp, cancel=cancel,
        )
        _emit(i, 100, f"{i + 1}/{n} {label}: terminé")
        return HourlyIndicatorResult(
            label=label,
            hazard=hazard,
            speed_path=vel,
            direction_path=find_direction_grid(work),
            winds=[(cx, cy, float(spd), float(drc))],
            elapsed_s=perf_counter() - start,
        )

    if on_progress is not None:
        on_progress(0, f"{n} heures en parallèle ×{workers} ({per_run_threads} threads/run)")

    failed: list[int] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_solve, i, per_run_threads): i for i in range(n)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()
            except Exception:
                if cancel is not None and cancel():
                    for other in futs:
                        other.cancel()
                    raise RuntimeError("cancelled")
                failed.append(i)  # retry alone after the pool drains (rules out contention)

    for i in sorted(failed):  # sequential fallback: no concurrency -> no rc=-1 races
        if cancel is not None and cancel():
            raise RuntimeError("cancelled")
        if on_progress is not None:
            on_progress(int(round(sum(progress) / n)), f"reprise séquentielle de l'heure {i + 1}/{n}…")
        results[i] = _solve(i, cpu)  # full threads, alone; raises only if it truly fails

    return [r for r in results if r is not None]
