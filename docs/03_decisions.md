# 03 — Architecture Decision Records (ADRs)

Each record: **Context → Decision → Consequences**. Newest decisions may supersede older
ones; supersessions are noted. This file is the "why" companion to `02_architecture.md`.

---

## ADR-0001 — Python as the implementation language

**Status:** accepted

**Context.** The project is dominated by *integration* work: geospatial data, weather
APIs, shelling out to a solver, and 3D visualization. The actual heavy numerics live
inside an external solver, not in our code. The developer is comfortable in programming
and wants to work in VSCode.

**Decision.** Implement in **Python**, src-layout package `sillage`.

**Rationale.** The scientific + geospatial ecosystem (rasterio, pyproj, numpy/scipy,
xarray, PyVista/VTK, requests) is unrivalled for exactly this glue-heavy profile. ~80% of
effort is data plumbing, where Python is strongest. If a custom hot loop ever becomes a
real bottleneck, isolate it later (pybind11/Rust) — but **not** preemptively.

**Consequences.** Fast iteration; trivial access to OpenFOAM reading via PyVista; no
performance wall in sight because the solve happens in compiled WindNinja/OpenFOAM.

---

## ADR-0002 — Wrap WindNinja instead of writing CFD from scratch

**Status:** accepted

**Context.** The goal needs flow over complex terrain. Writing and *calibrating* a CFD
code is a multi-person-year endeavour and a research discipline of its own; doing it
solo would mean endless numerical convergence work and never actually flying with the
tool. WindNinja is a free, open-source, validated wind model **built for wind in complex
terrain**, with a CLI, and with a momentum solver that is itself OpenFOAM CFD under the
hood.

**Decision.** **Wrap WindNinja** (`WindNinja_cli`) via subprocess. Treat it as the flow
engine. Optionally use the Docker packaging (the Katana ecosystem bundles WindNinja +
GDAL + wgrib2 for batch runs over areas/periods) for the hourly loop.

**Consequences.** We inherit a calibrated solver *and* its constraints (next ADRs). Our
job becomes data prep, orchestration, screening logic, and visualization — exactly where
we add value. The "v2 = write real CFD" idea is dropped: the momentum solver *is* the
real CFD. (Supersedes the initial from-scratch-CFD plan.)

---

## ADR-0003 — Two-pass design: mass screening, then momentum detail

**Status:** accepted — **the central architectural decision**

**Context.** Two hard facts collide:
1. The phenomenon we must show — the **rotor / recirculation** — is a *viscous
   separation* effect. **Potential/inviscid flow cannot produce it** (no separation), so
   "start with simple potential flow" was rejected outright.
2. Of WindNinja's two solvers: the fast **mass** solver **cannot capture reversed flow at
   all** (it shows low speed, never reversal) → it cannot show the rotor; the **momentum**
   solver **can**, but is far more expensive and **only accepts a single domain-average
   wind** (no weather-model or point initialization).

So: the cheap solver is blind to the target phenomenon; the solver that sees it cannot be
run over the whole area every hour, and cannot ingest the spatially-varying forecast.

**Decision.** **Two passes.**
- **Pass 1 (mass):** whole domain, **weather-model initialization** (the *only* solver
  that accepts it), one run per hour. Used to compute a **derived hazard indicator** that
  flags **candidates**, not to draw rotors.
- **Pass 2 (momentum):** small sub-domain around a flagged feature, **homogeneous wind**
  read from the Pass-1 field, producing the **true 3D recirculation**.

**Rationale.** Each solver is used exactly where valid. Pass 1 is the only place the
spatially-varying forecast can enter; Pass 2 lives in its natural regime (small domain,
one upstream wind is a sound BC). Adaptive multi-resolution keeps cost feasible.

**Consequences.**
- Pass 1 output is **candidates, not rotors** — must be framed that way everywhere
  (UI + docs), or it is dangerously misleading.
- Need a **handoff**: read crest-height wind from Pass 1 to drive Pass 2; buffer the crop.
- Two different physical quantities → two different visual representations (ADR-0005).

---

## ADR-0004 — Read the OpenFOAM case directly for the 3D field

**Status:** accepted

**Context.** To visualize Pass-2 recirculation in 3D we need the volumetric momentum
field. WindNinja exposes `write_vtk_output`, but **for momentum runs that VTK is the
corresponding *mass-solver mesh*, not the full OpenFOAM field** — i.e. it does *not*
contain the resolved recirculating volume we want.

**Decision.** After a momentum run, **read the OpenFOAM case directory directly** using
PyVista's OpenFOAM reader (`pyvista.OpenFOAMReader` / VTK's `vtkOpenFOAMReader`) to
recover the true 3D field, then build streamlines and threshold volumes.

**Consequences.** Must locate WindNinja's temporary OpenFOAM case directory for the run
(see `05_windninja_integration.md`). Gains the full field for honest 3D visualization;
avoids silently visualizing the wrong (mass) mesh.

---

## ADR-0005 — Keep Pass-1 and Pass-2 as distinct representations

**Status:** accepted

**Context.** Pass 1 yields a *derived likelihood* (and cannot show reversal); Pass 2
yields a *resolved mean field*. They are different quantities with different confidence.

**Decision.** Two separate views: a **2D screening map with a time slider** (Pass 1,
triage) and a **3D detail scene** (Pass 2). Do **not** blend them into one seamless
visual.

**Consequences.** The UI communicates the right epistemic status: triage vs detail.
Clicking a Pass-1 hotspot *launches* a Pass-2 run rather than morphing one view into the
other. Prevents implying a precision Pass 1 does not have.

---

## ADR-0006 — GPU is for rendering, not solving

**Status:** accepted (informational)

**Context.** The target workstation has a strong GPU. OpenFOAM's `simpleFoam` (the
momentum engine) is **CPU-bound** and does not use the GPU.

**Decision.** Budget the **GPU for 3D rendering** (PyVista/VTK: dense streamlines, large
fluid meshes, volumes). Budget **CPU cores** for solver throughput (concurrent hourly
mass runs, momentum iterations).

**Consequences.** Don't expect the GPU to speed up flow computation. Scale solve
performance with cores; scale visual fidelity with the GPU.

---

## ADR-0007 — Pass-1 spatial wind via AROME sampled per sub-zone (interim)

**Status:** accepted — **interim**; a stepping stone toward the full weather-model gridded
initialization of ADR-0003. To be superseded when GRIB/`wxModel` ingestion lands (M4/M5).

**Context.** Pass 1 currently runs with a **single domain-average wind per hour**, so it
cannot distinguish **valley-to-valley** wind differences. AROME (~1.3 km; AROME-HD
~1.5 km) resolves those valley-scale gradients and is the right meteo input. But:
1. WindNinja does **not** natively download AROME — its built-in NWP fetchers are US
   models (GFS/NAM/HRRR/NDFD).
2. Full **weather-model gridded init** (`wxModelInitialization` with an AROME GRIB) needs
   real GRIB plumbing — Météo-France API (key) or the Docker/Katana + `wgrib2` path —
   which is an M4/M5 effort, not a flag.

**Decision.** As an interim, capture spatial wind variation by **partitioning the domain
into sub-zones** and running the **mass solver per sub-zone**, each initialized with its
**own representative domain-average wind** sampled from **AROME via Open-Meteo's AROME
endpoint** (no key) at that sub-zone's **representative crest altitude**. Stitch the
sub-zone surface-wind fields into one Pass-1 map with **overlap buffers + blending** at the
seams.

**On "sub-zones by altitude".** Sub-zones are fundamentally **horizontal tiles** (the
WindNinja domain is a 2-D terrain patch; you do not run it on "only the high pixels").
**Altitude is not a separate partition axis** — it enters as the **per-zone sampling
height**: each tile draws its wind from the AROME *vertical profile* at its own
representative (crest) altitude, so a high massif and a low valley get different winds
*because of* their elevation. **Intra-zone** variation of wind with terrain height is
already handled inside each run by the mass solver itself. So: spatial tiles, each
parameterized by an altitude-appropriate wind — not altitude bands as independent domains.

**Consequences.**
- **Seams**: adjacent sub-zones have different uniform inputs → discontinuities at borders.
  Mitigate with overlap + blending; residual seams are acceptable for a **screening**
  product (this is still *candidates, not rotors* — ADR-0003).
- Cost ≈ *N* sub-zone runs per hour (cacheable per zone/hour/wind).
- Does **not** change the Pass-1 epistemic status: AROME makes candidates *better
  informed*, it does not let the mass solver show rotors.
- Partially resolves the open "AROME ingestion route" question: **Open-Meteo AROME per
  sub-zone for now**; full gridded `wxModel` init remains the eventual target.
- **Adaptive grid (2026-06-24):** the sub-zone count is **not fixed** — it scales with
  `AOI / forecast-cell` (`~11 km` for the Open-Meteo crest wind, **not** AROME's 1.3 km, which
  we don't have and which would mean hundreds of runs), clamped to ≤ 4×4. A small AOI (< a
  forecast cell) collapses to 1×1 (no spurious inter-zone blending). Intra-tile detail comes
  from WindNinja downscaling on the terrain, so the **WindNinja mesh is tied to the MNT
  resolution** (a mesh finer than the DEM is moot), floored for compute.

---

## ADR-0008 — Mesh resolution is a user-facing quality/time knob (Pass 2)

**Status:** accepted

**Context.** Pass-2 momentum cost ∝ **mesh cell count × iterations**, and the engine is
**CPU-bound** (ADR-0006). The **DEM** (IGN 5 m) is *not* the bottleneck — the
**computational mesh** is. A *uniform* 5 m mesh over a ~5 km window would be **millions** of
cells → long runtime + heavy RAM. "Finest possible" must therefore be a deliberate,
bounded choice, not the default.

**Decision.** Expose mesh resolution in the IHM as a **quality preset / target near-surface
resolution**, with a **displayed time + RAM estimate**. **Default = "medium"** (acceptable
runtime, keeps click-to-detail interactive); provide a **"refine"** control to push toward
the finest practical resolution when a zone is in doubt. Refinement targets
**near-surface / near-feature** cells, not uniform domain refinement.

**Consequences.**
- Users trade time for **lee accuracy** explicitly (recirculation regions converge slowest;
  more cells/iterations help most there — docs/05).
- Need sensible **bounds + a cost estimator** (cells → ~minutes) so "refine to max" cannot
  silently launch an hours-long solve.
- "5 m" is an **effective near-surface resolution** set via `mesh_count`, not the DEM step.

---

## ADR-0009 — IHM is a PySide6 desktop app embedding matplotlib (2D) + pyvistaqt (3D)

**Status:** accepted — **resolves the ADR-0006 "UI toolkit" open question**.

**Context.** V0 backend is done (Pass-1 hourly screening + Pass-2 3D recirculation, both
native on Windows). The "real software with GUI" phase needs a UI framework. The compute
(WindNinja mass + OpenFOAM momentum) is **heavy and local**; the existing stack is
**matplotlib** (2D map) + **PyVista/VTK** (3D). The core workflow is interactive: browse a
time-sliderable 2D screening map → click a hotspot → launch a local momentum solve →
inspect the resolved 3D rotor.

**Decision.** Build a **native desktop app in PySide6** (Qt for Python), embedding:
- the **2D screening map** via matplotlib's Qt canvas (`FigureCanvasQTAgg`),
- the **3D detail scene** via **pyvistaqt**'s `QtInteractor` (interactive VTK viewport),
in one window with a controls panel. Long solves run **off the UI thread** (QThread/worker;
cost governed by the ADR-0008 mesh knob). Package under `src/sillage/app/`, launched by
`scripts/sillage_gui.py` (and a `gui` optional-dependency extra).

**Rationale.** First-class Qt embedding for *both* libraries we already use; handles heavy
local compute and the click-to-detail loop natively; no server/browser indirection. A
web/mobile **consultation** surface (deck.gl/Cesium) stays possible **later** as a layer on
top of the Python core (roadmap *Later/research*), not now.

**Consequences.**
- New deps: **PySide6**, **pyvistaqt** (+ qtpy). Isolated in a `gui` extra so headless/CI
  installs stay lean.
- Need a **job/worker model** for non-blocking solves (progress + cancel).
- 3D rendering needs an **OpenGL context** — fine on the workstation; headless CI can only
  test the non-GL parts.
- Keep Pass-1 (2D triage) and Pass-2 (3D detail) as **distinct panels** (ADR-0005) — do not
  blend them into one view.

---

## ADR-0010 — Basemap under the Pass-1 map via contextily (IGN + open tiles)

**Status:** accepted

**Context.** The Pass-1 2D map (hillshade + hazard indicator) had **no geographic reference**
— no place names, roads, or valleys to orient against — which the user needs to read the
screening map. The DEM is in projected UTM.

**Decision.** Add an **optional web-tile basemap** under the Pass-1 map using **contextily**
(reprojects tiles to the axes CRS). Sources: **IGN plan / ortho** via the **key-free
Géoplateforme** (`data.geopf.fr`) as the default, plus **OpenStreetMap** and **OpenTopoMap**
(open, worldwide; topo is handy in the mountains). The hazard indicator is overlaid
**semi-transparent (α≈0.5)** above the basemap; a "Basemap" combo in the IHM selects the
source (or "None" = the original hillshade). `contextily` lives in the `[gui]` extra.

**Consequences.**
- Needs **network** when a basemap is selected; tiles are third-party (attribution applies).
- **Offline / fetch failure → falls back to the hillshade** (handled in the IHM), so the map
  always renders.
- Tile CRS handling is contextily's job; clicks stay in UTM (the axes keep the DEM extent),
  so the Pass-2 click handoff is unaffected.

---

## ADR-0011 — French IHM and Europe/Paris clock times

**Status:** accepted

**Context.** The user is a French paraglider pilot. The app's interface should be in
**French**, and the hourly screening times should read as **absolute local wall-clock hours**
(Europe/Paris) to plan a flight window — not relative "+0h/+1h" offsets.

**Decision.** All **user-facing IHM strings are French** (`app/main_window.py`, the shared
`map2d.DISCLAIMER`, axis/colorbar labels); Qt's built-in strings (dialog Yes/No, toolbar
tooltips) are localized by loading the `qtbase_fr` translator in the launcher. Hourly labels
are **absolute Europe/Paris clock hours** (`zoneinfo` + a `tzdata` dependency, since Windows
lacks the IANA db) — e.g. "mar. 18h". **Developer-facing** code, comments, docstrings, ADRs
and dev log stay **English**.

**Consequences.**
- New dependency: `tzdata` (for `zoneinfo` on Windows).
- Any new UI string must be added in French; mixing is a bug.
- The synthetic hourly series now carries real local-time *labels* (the wind values stay
  synthetic until AROME is wired). Dev scripts may remain partly English (not the product).

---

## ADR-0012 — Interactive selection map via QtWebEngine + Leaflet

**Status:** accepted

**Context.** The app needs a **first tab** with a real interactive **slippy map** (IGN tiles,
smooth drag/scroll pan-zoom, zoom-out to the whole world) centred on Ancelle (~30 km), on
which the user draws a **rectangle** that defines the Pass-1 area of interest (AOI).
matplotlib + contextily is **static** (one fetch per extent, no smooth pan/zoom) and cannot
deliver this.

**Decision.** Embed a **Leaflet** map in a **QWebEngineView** (Qt WebEngine, shipped in
PySide6-Addons). Layers: **IGN plan / ortho** (key-free Géoplateforme WMTS), **OSM**,
**OpenTopoMap**. A **Leaflet.draw** rectangle tool returns the rectangle's lat/lon bounds to
Python over a **QWebChannel** → a Qt signal (`MapTab.aoiSelected`) → stored as
`MainWindow.selected_bbox`. The web view is **skipped under the headless `offscreen`
platform** (Chromium can't render there and crashes at exit). `AA_ShareOpenGLContexts` is set
in the launcher so WebEngine (map) and VTK (3D viewport) OpenGL coexist.

**Consequences.**
- Uses **QtWebEngine** (already in the `gui` extra's PySide6) + Leaflet from a CDN → needs
  **network** (as do the tiles); headless/CI shows a placeholder.
- The AOI bbox is **captured** now; **wiring it to actually prepare a DEM** for an arbitrary
  area (IGN RGE ALTI download for any bbox — today's pipeline is Champsaur-specific) is the
  next step.
- A second map stack (Leaflet) lives alongside the Pass-1 matplotlib basemap (ADR-0010); the
  former is for *navigation/selection*, the latter for *rendering the hazard field*.

---

## ADR-0013 — Worldwide DEM acquisition for the AOI (terrarium tiles); coarse for Pass-1

**Status:** accepted

**Context.** The map tab (ADR-0012) lets the user select **any** AOI worldwide; Pass-1 then
needs a DEM for it. IGN RGE ALTI is **France-only** and heavy (per-département `.7z`). Pass-1
is a **coarse screening** (candidates at ~50-100 m), so it only needs ~90 m terrain — **fine
terrain matters only for Pass-2's small feature window**.

**Decision.** "Valider la zone" prepares a **coarse (~90 m) DEM** for the drawn bbox from the
worldwide, **key-free "terrarium" elevation tiles** (AWS), via `contextily.bounds2img` →
decode RGB→metres → reproject to UTM north-up (`terrain/acquire.py`). The tile **zoom** is
chosen for the target resolution and **capped** so a huge AOI degrades to a coarser DEM
instead of a giant fetch. **No precision control in the UI** — a finer DEM would not improve
Pass-1's coarse output. Fine terrain for **Pass-2** is to be fetched **per feature on demand**
(small crop, high zoom) — a separate step.

**Consequences.**
- Worldwide and keyless; needs network. Terrarium is ~30 m-capable; we target ~90 m for
  Pass-1 (fast, small).
- The DEM acquisition runs on the worker thread with progress; the prepared file feeds all
  Pass-1 actions (`MainWindow._dem_path`).
- The IGN RGE ALTI pipeline (`prepare_champsaur_ign.py`) stays available for high-fidelity
  French work; per-feature fine DEM for Pass-2 is the next acquisition step.

---

## ADR-0014 — DEM source: IGN RGE ALTI over France, terrarium worldwide

**Status:** accepted

**Context.** The worldwide **terrarium** tiles (ADR-0013) are **~30 m real resolution**
(SRTM/Copernicus class) — resampling them finer (the IHM resolution presets) yields a finer
*grid* but **no real detail** beyond ~30 m. **IGN RGE ALTI** is **real 1–5 m** over France.
Measured on a Champsaur AOI at a 30 m grid: **IGN roughness ≈ 13 m vs terrarium ≈ 5.5 m**
(2.4× more real relief), and IGN is **faster** (one WMS BIL request vs many tile fetches).

**Decision.** Prepare the AOI DEM from **IGN RGE ALTI** (Géoplateforme **WMS**, `image/x-bil;
bits=32` float32, key-free, clipped to the bbox) **over France**, and from **terrarium**
elsewhere. A **"Source MNT"** selector offers **Auto** (IGN where covered, terrarium
elsewhere — default) / **IGN France** / **Monde**. `prepare_dem(...)` dispatches and **falls
back to terrarium** if IGN fails or returns no data. The cache key includes the source +
resolution.

**Consequences.**
- Real fine terrain over France → better Pass-1 geometry (ridges/Winstral) *and* Pass-2 crops.
- Worldwide fallback keeps non-France usable; both are key-free.
- IGN WMS max image dims cap the grid; multi-request tiling for huge *fine* zones is a future
  refinement.
- The resolution presets are **honest for IGN**; for terrarium, finer than ~30 m is
  interpolation (the UI labels stay approximate).
- **De-stripe by fetching near native + ×5 average (revised 2026-06-25):** off-grid requests
  **stripe** (the WMS nearest-neighbour downsamples its **~1 m** true native to the target →
  duplicated rows that show as "steps" and propagate to WindNinja). Averaging removes it, but
  only at **~×5**: clean from 25 m (= 5 m fetch ×5), still striped at 5/10 m when the factor was
  <5. So `prepare_dem_ign` fetches at **`max(1 m, target/5)`** (≈native) and **block-averages
  ×5** — 5 m ⇒ 1 m fetch, 10 m ⇒ 2 m, 25 m ⇒ 5 m, 50 m ⇒ 10 m. Heavier fetch at fine targets,
  clean at every preset. *(Supersedes the earlier "5 m native / ×1–×10" note.)*
- IHM presets stay **5 / 10 / 25 / 50 m**. On the **worldwide** source real detail floors at
  **~30 m** (SRTM class; terrarium zoom capped at z13 ≈ 13–19 m grid), so 5/10 m there only
  upsample — the "Monde" label says ~30 m.

---

## ADR-0015 — Pass-2 selection by rectangle, parameters on the créneau tab, 3D tab display-only

**Status:** accepted

**Context.** Pass-2 was launched by a **single left-click** ("hotspot") on the Pass-1 result
map, which cropped a **fixed ±2.5 km** square; the mesh-quality preset and a "load a case"
control lived on the **3D (analyse) tab**. This split the act of *defining* an analysis (where
+ how) across two tabs, made the analysis window non-adjustable, and was inconsistent with the
**rectangle** AOI selection already used for the flight zone (ADR-0012).

**Decision.** The Pass-2 analysis window is now drawn as a **rectangle on the créneau map**
(toggle **"▭ Définir la zone Pass-2"** → drag a rectangle; pan/zoom otherwise), exactly mirroring
the Pass-1 AOI gesture. The **mesh-quality preset (ADR-0008)** and the **"▶ Lancer l'analyse
Pass-2 (3D)"** button move onto the **créneau tab** — so *define + parameterize + launch* happen
in one place, and the user **returns there to relaunch** with other parameters. The rectangle
sets the crop: centre = its centre, half-width = max(½ width, ½ height) (square covering it,
floored at `PASS2_MIN_HALF_WIDTH_M`). The **3D (analyse) tab becomes display-only** — just the
embedded viewport showing the **last** Pass-2 result; the "load a case" control is removed (for
now). The rectangle persists across hour-scrub/basemap re-renders and is cleared when a new zone
DEM is prepared (old UTM coords are meaningless).

**Consequences.**
- One coherent "local analysis" gesture; the window is **user-sized**, not fixed at 5 km.
- `_handle_hotspot` and `on_load_pass2`/`case_edit` are gone; `_launch_pass2_at` takes an
  explicit `half_m`. The Pass-2 **BC wind is unchanged** (`_pass2_wind_at` at the rectangle
  centre — Pass-1 field sampled upstream, else the créneau wind).
- No in-session way to reload a *previous* case without recomputing (acceptable "pour le
  moment"; easy to re-add a loader later).

**Note — Pass-2 wind source (recurring confusion, reaffirmed).** Pass-2's constant upstream
wind is **NOT AROME 1.3 km**. It is the **Open-Meteo crest wind (~11 km effective)**, either
**downscaled by the Pass-1 WindNinja field sampled just upstream** of the feature, or the
créneau wind at domain centre. The "local" character comes from the **position + crest-altitude
sampling** and **WindNinja terrain downscaling**, not from a finer forecast. Likewise Pass-1
**wind sub-zones are not 1.3 km**: the forecast we have is ~11 km, so sub-11 km tiles would
sample the **same** value (zero added spatial information) — the intra-tile detail comes from
WindNinja downscaling on the terrain. The 4×4 sub-zone cap (ADR-0007) is only a *secondary*
compute limit. Real AROME 1.3 km would need the **Météo-France GRIB API (key)** — not wired.

---

## ADR-0016 — AROME (Météo-France API) as the fine forecast source; key validated offline

**Status:** accepted (key + validation now; GRIB ingestion later)

**Context.** Pass-1 wind is Open-Meteo (~11 km effective); finer spatial wind needs a
convection-permitting model. **AROME (1.3 km)** is the natural fit over France but requires a
**Météo-France API key** (free, account-based) — unlike the keyless ~11 km path. The project
expects an **apiKey** subscribed to AROME (`/public/arome/1.0`). The key is a **JWT**: expiry
(`exp`) and subscribed APIs are readable **offline** in its payload.

**Decision.** Adopt AROME as the fine forecast source. **Now:** store the key only in **`.env`**
(gitignored, `METEOFRANCE_API_KEY`, already read by `config.py`) — *not* committed — and add
`wind/meteofrance.check_arome_key()` that validates it **offline** (decode JWT → ok / missing /
malformed / expired / not_subscribed / expiring_soon). The IHM checks it at startup
(`MainWindow._check_meteofrance_key`, deferred via `QTimer.singleShot` so it never blocks
headless tests): a *missing* key is silent (AROME optional, Open-Meteo default), a valid key is
a status-bar note, and an **invalid/expired/expiring** key raises a **popup** carrying the
renewal procedure. **Later:** a GRIB2 provider (cfgrib/eccodes) feeding the criblage
(roadmap M4). The renewal procedure is documented in **docs/support/meteofrance_arome.md**.

**Consequences.**
- The secret stays out of git (only `.env`); optional account hints also stay local via
  `METEOFRANCE_ACCOUNT_LOGIN` / `METEOFRANCE_ACCOUNT_EMAIL`.
- Offline validation = no network at startup, deterministic, unit-testable (forged JWTs).
- The popup + doc make renewal self-service without committing account details.
- A *signature* is not verified (we only read claims) — acceptable: the gateway enforces auth;
  we only need to detect "your key won't work / is about to stop".

---

## ADR-0017 — Sub-zone Pass-1 solves run in parallel (thread pool, CPU-capped per run)

**Status:** accepted

**Context.** The spatial refine (ADR-0007) runs one WindNinja **mass** solve per sub-zone,
nx×ny of them (up to 4×4). It did so **sequentially**, so a 3×3 / 4×4 refine felt very slow
even though each tile is a few seconds. The tiles are **independent** (own crop, own wind, own
work dir), and WindNinja is an **external subprocess** (Python's GIL is released while it runs).

**Decision.** `screening.subzones.subzone_speed_field` now runs the per-tile solves on a
**`ThreadPoolExecutor`**. `max_workers` defaults to ~`os.cpu_count()` (capped by tile count);
each WindNinja run is limited to **`cpu // workers`** threads (new `run_mass(num_threads=…)` →
`--num_threads`, as exposed by WindNinja 3.12) so parallel tiles **don't oversubscribe** the CPU. Progress is reported
from the pooling thread as tiles complete (kept single-threaded); `cancel` both stops the loop
and terminates in-flight runs (each `run_mass` polls `cancel`). The mosaic/blending step is
unchanged.

**Consequences.**
- ~`workers`× faster refine (≈ the number of cores, minus the per-run thread split); a 4×4 on
  an 8-core box drops from ~16 sequential solves to ~4 waves.
- WindNinja mass is light, so even with the per-run thread cap the parallelism dominates.
- Thread-safety holds because tiles touch disjoint files and only read the shared DEM array.
- The momentum Pass-2 is **not** parallelized here (single feature, one solve); this is Pass-1
  screening only.
- **Concurrent runs need isolation (2026-06-25):** parallel tiles failed intermittently with
  `rc=-1` (4294967295), sometimes with "ERROR 1: HTTP error code : 500", sometimes not. Two
  fixes in `flow.windninja._subprocess_env`: (1) **`PROJ_NETWORK=OFF`** stops PROJ/GDAL fetching
  datum grids from cdn.proj.org (the 500s); (2) the **real** root cause was a **shared temp dir**
  — concurrent WindNinja/GDAL processes raced on same-named scratch files. Each mass tile now
  gets an **isolated temp dir** (`<tile workdir>/_wn_tmp` via `TMP`/`TEMP`/`TMPDIR`/`CPL_TMPDIR`).
  Momentum/OpenFOAM keeps the normal system temp environment because project temp redirection can
  trigger access violations on this Windows build. Each tile also **retries once**.
- **Generalized (2026-06-25):** the same policy now drives **hourly Pass-1** too (independent
  hours run concurrently — `pass1.hourly_indicator_stack`). Both loops share one planner,
  **`pass1.parallel_run_plan(count, max_workers, hard_cap=4)`** (the 4-worker cap is the stability
  knob), and both do **parallel-then-sequential-retry** on failures. Separately, the IGN DEM
  **tile fetches** run concurrently (`acquire._fetch_ign_tiles`) — pure network I/O, the main win
  for fine 5 m fetches that pull many ~1 m-native tiles.

---

## ADR-0018 — Pass-2 re-fetches its window at IGN 5 m (per-feature fine DEM)

**Status:** accepted (default on; toggle in the UI)

**Context.** Pass-2's 3D terrain was a **crop of the prepared zone MNT**, so its detail equalled
the zone resolution the user picked (5/10/25/50 m — ADR-0014). At the default 25 m the 3D was
25 m, never the IGN 5 m the source can give. Re-fetching is cheap because the Pass-2 window is
**small** (a few km).

**Decision.** On Pass-2 launch, by default **re-fetch just the rectangle at IGN 5 m native**
(`terrain.acquire.prepare_dem(..., target_res_m=5.0, source="auto")`; terrarium fallback outside
France) and run the momentum solve on that, instead of cropping the zone MNT. The window's
lat/lon comes from `bbox_latlon_from_utm_window(dem.crs, cx, cy, half_m)`. A **"MNT fin 5 m
(IGN)"** checkbox (default on) lets the user keep the old crop (faster / offline). The crop's
**own CRS** is read back and passed to the 3D drape (so the basemap aligns even if the fine
fetch lands in a different UTM zone). Fetch progress is folded into the first 25 % of the bar.

**Consequences.**
- Best-available 3D terrain regardless of the zone resolution; honest IGN 5 m over France.
- One extra network fetch per Pass-2 (small area, fast); the checkbox covers offline/speed.
- The **momentum mesh** (ADR-0008) still caps the *effective* 3D resolution — a fine DEM helps
  the STL/terrain detail but the solved field is as fine as the mesh allows.

---

## ADR-0019 — Optional 3D view of the Pass-1 screening (2D map stays for selection)

**Status:** accepted

**Context.** The créneau tab shows Pass-1 screening on a flat matplotlib map. A 3D drape on the
real relief reads much better for a pilot, but that **2D map is also the surface for the Pass-2
rectangle selection** (ADR-0015) and the hour scrubbing — so a pure 3D replacement would break a
core interaction.

**Decision.** Add a **"Vue 3D" toggle** (a `QStackedWidget`: the matplotlib map on page 0, a lazy
`pyvistaqt` viewport on page 1). 2D stays the default and the place to draw the Pass-2 rectangle;
3D is a **view** of the displayed hour — the zone terrain draped with the basemap + the hazard
field as a translucent inferno overlay (`viz.volume3d.populate_pass1_3d`, reusing the corrected
basemap drape), with per-zone wind arrows and a north arrow. Scrubbing hours / changing the
basemap re-renders 3D while **keeping the camera**. The 3D viewport is created on first toggle
(no GL cost otherwise).

**Consequences.**
- One screening, two reads; selection and scrubbing keep working in 2D.
- Reuses the Pass-2 3D building blocks (drape, arrows) — the basemap-orientation fix and the
  texture approach carry over (no StructuredGrid scalar-ordering pitfalls).
- A second embedded `QtInteractor` (créneau + analyse tabs); acceptable GL cost, both lazy.

---

## ADR-0020 — Grow the prepared DEM by the edge-mask buffer so results cover the whole zone

**Status:** accepted

**Context.** The screening masks a ~1500 m border (`mask_edge_buffer`) to drop WindNinja
crop-edge artifacts, so the valid hazard ended up **noticeably smaller than the drawn zone** —
candidate results were missing near the edges the user selected.

**Decision.** "Valider la zone" now **grows the fetched bbox by `EDGE_BUFFER_M` (= the mask
width, 1500 m) on every side**, runs the criblage on the grown DEM, and the masking then trims
exactly that buffer — so the **valid area == the drawn zone**. The 2D view is **cropped back to
the selection** (`_aoi_inner_extent`, the DEM bounds minus the buffer) so the masked margin isn't
shown; "reset view" and the wind-arrow sizing use that inner extent. The cache key carries a
`_b1500` marker (old un-buffered DEMs aren't reused).

**Consequences.**
- Candidate results cover the full selection; the buffer is computed but hidden.
- Slightly larger fetch + compute per zone (~1.5 km margin); fine for typical flying areas, and
  a very small zone (margin would consume it) keeps the full view.
- Pass-2 crops near the zone edge get real terrain context from the buffer instead of nodata.

---

## ADR-0021 — Pass-2 solved on a buffered domain; rotor clipped back to the drawn zone

**Status:** accepted

**Context.** A lee/rotor reaching a **lateral domain boundary** (the downwind edge — east for a
west wind, north for a south wind, …) gets deflected **up** by the outlet BC, so the rotor seems
to "climb the map edge" (a BC artifact). Clipping that frame in the viz alone would eat into the
**drawn zone** if the real lee reached the edge — same problem the Pass-1 edge buffer solved
(ADR-0020).

**Decision.** The momentum solve runs on the drawn rectangle **grown by `PASS2_EDGE_BUFFER_M`
(700 m)** so the lateral boundaries sit away from the feature; the crop (or the IGN 5 m re-fetch)
uses the buffered window. The 3D rotor is then **clipped back to the drawn zone**
(`aoi_bounds` → `volume3d._clip_domain_boundary`), so the boundary artifacts (which live in the
buffer) are dropped and the displayed result == the selected zone. `_clip_domain_boundary` also
trims the top lid; without `aoi_bounds` (standalone/loaded scenes) it falls back to a fixed
lateral-margin frame.

**Consequences.**
- The rotor fills the drawn zone, free of the downwind-edge "climbing" artifact, at any wind
  direction (all four lateral edges handled).
- Slightly larger momentum domain (+700 m/side) → a bit more compute (and the same `mesh_count`
  spread over more area); fine for the small Pass-2 windows.
- The terrain still shows the buffered context; only the rotor is clipped to the zone.
- A genuinely taller/wider need (very large lee) would still want a larger drawn zone.

---

## ADR-0017b — Parallelize independent hourly Pass-1 mass solves, with timing breadcrumbs

**Status:** accepted

**Context.** The temporal criblage runs one WindNinja **mass** solve per hour. Those hours are
independent (same DEM, different domain-average wind, distinct work dirs), so running them
sequentially makes a 6 h window feel unnecessarily long. Sub-zone refine is already parallel
(ADR-0017); hourly Pass-1 can use the same idea, with its own stability cap.

**Decision.** Add `screening.pass1.hourly_indicator_stack(...)`: it runs independent hours on a
`ThreadPoolExecutor`, preserves the original time order in the returned stack, and caps each
WindNinja process with `run_mass(num_threads=...)` / `--num_threads`. The default plan is
conservative: at most **4 concurrent hourly runs**, with at most **4 WindNinja threads per run**.
Each hour keeps its own work dir and isolated temp dir (`<hour workdir>/_tmp`) through
`hourly_indicator(...)`.

The IHM `on_run_hourly` and `scripts/champsaur_pass1_hourly.py` now use this shared helper.
The script exposes `--workers` for manual benchmarking. A small `timing.RunTimings` helper
records coarse phase durations (DEM, wind preparation, Pass-1, per-hour timings) so future
optimizations are guided by real wall-clock evidence.

**Consequences.**
- Best first acceleration: 4 h/6 h windows should finish in waves instead of one long queue,
  bounded by CPU, disk and WindNinja startup overhead.
- Pass-2 momentum is **not** tiled/parallelized inside one solve because boundary conditions can
  create artifacts; only independent Pass-2 jobs should be parallelized later.
- The next worthwhile speedups are cache-oriented: persisted hazard stacks, forecast JSON/GRIB
  cache, and optional Pass-2 case reuse.

---

## ADR-0022 — Automatic full-resolution pipeline as an additive package (`sillage.auto`)

**Status:** accepted (active auto route/feature pipeline; tuning ongoing)

**Context.** The manual app picks ONE feature per Pass-2. We also want a **"one-click" mode**:
zone + window → solve the *whole* zone at the finest topo scale, automatically, then browse a
time-sliderable global 3D wake. This is a different orchestration (and UI), but it needs every
existing lower layer (DEM, partition, momentum, wind, 3D).

**Decision.** Add a **separate package `sillage.auto`** rather than fork the app — the manual
two-pass tool stays untouched. The auto pipeline:
- **Screens** the zone/corridor once with Pass-1, then places one bounded momentum domain per
  high-hazard feature (`auto.partition.feature_domains`, ADR-0023). The older relief-adaptive
  quadtree helper remains available but is not the active auto strategy.
- **Solves** Pass-2 momentum over each **(feature × hour)** on a buffered crop (`run_auto`),
  reusing `flow.windninja.run_momentum` and the parallel-then-sequential-retry pattern. Momentum
  is CPU-bound (ADR-0006) and its temp env can't be redirected (Entry 38). The default requested
  concurrency is **all detected cores** (`momentum_workers=detect_cores()`), then the effective
  workers are capped by the number of available `(feature × hour)` tasks; each solve receives
  `cores // effective_workers` threads.
- **Aggregates** each hour's cases into one scene (`auto.scene`), reusing `viz.volume3d` and
  clipping each rotor to its zone (ADR-0021); a `ProgressTracker` gives percent + **ETA**.

**Consequences.**
- Maximal reuse, zero risk to the existing app; the two share all physics/IO/viz code.
- **AROME wind wired (2026-06-25):** `auto.wind` reads **AROME France HD (1.5 km)** height-AGL
  wind from **Open-Meteo's `arome_france_hd`** model (keyless JSON), taking the highest available
  height (~120 m AGL) per hour, per feature centre → real valley-scale spatial variation. This is
  *finer* than the Météo-France GRIB API (which exposes only 2.5 km wind at 10–100 m, GRIB-only —
  it would need an `eccodes` dependency), so the GRIB path is deferred. The `.env` AROME key
  (ADR-0016) still gates/labels the run + drives the slider's available window (`auto.arome`).
  Per-point fallback to the Open-Meteo crest blend if HD is unavailable.
- Runs are long (`features × hours` solves); the ETA + caching manage that. The UI (2-tab route →
  run → time-slider 3D) is wired; current work is tuning, robustness and live validation.

---

## ADR-0023 — Auto Pass-2 domains placed on FEATURES, not a grid (seam continuity)

**Status:** accepted (supersedes the grid-quadtree decomposition of ADR-0022)

**Context.** The auto pipeline first tiled the zone into a relief-adaptive **grid** of momentum
sub-domains (ADR-0022). In testing this produced **bad results at every internal seam**: each
sub-zone is an *independent* RANS solve, so the field near a tile's downwind/lateral boundary is
set by that tile's outlet BC (mass conservation deflects the flow **up** — "le flux remonte"),
**not** by the neighbour. Clipping to the tile interior removes the artifact strip but **cuts any
rotor that spans a seam**, and no post-hoc stitching makes independent solves continuous. True
continuity would need solver **coupling** (halo exchange / Schwarz iteration), which WindNinja's
standalone CLI doesn't expose.

**Decision.** **Don't tile.** Screen the whole zone once with the continuous Pass-1 **mass** solver
→ a hazard map → **`find_candidates`** → place **one momentum domain per feature**
(`auto.partition.feature_domains`), each centred on a candidate and **half-sized to ~`lee_factor ×
local relief / 2`** (clamped) so it contains that feature's full lee in any wind direction. Features
are **spatially separated** (min-separation), so the domains don't pave the plane — **no internal
seams to reconcile**, no cut rotors. Flat areas between features get no rotor (physically correct).
Each (feature × hour) runs momentum with the local AROME wind; the 3D scene overlays the separated
rotors, each clipped to its own domain (ADR-0021).

**Consequences.**
- Physically sound: each rotor is resolved in one valid domain; no seam discontinuities.
- Reuses the original two-pass design (ADR-0003), automated: Pass-1 screening drives Pass-2
  placement. `partition_zone` (the grid) is kept but no longer used by `run_auto`.
- Coverage = the significant features, not a blanket grid — matches where rotors actually form.
- Cost scales with the **number of features × hours**, not the zone area; domain size tracks the
  feature's relief (the resolution-vs-coverage trade-off lives in `mesh_count`).

---

## ADR-0024 — Auto mode selects a flight ROUTE (corridor), not a rectangle

**Status:** accepted

**Context.** A paraglider flies a **route**, not a rectangle — a bbox AOI wastes most of its area
(and compute) on terrain that's never overflown. The auto mode should focus on the planned track.

**Decision.** The auto app's map is in **route mode** (`MapTab(mode="route")`): **left-click adds a
waypoint, right-click removes the last**; the route is emitted to Python **on every change** (so
**« Valider » uses the current route** — no double-click required) and a **corridor of the current
margin is drawn live** (turf.js buffer, updated via `MapTab.set_margin_km`).
`run_auto` takes `route_latlon` + `corridor_margin_km`: it fetches/screens the **route bbox + margin**
(`bbox_from_route`), then **restricts feature detection to a corridor** of that margin around the
polyline (`partition.corridor_mask` × the hazard) before placing the per-feature Pass-2 domains
(ADR-0023). So Pass-1 covers the corridor and the expensive Pass-2 runs **only on the candidate
reliefs along the route**. The **manual app keeps the rectangle** (`MapTab` default) — `MapTab`
supports both modes; the polyline JS is injected via a token (no `.format` brace escaping).

**Consequences.**
- Compute focused on the flown corridor, not a blanket bbox; a margin spinbox tunes the band.
- One shared `MapTab` serves both apps (rectangle for manual, route for auto).
- The DEM is still a rectangle (WindNinja needs one) = the route bbox + margin; only candidate
  detection is corridor-masked, so the cheap Pass-1 covers it but Pass-2 stays on-route.

---

## ADR-0025 — Auto momentum artifacts are session-scoped and cleaned on close (disk)

**Status.** Accepted (2026-06-26).

**Context.** Each momentum solve writes a full OpenFOAM case (`NINJAFOAM_*`, ~100–400 MB). The
auto pipeline runs one per (feature × hour) and **never deleted them**, so the compute cache grew
across runs until it filled the disk (observed: 107 cases ≈ 23 GB, machine down to 7 GB free).

**Decision.** Treat auto outputs as **temporary session artifacts**. During a normal UI run, keep the
full OpenFOAM cases, run dirs and crop DEMs so the 3D view can render/debug directly and no extra
VTK extraction step can destabilize the run. On **window close**, `cleanup_auto_artifacts(...)`
deletes `NINJAFOAM_*`, `z*_run`, `z*.tif` and `z*.vtu` under `<cache>/auto`; the same cleanup runs
at the start of the next auto run. The `z*.vtu` glob covers compact per-metric lee volumes and
re-analysable `*_source.vtu` files produced by optional compaction. Reusable DEMs (`dem_*.tif`) and
the keyed Pass-1 `screening/` cache are kept.

The previous compact-to-`.vtu` path remains available as `compact_cases_during_run=True` for a future
low-disk mode, but it is **not the default**. The disk guard remains only as a last-resort protection
if the volume is already dangerously low. `locate_openfoam_case` still matches cases by crop DEM
**stem** so parallel solves don't grab each other's case.

**Consequences.** The program no longer accumulates stale auto cases across sessions, while live runs
stay simple and inspectable. A completed result is kept only while the app is open; closing the window
means recompute later if the session artifacts were cleaned.

---

## ADR-0026 — Auto domains tighter + finer mesh + a real outflow buffer (refines ADR-0021/0023)

**Status.** Accepted (2026-06-26).

**Context.** Testing the route mode showed (1) the solve looked like **one coarse rectangle**, not
small high-resolution per-feature domains, and (2) the rotor still **"climbed" the domain edge** as if
a wall (`v=0`) blocked the flow. Root cause for (1): feature domains were sized up to **7 km half**
(`max_half_m=3500`, `lee_factor=6`) and meshed at only **150 k** cells → coarse, near zone-blanketing.
For (2): WindNinja/NinjaFOAM owns the BCs — the lateral/downwind faces are **inlet/outlet**, not `v=0`,
but `inletOutlet` clamps **reverse flow at the boundary** to the free-stream, deflecting recirculation
up along the edge. It is a domain-sizing artifact (the outlet sits inside the wake), not a settable BC.
The `AUTO_EDGE_BUFFER_M=700` margin was too thin and the clip kept cells up to the (large) zone edge.

**Decision.** (a) **Tighter domains**: `lee_factor 6→5`, `min_half_m 1200→1000`, `max_half_m 3500→2500`.
(b) **Finer mesh**: `mesh_count 150 k→300 k` (tighter domains, with session cleanup preventing stale
accumulation; ADR-0025). (c) **Bigger
outflow buffer**: `AUTO_EDGE_BUFFER_M 700→1200` so the boundary sits well off the lee. (d) **Clip always
drops a boundary band**: `_clip_domain_boundary` keeps cells inside the drawn zone **AND** off the solver
boundary (tighter of the two), so an edge feature can't show the climb. (e) **Draw each analysed domain**
as an outline on the 3D terrain (`scene._add_domain_box`) so the per-feature sub-rectangles are visible.

**Consequences.** Per-feature solves are smaller + finer (genuine precision) and the boundary artifact
is pushed outside the displayed zone and clipped. More CPU per solve (finer mesh, parallelised across
workers). `mesh_count`/half-sizes stay tunable; finest topo (IGN 5 m per crop) remains a future lever.

---

## ADR-0027 — 3D scenes reproject basemaps to the DEM CRS and place terrain on pixel centres

**Status.** Accepted (2026-06-26).

**Context.** In the 3D views, the web-tile basemap appeared shifted south by a few hundred metres
relative to the reconstructed relief. The 2D map path lets contextily reproject tiles to the axes CRS,
but the 3D path fetches a tile mosaic and turns it into a PyVista texture manually. Web tiles are in
EPSG:3857; stretching that mosaic directly on an UTM terrain plane is only an approximation and becomes
visible at valley scale. A smaller offset also came from placing DEM sample values on the outer raster
bounds instead of on pixel centres.

**Decision.** `viz.volume3d._drape_basemap` must treat the tile mosaic as EPSG:3857, reproject its RGB
bands into the DEM/terrain CRS, then texture-map the already-reprojected raster onto the terrain.
`_terrain_mesh` places DEM vertices at pixel centres. 3D views include a horizontal scale bar so future
alignment checks can be estimated visually.

**Consequences.** The basemap, hazard overlays, rotor volumes and terrain share the same projected
metric frame in 3D. A visual difference between Pass-1 and Pass-2 wakes may still be valid when the
Pass-2 local/hourly wind differs from the representative Pass-1 screening wind; it is no longer evidence
by itself of a basemap offset.

---

## ADR-0028 — Auto progress/ETA is wave-based (parallelism-aware), not tasks×mean

**Status.** Accepted (2026-06-26).

**Context.** The auto run's ETA was ``mean(observed solve time) × remaining_tasks``. With momentum
solves running ``workers`` at a time, that over-estimates by ~the worker count: a 5-solve / 5-worker
run logged "1/5 · 20% · reste ~122 min" then finished ~2 min later — the first completion was treated
as if four more solve-times remained, when the other four were nearly done (one parallel **wave**).

**Decision.** Model waves: ``waves = ceil(total / workers)``. Total wall estimate = ``mean solve ×
waves``; ``eta = max(0, estimate − elapsed)`` anchored to a wall clock (`ProgressTracker.start()`,
injectable clock for tests). The displayed percent is ``elapsed / estimate`` clamped to
``[done/total, 0.99]`` (smooth, but never below the genuinely completed fraction). The pipeline emits
`display_percent`; the window's ETA label ticks the last value **down** between updates.

**Consequences.** ETA/percent now track reality for multi-wave runs. Limitation: WindNinja momentum
emits no in-solve progress, so a single wave (workers ≥ tasks) shows indeterminate until the first
completion — fewer workers gives more feedback (more waves). A future lever is parsing NinjaFOAM phase
lines for coarse in-solve progress. (Right-drag 3D pan shipped in the same pass — see Entry 54.)

---

## ADR-0029 — Optional "blind paving" mode: Pass-2 everywhere along the route (no Pass-1)

**Status.** Accepted (2026-06-26), opt-in alongside the feature-based default (ADR-0023).

**Context.** Hazard-based feature detection under a fine corridor sometimes placed very few domains
("un seul rectangle"), so stretches of the route were never analysed. The pilot prefers, for now,
**guaranteed coverage**: compute Pass-2 *everywhere* along the route at max resolution and judge the
result directly, accepting the cost.

**Decision.** Add `AutoConfig.domain_mode="corridor"` (`partition.corridor_tiles`): **no Pass-1** —
lay a square momentum domain every `tile_step_m` of route arc length, half-size `tile_half_m`
(default = corridor half-width, ≥ 900 m), `step ≤ 2·half` so tiles overlap (no gaps). Each tile is
solved on tile+buffer at `target_res_m` (UI: 1/5/10/25 m, 1 m only meaningful under IGN HIGHRES
coverage) with local AROME-HD wind, clipped to the
tile. UI: a "Pavage aveugle" checkbox + sector step + topo resolution; the CPU plan estimates the
sector count from route length / step. `domain_mode="features"` keeps ADR-0023.

**Consequences.** Full route coverage, trivially parallel (more, smaller solves — good for OpenFOAM
threading). **Accepts the limits we documented:** independent tiles don't match at seams, and a tile
can't be smaller than ~lee+buffer, so rotors straddling a tile may be split. 1/5 m topo over a long
corridor is a heavy IGN fetch → use short routes first. This is a deliberate cost/quality trade for
guaranteed coverage; the feature-based mode remains the physically-cleaner default.

**Overlap rendering.** Overlapping sectors are translucent meshes, so alpha-compositing would *stack*
their opacity and fake a stronger rotor. The 3D scene therefore draws each space point from its
**nearest sector centre only** (a Voronoi clip of the rotor cells in `auto.scene.populate_auto_scene`),
so transparency reads as the true intensity — no double-counting. (True cross-solve averaging of the
fields is heavier and deferred; winner-take-all by nearest centre is the cheap, deterministic fix.)

---

## ADR-0030 — Multi-segment routes + portable result bundles (`.sillage`)

**Status.** Accepted (2026-06-26).

**Context.** (1) A continuous route forced paving across valley crossings/transitions the pilot
won't fly. (2) Auto runs are expensive; the result (the lee zones) should be re-openable without
recomputing, but the full OpenFOAM field is far too big to keep.

**Decision.** (1) The route is a **list of segments** (`AutoConfig.route_segments`); the map adds a
"＋ Segment" button that keeps the current segment and starts a new one. `run_auto` paves/screens
**each segment independently**, so the gaps between segments are never computed. `route_latlon`
stays the flattened route (DEM extent + wind). (2) `auto.store` saves a run as a single
**`.sillage` zip**: `manifest.json` (config, route segments, hours + absolute-date labels, per-case
wind/aoi metadata) + `dem.tif` + either compact per-metric `.vtu` volumes or, in v2
**re-analysable** mode, one `source_XXX.vtu` per case (clipped geometry + derived lee scalars,
not the full OpenFOAM case). `load_result` extracts to a temp dir and rebuilds an `AutoResult`
whose cases point at the bundled source/volumes, so the 3D scene redraws with no solve.

**Consequences.** Disjoint corridors skip transition zones (less compute, the pilot's intent). A
result file is portable; reopening restores the wake, route, per-day hour labels and parameters.
Saved labels are kept so a reopened result shows its **run day**, not today. Compact bundles are
small but volume thresholds are fixed at save-time. Re-analysable bundles are larger (roughly
2.5×–6× on observed files) but can re-threshold metrics after reopening. Both omit the full
OpenFOAM field, so higher mesh still requires recompute.

---

## ADR-0031 — Four lee-field representations; turbulence as absolute rms; per-metric persistence

**Status.** Accepted (2026-06-29).

**Context.** The single rotor (reversed-flow) volume answers "where does the air recirculate", but a
pilot also wants to read **how much the flow slows/reverses** and **where it lifts vs sinks** — and
the turbulence view showed big, "louche" disparities between adjacent sub-domains.

**Decision.** From each momentum case, `viz.volume3d._compute_lee_scalars` derives four cell fields
once: `along_flow` (m/s), `along_pct` (signed % of the upstream wind: −100 reversal → +100 free-stream),
`w_ms` (signed vertical velocity), `turb_rms` = √(2k/3) [m/s]. `_threshold_lee` then yields a volume
per **metric**: *rotor* (reversed flow, 2-D height×intensity colormap), *horizontal* (cells slowed
below a %-floor, diverging RdBu: red = rotor → blue = full wind), *vertical* (|w| ≥ a m/s-floor,
diverging RdYlGn: red = sink → green = lift), *turbulence* (rms ≥ a m/s-floor, 2-D height×rms). A UI
"Représentation" combo switches them in both apps; all share one **absolute** scale across sub-domains.

**Turbulence as absolute rms (m/s), not TI (%).** TI = √(2k/3)/U normalises by *each domain's own*
upstream wind, so equal turbulence read differently per domain. The absolute rms √(2k/3) is
comparable across domains regardless of wind. (Remaining inter-domain differences are then the
*physical* ones — independent RANS solves have genuinely different k at the seams; ADR-0029.)

**Persistence.** `CaseResult.vtu_paths` is a `{metric: .vtu}` dict for compact bundles;
`CaseResult.source_path` points at a threshold-independent `source_XXX.vtu` for re-analysable
bundles. `extract_case_volumes` writes every non-empty volume for compact reopen; `extract_case_source`
writes clipped geometry + `along_flow`, `along_pct`, `w_ms`, `w_abs`, `turb_rms` so a reopened
`.sillage` can rebuild metric volumes at new floors without the OpenFOAM case.

---

## ADR-0032 — Continuous rendering across sectors: feathered weighted-average blend

**Status.** Accepted (2026-06-29), replaces the nearest-sector (winner-take-all) display of ADR-0029.

**Context.** Independent momentum solves have different wind BCs + turbulence per sub-domain, so the
coloured value jumps at the sector boundary. Nearest-sector display made that a hard **diagonal
vertical plane** (the perpendicular bisector between two sector centres). The pilot wants a smooth
transition — a weighted average of the overlapping sectors.

**Decision.** Keep drawing each space point **once** (nearest sector centre → no alpha-stacking), but
colour each drawn cell by a **distance-weighted average of the metric's field across all overlapping
sectors** (`auto.scene.populate_auto_scene`). Per cell at `p`, weight from sector `j` is a feather
`(1 − clamp(‖p−centre_j‖ / half_j, 0, 1))²` (1 at the sector centre, 0 at its edge), counted only
where `p` is actually covered by `j` (nearest-cell distance ≤ a few cell sizes). The blended value is
`Σ w_j v_j / Σ w_j`. Both sides of a boundary compute the same blend, so the seam disappears while
each point is still drawn once. Blended draws are cached per `(zone, hour, metric, floor)`.

**Consequences.** Continuous colour across sectors, no alpha-stacking. From the **audit** of other
inter-zone value sources: (1) **fixed** — the horizontal metric's `along_pct` is now normalised by the
hour's **global** wind (median of the sectors' upstream winds), so the % means the same in every zone
(threshold + colour on the live path; colour on reopened bundles). (2) **known, offered** —
`_clip_domain_boundary` cuts the top `lid_frac` of *each* mesh's height, so the volume top sits at a
different absolute altitude per zone (a horizontal-extent step, not the diagonal seam); a fixed
AGL/absolute lid would remove it. Colour scale (`intensity_max`, `metric_range`), `vol_floor` and
`height_clim` are all shared across sectors, so they are **not** a source of the differences. The
dominant remaining cause is physical: independent RANS solves with per-zone wind BCs.

---

## ADR-0033 — One unified app (`SousLeVentWindow`) subclassing the auto app; two legacy backups

**Status.** Accepted (2026-07-05).

**Context.** Two separate desktop apps had grown in parallel: `app.main_window` (manual: draw one
feature → its precise rotor) and `auto.window` (automatic: draw a route/window → the whole corridor's
wake). Users had to pick the app before knowing which workflow they needed, and every display fix had
to be duplicated. We wanted a single entry point covering all workflows: route **or** rectangle
selection × forecast **or** manual wind × features / blind-corridor / screen-then-pick domains.

**Decision.** Add `sillage.souslevent.window.SousLeVentWindow`, which **subclasses**
`auto.window.AutoWindow` and overrides only the selection/mode/run-launch surface, inheriting the 3D
render loop, caches, wind-restore guards, close handling and opacity/legend machinery unchanged. It
becomes the `souslevent` console entry point; `sillage-gui` (manual) and `sillage-auto` (automatic)
remain as **legacy backups** so nothing is lost. A new `app/qt_image.py` (`set_label_image`) is shared
by all windows.

**Consequences.** One app to learn; the hard-won auto-app fixes (ADR-0025/0028/0030/0032 era:
disk-safe cases, ETA, saved-wind restore, blend) are inherited, not re-implemented. **Debt (tracked,
dev log):** the new app currently *copies* ~150+ lines of UI from `AutoWindow`/`main_window` (select-tab
rows, rubber-band rectangle selector, hazard-map rendering, CPU-plan text, run-launch, `main()`), so a
UI change may need to land in more than one window until shared row-builders/helpers are extracted.
"Three windows, one engine" holds only by that discipline until then.

---

## ADR-0034 — Manual homogeneous wind grid (speed × direction scenarios)

**Status.** Accepted (2026-07-05).

**Context.** Beyond the AROME forecast, a pilot often wants to ask "what does the lee look like for a
*chosen* wind" — e.g. 10/15/20 km/h from SW/W — to reason about a site independently of the day's
forecast, or to explore a classic dangerous configuration.

**Decision.** `AutoConfig` gains `wind_mode` (`forecast` | `manual_grid`) plus
`manual_wind_speeds_kmh` / `manual_wind_dirs_deg`. `pipeline.manual_wind_scenarios(cfg)` expands the
grid to `(case_id, speed_kmh, from_deg)` and `manual_wind_provider(cfg)` returns a homogeneous
`(speed_ms, from_deg)` per scenario (km/h → m/s at that edge, `/3.6`). The scenario index **reuses the
existing `hour` field** on `CaseResult`/results, so the whole solve/aggregate/save/slider machinery is
unchanged — only the label differs (`wind_label_for_case`: "15 km/h · Sud-Ouest" vs "14h"). Speeds are
deduped on a 5 km/h step, directions on a 45°/`%360` step, so the UI's `hours = range(n_speed×n_dir)`
always matches the scenario count. New `wind/directions.py` gives the French octant labels.

**Why reuse `hour`.** It keeps one code path for forecast and manual runs (tasks are still
`(zone, case_id)`), and `.sillage` round-trips for free once `wind_mode` + the two grids are persisted
in the manifest (they are). **Units caveat:** km/h lives in `AutoConfig`/UI (a display-edge unit) and
is converted to m/s inside `manual_wind_provider`; any new consumer of the grid must convert there —
never feed `manual_wind_speeds_kmh` to the solver as m/s (a 3.6× error in a safety tool).

---

## ADR-0035 — Pass-2 mesh-quality preset in the unified app (feature parity)

**Status.** Accepted (2026-07-05). Extends ADR-0008.

**Context.** The unified `SousLeVentWindow` is to become the sole published app (the two old UIs stay
as local backups). The manual app's **Pass-2 mesh quality/time knob** (ADR-0008: Grossier / Moyen /
Fin / Max = `(mesh_count, iterations)` + a rough minutes estimate) was the one Pass-2 control missing
from the auto/unified app, which always solved at a fixed 300 k mesh.

**Decision.** Move `PASS2_MESH_PRESETS` / `PASS2_MESH_DEFAULT` / `pass2_estimate_minutes` into
`auto/window.py` (the non-legacy base module, so the unified app doesn't depend on `app.main_window`)
and add a **"Maillage Pass-2"** combo to the unified select tab. `_build_cfg` sets `AutoConfig.mesh_count`
/`iterations` from it; `_on_run_selected_candidates` re-applies the *current* preset (Pass-1 screening
ignores the mesh); `_restore_controls` maps a reopened `mesh_count` back to the nearest preset; the CPU
plan shows `~min/calcul ⇒ ~min total`. Mesh is persisted already (`store` saves `mesh_count`/`iterations`).

**Consequences.** Full Pass-2 parity with the manual app; a quick coarse preview or a max-refined solve
is one combo away. The estimate is indicative (CPU-bound, ADR-0006).

---

## ADR-0036 — Hourly Pass-1 hazard stack, browsable in the candidates tab

**Status.** Accepted (2026-07-05).

**Context.** The manual app browsed the Pass-1 hazard **hour by hour** (danger zones shift through the
day) before committing to Pass-2. The unified app's screening (`screen_candidates`) collapsed the
window to a single representative-wind hazard, losing that view — the second manual-app feature gap.

**Decision.** `_prepare_domain_plan` gains `hourly_hazard`; when set (only `screen_candidates` passes
it), the feature branch screens **every hour/scenario** via `hourly_indicator_stack` and detects
candidates on the **element-wise-max aggregate** (so candidates cover the whole window, not one hour).
`ScreeningResult`/`_DomainPlan` carry a `hazard_stack` aligned to `hours`. The candidates tab gets an
**hour/scenario slider** (`_draw_candidate_map` renders `hazard_stack[i]`), hidden for a single map.
Both Pass-1 workflows now route through this tab: *Pass-1 seul* (manual pick) and *Pass-1 + candidats
auto* (the top-N are **pre-selected** so it's one extra click to launch Pass-2). `run_auto` keeps its
single-wind feature detection — the extra N mass solves happen only in the review workflow.

**Consequences.** The hourly hazard view is back and available to both Pass-1 modes; candidates are
more robust (aggregate over the window). Cost: the review path runs one Pass-1 mass solve per
hour/scenario (fast, capped at 4 concurrent). The auto-features mode is no longer strictly one-click —
it stops at the candidate review — which is the manual app's philosophy and lets the pilot see the
hazard before spending on Pass-2.

---

## Open questions tracked as future ADRs

- **Stability / diurnal winds on the momentum solver.** Available on the mass solver
  (diurnal slope winds, non-neutral stability); availability on momentum is **to verify**.
  Will become an ADR once confirmed. Affects how much of the physics enrichment lands in
  Pass 1 vs Pass 2.
- **Full weather-model gridded init (AROME `wxModel`)** — the eventual successor to
  ADR-0007's sub-zone interim. Needs the GRIB route decided (Météo-France API vs
  Docker/Katana + `wgrib2`). Tied to the batch-engine question below.
- **Batch engine choice for the hourly loop** — native `WindNinja_cli` subprocess vs the
  Docker/Katana packaging. Decide once Pass-1 volumes/time-ranges are real.
- ~~**2D map / UI toolkit**~~ — **resolved by ADR-0009** (PySide6 desktop embedding
  matplotlib + pyvistaqt). A web/mobile consultation surface stays a *later* layer.
