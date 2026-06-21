# 04 — Data sources

## Terrain (DEM)

### Primary: IGN RGE ALTI (France)
- **Resolution:** 1 m / 5 m — ideal for the fine detail this project needs in the Alps.
- **Why:** the flying area is French mountain terrain; RGE ALTI is the highest-quality
  national elevation product.
- **Use:** source DEM for both passes; Pass 2 crops a small window from it.

### Fallback / outside France: SRTM
- **Resolution:** ~30 m worldwide. Coarser but globally available for prototyping or
  non-French areas.

### Hard requirements before WindNinja will accept a DEM
These cause **silent wrong results** if violated:
- **North-up projected** coordinate system — use the **best-fit UTM** zone. WindNinja
  uses the meteorological wind convention (north = up), so the grid must be north-up.
- **Meters** for **both** horizontal and vertical units.
- Domain **< ~50 × 50 km** (recommended). Larger domains are discouraged.
- No-data handled — WindNinja can fill no-data via interpolation; we also fill in
  `terrain/dem.py` to be safe.

`terrain/dem.py` enforces all of the above on load.

## Wind forecast

### Open-Meteo (primary, free)
- Provides wind by **pressure level** (i.e. by altitude), **hourly**, with good Alpine
  coverage. Free, no key for typical usage.
- Use: build the **vertical wind profile** at the area, per hour → reduce to **wind at
  crest height** for solver input.

### Météo-France AROME (high-resolution local)
- ~**1.3 km** mesh, high-resolution local model. Use where finer forecast structure
  matters than Open-Meteo provides. Access via the Météo-France API.

### ERA5 (reanalysis, optional)
- Via `cdsapi`. Reanalysis (past), useful for **validation / hindcast** against known
  flying days, not for forecasting.

## What the solvers consume

| Pass | Wind input it accepts | Where it comes from |
|---|---|---|
| 1 (mass) | **weather-model initialization** (spatially varying) and/or domain-average; diurnal + stability supported | Open-Meteo / AROME, hour by hour |
| 2 (momentum) | **single domain-average** wind (speed + direction at a height) — *no* weather-model, *no* point init | **read from the Pass-1 field** at crest height upstream of the feature |

This asymmetry is the reason for the two-pass split; see `03_decisions.md` ADR-0003.

## Units & conventions (project-wide)
- Lengths: **meters**. Speeds: **m/s**. Convert only at the edges (ingest / display).
- Wind **direction = meteorological** convention: direction the wind comes *from*,
  degrees, 0° = North. Matches WindNinja.
- Time: store hourly snapshots with explicit timezone; WindNinja derives timezone from
  the DEM location, so keep DEM georeferencing correct.

## Caching / reproducibility
- Cache fetched forecasts and prepared DEMs under a local cache dir (see `config.py`),
  keyed by area + time, so runs are reproducible and offline-replayable for debugging.
- Record the exact forecast source, model run time, and DEM provenance alongside any
  saved result (important for third-party / AI reproduction of a past analysis).

## Licensing note
Respect the licenses of IGN, Météo-France, Open-Meteo, Copernicus/ERA5. Keep attribution
and terms in `docs/support/environment.md` as integrations are added.
