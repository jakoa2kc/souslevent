# 05 — WindNinja integration

The authoritative reference for how Sillage drives WindNinja. Pair with
`flow/windninja.py` (the wrapper) and `flow/openfoam_reader.py` (the 3D reader).

## What WindNinja is (and isn't)
- A **diagnostic** wind model for **complex terrain**. It computes the **spatial**
  variation of wind for **one instant in time**. It **does not step forward in time** and
  does **not** forecast future times itself. → our "flight window" is a **loop** over
  hourly snapshots, one run per hour, each driven by that hour's forecast.

## The two solvers — constraint matrix

| Capability | Conservation of **mass** | Conservation of mass **+ momentum** (NinjaFOAM) |
|---|---|---|
| Engine | native finite-element, steady, incompressible | OpenFOAM `simpleFoam`, k-epsilon, finite-volume, terrain-following hex mesh |
| Captures **eddies / reversed flow** | **No** (shows low speed, never reversal) | **Yes** (resolves recirculation) |
| **Weather-model** initialization | **Yes** | **No** |
| **Point** initialization | Yes | **No** |
| **Domain-average** wind init | Yes | **Yes** |
| Diurnal slope winds + non-neutral **stability** | **Yes** | **to verify** (do not assume) |
| Speed | seconds | seconds-to-minutes (more iterations / cells) |
| Our usage | **Pass 1** (whole domain, hourly, weather init) | **Pass 2** (local, homogeneous wind) |

> The single asymmetry that drives the architecture: the solver that ingests the
> spatially-varying forecast (mass) is the one that *cannot* show rotors; the solver that
> shows rotors (momentum) *only* takes one uniform wind. Hence two passes.

## Inputs WindNinja needs
- A **DEM** (`.tif`/`.asc`/`.lcp`/`.img`), **north-up UTM, meters H+V**, domain < ~50 km.
- An **initialization method**:
  - Pass 1: weather-model init (forecast) — *or* domain-average per hour.
  - Pass 2: **domain-average** wind = (speed, direction, input height).
- Output height, units, resolution, and output formats.

## CLI flags we rely on (see `WindNinja_cli`)
Names as they appear in the WindNinja CLI:

- `momentum_flag` — `true` selects the **momentum** solver (Pass 2). Default `false`
  (mass, Pass 1).
- `number_of_iterations` — momentum solver iteration count (accuracy/time trade-off;
  recirculation regions keep changing up to convergence, so more iterations = better lee
  accuracy, at cost).
- `mesh_count` — number of cells in the mesh (drives RAM/time; **computational**
  resolution, not DEM resolution, is the limiting factor).
- `turbulence_output_flag` — `true` to **write turbulence output**. We use turbulence
  **intensity** as a primary "is-this-dangerous" field in Pass 2 (often more meaningful
  than speed alone).
- `write_vtk_output` — **caution:** for momentum runs this writes the **mass-solver
  mesh**, *not* the full OpenFOAM field. **Do not** use it for the 3D recirculation; read
  the OpenFOAM **case directory** instead (ADR-0004).
- ASCII output options: `ascii_out_uv` (write u,v components), `ascii_out_geog`
  (EPSG:4326 lat/lon grids), `ascii_out_resolution`. UV components are convenient for the
  screening velocity-deficit computation in Pass 1.
- DEM units must be meters; WindNinja can download DEMs in the proper projection on some
  builds, but we supply our own IGN DEM.

> Always confirm exact flag names/defaults against the installed WindNinja version's
> `WindNinja_cli --help`. The wrapper centralizes them so a version change is a one-file
> fix. The CLI source of truth: `src/ninja/cli.cpp` in the WindNinja repo.

## Pass-1 invocation (mass, hourly loop) — shape
For each hour:
1. Provide the DEM + that hour's wind (weather-model init or domain-average).
2. Request ASCII u,v output at the screening resolution.
3. Collect the surface wind grid(s) → feed `screening/indicator.py`.

## Pass-2 invocation (momentum, single feature) — shape
1. **Crop** the DEM to the feature bbox **+ buffer** (upstream fetch + generous downwind
   margin so the recirculation isn't truncated by the outlet boundary).
2. Set `momentum_flag=true`, choose `mesh_count` for fine resolution (~10^6-cell class),
   set `number_of_iterations`, `turbulence_output_flag=true`.
3. Provide the **domain-average wind** read from the Pass-1 field at crest height
   upstream of the feature, at the chosen hour.
4. After the run, **read the OpenFOAM case directory** with PyVista (don't use the VTK
   export). Build streamlines + reversed-flow / turbulence-intensity volumes.

## Locating the OpenFOAM case directory
NinjaFOAM creates a **temporary OpenFOAM case** (template files come from
`WINDNINJA_DATA/ninjafoam.zip`; the DEM is converted to STL into `constant/triSurface`,
etc.). The wrapper must capture/record that temp case path for a run so
`openfoam_reader.py` can open it. Strategy: run with a known/working directory, log the
solver's temp dir from console output, and keep it until the 3D field is read. See
`flow/windninja.py` TODOs.

## Mesh sizing reference (for `mesh_count` intuition)
Historically the momentum solver used roughly **coarse ≈ 100k**, **medium ≈ 500k**,
**fine ≈ 1,000,000** cells (counts have since been tuned down). Convergence in
**non-recirculation** regions arrives earlier than in lee/recirculation regions, which
keep evolving toward full convergence. So for the **lee zones we care about**, prefer more
iterations; elsewhere fewer suffice. Run times historically land in the seconds-to-minutes
range depending on mesh/iterations.

## Docker / batch option
The **Katana** packaging runs WindNinja inside Docker bundled with **GDAL** and
**wgrib2**, designed to run WindNinja over **large areas and long periods**, cropping and
extracting forecast GRIB variables and organizing runs. A candidate for the Pass-1 hourly
loop at scale; decision deferred (ADR-0006 open question).
