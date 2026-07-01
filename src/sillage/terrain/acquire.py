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

# IGN RGE ALTI elevation (real 1-5 m in France) via the Géoplateforme WMS (BIL float32,
# key-free, clipped to a bbox). Much finer than the worldwide ~30 m terrarium source.
IGN_ELEV_WMS = "https://data.geopf.fr/wms-r/wms"
IGN_ELEV_LAYER = "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES"


def bbox_latlon_from_utm_window(crs, x: float, y: float, half_m: float):
    """(south, west, north, east) lat/lon for a square window of half-size ``half_m`` centred
    at (x, y) in ``crs`` (a projected/UTM CRS). Used to re-fetch a fine DEM for a Pass-2 crop."""
    from rasterio.crs import CRS as RCRS
    from rasterio.warp import transform as warp_xy

    lons, lats = warp_xy(crs, RCRS.from_epsg(4326),
                         [x - half_m, x + half_m], [y - half_m, y + half_m])
    return (min(lats), min(lons), max(lats), max(lons))


def in_france(bbox_latlon) -> bool:
    """Rough test: is the AOI centre over mainland France + Corsica (IGN RGE ALTI cover)?"""
    south, west, north, east = bbox_latlon
    clat, clon = (south + north) / 2.0, (west + east) / 2.0
    return 41.3 <= clat <= 51.2 and -5.2 <= clon <= 9.6


IGN_NATIVE_M = 1.0  # RGE ALTI HIGHRES true native ~1 m; coarser requests stair-step (the WMS
#                     nearest-neighbour downsamples 1 m -> target), so fetch ~native and average


def _ign_bil(south, west, north, east, w, h, timeout=90):
    """One IGN WMS GetMap BIL tile -> float32 (h, w) array."""
    import requests

    params = {
        "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "LAYERS": IGN_ELEV_LAYER,
        "CRS": "EPSG:4326", "BBOX": f"{south},{west},{north},{east}",  # 1.3.0: lat,lon order
        "WIDTH": str(w), "HEIGHT": str(h), "FORMAT": "image/x-bil;bits=32", "STYLES": "",
    }
    r = requests.get(IGN_ELEV_WMS, params=params, timeout=timeout)
    r.raise_for_status()
    if "bil" not in r.headers.get("Content-Type", "") or len(r.content) != w * h * 4:
        raise RuntimeError(
            f"réponse IGN inattendue ({r.headers.get('Content-Type')}, {len(r.content)} o)")
    return np.frombuffer(r.content, dtype="<f4").astype("float32").reshape(h, w)


def _fetch_ign_tiles(jobs, tile_w, tile_h, tx, ty, on_done=None, cancel=None, max_workers=6):
    """Fetch IGN tiles CONCURRENTLY (independent network requests) and stitch them into one
    array. ``jobs`` = ``[(j, i, south, west, north, east), ...]`` (row-block ``j``, column ``i``).
    ``on_done(done_count, total)`` reports progress as tiles arrive. The big win for fine fetches
    (a 5 m target pulls many ~1 m-native tiles)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    n = len(jobs)
    grid: dict[tuple[int, int], np.ndarray] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, min(n, max_workers))) as ex:
        futs = {ex.submit(_ign_bil, ts, tw, tn, te, tile_w, tile_h): (j, i)
                for (j, i, ts, tw, tn, te) in jobs}
        try:
            for fut in as_completed(futs):
                if cancel is not None and cancel():
                    raise RuntimeError("cancelled")
                grid[futs[fut]] = fut.result()  # raises on a failed tile
                done += 1
                if on_done is not None:
                    on_done(done, n)
        except BaseException:
            for f in futs:
                f.cancel()
            raise
    return np.vstack([np.hstack([grid[(j, i)] for i in range(tx)]) for j in range(ty)])


def _block_average(arr: np.ndarray, factor: int) -> np.ndarray:
    """Mean-pool by an integer factor (NaN-aware), trimming any remainder."""
    if factor <= 1:
        return arr
    h = (arr.shape[0] // factor) * factor
    w = (arr.shape[1] // factor) * factor
    blocks = arr[:h, :w].reshape(h // factor, factor, w // factor, factor)
    return np.nanmean(blocks, axis=(1, 3)).astype("float32")


def prepare_dem_ign(bbox_latlon, out_path, target_res_m: float = 50.0,
                    on_progress=None, cancel=None, max_px: int = 2048, tile_cap: int = 4) -> Path:
    """Prepare a UTM DEM from IGN RGE ALTI for ``bbox_latlon`` = (south, west, north, east).

    The Géoplateforme elevation WMS only returns artifact-free data when fetched close to native
    resolution (``IGN_NATIVE_M``; observed ~1 m on HIGHRES). Coarser direct WMS requests can stripe
    vertically — duplicated rows that show as "steps" in the hillshade and propagate to WindNinja.
    So we fetch close to native / ``target_res_m / 5`` (tiled if needed, ``tile_cap`` per axis), then
    **block-average** down to ``target_res_m`` when averaging is actually useful.
    Reprojected to UTM north-up. Key-free. Raises if outside IGN coverage (dispatcher falls
    back to the worldwide source).
    """
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

    clat = (south + north) / 2.0
    w_m = (east - west) * 111320.0 * max(0.05, math.cos(math.radians(clat)))
    h_m = (north - south) * 111320.0
    # Fetch at ~target/5 (floored at the ~1 m native) and average ×5 ourselves: the WMS's own
    # nearest-neighbour downsample-to-target leaves horizontal "stair-step" striping that only a
    # ~×5 average removes (observed: clean from 25 m = 5 m fetch ×5; striped at 5/10 m when the
    # factor was <5). At 1 m we keep the native fetch instead of forcing a fake 2 m average.
    fetch_res = max(IGN_NATIVE_M, target_res_m / 5.0)
    nat_w = max(2, round(w_m / fetch_res))
    nat_h = max(2, round(h_m / fetch_res))
    tx = min(tile_cap, max(1, math.ceil(nat_w / max_px)))
    ty = min(tile_cap, max(1, math.ceil(nat_h / max_px)))
    tile_w = min(nat_w, tx * max_px) // tx  # ~fetch_res unless capped (very large zones only)
    tile_h = min(nat_h, ty * max_px) // ty

    prog(8, f"Téléchargement IGN RGE ALTI ~{fetch_res:.1f} m ({tx}×{ty} tuiles)…")
    jobs = []
    for j in range(ty):
        tn = north - (north - south) * j / ty
        ts = north - (north - south) * (j + 1) / ty
        for i in range(tx):
            tw = west + (east - west) * i / tx
            te = west + (east - west) * (i + 1) / tx
            jobs.append((j, i, ts, tw, tn, te))

    arr = _fetch_ign_tiles(
        jobs, tile_w, tile_h, tx, ty, cancel=cancel,
        on_done=lambda d, total: prog(8 + d / total * 50.0, f"IGN tuile {d}/{total}…"))
    arr[(arr < -400.0) | (arr > 9000.0)] = np.nan  # mask nodata / out-of-range
    if not np.isfinite(arr).any():
        raise RuntimeError("zone hors couverture IGN RGE ALTI")

    prog(62, "Lissage vers la résolution cible…")
    cur_res = w_m / arr.shape[1]
    avg_factor = max(1, round(target_res_m / cur_res))
    if avg_factor > 1:
        arr = _block_average(arr, avg_factor)
    h, w = arr.shape
    transform = from_origin(west, north, (east - west) / w, (north - south) / h)

    prog(82, "Reprojection en UTM…")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.stem + "_wgs84.tif")
    profile = dict(driver="GTiff", height=h, width=w, count=1, dtype="float32",
                   crs=CRS.from_epsg(4326), transform=transform, nodata=np.nan)
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(arr, 1)
    dem = load_dem(str(tmp), max_domain_km=200.0)
    write_dem(dem, out_path)
    try:
        tmp.unlink()
    except OSError:
        pass

    prog(100, "MNT IGN prêt.")
    return out_path


def prepare_dem(bbox_latlon, out_path, target_res_m: float = 50.0, source: str = "auto",
                on_progress=None, cancel=None):
    """Prepare a DEM, picking the source: "ign" (France RGE ALTI), "world" (terrarium), or
    "auto" (IGN over France, terrarium elsewhere). Returns (path, used_label). Falls back to
    the worldwide source if IGN fails/empty. ADR-0014."""
    want_ign = source == "ign" or (source == "auto" and in_france(bbox_latlon))
    if want_ign:
        try:
            return prepare_dem_ign(bbox_latlon, out_path, target_res_m, on_progress, cancel), "IGN"
        except Exception as exc:
            if str(exc).lower() == "cancelled" or source == "ign":
                raise
            if on_progress is not None:
                on_progress(
                    5,
                    f"IGN indisponible ({type(exc).__name__}: {exc}); fallback monde.",
                )
    return prepare_dem_for_bbox(bbox_latlon, out_path, target_res_m, on_progress, cancel), "Monde"


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
