# CLAUDE.md — AI / third-party context entry point

> **Read this file first.** It is the map. It tells you what Sillage is, why every
> major decision was made, and where to look for detail. If you are an AI assistant
> or a developer joining this project, start here, then follow the pointers.

## What this project is, in one paragraph

**Sillage** computes and visualizes the *leeward turbulence / rotor zones* that form
when wind flows over mountain terrain, to help a paraglider pilot decide where it is
safe to fly. Input: a fine terrain model (DEM) of a flying area + wind forecasts by
altitude, hour by hour. Output: (1) a coarse, fast, domain-wide map flagging *where
trouble is likely*, time-sliderable; and (2) on demand, a precise 3D model of the
recirculating airflow at a specific feature and hour (a ridge, a summit, a shoulder).

"Sillage" is French for *wake* — the disturbed air a relief leaves downwind.

## The single most important thing to understand

There are **two passes**, using **two different solvers**, because **no single solver
can do both jobs**. This is not an arbitrary design — it is forced by a hard
constraint in the tooling. If you remember only one thing, remember this:

| | **Pass 1 — screening** | **Pass 2 — detail** |
|---|---|---|
| Solver | WindNinja *conservation of mass* | WindNinja *momentum* (NinjaFOAM / OpenFOAM RANS) |
| Domain | whole flying area | one small feature |
| Wind input | spatially-varying, from weather model, hour by hour | single homogeneous wind |
| **Captures rotors?** | **NO** — physically cannot (see below) | **YES** — resolves recirculation |
| Speed | seconds | seconds-to-minutes per run |
| What it produces | a *derived hazard indicator* (candidate zones) | the true 3D recirculating volume |

**Why Pass 1 cannot show rotors:** the mass-conserving solver does not solve momentum
properly, so it *cannot represent reversed flow at all*. In a lee eddy it just shows
very low wind speed, never a reversal. Therefore **Pass 1 is a detector of candidates,
not a map of rotors.** Treating its output as a rotor map would be physically wrong and
dangerous. See `docs/01_theory_and_physics.md`.

**Why Pass 2 takes only a uniform wind:** the momentum solver does **not** support
weather-model initialization or point initialization — only a single domain-average
wind. That is acceptable because Pass 2 operates on a *small* domain where one upstream
wind is a sound boundary condition. The wind it uses is *read from the Pass-1 field* at
crest height just upstream of the feature. See `docs/05_windninja_integration.md`.

## Where to look

| You want to understand... | Read |
|---|---|
| The vision, scope, non-goals | `docs/00_project_overview.md` |
| The physics & why mass-vs-momentum | `docs/01_theory_and_physics.md` |
| Module structure & dataflow | `docs/02_architecture.md` |
| *Why* each big choice was made | `docs/03_decisions.md` (ADRs) |
| Data sources, formats, gotchas | `docs/04_data_sources.md` |
| Exact WindNinja CLI usage & limits | `docs/05_windninja_integration.md` |
| The chronological reasoning trail | `docs/06_dev_log.md` |
| The one-click automatic pipeline | `docs/10_auto_pipeline.md` |
| What's built / what's next | `docs/07_roadmap.md` |
| Domain vocabulary (paragliding+CFD+meteo) | `docs/08_glossary.md` |
| How to debug / common failures | `docs/support/troubleshooting.md` |
| Install & system requirements | `docs/support/environment.md` |

## Code layout (src-layout)

```
src/sillage/
  config.py            global paths/settings/units (generated artefacts under C:\A2K\SousLeVent)
  terrain/dem.py       load DEM, reproject to UTM north-up, validate for WindNinja
  terrain/geometry.py  slope, aspect, ridge detection, Winstral shelter index
  terrain/acquire.py   fetch the DEM (IGN 1 m de-striped / world) at a target resolution
  wind/forecast.py     fetch wind profiles (Open-Meteo / AROME), hour by hour
  wind/profile.py      crest-height wind blend; per-point providers
  wind/meteofrance.py  validate the AROME API key (offline JWT check)
  flow/windninja.py    subprocess wrapper around WindNinja_cli (mass + momentum), case locator
  flow/openfoam_reader.py  read the OpenFOAM case directly via PyVista (U, k -> 3D field)
  screening/indicator.py   terrain geometry + velocity deficit + empirical rules -> hazard
  screening/pass1.py       hourly Pass-1 stack (parallel), candidate finder
  viz/map2d.py         2D screening map with a time slider
  viz/volume3d.py      3D rendering: basemap drape, rotor 2-D colormap, wind arrows, legends, pan
  auto/                ONE-CLICK automatic pipeline (see docs/10):
    pipeline.py          run_auto: DEM -> (Pass-1 features | blind corridor tiling) -> Pass-2 ×hours
    partition.py         feature_domains (hazard) + corridor_grid_tiles (global-surface paving) +
                         corridor_mask + mesh↔topo resolution helpers (ADR-0037)
    wind.py              local AROME-HD wind per domain + route wind series (arrows)
    arome.py             forecast window (absolute dates) from the AROME/Open-Meteo horizon
    scene.py             aggregate one hour into a 3D scene (extract_volume rotor/turbulence)
    pipeline.py          + manual homogeneous-wind grid (`manual_wind_scenarios`/`_provider`) and
                         `screen_candidates` (screening-only pass → pick candidates → Pass-2)
    store.py             save/open a run as a portable .sillage bundle (lee meshes only)
    progress.py          parallelism-aware (wave) progress + ETA
    window.py            the automatic-mode desktop app (route + window -> 3D wake) — legacy backup
  souslevent/
    window.py            **the primary unified desktop app** (`SousLeVentWindow`, subclasses
                         `auto.window.AutoWindow`): route OR rectangle selection × forecast OR manual
                         wind grid × features/corridor/screen-then-pick. This is the `souslevent`
                         entry point; the two apps below are kept as legacy backups.
  wind/directions.py     French octant labels for a meteorological FROM direction
  app/
    main_window.py       the manual 2-pass desktop app (draw zone, Pass-1 map, Pass-2 3D) — legacy backup
    map_tab.py           Leaflet map tab (rectangle OR multi-segment route) shared by all apps
    jobs.py              background QThread job runner (progress/cancel + shutdown)
    qt_image.py          RGBA numpy buffer -> QLabel pixmap (copy-safe; shared by the apps)
scripts/
  souslevent.py          launch the unified app   sillage_gui.py / sillage_auto.py  legacy backups
  demo_pass1.py          end-to-end Pass-1 walkthrough
```

**One primary app, one engine, two legacy backups.** `souslevent.window.SousLeVentWindow` is the
current app; it **subclasses** `auto.window.AutoWindow` and reuses `viz.volume3d`, `app.map_tab`,
`app.jobs`, `app.qt_image`, `flow/*` and `wind/*`. The older `app.main_window` (manual single-feature)
and `auto.window` (automatic route) still ship as `sillage-gui` / `sillage-auto` and share the same
engine, so **results and 3D rendering are identical** across all three (same rotor/turbulence 2-D
colormap, continuous wind colour scale, opacity, legends, scale bar, terrain-locked rotation +
right-drag pan). Because the unified app is copied — not fully composed — from the two, a UI fix may
need to land in more than one window until the shared row-builders are extracted (tracked debt).

## Hard facts you must not violate (they cause silent wrong results)

- DEM for WindNinja must be **north-up projected (UTM)**, in **meters** both
  horizontally *and* vertically, domain **< ~50 × 50 km**.
- WindNinja simulates **one instant in time**; the "flight period" is a **loop** over
  hourly snapshots, one run per hour. It does **not** step forward in time.
- The momentum solver's `write_vtk_output` writes the **mass-solver mesh**, *not* the
  full OpenFOAM field. To get the real 3D recirculation you must read the OpenFOAM
  **case directory** directly (`flow/openfoam_reader.py`). Do not trust that VTK for 3D.
- OpenFOAM (the momentum solver engine) is **CPU-bound**. A strong GPU does **not**
  accelerate the solve; it accelerates **rendering** only. Solver speed ≈ CPU cores.
- Pass-1 output is **candidates, not rotors** (see above). Keep the two passes visually
  distinct; never blend them into a fake continuum.

## Project status

Both passes work end-to-end, now in a single **unified app** (`souslevent.window`, ADR-0033, v1.0,
MIT-licensed, wheel + Windows exe built) plus two legacy backups. Pass-1 (hourly, parallel) screens
for candidate reliefs; Pass-2 (momentum + 3D) resolves the recirculation. **Every workflow reviews
its zones in the candidates tab before solving** (ADR-0036/0037): screened candidates with a
browsable hourly hazard, a paving preview (union corridor mask paved by ONE regular grid — no
stacked tilings on self-crossing routes), or hand-drawn rectangles; zone sizes are capped so the
**momentum mesh matches the topo resolution** (calibrated law, mesh↔topo arbitration popups). The
pipeline adds: AROME-HD local wind **or a manual homogeneous wind grid** (ADR-0034), parallel solves
with honest wave-based progress/ETA, disk-safe case handling, a time (or scenario) slider over
absolute dates, four lee representations with adjustable scales, route wind arrows (size/altitude
sliders), and save/open of results (`.sillage`, Zip-Slip-safe). See `docs/03_decisions.md` (ADRs),
`docs/07_roadmap.md`, `docs/10_auto_pipeline.md` and `docs/06_dev_log.md`.

## Conventions

- Wind direction follows the **meteorological convention**: the direction the wind
  comes *from*, in degrees, 0° = North. (Same as WindNinja.)
- All internal lengths in **meters**, speeds in **m/s**. Convert at the edges only. (Display-edge
  helpers may take km/h — e.g. `viz.volume3d.wind_color(speed_kmh)` — since km/h is the shown unit.)
- Functions that shell out to WindNinja or hit a network API live in a small set of clearly-named
  modules so they are easy to mock: `flow/windninja.py` (solver), `wind/forecast.py` +
  `wind/meteofrance.py` (forecast APIs), `auto/wind.py` (AROME-HD local/route wind, ADR-0031-era),
  and the basemap tiles via `viz/map2d.py` (`import_contextily`/`add_basemap`) — reused by
  `viz/volume3d._drape_basemap` and `terrain/acquire.py`. Keep new network calls inside these.
