"""Centralized configuration: paths, external-tool locations, and constants.

Reads from environment (.env). Keep *all* environment access here so the rest of the
codebase stays pure and testable. See docs/support/environment.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Project root = two levels up from src/sillage/config.py
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _user_config_dir() -> Path:
    """Stable per-user config dir (survives installs / PyInstaller exe): ``%APPDATA%\\SousLeVent``
    on Windows, ``~/.config/souslevent`` elsewhere."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "SousLeVent"
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "souslevent"


def _dotenv_candidates() -> list[Path]:
    """.env locations in priority order: next to a frozen exe, the project root (dev), the user dir."""
    import sys

    cands: list[Path] = []
    if getattr(sys, "frozen", False):  # PyInstaller / frozen build
        cands.append(Path(sys.executable).resolve().parent / ".env")
    cands.append(_PROJECT_ROOT / ".env")
    cands.append(_user_config_dir() / ".env")
    return cands


def _load_dotenv() -> None:
    """Load a ``.env`` into the environment, if python-dotenv is present.

    Searches, in priority order, next to a frozen exe, the project root (dev checkout), then the
    per-user config dir — so an installed app / PyInstaller build finds its config outside the
    (read-only, relocated) source tree. Kept optional so the package imports without the dependency.
    Real environment variables always win (``override=False``), so CI / shell exports take precedence.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for env_path in _dotenv_candidates():
        if env_path.is_file():
            load_dotenv(env_path, override=False)


def _get(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def _default_generated_root() -> Path:
    """Return the default out-of-tree workspace for generated artefacts."""
    if os.name == "nt":
        return Path(r"C:\A2K\SousLeVent")
    return _PROJECT_ROOT / ".generated"


def _resolve_dir(env_name: str, default: Path) -> Path:
    return Path(_get(env_name, str(default)) or str(default)).expanduser().resolve()


def _resolve_under(base: Path, path: str | Path, legacy_prefix: str) -> Path:
    """Resolve a generated path under ``base``, accepting old ``cache/...`` forms."""
    raw = Path(path).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    parts = raw.parts
    if parts and parts[0].lower() == legacy_prefix.lower():
        raw = Path(*parts[1:]) if len(parts) > 1 else Path()
    return (base / raw).resolve()


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration."""

    # External WindNinja tooling
    windninja_cli: str
    windninja_data: str | None

    # Generated artefacts live outside the source tree by default.
    generated_root: Path
    cache_dir: Path
    output_dir: Path
    temp_dir: Path

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
    """Build a Config from the environment, loading the project-root ``.env`` first."""
    _load_dotenv()

    generated_root = _resolve_dir("SILLAGE_GENERATED_ROOT", _default_generated_root())
    cache = _resolve_dir("SILLAGE_CACHE_DIR", generated_root / "cache")
    output = _resolve_dir("SILLAGE_OUTPUT_DIR", generated_root / "outputs")
    temp = _resolve_dir("SILLAGE_TMP_DIR", generated_root / "tmp")

    for path in (generated_root, cache, output, temp):
        path.mkdir(parents=True, exist_ok=True)

    return Config(
        windninja_cli=_get("WINDNINJA_CLI", "WindNinja_cli"),
        windninja_data=_get("WINDNINJA_DATA"),
        generated_root=generated_root,
        cache_dir=cache,
        output_dir=output,
        temp_dir=temp,
        meteofrance_api_key=_get("METEOFRANCE_API_KEY") or None,
    )


def resolve_cache_path(path: str | Path, cfg: Config) -> Path:
    """Resolve a cache/generated input path against ``cfg.cache_dir``."""
    return _resolve_under(cfg.cache_dir, path, "cache")


def resolve_output_path(path: str | Path, cfg: Config) -> Path:
    """Resolve an output path against ``cfg.output_dir``."""
    return _resolve_under(cfg.output_dir, path, "outputs")


def resolve_temp_path(path: str | Path, cfg: Config) -> Path:
    """Resolve a temporary path against ``cfg.temp_dir``."""
    return _resolve_under(cfg.temp_dir, path, "tmp")
