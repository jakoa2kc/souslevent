"""Pass-1 derived hazard indicator: WHERE is disturbed lee air likely (candidates).

CRITICAL framing: the mass solver CANNOT show rotors. This module does NOT pretend to.
It estimates the LIKELIHOOD/SEVERITY of disturbed lee air by combining signals the mass
field can legitimately give, with terrain geometry and empirical rules, then emits ranked
CANDIDATE features for the momentum solver (Pass 2). See docs/01 and docs/03 (ADR-0003).

Ingredients (each normalized to [0, 1], then weighted):
  1. terrain geometry  : lee exposure (slope facing away from wind) x steepness, on ridges
  2. mass-field signal : downwind velocity DEFICIT (a wake leaves a low-speed shadow)
  3. empirical rule    : ratio crest-wind / obstacle-height influence (placeholder hook)

Output: an indicator grid (same shape as the DEM screening grid) and a candidate list.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..terrain.dem import Dem
from ..terrain import geometry as geom


@dataclass
class Candidate:
    """A (location, score) flagged for a Pass-2 momentum run."""

    row: int
    col: int
    x: float  # CRS meters
    y: float  # CRS meters
    score: float


def _normalize(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype="float64")
    finite = np.isfinite(a)
    if not finite.any():
        return np.zeros_like(a)
    lo, hi = np.nanpercentile(a[finite], [2, 98])
    if hi <= lo:
        return np.zeros_like(a)
    return np.clip((a - lo) / (hi - lo), 0.0, 1.0)


def velocity_deficit(speed: np.ndarray, reference: float | None = None) -> np.ndarray:
    """Downwind speed deficit relative to a reference (free-stream) speed, in [0, 1].

    `speed` is the Pass-1 surface wind speed magnitude (from WindNinja u,v ASCII output).
    Reference defaults to a high percentile (≈ exposed free-stream). High deficit = wake
    shadow = candidate lee zone. NOTE: the mass solver shows low speed (not reversal) in
    eddies, so deficit is the right signal here.
    """
    speed = np.asarray(speed, dtype="float64")
    if reference is None:
        reference = float(np.nanpercentile(speed, 90))
    reference = max(reference, 1e-6)
    deficit = 1.0 - np.clip(speed / reference, 0.0, 1.0)
    return deficit


def geometric_hazard(dem: Dem, wind_from_deg: float) -> np.ndarray:
    """Terrain-only leeward hazard: lee exposure x steepness, emphasized on ridges.

    Pure DEM; no solver. Captures that separation forms where a steep slope drops away on
    the sheltered side of a crest, relative to the incoming wind direction.
    """
    slope, aspect = geom.slope_aspect(dem)
    lee = geom.lee_exposure(aspect, wind_from_deg)          # [0,1], leeward-ness
    steep = _normalize(slope)                                # [0,1]
    ridges = geom.ridge_mask(dem).astype("float64")          # {0,1}
    # weight ridges up but don't zero out off-ridge lee slopes
    ridge_weight = 0.5 + 0.5 * ridges
    return _normalize(lee * steep * ridge_weight)


def hazard_indicator(
    dem: Dem,
    wind_from_deg: float,
    speed_grid: np.ndarray | None = None,
    weights: tuple[float, float, float] = (0.5, 0.4, 0.1),
    use_shelter_prefilter: bool = True,
) -> np.ndarray:
    """Combine geometry + velocity deficit + empirical hook into one [0,1] indicator.

    Parameters
    ----------
    speed_grid : Pass-1 surface wind SPEED on the DEM grid (from WindNinja). If None,
        the velocity-deficit term is skipped (geometry-only screening still works).
    weights : (geometry, deficit, empirical) blend weights; renormalized internally.
    use_shelter_prefilter : multiply by a Winstral shelter factor so deeply sheltered AND
        deeply exposed cells are de-emphasized vs the transition zone where rotors live.

    Returns
    -------
    indicator : float array in [0, 1], same shape as dem.elevation.
    """
    g = geometric_hazard(dem, wind_from_deg)

    if speed_grid is not None:
        if speed_grid.shape != dem.shape:
            speed_grid = _resample_to(speed_grid, dem.shape)
        d = _normalize(velocity_deficit(speed_grid))
    else:
        d = np.zeros_like(g)

    # Empirical hook: placeholder uniform term; replace with a crest-wind/height ratio
    # field once ridge heights are estimated (roadmap M1/T5). Kept explicit, not hidden.
    e = np.zeros_like(g)

    wg, wd, we = weights
    s = wg + wd + we
    wg, wd, we = wg / s, wd / s, we / s
    indicator = wg * g + wd * d + we * e

    if use_shelter_prefilter:
        shelter = geom.winstral_shelter(dem, wind_from_deg)
        shelter_n = _normalize(shelter)
        # transition emphasis: peak where shelter is moderate (lee shoulder), not extreme
        transition = 1.0 - np.abs(2.0 * shelter_n - 1.0)
        indicator = indicator * (0.5 + 0.5 * transition)

    return np.clip(indicator, 0.0, 1.0)


def find_candidates(
    dem: Dem,
    indicator: np.ndarray,
    n: int = 10,
    min_separation_m: float = 250.0,
) -> list[Candidate]:
    """Pick the top-N local maxima of the indicator as Pass-2 candidates.

    Enforces a minimum spatial separation so candidates aren't clustered on one feature.
    """
    from scipy.ndimage import maximum_filter

    sep_px = max(int(min_separation_m / dem.resolution_m), 1)
    local_max = (indicator == maximum_filter(indicator, size=2 * sep_px + 1)) & (indicator > 0)
    rows, cols = np.where(local_max)
    scores = indicator[rows, cols]
    order = np.argsort(scores)[::-1][:n]

    out: list[Candidate] = []
    for i in order:
        r, c = int(rows[i]), int(cols[i])
        x, y = dem.transform * (c + 0.5, r + 0.5)
        out.append(Candidate(row=r, col=c, x=float(x), y=float(y), score=float(scores[i])))
    return out


def _resample_to(arr: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """Nearest-neighbour resample a 2D array to `shape` (screening-grade)."""
    from scipy.ndimage import zoom

    zy = shape[0] / arr.shape[0]
    zx = shape[1] / arr.shape[1]
    return zoom(arr, (zy, zx), order=1)
