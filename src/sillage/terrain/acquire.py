"""Prepare a coarse DEM for an arbitrary AOI (worldwide, key-free) for Pass-1.

Pass-1 is a coarse screening (candidate lee zones at ~50-100 m), so a ~90 m DEM over the
whole flight zone is plenty — fine terrain is only fetched per feature for Pass-2. Elevation
comes from the worldwide **terrarium** tiles (AWS), mosaicked by contextily, decoded to
metres, and reprojected to UTM north-up via terrain.dem. No API key needed.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

TERRARIUM = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
_MERC_M_PER_PX_Z0 = 156543.03  # web-mercator ground resolution at the equator, zoom 0


def zoom_for_resolution(center_lat: float, target_res_m: float, merc_width_m: float,
                        max_px: int = 2500) -> int:
    """Pick a tile zoom for ~``target_res_m`` ground resolution, capped so the mosaic stays
    below ``max_px`` on a side (so a huge AOI degrades to a coarser DEM, not a giant fetch)."""
    base = _MERC_M_PER_PX_Z0 * max(0.05, math.cos(math.radians(center_lat)))
    z = int(round(math.log2(base / max(target_res_m, 1.0))))
    z = max(1, min(13, z))
    while z > 1 and merc_width_m / (base / 2 ** z) > max_px:
        z -= 1
    return z


def decode_terrarium(img: np.ndarray) -> np.ndarray:
    """Decode a terrarium RGB(A) tile mosaic to elevation in metres."""
    r = img[:, :, 0].astype("float64")
    g = img[:, :, 1].astype("float64")
    b = img[:, :, 2].astype("float64")
    return ((r * 256.0 + g + b / 256.0) - 32768.0).astype("float32")


def _crop_to_bbox(elev, ext, bbox_lonlat):
    """Crop a web-mercator (elev array, extent) to the lon/lat bbox (west, south, east, north).

    bounds2img returns tile-aligned mosaics that often far exceed the requested area, so two
    nearby selections would otherwise share most of the same DEM. Cropping makes the DEM match
    the selected zone (distinct zones -> distinct DEMs; keeps it under ~50 km).
    """
    from rasterio.crs import CRS
    from rasterio.warp import transform as warp_xy

    west, south, east, north = bbox_lonlat
    xs, ys = warp_xy(CRS.from_epsg(4326), CRS.from_epsg(3857), [west, east], [south, north])
    bx0, bx1 = sorted(xs)
    by0, by1 = sorted(ys)
    xmin, xmax, ymin, ymax = ext
    h, w = elev.shape
    px, py = (xmax - xmin) / w, (ymax - ymin) / h
    c0 = max(0, int((bx0 - xmin) / px))
    c1 = min(w, int(math.ceil((bx1 - xmin) / px)))
    r0 = max(0, int((ymax - by1) / py))  # row 0 = top (ymax)
    r1 = min(h, int(math.ceil((ymax - by0) / py)))
    if c1 - c0 < 2 or r1 - r0 < 2:
        return elev, ext
    sub = np.ascontiguousarray(elev[r0:r1, c0:c1])
    new_ext = (xmin + c0 * px, xmin + c1 * px, ymax - r1 * py, ymax - r0 * py)
    return sub, new_ext


def prepare_dem_for_bbox(
    bbox_latlon, out_path, target_res_m: float = 50.0,
    on_progress=None, cancel=None, max_px: int = 3000,
) -> Path:
    """Prepare a UTM north-up DEM GeoTIFF for ``bbox_latlon`` = (south, west, north, east).

    Returns the written path. ``on_progress(pct, msg)`` / ``cancel()`` let the IHM worker
    report progress and cancel (the tile fetch is the slow part).
    """
    import contextily as cx
    import rasterio
    from rasterio.crs import CRS
    from rasterio.transform import from_origin

    from .dem import load_dem, write_dem

    south, west, north, east = bbox_latlon
    if not (north > south and east > west):
        raise ValueError("AOI invalide (bornes lat/lon).")

    def prog(p, m):
        if on_progress is not None:
            on_progress(int(p), m)

    if cancel is not None and cancel():
        raise RuntimeError("cancelled")

    center_lat = (south + north) / 2.0
    merc_w = (east - west) * 111320.0 * max(0.05, math.cos(math.radians(center_lat)))
    zoom = zoom_for_resolution(center_lat, target_res_m, merc_w, max_px)

    prog(8, f"Téléchargement de l'altimétrie (zoom {zoom})…")
    img, ext = cx.bounds2img(west, south, east, north, zoom=zoom, source=TERRARIUM, ll=True)
    if cancel is not None and cancel():
        raise RuntimeError("cancelled")

    prog(60, "Décodage de l'altimétrie…")
    elev = decode_terrarium(img)
    elev, ext = _crop_to_bbox(elev, ext, (west, south, east, north))  # match the exact AOI
    h, w = elev.shape
    xmin, xmax, ymin, ymax = ext  # web-mercator extent (left, right, bottom, top)
    transform = from_origin(xmin, ymax, (xmax - xmin) / w, (ymax - ymin) / h)

    prog(82, "Reprojection en UTM…")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.stem + "_merc3857.tif")
    profile = dict(driver="GTiff", height=h, width=w, count=1, dtype="float32",
                   crs=CRS.from_epsg(3857), transform=transform, nodata=np.nan)
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(elev, 1)
    dem = load_dem(str(tmp), max_domain_km=200.0)  # Pass-1 itself warns if > ~50 km
    write_dem(dem, out_path)
    try:
        tmp.unlink()
    except OSError:
        pass

    prog(100, "MNT prêt.")
    return out_path
