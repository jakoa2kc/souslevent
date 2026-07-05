"""Automatic full-resolution Pass-2 pipeline (parallel mode).

A "one-click" alternative to the manual app: draw a flight route + window, validate, and the
route corridor is screened once before Pass-2 runs over each selected feature × hour, then
aggregates the compact rotors into a time-sliderable 3D scene.

This package is **additive** — it reuses the existing libraries (``terrain``, ``flow``,
``wind``, ``viz``, ``screening.pass1.parallel_run_plan``, ``timing``) and leaves the current
app untouched. See docs/10_auto_pipeline.md and ADR-0022.
"""

from __future__ import annotations

from .partition import SubZone, feature_domains, partition_zone
from .pipeline import (
    AutoConfig,
    AutoResult,
    CaseResult,
    ScreeningResult,
    cleanup_auto_artifacts,
    run_auto,
    screen_candidates,
)
from .progress import ProgressTracker

__all__ = [
    "SubZone",
    "feature_domains",
    "partition_zone",
    "AutoConfig",
    "AutoResult",
    "CaseResult",
    "ScreeningResult",
    "cleanup_auto_artifacts",
    "run_auto",
    "screen_candidates",
    "ProgressTracker",
]
