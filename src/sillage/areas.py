"""Named study areas used by demos and repeatable analyses.

Coordinates are WGS84 lon/lat bounds. Keep these definitions small and explicit so
assistants and humans can run the same scenario without rediscovering the geography.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StudyArea:
    """A named rectangular study area in WGS84 coordinates."""

    name: str
    west: float
    south: float
    east: float
    north: float
    description: str

    @property
    def center_lonlat(self) -> tuple[float, float]:
        return ((self.west + self.east) / 2.0, (self.south + self.north) / 2.0)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (self.west, self.south, self.east, self.north)


CHAMPSAUR = StudyArea(
    name="champsaur",
    west=5.95,
    south=44.55,
    east=6.42,
    north=44.86,
    description=(
        "Vallee du Champsaur, Hautes-Alpes: Saint-Bonnet, Ancelle, Pont-du-Fosse, "
        "Orcieres, Chaillol, Champoleon."
    ),
)


