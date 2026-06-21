"""Terrain morphometry: slope, aspect, ridge detection, and the Winstral shelter index.

Pure array operations on a loaded DEM. No solver, no network. These feed the Pass-1
screening indicator (screening/indicator.py) and the cheap pre-filter that flags
candidates before any WindNinja run.

Wind direction is the METEOROLOGICAL convention throughout: the direction the wind comes
FROM, in degrees, 0 deg = North, increasing clockwise.
"""

from __future__ import annotations

import numpy as np

from .dem import Dem


def slope_aspect(dem: Dem) -> tuple[np.ndarray, np.ndarray]:
    """Return (slope_deg, aspect_deg).

    slope_deg : steepest descent angle from horizontal, degrees.
    aspect_deg : downslope azimuth, degrees, 0 deg = North, clockwise (compass aspect).
    """
    z = dem.elevation.astype("float64")
    res = dem.resolution_m
    # gradient: axis 0 is +y == North-up rows -> note sign for north
    dzdy, dzdx = np.gradient(z, res, res)
    # dzdy from np.gradient increases with row index (southward for north-up); flip to north
    dzdy_north = -dzdy

    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy_north)))

    # Aspect: direction of steepest *descent*. Downslope vector = -gradient.
    # Compass azimuth (0=N, CW) from the descent vector (east=dx, north=dy).
    aspect = np.degrees(np.arctan2(-dzdx, -dzdy_north))  # atan2(east, north) -> from N, CW
    aspect = np.mod(aspect, 360.0)
    return slope, aspect


def lee_exposure(aspect_deg: np.ndarray, wind_from_deg: float) -> np.ndarray:
    """How much a slope faces *away* from the wind (i.e. is leeward), in [0, 1].

    The wind comes FROM `wind_from_deg`, so it blows TOWARD (wind_from_deg + 180).
    A slope is most leeward when its downslope aspect points the same way the wind blows
    (the slope drops away on the sheltered side). Returns 1 for fully leeward, 0 windward.
    """
    blow_toward = np.mod(wind_from_deg + 180.0, 360.0)
    delta = np.deg2rad(aspect_deg - blow_toward)
    # cos(delta) = +1 when slope faces downwind (leeward). Map [-1,1] -> [0,1].
    return 0.5 * (1.0 + np.cos(delta))


def ridge_mask(dem: Dem, smooth_px: int = 2, threshold: float = 0.0) -> np.ndarray:
    """Boolean mask of crest/ridge cells via curvature (Laplacian) of a smoothed DEM.

    Ridges are convex-up: negative Laplacian. This is a simple, fast detector adequate
    for screening; replace with a directional-relief method later if needed.
    """
    from scipy.ndimage import gaussian_filter, laplace

    z = dem.elevation.astype("float64")
    zs = gaussian_filter(z, sigma=max(smooth_px, 0.5))
    lap = laplace(zs)
    # convex-up (ridge) => lap < -threshold
    return lap < -abs(threshold)


def winstral_shelter(
    dem: Dem,
    wind_from_deg: float,
    search_distance_m: float = 300.0,
    n_samples: int = 20,
) -> np.ndarray:
    """Winstral-style shelter index: max upwind slope within a search distance.

    For each cell, look UPWIND (toward `wind_from_deg`) up to `search_distance_m` and
    return the maximum upward slope angle (degrees) of terrain blocking the wind. High
    values = sheltered; ~0 = exposed. This is a NO-SOLVER pre-filter for candidate lee
    zones (docs/01_theory_and_physics.md).

    Notes
    -----
    O(n_samples) per cell, vectorized over the grid. Good enough for screening
    resolutions. For production, consider the published maxus algorithm.
    """
    from scipy.ndimage import map_coordinates

    z = dem.elevation.astype("float64")
    res = dem.resolution_m
    h, w = z.shape

    # Unit vector pointing UPWIND (toward the source of the wind), in grid space.
    # Compass: 0=N (up, -row), 90=E (+col). Upwind direction = wind_from_deg.
    theta = np.deg2rad(wind_from_deg)
    dcol = np.sin(theta)   # east component -> +col
    drow = -np.cos(theta)  # north component -> -row (north is up)

    rows, cols = np.mgrid[0:h, 0:w].astype("float64")
    max_slope = np.zeros((h, w), dtype="float64")

    step_m = search_distance_m / n_samples
    for k in range(1, n_samples + 1):
        dist = k * step_m
        rr = rows + drow * (dist / res)
        cc = cols + dcol * (dist / res)
        sampled = map_coordinates(z, [rr, cc], order=1, mode="nearest")
        # upward slope from the cell to the sampled upwind point
        slope_k = np.degrees(np.arctan2(sampled - z, dist))
        max_slope = np.maximum(max_slope, slope_k)

    return np.clip(max_slope, 0.0, None)
