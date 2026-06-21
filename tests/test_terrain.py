"""Tests for terrain morphometry on a synthetic DEM (no files, no network).

We build a small synthetic ridge directly as a Dem object, bypassing rasterio IO, to test
the pure-array geometry functions deterministically.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("scipy")
from rasterio.transform import from_origin  # noqa: E402
from rasterio.crs import CRS  # noqa: E402

from sillage.terrain.dem import Dem  # noqa: E402
from sillage.terrain import geometry as geom  # noqa: E402


def _synthetic_ridge(n: int = 64, res_m: float = 30.0) -> Dem:
    """A N-S ridge: elevation peaks along a central E-W line, falling off east and west."""
    x = np.linspace(-1, 1, n)
    ridge = np.exp(-(x**2) / 0.05) * 500.0  # gaussian ridge across columns
    z = np.tile(ridge, (n, 1))  # invariant along rows (N-S)
    transform = from_origin(600000.0, 4900000.0, res_m, res_m)  # north-up
    return Dem(elevation=z.astype("float32"), transform=transform,
               crs=CRS.from_epsg(32631), resolution_m=res_m)


def test_slope_aspect_shapes_and_ranges():
    dem = _synthetic_ridge()
    slope, aspect = geom.slope_aspect(dem)
    assert slope.shape == dem.shape == aspect.shape
    assert np.all(slope >= 0) and np.all(slope <= 90)
    assert np.all((aspect >= 0) & (aspect < 360.0))


def test_lee_exposure_is_high_on_sheltered_side():
    dem = _synthetic_ridge()
    _, aspect = geom.slope_aspect(dem)
    # Wind FROM the west (270). The eastern flank of the ridge is leeward.
    lee = geom.lee_exposure(aspect, wind_from_deg=270.0)
    n = dem.shape[1]
    west_flank = lee[:, : n // 2 - 2].mean()
    east_flank = lee[:, n // 2 + 2 :].mean()
    assert east_flank > west_flank  # leeward (east) more exposed-away than windward (west)


def test_ridge_mask_flags_the_crest():
    dem = _synthetic_ridge()
    ridges = geom.ridge_mask(dem)
    n = dem.shape[1]
    center_band = ridges[:, n // 2 - 2 : n // 2 + 2].mean()
    edge_band = ridges[:, :4].mean()
    assert center_band > edge_band


def test_winstral_shelter_responds_to_direction():
    dem = _synthetic_ridge()
    # A point just east (leeward for west wind) should be more sheltered from the west
    # than from the east.
    shelter_w = geom.winstral_shelter(dem, wind_from_deg=270.0, search_distance_m=300.0)
    shelter_e = geom.winstral_shelter(dem, wind_from_deg=90.0, search_distance_m=300.0)
    n = dem.shape[1]
    east_point = (slice(None), slice(n // 2 + 3, n // 2 + 6))
    assert shelter_w[east_point].mean() > shelter_e[east_point].mean()
