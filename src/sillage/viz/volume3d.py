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


def _drape_basemap(plotter, terrain, crs, source: str, zoom_boost: int = 0,
                   texture_cache=None) -> bool:
    """Texture-drape a web-tile basemap (top-down) onto the terrain surface. Returns success.

    Needs the terrain CRS (UTM) to fetch the basemap for the right lon/lat area. Tiles arrive in
    WebMercator, so reproject the RGB raster to the terrain CRS before texturing; directly
    stretching WebMercator pixels onto UTM can show as a north/south offset on larger AOIs.

    ``zoom_boost`` adds web-tile zoom levels above contextily's auto pick (each +1 ≈ 2× ground
    resolution) so the drape is sharp enough to inspect lee-zone detail when the camera zooms in.
    ``texture_cache`` (a dict) memoises the built texture by extent/source/zoom so hour-scrub and
    threshold re-renders reuse it instead of re-fetching tiles (the slow part).
    """
    try:
        import pyvista as pv

        from .map2d import BASEMAP_SOURCES

        if source not in BASEMAP_SOURCES:
            return False
        b = terrain.bounds
        xmin, xmax, ymin, ymax = b[0], b[1], b[2], b[3]
        key = (round(xmin), round(ymin), round(xmax), round(ymax), source, int(zoom_boost))
        tex = texture_cache.get(key) if texture_cache is not None else None
        if tex is None:
            import contextily as cx
            from rasterio.crs import CRS as RCRS
            from rasterio.enums import Resampling
            from rasterio.transform import from_bounds
            from rasterio.warp import reproject
            from rasterio.warp import transform as warp_xy

            dst_crs = RCRS.from_user_input(crs)
            lons, lats = warp_xy(dst_crs, RCRS.from_epsg(4326),
                                 [xmin, xmin, xmax, xmax], [ymin, ymax, ymin, ymax])
            w, e = min(lons), max(lons)
            s, n = min(lats), max(lats)
            family, layer = BASEMAP_SOURCES[source]
            prov = getattr(cx.providers, family)
            if layer is not None:
                prov = prov[layer]
            zoom = "auto"
            if zoom_boost:  # sharper tiles for lee detail (capped to the provider max)
                try:
                    from contextily.tile import _calculate_zoom

                    zmax = int(prov.get("max_zoom", 19)) if hasattr(prov, "get") else 19
                    zoom = min(_calculate_zoom(w, s, e, n) + int(zoom_boost), zmax)
                except Exception:
                    zoom = "auto"
            img, ext3857 = cx.bounds2img(w, s, e, n, zoom=zoom, source=prov, ll=True)
            # Keep the tile mosaic north-up (row 0 = north): texture_map_to_plane maps array row 0
            # to the north (point_v) edge, so NO vertical flip (flipping renders it upside down).
            img = np.ascontiguousarray(img[:, :, :3])
            h, ww = img.shape[:2]
            src_transform = from_bounds(ext3857[0], ext3857[2], ext3857[1], ext3857[3], ww, h)
            dst_transform = from_bounds(xmin, ymin, xmax, ymax, ww, h)
            warped = np.zeros((h, ww, 3), dtype=np.uint8)
            for band in range(3):
                reproject(
                    img[:, :, band], warped[:, :, band],
                    src_transform=src_transform, src_crs=RCRS.from_epsg(3857),
                    dst_transform=dst_transform, dst_crs=dst_crs,
                    resampling=Resampling.bilinear,
                )
            tex = pv.numpy_to_texture(np.ascontiguousarray(warped))
            if texture_cache is not None:
                texture_cache[key] = tex
        terrain.texture_map_to_plane(
            origin=(xmin, ymin, 0.0), point_u=(xmax, ymin, 0.0), point_v=(xmin, ymax, 0.0),
            inplace=True)
        plotter.add_mesh(terrain, texture=tex)
        return True
    except Exception:
        return False


def _terrain_mesh(dem, lift: float = 0.0):
    """A PyVista StructuredGrid from a north-up DEM (row 0 = north), optionally lifted in Z.

    DEM samples are cell-centre elevations. Put the 3D points at pixel centres, not at the outer
    raster bounds, otherwise the relief is slightly stretched and shifted against rasters/textures.
    """
    import pyvista as pv

    z = np.array(dem.elevation, dtype="float64")
    if not np.isfinite(z).all():
        fill = float(np.nanmin(z)) if np.isfinite(z).any() else 0.0
        z = np.where(np.isfinite(z), z, fill)
    left, bottom, right, top = dem.bounds
    ny, nx = z.shape
    res_x = (right - left) / max(nx, 1)
    res_y = (top - bottom) / max(ny, 1)
    xs = left + (np.arange(nx) + 0.5) * res_x
    ys = top - (np.arange(ny) + 0.5) * res_y  # row 0 -> north (top)
    xx, yy = np.meshgrid(xs, ys)
    return pv.StructuredGrid(xx, yy, z + lift)


_NICE_KM = (1, 2, 3, 5, 10, 20, 30, 50, 100, 200, 500)


def _nice_scale_length_m(avail_m: float) -> float:
    """A **whole number of km** (1/2/3/5/10…), the largest that fits in ``avail_m`` (≥ 1 km)."""
    avail_km = max(1.0, avail_m / 1000.0)
    length_km = 1
    for km in _NICE_KM:
        if km <= avail_km:
            length_km = km
    return float(length_km * 1000)


def _scale_label(length_m: float) -> str:
    return f"{int(round(length_m / 1000.0))} km"


def _add_horizontal_scale_bar(plotter, terrain) -> None:
    """Floating horizontal scale bar — always a whole number of km (terrain CRS units = metres)."""
    import pyvista as pv

    b = terrain.bounds
    dx, dy, dz = b[1] - b[0], b[3] - b[2], b[5] - b[4]
    length = _nice_scale_length_m(dx * 0.35)  # largest whole-km bar fitting ~a third of the width
    x0 = b[0] + 0.10 * dx
    y0 = b[2] + 0.08 * dy
    z = b[5] + 0.12 * max(dz, length * 0.12) + 20.0
    pts = np.array([[x0, y0, z], [x0 + length, y0, z]])
    plotter.add_mesh(pv.lines_from_points(pts), color="black", line_width=5, reset_camera=False)
    tick = max(length * 0.035, 20.0)
    for x in (x0, x0 + length):
        tpts = np.array([[x, y0 - tick, z], [x, y0 + tick, z]])
        plotter.add_mesh(pv.lines_from_points(tpts), color="black", line_width=5,
                         reset_camera=False)
    plotter.add_point_labels(
        [(x0 + length / 2.0, y0 + tick * 2.0, z)],
        [_scale_label(length)],
        font_size=11, text_color="black", shape_color="white", shape_opacity=0.65,
        always_visible=True,
    )


def _pan_camera(plotter, dx_px: float, dy_px: float) -> None:
    """Translate the camera (and its focal point) in the view plane by a pixel drag — a "grab and
    drag" pan. Pixel→world scale uses the focal-plane distance and the vertical field of view."""
    cam = plotter.camera
    pos = np.asarray(cam.position, dtype="float64")
    fp = np.asarray(cam.focal_point, dtype="float64")
    up = np.asarray(cam.up, dtype="float64")
    fwd = fp - pos
    dist = float(np.linalg.norm(fwd)) or 1.0
    fwd /= dist
    right = np.cross(fwd, up)
    rn = float(np.linalg.norm(right))
    right = right / rn if rn else np.array([1.0, 0.0, 0.0])
    true_up = np.cross(right, fwd)
    h_px = max(1, int(plotter.window_size[1]))
    world_per_px = 2.0 * dist * np.tan(np.radians(float(cam.view_angle)) / 2.0) / h_px
    shift = (-dx_px * world_per_px) * right + (-dy_px * world_per_px) * true_up
    cam.position = tuple(pos + shift)
    cam.focal_point = tuple(fp + shift)


def enable_right_drag_pan(plotter):
    """Add RIGHT-button drag panning (translation) on top of the terrain-style left-drag rotation.

    Terrain style locks rotation to azimuth/elevation but only pans with middle-drag / Shift+left;
    pilots expect a plain right-drag "grab". We observe the interactor directly (so this works under
    any interactor style) and abort the right-button events so the style's own right binding (zoom)
    doesn't fight the pan. Left-drag rotation is untouched (we only abort MouseMove while panning).
    Returns the observer state (kept alive by the caller) or None if the interactor is unavailable.
    """
    try:
        interactor = plotter.iren.interactor  # the raw vtkRenderWindowInteractor
    except Exception:
        return None
    st: dict = {"on": False, "last": None, "ids": {}}

    def _abort(caller, key):
        cid = st["ids"].get(key)
        cmd = caller.GetCommand(cid) if cid is not None else None
        if cmd is not None:
            cmd.SetAbortFlag(1)

    def _press(caller, _ev):
        st["on"] = True
        st["last"] = interactor.GetEventPosition()
        _abort(caller, "press")

    def _release(caller, _ev):
        st["on"] = False
        st["last"] = None
        _abort(caller, "release")

    def _move(caller, _ev):
        if not st["on"] or st["last"] is None:
            return  # not panning -> let the style handle the move (left-drag rotation)
        x, y = interactor.GetEventPosition()
        lx, ly = st["last"]
        st["last"] = (x, y)
        try:
            _pan_camera(plotter, x - lx, y - ly)
            plotter.render()
        except Exception:
            pass
        _abort(caller, "move")

    st["ids"]["press"] = interactor.AddObserver("RightButtonPressEvent", _press, 10.0)
    st["ids"]["release"] = interactor.AddObserver("RightButtonReleaseEvent", _release, 10.0)
    st["ids"]["move"] = interactor.AddObserver("MouseMoveEvent", _move, 10.0)
    plotter._right_drag_pan = st  # keep the closures alive for the plotter's lifetime
    return st


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


# Continuous wind-speed colour scale (km/h), 0 → WIND_VMAX_KMH, shared by the 2-D map arrows, the
# 3-D arrows and the legend. Green (calm) → red (40 km/h, the practical do-not-fly ceiling).
WIND_VMAX_KMH = 40.0
WIND_STOPS = ("#1a9850", "#a6d96a", "#ffcc00", "#fb8c2a", "#d73027")  # green→red, vivid mid


def _wind_cmap():
    from matplotlib.colors import LinearSegmentedColormap

    return LinearSegmentedColormap.from_list("wind", WIND_STOPS)


def wind_color(speed_kmh: float) -> str:
    """Hex colour for a wind speed (km/h) on the continuous 0–``WIND_VMAX_KMH`` scale (clamped)."""
    t = min(max(float(speed_kmh) / WIND_VMAX_KMH, 0.0), 1.0)
    r, g, b, _a = _wind_cmap()(t)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


def wind_legend_image(vmax: float = WIND_VMAX_KMH, n: int = 256):
    """Horizontal continuous wind-speed colourbar (0→``vmax`` km/h) as an RGBA uint8 array."""
    grad = _wind_cmap()(np.linspace(0.0, 1.0, n))[:, :3]
    img = np.tile(grad[None, :, :], (8, 1, 1))
    fig = _agg_pyplot().figure(figsize=(2.5, 0.8), dpi=100)
    ax = fig.add_axes([0.08, 0.42, 0.88, 0.30])
    ax.imshow(img, origin="lower", aspect="auto", extent=[0.0, float(vmax), 0.0, 1.0])
    ax.set_yticks([])
    ax.set_xlabel("Vent (km/h)", fontsize=8)
    ax.tick_params(labelsize=7)
    return _fig_to_rgba(fig)


def _add_wind_arrows_3d(plotter, terrain, winds):
    """One wind arrow per cell, above the terrain, coloured by the continuous wind scale. Returns
    ``[(actor, origin), …]`` so ``enable_wind_arrow_autoscale`` can keep them ~constant on screen."""
    import pyvista as pv
    from scipy.spatial import cKDTree

    tpts = np.asarray(terrain.points)
    tree = cKDTree(tpts[:, :2])
    b = terrain.bounds
    side = max(1, round(len(winds) ** 0.5))
    length = min(0.06 * min(b[1] - b[0], b[3] - b[2]),
                 0.32 * min(b[1] - b[0], b[3] - b[2]) / side)
    lift = 0.04 * (b[5] - b[4]) + 10.0
    actors = []
    for x, y, spd, drc in winds:
        zi = float(tpts[tree.query([x, y])[1], 2])
        blow = np.deg2rad((float(drc) + 180.0) % 360.0)
        d = (float(np.sin(blow)), float(np.cos(blow)), 0.0)
        origin = (float(x), float(y), zi + lift)
        start = (x - d[0] * length / 2, y - d[1] * length / 2, zi + lift)
        arrow = pv.Arrow(start=start, direction=d, scale=length,
                         tip_length=0.30, tip_radius=0.10, shaft_radius=0.04)
        actor = plotter.add_mesh(arrow, color=wind_color(float(spd) * 3.6))
        actors.append((actor, origin))
    return actors


def _camera_screen_metric(camera):
    """A scalar ∝ world-units-per-screen-pixel for the camera: ``parallel_scale`` in parallel
    projection, else ``distance × tan(view_angle/2)`` (perspective). Wheel-zoom changes the view
    angle (not the distance), so this captures zoom AND dolly/pan/tilt — unlike distance alone."""
    if getattr(camera, "parallel_projection", False):
        return max(float(camera.parallel_scale), 1e-6)
    pos = np.asarray(camera.position, dtype="float64")
    fp = np.asarray(camera.focal_point, dtype="float64")
    dist = float(np.linalg.norm(pos - fp)) or 1.0
    return max(dist * np.tan(np.radians(float(camera.view_angle)) / 2.0), 1e-6)


def baseline_wind_autoscale(plotter):
    """Capture the current view as the wind-arrow autoscale baseline (factor 1) and reset arrows to
    their built size. Call right after a render once the camera is in place."""
    try:
        plotter._wind_ref_metric = _camera_screen_metric(plotter.camera)
        for actor, _origin in getattr(plotter, "_wind_arrows", None) or []:
            actor.SetScale(1.0, 1.0, 1.0)
    except Exception:
        pass


def enable_wind_arrow_autoscale(plotter):
    """Keep 3-D wind arrows ~constant on screen: on each **discrete** view change (drag-end + mouse
    wheel) rescale them by the on-screen metric relative to ``plotter._wind_ref_metric`` (set by
    ``baseline_wind_autoscale`` after a render). Discrete events only — not the camera ModifiedEvent,
    which fires on every intermediate state during a reset and could collapse the arrows."""
    try:
        interactor = plotter.iren.interactor
    except Exception:
        return

    def _rescale(*_a):
        arrows = getattr(plotter, "_wind_arrows", None)
        ref = getattr(plotter, "_wind_ref_metric", None)
        if not arrows or not ref:
            return
        f = float(max(0.15, min(8.0, _camera_screen_metric(plotter.camera) / ref)))
        for actor, origin in arrows:
            try:
                actor.SetOrigin(*origin)
                actor.SetScale(f, f, f)
            except Exception:
                pass
        try:
            plotter.render()
        except Exception:
            pass

    for ev in ("EndInteractionEvent", "MouseWheelForwardEvent", "MouseWheelBackwardEvent"):
        try:
            interactor.AddObserver(ev, _rescale, -1.0)  # after the style applies the zoom/move
        except Exception:
            pass
    plotter._wind_autoscale = _rescale  # keep alive


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
    _add_horizontal_scale_bar(plotter, terrain)

    plotter.add_text("Pass 1 — zones candidates (drapé 3D)", font_size=9)
    plotter.add_text(DISCLAIMER, position="lower_left", font_size=8)
    plotter.show_axes()
    return plotter


def _rotor_intensity_cmap():
    from matplotlib.colors import LinearSegmentedColormap

    return LinearSegmentedColormap.from_list("rotor_intensity", ["#ffff66", "#ff8c00", "#7a00b0"])


def _wind_balance_cmap():
    from matplotlib.colors import LinearSegmentedColormap

    return LinearSegmentedColormap.from_list("wind_balance", ["#d73027", "#fff7bc", "#2ca02c"])


def _vertical_motion_cmap():
    from matplotlib.colors import LinearSegmentedColormap

    return LinearSegmentedColormap.from_list("vertical_motion", ["#b2182b", "#fff7bc", "#1a9850"])


def range_legend_image(vmin: float, vmax: float, cmap, label: str, title: str,
                       *, center: float | None = None, n: int = 256):
    """Horizontal scalar colourbar. If ``center`` is given, it stays at the middle colour."""
    import matplotlib
    from matplotlib.colors import Normalize, TwoSlopeNorm

    cm = matplotlib.colormaps[cmap] if isinstance(cmap, str) else cmap
    vmin, vmax = float(vmin), float(vmax)
    if center is not None:
        center = float(center)
        vmin = min(vmin, center - 1e-6)
        vmax = max(vmax, center + 1e-6)
        norm = TwoSlopeNorm(vmin=vmin, vcenter=center, vmax=vmax)
    else:
        vmax = max(vmax, vmin + 1e-6)
        norm = Normalize(vmin=vmin, vmax=vmax)
    vals = np.linspace(vmin, vmax, n)
    grad = cm(np.clip(norm(vals), 0.0, 1.0))[:, :3]
    img = np.tile(grad[None, :, :], (10, 1, 1))

    plt = _agg_pyplot()
    fig = plt.figure(figsize=(2.5, 0.95), dpi=100)
    ax = fig.add_axes([0.10, 0.42, 0.86, 0.26])
    ax.imshow(img, origin="lower", aspect="auto", extent=[vmin, vmax, 0.0, 1.0])
    ax.set_yticks([])
    ax.set_xlabel(label, fontsize=8)
    ax.set_title(title, fontsize=8)
    ax.tick_params(labelsize=7)
    return _fig_to_rgba(fig)


def _agg_pyplot():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def _fig_to_rgba(fig):
    """Render a matplotlib figure to an ``(H, W, 4)`` uint8 RGBA array and close it (one place, so
    the DPI/backend/close-on-exit handling can't drift between the several legend builders)."""
    import matplotlib.pyplot as plt

    fig.canvas.draw()
    w, h = fig.canvas.get_width_height()
    buf = np.frombuffer(fig.canvas.buffer_rgba(), dtype=np.uint8).reshape(h, w, 4).copy()
    plt.close(fig)
    return buf


def _safe_p95(field) -> float:
    """95th percentile ignoring NaNs; a safe positive fallback when the field is empty/all-NaN (so
    the colour Normalize never gets a NaN vmax → grey/garbage volume)."""
    f = np.asarray(field, dtype="float64")
    f = f[np.isfinite(f)]
    return float(np.percentile(f, 95)) if f.size else 1.0


def set_rotor_opacity(plotter, alpha: float) -> bool:
    """Set the opacity of the lee-volume actors published on ``plotter._rotor_actors`` (live, no
    rebuild). Returns True if any actor exists. Both apps' opacity sliders call this, so the
    actor-opacity protocol lives in one place next to where ``_rotor_actors`` is created."""
    actors = getattr(plotter, "_rotor_actors", None)
    if not actors:
        return False
    a = float(np.clip(alpha, 0.02, 1.0))
    for act in actors:
        try:
            act.GetProperty().SetOpacity(a)
        except Exception:
            pass
    try:
        plotter.render()
    except Exception:
        pass
    return True


def _add_rotor(plotter, rev, terrain, opacity: float = 0.5,
               intensity_max: float | None = None, metric: str = "rotor", metric_range=None):
    """Colour the lee volume and add it. Returns the actor.

    Both apps use scalar colour ramps controlled by metric-specific sliders: rotor/turbulence
    min→max (upper values clamp), horizontal red→yellow→green centred on 0, and vertical
    red→pale-yellow→green centred on 0 with a hidden calm gap. Opacity is uniform + adjustable
    (actor-level, so a slider changes it live)."""
    from matplotlib.colors import Normalize, TwoSlopeNorm

    n_cells = rev.n_cells
    alpha = float(np.clip(opacity, 0.02, 1.0))
    metric_range = metric_range or {}

    def _paint(field, cmap, vmin, vmax, *, center=None):
        field = np.asarray(field, dtype="float64")
        if center is None:
            vmax2 = max(float(vmax), float(vmin) + 1e-6)
            norm = Normalize(float(vmin), vmax2)
        else:
            center2 = float(center)
            vmin2 = min(float(vmin), center2 - 1e-6)
            vmax2 = max(float(vmax), center2 + 1e-6)
            norm = TwoSlopeNorm(vmin=vmin2, vcenter=center2, vmax=vmax2)
        rgb = cmap(np.clip(norm(field), 0.0, 1.0))[:, :3]
        rev.cell_data["rotor_rgb"] = (rgb * 255).astype(np.uint8)
        return plotter.add_mesh(rev, scalars="rotor_rgb", rgb=True, opacity=alpha,
                                reset_camera=False)

    if metric == "horizontal":
        field = rev.cell_data.get("along_pct")
        field = np.asarray(field, dtype="float64") if field is not None else np.zeros(n_cells)
        vmin = metric_range.get("min", -(float(intensity_max) if intensity_max else 100.0))
        vmax = metric_range.get("max", float(intensity_max) if intensity_max else 100.0)
        actor = _paint(field, _wind_balance_cmap(), vmin, vmax, center=0.0)
    elif metric == "vertical":
        field = rev.cell_data.get("w_ms")
        field = np.asarray(field, dtype="float64") if field is not None else np.zeros(n_cells)
        vmax = float(intensity_max) if intensity_max else 3.0
        vmin = metric_range.get("sink_min", -vmax)
        vmax = metric_range.get("lift_max", vmax)
        actor = _paint(field, _vertical_motion_cmap(), vmin, vmax, center=0.0)
    elif metric == "turbulence":
        field = rev.cell_data.get("turb_rms")
        field = np.asarray(field, dtype="float64") if field is not None else np.zeros(n_cells)
        vmin = metric_range.get("min", 0.0)
        vmax = metric_range.get(
            "max", float(intensity_max) if intensity_max else _safe_p95(field))
        actor = _paint(field, _rotor_intensity_cmap(), vmin, vmax)
    else:
        along = rev.cell_data.get("along_flow")
        field = np.clip(-np.asarray(along), 0.0, None) if along is not None else np.ones(n_cells)
        vmin = metric_range.get("min", 0.0)
        vmax = metric_range.get(
            "max", float(intensity_max) if intensity_max else _safe_p95(field))
        actor = _paint(field, _rotor_intensity_cmap(), vmin, vmax)
    return actor


LEE_METRICS = ("rotor", "horizontal", "vertical", "turbulence")
DEFAULT_VOL_FLOORS = {"rotor": 0.0, "horizontal": 50.0, "vertical": 1.0, "turbulence": 1.0}
LEE_SOURCE_ARRAYS = ("along_flow", "along_pct", "w_ms", "w_abs", "turb_rms")


def _compute_lee_scalars(mesh, mean_flow_dir, ref_speed_ms) -> None:
    """Attach the lee cell scalars to ``mesh`` (computed once, then thresholded per metric):
    ``along_flow`` (m/s), ``along_pct`` (% of upstream wind, signed: −100 reversal → +100 free-stream),
    ``w_ms`` (vertical velocity, signed) + ``w_abs``, ``turb_rms`` = √(2k/3) [m/s] (turbulence rms —
    an ABSOLUTE field, comparable across sub-domains regardless of their wind)."""
    along = np.asarray(ofr.along_flow_component(mesh, mean_flow_dir), dtype="float64")
    mesh["along_flow"] = along
    mesh["along_pct"] = along / max(float(ref_speed_ms or 0.0), 0.1) * 100.0
    try:
        w = np.asarray(ofr.velocity(mesh), dtype="float64")[:, 2]
        mesh["w_ms"] = w
        mesh["w_abs"] = np.abs(w)
    except Exception:
        pass
    try:
        rms = ofr.turbulence_intensity(mesh, reference_speed=1.0)  # ref 1 → √(2k/3) in m/s
        if rms is not None:
            mesh["turb_rms"] = np.asarray(rms, dtype="float64")
    except Exception:
        pass


def _cell_array(mesh, name: str):
    if name in mesh.cell_data:
        arr = np.asarray(mesh.cell_data[name])
    elif name in mesh.array_names:
        arr = np.asarray(mesh[name])
    else:
        return None
    return arr if len(arr) == mesh.n_cells else None


def _extract_cells_by_mask(mesh, mask):
    idx = np.nonzero(np.asarray(mask, dtype=bool))[0]
    return mesh.extract_cells(idx.astype(np.int64))


def _threshold_lee(mesh, metric, vol_floor, aoi_bounds, metric_range=None):
    vol = _threshold_lee_source(mesh, metric, vol_floor, metric_range=metric_range)
    if vol is None:
        return None
    return _clip_domain_boundary(vol, mesh, aoi_bounds=aoi_bounds, keep_if_empty=False)


def _threshold_lee_source(mesh, metric, vol_floor, metric_range=None):
    metric_range = metric_range or {}
    if metric_range:
        if metric == "turbulence":
            vals = _cell_array(mesh, "turb_rms")
            if vals is None:
                return None
            return _extract_cells_by_mask(mesh, vals >= float(metric_range.get("min", vol_floor)))
        if metric == "horizontal":
            vals = _cell_array(mesh, "along_pct")
            if vals is None:
                return None
            return _extract_cells_by_mask(mesh, vals <= float(metric_range.get("max", vol_floor)))
        if metric == "vertical":
            vals = _cell_array(mesh, "w_ms")
            if vals is None:
                return None
            sink_max = float(metric_range.get("sink_max", -abs(float(vol_floor))))
            lift_min = float(metric_range.get("lift_min", abs(float(vol_floor))))
            return _extract_cells_by_mask(mesh, (vals <= sink_max) | (vals >= lift_min))
        vals = _cell_array(mesh, "along_flow")
        if vals is None:
            return None
        intensity = -np.asarray(vals, dtype="float64")
        return _extract_cells_by_mask(
            mesh, (vals < 0.0) & (intensity >= float(metric_range.get("min", 0.0))))

    if metric == "turbulence":
        if "turb_rms" not in mesh.array_names:
            return None
        return mesh.threshold(value=float(vol_floor), scalars="turb_rms")            # rms ≥ floor m/s
    elif metric == "horizontal":
        if "along_pct" not in mesh.array_names:
            return None
        return mesh.threshold(value=float(vol_floor), scalars="along_pct", invert=True)  # ≤ floor %
    elif metric == "vertical":
        if "w_abs" not in mesh.array_names:
            return None
        return mesh.threshold(value=float(vol_floor), scalars="w_abs")               # |w| ≥ floor m/s
    if "along_flow" not in mesh.array_names:
        return None
    return mesh.threshold(value=0.0, scalars="along_flow", invert=True)              # reversed


def _slim_lee_source(mesh):
    """Keep only the scalar fields needed to re-threshold/re-render a saved lee source."""
    for name in list(mesh.cell_data.keys()):
        if name not in LEE_SOURCE_ARRAYS:
            del mesh.cell_data[name]
    for name in list(mesh.point_data.keys()):
        del mesh.point_data[name]
    for name in LEE_SOURCE_ARRAYS:
        if name in mesh.cell_data:
            mesh.cell_data[name] = np.asarray(mesh.cell_data[name], dtype=np.float32)
    return mesh


def extract_lee_volume(mesh, mean_flow_dir, *, metric: str = "rotor", ref_speed_ms=None,
                       vol_floor: float = 0.20, aoi_bounds=None, metric_range=None):
    """Compute the lee scalars then return the **clipped volume for ``metric``** (or ``None``).
    Shared by the auto scene and the manual app so both extract identically."""
    _compute_lee_scalars(mesh, mean_flow_dir, ref_speed_ms)
    return _threshold_lee(mesh, metric, vol_floor, aoi_bounds, metric_range=metric_range)


def extract_lee_source(mesh, mean_flow_dir, *, ref_speed_ms=None, aoi_bounds=None):
    """Return the clipped, threshold-independent lee source used by ``.sillage`` v2.

    It keeps all cells inside the displayed analysis domain (with the same boundary/lid trimming as
    rendered volumes) and only the derived scalar fields needed to rebuild any metric threshold.
    This is far smaller than a full OpenFOAM case but lets reopened bundles re-extract volumes.
    """
    _compute_lee_scalars(mesh, mean_flow_dir, ref_speed_ms)
    source = _clip_domain_boundary(mesh, mesh, aoi_bounds=aoi_bounds, keep_if_empty=False)
    if source is None or not source.n_cells:
        return None
    return _slim_lee_source(source)


def threshold_lee_source(source, *, metric: str = "rotor", vol_floor: float = 0.20,
                         metric_range=None):
    """Threshold a saved lee source without re-reading an OpenFOAM case."""
    vol = _threshold_lee_source(source, metric, vol_floor, metric_range=metric_range)
    return vol if vol is not None and vol.n_cells else None


def extract_lee_volumes(mesh, mean_flow_dir, *, ref_speed_ms=None, floors=None, aoi_bounds=None):
    """Compute the scalars ONCE and threshold **all metrics** → ``{metric: volume}`` (non-empty only).
    For persisting every representation from a single case read."""
    floors = floors or DEFAULT_VOL_FLOORS
    _compute_lee_scalars(mesh, mean_flow_dir, ref_speed_ms)
    out = {}
    for metric in LEE_METRICS:
        vol = _threshold_lee(mesh, metric, floors.get(metric, 0.0), aoi_bounds)
        if vol is not None and vol.n_cells:
            out[metric] = vol
    return out


def _clip_domain_boundary(rev, mesh, aoi_bounds=None, lateral_frac: float = 0.08,
                          lid_frac: float = 0.12, keep_if_empty: bool = True):
    """Drop reversed-flow cells hugging the momentum domain's boundaries.

    The solver's boundaries induce spurious reversed/stagnant flow: chiefly at the **lateral
    edges** — a lee reaching the N/S/E/W boundary gets deflected UP, so the rotor seems to
    "climb the map edge" (the user's "ça bute contre le bord") — and a little under the top lid.
    When ``aoi_bounds`` = (xmin, xmax, ymin, ymax) is given (the drawn zone, which the momentum
    domain was buffered around), keep only cells **inside the drawn zone** — the boundary
    artifacts live in the buffer outside it. Otherwise fall back to a fixed ``lateral_frac``
    margin. Always drops the top ``lid_frac``. By default returns the input unchanged if clipping
    empties it (don't blank the manual view). Auto compaction passes ``keep_if_empty=False`` so a
    rotor made only of boundary artifacts is stored as empty instead of resurrected."""
    if not rev.n_cells:
        return rev
    b = mesh.bounds  # xmin, xmax, ymin, ymax, zmin, zmax
    mx, my = lateral_frac * (b[1] - b[0]), lateral_frac * (b[3] - b[2])
    ix0, ix1, iy0, iy1 = b[0] + mx, b[1] - mx, b[2] + my, b[3] - my  # always drop a boundary band
    if aoi_bounds is not None:
        ax0, ax1, ay0, ay1 = aoi_bounds
        # keep cells inside the drawn zone AND off the solver boundary (the tighter of the two on
        # each side): a lee reaching the buffer edge can't "climb" the outlet/lateral boundary, yet
        # an edge feature whose zone hugs the boundary still gets its boundary band cut.
        x0, x1 = max(ax0, ix0), min(ax1, ix1)
        y0, y1 = max(ay0, iy0), min(ay1, iy1)
    else:
        x0, x1, y0, y1 = ix0, ix1, iy0, iy1
    lid = b[5] - lid_frac * (b[5] - b[4])
    c = rev.cell_centers().points
    keep = ((c[:, 0] > x0) & (c[:, 0] < x1) & (c[:, 1] > y0) & (c[:, 1] < y1) & (c[:, 2] < lid))
    if not keep.any():
        if not keep_if_empty:
            return rev.extract_cells(np.asarray([], dtype=np.int64))
        return rev
    out = rev.extract_cells(np.nonzero(keep)[0])
    if out.n_cells or not keep_if_empty:
        return out
    return rev


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
            txt = f"vent {wind_speed_ms * 3.6:.0f} km/h"
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
    zoom_boost: int = 0,
    opacity: float = 0.5,
    intensity_max=None,
    metric=None,
    vol_floor: float = 0.20,
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

    has_terrain = terrain is not None and terrain.n_points
    if has_terrain:
        if not (crs is not None
                and _drape_basemap(plotter, terrain, crs, basemap_source, zoom_boost)):
            terrain["elevation_m"] = terrain.points[:, 2]
            plotter.add_mesh(terrain, scalars="elevation_m", cmap="gist_earth",
                             show_scalar_bar=False)
        _add_compass(plotter, terrain, mean_flow_dir, wind_speed_ms, wind_from_deg)
        _add_horizontal_scale_bar(plotter, terrain)

    if show_streamlines:
        lines = _seed_streamlines(mesh, mean_flow_dir)
        if lines is not None:
            plotter.add_mesh(lines.tube(radius=max(lines.length / 1500.0, 1.0)),
                             color="white", opacity=0.5)

    captions = {
        "rotor": "Couleur = intensité du rotor (vitesse de flux inversé, m/s) · opacité réglable",
        "turbulence": "Couleur = turbulence rms √(2k/3) (m/s) · opacité réglable",
        "horizontal": "Couleur = vitesse horizontale (% vent) · rouge = rotor, vert = plein vent",
        "vertical": "Couleur = vitesse verticale · vert = ascendance, rouge = dégueulante",
    }
    rotor_actors = []  # published on the plotter so the app's opacity slider can update them live
    if metric is not None:  # unified metric path — shared with the auto app (identical rendering)
        vol = extract_lee_volume(mesh, mean_flow_dir, metric=metric, ref_speed_ms=wind_speed_ms,
                                 vol_floor=vol_floor, aoi_bounds=aoi_bounds)
        if vol is not None and vol.n_cells:
            if has_terrain:
                a = _add_rotor(plotter, vol, terrain, opacity=opacity,
                               intensity_max=intensity_max, metric=metric)
                if a is not None:
                    rotor_actors.append(a)
            else:
                rotor_actors.append(plotter.add_mesh(vol, color=REVERSED_COLOR, opacity=opacity))
        caption = captions.get(metric, "")
    else:  # legacy boolean path (build_scene / CLI snapshots): reversed-flow + optional turbulence
        mesh["along_flow"] = ofr.along_flow_component(mesh, mean_flow_dir)
        rms = ofr.turbulence_intensity(mesh, reference_speed=1.0)  # √(2k/3) m/s (what _add_rotor reads)
        if rms is not None:
            mesh["turb_rms"] = np.asarray(rms)
        if show_reversed_flow:
            rev = mesh.threshold(value=0.0, scalars="along_flow", invert=True)
            rev = _clip_domain_boundary(rev, mesh, aoi_bounds=aoi_bounds)
            if rev.n_cells and has_terrain:
                a = _add_rotor(plotter, rev, terrain, opacity=opacity, intensity_max=intensity_max,
                               metric="rotor")
                if a is not None:
                    rotor_actors.append(a)
            elif rev.n_cells:
                rotor_actors.append(plotter.add_mesh(rev, color=REVERSED_COLOR, opacity=opacity))
        if show_turbulence and "turb_rms" in mesh.array_names:
            turb = mesh.threshold(value=turbulence_threshold, scalars="turb_rms")
            turb = _clip_domain_boundary(turb, mesh, aoi_bounds=aoi_bounds)
            if turb.n_cells and has_terrain:
                a = _add_rotor(plotter, turb, terrain, opacity=opacity, intensity_max=intensity_max,
                               metric="turbulence")
                if a is not None:
                    rotor_actors.append(a)
            elif turb.n_cells:
                rotor_actors.append(plotter.add_mesh(turb, color=TURB_COLOR, opacity=0.35))
        caption = captions["rotor"]

    plotter.add_text(SCENE_TEXT, font_size=9)
    plotter.add_text(caption, position="lower_left", font_size=8)
    plotter.show_axes()
    plotter.view_isometric()
    plotter._rotor_actors = rotor_actors  # the manual app updates their opacity live (no rebuild)
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
