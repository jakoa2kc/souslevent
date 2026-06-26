"""Feature-domain planning for the auto Pass-2 momentum solves.

The active auto pipeline screens the full route corridor with Pass-1, then places one bounded
momentum domain per high-hazard **feature** (`feature_domains`). The older relief-adaptive
quadtree (`partition_zone`) is kept as a pure/tested planning helper, but is no longer the main
auto strategy because independent grid tiles produced seam artifacts.

For the quadtree helper, a tile splits while either:

  * its estimated WindNinja mesh size exceeds ``max_cells`` (cost / memory budget), or
  * its **relief span** (max-min elevation) exceeds ``max_relief_m`` — so that ONE
    upstream-constant wind stays a sound boundary condition for that sub-domain (the Pass-2
    assumption, ADR-0003).

Splitting stops at ``min_tile_m`` so a very steep patch doesn't recurse to dust. The result is
fine tiling where the terrain is busy, coarse where it's flat. Pure function over the DEM array
All functions are pure over the DEM array (no IO) → unit-testable. See docs/10_auto_pipeline.md /
ADR-0022/0023.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..terrain.dem import Dem


@dataclass(frozen=True)
class SubZone:
    """One momentum sub-domain of the partitioned flight zone."""

    bbox: tuple[float, float, float, float]   # UTM left, bottom, right, top
    center: tuple[float, float]               # UTM x, y
    crest_alt_m: float                        # representative crest altitude (80th pct elevation)
    relief_m: float                           # elevation span max - min
    est_cells: int                            # estimated horizontal mesh cells at target_res
    pixel_window: tuple[int, int, int, int]   # r0, r1, c0, c1 in the parent DEM grid


def estimate_cells(width_m: float, height_m: float, target_res_m: float) -> int:
    """Horizontal mesh-cell estimate for a tile of this size at ``target_res_m``."""
    if target_res_m <= 0:
        return 0
    return int(round((width_m / target_res_m) * (height_m / target_res_m)))


def partition_zone(
    dem: Dem,
    *,
    target_res_m: float,
    max_cells: int = 600_000,
    max_relief_m: float = 400.0,
    min_tile_m: float = 600.0,
) -> list[SubZone]:
    """Tile ``dem`` into momentum sub-domains (relief-adaptive quadtree). Returns the leaves
    (a complete, non-overlapping cover of the DEM grid)."""
    elev = np.asarray(dem.elevation, dtype="float64")
    h_px, w_px = elev.shape
    res = float(dem.resolution_m)
    left, _bottom, _right, top = dem.bounds
    min_px = max(2, int(round(min_tile_m / res)))
    leaves: list[SubZone] = []

    def bbox_of(r0, r1, c0, c1):  # pixel window -> UTM (row 0 = north/top)
        return (left + c0 * res, top - r1 * res, left + c1 * res, top - r0 * res)

    def relief_of(sub):
        finite = sub[np.isfinite(sub)]
        return (float(finite.max()) - float(finite.min())) if finite.size else 0.0

    def emit(r0, r1, c0, c1):
        sub = elev[r0:r1, c0:c1]
        finite = sub[np.isfinite(sub)]
        bb = bbox_of(r0, r1, c0, c1)
        leaves.append(SubZone(
            bbox=bb,
            center=((bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0),
            crest_alt_m=float(np.nanpercentile(sub, 80)) if finite.size else 0.0,
            relief_m=relief_of(sub),
            est_cells=estimate_cells(bb[2] - bb[0], bb[3] - bb[1], target_res_m),
            pixel_window=(r0, r1, c0, c1),
        ))

    def rec(r0, r1, c0, c1):
        bb = bbox_of(r0, r1, c0, c1)
        cells = estimate_cells(bb[2] - bb[0], bb[3] - bb[1], target_res_m)
        too_big = cells > max_cells or relief_of(elev[r0:r1, c0:c1]) > max_relief_m
        can_split = (r1 - r0) >= 2 * min_px and (c1 - c0) >= 2 * min_px
        if too_big and can_split:
            rm, cm = (r0 + r1) // 2, (c0 + c1) // 2
            rec(r0, rm, c0, cm)
            rec(r0, rm, cm, c1)
            rec(rm, r1, c0, cm)
            rec(rm, r1, cm, c1)
        else:
            emit(r0, r1, c0, c1)

    if h_px and w_px:
        rec(0, h_px, 0, w_px)
    return leaves


def corridor_mask(dem: Dem, route_xy, margin_m: float) -> np.ndarray:
    """Boolean DEM-grid mask: pixels within ``margin_m`` of the flight route polyline
    (``route_xy`` = ``[(x, y), ...]`` in the DEM CRS). Restricts feature detection to a corridor
    around the planned route (the user won't overfly the whole bbox). Returns all-True if the
    route lands outside the DEM (don't mask)."""
    from scipy.ndimage import distance_transform_edt

    h_px, w_px = dem.shape
    res = float(dem.resolution_m)
    left, _bottom, _right, top = dem.bounds
    line = np.zeros((h_px, w_px), dtype=bool)

    def to_px(x, y):
        return int(round((top - y) / res)), int(round((x - left) / res))

    pts = [to_px(x, y) for x, y in route_xy]
    for (r0, c0), (r1, c1) in zip(pts, pts[1:]):  # rasterize each segment
        steps = max(abs(r1 - r0), abs(c1 - c0), 1)
        for t in range(steps + 1):
            r = int(round(r0 + (r1 - r0) * t / steps))
            c = int(round(c0 + (c1 - c0) * t / steps))
            if 0 <= r < h_px and 0 <= c < w_px:
                line[r, c] = True
    for r, c in pts:  # also mark the waypoints (covers a single-point route)
        if 0 <= r < h_px and 0 <= c < w_px:
            line[r, c] = True
    if not line.any():
        return np.ones((h_px, w_px), dtype=bool)
    return (distance_transform_edt(~line) * res) <= margin_m


def feature_domains(
    dem: Dem,
    hazard: np.ndarray,
    *,
    max_features: int = 12,
    min_separation_m: float = 1200.0,
    lee_factor: float = 5.0,
    min_half_m: float = 900.0,
    max_half_m: float = 2500.0,
    target_res_m: float = 10.0,
    relief_radius_m: float = 900.0,
) -> list[SubZone]:
    """One momentum domain per high-hazard **feature** (ridge/summit) — NOT a grid, so there are
    no internal seams to reconcile (ADR-0022). Each domain is centred on a Pass-1 candidate and
    half-sized to ``lee_factor × local relief / 2`` (clamped) so it contains the feature's full lee
    in any wind direction. Distinct, separated domains → independent, physically-valid solves.

    More, smaller, well-separated features parallelise better: NinjaFOAM/OpenFOAM scales poorly with
    threads, so many 1-thread solves beat a few many-thread ones. ``min_separation_m`` sets how close
    two candidates may be (lower ⇒ more features); ``max_features`` caps the count. The domain size is
    still floored by the **lee + outflow buffer** (physics), so it can't shrink to a thin corridor."""
    from ..screening import indicator as ind

    elev = np.asarray(dem.elevation, dtype="float64")
    h_px, w_px = elev.shape
    res = float(dem.resolution_m)
    left, _bottom, _right, top = dem.bounds
    rpx = max(1, int(round(relief_radius_m / res)))

    zones: list[SubZone] = []
    for c in ind.find_candidates(dem, hazard, n=max_features, min_separation_m=min_separation_m):
        loc = elev[max(0, c.row - rpx):c.row + rpx, max(0, c.col - rpx):c.col + rpx]
        fin = loc[np.isfinite(loc)]
        relief = (float(fin.max()) - float(fin.min())) if fin.size else 0.0
        half = float(min(max_half_m, max(min_half_m, lee_factor * relief / 2.0)))
        cx, cy = c.x, c.y
        bbox = (cx - half, cy - half, cx + half, cy + half)
        pr0, pr1 = max(0, int((top - (cy + half)) / res)), min(h_px, int((top - (cy - half)) / res))
        pc0, pc1 = max(0, int((cx - half - left) / res)), min(w_px, int((cx + half - left) / res))
        dsub = elev[pr0:pr1, pc0:pc1]
        dfin = dsub[np.isfinite(dsub)]
        zones.append(SubZone(
            bbox=bbox, center=(cx, cy),
            crest_alt_m=float(np.nanpercentile(dsub, 80)) if dfin.size else 0.0,
            relief_m=(float(dfin.max()) - float(dfin.min())) if dfin.size else relief,
            est_cells=estimate_cells(2 * half, 2 * half, target_res_m),
            pixel_window=(pr0, pr1, pc0, pc1),
        ))
    return zones
