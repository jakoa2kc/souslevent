"""Human-readable meteorological wind directions."""

from __future__ import annotations


_DIRECTION_LABELS = (
    "Nord",
    "Nord-Est",
    "Est",
    "Sud-Est",
    "Sud",
    "Sud-Ouest",
    "Ouest",
    "Nord-Ouest",
)


def direction_label(from_deg: float | int | None) -> str:
    """French label for a meteorological direction, i.e. where the wind comes from."""
    if from_deg is None:
        return "?"
    idx = int((float(from_deg) % 360.0 + 22.5) // 45.0) % len(_DIRECTION_LABELS)
    return _DIRECTION_LABELS[idx]
