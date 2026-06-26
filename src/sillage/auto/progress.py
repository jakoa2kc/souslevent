"""Progress + ETA for the long auto pipeline (potentially many momentum solves).

The auto run is ``len(zones) × len(hours)`` momentum solves. They run **in parallel** (``workers``
at a time), so the naive "mean task time × remaining tasks" over-estimates the wait by roughly the
worker count: when 5 solves run at once as a single wave, the first completion does NOT mean four
more solve-times remain — the other four finish almost together.

So we model **waves**: ``ceil(total / workers)`` batches, each ~one solve long. The total wall
estimate is ``mean(observed solve time) × waves``; the ETA is that minus the elapsed wall clock, and
the displayed percent is the elapsed fraction of it (floored by the real completed-task fraction).
The clock is injectable for tests. See docs/10_auto_pipeline.md / ADR-0022.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from ..timing import format_seconds


@dataclass
class ProgressTracker:
    """Count completed tasks + estimate remaining wall time, **accounting for parallel workers**."""

    total: int
    workers: int = 1
    done: int = 0
    clock: Callable[[], float] = time.monotonic
    _durations: list[float] = field(default_factory=list, repr=False)
    _t0: float | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def start(self) -> None:
        """Anchor the wall clock at the moment the solves begin (call before the first solve)."""
        with self._lock:
            self._t0 = self.clock()

    def record(self, seconds: float = 0.0) -> None:
        """Mark one task done, with how long that solve took (feeds the wave-time estimate)."""
        with self._lock:
            self.done += 1
            self._durations.append(max(0.0, float(seconds)))
            if self._t0 is None:
                self._t0 = self.clock()

    # --- task-completion view (exact, lumpy) ---------------------------------
    @property
    def fraction(self) -> float:
        return (self.done / self.total) if self.total > 0 else 1.0

    @property
    def percent(self) -> int:
        return int(round(self.fraction * 100))

    # --- wall-clock / wave view (smooth, parallelism-aware) ------------------
    def _waves_total(self) -> int:
        w = max(1, int(self.workers))
        return max(1, math.ceil(self.total / w)) if self.total > 0 else 1

    def _elapsed(self) -> float:
        return (self.clock() - self._t0) if self._t0 is not None else 0.0

    def _total_estimate(self) -> float | None:
        """Estimated total wall time = mean solve time × number of waves; None before any solve."""
        with self._lock:
            if not self._durations:
                return None
            mean = sum(self._durations) / len(self._durations)
        return mean * self._waves_total()

    @property
    def eta_seconds(self) -> float | None:
        """Seconds remaining; 0 when finished, None before the first solve completes."""
        if self.done >= self.total:
            return 0.0
        est = self._total_estimate()
        if est is None:
            return None
        return max(0.0, est - self._elapsed())

    @property
    def display_fraction(self) -> float:
        """Smooth progress in [done/total, 0.99]: the elapsed fraction of the total estimate, never
        below the genuinely completed fraction. Falls back to the task fraction before any solve."""
        if self.total <= 0 or self.done >= self.total:
            return 1.0
        floor = self.done / self.total
        est = self._total_estimate()
        if est is None or est <= 0:
            return floor
        return min(0.99, max(floor, self._elapsed() / est))

    @property
    def display_percent(self) -> int:
        return int(round(self.display_fraction * 100))

    def summary(self, prefix: str = "") -> str:
        eta = self.eta_seconds
        eta_txt = "reste ?" if eta is None else f"reste ~{format_seconds(eta)}"
        head = f"{prefix} · " if prefix else ""
        return f"{head}{self.done}/{self.total} cas · {self.display_percent}% · {eta_txt}"
