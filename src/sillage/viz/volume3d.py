"""Pass-2 visualization: 3D recirculation volumes from the OpenFOAM field.

DETAIL view. Given the OpenFOAM case from a momentum run (flow/openfoam_reader.py), build
a scene with: the terrain surface, flow streamlines, and threshold volumes marking danger
-- the REVERSED-FLOW volume (along-mean-flow velocity component < 0, i.e. the rotor) and
the TURBULENCE-INTENSITY volume.

GPU note: PyVista/VTK rendering is where the workstation GPU helps; the solve itself was
CPU-bound (ADR-0006). Keep streamline seed counts and mesh size sane for interactivity.

Two entry points:
  * build_scene(...) -> a configured pv.Plotter (interactive or off_screen).
  * save_png(...)    -> render the scene headless to a PNG (no display needed).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ..flow import openfoam_reader as ofr

REVERSED_COLOR = "orangered"
TURB_COLOR = "red"
SCENE_TEXT = "Pass 2 — resolved recirculation (steady RANS mean; lee accuracy is indicative)"


def _seed_streamlines(mesh, mean_flow_dir: np.ndarray, n_points: int = 200):
    """Best-effort streamlines seeded on an upstream disc. Returns PolyData or None.

    Streamlines need point-associated vectors, so we interpolate cell U to points. Seeding
    upstream (opposite the mean-flow direction) and integrating forward traces the flow as
    it separates over the crest into the lee.
    """
    try:
        pm = mesh.cell_data_to_point_data()
        if "U" not in pm.point_data:
            return None
        pm.set_active_vectors("U")
        b = pm.bounds  # xmin, xmax, ymin, ymax, zmin, zmax
        cx, cy = (b[0] + b[1]) / 2.0, (b[2] + b[3]) / 2.0
        span = max(b[1] - b[0], b[3] - b[2])
        d = np.asarray(mean_flow_dir, dtype="float64")
        d = d / (np.linalg.norm(d) + 1e-12)
        seed = np.array([cx, cy, 0.0]) - d * span * 0.4
        seed[2] = b[4] + (b[5] - b[4]) * 0.25  # lower quarter of the domain height
        lines = pm.streamlines(
            vectors="U",
            source_center=tuple(seed),
            source_radius=span * 0.3,
            n_points=n_points,
            integration_direction="forward",
            max_time=span * 4.0,
        )
        return lines if lines.n_points else None
    except Exception:
        return None


def populate_plotter(
    plotter,
    case_dir: str,
    mean_flow_dir: np.ndarray,
    show_streamlines: bool = True,
    show_reversed_flow: bool = True,
    show_turbulence: bool = False,
    turbulence_threshold: float = 0.2,
):
    """Add the Pass-2 scene (terrain + volumes + streamlines) to an EXISTING plotter.

    Works with both a standalone ``pyvista.Plotter`` and an embedded
    ``pyvistaqt.QtInteractor`` (the IHM, ADR-0009), so the scene-building logic lives in one
    place. Returns the same plotter.
    """
    mesh = ofr.read_case(case_dir)
    labelled = False

    # Terrain surface: the STL ground NinjaFOAM derived from the DEM (lower boundary).
    terrain = ofr.read_terrain_stl(case_dir)
    if terrain is not None and terrain.n_points:
        terrain["elevation_m"] = terrain.points[:, 2]
        plotter.add_mesh(terrain, scalars="elevation_m", cmap="gist_earth",
                         show_scalar_bar=False, opacity=1.0)

    if show_streamlines:
        lines = _seed_streamlines(mesh, mean_flow_dir)
        if lines is not None:
            plotter.add_mesh(lines.tube(radius=max(lines.length / 1500.0, 1.0)),
                             color="white", opacity=0.6)

    if show_reversed_flow:
        along = ofr.along_flow_component(mesh, mean_flow_dir)
        mesh["along_flow"] = along
        reversed_vol = mesh.threshold(value=0.0, scalars="along_flow", invert=True)
        if reversed_vol.n_cells:
            plotter.add_mesh(reversed_vol, color=REVERSED_COLOR, opacity=0.5,
                             label="reversed flow (rotor)")
            labelled = True

    if show_turbulence:
        ti = ofr.turbulence_intensity(mesh)
        if ti is not None:
            mesh["turb_intensity"] = ti
            turb_vol = mesh.threshold(value=turbulence_threshold, scalars="turb_intensity")
            if turb_vol.n_cells:
                plotter.add_mesh(turb_vol, color=TURB_COLOR, opacity=0.4,
                                 label="high turbulence intensity")
                labelled = True

    plotter.add_text(SCENE_TEXT, font_size=9)
    plotter.show_axes()
    if labelled:
        plotter.add_legend()
    plotter.view_isometric()
    return plotter


def build_scene(
    case_dir: str,
    mean_flow_dir: np.ndarray,
    show_streamlines: bool = True,
    show_reversed_flow: bool = True,
    show_turbulence: bool = False,
    turbulence_threshold: float = 0.2,
    off_screen: bool = False,
):
    """Assemble a standalone PyVista plotter for one Pass-2 feature. Returns the Plotter.

    For the embedded IHM viewport, call ``populate_plotter`` on a QtInteractor instead.
    """
    import pyvista as pv

    plotter = pv.Plotter(off_screen=off_screen)
    return populate_plotter(
        plotter, case_dir, mean_flow_dir,
        show_streamlines=show_streamlines, show_reversed_flow=show_reversed_flow,
        show_turbulence=show_turbulence, turbulence_threshold=turbulence_threshold,
    )


def save_png(
    case_dir: str,
    mean_flow_dir: np.ndarray,
    path: str | Path,
    window_size: tuple[int, int] = (1280, 960),
    **kwargs,
) -> Path:
    """Render the scene to a PNG headless (off_screen). Returns the path.

    Lets Pass-2 produce a reproducible 3D snapshot without an interactive display
    (CI / servers / quick review). Extra kwargs pass through to build_scene.
    """
    kwargs.setdefault("off_screen", True)
    plotter = build_scene(case_dir, mean_flow_dir, **kwargs)
    plotter.window_size = list(window_size)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    plotter.screenshot(str(out))
    plotter.close()
    return out


def show(case_dir: str, mean_flow_dir: np.ndarray, **kwargs) -> None:  # pragma: no cover
    """Convenience: build and display the scene interactively."""
    build_scene(case_dir, mean_flow_dir, **kwargs).show()


def mean_flow_vector(wind_from_deg: float) -> np.ndarray:
    """Horizontal unit vector pointing where the wind BLOWS TO (meteorological 'from')."""
    blow_to = np.deg2rad((wind_from_deg + 180.0) % 360.0)
    return np.array([np.sin(blow_to), np.cos(blow_to), 0.0])
