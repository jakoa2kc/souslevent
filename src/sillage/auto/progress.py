"""Progress + ETA for the long auto pipeline (potentially many momentum solves).

The auto run is ``len(zones) × len(hours)`` momentum solves — minutes to a long while. We track
completed tasks and their durations to show a percent and a **time-remaining** estimate (mean of
observed task times × remaining tasks — robust and deterministic, easy to test).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..timing import format_seconds


@dataclass
class ProgressTracker:
    """Count completed tasks + estimate the remaining time from observed task durations."""

    total: int
    done: int = 0
    _durations: list[float] = field(default_factory=list)

    def record(self, seconds: float) -> None:
        """Mark one task done, with how long it took (feeds the ETA)."""
        self.done += 1
        self._durations.append(max(0.0, float(seconds)))

    @property
    def fraction(self) -> float:
        return (self.done / self.total) if self.total > 0 else 1.0

    @property
    def percent(self) -> int:
        return int(round(self.fraction * 100))

    @property
    def eta_seconds(self) -> float | None:
        """Estimated seconds remaining; 0 when finished, None before the first task completes."""
        if self.done >= self.total:
            return 0.0
        if not self._durations:
            return None
        mean = sum(self._durations) / len(self._durations)
        return mean * (self.total - self.done)

    def summary(self, prefix: str = "") -> str:
        eta = self.eta_seconds
        eta_txt = "reste ?" if eta is None else f"reste ~{format_seconds(eta)}"
        head = f"{prefix} · " if prefix else ""
        return f"{head}{self.done}/{self.total} · {self.percent}% · {eta_txt}"
