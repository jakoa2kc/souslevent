"""Centralized configuration: paths, external-tool locations, and constants.

Reads from environment (.env). Keep *all* environment access here so the rest of the
codebase stays pure and testable. See docs/support/environment.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _get(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration."""

    # External WindNinja tooling
    windninja_cli: str
    windninja_data: str | None

    # Caches / working dirs
    cache_dir: Path

    # Optional API keys
    meteofrance_api_key: str | None

    # --- Project-wide constants (do not vary at runtime) ---
    # WindNinja recommends DEM domains below ~50 km on a side.
    max_domain_km: float = 50.0
    # Default coarse computational resolution for Pass 1 (meters).
    pass1_resolution_m: float = 50.0
    # Default fine computational resolution for Pass 2 (meters).
    pass2_resolution_m: float = 20.0
    # Empirical downwind extent of the disturbed lee zone, in relief-heights.
    lee_extent_in_heights: float = 6.0  # ~5-7 x H rule of thumb


def load_config() -> Config:
    """Build a Config from the environment (after .env is loaded by the caller/app)."""
    cache = Path(_get("SILLAGE_CACHE_DIR", "./cache")).expanduser().resolve()
    cache.mkdir(parents=True, exist_ok=True)
    return Config(
        windninja_cli=_get("WINDNINJA_CLI", "WindNinja_cli"),
        windninja_data=_get("WINDNINJA_DATA"),
        cache_dir=cache,
        meteofrance_api_key=_get("METEOFRANCE_API_KEY") or None,
    )
