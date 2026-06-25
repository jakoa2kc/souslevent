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
        # Keep the tile mosaic north-up (row 0 = north): with texture_map_to_plane below,
        # VTK maps array row 0 to the north (point_v) edge, so NO vertical flip — flipping
        # renders the basemap (and its text) upside down. Verified by a top-down drape test.
        img = np.ascontiguousarray(img[:, :, :3])
        tex = pv.numpy_to_texture(img)
        terrain.texture_map_to_plane(
            origin=(xmin, ymin, 0.0), point_u=(xmax, ymin, 0.0), point_v=(xmin, ymax, 0.0),
            inplace=True)
        plotter.add_mesh(terrain, texture=tex)
        return True
    except Exception:
        return False


def _terrain_mesh(dem, lift: float = 0.0):
    """A PyVista StructuredGrid from a north-up DEM (row 0 = north), optionally lifted in Z."""
    import pyvista as pv

    z = np.array(dem.elevation, dtype="float64")
    if not np.isfinite(z).all():
        fill = float(np.nanmin(z)) if np.isfinite(z).any() else 0.0
        z = np.where(np.isfinite(z), z, fill)
    left, bottom, right, top = dem.bounds
    ny, nx = z.shape
    xs = np.linspace(left, right, nx)
    ys = np.linspace(top, bottom, ny)  # row 0 -> north (top)
    xx, yy = np.meshgrid(xs, ys)
    return pv.StructuredGrid(xx, yy, z + lift)


def _hazard_texture_image(hazard):
    """RGBA image (row 0 = north) of the hazard field: inferno colour with alpha ∝ hazard, fully
    transparent where masked/zero so the draped basemap shows through outside danger zones."""
    import matplotlib

    h = np.array(hazard, dtype="float64")
    finite = np.isfinite(h)
    hc = np.clip(np.where(finite, h, 0.0), 0.0, 1.0)
    rgba = matplotlib.colormaps["inferno"](hc)
    rgba[..., 3] = np.where(finite, hc, 0.0) * 0.85
    return (rgba * 255).astype("uint8")


def _add_north_arrow(plotter, terrain) -> None:
    import pyvista as pv

    b = terrain.bounds
    dx, dy, dz = b[1] - b[0], b[3] - b[2], b[5] - b[4]
    length = 0.16 * min(dx, dy)
    base = (b[0] + 0.10 * dx, b[2] + 0.10 * dy, b[5] + 0.20 * max(dz, length))
    arrow = pv.Arrow(start=base, direction=(0.0, 1.0, 0.0), scale=length,
                     tip_length=0.28, tip_radius=0.09, shaft_radius=0.035)
    plotter.add_mesh(arrow, color="#222222")
    plotter.add_point_labels([(base[0], base[1] + length * 1.15, base[2])], ["N"], font_size=12,
                             text_color="black", shape_color="white", shape_opacity=0.55,
                             always_visible=True)


def _add_wind_arrows_3d(plotter, terrain, winds) -> None:
    """One wind arrow per WindNinja zone, sitting above the terrain, coloured by speed (turbo)."""
    import matplotlib
    import pyvista as pv
    from matplotlib.colors import Normalize
    from scipy.spatial import cKDTree

    tpts = np.asarray(terrain.points)
    tree = cKDTree(tpts[:, :2])
    b = terrain.bounds
    side = max(1, round(len(winds) ** 0.5))
    length = min(0.14 * min(b[1] - b[0], b[3] - b[2]),
                 0.42 * min(b[1] - b[0], b[3] - b[2]) / side)
    lift = 0.04 * (b[5] - b[4]) + 10.0
    norm = Normalize(0.0, 20.0)
    cmap = matplotlib.colormaps["turbo"]
    for x, y, spd, drc in winds:
        zi = float(tpts[tree.query([x, y])[1], 2])
        blow = np.deg2rad((float(drc) + 180.0) % 360.0)
        d = (float(np.sin(blow)), float(np.cos(blow)), 0.0)
        start = (x - d[0] * length / 2, y - d[1] * length / 2, zi + lift)
        arrow = pv.Arrow(start=start, direction=d, scale=length,
                         tip_length=0.30, tip_radius=0.10, shaft_radius=0.04)
        plotter.add_mesh(arrow, color=cmap(norm(float(spd)))[:3])


def populate_pass1_3d(plotter, dem, hazard=None, winds=None, crs=None,
                      basemap_source: str = "IGN plan"):
    """Pass-1 screening in 3D: the zone terrain draped with a basemap, the hazard field (if
    given) as a translucent coloured overlay (candidate zones), per-zone wind arrows and a north
    arrow. The 3D twin of viz.map2d, on the real relief. ``hazard=None`` shows the bare relief
    (e.g. before a criblage). The caller sets the camera (no view reset here, to keep it on
    hour-scrub re-renders)."""
    import pyvista as pv

    from .map2d import DISCLAIMER

    terrain = _terrain_mesh(dem)
    if not (crs is not None and _drape_basemap(plotter, terrain, crs, basemap_source)):
        terrain["elevation_m"] = terrain.points[:, 2]
        plotter.add_mesh(terrain, scalars="elevation_m", cmap="gist_earth",
                         show_scalar_bar=False, reset_camera=False)

    if hazard is not None:
        b = terrain.bounds
        overlay = _terrain_mesh(dem, lift=max(0.012 * (b[5] - b[4]), 8.0))
        tex = pv.numpy_to_texture(_hazard_texture_image(hazard))
        ob = overlay.bounds
        overlay.texture_map_to_plane(
            origin=(ob[0], ob[2], 0.0), point_u=(ob[1], ob[2], 0.0), point_v=(ob[0], ob[3], 0.0),
            inplace=True)
        plotter.add_mesh(overlay, texture=tex, reset_camera=False)

    if winds:
        _add_wind_arrows_3d(plotter, terrain, winds)
    _add_north_arrow(plotter, terrain)

    plotter.add_text("Pass 1 — zones candidates (drapé 3D)", font_size=9)
    plotter.add_text(DISCLAIMER, position="lower_left", font_size=8)
    plotter.show_axes()
    return plotter


def _add_rotor(plotter, rev, terrain, show_legend: bool = True, clim=None) -> None:
    """Add the reversed-flow (rotor) volume coloured by HEIGHT ABOVE GROUND (yellow near the
    ground -> red -> purple high up) with OPACITY proportional to rotor intensity.

    ``clim=(lo, hi)`` forces the height scale (so an aggregate of many sub-zone rotors shares one
    consistent scale + legend); ``show_legend=False`` skips the per-rotor scalar bar (the
    aggregate adds a single legend itself)."""
    from matplotlib.colors import LinearSegmentedColormap, Normalize
    from scipy.spatial import cKDTree

    centers = rev.cell_centers().points
    tpts = np.asarray(terrain.points)
    idx = cKDTree(tpts[:, :2]).query(centers[:, :2])[1]
    hagl = centers[:, 2] - tpts[idx, 2]  # height above the terrain
    along = rev.cell_data.get("along_flow")
    intensity = np.clip(-np.asarray(along), 0.0, None) if along is not None else np.ones(len(centers))

    cmap = LinearSegmentedColormap.from_list("yrp", ["#ffff00", "#ff2a00", "#7a00b0"])
    lo, hi = clim if clim is not None else (np.nanpercentile(hagl, 5), np.nanpercentile(hagl, 95))
    hi = max(hi, lo + 1e-6)
    rgb = cmap(np.clip(Normalize(lo, hi)(hagl), 0, 1))[:, :3]
    imax = max(float(np.nanpercentile(intensity, 95)), 1e-6)
    alpha = 0.12 + 0.85 * np.clip(intensity / imax, 0.0, 1.0)  # weak transparent -> strong opaque
    rev.cell_data["rotor_rgba"] = (np.c_[rgb, alpha] * 255).astype(np.uint8)
    plotter.add_mesh(rev, scalars="rotor_rgba", rgba=True, reset_camera=False)
    if not show_legend:
        return

    # Legend for the colour -> height-above-ground (m) mapping. The rotor itself is drawn with
    # raw RGBA (colour=height, opacity=intensity), which carries no scalar bar, so add a tiny
    # invisible proxy carrying the [lo, hi] range + the same colormap to render the bar.
    import pyvista as pv

    seed = centers[:2] if len(centers) >= 2 else np.zeros((2, 3))
    proxy = pv.PolyData(np.asarray(seed, dtype="float64"))
    proxy["Hauteur sol (m)"] = np.array([lo, hi], dtype="float64")
    plotter.add_mesh(
        proxy, scalars="Hauteur sol (m)", cmap=cmap, clim=(lo, hi), style="points",
        point_size=1.0, opacity=0.0, reset_camera=False, show_scalar_bar=True,
        scalar_bar_args=dict(title="Hauteur sol (m)", n_labels=5, fmt="%.0f", vertical=True,
                             title_font_size=14, label_font_size=12, position_x=0.86,
                             position_y=0.30, width=0.10, height=0.45),
    )


def _clip_domain_boundary(rev, mesh, aoi_bounds=None, lateral_frac: float = 0.08,
                          lid_frac: float = 0.12):
    """Drop reversed-flow cells hugging the momentum domain's boundaries.

    The solver's boundaries induce spurious reversed/stagnant flow: chiefly at the **lateral
    edges** — a lee reaching the N/S/E/W boundary gets deflected UP, so the rotor seems to
    "climb the map edge" (the user's "ça bute contre le bord") — and a little under the top lid.
    When ``aoi_bounds`` = (xmin, xmax, ymin, ymax) is given (the drawn zone, which the momentum
    domain was buffered around), keep only cells **inside the drawn zone** — the boundary
    artifacts live in the buffer outside it. Otherwise fall back to a fixed ``lateral_frac``
    margin. Always drops the top ``lid_frac``. Returns the input unchanged if clipping empties
    it (don't blank the view)."""
    if not rev.n_cells:
        return rev
    b = mesh.bounds  # xmin, xmax, ymin, ymax, zmin, zmax
    if aoi_bounds is not None:
        x0, x1, y0, y1 = aoi_bounds
    else:
        mx, my = lateral_frac * (b[1] - b[0]), lateral_frac * (b[3] - b[2])
        x0, x1, y0, y1 = b[0] + mx, b[1] - mx, b[2] + my, b[3] - my
    lid = b[5] - lid_frac * (b[5] - b[4])
    c = rev.cell_centers().points
    keep = ((c[:, 0] > x0) & (c[:, 0] < x1) & (c[:, 1] > y0) & (c[:, 1] < y1) & (c[:, 2] < lid))
    if not keep.any():
        return rev
    out = rev.extract_cells(np.nonzero(keep)[0])
    return out if out.n_cells else rev


def _add_compass(plotter, terrain, mean_flow_dir, wind_speed_ms=None, wind_from_deg=None):
    """Add an info compass above the terrain: a NORTH arrow (+Y, dark) and a LOCAL-WIND arrow
    (blue, pointing where the wind blows TO) labelled with speed + meteo direction."""
    import pyvista as pv

    b = terrain.bounds  # xmin, xmax, ymin, ymax, zmin, zmax
    dx, dy, dz = b[1] - b[0], b[3] - b[2], b[5] - b[4]
    length = 0.22 * min(dx, dy)
    base = (b[0] + 0.14 * dx, b[2] + 0.14 * dy, b[5] + 0.30 * max(dz, length))

    pts, labels = [], []
    north = pv.Arrow(start=base, direction=(0.0, 1.0, 0.0), scale=length,
                     tip_length=0.28, tip_radius=0.09, shaft_radius=0.035)
    plotter.add_mesh(north, color="#222222")
    pts.append((base[0], base[1] + length * 1.15, base[2]))
    labels.append("N")

    wd = np.asarray(mean_flow_dir, dtype="float64") if mean_flow_dir is not None else None
    if wd is not None and np.linalg.norm(wd) > 1e-9:
        wd = wd / np.linalg.norm(wd)
        wind = pv.Arrow(start=base, direction=tuple(wd), scale=length,
                        tip_length=0.28, tip_radius=0.09, shaft_radius=0.035)
        plotter.add_mesh(wind, color="#1565c0")
        txt = "vent"
        if wind_speed_ms is not None:
            txt = f"vent {wind_speed_ms:.0f} m/s"
            if wind_from_deg is not None:
                txt += f" · {wind_from_deg:.0f}°"
        pts.append((base[0] + wd[0] * length * 1.15, base[1] + wd[1] * length * 1.15,
                    base[2] + wd[2] * length * 1.15))
        labels.append(txt)

    plotter.add_point_labels(pts, labels, font_size=12, text_color="black",
                             shape_color="white", shape_opacity=0.55, always_visible=True)


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
    wind_speed_ms=None,
    wind_from_deg=None,
    aoi_bounds=None,
):
    """Add the Pass-2 scene to an EXISTING plotter (standalone Plotter or embedded
    QtInteractor). Terrain is draped with a basemap (if ``crs`` is given) instead of an
    elevation colormap; the rotor is coloured by height-above-ground with opacity ∝ intensity.
    A north arrow + a local-wind arrow (speed/direction) are added as orientation cues.
    ``aoi_bounds`` (xmin, xmax, ymin, ymax) clips the rotor back to the drawn zone (the solve was
    buffered around it), keeping the boundary artifacts out of the result.
    """
    mesh = ofr.read_case(case_dir)
    terrain = ofr.read_terrain_stl(case_dir)

    if terrain is not None and terrain.n_points:
        if not (crs is not None and _drape_basemap(plotter, terrain, crs, basemap_source)):
            terrain["elevation_m"] = terrain.points[:, 2]
            plotter.add_mesh(terrain, scalars="elevation_m", cmap="gist_earth",
                             show_scalar_bar=False)
        _add_compass(plotter, terrain, mean_flow_dir, wind_speed_ms, wind_from_deg)

    if show_streamlines:
        lines = _seed_streamlines(mesh, mean_flow_dir)
        if lines is not None:
            plotter.add_mesh(lines.tube(radius=max(lines.length / 1500.0, 1.0)),
                             color="white", opacity=0.5)

    if show_reversed_flow:
        mesh["along_flow"] = ofr.along_flow_component(mesh, mean_flow_dir)
        rev = mesh.threshold(value=0.0, scalars="along_flow", invert=True)
        # Drop the spurious rotor hugging the domain boundaries: clip to the drawn zone (the
        # solve was buffered, so the downwind-edge artifact is outside it), + the lid.
        rev = _clip_domain_boundary(rev, mesh, aoi_bounds=aoi_bounds)
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
    wind_speed_ms=None,
    wind_from_deg=None,
    aoi_bounds=None,
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
        wind_speed_ms=wind_speed_ms, wind_from_deg=wind_from_deg, aoi_bounds=aoi_bounds,
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
