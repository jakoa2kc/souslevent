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

## Entry 12 — IHM slice 6: hourly Pass-1 time slider  (2026-06-21)

**What changed.** A "Run hourly (Pass-1)" button runs a synthetic N-hour mass loop on the
worker (per-hour progress aggregated to an overall 0–100%), populating a **time slider** in
the 2D tab. Scrubbing redraws the map for that hour and swaps the Pass-1 wind field used by
the click-to-Pass-2 handoff, so each hour's click uses that hour's upstream wind. Single-map
actions (geometry / single mass) hide the slider. Factored `synthetic_series` into
`screening.pass1` (shared with `champsaur_pass1_hourly.py`).

**Why.** The M1 product — triage by hour — now lives inside the app.

**Result.** Verified headless: a 2-hour run via the worker reached 100%, slider max=1, scrub
0→1 redraws and swaps the wind field; the embedded canvas shows the per-hour map. Tests:
30 passed.

**Open questions.** Hours are synthetic for now; real spatial winds come from AROME
sub-zones (ADR-0007, the next slice). A save/export (GIF) button could reuse
`viz.map2d.save_timeline_gif`.

---

## Entry 13 — IHM slice 7: Pass-1 spatial wind via sub-zones (ADR-0007)  (2026-06-21)

**What changed.** Realized ADR-0007's interim. New `screening/subzones.py`: `subzone_bboxes`
tiles the domain (with overlap), `subzone_speed_field` runs the **mass solver per tile** with
that tile's own wind, and `assemble_mosaic` stitches the per-tile speed fields onto the full
DEM grid with **feathered blending** in the overlaps. The per-tile wind is a pluggable
provider. Added the AROME client `wind.forecast.fetch_arome` (Open-Meteo Meteo-France
endpoint, sharing the pressure-level core with `fetch_open_meteo`) and
`wind.profile.crest_wind_provider(source=...)` that samples the forecast at each tile centre's
crest altitude (memoized per lon/lat). New IHM button **"Run sub-zones (Pass-1, spatial)"**
runs a 2x2 sub-zone Pass-1 with a synthetic spatial wind on the worker and shows the
mosaicked map.

**Why.** Capture valley-to-valley wind differences without full gridded `wxModel` init.

**Result.** Verified: tiling + mosaic unit-tested; a real 2x2 sub-zone run on Champsaur
(4 mass runs, 150 m) mosaics to **full coverage** (775x824); the GUI button renders the
spatial map; `crest_wind_provider` tested with a mocked fetch. Tests: 33 passed.

**Limitations.** The GUI uses a *synthetic* spatial provider (AROME endpoint/variables to be
verified against a live response — no network here); the indicator's geometry term still uses
one representative direction; the sub-zone mosaic has no single vel/ang grid, so the
click-to-Pass-2 handoff after a sub-zone run falls back to the controls wind. Seams are
feathered, acceptable for a *screening* product. Eventual target = full gridded `wxModel`.

---

## Entry 14 — IHM: basemap under the Pass-1 map (orientation)  (2026-06-21)

**What changed.** Added an optional web-tile **basemap** under the Pass-1 2D map (**ADR-0010**):
`viz.map2d.add_basemap` + `BASEMAP_SOURCES` (IGN plan/ortho via the key-free Géoplateforme,
OpenStreetMap, OpenTopoMap) using **contextily** (reprojects tiles to the DEM CRS). A
"Basemap" combo in the IHM (default **IGN plan**); all Pass-1 views now go through one
`_render_map` that overlays the hazard at α≈0.5 over the basemap and **falls back to the
hillshade** if tiles can't be fetched. `contextily` added to the `[gui]` extra.

**Why.** Orientation — place names / roads / relief under the candidate zones.

**Result.** Verified: OSM / OpenTopoMap / IGN-plan tiles fetch from here; the geometry map
renders over IGN plan with the hazard on top; an unknown source raises (no-network test).
Tests: 34 passed.

---

## Entry 15 — IHM: French interface + Europe/Paris hourly times (ADR-0011)  (2026-06-21)

**What changed.** Translated the IHM to **French** (buttons, labels, tabs, dialogs, status,
titles, the shared `map2d.DISCLAIMER`, axis/colorbar labels) and loaded the `qtbase_fr`
translator in `scripts/sillage_gui.py` for Qt's built-in strings. The hourly slider now shows
**absolute Europe/Paris clock hours**: `screening.pass1.synthetic_series` labels each hour via
`zoneinfo` (e.g. "mar. 18h"), with a new `tzdata` dependency (Windows has no IANA db). Mesh
presets and the Pass-2 wind-source tags ("Pass-1 amont" / "contrôles") are French too.

**Why.** The user (French pilot) needs a French UI and real wall-clock flight-window hours.

**Result.** Verified: window title/tabs/buttons in French; `synthetic_series(4)` →
`['mar. 18h', 'mar. 19h', ...]`; a rendered geometry map is fully French (title, "Est/Nord
(m)", colorbar, disclaimer). Updated the one test asserting the wind-source tag. Tests 34.

**Open questions.** Developer-facing code/docs stay English (ADR-0011); dev scripts remain
partly English. A start-hour/day picker for the window could replace "now" as the default.

---

## Entry 16 — IHM: interactive selection map (Leaflet/QtWebEngine, ADR-0012)  (2026-06-21)

**What changed.** New **first tab "Carte"**: a Leaflet slippy-map in a `QWebEngineView`
(`app/map_tab.py`). Pan (drag) + scroll-zoom, zoom-out world-wide, centred on **Ancelle
(~30 km)**. Layers: **IGN plan/ortho** (key-free Géoplateforme WMTS), OSM, OpenTopoMap. A
**Leaflet.draw rectangle** returns its lat/lon bounds to Python via a **QWebChannel**
(`_MapBridge.on_rectangle` → `MapTab.aoiSelected`), stored as `MainWindow.selected_bbox`. The
launcher sets `AA_ShareOpenGLContexts` (WebEngine + VTK coexistence). The web view is
**skipped under the offscreen platform** (Chromium can't render and was crashing pytest at
exit) — a placeholder is shown there.

**Why.** Let the user navigate a real map and pick the Pass-1 AOI by rectangle.

**Result.** Verified headless: window builds with tabs `['Carte','Passe 1…','Passe 2…']`, a
simulated rectangle sets `selected_bbox`, suite exits cleanly (was code 5 from Chromium
teardown before the headless guard). `_build_html` produces valid Leaflet HTML with the
Ancelle fitBounds (~±0.27°/±0.38°) and the IGN layer. Tests: 34 passed (exit 0).

**Open questions.** Wire the AOI → DEM preparation for an arbitrary bbox (IGN RGE ALTI for any
area; today's pipeline is Champsaur-specific) so the rectangle actually drives Pass-1. A
"Préparer la Pass-1 sur cette zone" button will trigger it.

---

## Entry 17 — IHM reorg: workflow tabs, no left panel  (2026-06-21)

**What changed.** Removed the left controls panel; every control now lives in the tab it
belongs to, and the three tabs are renamed around the pilot's workflow:
1. **"Sélection de la zone de vol"** — the Leaflet map + the MNT (DEM) field.
2. **"Sélection du créneau de vol"** — Pass-1 controls (wind dir/speed, basemap,
   Géométrie/WindNinja masse/Horaire/Sous-zones, hours), the 2D canvas, and the
   hour ("Créneau") slider.
3. **"Analyse locale des zones sous le vent"** — Pass-2 mesh/case controls + the 3D viewport.
The job **progress bar + Cancel** moved to the **status bar** (a run can start from any tab).

**Why.** Match the user's mental model (zone → créneau → analyse locale) and free up width
for the map / canvas / 3D.

**Result.** Verified headless: the 3 renamed tabs, central widget is the tab stack (no side
panel), all controls live in their tabs, geometry compute still works, and the click→Pass-2
handoff switches to the analysis tab. Tests: 34 passed (exit 0).

---

## Entry 18 — Zone tab: "Valider" prepares the AOI DEM (worldwide, ADR-0013)  (2026-06-21)

**What changed.** Removed the useless MNT text field from tab 1. Added **"Valider la zone
(préparer le MNT)"**: it prepares a coarse (~90 m) DEM for the drawn rectangle on the worker
thread (progress in the status bar) and, when ready, **switches to the créneau tab**. New
`terrain/acquire.py`: `prepare_dem_for_bbox` fetches worldwide **terrarium** elevation tiles
(AWS, key-free) via `contextily.bounds2img`, decodes RGB→metres, and reprojects to UTM
(`zoom_for_resolution` picks/caps the tile zoom). The prepared file replaces `MainWindow.
_dem_path`, which now feeds every Pass-1 action (the old `dem_edit` is gone).

**Why.** Make the zone selection actually drive Pass-1, worldwide, without IGN per-département
downloads. Pass-1 is coarse, so no MNT-precision control is needed (ADR-0013).

**Result.** Verified end to end: a ~30 km AOI around Ancelle → "Valider" (progress
8→60→82→100) → `cache/aoi/dem_*.tif` (528×529 @ 109 m, elev 553-3388 m) → auto-switch to the
créneau tab. Offline tests mock `bounds2img` (decode→reproject ~1000 m preserved) and check
`zoom_for_resolution` capping. Tests: 36 passed.

**Open questions.** Per-feature **fine** DEM for Pass-2 (high-zoom crop) rather than reusing
the coarse zone DEM. Optional IGN RGE ALTI path for high-fidelity French zones.

---

## Entry 19 — IHM polish: maximized map, flight-window range slider, MNT view  (2026-06-21)

**What changed.**
- **Tab 1**: the Leaflet map is now maximized (info line removed from the MapTab); the AOI
  info sits **bottom-left** next to a **prominent green "Valider"** button.
- **Tab 2**: removed the manual **wind direction/speed** and **"Heures"** fields. Added a
  **double-handle range slider** (superqt `QRangeSlider`) for the **flight window** — clock
  hours of the day in Europe/Paris (label e.g. "mer. 09h → mer. 15h (6 h)"). On arrival
  (after "Valider"), the tab shows the **bare MNT** (hillshade, no hazard overlay) via the
  new `map2d.draw_hillshade`.
- **Wind source**: with the manual fields gone, every Pass-1 action now derives its wind from
  the selected window — `_window_series()` (synthetic per hour) and `_representative_wind()`
  (the window's first hour) for the single-shot buttons; the Pass-2 fallback wind tag became
  "créneau". (Wind stays synthetic until AROME is wired; only the *hours* are real.)

**Why.** Match the workflow: pick the zone, pick the flight window, screen — no loose manual
wind/hour fields. A pilot reads absolute clock hours, not "+N h".

**Result.** Verified headless: tab 2 has the range slider (no wind/hours fields), the window
label updates on drag, `_representative_wind()` = (6 m/s, 300°), and "Valider" → the MNT
hillshade renders in tab 2 then waits for a run. New `superqt` dep. Tests: 36 passed.

**Open questions.** Day picker (today vs tomorrow) for the window; real per-hour AROME wind
so the window actually changes the wind (not just labels).

---

## Entry 20 — Tab 2 ergonomics: multi-day window, drag/scroll map, MNT+basemap  (2026-06-21)

**What changed.**
- The flight-window range slider now spans **0–72 h** (today → day-after-tomorrow, ≈ the
  AROME horizon) and its label shows the **date** ("mar. 23/06 09h → 15h").
- A prominent **"Valider le créneau horaire"** button under the slider launches the per-hour
  screening directly (`on_run_hourly`).
- The result map is navigated by **drag (pan) + scroll (zoom)**, **double-click resets** the
  view, and a **plain left-click analyses** a hotspot (Pass-2). The matplotlib nav toolbar is
  removed — drag/scroll replaces the "manipulation" buttons; pan vs click is disambiguated by
  a small movement threshold.
- The default **MNT preview now shows the basemap** (IGN plan) with the hillshade overlaid
  (`map2d.hillshade`).
- Decluttered the action row to **Aperçu (géométrie)** + **Criblage spatial**; the single
  "Criblage WindNinja" button was dropped and the créneau button replaces the old
  "Criblage du créneau".

**Why.** A pilot picks a flight window across days and explores the result like a map.

**Result.** Verified headless: slider 0–72 with a dated label, the créneau button, the
drag/scroll/double-click handlers, and the MNT+IGN preview renders. Tests: 36 passed.

**Open questions.** Cap the window at the real AROME horizon once the forecast is wired; the
basemap stays static while panning the matplotlib result (not a live slippy map).

---

## Entry 21 — Real forecast wind (Open-Meteo) + spatial-per-hour criblage  (2026-06-21)

**What changed.** "Valider le créneau horaire" now runs a **spatial (sub-zone) Pass-1 per
hour** driven by the **real forecast**: `wind.profile.window_forecast_provider` samples
Open-Meteo **crest-height** wind per tile centre (memoized per point) for each hour of the
flight window. It **falls back to synthetic** if there's no network/crest data. Removed the
separate "Criblage spatial" button — spatial is now the default. Crest altitude = DEM 80th
percentile; per-hour labels carry the date.

**AROME note.** AROME via Open-Meteo (`arome_france_hd`) does **not** expose crest-height
pressure levels (empty crest series), so the working real source is the **Open-Meteo global
blend**; true AROME crest data needs the **Météo-France GRIB API** (key) — future.
`fetch_arome` / `source="arome"` are kept for that.

**Duration (the user's question).** Measured **~30–40 s per hour** for the 2×2 spatial
sub-zone on a ~30 km AOI — actually a touch faster than a single full-domain run (~40 s),
since the tiles are small coarse crops. So **~3–4 min for a 6 h window**, ~12 min for 24 h;
**instant afterwards** (cached per day+hour).

**Result.** Verified end to end (network): a 2 h window → ~80 s → 2 hourly spatial maps from
"prévision" with dated labels. Tests: 37 passed (added `window_forecast_provider`).

**Open questions.** Cap the window at the real forecast horizon; a per-hour upstream wind for
the Pass-2 BC (the mosaic has no single vel/ang grid, so a click currently uses the window
wind). Dead single-shot handlers (`on_run_mass`, `on_run_subzones`) to prune.

---

## Entry 22 — Cap the flight-window slider at the forecast horizon  (2026-06-21)

**What changed.** The créneau range slider now caps at **now + `FORECAST_HORIZON_H` (48 h)**
clock hours (was a fixed 72), so you can't pick a window beyond the (AROME-class) forecast
horizon. A grey note shows the limit ("Prévision disponible jusqu'à ~ jeu. 25/06 23h (AROME
~48 h)"). The constant is a placeholder for when the real Météo-France AROME GRIB is wired —
then it reads the run's actual last valid hour.

**Result.** Verified headless: at 23 h Paris the slider max = 71 (= 23 + 48) and the limit
label is correct. Tests: 37 passed.

---

## Entry 23 — Temporal-first criblage + per-hour spatial refine; bbox crop; button polish  (2026-06-24)

**What changed.**
- **"Valider le créneau horaire"** now runs a **fast TEMPORAL criblage**: a single-domain
  Pass-1 per hour (forecast wind at the domain centre, 200 m) — **~7–8 s/hour** (6 h ≈ 45 s),
  and it keeps the per-hour `vel`/`ang` grids (so the Pass-2 upstream wind works).
- New **"Affiner spatialement l'heure affichée"** button: runs the spatial sub-zone criblage
  for the **currently shown hour** (~20 s) and **stores it back** into the hourly stack —
  re-shown (tagged "(spatial)") when you scrub back to that hour.
- Removed the now-useless **"Aperçu (géométrie)"** button.
- Green "Valider" buttons now **grey out while running** (explicit `:disabled` QSS — a custom
  stylesheet was masking Qt's disabled look). `btn_refine` is disabled until a criblage runs.
- **Fixed "a new zone shows the old MNT":** terrarium `bounds2img` over-fetches a tile-aligned
  mosaic (we saw 57 km for a 36 km AOI), so nearby selections shared most of the same DEM.
  The mosaic is now **cropped to the exact bbox** (`acquire._crop_to_bbox`) → distinct zones
  give distinct DEMs, and the DEM stays under ~50 km.

**Result.** Verified end to end: temporal 2 h = 15 s (vel grids set), spatial refine of one
hour = 19 s (tagged "(spatial)"); two different bboxes → different DEMs (Champsaur 1053 m vs
Mont-Blanc 1687 m, ~25 km). Tests: 37 passed.

**Open questions.** Prune dead single-shot handlers (`on_compute_pass1`, `on_run_mass`,
`on_run_subzones`). Persist refined hours to disk across sessions (currently in-memory +
WindNinja work-dir cache).

---

## Entry 24 — Finer AOI DEM (~54 m)  (2026-06-24)

**What changed.** `prepare_dem_for_bbox` default `target_res_m` 90 → **50** (terrarium zoom
11, ~54 m; `max_px` 2500 → 3000), and the IHM "Valider la zone" uses 50 m — so the MNT is
**~2× more detailed**. Measured: the geometry indicator stays **~0.3 s** on the finer grid
(528² for a ~28 km zone, vs 1.3 s at 27 m), so no criblage slowdown.

**Result.** Verified: a 28 km zone → 528×529 @ 54 m, clearly more terrain detail in the MNT
hillshade. Tests: 37 passed.

---

## Entry 25 — MNT resolution selector in the IHM  (2026-06-24)

**What changed.** Tab 1 gains a **"Résolution MNT" combo** (Grossier ~110 m / Moyen ~55 m /
Fin ~27 m / Très fin ~14 m → `target_res_m` 90/50/30/15). "Valider la zone" prepares the DEM
at the chosen resolution; the cache filename now includes it, so different resolutions cache
separately. Default **Moyen (~55 m)**. Finer is heavier (the geometry indicator scales with
cells: ~0.3 s at 54 m, ~1.3 s at 27 m); resolution still adapts **down** for very large zones
(the `acquire` `max_px` cap).

**Result.** Verified headless: 4 presets, default Moyen → 50 m, Fin → 30 m. Tests: 37 passed.

---

## Entry 26 — MNT source selector: IGN RGE ALTI over France (ADR-0014)  (2026-06-24)

**What changed.** Added a **"Source MNT"** selector to tab 1 (Auto / IGN France / Monde,
default Auto). New `terrain.acquire.prepare_dem_ign` fetches **IGN RGE ALTI** elevation from
the Géoplateforme **WMS** (BIL float32, key-free, clipped to the bbox) and reprojects to UTM;
`prepare_dem(...)` dispatches IGN-over-France vs terrarium and falls back to terrarium if IGN
fails. `in_france` is a rough cover test. "Valider la zone" reports the used source; the cache
key includes source + resolution.

**Why (the user's question).** Sources differ in real precision: terrarium is ~30 m
worldwide (resampling finer is interpolation), IGN is real 1–5 m over France.

**Result.** Verified on a Champsaur AOI at a 30 m grid: **IGN roughness 13 m vs terrarium
5.5 m** (2.4× more real relief), IGN 2 s vs terrarium 10 s. Source selector headless-checked
(default Auto → "auto"). Tests: 39 passed (added `in_france`, `prepare_dem_ign`).

**Open questions.** Multi-request WMS tiling for huge *fine* IGN zones (current single GetMap
is capped at the WMS max dims). IGN coverage beyond métropole (DOM-TOM layers).

---

## Entry 27 — Clean MNT preview (no basemap contours) + spatial-refine scale selector  (2026-06-24)

**What changed.**
- **"Lines on the IGN MNT" diagnosed**: they were the **IGN plan basemap's contour lines**
  showing through the semi-transparent hillshade (the pure hillshade is smooth) — not an MNT
  defect. The **MNT preview is now a bare hillshade** (no basemap overlay); the basemap
  returns on the **criblage result maps**, where orientation matters.
- Tab 2 gains an **"Échelle d'affinage"** selector (Standard 150 m / Fin 75 m / Très fin 40 m
  / Maximum 25 m) driving the spatial sub-zone refine mesh (`subzone_speed_field` resolution);
  the cache key includes it. Finer = more local detail, slower.

**Result.** Verified: the MNT preview is clean even with "IGN plan" selected; the refine
presets are wired (default 150 m). Tests: 39 passed.

**Open questions.** A "MNT + fond" toggle if the user wants context back on the preview (e.g.
over IGN ortho, which has no contour clash). Per-tile time estimate for the finest refine.

---

## Entry 28 — Adaptive sub-zone count + WindNinja mesh tied to the MNT (refines ADR-0007)  (2026-06-24)

**What changed.** The spatial refine was a **fixed 2×2** (4 wind zones) at a manually-selected
mesh, regardless of AOI/MNT size. Now:
- **Wind sub-zone count adapts**: `nx,ny = clamp(round(extent_km / FORECAST_CELL_KM=11), 1,
  MAX_SUBZONES=4)`. A small AOI (< one forecast cell) → **1×1** (a single domain — no spurious
  inter-zone blending); ~30 km → 3×3; large → 4×4 (capped). Based on the Open-Meteo crest
  wind's **~11 km** effective resolution, **not** AROME's 1.3 km (which we don't have, and
  which would mean hundreds of WindNinja runs). Intra-tile detail comes from WindNinja
  downscaling on the terrain.
- **WindNinja mesh tied to the MNT resolution**: `max(25 m floor, MNT res, tile/600 px)`,
  replacing the manual "Échelle d'affinage" selector (a mesh finer than the DEM is moot). A
  grey label shows the auto grid ("3×3 zones · maille ~54 m").

**Result.** Verified: 10 km@54 m → 1×1 / 54 m; 30 km@54 m → 3×3 / 54 m; 50 km@110 m → 4×4 /
110 m; 30 km@14 m → 3×3 / 25 m. Tests: 39 passed.

---

## Entry 29 — Fix IGN "stair-step" striping (native 5 m fetch + average) + restore MNT basemap  (2026-06-24)

**What changed.** The "steps/lines" on the IGN MNT — also visible on the WindNinja outputs —
were a **real DEM artifact**, not the basemap (my earlier diagnosis was wrong). The
Géoplateforme elevation WMS returns **vertically-striped** data (~21% duplicated rows,
sawtooth row-means) for **any off-native-grid** request; it is clean **only at its ~5 m
native grid** (measured: vert frac==0 0.044 at 5 m vs ~0.20 at 7/14/28 m). Fix:
`prepare_dem_ign` now fetches at ~5 m native (**tiled**, ≤ `tile_cap` per axis) and
**block-averages** down to the target resolution. Restored the **basemap under the MNT
preview** (the lines were never the basemap).

**Result.** Verified: a ~13 km IGN zone at 50 m → **vertical striping frac 0.21 → 0.00**,
vert/horz mean ratio 2.2 → 1.3 (isotropic), clean hillshade, ~19 s (native-5 m tiled fetch +
average). Tests: 39 passed.

**Open questions.** Heavier IGN fetch for big/fine zones (tiled 5 m); a fetch-size guard /
progress refinement.

---

## Entry 30 — Pass-2 progress (no frozen 99%) + 3D: basemap drape & rotor by height/intensity  (2026-06-24)

**What changed.**
- **Pass-2 "stuck at 99%"**: the long post-solver phase (mass-mesh sampling/output, ~1 min)
  prints no "%", so the bar froze. `flow.windninja._run` now also surfaces **phase lines**
  (meshing, solving, sampling, generating, writing, …) with the last %, and the IHM switches
  the progress bar to **"busy" (pulsing) at ≥99 %** — clearly working, not frozen.
- **3D rendering (`volume3d`)**:
  - the terrain is **draped with the basemap** (IGN/OSM/…) via a planar texture (needs the
    CRS, passed from the IHM) instead of the elevation colormap (fallback when no CRS).
  - the **rotor** (reversed-flow volume) is coloured by **height above ground** (yellow near
    the ground → red → purple high) with **opacity ∝ intensity** (|reversed along-flow|), via
    a per-cell RGBA array; height-AGL from a KD-tree lookup on the terrain surface.

**Result.** Verified: an off-screen 3D render shows the basemap-draped terrain + the
height/intensity-coded rotor; the Pass-2 phase lines now emit. Tests: 39 passed (adjusted the
streaming-progress assertion for the new phase emission).

**Open questions.** The texture is a top-down planar drape (web-mercator ≈ UTM over a few km);
"intensity" uses reversed-flow magnitude (turbulence intensity is an alternative).

---

## Entry 42 — WindNinja error-box review: real `num_threads`, temp isolation, clearer failures  (2026-06-25)

**What changed.**
- **Root cause confirmed against the installed binary:** WindNinja 3.12 exposes
  `--num_threads`, not `--number_of_threads`. The wrapper and tests now use the real flag.
  This directly fixes spatial refine/sub-zone error boxes caused by "unknown option
  number_of_threads".
- **Momentum temp environment:** `load_config()` no longer mutates global `TMP`/`TEMP`/`TMPDIR`.
  `_subprocess_env(tmp_dir=None)` restores the system temp-related variables captured at import
  time, so Pass-2/OpenFOAM keeps its normal temp environment. Concurrent mass sub-zones still
  opt into isolated per-run temp dirs (`<tile workdir>/_wn_tmp`) plus isolated PROJ cache.
- **Stale output protection:** Pass-1 speed/direction grid discovery now prefers the newest
  WindNinja ASCII raster, and IHM workdirs include the active DEM stem so different AOIs no
  longer share the same hourly/refine/sub-zone cache.
- **Diagnostics:** WindNinja failures now include rc, cwd, command, stderr tail, and stdout tail
  via `format_run_failure(...)`; the IHM Pass-2 error box no longer drops stderr.
- **DEM fallback:** explicit `source="ign"` and user cancellation are no longer swallowed by the
  automatic IGN -> world fallback.

**Result.** Tests: `.\.venv\Scripts\python.exe -m pytest -q` -> **58 passed**.
`WindNinja_cli --help` shows `--num_threads` and not `--number_of_threads`. Probes to keep in
mind: `_subprocess_env()` with no `tmp_dir` must not leak project TMP markers, while
`_subprocess_env(tmp)` must set TMP/TEMP/TMPDIR/CPL_TMPDIR and `PROJ_USER_WRITABLE_DIRECTORY`
to that run directory.

---

## Entry 41 — Pass-2: kill the rotor "climbing the map edge" — buffered solve + clip to the drawn zone (ADR-0021)  (2026-06-25)

**What changed.** A lee/rotor reaching a **lateral domain boundary** (the downwind edge — east
for a west wind, north for a south wind) is deflected up by the outlet BC and "climbs the map
edge" (a BC artifact; I first mis-read it as an altitude/lid issue). Two-part fix:
- **Buffered solve (ADR-0021):** momentum runs on the drawn rectangle **grown by
  `PASS2_EDGE_BUFFER_M` = 700 m** (crop + IGN 5 m re-fetch use the buffered window), so the
  boundaries sit away from the feature.
- **Clip back to the drawn zone:** `volume3d._clip_domain_boundary(rev, mesh, aoi_bounds=…)`
  keeps only rotor cells **inside the drawn zone** (+ trims the top lid); the artifacts live in
  the buffer and are dropped. Without `aoi_bounds` it falls back to a fixed lateral-margin frame
  (all four edges). `aoi_bounds` is threaded through `_launch_pass2_at` → result → `populate_plotter`.

**Result.** Tests: **57 passed** (`_clip_domain_boundary` drops the lateral frame + lid, and
clips to explicit AOI bounds). Same idea as the Pass-1 edge buffer (ADR-0020).

## Entry 40 — Full-zone coverage (edge buffer), 5/10 m de-stripe (1 m native ×5), 3D toggle fixes  (2026-06-25)

**What changed.**
- **Results cover the whole selected zone (ADR-0020):** the prepared DEM is grown by
  `EDGE_BUFFER_M` (1500 m, = the mask) and the 2D view is cropped back to the selection
  (`_aoi_inner_extent`). Cache key gets a `_b1500` marker.
- **Striping still at 5/10 m:** the WMS's true native is **~1 m**, so 5 m requests stair-step and
  need **~×5** averaging to clean (25 m was clean = 5 m fetch ×5; 5/10 m weren't). `prepare_dem_ign`
  now fetches at **`max(1 m, target/5)`** and averages ×5 (5 m⇒1 m fetch, 10 m⇒2 m…). Updates
  the ADR-0014 note.
- **"Vue 3D" checkbox didn't work:** it was gated on a hazard existing (so it did nothing before
  a criblage) and swallowed errors. Now it works with the **bare relief** too (`populate_pass1_3d`
  takes `hazard=None`), **surfaces errors** in a dialog, and **preserves the camera** across
  hour-scrub re-renders (no `view_isometric` inside; `reset_camera=False` on the draped meshes;
  caller restores `camera_position`).

**Result.** Tests: **56 passed**; de-stripe factors verified ×5 at 5/10/25/50 m; terrain-only 3D
builds the expected actors.

## Entry 39 — Optional 3D view of the créneau screening (2D/3D toggle, ADR-0019)  (2026-06-25)

**What changed.** A **"Vue 3D"** checkbox on the créneau tab swaps the matplotlib map for an
embedded 3D viewport (`QStackedWidget` + lazy `pyvistaqt`). `viz.volume3d.populate_pass1_3d`
builds the zone terrain (`_terrain_mesh`), drapes the basemap (reusing the fixed, non-flipped
drape), overlays the hazard as a **translucent inferno texture** (alpha ∝ hazard — transparent
outside danger zones, no StructuredGrid scalar-ordering issues), and adds per-zone wind arrows +
a north arrow. The 2D map stays the default and keeps the Pass-2 rectangle selection + hour
scrub; toggling/scrubbing/basemap re-renders 3D while **preserving the camera**.

**Result.** Verified headless: top-down render places the north-half hazard correctly at the top,
basemap readable, N + wind arrows present; isometric shows the relief draped. Tests: **56 passed**
(+ `populate_pass1_3d` builds the expected actors).

**Open questions.** Still pending: candidate results should cover the **full selected zone** —
the edge-buffer mask shrinks the valid area, so the compute domain needs to be expanded by the
buffer upstream (next).

## Entry 38 — Robustness: refine rc=-1 sequential fallback, Pass-2 crash (momentum env), IGN de-stripe at 5 m  (2026-06-25)

**What changed.**
- **Refine `rc=-1` persisted** after PROJ/TMP isolation. New strategy in `subzone_speed_field`:
  also isolate the **PROJ cache** per run (`PROJ_USER_WRITABLE_DIRECTORY`), and — decisive — a
  tile that fails in the parallel pass is **retried sequentially** at the end (no concurrency →
  rules out contention). Only a tile failing *alone* raises, now with **full stdout+stderr**.
- **Pass-2 crash `rc=3221225477` (0xC0000005 access violation)** at "Writing output files".
  Cause: the temp-dir redirect applied to the **momentum/OpenFOAM** run too (it's env-sensitive)
  and was pointless there (single run). `_run` now takes `tmp_dir` and **only the parallel mass
  runs isolate temp**; momentum keeps its normal env (just `PROJ_NETWORK=OFF`).
- **IGN striping back at 5 m.** At the 5 m preset / Pass-2 5 m re-fetch the block-average factor
  was 1 (no smoothing) and the WMS's own nearest-neighbour downsample-to-target striped.
  `prepare_dem_ign` now **fetches finer than the target** (`min(native, target/2)`) and averages
  **≥2** ourselves → de-striped.

**Result.** Tests: **55 passed**. The momentum-crash fix is a hypothesis (env): if it persists
with "MNT fin 5 m" checked, unchecking it (coarse zone crop) isolates whether the 5 m DEM is the
trigger.

## Entry 37 — Pass-2 fine 5 m DEM + fixes: slider ticks alignment, parallel rc=-1 (temp isolation)  (2026-06-25)

**What changed.**
- **Pass-2 re-fetches its window at IGN 5 m (ADR-0018).** New "MNT fin 5 m (IGN)" checkbox
  (default on): on launch, `_launch_pass2_at` re-fetches just the rectangle at 5 m native
  (`prepare_dem(target_res_m=5.0, source="auto")`, terrarium fallback) via
  `acquire.bbox_latlon_from_utm_window`, runs momentum on it, and drapes the basemap with the
  crop's **own CRS** (returned through the result). Fetch progress folded into 0–25 %.
- **Slider ticks were all collapsed left.** The `_TickRuler` mapped via the slider geometry
  (`mapFrom`), which broke when the slider sat in a row with a label/button. Fix: stack the
  ruler **directly under its slider in a vertical column** so it shares the slider's exact width
  + x-origin; ticks then map to `handle/2 + frac*(width-handle)` in the ruler's own coords.
  Verified: ticks span 8→632 of a 640 px ruler.
- **Parallel refine still failed `rc=-1` (sometimes with HTTP 500, sometimes not).** Root cause
  was a **shared temp dir**, so concurrent WindNinja/GDAL raced on scratch files.
  `_subprocess_env` now also gives each mass tile an **isolated temp dir**
  (`<tile workdir>/_wn_tmp` via TMP/TEMP/TMPDIR/CPL_TMPDIR), on top of `PROJ_NETWORK=OFF` +
  per-tile retry (ADR-0017). Since Entry 42, `load_config()` no longer pins global TMP/TEMP.

**Result.** Tests: **55 passed** (+ bbox round-trip, + temp-isolation assertions). Slider +
fine-checkbox verified headless.

**Open questions.** If `rc=-1` ever persists after temp isolation, the next levers are lowering
the worker count or capturing WindNinja stdout for the real cause (the error box already shows
the stderr tail).

## Entry 36 — Pass-2 3D: fix upside-down basemap, add north + wind arrows, height legend  (2026-06-25)

**What changed (`viz/volume3d`).**
- **Basemap was upside down** ("texte à l'envers"). The drape applied `img[::-1]` then
  `texture_map_to_plane(origin=SW, point_v=NW)`; VTK already maps array row 0 → the north edge,
  so the extra vertical flip inverted it. Removed the flip (`img[:, :, :3]`). Verified with a
  deterministic top-down drape of a labelled test texture (corner colours + an "F" — now NW=top-
  left and the "F" reads upright).
- **North arrow + local-wind arrow** (`_add_compass`): a dark "N" arrow (+Y) and a blue arrow
  pointing where the wind blows TO, labelled `vent <spd> m/s · <dir>°`. Wind speed/direction are
  threaded from the Pass-2 result (`_launch_pass2_at` now returns `bc_spd` too).
- **Height legend**: the rotor is drawn with raw RGBA (colour=height-AGL, opacity=intensity),
  which has no scalar bar, so a tiny invisible proxy carries the `[lo, hi]` range + the yellow→
  red→purple colormap to render a **"Hauteur sol (m)"** scalar bar.

**Result.** Real cached case rendered headless: basemap upright + readable, N + wind arrows, and
the height scalar bar (8 → 229 m). Tests: **54 passed** (+2: `mean_flow_vector` blow-to,
`_add_compass` adds 2 arrows + labels).

## Entry 35 — Fix: tick labels aligned to slider values + parallel-refine HTTP 500 (PROJ network)  (2026-06-25)

**What changed.**
- **Tick labels didn't match the handle.** The evenly-spaced label row ignored the groove
  geometry (the handle margin insets the usable track), so the label under the cursor was off.
  Replaced with **`_TickRuler`** — a painted widget that maps each tick *value* to its handle
  pixel via the slider's own geometry (`PM_SliderLength` + width) and draws the mark there,
  translated into ruler coords (`mapFrom`). Verified: window ticks → x = 4…628, hour ticks
  evenly 4/108/…/628 across a 640 px track. Works for QSlider + superqt QRangeSlider.
- **Spatial refine error box "sub-zone 1 mass failed rc=4294967295 — ERROR 1: HTTP error code
  : 500".** That's PROJ/GDAL fetching datum grids from cdn.proj.org; the parallel sub-zones
  (ADR-0017) hit it concurrently and tripped transient 500s. WindNinja subprocesses now run
  with **`PROJ_NETWORK=OFF`** (`flow.windninja._subprocess_env`, applied to both the blocking
  and streaming paths), and each tile **retries once**.

**Result.** Tests: **52 passed** (+2: PROJ_NETWORK=OFF in the subprocess env; a tile recovers
after one transient failure). Ruler alignment checked headless.

## Entry 34 — IHM batch: detailed basemap, per-zone wind arrows, bigger map + ergonomic sliders, parallel sub-zones (ADR-0017)  (2026-06-24)

**What changed.**
- **Basemap detail**: `map2d.add_basemap` now takes `zoom_adjust` (default **+1**) → contextily
  fetches one tile-zoom finer for a sharper basemap on the Pass-1 crop (and MNT preview).
- **Wind arrows per zone/hour**: `_render_map(..., winds=…)` overlays one arrow per WindNinja
  zone for the displayed hour — direction = where the wind blows TO (meteo FROM), colour by
  speed (turbo 0–20 m/s) + a "X m/s" label. Winds are stored in the hourly stack (temporal: one
  domain wind/hour; spatial refine: the nx×ny per-tile input winds). A **"Flèches vent"**
  checkbox toggles them.
- **Layout (tab 2)**: the result map gets `stretch=1` + an expanding canvas (min 360 px) so it
  **dominates and grows on resize**; the flight-window **range slider shares one compact line
  with "Valider le créneau ▶"**; both sliders are **thicker** (green groove/handle QSS) with a
  **tick-label strip** (≤6 day/hour marks under the window slider, hour marks under the hour
  slider).
- **Parallel sub-zones (ADR-0017)**: `subzone_speed_field` runs the per-tile mass solves on a
  `ThreadPoolExecutor` (~CPU-count workers, each WindNinja run capped to `cpu // workers`
  threads via the new `run_mass(num_threads=…)` / `--num_threads`); progress reported as tiles complete; cancel
  propagates. The refine is now ~cores× faster.

**Result.** Tests: **50 passed** (+4: parallel tiles all solved, a `Barrier` proves true
concurrency, cancel propagates, `--num_threads` flag). Headless smoke checks for the
arrows + tick strips + button placement.

**Open questions.** Tick labels are evenly spaced (approximate alignment to the groove, not
pixel-exact). Parallelism assumes WindNinja mass is light enough that the per-run thread cap
isn't the bottleneck — true for screening meshes.

## Entry 33 — MNT resolution presets = 5/10/25/50 m (native block-average factors) (refines ADR-0014)  (2026-06-24)

**What changed.** IHM MNT presets are now **5 / 10 / 25 / 50 m** (default **25 m**), replacing
the old 90/50/30/15 m. They are exact **block-average factors of the IGN ~5 m native fetch**
(×1/×2/×5/×10) → clean, fast pooling with no resampling artifacts (`_block_average` gets an
integer factor every time).

**Why (answers the question).** Since IGN is always fetched at 5 m native then averaged, scales
that are integer multiples of 5 m make the averaging exact and fast — that's the "moyennes
rapides" the presets should offer. **Worldwide source floor ≈ 30 m** (terrarium = SRTM class;
`zoom_for_resolution` caps at z13 ≈ 13–19 m grid), so 5/10 m on "Monde" only upsample (no real
detail) — the label keeps "~30 m".

**Result.** 46 tests pass; default `25 m` confirmed present in the presets.

## Entry 32 — Météo-France AROME key: stored in .env, validated offline, popup on expiry (ADR-0016)  (2026-06-24)

**What changed.**
- Added support for an AROME apiKey subscribed to `/public/arome/1.0`. Stored **only in `.env`**
  (`METEOFRANCE_API_KEY`, gitignored) — not committed. Optional account hints also stay local
  via `METEOFRANCE_ACCOUNT_LOGIN` / `METEOFRANCE_ACCOUNT_EMAIL`.
- New `wind/meteofrance.py`: `check_arome_key()` decodes the JWT **offline** and returns a
  `KeyStatus` (ok / missing / malformed / expired / not_subscribed / expiring_soon) +
  `renewal_text()`. The key is valid → confirmed (1095 j left).
- IHM: `MainWindow._check_meteofrance_key()` runs at startup (deferred `QTimer.singleShot(0)`
  so headless tests never hit a modal). Missing key = silent (AROME optional); valid = status
  note; **invalid/expired/expiring → popup** with the renewal procedure.
- Docs: **docs/support/meteofrance_arome.md** (model, key location, renewal steps);
  ADR-0016; env var noted in environment.md.

**Why (also answers two questions).** **ICON-D2** = DWD's *convection-permitting* (non-
hydrostatic) ~2.2 km limited-area ICON over central Europe, **keyless** — same class as AROME/
HRRR. **Weather4D** *does* ship AROME 1.3 km, but as a **closed consumer GRIB delivery** (in-
app/subscription), **not an open/programmatic source** — so for Sillage the open routes are the
**Météo-France API** (this key) or **meteo.data.gouv.fr**.

**Result.** Tests: **46 passed** (+7 for the key checker, forged JWTs). Local keys validate
end-to-end through `config` → `check_arome_key`.

**Security note.** Raw key lives only in `.env` (gitignored); the committed doc holds the
**procedure + login account**, not the secret. Signature is not verified (we only read claims
to detect expiry/scope — the gateway enforces real auth).

**Open questions.** GRIB2 ingestion (cfgrib/eccodes) + crest-level selection still to wire
(M4); only then does AROME actually feed the criblage.

## Entry 31 — Pass-2 selection by rectangle; params on the créneau tab; 3D tab display-only (ADR-0015)  (2026-06-24)

**What changed.**
- Pass-2 is no longer a **single click** on the map. A toggle **"▭ Définir la zone Pass-2"**
  on the créneau tab switches the result map into **rectangle-draw mode** (press→drag→release,
  cyan dashed box); it mirrors the Pass-1 AOI gesture. The box persists across hour-scrub /
  basemap re-renders and is cleared when a **new zone DEM** is prepared.
- The **mesh-quality preset** and a green **"▶ Lancer l'analyse Pass-2 (3D)"** button moved
  onto the **créneau tab** — define + parameterize + launch in one place; come back to relaunch
  with other parameters. The launch button is enabled only when a rectangle exists.
- The rectangle sets the momentum crop: centre = its centre, half-width =
  max(½·max(width,height), `PASS2_MIN_HALF_WIDTH_M`). `_launch_pass2_at` now takes `half_m`.
- The **3D (analyse) tab is display-only**: removed the mesh combo, the case field and the
  "Charger un case" button (and `on_load_pass2`); it now just hosts the viewport. `_run_buttons`
  swaps `btn_load_p2` → `btn_pass2`; `_set_running` gates `btn_rect`/`btn_pass2`.

**Why.** The old flow split "where + how" across two tabs and forced a fixed ±2.5 km window.
The rectangle makes the window user-sized and consistent with zone selection (ADR-0012/0015).

**Result.** Tests: **39 passed**. Off-screen smoke test drives press/motion/release → correct
`_pass2_rect`, button gating, tiny-click cancel, and rectangle redraw on re-render.

**Clarification recorded (ADR-0015 Note).** Pass-2 wind = **Open-Meteo ~11 km** (upstream-
sampled Pass-1 field or créneau wind), **not AROME 1.3 km**; Pass-1 sub-zones aren't 1.3 km
because the forecast itself is ~11 km (finer tiles = same value) — intra-tile detail is from
WindNinja downscaling. Real AROME 1.3 km needs the Météo-France GRIB API (key), not wired.

**Open questions.** Reloading a *previous* Pass-2 case without recomputing was dropped "pour le
moment" — re-add a loader if reviewing cached results becomes useful.

---

<!-- TEMPLATE for new entries — copy below the line
## Entry N — <short title>  (YYYY-MM-DD)
**What changed / what I tried.**
**Why.**
**Result / decision.** (link any new ADR)
**Open questions raised.**
-->
