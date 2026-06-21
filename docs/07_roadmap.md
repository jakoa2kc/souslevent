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
- [ ] `viz/map2d.py`: 2D hazard map (single hour first, then the **time slider**).
- [ ] `scripts/demo_pass1.py`: runs the above on the chosen relief.
**Definition of done:** a 2D, time-sliderable hazard map over a real area, from a real
DEM and real (or stubbed) hourly winds — *useful even with zero momentum runs*.

## M2 — Pass-2 single run + 3D view
Prove the detailed branch on **one feature/hour**.
- [ ] `flow/windninja.py`: `run_momentum(...)` (crop+buffer DEM, domain-average wind,
      `momentum_flag`, `turbulence_output_flag`, `mesh_count`, iterations).
- [ ] Capture/record the **OpenFOAM case directory** path for the run.
- [ ] `flow/openfoam_reader.py`: read the case via PyVista → 3D field.
- [ ] `viz/volume3d.py`: terrain + streamlines + **reversed-flow** and/or
      **turbulence-intensity** volumes (windward green / leeward red-orange).
- [ ] `scripts/demo_pass2_single.py`.
**Definition of done:** a real 3D recirculation volume for a hand-picked arête + wind.

## M3 — Wire the handoff (Pass 1 → Pass 2)
- [ ] Read crest-height upstream wind from the Pass-1 field at a candidate.
- [ ] Click a hotspot in `map2d` → build `(feature_bbox+buffer, hour, wind)` → queue a
      momentum run → show the 3D scene.
- [ ] Buffer heuristics (upstream fetch, downwind margin) tuned so eddies aren't truncated.
**Definition of done:** click-to-detail works for one area across a few hours.

## M4 — Robust hourly batch
- [ ] Full hourly loop over a flight window; caching of DEM + forecasts for reproducibility.
- [ ] Evaluate **Docker/Katana** batch vs native subprocess (ADR-0006 open question).
**Definition of done:** a full-window screening run is one command and reproducible.

## M5 — Physics enrichment (Pass 1 first)
- [ ] Diurnal slope winds + non-neutral **stability** in the mass solver (Pass 1).
- [ ] **Resolve the open question:** stability/diurnal availability on the **momentum**
      solver; record as an ADR; enrich Pass 2 if supported.
**Definition of done:** screening reflects time-of-day & stability, not just gradient wind.

## M6 — UX hardening
- [ ] Uncertainty communication in the UI (candidates ≠ rotors; RANS-mean caveats).
- [ ] Save/load an analysis with full provenance (DEM, forecast run, params).
- [ ] Performance: concurrent hourly mass runs (CPU cores); GPU-accelerated rendering.

## Later / research
- Humidity & latent effects; lee-wave structure; validation/hindcast against known flying
  days via ERA5; possible mobile/web consultation surface (Cesium/deck.gl) on top of the
  Python core.

## Cross-cutting, always-on
- Keep `06_dev_log.md` and `03_decisions.md` current — they are the project's memory.
- Keep `flow/windninja.py` flags verified against the installed WindNinja version.
- Never let Pass-1 output be presented as a rotor map.
