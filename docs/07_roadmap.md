# 07 — Roadmap

Sequenced so that an **end-to-end skeleton works before any physics enrichment**. Each
milestone is independently demonstrable.

## M0 — Scaffold (done)
Repository structure, docs/traceability, module stubs, packaging, prompts. ✔

## M1 — Pass-1 pipeline, end to end (current target)
Prove the whole screening chain on **one known relief**.
- [ ] `terrain/dem.py`: load a real IGN/SRTM DEM → reproject **UTM north-up**, meters,
      validate (<50 km), fill no-data.
- [ ] `terrain/geometry.py`: slope, aspect, ridge detection, Winstral shelter index.
- [ ] `wind/forecast.py` + `profile.py`: fetch hourly wind profile (Open-Meteo) →
      crest-height wind per hour. (Start with a single hour / hard-coded wind to unblock.)
- [ ] `flow/windninja.py`: `run_mass(...)` shelling out to `WindNinja_cli`, ASCII u,v out.
- [ ] `screening/indicator.py`: combine geometry + velocity deficit + empirical rules →
      normalized hazard indicator + ranked candidates.
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
- [ ] Read crest-height upstream wind from the Pass-1 field at a candidate.
- [ ] Click a hotspot in `map2d` → build `(feature_bbox+buffer, hour, wind)` → queue a
      momentum run → show the 3D scene.
- [ ] Buffer heuristics (upstream fetch, downwind margin) tuned so eddies aren't truncated.
**Definition of done:** click-to-detail works for one area across a few hours.

## M4 — Robust hourly batch + spatial wind input
- [ ] Full hourly loop over a flight window; caching of DEM + forecasts for reproducibility.
- [ ] **Spatial wind input via AROME sub-zones (ADR-0007):** sample AROME (Open-Meteo
      endpoint, no key) per sub-zone at its crest altitude; run the mass solver per tile;
      stitch with overlap + blending. Stepping stone to full gridded `wxModel` init.
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
- [ ] Uncertainty communication in the UI (candidates ≠ rotors; RANS-mean caveats).
- [ ] Save/load an analysis with full provenance (DEM, forecast run, params).
- [ ] **Pass-2 mesh resolution as a quality/time knob (ADR-0008):** preset (coarse/medium/
      fine) or target near-surface resolution, with a displayed time/RAM estimate; default
      medium, "refine to max" on doubt; bounded by a cost estimator.
- [ ] Performance: concurrent hourly mass runs (CPU cores); GPU-accelerated rendering.

## Later / research
- Humidity & latent effects; lee-wave structure; validation/hindcast against known flying
  days via ERA5; possible mobile/web consultation surface (Cesium/deck.gl) on top of the
  Python core.

## Cross-cutting, always-on
- Keep `06_dev_log.md` and `03_decisions.md` current — they are the project's memory.
- Keep `flow/windninja.py` flags verified against the installed WindNinja version.
- Never let Pass-1 output be presented as a rotor map.
