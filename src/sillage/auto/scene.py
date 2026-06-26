"""Aggregate the auto feature cases of ONE hour into a single 3D scene.

Reuses the Pass-2 building blocks (``viz.volume3d``): drape the fine zone DEM once, then overlay
each feature's rotor (reversed-flow volume), **clipped to its own zone bounds** so the buffered
boundaries don't leak in (ADR-0021). The window's hour slider just re-calls this with the cases
of the selected hour. See docs/10_auto_pipeline.md.
"""

from __future__ import annotations


def extract_rotor(case_dir: str, wind_from_deg: float, aoi_bounds):
    """Read an OpenFOAM momentum case and return the **clipped reversed-flow (rotor) mesh** —
    the only thing the 3D scene draws. Carries ``along_flow`` (cell data) so ``_add_rotor`` can
    colour it. Returns ``None`` if empty/unreadable.

    This is the heavy step (reading the full OpenFOAM field). The auto pipeline calls it once,
    right after each solve, to persist this small mesh and then **delete the bulky case** (so
    disk doesn't fill with NINJAFOAM_* cases in the optional low-disk mode — see pipeline
    ``_compact_case``). Normal UI runs keep full cases until window close."""
    from ..flow import openfoam_reader as ofr
    from ..viz import volume3d as v3

    mesh = ofr.read_case(case_dir)
    mesh["along_flow"] = ofr.along_flow_component(mesh, v3.mean_flow_vector(wind_from_deg))
    rev = mesh.threshold(value=0.0, scalars="along_flow", invert=True)
    return v3._clip_domain_boundary(rev, mesh, aoi_bounds=aoi_bounds, keep_if_empty=False)


def _add_domain_box(plotter, terrain, aoi_bounds, color: str = "#10c0ff") -> None:
    """Draw the analysed feature domain (the un-buffered zone the rotor is clipped to) as a thin
    rectangle floating just above the local terrain — so the per-feature sub-domains are visible
    instead of one anonymous big rectangle."""
    import numpy as np
    import pyvista as pv

    x0, x1, y0, y1 = aoi_bounds
    pts = np.asarray(terrain.points)
    inb = (pts[:, 0] >= x0) & (pts[:, 0] <= x1) & (pts[:, 1] >= y0) & (pts[:, 1] <= y1)
    zb = terrain.bounds
    z = (float(pts[inb, 2].max()) if inb.any() else float(zb[5])) + 0.03 * (zb[5] - zb[4]) + 25.0
    ring = np.array([[x0, y0, z], [x1, y0, z], [x1, y1, z], [x0, y1, z], [x0, y0, z]])
    plotter.add_mesh(pv.lines_from_points(ring), color=color, line_width=3, reset_camera=False)


def populate_auto_scene(plotter, dem, cases, crs=None, basemap_source: str = "IGN plan",
                        route_winds=None):
    """Add the global wake scene for one hour to ``plotter``.

    ``dem`` is the full fine zone DEM (terrain), ``cases`` the :class:`auto.pipeline.CaseResult`
    objects for the chosen hour. ``route_winds`` (optional) is ``[(x, y, speed_ms, from_deg), …]``
    in the DEM CRS — the AROME wind sampled along the route at this hour, drawn as arrows. The
    caller sets the camera (no view reset here)."""
    from ..viz import volume3d as v3

    terrain = v3._terrain_mesh(dem)
    if not (crs is not None and v3._drape_basemap(plotter, terrain, crs, basemap_source)):
        terrain["elevation_m"] = terrain.points[:, 2]
        plotter.add_mesh(terrain, scalars="elevation_m", cmap="gist_earth",
                         show_scalar_bar=False, reset_camera=False)

    rendered = 0
    for case in cases:
        rev = None
        rotor_path = getattr(case, "rotor_path", "")
        if rotor_path:  # compacted: read the small persisted rotor mesh directly
            try:
                import pyvista as pv

                rev = pv.read(rotor_path)
            except Exception:
                rev = None
        if rev is None and case.case_dir:  # not compacted yet: extract from the OpenFOAM case
            try:
                rev = extract_rotor(case.case_dir, case.wind_from_deg, case.aoi_bounds)
            except Exception:
                rev = None  # a missing/failed case shouldn't blank the whole scene
        _add_domain_box(plotter, terrain, case.aoi_bounds)  # show the analysed feature sub-domain
        if rev is not None and rev.n_cells:
            # One shared legend (on the first rotor); TODO: a global height clim across zones.
            v3._add_rotor(plotter, rev, terrain, show_legend=(rendered == 0))
            rendered += 1

    if route_winds:  # AROME wind sampled along the route, at the rendered hour
        v3._add_wind_arrows_3d(plotter, terrain, route_winds)
    v3._add_north_arrow(plotter, terrain)
    v3._add_horizontal_scale_bar(plotter, terrain)
    plotter.add_text("Pass 2 — sillage global (auto)", font_size=9)
    plotter.add_text(v3.SCENE_TEXT, position="lower_left", font_size=8)
    plotter.show_axes()
    return plotter
