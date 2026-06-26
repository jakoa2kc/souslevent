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
(flight ROUTE [(lat,lon),…] + corridor margin, flight-window hours)   ─ ADR-0024
  │   bbox = route bbox + margin ; later the hazard is masked to a corridor around the route
  │
  ├─ terrain.acquire.prepare_dem  ──────────────►  fine zone DEM (IGN ~1 m native, buffered)
  │
  ├─ Pass-1 mass over the WHOLE zone (continuous) ►  hazard map  (screening.pass1.hourly_indicator)
  │      find_candidates → auto.partition.feature_domains:
  │      ONE momentum domain per feature, half ~ lee_factor × local relief / 2  (ADR-0023)
  │      → separated domains, NO grid seams to reconcile
  │
  ├─ for each (feature × hour):                   auto.pipeline.run_auto  (bounded concurrency)
  │      auto.wind.local_wind_provider(hour)(centre) → (speed, from_deg)   [AROME HD / fallback]
  │      terrain.crop_dem(zone + AUTO_EDGE_BUFFER) → flow.windninja.run_momentum → OpenFOAM case
  │      (parallel-then-sequential retry, like the Pass-1 loops)
  │
  └─ AutoResult: { compact rotor mesh per (feature, hour), wind, zone bounds }  + timings
        │
        └─ auto.scene.populate_auto_scene(hour)  → global 3D for that hour
             drape fine DEM once (basemap reprojected to DEM CRS) + overlay each feature rotor,
             CLIPPED to its zone (ADR-0021 / ADR-0027)
```

Progress + **ETA** (`auto.progress.ProgressTracker`): `done/total · % · reste ~Xm`, the ETA from
the mean of observed per-task durations × remaining tasks.

## Modules (`src/sillage/auto/`)

| Module | Role | Status |
|---|---|---|
| `partition.py` | `feature_domains` (one domain per Pass-1 feature; `partition_zone` grid kept, unused) | **done, tested** |
| `progress.py` | `ProgressTracker` (percent + ETA) | **done, tested** |
| `wind.py` | per-feature upstream wind (`make(hour)->provider`) | **done** (AROME HD via Open-Meteo + fallback) |
| `pipeline.py` | `AutoConfig` / `AutoResult` / `run_auto` orchestrator | **done** (integration-run) |
| `scene.py` | aggregate one hour's cases into a 3D scene | **done** (reuses `viz.volume3d`) |
| `window.py` | the 2-tab IHM (route → run → 3D time slider) | **done** (needs live tuning) |

## Reuse map (no duplication)

- DEM: `terrain.acquire.prepare_dem` (IGN/world, the de-stripe + tile parallelism), `terrain.dem.crop_dem/load_dem/write_dem`.
- Solver: `flow.windninja.run_momentum` + `format_run_failure`; cases via `flow.openfoam_reader`.
- Wind: `auto.wind.local_wind_provider` (AROME France HD via Open-Meteo, fallback crest blend).
- 3D: `viz.volume3d` building blocks (`_terrain_mesh`, `_drape_basemap`, `_add_rotor`,
  `_clip_domain_boundary`, `_add_north_arrow`, `_add_horizontal_scale_bar`, `mean_flow_vector`).
- Concurrency: `screening.pass1.parallel_run_plan` policy + the parallel-then-sequential-retry pattern.
- Misc: `timing.RunTimings`, `app.jobs.SolveJob` (worker thread), `app.map_tab.MapTab` (Leaflet route/AOI).

## UI contract (`window.py`)

- **Tab 1 — sélection:** `MapTab(mode="route")` (IGN; draw the flight route — left-click add,
  right-click undo, double-click finish) + a **window range slider** (absolute AROME dates) +
  **« Calculs simultanés »** + **« Marge corridor »** + **« Valider »** → `run_auto` on a
  `SolveJob`; the CPU plan shows integer division (`jobs × threads/job = used/total cores`,
  perfect divisors, useful cap from the selected window). A live step **log** + a
  **% / elapsed / ETA** line (a 1 s timer keeps it ticking).
- **Tab 2 — rendu 3D:** an embedded `QtInteractor` (terrain-locked rotation, ADR: azimuth/elev) +
  an **hour slider** over the computed range → `populate_auto_scene(result.cases_for_hour(h))`;
  pan/zoom/tilt + a movable focal point (PyVista camera). Switches here when the run finishes.

## Key decisions / open items

- **AROME wind: wired via Open-Meteo HD.** `auto.wind` reads **AROME France HD (1.5 km)**
  height-AGL wind from Open-Meteo's `arome_france_hd` (highest available level ≈120 m), per
  feature centre → real valley-scale variation, keyless. This beats the Météo-France GRIB API
  (2.5 km, 10–100 m, GRIB-only → needs `eccodes`), so GRIB is deferred to an optional path for
  >120 m / pressure levels. The `.env` key still labels/gates the run + drives the slider window.
- **Momentum is CPU-bound (ADR-0006)** and its temp env can't be redirected (OpenFOAM crash,
  Entry 38), so `momentum_workers` defaults to **min(4, detected cores)**; the UI slider can
  raise it to all cores after benchmarking RAM/disk behaviour. The honest speed lever remains
  `mesh_count` + a lean per-feature domain.
- **Cost.** A run is `len(zones) × len(hours)` momentum solves — minutes to a long while; the ETA
  sets expectations. Caching (`*_vel.asc` reuse, the DEM cache) keeps re-launches cheaper.
- **Disk cleanup.** Auto OpenFOAM cases are kept while the window is open, then deleted on close
  (and before the next run) by `cleanup_auto_artifacts`; reusable DEMs + Pass-1 screening cache stay.
- **3D georeferencing.** Web-tile basemaps are reprojected from WebMercator to the DEM CRS before
  texture draping; terrain vertices use DEM pixel centres, and the 3D scene includes a horizontal
  scale bar (ADR-0027).
- **Global height legend.** `scene.py` currently legends off the first rotor; a global height
  `clim` across zones is a small follow-up (`_add_rotor(clim=…)` already supports it).
