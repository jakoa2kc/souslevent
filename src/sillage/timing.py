"""Small timing helpers for long Sillage jobs.

The goal is not profiling precision; it is to leave useful breadcrumbs in the UI/CLI when a
DEM fetch, WindNinja solve, or rendering step is slow. The collector is thread-safe so
parallel Pass-1 runs can report per-hour timings.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from time import perf_counter


@dataclass(frozen=True)
class PhaseTiming:
    """Elapsed duration for one named phase."""

    name: str
    seconds: float


class RunTimings:
    """Collect elapsed durations for a job, including phases running in worker threads."""

    def __init__(self) -> None:
        self._items: list[PhaseTiming] = []
        self._lock = Lock()

    @contextmanager
    def measure(self, name: str):
        start = perf_counter()
        try:
            yield
        finally:
            self.add(name, perf_counter() - start)

    def add(self, name: str, seconds: float) -> None:
        with self._lock:
            self._items.append(PhaseTiming(name, max(0.0, float(seconds))))

    @property
    def items(self) -> tuple[PhaseTiming, ...]:
        with self._lock:
            return tuple(self._items)

    @property
    def total_s(self) -> float:
        return sum(item.seconds for item in self.items)

    def summary(self, max_items: int = 4) -> str:
        items = self.items
        if not items:
            return "0s"
        shown = items[:max_items]
        parts = [f"{item.name} {format_seconds(item.seconds)}" for item in shown]
        if len(items) > len(shown):
            parts.append(f"+{len(items) - len(shown)} phases")
        parts.append(f"total {format_seconds(sum(item.seconds for item in items))}")
        return " · ".join(parts)


def format_seconds(seconds: float) -> str:
    """Compact human duration, stable enough for status bars and logs."""
    seconds = max(0.0, float(seconds))
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60.0:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(seconds, 60.0)
    return f"{int(minutes)}m{int(round(rest)):02d}s"
