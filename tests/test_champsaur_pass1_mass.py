"""Tests for Champsaur Pass-1 mass helpers."""

from __future__ import annotations

import numpy as np

from scripts.champsaur_pass1_mass import mask_edge_buffer


def test_mask_edge_buffer_zeros_only_border():
    indicator = np.ones((10, 10), dtype=float)
    masked = mask_edge_buffer(indicator, resolution_m=10.0, edge_buffer_m=20.0)
    assert masked[:2, :].sum() == 0
    assert masked[-2:, :].sum() == 0
    assert masked[:, :2].sum() == 0
    assert masked[:, -2:].sum() == 0
    assert masked[2:-2, 2:-2].min() == 1.0
