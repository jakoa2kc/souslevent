"""DEM loading, reprojection to UTM north-up, and validation for WindNinja.

WindNinja requires the DEM to be:
  * north-up in a *projected* CRS (best-fit UTM),
  * in METERS for BOTH horizontal and vertical units,
  * a domain below ~50 km on a side (recommended).

Violating any of these produces SILENT wrong results downstream (see
docs/support/troubleshooting.md). This module enforces them on load.

This is the shared terrain source for BOTH passes; Pass 2 crops a window from it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

try:
    import rasterio
    from rasterio.warp import calculate_default_transform, reproject, Resampling
    from rasterio.crs import CRS
    from rasterio.fill import fillnodata
    from rasterio.transform import array_bounds
except Exception as exc:  # pragma: no cover - dependency guard
    raise ImportError(
        "terrain.dem requires rasterio (GDAL-backed). See docs/support/environment.md."
    ) from exc


@dataclass
class Dem:
    """A validated, UTM north-up, meters DEM ready for WindNinja / morphometry."""

    elevation: np.ndarray  # 2D array, meters
    transform: "rasterio.Affine"  # affine geotransform (north-up)
    crs: "CRS"  # projected UTM CRS, meters
    resolution_m: float  # pixel size, meters (assumes square pixels)

    @property
    def shape(self) -> tuple[int, int]:
        return self.elevation.shape

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(left, bottom, right, top) in CRS units (meters)."""
        h, w = self.elevation.shape
        return array_bounds(h, w, self.transform)

    @property
    def extent_km(self) -> tuple[float, float]:
        left, bottom, right, top = self.bounds
        return (abs(right - left) / 1000.0, abs(top - bottom) / 1000.0)


def _best_fit_utm_epsg(lon: float, lat: float) -> int:
    """EPSG code of the best-fit UTM zone for a lon/lat (WGS84)."""
    zone = int((lon + 180.0) // 6.0) + 1
    return (32600 if lat >= 0 else 32700) + zone


def load_dem(
    path: str,
    target_epsg: int | None = None,
    fill_nodata: bool = True,
    max_domain_km: float = 50.0,
) -> Dem:
    """Load a DEM and return it reprojected to UTM north-up, in meters, validated.

    Parameters
    ----------
    path : str
        GeoTIFF (or any rasterio-readable) DEM.
    target_epsg : int, optional
        Force a specific projected EPSG (UTM). If None, auto-pick the best-fit UTM zone
        from the dataset center. Force this explicitly if the area straddles a zone edge.
    fill_nodata : bool
        Interpolate over no-data cells (WindNinja can also fill, but we be safe).
    max_domain_km : float
        Warn/raise if the reprojected domain exceeds this on a side.

    Notes
    -----
    Vertical units are assumed to be meters (true for IGN RGE ALTI and SRTM). There is no
    universal metadata tag for vertical units; if you ingest a source in feet, convert
    here. See docs/04_data_sources.md.
    """
    with rasterio.open(path) as src:
        src_crs = src.crs
        if src_crs is None:
            raise ValueError(
                f"DEM {path!r} has no CRS. WindNinja needs a georeferenced, north-up "
                "projected DEM. Assign/repair the CRS first (docs/04_data_sources.md)."
            )

        # Determine target UTM CRS
        if target_epsg is None:
            # dataset center in lon/lat to pick the UTM zone
            lon, lat = _dataset_center_lonlat(src)
            target_epsg = _best_fit_utm_epsg(lon, lat)
        dst_crs = CRS.from_epsg(target_epsg)

        dst_transform, dst_w, dst_h = calculate_default_transform(
            src_crs, dst_crs, src.width, src.height, *src.bounds
        )
        dst = np.empty((dst_h, dst_w), dtype="float32")
        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            src_transform=src.transform,
            src_crs=src_crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            resampling=Resampling.bilinear,
            dst_nodata=np.nan,
        )

    if fill_nodata:
        mask = np.isfinite(dst).astype("uint8")
        if mask.min() == 0:
            dst = fillnodata(dst, mask=mask, max_search_distance=100.0)

    # pixel size (meters); dst_transform.a is +x size, .e is -y size for north-up
    res_x = abs(dst_transform.a)
    res_y = abs(dst_transform.e)
    if not np.isclose(res_x, res_y, rtol=0.05):
        # WindNinja resamples internally, but flag strong anisotropy.
        import warnings

        warnings.warn(f"Non-square pixels ({res_x:.2f} x {res_y:.2f} m); using x.")
    resolution_m = float(res_x)

    dem = Dem(elevation=dst, transform=dst_transform, crs=dst_crs, resolution_m=resolution_m)
    _validate(dem, max_domain_km)
    return dem


def _dataset_center_lonlat(src) -> tuple[float, float]:
    """Center of a rasterio dataset as WGS84 lon/lat."""
    from rasterio.warp import transform as warp_xy

    left, bottom, right, top = src.bounds
    cx, cy = (left + right) / 2.0, (bottom + top) / 2.0
    lon, lat = warp_xy(src.crs, CRS.from_epsg(4326), [cx], [cy])
    return lon[0], lat[0]


def _validate(dem: Dem, max_domain_km: float) -> None:
    """Enforce WindNinja's hard requirements; raise/warn with actionable messages."""
    if not dem.crs.is_projected:
        raise ValueError(
            "DEM CRS is not projected. WindNinja needs north-up UTM in meters "
            "(docs/05_windninja_integration.md)."
        )
    ex, ey = dem.extent_km
    if ex > max_domain_km or ey > max_domain_km:
        import warnings

        warnings.warn(
            f"DEM domain {ex:.1f} x {ey:.1f} km exceeds the recommended "
            f"{max_domain_km:.0f} km. Consider a smaller area (docs/04)."
        )
    if not np.isfinite(dem.elevation).any():
        raise ValueError("DEM is entirely no-data after load.")
