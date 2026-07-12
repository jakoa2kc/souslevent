"""Aggregate the auto feature cases of ONE hour into a single 3D scene.

Reuses the Pass-2 building blocks (``viz.volume3d``): drape the fine zone DEM once, then overlay
each feature's rotor (reversed-flow volume), **clipped to its own zone bounds** so the buffered
boundaries don't leak in (ADR-0021). The window's hour slider just re-calls this with the cases
of the selected hour. See docs/10_auto_pipeline.md.
"""

from __future__ import annotations


def extract_volume(case_dir: str, wind_from_deg: float, aoi_bounds, *, metric: str = "rotor",
                   ref_speed_ms=None, vol_floor: float = 0.20, metric_range=None):
    """Read an OpenFOAM momentum case and return the clipped 3-D **volume for ``metric``**:

      - ``"rotor"``       — reversed-flow recirculation (``along_flow`` < 0);
      - ``"horizontal"``  — flow slowed below ``vol_floor`` % of the upstream wind (incl. reversal);
      - ``"vertical"``    — strong vertical motion (|w| ≥ ``vol_floor`` m/s) — lift & sink;
      - ``"turbulence"``  — turbulent zone (turbulence intensity ≥ ``vol_floor``).

    Every volume carries ALL the cell scalars so ``_add_rotor`` can colour by any field without
    re-reading: ``along_flow`` (m/s), ``along_pct`` (% of upstream wind, signed: −100 reversal →
    +100 free-stream), ``w_ms`` (vertical velocity, signed), ``turb_rms`` = √(2k/3) [m/s].
    Returns ``None`` if empty/unavailable. ``vol_floor`` is in the metric's native unit.

    Thin wrapper over ``viz.volume3d.extract_lee_volume`` (shared with the manual app)."""
    from ..flow import openfoam_reader as ofr
    from ..viz import volume3d as v3

    mesh = ofr.read_case(case_dir)
    return v3.extract_lee_volume(mesh, v3.mean_flow_vector(wind_from_deg), metric=metric,
                                 ref_speed_ms=ref_speed_ms, vol_floor=vol_floor,
                                 aoi_bounds=aoi_bounds, metric_range=metric_range)


def extract_case_volumes(case_dir: str, wind_from_deg: float, aoi_bounds, *, ref_speed_ms=None,
                         floors=None):
    """Read a case ONCE and return ``{metric: volume}`` for all representations (non-empty only) —
    used to persist every view (rotor / horizontal / vertical / turbulence) to a ``.sillage``."""
    from ..flow import openfoam_reader as ofr
    from ..viz import volume3d as v3

    mesh = ofr.read_case(case_dir)
    return v3.extract_lee_volumes(mesh, v3.mean_flow_vector(wind_from_deg),
                                  ref_speed_ms=ref_speed_ms, floors=floors, aoi_bounds=aoi_bounds)


def extract_case_source(case_dir: str, wind_from_deg: float, aoi_bounds, *, ref_speed_ms=None):
    """Read a case ONCE and return the compact, threshold-independent lee source for ``.sillage``
    re-analysis. The full OpenFOAM case is not stored; only the clipped cells + derived scalars."""
    from ..flow import openfoam_reader as ofr
    from ..viz import volume3d as v3

    mesh = ofr.read_case(case_dir)
    return v3.extract_lee_source(mesh, v3.mean_flow_vector(wind_from_deg),
                                 ref_speed_ms=ref_speed_ms, aoi_bounds=aoi_bounds)


def downsample_dem_for_web(dem, max_px: int = 700):
    """A strided copy of ``dem`` capped at ``max_px`` on its longest side — the full corridor DEM
    (millions of points at 5-10 m) would dominate a web export; ~0.5 Mpt is plenty for a browser."""
    import numpy as np

    from ..terrain.dem import Dem

    h, w = dem.shape
    stride = max(1, int(np.ceil(max(h, w) / max_px)))
    if stride == 1:
        return dem
    from rasterio.transform import Affine

    t = dem.transform
    return Dem(elevation=np.ascontiguousarray(dem.elevation[::stride, ::stride]),
               transform=Affine(t.a * stride, t.b, t.c, t.d, t.e * stride, t.f),
               crs=dem.crs, resolution_m=float(dem.resolution_m) * stride)


def export_web_html(dem, cases, out_path, *, metric: str = "rotor", vol_floor: float = 0.20,
                    metric_range=None, route_winds=None, rotor_opacity: float = 0.5,
                    intensity_max=None, wind_size_factor: float = 1.0,
                    wind_altitude_m: float = 20.0, max_terrain_px: int = 700,
                    title: str = "") -> str:
    """Export ONE hour/scenario of an auto result as a **standalone interactive HTML** (vtk.js via
    ``pyvista.Plotter.export_html``) — openable in any browser, nothing to install, shareable on a
    website. The scene is the same as the app's 3D tab (same blend/colours/arrows), except the
    terrain uses the elevation colormap (basemap textures don't survive the vtk.js export) and is
    downsampled to keep the file size web-friendly. Frozen scene: one hour, one representation."""
    import pyvista as pv

    pl = pv.Plotter(off_screen=True)
    dem_web = downsample_dem_for_web(dem, max_terrain_px)
    populate_auto_scene(pl, dem_web, cases, crs=None,  # crs=None → elevation colormap (no tiles)
                        route_winds=route_winds, rotor_cache=None, rotor_opacity=rotor_opacity,
                        intensity_max=intensity_max, metric=metric, vol_floor=vol_floor,
                        metric_range=metric_range, wind_size_factor=wind_size_factor,
                        wind_altitude_m=wind_altitude_m)
    if title:
        pl.add_text(title, position="upper_right", font_size=9)
    pl.view_isometric()
    out = str(out_path)
    pl.export_html(out)
    pl.close()
    return out


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
                        rotor_opacity: float = 0.5, intensity_max=None,
                        metric: str = "rotor", vol_floor: float = 0.20, texture_cache=None,
                        metric_range=None, terrain_cache=None,
                        wind_size_factor: float = 1.0, wind_altitude_m: float = 20.0):
    """Add the global wake scene for one hour to ``plotter``.

    ``dem`` is the full fine zone DEM (terrain), ``cases`` the :class:`auto.pipeline.CaseResult`
    objects for the chosen hour. ``route_winds`` (optional) is ``[(x, y, speed_ms, from_deg), …]``
    in the DEM CRS — the AROME wind sampled along the route at this hour, drawn as arrows.
    ``basemap_zoom_boost`` sharpens the draped basemap (lee-zone detail). ``rotor_cache`` is an
    optional dict reused across hour scrubs so each case's rotor mesh is read only once. The
    caller sets the camera (no view reset here)."""
    from ..viz import volume3d as v3

    # The terrain StructuredGrid is geometry-only (independent of hour/metric/opacity); building it
    # from a fine corridor DEM is costly, so reuse it across scrubs when a cache dict is provided.
    terrain = terrain_cache.get(id(dem)) if terrain_cache is not None else None
    if terrain is None:
        terrain = v3._terrain_mesh(dem)
        if terrain_cache is not None:
            terrain_cache[id(dem)] = terrain
    if not (crs is not None
            and v3._drape_basemap(plotter, terrain, crs, basemap_source, basemap_zoom_boost,
                                  texture_cache=texture_cache)):
        terrain["elevation_m"] = terrain.points[:, 2]
        plotter.add_mesh(terrain, scalars="elevation_m", cmap="gist_earth",
                         show_scalar_bar=False, reset_camera=False)

    # Each space point is drawn by ONE sector (its nearest centre) so overlapping sectors don't
    # alpha-stack; but the coloured value is a **distance-weighted average across all overlapping
    # sectors** (feathered), so it is continuous across sector boundaries — no diagonal seams from
    # the differing per-sector wind BCs. See ADR-0029 / ADR-0032 (overlap handling + blend).
    import numpy as np

    try:
        from scipy.spatial import cKDTree
    except Exception:
        cKDTree = None

    # the cell field each metric colours by (blended below)
    field_name = {"rotor": "along_flow", "horizontal": "along_pct",
                  "vertical": "w_ms", "turbulence": "turb_rms"}.get(metric, "along_flow")

    centers = np.array([[(c.aoi_bounds[0] + c.aoi_bounds[1]) / 2.0,
                         (c.aoi_bounds[2] + c.aoi_bounds[3]) / 2.0] for c in cases]) \
        if cases else np.zeros((0, 2))

    range_key = tuple(sorted((k, round(float(v), 3)) for k, v in (metric_range or {}).items()))
    floor_key = range_key or (round(float(vol_floor), 3) if metric != "rotor" else 0)

    # Global reference wind for THIS hour (median of the sectors' upstream winds): the horizontal
    # metric shows the along-wind speed as a % of THIS single wind, so both the threshold and the
    # colour mean the same thing in every sector (instead of each zone's own wind) — on EVERY path
    # (live, saved source, compact). global_wind is deterministic per hour (cases fixed), so it need
    # not enter the cache key as long as the caller always renders the whole hour (it does).
    winds = [float(c.wind_speed_ms) for c in cases if getattr(c, "wind_speed_ms", 0)]
    global_wind = max(float(np.median(winds)) if winds else 1.0, 0.1)

    def _global_horizontal(mesh):
        """Express along_pct as % of the hour's GLOBAL wind (not each zone's own), so threshold AND
        colour are comparable across sectors regardless of which path produced the mesh."""
        if metric == "horizontal" and mesh is not None and "along_flow" in mesh.array_names:
            mesh["along_pct"] = np.asarray(mesh["along_flow"], dtype="float64") / global_wind * 100.0
        return mesh

    def _source_for(case):
        """The threshold-independent lee source for a case, cached in RAM: read from a saved
        ``source_path`` or by reading the OpenFOAM case ONCE. Re-thresholding on metric/floor change
        is then instant (no OpenFOAM re-read per metric)."""
        skey = (case.zone_index, case.hour, "source", 0)
        src = rotor_cache.get(skey) if rotor_cache is not None else None
        if src is None:
            try:
                sp = getattr(case, "source_path", "")
                if sp:
                    import pyvista as pv
                    src = pv.read(sp)
                elif case.case_dir:
                    src = extract_case_source(case.case_dir, case.wind_from_deg, case.aoi_bounds,
                                              ref_speed_ms=case.wind_speed_ms)
            except Exception:
                src = None
            if src is not None and rotor_cache is not None:
                rotor_cache[skey] = src
        return src

    def _rev_for(case):
        key = (case.zone_index, case.hour, metric, floor_key)
        rev = rotor_cache.get(key) if rotor_cache is not None else None
        if rev is not None:
            return rev
        src = _source_for(case)  # live case (read once) OR saved re-analysable source
        if src is not None:
            try:
                rev = v3.threshold_lee_source(_global_horizontal(src), metric=metric,
                                              vol_floor=vol_floor, metric_range=metric_range)
            except Exception:
                rev = None
        if rev is None:  # compact .sillage: a pre-thresholded per-metric volume (can only narrow)
            path = getattr(case, "vtu_paths", {}).get(metric, "")
            if path:
                try:
                    import pyvista as pv

                    rev = _global_horizontal(pv.read(path))
                    if metric_range:
                        rev = v3.threshold_lee_source(rev, metric=metric, vol_floor=vol_floor,
                                                      metric_range=metric_range)
                except Exception:
                    rev = None  # a missing/failed case shouldn't blank the whole scene
        if rotor_cache is not None and rev is not None:
            rotor_cache[key] = rev
        return rev

    # Pass 1 — extract every sector's volume; build a KDTree of its cell centres + its metric field.
    sectors = []
    for i, case in enumerate(cases):
        rev = _rev_for(case)
        if rev is None or not rev.n_cells or cKDTree is None:
            sectors.append(None)
            continue
        cc = np.asarray(rev.cell_centers().points)[:, :2]
        fld = rev.cell_data.get(field_name)
        half = max((case.aoi_bounds[1] - case.aoi_bounds[0]) / 2.0,
                   (case.aoi_bounds[3] - case.aoi_bounds[2]) / 2.0, 1.0)
        spacing = float(np.sqrt((2.0 * half) ** 2 / max(len(cc), 1)))
        sectors.append({
            "rev": rev, "cc": cc, "center": centers[i], "half": half,
            "cover": max(3.0 * spacing, 0.06 * half),  # nearest-cell dist to count as "covered"
            "fvals": np.asarray(fld, dtype="float64") if fld is not None else None,
            "tree": cKDTree(cc),
        })
    for i, s in enumerate(sectors):  # neighbour = sectors whose aoi box overlaps
        if s is None:
            continue
        s["neigh"] = [j for j, t in enumerate(sectors)
                      if t is not None and j != i
                      and abs(s["center"][0] - t["center"][0]) < s["half"] + t["half"]
                      and abs(s["center"][1] - t["center"][1]) < s["half"] + t["half"]]

    # Ownership tree over LIVE sectors only: a failed/empty sector must not "own" (and thus blank)
    # overlap cells a valid neighbour could draw. Query indices map back to sector ids via `live`.
    live = np.array([i for i, s in enumerate(sectors) if s is not None], dtype=int)
    owntree = cKDTree(centers[live]) if (cKDTree is not None and len(live) > 1) else None

    def _blend(pts, i):
        wsum = np.zeros(len(pts))
        vsum = np.zeros(len(pts))
        for j in [i] + sectors[i]["neigh"]:
            t = sectors[j]
            if t is None or t["fvals"] is None:
                continue
            # distance_upper_bound lets the tree prune points outside the overlap band early; beyond
            # it, query returns d=inf and idx=n_cells (out of range) → clamp idx before indexing.
            d, idx = t["tree"].query(pts, distance_upper_bound=t["cover"])
            covered = np.isfinite(d)
            idx = np.where(covered, idx, 0)
            dc = np.hypot(pts[:, 0] - t["center"][0], pts[:, 1] - t["center"][1])
            w = np.clip(1.0 - np.clip(dc / t["half"], 0.0, 1.0), 0.0, 1.0) ** 2  # feather to edge
            w = np.where(covered, w, 0.0)                                        # only where covered
            vsum += w * t["fvals"][idx]
            wsum += w
        return wsum, vsum

    # Pass 2 — draw each sector's OWNED cells (one draw per point), coloured by the blended field.
    rendered = 0
    rotor_actors = []
    for i, case in enumerate(cases):
        _add_domain_box(plotter, terrain, case.aoi_bounds)  # show the analysed sub-domain
        s = sectors[i]
        if s is None:
            continue
        owned = (np.nonzero(live[owntree.query(s["cc"])[1]] == i)[0] if owntree is not None
                 else np.arange(len(s["cc"])))
        if not len(owned):
            continue
        bkey = (case.zone_index, case.hour, metric, floor_key, "blend")
        draw = rotor_cache.get(bkey) if rotor_cache is not None else None
        if draw is None:
            draw = s["rev"].extract_cells(owned)
            if s["fvals"] is not None:  # replace the metric field by the cross-sector weighted mean
                wsum, vsum = _blend(s["cc"][owned], i)
                own = s["fvals"][owned]
                draw.cell_data[field_name] = np.where(wsum > 1e-9,
                                                      vsum / np.maximum(wsum, 1e-12), own)
            if rotor_cache is not None:
                rotor_cache[bkey] = draw
        if draw.n_cells:
            actor = v3._add_rotor(plotter, draw, terrain, opacity=rotor_opacity,
                                  intensity_max=intensity_max, metric=metric,
                                  metric_range=metric_range)
            if actor is not None:
                rotor_actors.append(actor)
            rendered += 1

    if route_winds:  # AROME wind sampled along the route, at the rendered hour
        plotter._wind_arrows = v3._add_wind_arrows_3d(
            plotter, terrain, route_winds,
            size_factor=wind_size_factor, altitude_m=wind_altitude_m)
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
