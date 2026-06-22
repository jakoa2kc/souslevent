"""Build the first Sillage Pass-1 map for the Champsaur study area.

This is a bootstrap command: it can sample Open-Meteo's elevation API to create a coarse
DEM, then computes the geometry-only screening indicator and saves a PNG map. The output
is for pipeline validation and visual exploration, not flight decisions.
"""

from __future__ import annotations

import time
from pathlib import Path

import click
import numpy as np
import requests

from sillage.areas import CHAMPSAUR, StudyArea
from sillage.config import load_config, resolve_output_path
from sillage.screening import indicator as ind
from sillage.terrain.dem import load_dem, write_dem

ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"


@click.command()
@click.option("--dem", "dem_path", default="", type=click.Path(exists=False),
              help="Existing DEM to use instead of the cached/downloaded Champsaur DEM.")
@click.option("--download-dem", is_flag=True,
              help="Download/rebuild the bootstrap DEM from Open-Meteo Elevation.")
@click.option("--grid-spacing", default=2000.0, show_default=True,
              help="Bootstrap DEM sampling spacing in meters.")
@click.option("--request-delay", default=1.0, show_default=True,
              help="Delay between Open-Meteo elevation requests, in seconds.")
@click.option("--wind-dir", "wind_from_deg", default=320.0, show_default=True,
              help="Wind FROM direction (meteorological degrees, 0=N).")
@click.option("--wind-speed", "wind_speed_ms", default=8.0, show_default=True,
              help="Wind speed in m/s, used for the map title for now.")
@click.option("--save", "save_path", default="outputs/champsaur/champsaur_pass1_geometry.png",
              show_default=True, help="PNG output path.")
@click.option("--show", is_flag=True, help="Show the matplotlib window after saving.")
def main(
    dem_path,
    download_dem,
    grid_spacing,
    request_delay,
    wind_from_deg,
    wind_speed_ms,
    save_path,
    show,
):
    """Create a first 2D Champsaur screening map."""
    cfg = load_config()
    work_dir = cfg.cache_dir / "champsaur"
    work_dir.mkdir(parents=True, exist_ok=True)

    source_dem = Path(dem_path) if dem_path else work_dir / "champsaur_open_meteo_wgs84.tif"
    prepared_dem = work_dir / "champsaur_prepared_utm.tif"

    if dem_path:
        click.echo(f"[1/4] Using provided DEM: {source_dem}")
    elif download_dem or not source_dem.exists():
        click.echo("[1/4] Building bootstrap DEM from Open-Meteo Elevation ...")
        build_open_meteo_dem(
            CHAMPSAUR,
            source_dem,
            grid_spacing_m=grid_spacing,
            request_delay_s=request_delay,
        )
        click.echo(f"      wrote {source_dem}")
    else:
        click.echo(f"[1/4] Reusing cached bootstrap DEM: {source_dem}")

    click.echo("[2/4] Loading/reprojecting DEM to UTM north-up meters ...")
    dem = load_dem(str(source_dem), max_domain_km=cfg.max_domain_km)
    write_dem(dem, prepared_dem)
    ex, ey = dem.extent_km
    click.echo(
        f"      grid {dem.shape}, res {dem.resolution_m:.1f} m, "
        f"domain {ex:.1f} x {ey:.1f} km, CRS {dem.crs.to_string()}"
    )
    click.echo(f"      prepared WindNinja DEM: {prepared_dem}")

    click.echo("[3/4] Computing geometry-only Pass-1 indicator + candidates ...")
    hazard = ind.hazard_indicator(dem, wind_from_deg, speed_grid=None)
    candidates = ind.find_candidates(dem, hazard, n=10)
    for i, c in enumerate(candidates, start=1):
        click.echo(f"      #{i:02d} x={c.x:.0f} y={c.y:.0f} score={c.score:.2f}")

    click.echo("[4/4] Rendering map ...")
    from sillage.viz.map2d import show_static

    fig = show_static(
        dem,
        hazard,
        title=(
            f"Sillage Pass-1 bootstrap Champsaur - wind {wind_speed_ms:.0f} m/s "
            f"from {wind_from_deg:.0f} deg"
        ),
    )
    out = resolve_output_path(save_path, cfg)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    click.echo(f"      saved {out}")

    if show:
        import matplotlib.pyplot as plt

        plt.show()


def build_open_meteo_dem(
    area: StudyArea,
    out_path: Path,
    grid_spacing_m: float = 2000.0,
    request_delay_s: float = 1.0,
) -> Path:
    """Sample Open-Meteo elevation over ``area`` and write a WGS84 GeoTIFF."""
    import rasterio
    from rasterio.transform import from_bounds

    lons, lats = _lonlat_grid(area, grid_spacing_m)
    lon_grid, lat_grid = np.meshgrid(lons, lats)
    elevation = _fetch_elevation(
        lat_grid.ravel(),
        lon_grid.ravel(),
        request_delay_s=request_delay_s,
    ).reshape(lat_grid.shape)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    nodata = -9999.0
    data = np.where(np.isfinite(elevation), elevation, nodata).astype("float32")
    transform = from_bounds(area.west, area.south, area.east, area.north, len(lons), len(lats))
    with rasterio.open(
        out_path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
        nodata=nodata,
    ) as dst:
        dst.write(data, 1)
    return out_path


def _lonlat_grid(area: StudyArea, spacing_m: float) -> tuple[np.ndarray, np.ndarray]:
    mid_lat = (area.south + area.north) / 2.0
    meters_per_deg_lat = 111_320.0
    meters_per_deg_lon = meters_per_deg_lat * np.cos(np.deg2rad(mid_lat))
    width_m = (area.east - area.west) * meters_per_deg_lon
    height_m = (area.north - area.south) * meters_per_deg_lat
    nx = max(2, int(round(width_m / spacing_m)) + 1)
    ny = max(2, int(round(height_m / spacing_m)) + 1)
    return (
        np.linspace(area.west, area.east, nx),
        np.linspace(area.north, area.south, ny),
    )


def _fetch_elevation(
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    batch_size: int = 100,
    request_delay_s: float = 1.0,
    max_retries: int = 4,
) -> np.ndarray:
    vals: list[float] = []
    total = len(latitudes)
    for start in range(0, total, batch_size):
        end = min(start + batch_size, total)
        params = {
            "latitude": ",".join(f"{v:.6f}" for v in latitudes[start:end]),
            "longitude": ",".join(f"{v:.6f}" for v in longitudes[start:end]),
        }
        resp = None
        for attempt in range(max_retries + 1):
            try:
                resp = requests.get(ELEVATION_URL, params=params, timeout=30.0)
            except requests.RequestException as exc:
                if attempt == max_retries:
                    raise
                wait_s = max(15.0, request_delay_s * (attempt + 1) * 5.0)
                click.echo(
                    f"      network error ({exc.__class__.__name__}); waiting {wait_s:.0f}s",
                    err=True,
                )
                time.sleep(wait_s)
                continue
            if resp.status_code != 429:
                break
            if attempt == max_retries:
                resp.raise_for_status()
            wait_s = max(15.0, request_delay_s * (attempt + 1) * 5.0)
            click.echo(f"      rate limited; waiting {wait_s:.0f}s", err=True)
            time.sleep(wait_s)
        if resp is None:
            raise RuntimeError("Open-Meteo elevation request did not return a response.")
        resp.raise_for_status()
        chunk = resp.json().get("elevation")
        if chunk is None or len(chunk) != end - start:
            raise RuntimeError("Unexpected Open-Meteo elevation response shape.")
        vals.extend(np.nan if v is None else float(v) for v in chunk)
        click.echo(f"      elevation samples {end}/{total}", err=True)
        if end < total and request_delay_s > 0:
            time.sleep(request_delay_s)
    return np.asarray(vals, dtype="float64")


if __name__ == "__main__":
    main()



