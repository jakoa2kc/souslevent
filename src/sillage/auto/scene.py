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
                    wind_altitude_m: float = 20.0, max_terrain_px: int = 900,
                    crs=None, basemap_source: str = "IGN plan", title: str = "") -> str:
    """Export ONE hour/scenario of an auto result as a **standalone interactive HTML** (vtk.js via
    ``pyvista.Plotter.export_html``) — openable in any browser, nothing to install, shareable on a
    website. Same scene as the app's 3D tab (blend/colours/arrows); the basemap is **baked into the
    terrain vertex colours** when ``crs`` is given (vtk.js exports don't carry textures; falls back
    to the elevation colormap offline), and the terrain is downsampled to stay web-friendly.
    Frozen scene: one hour, one representation. Navigation: left-drag = rotate, Shift+left or
    middle-drag = pan, wheel/right-drag = zoom (vtk.js standard bindings)."""
    import pyvista as pv

    from ..viz import volume3d as v3

    pl = pv.Plotter(off_screen=True)
    dem_web = downsample_dem_for_web(dem, max_terrain_px)
    terrain = v3._terrain_mesh(dem_web)
    if crs is not None:  # bake the basemap into per-point colours (offline → elevation colormap)
        v3.bake_basemap_rgb(terrain, crs, basemap_source, zoom_boost=1)
    populate_auto_scene(pl, dem_web, cases, crs=None,  # no texture drape: web_rgb or elevation
                        route_winds=route_winds, rotor_cache=None, rotor_opacity=rotor_opacity,
                        intensity_max=intensity_max, metric=metric, vol_floor=vol_floor,
                        metric_range=metric_range, wind_size_factor=wind_size_factor,
                        wind_altitude_m=wind_altitude_m, terrain_cache={id(dem_web): terrain})
    if title:
        pl.add_text(title, position="upper_right", font_size=9)
    pl.add_text("glisser = rotation (azimut/élévation) · clic droit ou molette-clic = translation · "
                "molette = zoom", position="lower_right", font_size=7)
    pl.view_isometric()
    out = str(out_path)
    pl.export_html(out)
    pl.close()
    _fix_web_export_bootstrap(out)
    return out


# Injected into the exported HTML: map-coherent navigation, same feel as the app's 3D tab.
# The trame-vtk bundle exposes the render window as ``global.renderWindow`` (set inside its load
# function). Everything hooks the INTERACTOR events, not the interactor style: the stock
# vtkInteractorStyleTrackballCamera has no right-button handler to remap, the synchroniser may
# swap styles after load, and vtk.js freezes its publicAPI objects (property writes silently
# no-op) — the interactor itself persists and its ``onXxx`` subscriptions do work:
#   - right-drag = PAN, self-implemented as a camera-plane translation of the poked renderer's
#     camera (zoom stays on the wheel; the context menu is suppressed);
#   - rotation locked to azimuth/elevation: on every non-pan mouse move the view-up is reset to
#     +Z (no roll) and the elevation clamped (5°–85°) so the map can never flip over or roll.
# ``window.__slvNavDebug`` exposes counters/last-error for field debugging from the console.
_WEB_NAV_JS = """
(function(){
  var LO = 5 * Math.PI / 180, HI = 85 * Math.PI / 180;
  function rw() { return (window.global && window.global.renderWindow) || window.renderWindow; }
  function clampCam(cam) {                    /* azimuth free; elevation clamped; no roll */
    var fp = cam.getFocalPoint(), p = cam.getPosition();
    var dx = p[0]-fp[0], dy = p[1]-fp[1], dz = p[2]-fp[2];
    var r = Math.sqrt(dx*dx + dy*dy + dz*dz) || 1;
    var el = Math.asin(Math.max(-1, Math.min(1, dz / r)));
    if (el < LO || el > HI) {
      var el2 = Math.min(HI, Math.max(LO, el));
      var az = Math.atan2(dy, dx), c = Math.cos(el2);
      cam.setPosition(fp[0] + r*c*Math.cos(az), fp[1] + r*c*Math.sin(az), fp[2] + r*Math.sin(el2));
    }
    cam.setViewUp(0, 0, 1);
  }
  function clampAll() {
    var g = rw(); if (!g || !g.getRenderers) { return; }
    var rens = g.getRenderers();
    for (var i = 0; i < rens.length; i++) { clampCam(rens[i].getActiveCamera()); }
  }
  /* Self-contained right-drag PAN at the INTERACTOR level (the stock TrackballCamera style has no
     right-button handler at all, and the scene synchroniser may swap styles after load — the
     interactor persists). Camera-plane translation of the poked renderer's camera. */
  function norm(v) { var n = Math.hypot(v[0], v[1], v[2]) || 1; return [v[0]/n, v[1]/n, v[2]/n]; }
  function cross(a, b) {
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
  }
  var panRen = null, lastXY = null;
  function panMove(cd) {
    if (!panRen || !lastXY || !cd || !cd.position) { return; }
    var cam = panRen.getActiveCamera();
    var dx = cd.position.x - lastXY[0], dy = cd.position.y - lastXY[1];
    lastXY = [cd.position.x, cd.position.y];
    var fp = cam.getFocalPoint(), p = cam.getPosition();
    var dir = [fp[0]-p[0], fp[1]-p[1], fp[2]-p[2]];
    var dist = Math.hypot(dir[0], dir[1], dir[2]) || 1;
    var g = rw();
    var size = (g.getViews && g.getViews()[0] && g.getViews()[0].getSize)
               ? g.getViews()[0].getSize() : [1000, 800];
    var scale = 2 * dist * Math.tan((cam.getViewAngle() * Math.PI / 180) / 2) / (size[1] || 800);
    var right = norm(cross(dir, cam.getViewUp()));
    var upv = norm(cross(right, dir));
    var mx = -dx * scale, my = -dy * scale;   /* drag right/up -> map follows the cursor */
    var move = [right[0]*mx + upv[0]*my, right[1]*mx + upv[1]*my, right[2]*mx + upv[2]*my];
    dbg.panmove++;
    cam.setFocalPoint(fp[0]+move[0], fp[1]+move[1], fp[2]+move[2]);
    cam.setPosition(p[0]+move[0], p[1]+move[1], p[2]+move[2]);
    g.render();
  }
  var patchedIa = null;   /* vtk.js freezes its publicAPI objects: track state HERE, not on them */
  var dbg = { patched: 0, rbp: 0, panmove: 0, clamp: 0, lastErr: null };
  window.__slvNavDebug = dbg;   /* field-debuggable from the browser console */
  function patch() {
    try {
      var g = rw(); if (!g) { return; }
      var ia = g.getInteractor && g.getInteractor(); if (!ia || ia === patchedIa) { return; }
      if (!ia.onMouseMove || !ia.onRightButtonPress) { return; }
      patchedIa = ia;
      dbg.patched++;
      ia.onRightButtonPress(function (cd) {
        try {
          var pos = cd && cd.position;
          if (!pos) { return; }
          dbg.rbp++;
          lastXY = [pos.x, pos.y];
          panRen = (cd.pokedRenderer)
                   || (ia.findPokedRenderer && ia.findPokedRenderer(pos.x, pos.y))
                   || g.getRenderers()[g.getRenderers().length - 1];
        } catch (e) { dbg.lastErr = "rbp: " + e; }
      });
      ia.onRightButtonRelease(function () { panRen = null; lastXY = null; });
      ia.onMouseMove(function (cd) {
        try {
          if (panRen) { panMove(cd); } else { dbg.clamp++; clampAll(); }
        } catch (e) { dbg.lastErr = "move: " + e; }
      });
      if (ia.onEndMouseWheel) { ia.onEndMouseWheel(clampAll); }
      var cont = ia.getContainer && ia.getContainer();
      if (cont) { cont.addEventListener('contextmenu', function (e) { e.preventDefault(); }); }
      clampAll();
    } catch (e) { dbg.lastErr = "patch: " + e; }
  }
  setInterval(patch, 500);
  patch();
})();
"""


def _fix_web_export_bootstrap(path) -> None:
    """Post-process trame-vtk's standalone HTML: (1) work around the template's load race — the
    final classic <script> calls ``OfflineLocalView.load(...)`` before the deferred module that
    defines it has run, leaving the page stuck on the vtk.js drop-file splash — by replacing the
    one-shot call with a ready-wait loop; (2) inject map-coherent navigation (``_WEB_NAV_JS``:
    right-drag pan + azimuth/elevation-locked rotation). No-op if the template changes upstream."""
    from pathlib import Path

    p = Path(path)
    html = p.read_text(encoding="utf-8")
    racy = "setTimeout(() => OfflineLocalView.load(container, { base64Str }),0);"
    if racy not in html:
        return
    ready_wait = ("(function boot(){ if (window.OfflineLocalView) { "
                  "OfflineLocalView.load(container, { base64Str }); } "
                  "else { setTimeout(boot, 100); } })();"
                  + _WEB_NAV_JS)
    p.write_text(html.replace(racy, ready_wait), encoding="utf-8")


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
        if "web_rgb" in terrain.point_data:  # web export: basemap baked into vertex colours
            plotter.add_mesh(terrain, scalars="web_rgb", rgb=True,
                             show_scalar_bar=False, reset_camera=False)
        else:
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
