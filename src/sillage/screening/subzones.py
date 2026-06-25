"""Pass-1 spatial wind via sub-zones (ADR-0007, interim).

The mass solver takes a single domain-average wind, so to get valley-to-valley wind
differences without full gridded weather-model init we **tile** the domain, run the mass
solver per tile with that tile's own representative wind, and **mosaic** the per-tile surface
speed fields back onto the full DEM grid with feathered blending in the overlaps.

The per-tile wind comes from a pluggable provider ``wind_at_center(x, y) -> (speed_ms,
from_deg)`` so the mechanism is independent of the source — a synthetic field for tests, or
AROME / Open-Meteo sampled at each tile's centre + crest altitude in production.

Altitude is NOT a tiling axis: it enters only as the per-tile sampling height of the
forecast profile; intra-tile elevation variation is handled by the mass solver (ADR-0007).
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from ..flow.windninja import format_run_failure, run_mass
from ..terrain.dem import Dem, crop_dem, write_dem
from .pass1 import find_speed_grid, load_speed_grid


def subzone_bboxes(
    dem: Dem, nx: int, ny: int, overlap_frac: float = 0.2
) -> list[tuple[tuple[float, float, float, float], tuple[float, float]]]:
    """Split the DEM into nx*ny tiles with an overlap margin. Returns [(bbox, center)].

    bbox = (left, bottom, right, top) in CRS meters, expanded by ``overlap_frac`` of the tile
    size on each side (clamped to the domain). center = the tile's *core* centre (no overlap).
    """
    left, bottom, right, top = dem.bounds
    tw, th = (right - left) / nx, (top - bottom) / ny
    ox, oy = overlap_frac * tw, overlap_frac * th
    tiles = []
    for j in range(ny):
        for i in range(nx):
            cl, cr = left + i * tw, left + (i + 1) * tw
            cb, ct = bottom + j * th, bottom + (j + 1) * th
            center = ((cl + cr) / 2.0, (cb + ct) / 2.0)
            bbox = (
                max(left, cl - ox), max(bottom, cb - oy),
                min(right, cr + ox), min(top, ct + oy),
            )
            tiles.append((bbox, center))
    return tiles


def _feather(h: int, w: int) -> np.ndarray:
    """Blend weight: ~1 in the tile core, ramping to 0.1 at the edges (overlap feathering)."""
    ry = 1.0 - np.abs(np.linspace(-1.0, 1.0, h))
    rx = 1.0 - np.abs(np.linspace(-1.0, 1.0, w))
    return 0.1 + 0.9 * np.outer(ry, rx)


def _resample(arr: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    from scipy.ndimage import zoom

    arr = np.asarray(arr, dtype="float64")
    if not np.isfinite(arr).all():  # fill nodata so zoom doesn't spread NaN
        fill = float(np.nanmean(arr)) if np.isfinite(arr).any() else 0.0
        arr = np.where(np.isfinite(arr), arr, fill)
    if arr.shape == shape:
        return arr
    return zoom(arr, (shape[0] / arr.shape[0], shape[1] / arr.shape[1]), order=1)


def assemble_mosaic(dem: Dem, contributions) -> np.ndarray:
    """Mosaic per-tile speed grids onto the full DEM grid with feathered blending.

    ``contributions`` = sequence of (bbox, speed_grid). Each grid is resampled to its bbox
    pixel window in the DEM grid and accumulated with a feather weight; overlaps are blended.
    Cells never covered are NaN.
    """
    from rasterio.transform import rowcol

    h, w = dem.shape
    acc = np.zeros((h, w))
    wsum = np.zeros((h, w))
    for bbox, grid in contributions:
        left, bottom, right, top = bbox
        r0, c0 = rowcol(dem.transform, left, top, op=math.floor)
        r1, c1 = rowcol(dem.transform, right, bottom, op=math.ceil)
        r0, r1 = sorted((max(0, int(r0)), min(h, int(r1))))
        c0, c1 = sorted((max(0, int(c0)), min(w, int(c1))))
        wh, ww = r1 - r0, c1 - c0
        if wh <= 0 or ww <= 0:
            continue
        g = _resample(grid, (wh, ww))
        feather = _feather(wh, ww)
        acc[r0:r1, c0:c1] += g * feather
        wsum[r0:r1, c0:c1] += feather
    out = np.full((h, w), np.nan)
    nz = wsum > 0
    out[nz] = acc[nz] / wsum[nz]
    return out


def subzone_speed_field(
    *,
    dem: Dem,
    cli: str,
    wind_at_center,
    nx: int,
    ny: int,
    work_root: Path,
    resolution_m: float = 100.0,
    vegetation: str = "grass",
    overlap_frac: float = 0.2,
    on_progress=None,
    cancel=None,
    max_workers: int | None = None,
) -> np.ndarray:
    """Run the mass solver per sub-zone with its own wind, mosaic the speed fields.

    ``wind_at_center(x, y) -> (speed_ms, from_deg)`` supplies each tile's representative wind
    (AROME/Open-Meteo in production, synthetic in tests). Returns a full-DEM-grid speed field.

    The per-tile solves are **independent**, so they run **concurrently** on a thread pool
    (WindNinja is a subprocess — the GIL is released while it runs). ``max_workers`` defaults to
    ~the CPU count (capped by tile count); each WindNinja run is limited to ``cpu // workers``
    threads so the parallel tiles don't oversubscribe the CPU.

    Robustness: a tile that fails in the parallel pass is **retried sequentially** at the end,
    with no other run in flight — this rules out any cross-process contention (the cause of the
    intermittent ``rc=-1``). Only a tile that fails even *alone* raises (with the full WindNinja
    output). ``on_progress`` is reported from this thread; ``cancel`` stops the loop and
    terminates in-flight runs.
    """
    import os
    from concurrent.futures import ThreadPoolExecutor, as_completed

    work_root = Path(work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    tiles = subzone_bboxes(dem, nx, ny, overlap_frac)
    n = len(tiles)
    if n == 0:
        return assemble_mosaic(dem, [])

    cpu = os.cpu_count() or 4
    workers = max(1, min(n, cpu if max_workers is None else max_workers))
    per_run_threads = max(1, cpu // workers)

    def _solve(i, threads):
        if cancel is not None and cancel():
            raise RuntimeError("cancelled")
        bbox, (cx, cy) = tiles[i]
        spd, drc = wind_at_center(cx, cy)
        half_w = (bbox[2] - bbox[0]) / 2.0
        half_h = (bbox[3] - bbox[1]) / 2.0
        crop = crop_dem(dem, (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0,
                        half_w, half_h)
        crop_path = work_root / f"tile_{i:02d}.tif"
        write_dem(crop, crop_path)
        work = work_root / f"tile_{i:02d}"
        run = run_mass(
            cli=cli, dem_path=str(crop_path), working_dir=str(work),
            wind_speed_ms=spd, wind_from_deg=drc, output_resolution_m=resolution_m,
            vegetation=vegetation, num_threads=threads,
            tmp_dir=work / "_wn_tmp", cancel=cancel,
        )
        if run.returncode not in (0, None):
            raise RuntimeError(format_run_failure(run, f"sub-zone {i} mass"))
        vel = find_speed_grid(work)
        if vel is None:
            raise RuntimeError(f"sub-zone {i}: no *_vel.asc in {work}")
        return bbox, load_speed_grid(vel)

    contributions: list = [None] * n
    done = 0
    failed: list[int] = []
    if on_progress is not None:
        on_progress(0, f"0/{n} zones (parallèle ×{workers})…")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_solve, i, per_run_threads): i for i in range(n)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                contributions[i] = fut.result()
                done += 1
                if on_progress is not None:
                    on_progress(int(done / n * 100), f"{done}/{n} zones (parallèle ×{workers})")
            except Exception:
                if cancel is not None and cancel():
                    for f in futs:
                        f.cancel()
                    raise
                failed.append(i)  # retry alone, after the pool drains

    for i in sorted(failed):  # sequential fallback: no concurrency -> no contention
        if cancel is not None and cancel():
            raise RuntimeError("cancelled")
        if on_progress is not None:
            on_progress(int(done / n * 100), f"reprise séquentielle de la zone {i + 1}/{n}…")
        contributions[i] = _solve(i, cpu)  # full threads, alone; raises if it truly fails
        done += 1

    return assemble_mosaic(dem, [c for c in contributions if c is not None])
