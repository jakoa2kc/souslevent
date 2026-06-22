"""Run/reuse WindNinja mass outputs and render a Champsaur Pass-1 screening map.

This is the first real Pass-1 map: terrain geometry plus WindNinja mass-solver velocity
shadow. It still shows CANDIDATE disturbed lee-air zones, not rotor boundaries.
"""

from __future__ import annotations

from pathlib import Path

import click
import numpy as np

from sillage.config import load_config, resolve_cache_path, resolve_output_path
from sillage.flow.windninja import run_mass
from sillage.screening import indicator as ind
from sillage.terrain.dem import load_dem

DEFAULT_DEM = "cache/champsaur/ign/champsaur_rgealti_50m_prepared_utm.tif"


@click.command()
@click.option("--dem", "dem_path", default=DEFAULT_DEM, show_default=True,
              type=click.Path(exists=False), help="Prepared UTM DEM for WindNinja/Sillage.")
@click.option("--wind-dir", "wind_from_deg", default=320.0, show_default=True,
              help="Wind FROM direction (meteorological degrees, 0=N).")
@click.option("--wind-speed", "wind_speed_ms", default=8.0, show_default=True,
              help="Domain-average wind speed in m/s.")
@click.option("--resolution", "resolution_m", default=100.0, show_default=True,
              help="WindNinja mass mesh / ASCII output resolution in meters.")
@click.option("--vegetation", default="grass", show_default=True,
              type=click.Choice(["grass", "brush", "trees"], case_sensitive=False),
              help="Dominant vegetation type required by WindNinja.")
@click.option("--edge-buffer", "edge_buffer_m", default=1500.0, show_default=True,
              help="Mask this border width in meters to reduce DEM edge artifacts.")
@click.option("--force-run", is_flag=True, help="Run WindNinja even if _vel.asc exists.")
@click.option("--save", "save_path", default="outputs/champsaur/champsaur_pass1_mass_320_8_100m.png",
              show_default=True, help="PNG output path.")
def main(
    dem_path,
    wind_from_deg,
    wind_speed_ms,
    resolution_m,
    vegetation,
    edge_buffer_m,
    force_run,
    save_path,
):
    """Render a Champsaur Pass-1 map with WindNinja mass velocity deficit."""
    cfg = load_config()
    dem_file = resolve_cache_path(dem_path, cfg)
    if not dem_file.exists():
        raise SystemExit(f"DEM not found: {dem_file}")
    work_dir = cfg.cache_dir / "champsaur" / (
        f"windninja_mass_{wind_from_deg:.0f}_{wind_speed_ms:.0f}_{resolution_m:.0f}m"
    )

    click.echo(f"[1/5] Loading DEM {dem_file}")
    dem = load_dem(str(dem_file), max_domain_km=cfg.max_domain_km)
    ex, ey = dem.extent_km
    click.echo(f"      grid {dem.shape}, res {dem.resolution_m:.1f} m, domain {ex:.1f} x {ey:.1f} km")

    speed_path = find_speed_grid(work_dir)
    if force_run or speed_path is None:
        click.echo("[2/5] Running WindNinja mass solver ...")
        run = run_mass(
            cli=cfg.windninja_cli,
            dem_path=str(dem_file),
            working_dir=str(work_dir),
            wind_speed_ms=wind_speed_ms,
            wind_from_deg=wind_from_deg,
            output_resolution_m=resolution_m,
            vegetation=vegetation,
        )
        if run.returncode not in (0, None):
            raise SystemExit(
                f"WindNinja failed rc={run.returncode}\n"
                f"STDOUT tail:\n{run.stdout[-1200:]}\nSTDERR tail:\n{run.stderr[-1200:]}"
            )
        speed_path = find_speed_grid(work_dir)
        if speed_path is None:
            raise SystemExit(f"WindNinja succeeded but no *_vel.asc found in {work_dir}")
        click.echo(f"      outputs: {[p.name for p in run.output_paths]}")
    else:
        click.echo(f"[2/5] Reusing WindNinja speed grid {speed_path}")

    click.echo("[3/5] Loading speed grid and computing indicator ...")
    speed_grid = load_speed_grid(speed_path)
    click.echo(
        f"      speed grid {speed_grid.shape}, "
        f"min/mean/max {np.nanmin(speed_grid):.2f}/{np.nanmean(speed_grid):.2f}/{np.nanmax(speed_grid):.2f} m/s"
    )
    hazard = ind.hazard_indicator(dem, wind_from_deg, speed_grid=speed_grid)
    hazard = mask_edge_buffer(hazard, dem.resolution_m, edge_buffer_m)

    click.echo("[4/5] Ranking candidates ...")
    candidates = ind.find_candidates(dem, hazard, n=10)
    for i, c in enumerate(candidates, start=1):
        click.echo(f"      #{i:02d} x={c.x:.0f} y={c.y:.0f} score={c.score:.2f}")

    click.echo("[5/5] Rendering map ...")
    from sillage.viz.map2d import show_static

    fig = show_static(
        dem,
        hazard,
        title=(
            f"Sillage Pass-1 Champsaur - WindNinja mass {resolution_m:.0f}m - "
            f"wind {wind_speed_ms:.0f} m/s from {wind_from_deg:.0f} deg"
        ),
    )
    out = resolve_output_path(save_path, cfg)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    click.echo(f"      saved {out}")


def find_speed_grid(work_dir: Path) -> Path | None:
    """Return the WindNinja speed magnitude ASCII grid, if present."""
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
    """Zero a border around the indicator to suppress crop-edge artifacts."""
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


if __name__ == "__main__":
    main()


