"""Aggregate the auto feature cases of ONE hour into a single 3D scene.

Reuses the Pass-2 building blocks (``viz.volume3d``): drape the fine zone DEM once, then overlay
each feature's rotor (reversed-flow volume), **clipped to its own zone bounds** so the buffered
boundaries don't leak in (ADR-0021). The window's hour slider just re-calls this with the cases
of the selected hour. See docs/10_auto_pipeline.md.
"""

from __future__ import annotations


def extract_volume(case_dir: str, wind_from_deg: float, aoi_bounds, *, metric: str = "rotor",
                   ref_speed_ms=None, vol_floor: float = 0.20):
    """Read an OpenFOAM momentum case and return the clipped 3-D **volume for ``metric``**:

      - ``"rotor"``       — reversed-flow recirculation (``along_flow`` < 0);
      - ``"horizontal"``  — flow slowed below ``vol_floor`` % of the upstream wind (incl. reversal);
      - ``"vertical"``    — strong vertical motion (|w| ≥ ``vol_floor`` m/s) — lift & sink;
      - ``"turbulence"``  — turbulent zone (turbulence intensity ≥ ``vol_floor``).

    Every volume carries ALL the cell scalars so ``_add_rotor`` can colour by any field without
    re-reading: ``along_flow`` (m/s), ``along_pct`` (% of upstream wind, signed: −100 reversal →
    +100 free-stream), ``w_ms`` (vertical velocity, signed), ``turb_intensity`` (√(2k/3)/U_ref).
    Returns ``None`` if empty/unavailable. ``vol_floor`` is in the metric's native unit.

    Thin wrapper over ``viz.volume3d.extract_lee_volume`` (shared with the manual app)."""
    from ..flow import openfoam_reader as ofr
    from ..viz import volume3d as v3

    mesh = ofr.read_case(case_dir)
    return v3.extract_lee_volume(mesh, v3.mean_flow_vector(wind_from_deg), metric=metric,
                                 ref_speed_ms=ref_speed_ms, vol_floor=vol_floor,
                                 aoi_bounds=aoi_bounds)


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
    plotter.add_mesh(pv.lines_from_points(ring), color=color, line_width=2, opacity=0.3,
                     reset_camera=False)  # faint, so the sectors don't dominate the render


def populate_auto_scene(plotter, dem, cases, crs=None, basemap_source: str = "IGN plan",
                        route_winds=None, basemap_zoom_boost: int = 2, rotor_cache=None,
                        rotor_opacity: float = 0.5, height_clim=None, intensity_max=None,
                        metric: str = "rotor", vol_floor: float = 0.20, texture_cache=None):
    """Add the global wake scene for one hour to ``plotter``.

    ``dem`` is the full fine zone DEM (terrain), ``cases`` the :class:`auto.pipeline.CaseResult`
    objects for the chosen hour. ``route_winds`` (optional) is ``[(x, y, speed_ms, from_deg), …]``
    in the DEM CRS — the AROME wind sampled along the route at this hour, drawn as arrows.
    ``basemap_zoom_boost`` sharpens the draped basemap (lee-zone detail). ``rotor_cache`` is an
    optional dict reused across hour scrubs so each case's rotor mesh is read only once. The
    caller sets the camera (no view reset here)."""
    from ..viz import volume3d as v3

    terrain = v3._terrain_mesh(dem)
    if not (crs is not None
            and v3._drape_basemap(plotter, terrain, crs, basemap_source, basemap_zoom_boost,
                                  texture_cache=texture_cache)):
        terrain["elevation_m"] = terrain.points[:, 2]
        plotter.add_mesh(terrain, scalars="elevation_m", cmap="gist_earth",
                         show_scalar_bar=False, reset_camera=False)

    # Sector centres (aoi midpoints) → each space point is drawn by its NEAREST sector only, so
    # overlapping sectors don't alpha-stack their opacity (which would fake a stronger rotor). The
    # transparency then reads as the true intensity. See ADR-0029 (overlap handling).
    import numpy as np

    centers = np.array([[(c.aoi_bounds[0] + c.aoi_bounds[1]) / 2.0,
                         (c.aoi_bounds[2] + c.aoi_bounds[3]) / 2.0] for c in cases])
    ctree = None
    if len(centers) > 1:
        try:
            from scipy.spatial import cKDTree

            ctree = cKDTree(centers)
        except Exception:
            ctree = None

    rendered = 0
    rotor_actors = []
    for i, case in enumerate(cases):
        # cache per (zone, hour, metric, floor) — each metric is a DIFFERENT volume
        key = (case.zone_index, case.hour, metric,
               round(float(vol_floor), 3) if metric != "rotor" else 0)
        rev = rotor_cache.get(key) if rotor_cache is not None else None
        if rev is None:
            # rotor has its own persisted .vtu; turbulence its own; the velocity fields are extracted
            # live (they reuse the rotor mesh's scalars only if it happens to cover them).
            path = (getattr(case, "rotor_path", "") if metric == "rotor"
                    else getattr(case, "turb_path", "") if metric == "turbulence" else "")
            if path:  # compacted/loaded: read the persisted volume for this metric
                try:
                    import pyvista as pv

                    rev = pv.read(path)
                except Exception:
                    rev = None
            if rev is None and case.case_dir:  # not compacted: extract from the OpenFOAM case
                try:
                    rev = extract_volume(case.case_dir, case.wind_from_deg, case.aoi_bounds,
                                         metric=metric, ref_speed_ms=case.wind_speed_ms,
                                         vol_floor=vol_floor)
                except Exception:
                    rev = None  # a missing/failed case shouldn't blank the whole scene
            if rotor_cache is not None and rev is not None:
                rotor_cache[key] = rev
        _add_domain_box(plotter, terrain, case.aoi_bounds)  # show the analysed sub-domain
        draw = rev
        if rev is not None and rev.n_cells and ctree is not None:
            # keep only cells this sector "owns" (its centre is the nearest) — no overlap double-draw
            cc = np.asarray(rev.cell_centers().points)[:, :2]
            owned = np.nonzero(ctree.query(cc)[1] == i)[0]
            draw = rev.extract_cells(owned) if len(owned) else None
        if draw is not None and draw.n_cells:
            # Shared absolute scales across sectors (clim + intensity_max) — the window draws the
            # 2-D legend, so no in-scene scalar bar here.
            actor = v3._add_rotor(plotter, draw, terrain, show_legend=False, opacity=rotor_opacity,
                                  clim=height_clim, intensity_max=intensity_max, metric=metric)
            if actor is not None:
                rotor_actors.append(actor)
            rendered += 1

    if route_winds:  # AROME wind sampled along the route, at the rendered hour
        plotter._wind_arrows = v3._add_wind_arrows_3d(plotter, terrain, route_winds)
        plotter._wind_ref_metric = None  # re-baseline the zoom-autoscale for this build
    else:
        plotter._wind_arrows = []
    v3._add_north_arrow(plotter, terrain)
    v3._add_horizontal_scale_bar(plotter, terrain)
    plotter.add_text("Pass 2 — sillage global (auto)", font_size=9)
    plotter.add_text(v3.SCENE_TEXT, position="lower_left", font_size=8)
    plotter.show_axes()
    plotter._rotor_actors = rotor_actors  # the window updates their opacity live (no rebuild)
    return plotter
