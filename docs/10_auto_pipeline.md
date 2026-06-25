# 10 — Automatic full-resolution pipeline (`sillage.auto`)

> A "one-click" alternative to the manual two-pass app: pick a flight zone + window, validate,
> and the **whole zone** is solved at the finest topo scale by subdividing it into momentum
> sub-domains, solving Pass-2 over each (zone × hour), and aggregating into a **time-sliderable
> 3D scene**. Additive to the existing app (`sillage.app`), reusing the shared libraries.

## Why a second pipeline (not a rewrite)

The manual app stays: it's the precise, interactive tool (draw a feature, tune the mesh, inspect).
The **auto** pipeline trades interactivity for coverage + automation — it answers "show me the
whole flying area's wake for this window" without hand-picking features. They share every lower
layer; only the orchestration + UI differ. See **ADR-0022**.

## Dataflow

```
(bbox lat/lon, flight-window hours)
  │
  ├─ terrain.acquire.prepare_dem  ──────────────►  fine zone DEM (IGN ~1 m native, buffered)
  │
  ├─ auto.partition.partition_zone ─────────────►  relief-adaptive quadtree of sub-domains
  │      split while  est_cells > max_cells       (mesh budget)
  │                or relief_span > max_relief_m   (one upstream wind stays valid — ADR-0003)
  │
  ├─ for each (sub-zone × hour):                  auto.pipeline.run_auto  (bounded concurrency)
  │      auto.wind.local_wind_provider(hour)(centre) → (speed, from_deg)   [Open-Meteo; AROME next]
  │      terrain.crop_dem(zone + AUTO_EDGE_BUFFER) → flow.windninja.run_momentum → OpenFOAM case
  │      (parallel-then-sequential retry, like the Pass-1 loops)
  │
  └─ AutoResult: { case_dir per (zone, hour), wind, zone bounds }  + timings
        │
        └─ auto.scene.populate_auto_scene(hour)  → global 3D for that hour
             drape fine DEM once + overlay each sub-zone rotor, CLIPPED to its zone (ADR-0021)
```

Progress + **ETA** (`auto.progress.ProgressTracker`): `done/total · % · reste ~Xm`, the ETA from
the mean of observed per-task durations × remaining tasks.

## Modules (`src/sillage/auto/`)

| Module | Role | Status |
|---|---|---|
| `partition.py` | relief + mesh-budget quadtree → `list[SubZone]` | **done, tested** |
| `progress.py` | `ProgressTracker` (percent + ETA) | **done, tested** |
| `wind.py` | per-sub-zone upstream wind (`make(hour)->provider`) | **done** (Open-Meteo; AROME stub) |
| `pipeline.py` | `AutoConfig` / `AutoResult` / `run_auto` orchestrator | **done** (integration-run) |
| `scene.py` | aggregate one hour's cases into a 3D scene | **done** (reuses `viz.volume3d`) |
| `window.py` | the 2-tab IHM (select → run → 3D time slider) | **skeleton** (wiring + viewport) |

## Reuse map (no duplication)

- DEM: `terrain.acquire.prepare_dem` (IGN/world, the de-stripe + tile parallelism), `terrain.dem.crop_dem/load_dem/write_dem`.
- Solver: `flow.windninja.run_momentum` + `format_run_failure`; cases via `flow.openfoam_reader`.
- Wind: `wind.profile.window_forecast_provider` (Open-Meteo crest wind).
- 3D: `viz.volume3d` building blocks (`_terrain_mesh`, `_drape_basemap`, `_add_rotor`,
  `_clip_domain_boundary`, `_add_north_arrow`, `mean_flow_vector`).
- Concurrency: `screening.pass1.parallel_run_plan` policy + the parallel-then-sequential-retry pattern.
- Misc: `timing.RunTimings`, `app.jobs.SolveJob` (worker thread), `app.map_tab.MapTab` (Leaflet AOI).

## UI contract (`window.py`)

- **Tab 1 — sélection:** `MapTab` (IGN, rectangle AOI) + a multi-day **window range slider** +
  **« Valider »** → launches `run_auto` on a `SolveJob`; the status bar shows the progress bar +
  ETA (`tracker.summary`).
- **Tab 2 — rendu 3D:** an embedded `QtInteractor` (terrain-locked rotation, ADR: azimuth/elev) +
  an **hour slider** over the computed range → `populate_auto_scene(result.cases_for_hour(h))`;
  pan/zoom/tilt + a movable focal point (PyVista camera). Switches here when the run finishes.

## Key decisions / open items

- **AROME wind: wired via Open-Meteo HD.** `auto.wind` reads **AROME France HD (1.5 km)**
  height-AGL wind from Open-Meteo's `arome_france_hd` (highest available level ≈120 m), per
  sub-zone centre → real valley-scale variation, keyless. This beats the Météo-France GRIB API
  (2.5 km, 10–100 m, GRIB-only → needs `eccodes`), so GRIB is deferred to an optional path for
  >120 m / pressure levels. The `.env` key still labels/gates the run + drives the slider window.
- **Momentum is CPU-bound (ADR-0006)** and its temp env can't be redirected (OpenFOAM crash,
  Entry 38), so `momentum_workers` defaults to **1** (sequential); raise it to overlap solves
  where RAM/cores allow. The honest speed lever remains `mesh_count` + a lean per-zone domain.
- **Cost.** A run is `len(zones) × len(hours)` momentum solves — minutes to a long while; the ETA
  sets expectations. Caching (`*_vel.asc` reuse, the DEM cache) keeps re-launches cheaper.
- **Global height legend.** `scene.py` currently legends off the first rotor; a global height
  `clim` across zones is a small follow-up (`_add_rotor(clim=…)` already supports it).
