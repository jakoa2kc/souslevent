# 02 — Architecture

## Principle

**Adaptive multi-resolution.** Cheap, broad screening over the whole domain and the whole
flight window (Pass 1), then expensive, precise detail only where and when screening flags
a candidate (Pass 2). Each solver is used strictly where it is physically valid.

## Dataflow

```
                         ┌──────────────────────────────────────────┐
                         │                TERRAIN                    │
   IGN RGE ALTI DEM ───► │  load → reproject UTM north-up → validate │ ──┐
                         │  derive: slope, aspect, ridges, shelter   │   │
                         └──────────────────────────────────────────┘   │
                                                                         │
   Open-Meteo / AROME                                                    │
   wind by altitude, ──► ┌───────────────┐                              │
   hour by hour          │     WIND      │ wind profile @ crest height  │
                         │  fetch+profile│ ───────────────┐             │
                         └───────────────┘                │             │
                                                          ▼             ▼
                                         ┌────────────────────────────────────────┐
   ===================  PASS 1  =========│  FLOW (mass solver, whole domain)       │
                                         │  WindNinja_cli, weather-model init,     │
                                         │  one run per hour  → surface wind grids │
                                         └───────────────────────┬────────────────┘
                                                                 │ velocity field(s)
                                                                 ▼
                                         ┌────────────────────────────────────────┐
                                         │  SCREENING (derived hazard indicator)   │
                                         │  terrain geom ⊕ velocity deficit ⊕      │
                                         │  empirical rules  → candidate zones     │
                                         └───────────────────────┬────────────────┘
                                                                 │ candidates (x,y,hour)
                                                                 ▼
                                         ┌────────────────────────────────────────┐
                                         │  VIZ map2d: 2D hazard map + time slider │  ◄── user explores,
                                         └───────────────────────┬────────────────┘      clicks a hotspot
                                                                 │ (feature bbox, hour)
   ===================  PASS 2  ==========================================▼=========
                                         ┌────────────────────────────────────────┐
                                         │  FLOW (momentum solver, local sub-domain)│
                                         │  crop DEM + buffer; homogeneous wind read│
                                         │  from Pass-1 field; WindNinja momentum   │
                                         └───────────────────────┬────────────────┘
                                                                 │ OpenFOAM case dir
                                                                 ▼
                                         ┌────────────────────────────────────────┐
                                         │  FLOW openfoam_reader (PyVista)         │
                                         │  read CASE directly (not the VTK export)│
                                         └───────────────────────┬────────────────┘
                                                                 ▼
                                         ┌────────────────────────────────────────┐
                                         │  VIZ volume3d: streamlines + reversed-  │
                                         │  flow / turbulence-intensity volumes    │
                                         └────────────────────────────────────────┘
```

## Modules and responsibilities

### `terrain/`
- `dem.py` — load a DEM (GeoTIFF), **reproject to the best-fit UTM zone, north-up**,
  ensure meters in H and V, validate domain size (< ~50 km), fill no-data. This is the
  shared source for both passes; Pass 2 crops from it.
- `geometry.py` — morphometry from the DEM: **slope**, **aspect**, **ridge/crest
  detection**, and the **Winstral shelter index** (max upwind slope within a search
  radius, per wind direction). Pure NumPy/array ops; no solver, no network.

### `wind/`
- `forecast.py` — fetch wind by **altitude/pressure level**, **hour by hour**, for the
  area (Open-Meteo; AROME for high-res local). Network-isolated for mockability.
- `profile.py` — reduce a forecast to the quantities the solvers need: **wind speed +
  direction at crest height** for each hour (Pass-1 init and Pass-2 boundary condition).

### `flow/`
- `windninja.py` — the **only** place that shells out to `WindNinja_cli`. Two entry
  points: `run_mass(...)` (Pass 1, weather-model init, hourly loop) and
  `run_momentum(...)` (Pass 2, domain-average wind). Returns paths to outputs / the
  OpenFOAM case directory. Pure subprocess orchestration + argument building.
- `openfoam_reader.py` — read the **OpenFOAM case directory** with PyVista's OpenFOAM
  reader to recover the true 3D field. **Do not** rely on WindNinja's momentum
  `write_vtk_output` for the 3D field — that export is the *mass-mesh*, not the foam
  field. (See ADR-0004.)

### `screening/`
- `indicator.py` — combine terrain geometry (lee-slope vs wind dir, ridges), the
  **velocity deficit** from the Pass-1 mass field, and **empirical ratios** into a
  single normalized **hazard indicator** per cell per hour. Emits ranked **candidate
  features** (location + hour) for Pass 2.

### `viz/`
- `map2d.py` — 2D hazard map over the domain with a **time slider**; clicking a hotspot
  yields a `(feature_bbox, hour)` request for Pass 2. (Triage view.)
- `volume3d.py` — PyVista scene: terrain surface + streamlines + **reversed-flow
  volume** (`threshold` on along-flow velocity sign) and/or **turbulence-intensity
  volume**, windward green / leeward red-orange by severity. (Detail view.)

### `app/` and `auto/` — the application layer (two apps, one engine)

The libraries above are driven by **two desktop apps** that share every lower layer (so their
**results and 3D rendering are identical**):

- `app/main_window.py` — the **manual** 2-pass app (`sillage-gui`): draw a zone, browse the Pass-1
  hazard by hour, draw a Pass-2 rectangle, inspect the 3D rotor/turbulence of that feature.
- `auto/` — the **automatic** pipeline (`sillage-auto`, see `docs/10_auto_pipeline.md`): draw a flight
  **route** (multi-segment) + a window, then `run_auto` solves Pass-2 along the corridor — either on
  Pass-1 **features** or by **blind paving** — and aggregates per-hour 3D scenes. Submodules:
  `pipeline` (orchestrator), `partition` (feature/corridor domains), `wind` (AROME-HD + route arrows),
  `arome` (forecast window), `scene` (`extract_volume` + aggregate), `store` (`.sillage` save/open),
  `progress` (wave ETA), `window` (UI).
- `app/map_tab.py` — the **Leaflet/QtWebEngine** map shared by both (rectangle AOI **or** multi-segment
  route + live corridor + wind arrows). `app/jobs.py` — the background `SolveJob` (progress/cancel).

Rendering is centralised in `viz/volume3d.py`: basemap drape (reprojected, zoom-boosted), the rotor /
turbulence **2-D colormap** (height × intensity) on a single absolute scale, uniform adjustable
opacity, continuous wind-speed arrows + legends, scale bar, terrain-locked rotation + right-drag pan.

## The two handoffs (the load-bearing interfaces)

1. **Wind → Pass 2 boundary condition.** Pass 2's homogeneous input wind is *read from
   the Pass-1 field* at crest height just upstream of the feature, at the chosen hour.
   Pass 1 manufactures Pass 2's boundary condition.
2. **Screening → Pass 2 domain.** The candidate's bounding box, **buffered** with
   upstream fetch and generous downwind margin (the eddy extends far; don't truncate it),
   defines the Pass-2 crop.

## Why two distinct representations (not one blended view)

Pass 1 and Pass 2 measure **different physical quantities** (a derived likelihood vs a
resolved mean field). Blending them into a single seamless visual would imply a precision
Pass 1 does not have. The UI keeps them distinct: a 2D triage map and a separate 3D
detail scene. (ADR-0005.)

## Performance posture

- Pass 1 runs at **coarse computational resolution** (~30–100 m) over the full domain;
  many hours, but each run is seconds.
- Pass 2 runs at **fine resolution** (~10–30 m) on a small crop, toward the ~10^6-cell
  fine mesh; seconds-to-minutes per run.
- The solver is **CPU-bound** (OpenFOAM). The **GPU accelerates rendering only**
  (PyVista/VTK). Parallelism budget: more CPU cores → more concurrent/faster solves.
- It is the **computational** resolution (the solver mesh), not the DEM resolution, that
  drives RAM and time. The fine IGN DEM is just the common source; WindNinja resamples it
  to build its internal mesh.
