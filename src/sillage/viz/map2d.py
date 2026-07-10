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
import os
from pathlib import Path
import tempfile

import numpy as np

from ..terrain.dem import Dem

DISCLAIMER = "Zones candidates (probabilité d'air perturbé sous le vent) — PAS des limites de rotor"

# Web-tile basemaps for orientation under the Pass-1 map (contextily). (family, layer).
# IGN uses the key-free Geoplateforme (data.geopf.fr); OSM/OpenTopoMap are open worldwide.
BASEMAP_SOURCES = {
    "IGN plan": ("GeoportailFrance", "plan"),
    "IGN ortho": ("GeoportailFrance", "orthos"),
    "OpenStreetMap": ("OpenStreetMap", "Mapnik"),
    "OpenTopoMap": ("OpenTopoMap", None),
}


def import_contextily():
    """Import contextily with its joblib/temp cache rooted in the project temp directory.

    On this Windows setup, the OS temp directory can reject joblib's import-time cache creation,
    which silently disables 2-D/3-D basemaps. Keep the override scoped to the import so WindNinja /
    OpenFOAM subprocesses don't inherit a project TMP unless their caller explicitly asks for it.
    """
    candidates: list[Path] = []
    try:
        from ..config import load_config

        candidates.append(Path(load_config().temp_dir) / "contextily")
    except Exception:
        pass
    # Fallback = the OS temp dir, NEVER the (drive-synced) source tree — so nothing generated at
    # runtime leaks into the dev/repo folder even if the configured C:\A2K temp is unavailable.
    candidates.append(Path(tempfile.gettempdir()) / "souslevent" / "contextily")
    tmp = session_tmp = None
    for candidate in candidates:
        try:
            # Require the WHOLE tree (dir + session/joblib) so a candidate whose parent is writable
            # but whose subdirs are not falls through to the next one instead of raising.
            candidate.mkdir(parents=True, exist_ok=True)
            (candidate / "session" / "joblib").mkdir(parents=True, exist_ok=True)
            tmp, session_tmp = candidate, candidate / "session"
            break
        except OSError:
            continue
    if tmp is None:
        raise PermissionError("Aucun dossier temporaire accessible pour contextily/joblib.")
    keys = ("TMP", "TEMP", "TMPDIR")
    old = {k: os.environ.get(k) for k in keys}
    old_tempdir = tempfile.tempdir
    old_mkdtemp = tempfile.mkdtemp

    def stable_mkdtemp(*_args, **_kwargs):
        session_tmp.mkdir(parents=True, exist_ok=True)
        return str(session_tmp)

    try:
        for key in keys:
            os.environ[key] = str(tmp)
        tempfile.tempdir = str(tmp)
        tempfile.mkdtemp = stable_mkdtemp
        import contextily as cx
        return cx
    finally:
        tempfile.mkdtemp = old_mkdtemp
        tempfile.tempdir = old_tempdir
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def add_basemap(ax, crs, source: str = "IGN plan", attribution: bool = False, zorder: int = 0,
                zoom_adjust: int = 1):
    """Add a web-tile basemap under the current axes (reprojected to ``crs``).

    Needs network. Raises if the source is unknown or tiles can't be fetched (callers should
    fall back to the hillshade). ``crs`` is a rasterio/pyproj CRS or an "EPSG:xxxx" string.

    ``zoom_adjust`` bumps contextily's auto-detected tile zoom by that many levels for a more
    detailed basemap on a small crop (+1 ≈ one level finer; each level ~4× the tiles). Ignored
    on contextily builds without the parameter.
    """
    if source not in BASEMAP_SOURCES:
        raise ValueError(f"Unknown basemap source {source!r}; have {list(BASEMAP_SOURCES)}")
    import inspect

    cx = import_contextily()

    family, layer = BASEMAP_SOURCES[source]
    provider = getattr(cx.providers, family)
    if layer is not None:
        provider = provider[layer]
    crs_str = crs.to_string() if hasattr(crs, "to_string") else str(crs)
    kwargs = dict(crs=crs_str, source=provider, attribution=attribution, zorder=zorder)
    if zoom_adjust and "zoom_adjust" in inspect.signature(cx.add_basemap).parameters:
        kwargs["zoom_adjust"] = zoom_adjust
    cx.add_basemap(ax, **kwargs)


@dataclass
class HotspotClick:
    """Result of clicking the map: a region + hour to hand to Pass 2."""

    x: float
    y: float
    hour_index: int
    bbox_m: tuple[float, float, float, float]  # buffered (left, bottom, right, top)


def hillshade(dem: Dem) -> np.ndarray:
    """Public hillshade array for the DEM (0..1), for overlaying on a basemap."""
    return _hillshade(dem)


def draw_hillshade(ax, dem: Dem):
    """Draw only the DEM hillshade (no hazard overlay) — the bare terrain view. Returns im."""
    left, bottom, right, top = dem.bounds
    im = ax.imshow(_hillshade(dem), cmap="gray", extent=(left, right, bottom, top),
                   origin="upper")
    ax.set_xlabel("Est (m)")
    ax.set_ylabel("Nord (m)")
    return im


def draw_indicator(ax, dem: Dem, indicator: np.ndarray):
    """Draw hillshade + hazard overlay onto an EXISTING Axes; returns the overlay image.

    Lets the embedded IHM canvas (ADR-0009) reuse the exact same rendering as the
    standalone figures, so the 2D map looks identical in-app and in saved PNGs.
    """
    return _draw_base(ax, dem, np.asarray(indicator, dtype="float64"))


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


def _draw_base(ax, dem: Dem, first: np.ndarray):
    """Draw hillshade + indicator overlay; return the overlay image handle."""
    from matplotlib import colors

    left, bottom, right, top = dem.bounds
    extent = (left, right, bottom, top)
    ax.imshow(_hillshade(dem), cmap="gray", extent=extent, origin="upper", alpha=0.7)
    im = ax.imshow(
        first, cmap="inferno", extent=extent, origin="upper", alpha=0.55,
        norm=colors.Normalize(0, 1),
    )
    ax.set_xlabel("Est (m)")
    ax.set_ylabel("Nord (m)")
    return im


def show_timeline(dem: Dem, indicator_by_hour, hours, title: str = "Sillage — screening",
                  on_click=None):
    """Render the hazard map with an interactive hourly slider. Returns the Figure.

    Parameters
    ----------
    indicator_by_hour : sequence of 2D arrays (one per hour), each in [0, 1].
    hours : sequence of labels (e.g. ISO times) matching indicator_by_hour.
    on_click : optional callable(HotspotClick) -> None, invoked when the user clicks the
        map. The buffered bbox heuristic here is intentionally simple; the upstream
        fetch / downwind margin tuning is roadmap M3.
    """
    import matplotlib.pyplot as plt
    from matplotlib.widgets import Slider

    stack = [np.asarray(a, dtype="float64") for a in indicator_by_hour]
    labels = list(hours)
    if not stack:
        raise ValueError("indicator_by_hour is empty.")
    if len(labels) != len(stack):
        raise ValueError("hours and indicator_by_hour must have the same length.")

    fig, ax = plt.subplots(figsize=(9, 7.5))
    fig.subplots_adjust(bottom=0.16)
    im = _draw_base(ax, dem, stack[0])
    fig.colorbar(im, ax=ax, label="leeward hazard indicator (0–1)")

    def _set_title(i: int):
        ax.set_title(f"{title}\n{labels[i]}  —  {DISCLAIMER}")

    _set_title(0)

    slider = None
    if len(stack) > 1:
        sax = fig.add_axes([0.15, 0.05, 0.7, 0.03])
        slider = Slider(sax, "hour", 0, len(stack) - 1, valinit=0, valstep=1)

        def _update(_val):
            i = int(slider.val)
            im.set_data(stack[i])
            _set_title(i)
            fig.canvas.draw_idle()

        slider.on_changed(_update)
        fig._sillage_slider = slider  # keep a reference so the widget stays responsive

    if on_click is not None:
        def _onclick(event):
            if event.inaxes is not ax or event.xdata is None:
                return
            i = int(slider.val) if slider is not None else 0
            buf = 2000.0  # minimal symmetric buffer; M3 tunes upstream/downwind margins
            bbox = (event.xdata - buf, event.ydata - buf,
                    event.xdata + buf, event.ydata + buf)
            on_click(HotspotClick(x=float(event.xdata), y=float(event.ydata),
                                  hour_index=i, bbox_m=bbox))

        fig.canvas.mpl_connect("button_press_event", _onclick)

    return fig


def save_timeline_gif(dem: Dem, indicator_by_hour, hours, path,
                      title: str = "Sillage — screening", fps: int = 2):
    """Render the hourly stack to an animated GIF (headless, no display). Returns the path.

    A reproducible artefact alternative to the interactive slider; uses Pillow (a
    matplotlib dependency), so no extra requirement.
    """
    from pathlib import Path

    import matplotlib.pyplot as plt
    from matplotlib import animation

    stack = [np.asarray(a, dtype="float64") for a in indicator_by_hour]
    labels = list(hours)
    if not stack:
        raise ValueError("indicator_by_hour is empty.")

    fig, ax = plt.subplots(figsize=(9, 7.5))
    im = _draw_base(ax, dem, stack[0])
    fig.colorbar(im, ax=ax, label="leeward hazard indicator (0–1)")
    ttl = ax.set_title("")

    def _frame(i: int):
        im.set_data(stack[i])
        ttl.set_text(f"{title}\n{labels[i]}  —  {DISCLAIMER}")
        return im, ttl

    anim = animation.FuncAnimation(fig, _frame, frames=len(stack), blit=False)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    anim.save(str(out), writer=animation.PillowWriter(fps=fps))
    plt.close(fig)
    return out


def _hillshade(dem: Dem, azdeg: float = 315.0, altdeg: float = 45.0) -> np.ndarray:
    from matplotlib.colors import LightSource

    ls = LightSource(azdeg=azdeg, altdeg=altdeg)
    return ls.hillshade(dem.elevation, vert_exag=1.0, dx=dem.resolution_m, dy=dem.resolution_m)
