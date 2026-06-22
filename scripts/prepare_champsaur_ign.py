"""Prepare a real IGN RGE ALTI DEM crop for the Champsaur study area.

The official RGE ALTI download is departmental. This script keeps the workflow sane:
select only the 5 km ASC tiles intersecting the Champsaur bounds, extract them from the
D005 Hautes-Alpes archive, mosaic/crop the area, reproject to UTM, downsample to a Pass-1
analysis resolution, then render the geometry-only screening map.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import numpy as np
import py7zr
import requests

from sillage.areas import CHAMPSAUR, StudyArea
from sillage.config import load_config, resolve_output_path
from sillage.screening import indicator as ind
from sillage.terrain.dem import load_dem, write_dem

RGEALTI_D005_5M_TITLE = "RGEALTI_2-0_5M_ASC_LAMB93-IGN69_D005_2020-10-14"
RGEALTI_D005_5M_URL = (
    "https://data.geopf.fr/telechargement/download/RGEALTI/"
    f"{RGEALTI_D005_5M_TITLE}/{RGEALTI_D005_5M_TITLE}.7z"
)
LAMB93 = "EPSG:2154"
TILE_RE = re.compile(r"RGEALTI_FXX_(\d{4})_(\d{4})_MNT_LAMB93_IGN69\.asc$", re.I)
TILE_SIZE_M = 5000.0


@click.command()
@click.option("--archive", default="", type=click.Path(exists=False),
              help="Path to the RGE ALTI D005 5m .7z archive. Downloads if missing.")
@click.option("--download", is_flag=True,
              help="Force download/re-download of the official D005 5m archive.")
@click.option("--analysis-resolution", default=50.0, show_default=True,
              help="Output resolution in meters for the geometry-only Pass-1 map.")
@click.option("--wind-dir", "wind_from_deg", default=320.0, show_default=True,
              help="Wind FROM direction (meteorological degrees, 0=N).")
@click.option("--wind-speed", "wind_speed_ms", default=8.0, show_default=True,
              help="Wind speed in m/s, used for the map title only.")
@click.option("--save", "save_path", default="outputs/champsaur/champsaur_pass1_geometry_ign50m.png",
              show_default=True, help="PNG output path.")
def main(archive, download, analysis_resolution, wind_from_deg, wind_speed_ms, save_path):
    """Build the Champsaur Pass-1 geometry map from official IGN RGE ALTI 5m data."""
    cfg = load_config()
    base_dir = cfg.cache_dir / "champsaur" / "ign"
    base_dir.mkdir(parents=True, exist_ok=True)

    archive_path = Path(archive) if archive else base_dir / f"{RGEALTI_D005_5M_TITLE}.7z"
    if download or not archive_path.exists():
        click.echo(f"[1/6] Downloading official RGE ALTI D005 5m archive -> {archive_path}")
        download_file(RGEALTI_D005_5M_URL, archive_path)
    else:
        click.echo(f"[1/6] Reusing archive: {archive_path}")

    click.echo("[2/6] Selecting Champsaur ASC tiles from archive ...")
    bounds_l93 = area_bounds_l93(CHAMPSAUR)
    selected = select_tiles(archive_path, bounds_l93)
    if not selected:
        raise SystemExit("No RGE ALTI tiles intersect the Champsaur bounds.")
    click.echo(f"      selected {len(selected)} tiles")

    extract_dir = base_dir / "extracted_champsaur_5m"
    click.echo(f"[3/6] Extracting selected tiles -> {extract_dir}")
    extract_tiles(archive_path, selected, extract_dir)

    asc_paths = sorted(extract_dir.rglob("*.asc"))
    crop_l93 = base_dir / "champsaur_rgealti_5m_l93.tif"
    click.echo(f"[4/6] Mosaicking/cropping 5m Lambert-93 DEM -> {crop_l93}")
    mosaic_crop_l93(asc_paths, crop_l93, bounds_l93)

    analysis_utm = base_dir / f"champsaur_rgealti_{int(analysis_resolution)}m_utm.tif"
    click.echo(f"[5/6] Reprojecting/downsampling to UTM {analysis_resolution:.0f}m -> {analysis_utm}")
    reproject_to_utm(crop_l93, analysis_utm, resolution_m=analysis_resolution)

    click.echo("[6/6] Computing geometry-only indicator and rendering map ...")
    dem = load_dem(str(analysis_utm), max_domain_km=cfg.max_domain_km)
    prepared = base_dir / f"champsaur_rgealti_{int(analysis_resolution)}m_prepared_utm.tif"
    write_dem(dem, prepared)
    hazard = ind.hazard_indicator(dem, wind_from_deg, speed_grid=None)
    candidates = ind.find_candidates(dem, hazard, n=10)
    for i, c in enumerate(candidates, start=1):
        click.echo(f"      #{i:02d} x={c.x:.0f} y={c.y:.0f} score={c.score:.2f}")

    from sillage.viz.map2d import show_static

    fig = show_static(
        dem,
        hazard,
        title=(
            f"Sillage Pass-1 Champsaur IGN RGE ALTI {analysis_resolution:.0f}m - "
            f"wind {wind_speed_ms:.0f} m/s from {wind_from_deg:.0f} deg"
        ),
    )
    out = resolve_output_path(save_path, cfg)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    ex, ey = dem.extent_km
    click.echo(f"      map saved: {out}")
    click.echo(
        f"      analysis DEM: {dem.shape}, res {dem.resolution_m:.1f} m, "
        f"domain {ex:.1f} x {ey:.1f} km, CRS {dem.crs.to_string()}"
    )
    click.echo(f"      prepared DEM: {prepared}")


def download_file(url: str, path: Path, chunk_size: int = 1024 * 1024) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with requests.get(url, headers={"User-Agent": "Sillage/0.1"}, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", "0") or 0)
        done = 0
        with tmp.open("wb") as f:
            for chunk in r.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if total and done // (50 * chunk_size) != (done - len(chunk)) // (50 * chunk_size):
                    click.echo(f"      downloaded {done / 1_000_000:.0f}/{total / 1_000_000:.0f} MB")
    tmp.replace(path)


def area_bounds_l93(area: StudyArea) -> tuple[float, float, float, float]:
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:4326", LAMB93, always_xy=True)
    pts = [
        tr.transform(area.west, area.south),
        tr.transform(area.west, area.north),
        tr.transform(area.east, area.south),
        tr.transform(area.east, area.north),
    ]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def tile_bounds_from_name(name: str) -> tuple[float, float, float, float] | None:
    m = TILE_RE.search(Path(name).name)
    if not m:
        return None
    x0 = float(int(m.group(1)) * 1000)
    y0 = float(int(m.group(2)) * 1000)
    return x0, y0, x0 + TILE_SIZE_M, y0 + TILE_SIZE_M


def intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def select_tiles(archive_path: Path, bounds_l93: tuple[float, float, float, float]) -> list[str]:
    with py7zr.SevenZipFile(archive_path, "r") as z:
        names = z.getnames()
    selected = []
    for name in names:
        tb = tile_bounds_from_name(name)
        if tb and intersects(tb, bounds_l93):
            selected.append(name)
    return selected


def extract_tiles(archive_path: Path, selected: list[str], extract_dir: Path) -> None:
    extract_dir.mkdir(parents=True, exist_ok=True)
    existing = {p.name for p in extract_dir.rglob("*.asc")}
    wanted_names = {Path(s).name for s in selected}
    if wanted_names.issubset(existing):
        click.echo("      selected tiles already extracted")
        return
    with py7zr.SevenZipFile(archive_path, "r") as z:
        z.extract(path=extract_dir, targets=selected)


def mosaic_crop_l93(
    asc_paths: list[Path],
    out_path: Path,
    bounds_l93: tuple[float, float, float, float],
) -> Path:
    import rasterio
    from rasterio.merge import merge

    srcs = [rasterio.open(p) for p in asc_paths]
    try:
        nodata = srcs[0].nodata
        mosaic, transform = merge(srcs, bounds=bounds_l93, nodata=nodata)
        profile = srcs[0].profile.copy()
        profile.update(
            driver="GTiff",
            height=mosaic.shape[1],
            width=mosaic.shape[2],
            count=1,
            crs=LAMB93,
            transform=transform,
            nodata=nodata,
            dtype=str(mosaic.dtype),
            compress="deflate",
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(mosaic[0], 1)
    finally:
        for src in srcs:
            src.close()
    return out_path


def reproject_to_utm(src_path: Path, out_path: Path, resolution_m: float = 50.0) -> Path:
    import rasterio
    from rasterio.crs import CRS
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    with rasterio.open(src_path) as src:
        dst_crs = CRS.from_epsg(32632)
        transform, width, height = calculate_default_transform(
            src.crs,
            dst_crs,
            src.width,
            src.height,
            *src.bounds,
            resolution=resolution_m,
        )
        profile = src.profile.copy()
        profile.update(
            driver="GTiff",
            crs=dst_crs,
            transform=transform,
            width=width,
            height=height,
            count=1,
            dtype="float32",
            compress="deflate",
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(out_path, "w", **profile) as dst:
            reproject(
                source=rasterio.band(src, 1),
                destination=rasterio.band(dst, 1),
                src_transform=src.transform,
                src_crs=src.crs,
                src_nodata=src.nodata,
                dst_transform=transform,
                dst_crs=dst_crs,
                dst_nodata=src.nodata,
                resampling=Resampling.bilinear,
            )
    return out_path


if __name__ == "__main__":
    main()



