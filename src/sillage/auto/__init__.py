"""Automatic full-resolution Pass-2 pipeline (parallel mode).

A "one-click" alternative to the manual app: pick a flight zone + window, validate, and the
whole zone is solved at the finest topo scale by **subdividing it into momentum sub-domains**
and running Pass-2 over each (zone × hour), then aggregating into a time-sliderable 3D scene.

This package is **additive** — it reuses the existing libraries (``terrain``, ``flow``,
``wind``, ``viz``, ``screening.pass1.parallel_run_plan``, ``timing``) and leaves the current
app untouched. See docs/10_auto_pipeline.md and ADR-0022.
"""

from __future__ import annotations

from .partition import SubZone, partition_zone
from .pipeline import AutoConfig, AutoResult, CaseResult, run_auto
from .progress import ProgressTracker

__all__ = [
    "SubZone",
    "partition_zone",
    "AutoConfig",
    "AutoResult",
    "CaseResult",
    "run_auto",
    "ProgressTracker",
]
