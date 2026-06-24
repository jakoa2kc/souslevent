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

<!-- TEMPLATE for new entries — copy below the line
## Entry N — <short title>  (YYYY-MM-DD)
**What changed / what I tried.**
**Why.**
**Result / decision.** (link any new ADR)
**Open questions raised.**
-->
