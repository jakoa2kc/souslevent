# Support — Troubleshooting

Symptom → likely cause → fix. Written so a third party or AI can diagnose quickly. Many
of these are *silent* failures (plausible-looking but wrong output) — the dangerous kind.

## Results look wrong but no error

| Symptom | Likely cause | Fix |
|---|---|---|
| Lee zones show only **low speed, never reversal**, even in Pass 2 | You read the **mass** field, or the momentum `write_vtk_output` (which is the mass mesh) | Use the **momentum** solver *and* read the **OpenFOAM case directory** (`openfoam_reader.py`), not the VTK export. (ADR-0004) |
| Wind directions all rotated / mirrored; flow ignores ridge orientation | DEM **not north-up**, or not projected | Reproject DEM to **best-fit UTM, north-up** in `terrain/dem.py`. |
| Speeds an order of magnitude off; weird scaling | DEM vertical/horizontal units **not meters** | Ensure meters in **both** H and V before solving. |
| Recirculation zone **cut off** at a domain edge | Pass-2 crop too tight; outlet boundary truncates the eddy | Increase **downwind buffer** (and upstream fetch) in the crop. |
| Pass-1 "rotor map" disagrees with reality / looks too smooth | Treating Pass-1 (mass) output **as rotors** | It is **candidates, not rotors** by design. Use the derived indicator; confirm with Pass 2. (ADR-0003/0005) |
| Time slider shows no change across hours | Forecast not actually varying per hour, or cached stale | Check `wind/forecast.py` fetch + cache keys; confirm distinct hourly winds. |

## WindNinja invocation errors

| Symptom | Likely cause | Fix |
|---|---|---|
| `WindNinja_cli` not found | `WINDNINJA_CLI` unset / wrong path | Set it in `.env`; verify `WindNinja_cli --help`. |
| Momentum run rejects weather-model or point init | Momentum solver supports **domain-average only** | Provide a single (speed, dir, height); read it from the Pass-1 field. (docs/05) |
| Unknown/!changed CLI flag | WindNinja version differs from docs/05 | Re-check `WindNinja_cli --help`; update **only** `flow/windninja.py` (flags are centralized there). |
| Momentum run very slow / OOM | `mesh_count` too high / domain too large | Lower mesh/iterations; shrink the crop; remember mesh (not DEM res) drives cost. |
| Can't find the OpenFOAM case dir to read | Temp case path not captured | Run with a known working dir; log/record the solver temp path; keep it until read. (docs/05) |
| Lee accuracy poor even with momentum | Too few iterations; lee regions converge slowest | Increase `number_of_iterations` (lee/recirculation needs more than attached flow). |

## GPU / performance confusion

| Symptom | Likely cause | Fix |
|---|---|---|
| Strong GPU but solver no faster | OpenFOAM `simpleFoam` is **CPU-bound** | Expected. GPU accelerates **rendering** only; scale solves with **CPU cores**. (ADR-0006) |
| Rendering slow / laggy in 3D | Too many streamlines / huge mesh on CPU rendering | Ensure PyVista uses the GPU; decimate mesh; cap streamline seeds. |

## Geospatial / dependency issues

| Symptom | Likely cause | Fix |
|---|---|---|
| `rasterio`/`pyproj` import or PROJ errors | Missing/mismatched system GDAL/PROJ | Use conda-forge or a GDAL-provisioned image; align versions. |
| DEM has holes / NaNs after load | No-data not filled | Fill no-data in `terrain/dem.py` (and/or let WindNinja fill). |
| Reprojection picks the wrong UTM zone | Area spans a zone boundary / auto-pick off | Force the best-fit UTM zone explicitly for the area. |

## Data / forecast issues

| Symptom | Likely cause | Fix |
|---|---|---|
| Forecast fetch fails / rate-limited | Network, API change, missing key (AROME) | Check `wind/forecast.py`; set `METEOFRANCE_API_KEY` if using AROME; rely on cache offline. |
| Crest-height wind looks unphysical | Profile reduction picking wrong level | Verify pressure-level→altitude mapping in `wind/profile.py`. |

## General debugging method
1. **Reproduce offline** from cached DEM + forecast (deterministic).
2. **Isolate the layer:** terrain vs wind vs solver vs screening vs viz — each module is
   separable and mockable by design.
3. For solver issues, inspect WindNinja **console output** and the OpenFOAM **log files**
   in the temp case (`log.checkMesh`, etc.).
4. Re-read `CLAUDE.md` "Hard facts you must not violate" — most silent-wrong-result bugs
   are a violation of one of them.
