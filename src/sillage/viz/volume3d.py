"""Pass-2 visualization: 3D recirculation volumes from the OpenFOAM field.

DETAIL view. Given the mesh read from the OpenFOAM case directory
(flow/openfoam_reader.py), build a scene with: the terrain surface, streamlines of the
flow, and threshold volumes marking danger -- the REVERSED-FLOW volume (along-mean-flow
velocity component < 0) and/or the TURBULENCE-INTENSITY volume. Windward green, leeward
red-orange by severity.

GPU note: PyVista/VTK rendering is where the workstation GPU helps; the solve itself was
CPU-bound (ADR-0006). Keep streamline seed counts and mesh size sane for interactivity.

Status: contract + skeleton (roadmap M2/T10). Reads real fields; rendering wiring is TODO.
"""

from __future__ import annotations

import numpy as np

from ..flow import openfoam_reader as ofr


def build_scene(
    case_dir: str,
    mean_flow_dir: np.ndarray,
    show_streamlines: bool = True,
    show_reversed_flow: bool = True,
    show_turbulence: bool = False,
    turbulence_threshold: float = 0.2,
):
    """Assemble a PyVista plotter for one Pass-2 feature. Returns the Plotter.

    Parameters
    ----------
    case_dir : OpenFOAM case directory from a momentum run (flow/windninja.run_momentum).
    mean_flow_dir : reference mean-flow unit vector (e.g. from the Pass-1 upstream wind),
        used to define "reversed" flow.
    show_reversed_flow : threshold the volume where along-flow velocity < 0 (the rotor).
    show_turbulence : threshold where turbulence intensity exceeds `turbulence_threshold`.
    """
    import pyvista as pv

    mesh = ofr.read_case(case_dir)
    plotter = pv.Plotter()

    # Terrain surface: the lower boundary of the OpenFOAM domain (the STL-derived ground).
    # TODO (T10): extract the ground patch from the case and add as an opaque surface.

    if show_streamlines:
        # TODO (T10): seed a line/plane upstream and integrate streamlines; color by speed.
        # streams = mesh.streamlines(...); plotter.add_mesh(streams, ...)
        pass

    if show_reversed_flow:
        along = ofr.along_flow_component(mesh, mean_flow_dir)
        mesh["along_flow"] = along
        reversed_vol = mesh.threshold(value=0.0, scalars="along_flow", invert=True)
        if reversed_vol.n_cells:
            plotter.add_mesh(reversed_vol, color="orangered", opacity=0.5,
                             label="reversed flow (rotor)")

    if show_turbulence:
        ti = ofr.turbulence_intensity(mesh)
        if ti is not None:
            mesh["turb_intensity"] = ti
            turb_vol = mesh.threshold(value=turbulence_threshold, scalars="turb_intensity")
            if turb_vol.n_cells:
                plotter.add_mesh(turb_vol, color="red", opacity=0.4,
                                 label="high turbulence intensity")

    plotter.add_text(
        "Pass 2 — resolved recirculation (steady RANS mean; lee accuracy is indicative)",
        font_size=10,
    )
    plotter.add_legend()
    return plotter


def show(case_dir: str, mean_flow_dir: np.ndarray, **kwargs) -> None:  # pragma: no cover
    """Convenience: build and display the scene interactively."""
    build_scene(case_dir, mean_flow_dir, **kwargs).show()
