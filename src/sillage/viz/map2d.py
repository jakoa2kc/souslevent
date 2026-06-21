"""Pass-1 visualization: 2D hazard map over the domain, with an hourly time slider.

TRIAGE view. Renders the derived hazard indicator (screening/indicator.py) on the terrain
extent. MUST be labelled clearly: these are CANDIDATE zones (likelihood of disturbed air),
NOT rotor boundaries -- the mass solver cannot show rotors (docs/03 ADR-0003/0005).

The click hook returns a (feature_bbox, hour) request to launch a Pass-2 momentum run.
First implementation uses matplotlib; a richer Qt/web surface can come later (ADR-0006
open question).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..terrain.dem import Dem

DISCLAIMER = "Candidates (likelihood of disturbed lee air) — NOT rotor boundaries"


@dataclass
class HotspotClick:
    """Result of clicking the map: a region + hour to hand to Pass 2."""

    x: float
    y: float
    hour_index: int
    bbox_m: tuple[float, float, float, float]  # buffered (left, bottom, right, top)


def show_static(dem: Dem, indicator: np.ndarray, title: str = "Sillage — screening"):
    """Render a single-hour hazard map over the DEM hillshade. Returns the Figure.

    Overlays the indicator (0..1) with a perceptually-ordered colormap and draws the
    DISCLAIMER prominently.
    """
    import matplotlib.pyplot as plt
    from matplotlib import colors

    left, bottom, right, top = dem.bounds
    extent = (left, right, bottom, top)

    fig, ax = plt.subplots(figsize=(9, 7))
    ax.imshow(_hillshade(dem), cmap="gray", extent=extent, origin="upper", alpha=0.7)
    im = ax.imshow(
        indicator, cmap="inferno", extent=extent, origin="upper", alpha=0.55,
        norm=colors.Normalize(0, 1),
    )
    fig.colorbar(im, ax=ax, label="leeward hazard indicator (0–1)")
    ax.set_title(f"{title}\n{DISCLAIMER}")
    ax.set_xlabel("Easting (m)")
    ax.set_ylabel("Northing (m)")
    return fig


def show_timeline(dem: Dem, indicator_by_hour, hours, on_click=None):
    """Render the hazard map with an hourly slider.

    Parameters
    ----------
    indicator_by_hour : sequence of 2D arrays (one per hour), each in [0, 1].
    hours : sequence of labels (e.g. ISO times) matching indicator_by_hour.
    on_click : optional callable(HotspotClick) -> None, invoked when the user clicks.

    TODO (roadmap M1/T6): wire the Slider widget and the pick event; compute the buffered
    bbox from the clicked feature (upstream fetch + downwind margin) before handing to
    Pass 2. Kept as a contract here so the demo can call it.
    """
    raise NotImplementedError(
        "Timeline slider not implemented yet (roadmap M1/T6). show_static() works now."
    )


def _hillshade(dem: Dem, azdeg: float = 315.0, altdeg: float = 45.0) -> np.ndarray:
    from matplotlib.colors import LightSource

    ls = LightSource(azdeg=azdeg, altdeg=altdeg)
    return ls.hillshade(dem.elevation, vert_exag=1.0, dx=dem.resolution_m, dy=dem.resolution_m)
