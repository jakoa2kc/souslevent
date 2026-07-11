# 10 — Automatic full-resolution pipeline (`sillage.auto`)

> A "one-click" alternative to the manual two-pass app: draw a flight route + window, validate,
> screen the route corridor once, then solve Pass-2 over each detected feature × hour and
> aggregate compact rotors into a **time-sliderable 3D scene**. Additive to the existing app
> (`sillage.app`), reusing the shared libraries.

## Why a second pipeline (not a rewrite)

The manual app stays: it's the precise, interactive tool (draw a feature, tune the mesh, inspect).
The **auto** pipeline trades interactivity for coverage + automation — it answers "show me the
whole flying area's wake for this window" without hand-picking features. They share every lower
layer; only the orchestration + UI differ. See **ADR-0022**.

## Dataflow

```
(flight ROUTE = list of SEGMENTS [[(lat,lon),…],…] + corridor margin, window hours)  ─ ADR-0024/0030
  │   bbox = route bbox + margin ; gaps BETWEEN segments (valley crossings) are never computed
  │
  ├─ terrain.acquire.prepare_dem  ──────────────►  corridor DEM (IGN de-striped, target res 1/5/10/25 m)
  │
  ├─ domains, per the chosen mode (paving tiles are mesh-capped; feature candidates keep their
  │   physical lee extent — res ≈ 1.45·(side+2·buffer)/√mesh_count, calibrated; ADR-0037):
  │   • "features" (default, ADR-0023): Pass-1 mass over the corridor → hazard → find_candidates →
  │       auto.partition.feature_domains: ONE domain per relief, half ~ lee_factor×relief/2, no seams
  │   • "corridor" (blind paving, ADR-0029): NO Pass-1 — union corridor MASK over all segments,
  │       paved by ONE regular grid (auto.partition.corridor_grid_tiles): full-surface coverage,
  │       no stacked tilings when the route doubles back / crosses itself
  │   • "manual" (ADR-0036/0037): zones picked in the candidates tab (screened candidates, paving
  │       preview, or hand-drawn rectangles) after the mesh↔topo arbitration popup
  │
  ├─ for each (domain × hour):                    auto.pipeline.run_auto  (bounded concurrency)
  │      auto.wind.local_wind_provider(hour)(centre) → (speed, from_deg)   [AROME HD / fallback]
  │      terrain.crop_dem(zone + AUTO_EDGE_BUFFER) → flow.windninja.run_momentum → OpenFOAM case
  │      (parallel-then-sequential retry; disk-safe: optional compaction to a small .vtu)
  │
  └─ AutoResult: { rotor + turbulence volume per (domain, hour), wind, zone bounds }  + timings
        │
        └─ auto.scene.populate_auto_scene(hour, metric)  → global 3D for that hour
             drape DEM once (basemap reprojected to DEM CRS, zoom-boosted) + overlay each domain's
             volume, CLIPPED to its zone, drawn ONCE per point (nearest sector, no alpha-stacking)
             but COLOURED by a feathered cross-sector weighted average → continuous across
             boundaries (ADR-0021 / 0027 / 0029 / 0032)
```

Route **AROME wind arrows** (`auto.wind.route_wind_series` / `arrows_at_hour`) are drawn on the 2-D
map (keyed to the moving window handle) and in 3-D (keyed to the render hour); they are saved with a
run so a reopened result shows the **run's** winds, not today's (ADR-0030).

Progress + **ETA** (`auto.progress.ProgressTracker`): the solves run `workers` at a time, so the ETA
is **wave-based** — `ceil(total/workers)` waves, `total estimate = mean solve × waves`, `eta =
estimate − elapsed` (wall-clock; ADR-0028). The headline % is the elapsed fraction of that estimate,
floored by the genuinely completed fraction.

## Modules (`src/sillage/auto/`)

| Module | Role | Status |
|---|---|---|
| `partition.py` | `feature_domains` (hazard) + `corridor_grid_tiles` (global-surface paving) + `corridor_mask` + mesh↔topo helpers (`ninjafoam_resolution_m`…, ADR-0037) | **done, tested** |
| `progress.py` | `ProgressTracker` (wave-based % + ETA, ADR-0028) | **done, tested** |
| `wind.py` | per-domain upstream wind + `route_wind_series`/`arrows_at_hour` (arrows) | **done** (AROME HD via Open-Meteo + fallback) |
| `arome.py` | forecast window (absolute dates) from the AROME/Open-Meteo horizon | **done, tested** |
| `pipeline.py` | `AutoConfig` / `AutoResult` / `run_auto`; modes, disk-safe compaction, parallel plan | **done** (integration-run) |
| `scene.py` | `extract_volume` (rotor/turbulence) + aggregate one hour into a 3D scene | **done** (reuses `viz.volume3d`) |
| `store.py` | save/open a run as a portable `.sillage` bundle: compact volumes or re-analysable sources (ADR-0030/0031) | **done, tested** |
| `window.py` | the legacy 2-tab IHM (kept as backup); the published UI is `souslevent.window` (3 tabs, ADR-0033) | **done** |

## Reuse map (no duplication)

- DEM: `terrain.acquire.prepare_dem` (IGN/world, the de-stripe + tile parallelism), `terrain.dem.crop_dem/load_dem/write_dem`.
- Solver: `flow.windninja.run_momentum` + `format_run_failure`; cases via `flow.openfoam_reader`.
- Wind: `auto.wind.local_wind_provider` (AROME France HD via Open-Meteo, fallback crest blend).
- 3D: `viz.volume3d` building blocks (`_terrain_mesh`, `_drape_basemap`, `_add_rotor`,
  `_clip_domain_boundary`, `_add_north_arrow`, `_add_horizontal_scale_bar`, `mean_flow_vector`).
- Concurrency: `screening.pass1.parallel_run_plan` policy + the parallel-then-sequential-retry pattern.
- Misc: `timing.RunTimings`, `app.jobs.SolveJob` (worker thread), `app.map_tab.MapTab` (Leaflet route/AOI).

## UI contract (`window.py`)

The **unified app** (`souslevent.window`, the published UI) has THREE tabs; the legacy `auto.window`
keeps the old two-tab flow.

- **Tab 1 — sélection + calcul:** the map takes the LEFT side (route **or** rectangle selection;
  left-click add, right-click undo, double-click finish, **« ＋ » = new segment**), with every
  parameter in a **scrollable right-hand column**: Sélection, Vent (**AROME forecast créneau** OR
  **manual speed × direction grid**, ADR-0034), Calcul (**Pass-1 puis sélection** / **pavage auto**),
  Calculs simultanés, **Topo (1/5/10/25 m)**, **Maillage Pass-2** (Grossier→Max, ADR-0008/0035),
  Marge corridor, Candidats max, Pas secteurs, the CPU plan (with the ~min/solve estimate),
  **« Valider »**, avancement + log. Both calc modes stop at the candidates tab — no solve is
  launched from tab 1 (ADR-0036/0037).
- **Tab 2 — candidats Pass-1:** the screened candidates (browsable **hourly hazard** slider,
  ADR-0036) OR the paving preview (every sector pre-selected). Click sectors on the map or the list
  to (de)select; drag a rectangle to add a **manual zone** (mesh↔topo popup, ADR-0037). The plan
  text shows `zones × créneaux = solves`, the **effective mesh resolution** and the ~total minutes.
  **« Lancer Pass-2 sur la sélection »** applies the final mesh↔topo arbitration popup, then runs
  `run_auto(domain_mode="manual")` on exactly the selected zones.
- **Tab 3 — rendu 3D:** an embedded `QtInteractor` (rotation locked to azimuth/elev + **right-drag
  pan**) + an **hour/scenario slider** (absolute-date or wind-grid labels) → `populate_auto_scene`.
  Right panel: **Représentation**, the metric legend, only the useful **range sliders** for that
  representation, **« Recalculer la vue 3D »**, an **Opacité** slider (live), the continuous **wind
  colourbar** with the **wind-arrow size/altitude sliders** under it, and **📂 Ouvrir /
  💾 Sauvegarder** (`.sillage`).

## Key decisions / open items

- **AROME wind: wired via Open-Meteo HD.** `auto.wind` reads **AROME France HD (1.5 km)**
  height-AGL wind from Open-Meteo's `arome_france_hd` (highest available level ≈120 m), per
  feature centre → real valley-scale variation, keyless. This beats the Météo-France GRIB API
  (2.5 km, 10–100 m, GRIB-only → needs `eccodes`), so GRIB is deferred to an optional path for
  >120 m / pressure levels. The `.env` key still labels/gates the run + drives the slider window.
- **Topo 1 m is available when IGN HIGHRES covers the route.** The auto UI exposes `1 m (IGN)` in
  addition to 5/10/25 m. It keeps the native ~1 m fetch instead of block-averaging, so use it only
  on short corridors; outside IGN coverage `prepare_dem(source="auto")` still falls back to the
  worldwide source.
- **Momentum is CPU-bound (ADR-0006)** and its temp env can't be redirected (OpenFOAM crash,
  Entry 38), so `momentum_workers` defaults to **all detected cores** as a max request; the
  effective workers are capped by the real `domains × hours` task count. The honest speed lever
  remains `mesh_count` + a lean per-domain solve. **Mesh ↔ topo (ADR-0037):** the effective mesh
  resolution is `≈ 1.45·(side+2·buffer)/√mesh_count` (calibrated on real cases). Paving tiles are
  capped to match the chosen topo; relief candidates keep the physical wake extent and the UI asks
  to adapt mesh or topo before launching.
- **Cost.** A run is `len(zones) × len(hours)` momentum solves — minutes to a long while; the ETA
  sets expectations. Caching (`*_vel.asc` reuse, the DEM cache) keeps re-launches cheaper.
- **Disk.** OpenFOAM cases are kept while the window is open, then deleted on close (and before the
  next run) by `cleanup_auto_artifacts`; reusable DEMs + Pass-1 screening cache stay. Less than 3 Go
  free emits a warning but never truncates a batch. Optional `compact_cases_during_run` extracts the
  lee meshes and deletes cases mid-run (ADR-0025). `locate_openfoam_case(dem_stem=…)` keeps parallel
  solves from colliding.
- **3D georeferencing + rendering.** Basemaps reprojected WebMercator→DEM CRS (zoom-boosted),
  terrain on pixel centres, scale bar (ADR-0027). Metric colour scales are scalar and slider-driven;
  uniform adjustable opacity, overlaps drawn by nearest sector (no alpha-stacking). Wind on a
  continuous 0–40 km/h scale.
- **Four representations** (ADR-0031), one absolute scale across sub-domains, switchable per result:
  *rotor* (reversed-flow intensity min/max), *horizontal* (% of upstream wind, red = reverse /
  yellow = 0 / green = same-sense wind, with values above max hidden), *vertical* (m/s, separate
  sink and lift ranges with the calm gap hidden), *turbulence* (absolute rms √(2k/3) [m/s], min/max).
  Computed once per case/source; re-analysable `.sillage` files re-threshold from the saved scalars.
- **Save/open.** A run saves to a `.sillage` zip (manifest + DEM + route winds + per-day hour
  labels). Compact mode stores **per-metric** lee `.vtu`s (`CaseResult.vtu_paths`): small, but volume
  thresholds are fixed at save time. Re-analysable mode stores `source_XXX.vtu`
  (`CaseResult.source_path`): larger, but "Seuil volume" re-extracts after reopening without the
  OpenFOAM case (ADR-0030/0031).
