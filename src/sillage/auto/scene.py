"""Aggregate the auto sub-zone cases of ONE hour into a single 3D scene.

Reuses the Pass-2 building blocks (``viz.volume3d``): drape the fine zone DEM once, then overlay
each sub-zone's rotor (reversed-flow volume), **clipped to its own zone bounds** so the buffered
boundaries don't leak in (ADR-0021). The window's hour slider just re-calls this with the cases
of the selected hour. See docs/10_auto_pipeline.md.
"""

from __future__ import annotations


def populate_auto_scene(plotter, dem, cases, crs=None, basemap_source: str = "IGN plan"):
    """Add the global wake scene for one hour to ``plotter``.

    ``dem`` is the full fine zone DEM (terrain), ``cases`` the :class:`auto.pipeline.CaseResult`
    objects for the chosen hour. The caller sets the camera (no view reset here)."""
    from ..flow import openfoam_reader as ofr
    from ..viz import volume3d as v3

    terrain = v3._terrain_mesh(dem)
    if not (crs is not None and v3._drape_basemap(plotter, terrain, crs, basemap_source)):
        terrain["elevation_m"] = terrain.points[:, 2]
        plotter.add_mesh(terrain, scalars="elevation_m", cmap="gist_earth",
                         show_scalar_bar=False, reset_camera=False)

    rendered = 0
    for case in cases:
        try:
            mesh = ofr.read_case(case.case_dir)
        except Exception:
            continue  # a missing/failed case shouldn't blank the whole scene
        mfd = v3.mean_flow_vector(case.wind_from_deg)
        mesh["along_flow"] = ofr.along_flow_component(mesh, mfd)
        rev = mesh.threshold(value=0.0, scalars="along_flow", invert=True)
        rev = v3._clip_domain_boundary(rev, mesh, aoi_bounds=case.aoi_bounds)
        if rev.n_cells:
            # One shared legend (on the first rotor); TODO: a global height clim across zones.
            v3._add_rotor(plotter, rev, terrain, show_legend=(rendered == 0))
            rendered += 1

    v3._add_north_arrow(plotter, terrain)
    plotter.add_text("Pass 2 — sillage global (auto)", font_size=9)
    plotter.add_text(v3.SCENE_TEXT, position="lower_left", font_size=8)
    plotter.show_axes()
    return plotter
