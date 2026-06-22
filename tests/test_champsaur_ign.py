"""Tests for the Champsaur IGN RGE ALTI preparation helpers."""

from __future__ import annotations

from scripts.prepare_champsaur_ign import intersects, tile_bounds_from_name


def test_tile_bounds_from_rgealti_name():
    name = "RGEALTI_FXX_0935_6385_MNT_LAMB93_IGN69.asc"
    assert tile_bounds_from_name(name) == (935000.0, 6385000.0, 940000.0, 6390000.0)


def test_intersects_rectangles():
    assert intersects((0, 0, 10, 10), (5, 5, 15, 15))
    assert not intersects((0, 0, 10, 10), (10, 10, 20, 20))
