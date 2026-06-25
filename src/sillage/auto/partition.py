"""Relief-aware subdivision of a flight zone into Pass-2 momentum sub-domains.

The automatic mode runs the momentum solver over the WHOLE zone, not one hand-picked feature.
To keep each solve valid and affordable we tile the zone with a **relief-adaptive quadtree**,
splitting a tile while either:

  * its estimated WindNinja mesh size exceeds ``max_cells`` (cost / memory budget), or
  * its **relief span** (max-min elevation) exceeds ``max_relief_m`` — so that ONE
    upstream-constant wind stays a sound boundary condition for that sub-domain (the Pass-2
    assumption, ADR-0003).

Splitting stops at ``min_tile_m`` so a very steep patch doesn't recurse to dust. The result is
fine tiling where the terrain is busy, coarse where it's flat. Pure function over the DEM array
(no IO) → unit-testable. See docs/10_auto_pipeline.md / ADR-0022.
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
