"""Read the OpenFOAM CASE directory directly to recover the true 3D momentum field.

Why this exists: for momentum runs, WindNinja's ``write_vtk_output`` writes the
mass-solver MESH, not the resolved OpenFOAM field. To visualize real recirculation we
must read the OpenFOAM case directory itself. (ADR-0004, docs/05.)

PyVista wraps VTK's OpenFOAM reader. We return the internal mesh with the velocity (and,
if present, turbulence) fields, plus a couple of derived helpers used by viz.volume3d.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

try:
    import pyvista as pv
except Exception as exc:  # pragma: no cover - dependency guard
    raise ImportError(
        "flow.openfoam_reader requires pyvista (VTK-backed). See docs/support/environment.md."
    ) from exc


# Common OpenFOAM field names (verify against the WindNinja/NinjaFOAM case).
VELOCITY_FIELDS = ("U", "Umean", "UMean")
TKE_FIELDS = ("k",)            # turbulent kinetic energy
EPSILON_FIELDS = ("epsilon",)


def read_case(case_dir: str | Path, last_time_only: bool = True) -> "pv.DataSet":
    """Open an OpenFOAM case directory and return the internal volume mesh.

    Parameters
    ----------
    case_dir : path to the OpenFOAM case (the dir containing constant/ and system/).
    last_time_only : read only the final time step (steady-state result we want).
    """
    case_dir = Path(case_dir)
    foam = case_dir / f"{case_dir.name}.foam"
    if not foam.exists():
        # PyVista's OpenFOAMReader expects a .foam stub; create an empty one if missing.
        foam.touch()

    reader = pv.OpenFOAMReader(str(foam))
    try:
        if last_time_only and reader.time_values:
            reader.set_active_time_value(reader.time_values[-1])
    except Exception:
        pass  # some builds expose times differently; fall back to default

    multiblock = reader.read()
    # The internal mesh block holds the volume field; combine to be robust.
    mesh = multiblock.combine() if hasattr(multiblock, "combine") else multiblock
    return mesh


def read_terrain_stl(case_dir: str | Path) -> "pv.PolyData | None":
    """Read the terrain surface STL that NinjaFOAM derives from the DEM.

    NinjaFOAM writes the ground as ``constant/triSurface/<dem>.stl`` (plus a ``_out.stl``
    domain box). We return the terrain patch (the non-``_out`` STL) as PolyData for the 3D
    scene, or None if absent. Used by viz.volume3d to draw the opaque ground.
    """
    tri = Path(case_dir) / "constant" / "triSurface"
    if not tri.is_dir():
        return None
    stls = sorted(p for p in tri.glob("*.stl") if not p.stem.endswith("_out"))
    if not stls:
        stls = sorted(tri.glob("*.stl"))
    if not stls:
        return None
    return pv.read(str(stls[0]))


def _first_present(mesh, names: tuple[str, ...]) -> str | None:
    for n in names:
        if n in mesh.array_names:
            return n
    return None


def velocity(mesh) -> np.ndarray:
    """Return the cell/point velocity vectors (N, 3) or raise if absent."""
    name = _first_present(mesh, VELOCITY_FIELDS)
    if name is None:
        raise KeyError(
            f"No velocity field found (looked for {VELOCITY_FIELDS}). "
            f"Available: {list(mesh.array_names)}"
        )
    return np.asarray(mesh[name])


def turbulence_intensity(mesh, reference_speed: float | None = None) -> np.ndarray | None:
    """Turbulence intensity I = sqrt(2k/3) / U_ref, if k is available; else None.

    `reference_speed` defaults to the mean velocity magnitude over the domain. Turbulence
    intensity is often a more meaningful "is-this-dangerous" field than speed alone
    (docs/01_theory_and_physics.md).
    """
    kname = _first_present(mesh, TKE_FIELDS)
    if kname is None:
        return None
    k = np.asarray(mesh[kname])
    if reference_speed is None:
        try:
            reference_speed = float(np.linalg.norm(velocity(mesh), axis=1).mean())
        except Exception:
            reference_speed = 1.0
    reference_speed = max(reference_speed, 1e-6)
    return np.sqrt(np.maximum(2.0 / 3.0 * k, 0.0)) / reference_speed


def along_flow_component(mesh, mean_flow_dir: np.ndarray) -> np.ndarray:
    """Signed velocity component along a reference mean-flow direction (unit vector).

    Negative values mark REVERSED flow -> the operational definition of the
    recirculation / rotor volume (see viz.volume3d).
    """
    u = velocity(mesh)
    d = np.asarray(mean_flow_dir, dtype="float64")
    d = d / (np.linalg.norm(d) + 1e-12)
    return u @ d
