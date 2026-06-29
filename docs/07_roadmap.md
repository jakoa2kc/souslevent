# 07 — Roadmap

Sequenced so that an **end-to-end skeleton works before any physics enrichment**. Each
milestone is independently demonstrable.

## M0 — Scaffold (done)
Repository structure, docs/traceability, module stubs, packaging, prompts. ✔

## M1 — Pass-1 pipeline, end to end ✔ (done)
Prove the whole screening chain on **one known relief**.
- [x] `terrain/dem.py`: load a real IGN/SRTM DEM → reproject **UTM north-up**, meters,
      validate (<50 km), fill no-data. ✔
- [x] `terrain/geometry.py`: slope, aspect, ridge detection, Winstral shelter index. ✔
- [x] `wind/forecast.py` + `profile.py`: hourly wind profile (Open-Meteo / AROME HD) →
      crest-height wind per hour. ✔
- [x] `flow/windninja.py`: `run_mass(...)` shelling out to `WindNinja_cli`, ASCII u,v out. ✔
- [x] `screening/indicator.py`: geometry + velocity deficit + empirical rules →
      normalized hazard indicator + ranked candidates (`find_candidates`). ✔
- [x] `viz/map2d.py`: 2D hazard map (single hour + **time slider** + animated GIF). ✔
- [x] `scripts/demo_pass1.py`: runs the above; `scripts/champsaur_pass1_hourly.py` adds the
      hourly loop on the Champsaur IGN DEM. ✔
**Definition of done:** a 2D, time-sliderable hazard map over a real area, from a real
DEM and real (or stubbed) hourly winds — *useful even with zero momentum runs*.

## M2 — Pass-2 single run + 3D view
Prove the detailed branch on **one feature/hour**.
- [x] `flow/windninja.py`: `run_momentum(...)` (crop+buffer DEM, domain-average wind,
      `momentum_flag`, `turbulence_output_flag`, `mesh_count`, iterations). **Runs NATIVELY
      on Windows** (WindNinja 3.12); needs `write_goog_output` with turbulence. ✔
- [x] Capture/record the **OpenFOAM case directory** path for the run. `locate_openfoam_case`
      finds the `NINJAFOAM_*` dir written next to the DEM. ✔
- [x] `flow/openfoam_reader.py`: read the case via PyVista → 3D field. Verified on a real
      case (65k cells; U/k/epsilon/nut; ~26% reversed-flow cells). ✔
- [x] `viz/volume3d.py`: terrain (STL) + reversed-flow + turbulence-intensity volumes +
      best-effort streamlines; interactive `show` and headless `save_png`. ✔
- [x] `scripts/demo_pass2_single.py` end-to-end (+ `scripts/pass2_smoke_test.py` for the
      solver smoke test). ✔
**Definition of done:** a real 3D recirculation volume for a hand-picked arête + wind. **✔
MET (2026-06-21)** — headless PNG of the Champsaur candidate shows the lee rotor volume.
**De-risked:** solver + case-read + 3D render all work natively on Windows (no Docker for
Pass 2; Docker stays a *scale* option for M4 batch only). Remaining polish: clearer
streamlines, terrain-vs-volume opacity. Next: M3 click-to-detail handoff.

## M3 — Wire the handoff (Pass 1 → Pass 2)
- [x] Read upstream wind from the Pass-1 field at a candidate: the handoff samples the
      `*_vel`/`*_ang` grids a short fetch upstream of the click for the Pass-2 BC, falling
      back to the controls wind. ✔ *(True crest-height free-stream vs surface 10 m: later.)*
- [x] Click a hotspot in `map2d` → crop+buffer → queue a momentum run (worker) → show the
      3D scene. Implemented in the IHM (`on_map_click` → `SolveJob` → 3D tab). ✔
- [~] Buffer heuristics. *(Interim: centered ±2.5 km window; asymmetric downwind margin so
      big rotors aren't truncated is TODO.)*
**Definition of done:** click-to-detail works for one area across a few hours. *(Single-wind
click-to-detail works end to end; multi-hour + upstream wind are the remaining refinements.)*

## M4 — Robust hourly batch + spatial wind input
- [ ] Full hourly loop over a flight window; caching of DEM + forecasts for reproducibility.
- [x] **AROME API access (ADR-0016):** Météo-France AROME apiKey supported via `.env`;
      `wind/meteofrance.check_arome_key` validates it offline + IHM popup on expiry.
      Renewal procedure: docs/support/meteofrance_arome.md. ✔
- [~] **Spatial wind via AROME sub-zones (ADR-0007):** mechanism done — `screening.subzones`
      tiles the domain, runs the mass solver per tile **in parallel** (ThreadPoolExecutor,
      CPU-capped per run — ADR-0017), mosaics with feathered blending; `wind.forecast.fetch_arome`
      + `wind.profile.crest_wind_provider` supply per-tile winds.
      *Remaining: GRIB2 ingestion (cfgrib) of `/public/arome/1.0` at crest levels; default the
      GUI/loop to AROME (vs the ~11 km Open-Meteo blend / synthetic).*
- [ ] Evaluate **Docker/Katana** batch vs native subprocess (ADR-0006 open question);
      tied to the eventual GRIB `wxModel` route.
**Definition of done:** a full-window screening run is one command and reproducible, with
valley-to-valley wind differentiation.

## M5 — Physics enrichment (Pass 1 first)
- [ ] Diurnal slope winds + non-neutral **stability** in the mass solver (Pass 1).
- [ ] **Resolve the open question:** stability/diurnal availability on the **momentum**
      solver; record as an ADR; enrich Pass 2 if supported.
**Definition of done:** screening reflects time-of-day & stability, not just gradient wind.

## M6 — UX hardening
- [x] Basemap under the Pass-1 map (IGN Géoplateforme / OSM / OpenTopoMap) for orientation,
      with hillshade fallback offline (ADR-0010). ✔
- [ ] Uncertainty communication in the UI (candidates ≠ rotors; RANS-mean caveats).
- [x] Save/load an analysis with provenance — auto results to `.sillage` (DEM + lee meshes +
      route winds + params + run-day labels), reopened without recomputing (ADR-0030). ✔
- [ ] **Pass-2 mesh resolution as a quality/time knob (ADR-0008):** preset (coarse/medium/
      fine) or target near-surface resolution, with a displayed time/RAM estimate; default
      medium, "refine to max" on doubt; bounded by a cost estimator.
- [ ] Performance: concurrent hourly mass runs (CPU cores); GPU-accelerated rendering.

## M7 — Desktop IHM (PySide6, ADR-0009)
The "real software" surface. Built incrementally; adapt as results come in.
- [x] "Carte" tab: interactive Leaflet/QtWebEngine map (IGN / OSM / OpenTopoMap), centred on
      Ancelle (~30 km), world-zoomable, with a rectangle tool that captures the Pass-1 AOI
      (ADR-0012). ✔
- [x] Wire the selected AOI → DEM preparation: "Valider la zone" prepares a coarse (~90 m)
      worldwide DEM from terrarium tiles (`terrain/acquire.py`) on the worker, then moves to
      the créneau tab; the prepared DEM drives Pass-1 (ADR-0013). ✔
- [x] Per-feature **fine** DEM for Pass-2: on launch, re-fetch the window at **IGN 5 m** native
      (terrarium fallback) instead of reusing the zone MNT (ADR-0018; toggle in the UI). ✔
- [x] App shell: controls + Pass-1 (2D matplotlib) / Pass-2 (3D pyvistaqt) tabs;
      `sillage-gui` entry. Reuses headless rendering (`map2d.draw_indicator`,
      `volume3d.populate_plotter`); 3D viewport created lazily (needs a GL context). ✔ slice 1
- [x] Worker thread for long solves (WindNinja mass/momentum): progress + cancel
      (`app/jobs.py` SolveJob; streaming runner in `flow/windninja`). ✔ slice 2
- [x] Hourly time slider in the 2D tab (synthetic hourly winds; worker-driven loop;
      scrubbing swaps the per-hour wind field used by the handoff). ✔ slice 6
- [~] AROME sub-zone Pass-1 (ADR-0007) in the 2D tab. Mechanism + "Run sub-zones (spatial)"
      button done (synthetic provider); *remaining: AROME-fed + per-hour sub-zone stack.* ✔ slice 7
- [x] Click-on-map hotspot → crop+buffer → launch Pass-2 → show 3D (the M3 handoff). ✔ slice 3
      *(superseded by the rectangle selection below, ADR-0015.)*
- [x] **Pass-2 selection by rectangle on the créneau tab** (like the Pass-1 AOI), mesh preset +
      launch button on that tab, 3D tab **display-only** (ADR-0015). ✔ slice 8
- [x] Mesh quality/time knob (ADR-0008) in the Pass-2 controls: Coarse/Medium/Fine/Max
      presets + rough cell-count/minutes estimate; the handoff uses the selection. ✔ slice 4
- [x] Upstream wind sampling from the Pass-1 field for the Pass-2 BC. ✔ slice 5
**Definition of done:** browse screening by hour, draw a Pass-2 rectangle, get the 3D rotor — one app.

## M8 — Automatic full-resolution pipeline (`sillage.auto`, ADR-0022)
A "one-click" mode parallel to the manual app: zone + window → solve the WHOLE zone at the finest
topo scale, then a time-sliderable global 3D wake. See docs/10_auto_pipeline.md.
- [x] Engine: **feature-based domains** (`feature_domains` from Pass-1 candidates — no grid seams,
      ADR-0023), `ProgressTracker` (ETA), `local_wind_provider`, `run_auto` orchestrator (reuses
      momentum + retry), `populate_auto_scene` aggregation. Tested. ✔
- [x] 2-tab IHM skeleton (`auto.window`): MapTab + window slider → run_auto on a worker; 3D tab +
      hour slider. Constructs headless. ✔
- [x] **AROME 1.5 km local wind** via Open-Meteo `arome_france_hd` (height-AGL, highest level),
      per sub-zone → valley-scale variation; keyless JSON (finer than the MF 2.5 km GRIB API).
      The `.env` key still labels/gates the run + drives the slider window. ✔
- [x] Parallel feature/hour solves (`momentum_workers` conservative default, slider up to cores)
      + live progress (steps, %, ETA). ✔
- [x] **Blind corridor paving** (`domain_mode="corridor"`, ADR-0029): Pass-2 everywhere along the
      route at max topo res, no Pass-1; **multi-segment routes** skip valley crossings (ADR-0030). ✔
- [x] **Rendering**: four lee representations (rotor / horizontal % / vertical m/s / turbulence rms),
      single absolute scale across sectors, adjustable maxima + volume floors, uniform opacity slider,
      2-D + continuous wind legends; overlaps drawn by nearest sector (no alpha-stacking); turbulence
      as absolute rms (comparable between domains); all four persisted in `.sillage` (ADR-0031). ✔
- [x] **Wave-based progress/ETA** (ADR-0028); disk-safe cases (ADR-0025); right-drag 3D pan. ✔
- [x] **Save/open** results as a portable `.sillage` bundle (lee meshes + route winds + params), with
      run-day labels (ADR-0030); both apps render identically (Entry 60). ✔
- [ ] Optional: the Météo-France **GRIB** path (eccodes) for >120 m AGL / pressure levels.
- [ ] Open levers: per-sector IGN 5 m fetch (vs one corridor DEM), turbulence-volume floor as a
      validated danger threshold, `momentum_workers` vs CPU benchmarking.

## Later / research
- Humidity & latent effects; lee-wave structure; validation/hindcast against known flying
  days via ERA5; possible mobile/web consultation surface (Cesium/deck.gl) on top of the
  Python core.

## Cross-cutting, always-on
- Keep `06_dev_log.md` and `03_decisions.md` current — they are the project's memory.
- Keep `flow/windninja.py` flags verified against the installed WindNinja version.
- Never let Pass-1 output be presented as a rotor map.
