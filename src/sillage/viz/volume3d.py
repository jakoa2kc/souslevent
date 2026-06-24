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


def _drape_basemap(plotter, terrain, crs, source: str) -> bool:
    """Texture-drape a web-tile basemap (top-down) onto the terrain surface. Returns success.

    Needs the terrain CRS (UTM) to fetch the basemap for the right lon/lat area. Over a few
    km, web-mercator ≈ UTM, so a planar drape is fine.
    """
    try:
        import contextily as cx
        import pyvista as pv
        from rasterio.crs import CRS as RCRS
        from rasterio.warp import transform as warp_xy

        from .map2d import BASEMAP_SOURCES

        if source not in BASEMAP_SOURCES:
            return False
        b = terrain.bounds
        xmin, xmax, ymin, ymax = b[0], b[1], b[2], b[3]
        lons, lats = warp_xy(crs, RCRS.from_epsg(4326), [xmin, xmax], [ymin, ymax])
        w, e = min(lons), max(lons)
        s, n = min(lats), max(lats)
        family, layer = BASEMAP_SOURCES[source]
        prov = getattr(cx.providers, family)
        if layer is not None:
            prov = prov[layer]
        img, _ext = cx.bounds2img(w, s, e, n, source=prov, ll=True)
        img = np.ascontiguousarray(img[::-1, :, :3])  # flip so row 0 -> south (v=0)
        tex = pv.numpy_to_texture(img)
        terrain.texture_map_to_plane(
            origin=(xmin, ymin, 0.0), point_u=(xmax, ymin, 0.0), point_v=(xmin, ymax, 0.0),
            inplace=True)
        plotter.add_mesh(terrain, texture=tex)
        return True
    except Exception:
        return False


def _add_rotor(plotter, rev, terrain) -> None:
    """Add the reversed-flow (rotor) volume coloured by HEIGHT ABOVE GROUND (yellow near the
    ground -> red -> purple high up) with OPACITY proportional to rotor intensity."""
    from matplotlib.colors import LinearSegmentedColormap, Normalize
    from scipy.spatial import cKDTree

    centers = rev.cell_centers().points
    tpts = np.asarray(terrain.points)
    idx = cKDTree(tpts[:, :2]).query(centers[:, :2])[1]
    hagl = centers[:, 2] - tpts[idx, 2]  # height above the terrain
    along = rev.cell_data.get("along_flow")
    intensity = np.clip(-np.asarray(along), 0.0, None) if along is not None else np.ones(len(centers))

    cmap = LinearSegmentedColormap.from_list("yrp", ["#ffff00", "#ff2a00", "#7a00b0"])
    lo, hi = np.nanpercentile(hagl, 5), np.nanpercentile(hagl, 95)
    rgb = cmap(np.clip(Normalize(lo, max(hi, lo + 1e-6))(hagl), 0, 1))[:, :3]
    imax = max(float(np.nanpercentile(intensity, 95)), 1e-6)
    alpha = 0.12 + 0.85 * np.clip(intensity / imax, 0.0, 1.0)  # weak transparent -> strong opaque
    rev.cell_data["rotor_rgba"] = (np.c_[rgb, alpha] * 255).astype(np.uint8)
    plotter.add_mesh(rev, scalars="rotor_rgba", rgba=True)


def populate_plotter(
    plotter,
    case_dir: str,
    mean_flow_dir: np.ndarray,
    show_streamlines: bool = False,
    show_reversed_flow: bool = True,
    show_turbulence: bool = False,
    turbulence_threshold: float = 0.2,
    crs=None,
    basemap_source: str = "IGN plan",
):
    """Add the Pass-2 scene to an EXISTING plotter (standalone Plotter or embedded
    QtInteractor). Terrain is draped with a basemap (if ``crs`` is given) instead of an
    elevation colormap; the rotor is coloured by height-above-ground with opacity ∝ intensity.
    """
    mesh = ofr.read_case(case_dir)
    terrain = ofr.read_terrain_stl(case_dir)

    if terrain is not None and terrain.n_points:
        if not (crs is not None and _drape_basemap(plotter, terrain, crs, basemap_source)):
            terrain["elevation_m"] = terrain.points[:, 2]
            plotter.add_mesh(terrain, scalars="elevation_m", cmap="gist_earth",
                             show_scalar_bar=False)

    if show_streamlines:
        lines = _seed_streamlines(mesh, mean_flow_dir)
        if lines is not None:
            plotter.add_mesh(lines.tube(radius=max(lines.length / 1500.0, 1.0)),
                             color="white", opacity=0.5)

    if show_reversed_flow:
        mesh["along_flow"] = ofr.along_flow_component(mesh, mean_flow_dir)
        rev = mesh.threshold(value=0.0, scalars="along_flow", invert=True)
        if rev.n_cells and terrain is not None and terrain.n_points:
            _add_rotor(plotter, rev, terrain)
        elif rev.n_cells:
            plotter.add_mesh(rev, color=REVERSED_COLOR, opacity=0.5)

    if show_turbulence:
        ti = ofr.turbulence_intensity(mesh)
        if ti is not None:
            mesh["turb_intensity"] = ti
            turb_vol = mesh.threshold(value=turbulence_threshold, scalars="turb_intensity")
            if turb_vol.n_cells:
                plotter.add_mesh(turb_vol, color=TURB_COLOR, opacity=0.35)

    plotter.add_text(SCENE_TEXT, font_size=9)
    plotter.add_text("Rotor : jaune = près du sol → rouge → violet = haut ·"
                     " opacité = intensité", position="lower_left", font_size=8)
    plotter.show_axes()
    plotter.view_isometric()
    return plotter


def build_scene(
    case_dir: str,
    mean_flow_dir: np.ndarray,
    show_streamlines: bool = False,
    show_reversed_flow: bool = True,
    show_turbulence: bool = False,
    turbulence_threshold: float = 0.2,
    crs=None,
    basemap_source: str = "IGN plan",
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
        crs=crs, basemap_source=basemap_source,
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
