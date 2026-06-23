# 06 — Development log (reasoning trail)

A chronological journal of the *thinking*, not just the code. Append newest entries at
the bottom. The point is that a third party or AI can reconstruct **how** we arrived here,
including ideas we tried and dropped. Keep entries dated and honest about dead ends.

---

## Entry 1 — Initial concept

**Idea.** Build an app that computes and shows, in 3D, the leeward/windward zones over a
flying area from fine cartography + fine wind forecasts by altitude and hour. Imagined a
"méca flu" core for airflow around terrain — starting simple (perfect-gas / inviscid
style), later adding stability, humidity, etc. — plus a 3D particle/volume viewer, fine
DEM ingestion, and fine wind-by-altitude forecast ingestion. Develop in VSCode.

**First assessment.**
- The need is real; visual estimation of lee zones is a genuine gap in paragliding
  practice, so a dedicated tool has real value.
- **Big trap identified:** true Navier-Stokes CFD over real mountain terrain at fine
  resolution is *not* an interactive-laptop computation (WRF/OpenFOAM/SU2 class: fine 3D
  mesh, cluster hours, calibration expertise). Going straight there risks 18 months of
  convergence work and never flying with the tool.
- **Potential/inviscid "perfect gas" flow rejected:** it never separates, so by
  construction it produces **no rotor** — exactly the phenomenon of interest. Seductive
  but wrong for this problem.
- **Language:** Python — the work is ~80% data/geo/viz integration; ecosystem is
  unrivalled there. Drop to C++/Rust only if a real perf wall appears later.
- Proposed gradation: empirical heuristics → mass-consistent diagnostic (e.g. WindNinja)
  → RANS later. Start simple.

**Tooling sketch.** terrain (rasterio, IGN RGE ALTI 1 m), wind (Open-Meteo / AROME),
flow (WindNinja or mass-consistent), viz (PyVista/VTK).

---

## Entry 2 — User direction

**Decisions from the pilot:**
- Flow core: **wrap WindNinja** to start (fast, proven) rather than build from scratch.
- Target: **desktop PC app**, workstation has an **NVIDIA RTX 5060 Ti** available; first
  goal is to *see results and explore possibilities*.
- Programming comfort: **advanced**.

**Implication.** Lean into wrapping + Python glue; can be technical. GPU available → note
where it actually helps.

---

## Entry 3 — The pivotal WindNinja finding (reshaped the architecture)

Investigated WindNinja's solvers. **Key discovery that changed the plan:**
- WindNinja has **two** solvers. The fast **conservation-of-mass** solver, by how it
  represents momentum, **cannot capture eddies (reversed flow) at all** — in a lee eddy it
  shows only very low speed, never reversal. → **It cannot show the rotor.**
- The **conservation-of-mass-and-momentum** solver (**NinjaFOAM**, built on OpenFOAM
  `simpleFoam`, k-epsilon, terrain-following hex mesh) **does** capture lee eddies. → this
  is the one that answers our question. It is, in effect, real RANS CFD — so the "v2 =
  write real CFD" idea is unnecessary; it's already here.

**Two consequences:**
1. The "simple potential core then real CFD" plan collapses into "use the momentum solver
   for the real thing." (ADR-0002, ADR-0003.)
2. **Constraint:** the momentum solver **does not support weather-model or point
   initialization — only a single domain-average wind.** So the spatially-varying forecast
   *cannot* drive it directly.

Also learned:
- WindNinja simulates **one instant** → flight window = **hourly loop**, one run per hour.
- DEM must be **north-up UTM, meters H+V, < ~50 km**.
- `turbulence_output_flag` exists → turbulence intensity as a danger proxy.
- **Gotcha:** momentum `write_vtk_output` writes the **mass-mesh**, not the OpenFOAM
  field. Real 3D must come from reading the **OpenFOAM case directory** (PyVista). (ADR-0004.)
- **GPU reality:** OpenFOAM `simpleFoam` is **CPU-bound**; the RTX accelerates
  **rendering**, not the solve. (ADR-0006.)

---

## Entry 4 — The two-pass architecture (pilot's refinement, adopted)

**Pilot proposal (accepted as the core design):** first a *fast mass* computation over the
**whole map and the whole route**, with the local winds (altitude, hour by hour), for a
first **coarse** visualization/analysis; then a *more precise NinjaFOAM* computation at
**key identified places/moments** (rock arêtes, summits, shoulders, combes at a given
hour, hence with a homogeneous wind over that limited zone).

**Why this is the right call:**
- It is textbook **adaptive multi-resolution**: cheap broad screening + expensive local
  refinement.
- It uses each solver **exactly where it is valid**: Pass 1 is the **only** place the
  spatially-varying forecast can enter (weather-model init is mass-only); Pass 2 is in its
  natural regime (small domain, one upstream wind = sound BC).

**The crucial correction recorded so we don't fool ourselves in flight:**
- The mass solver **cannot draw rotors**. So **Pass 1 is a *candidate detector*, not a
  rotor map.** Its real signal is a **derived indicator** built from: (a) terrain geometry
  (lee-slope steepness vs the hour's wind direction; crest/arête/shoulder detection),
  (b) mass-field signals (downwind **velocity deficit**; strong sub-crest velocity
  gradient as a separation proxy), and (c) empirical rules (crest-wind/height ratio;
  ~5–7×H downwind extent). Threshold on the combination → "run momentum here." (ADR-0005.)
- **Even cheaper pre-filter:** a purely geometric **Winstral shelter index** (max upwind
  slope within a search distance → sheltered vs exposed per wind direction), computed from
  the DEM with no solver call.

**Pass-2 refinements noted:**
- **Buffer** the crop: upstream fetch for flow to establish + generous downwind margin so
  the recirculation isn't truncated by the outlet boundary.
- **Resolution:** Pass 1 coarse (~30–100 m), Pass 2 fine (~10–30 m, toward ~10^6 cells);
  the IGN 1 m DEM is the shared source. Computational (mesh) resolution drives cost, not
  DEM resolution.
- **Handoff:** Pass 2's homogeneous wind = the wind **read from the Pass-1 field** at
  crest height upstream of the feature, that hour. Pass 1 manufactures Pass 2's BC.

**Two distinct quantities → two distinct views:** a 2D screening map + time slider (Pass
1, triage) and a separate 3D recirculation scene (Pass 2, detail). Don't blend them.
(ADR-0005.)

**Physics axis flagged:** anabatic/katabatic + stability matter a lot in mountains.
Diurnal slope winds + non-neutral stability are available in the **mass** solver → natural
enrichment of **Pass 1**, *after* the skeleton works. Their availability on the
**momentum** solver is **to verify** (open question).

---

## Entry 5 — Scaffolding the project (this commit)

Created the repository skeleton: docs (overview, theory, architecture, ADRs, data
sources, WindNinja integration, this log, roadmap, glossary), support docs
(troubleshooting, environment), AI/third-party prompts, src-layout package with module
stubs and real starter code for `terrain` and `screening` plus the WindNinja wrapper and
OpenFOAM reader contracts, a Pass-1 demo script, and packaging files.

**Next:** stand up the Pass-1 pipeline end to end on a known relief (real DEM, one wind,
mass run, indicator, 2D map). Then close the momentum/stability open question and wire the
Pass-1→Pass-2 click handoff. See `07_roadmap.md`.

---

## Entry 6 — V0 baseline + Pass-2 verified native on Windows  (2026-06-21)

**What changed.** Brought the project from scaffold to a working **V0** (commit tagged
`v0`). Installed WindNinja 3.12 natively on Windows and wired it via `.env`. Centralized
all generated artefacts out-of-tree under `C:\A2K\SousLeVent` (config.py). Built the Pass-1
**hourly loop** (mass per hour + time-slider map + GIF). Then de-risked Pass-2 with a smoke
test on the Champsaur top candidate.

**Why.** Pass-2 viability on Windows had been an open worry (OpenFOAM is Linux-native; we
assumed Docker might be required).

**Result / decision.** The **momentum solver runs natively on this Windows build** — no
Docker needed for the solve. Findings: WindNinja 3.12 requires `write_goog_output` when
`turbulence_output_flag=true`; NinjaFOAM writes the OpenFOAM case as `NINJAFOAM_*` next to
the **DEM** (not in the run working dir) — fixed `locate_openfoam_case` accordingly. The
full read path works (`openfoam_reader` → 65k cells, ~26% reversed-flow cells, TI≈0.17),
and `volume3d` renders the rotor volume both interactively and headless (PNG). Docker is
demoted to a **scale** option for the M4 batch only.

**Two architecture decisions recorded (this entry's main output):**
- **ADR-0007** — Pass-1 spatial wind via **AROME sampled per sub-zone** (interim). Chosen
  over both the current single-domain-average and the full GRIB `wxModel` gridded init.
  Captures valley-to-valley differences cheaply via Open-Meteo's AROME endpoint (no key).
  Key clarification: **sub-zones are horizontal tiles**; altitude enters as the *per-zone
  sampling height* of the AROME vertical profile, not as a separate partition axis.
- **ADR-0008** — Pass-2 **mesh resolution is a UI quality/time knob** (default medium,
  "refine to max" on doubt). The limiter is mesh cells × iterations (CPU-bound), not the
  5 m DEM; uniform 5 m would be millions of cells.

**Open questions raised.** Seam handling when stitching AROME sub-zone fields (overlap +
blend). Cost estimator (cells → minutes) to bound the "refine" control. Eventual move to
full gridded `wxModel` init (M4/M5) supersedes ADR-0007.

---

## Entry 7 — IHM kickoff: PySide6 desktop shell (slice 1)  (2026-06-21)

**What changed.** Locked the UI framework (**ADR-0009**: PySide6 + pyvistaqt) and scaffolded
the desktop app: `src/sillage/app/main_window.py` + `scripts/sillage_gui.py` (`sillage-gui`
entry). First vertical slice: a controls panel + two tabs — **Pass-1 screening** (embedded
matplotlib canvas) and **Pass-2 detail** (embedded pyvistaqt `QtInteractor`). Refactored
`viz.volume3d` (`populate_plotter`) and `viz.map2d` (`draw_indicator`) so the app reuses the
*exact* headless rendering rather than duplicating it.

**Why.** Begin the "real software with IHM" phase; iterate on results (user's call).

**Result.** Window builds headless (`QT_QPA_PLATFORM=offscreen`); "Compute Pass-1
(geometry)" loads the real Champsaur DEM (775×824) and draws the hazard map in the embedded
canvas. The **3D viewport needs a real GL context** (VTK fails to get a pixel format under
offscreen), so it is created **lazily** on first Pass-2 use — verified on the workstation,
not in headless CI. GUI deps isolated in the `[gui]` extra. Tests: 23 passed.

**Open questions raised.** Worker-thread/job model for the long WindNinja/OpenFOAM solves
(progress + cancel) — next increment. Then the click-on-map → launch Pass-2 handoff (M3),
the hourly slider in-app, and the ADR-0008 mesh knob in the Pass-2 controls.

---

## Entry 8 — IHM slice 2: worker thread for solves (progress + cancel)  (2026-06-21)

**What changed.** Long WindNinja solves now run **off the UI thread**. `flow.windninja._run`
gained a streaming `Popen` path (parses `% complete`, cooperative **cancel** via subprocess
terminate/kill); `run_mass`/`run_momentum`/`hourly_indicator` forward `on_progress`/`cancel`
(default `None` → unchanged blocking path, so the verified momentum smoke is untouched). New
`src/sillage/app/jobs.py` `SolveJob` (worker `QObject` moved to a `QThread`, signals
`progress`/`finished`/`failed`). `MainWindow` gained a **Run WindNinja mass** button, a
**progress bar**, and a **Cancel** button; the map renders on completion.

**Why.** A multi-minute momentum solve (and even a mass run) must not freeze the IHM.

**Result.** Verified headless: `SolveJob` delivers progress → finished, and cancel → failed
with a "cancelled" message; a **real** WindNinja mass run driven through the worker reached
100%, rendered the hazard map, and re-enabled the buttons. Tests: 26 passed (added
`_parse_progress`, streamed-progress capture, and cancel-terminates).

**Open questions raised.** Next IHM slices: hourly time slider + AROME sub-zones (ADR-0007)
in the 2D tab; then the **click-on-map → Pass-2 handoff (M3)**, reusing the same `SolveJob`
to launch the momentum solve and show the 3D rotor.

---

## Entry 9 — IHM slice 3: click-to-detail handoff (M3)  (2026-06-21)

**What changed.** Left-clicking a hotspot on the Pass-1 map now launches a Pass-2 momentum
solve there: `on_map_click` → `crop_dem` (±2.5 km window) → `run_momentum` via the slice-2
`SolveJob` (progress + cancel) → load the OpenFOAM case into the embedded 3D viewport and
switch to the Pass-2 tab. The picked spot is starred on the map. Guards: ignores pan/zoom
clicks, requires a Pass-1 map first, one job at a time, and confirms before the multi-minute
solve.

**Why.** This is the core experience — triage in 2D, then resolve the actual rotor in 3D on
demand — and the payoff of the worker-thread foundation.

**Result.** Verified headless end-to-end (reduced mesh for speed): click coords → crop →
momentum through the worker (33 progress samples → 99%) → located the
`NINJAFOAM_ihm_crop_*` case and returned it with the wind. The embedded 3D render needs a
real GL context (proven separately on the real case). Tests: 26 passed.

**Interim choices / open questions.** Pass-2 wind currently = the controls' domain wind
(upstream-crest sampling from the Pass-1 field is the next refinement); crop is a centered
square (asymmetric downwind margin TODO); mesh fixed at 50k (ADR-0008 quality/time knob is
the next IHM slice).

---

## Entry 10 — IHM slice 4: Pass-2 mesh quality/time knob (ADR-0008)  (2026-06-21)

**What changed.** Added a mesh preset combo to the Pass-2 controls
(Coarse/Medium/Fine/Max → `(mesh_count, iterations)`) with a rough "~N cells, ~M min"
estimate label. The click-to-detail handoff now uses the selected preset instead of a fixed
50k. Default = Medium.

**Why.** ADR-0008: make the time-vs-lee-accuracy trade explicit; "refine on doubt" by
picking a finer preset; the rough estimate bounds the choice.

**Result.** Verified headless: default Medium (50k/200); switching to Fine → 150k/300 with
the hint updating; the confirm dialog quotes the chosen preset + estimate. Replaced a couple
of non-ASCII glyphs in UI strings to avoid console-encoding noise. Tests: 27 passed.

**Open questions.** The estimate is a crude linear proxy (could calibrate per-machine); a
"target near-surface resolution" input could replace presets later.

---

## Entry 11 — IHM slice 5: upstream wind for the Pass-2 BC (M3 refinement)  (2026-06-21)

**What changed.** The click-to-detail handoff now derives the Pass-2 boundary wind from the
**Pass-1 field** instead of the controls. New `screening.pass1` helpers:
`find_direction_grid`, `sample_grid_at`, `upstream_crest_wind` (samples the `*_vel`/`*_ang`
grids a short fetch upstream of the click, toward the wind's source bearing). The GUI stores
the last mass run's vel/ang grids; `_pass2_wind_at` returns the upstream-sampled
(speed, from_deg) when available, else the controls wind — the confirm dialog and status line
show which source was used.

**Why.** docs/05 / ADR-0003: Pass-2's single homogeneous wind should be the wind just
**upstream** of the feature read from Pass-1, not a global domain wind.

**Result.** Verified: helpers sample a synthetic field correctly; the GUI returns
"Pass-1 upstream" when a mass field is present (and "controls" otherwise). Tests: 29 passed.

**Limitations.** Samples the Pass-1 **surface (10 m)** wind, not a true crest-height
free-stream; fetch is a fixed 1.5 km; the field reflects the *last* mass run (stale if the
controls wind changed since). Asymmetric downwind crop margin is still TODO.

---

<!-- TEMPLATE for new entries — copy below the line
## Entry N — <short title>  (YYYY-MM-DD)
**What changed / what I tried.**
**Why.**
**Result / decision.** (link any new ADR)
**Open questions raised.**
-->
