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
    partition.py         feature_domains (hazard) + corridor_tiles (blind paving) + corridor_mask
    wind.py              local AROME-HD wind per domain + route wind series (arrows)
    arome.py             forecast window (absolute dates) from the AROME/Open-Meteo horizon
    scene.py             aggregate one hour into a 3D scene (extract_volume rotor/turbulence)
    store.py             save/open a run as a portable .sillage bundle (lee meshes only)
    progress.py          parallelism-aware (wave) progress + ETA
    window.py            the automatic-mode desktop app (route + window -> 3D wake)
  app/
    main_window.py       the manual 2-pass desktop app (draw zone, Pass-1 map, Pass-2 3D)
    map_tab.py           Leaflet map tab (rectangle OR multi-segment route) shared by both apps
    jobs.py              background QThread job runner (progress/cancel)
scripts/
  sillage_gui.py         launch the manual app    sillage_auto.py  launch the automatic app
  demo_pass1.py          end-to-end Pass-1 walkthrough
```

**Two apps, one engine.** `app.main_window` (manual: pick a feature, see its precise rotor) and
`auto.window` (automatic: draw a route, get the whole corridor's wake) share `viz.volume3d`,
`app.map_tab`, `flow/*` and `wind/*`, so **results and 3D rendering are identical** (same rotor/turbulence
2-D colormap, same continuous wind colour scale, opacity, legends, scale bar, terrain-locked rotation
+ right-drag pan).

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

Both passes work end-to-end in two desktop apps. Pass-1 (hourly, parallel) screens for candidate
reliefs; Pass-2 (momentum + 3D) resolves the recirculation and is driven either by hand (manual app)
or automatically along a flight route (auto app). The automatic pipeline adds: AROME-HD local wind,
feature-based **or** blind-corridor domains, parallel solves with honest wave-based progress/ETA,
disk-safe case handling, a time slider over absolute dates, rotor **and** turbulence volumes on a
2-D (height × intensity) colormap with adjustable scales, route wind arrows, and save/open of results
(`.sillage`). See `docs/07_roadmap.md`, `docs/10_auto_pipeline.md` and `docs/06_dev_log.md`.

## Conventions

- Wind direction follows the **meteorological convention**: the direction the wind
  comes *from*, in degrees, 0° = North. (Same as WindNinja.)
- All internal lengths in **meters**, speeds in **m/s**. Convert at the edges only.
- Functions that shell out to WindNinja or hit a network API are isolated in
  `flow/windninja.py` and `wind/forecast.py` so they are easy to mock in tests.
