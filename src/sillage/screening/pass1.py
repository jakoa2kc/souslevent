"""Pass-1 driver helpers: turn one hour's wind into a masked hazard indicator.

Wraps the recurring chain used by the screening scripts and the hourly loop:
  WindNinja mass run (or reuse) -> read *_vel.asc speed grid -> hazard indicator ->
  edge-buffer mask. Network-free and binary-free parts stay importable for tests; the
  actual WindNinja call is delegated to flow.windninja (mockable).

See docs/05 (WindNinja CLI) and roadmap M1/M4 (hourly loop).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ..flow.windninja import run_mass
from ..terrain.dem import Dem
from . import indicator as ind


def find_speed_grid(work_dir: Path) -> Path | None:
    """Return the WindNinja speed-magnitude ASCII grid (``*_vel.asc``), if present."""
    work_dir = Path(work_dir)
    if not work_dir.exists():
        return None
    matches = sorted(work_dir.glob("*_vel.asc"))
    return matches[0] if matches else None


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
            on_progress=on_progress,
            cancel=cancel,
        )
        if run.returncode not in (0, None):
            raise RuntimeError(
                f"WindNinja mass failed rc={run.returncode}\n"
                f"STDERR tail:\n{run.stderr[-1000:]}"
            )
        speed_path = find_speed_grid(work_dir)
        if speed_path is None:
            raise RuntimeError(f"WindNinja succeeded but no *_vel.asc in {work_dir}")

    speed_grid = load_speed_grid(speed_path)
    hazard = ind.hazard_indicator(dem, wind_from_deg, speed_grid=speed_grid)
    hazard = mask_edge_buffer(hazard, dem.resolution_m, edge_buffer_m)
    return hazard, speed_path
