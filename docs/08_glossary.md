# 08 — Glossary

Cross-domain vocabulary (paragliding + CFD + meteorology + geospatial) so a newcomer or
AI can read the codebase and docs without ambiguity.

## Flying / phenomenon
- **Leeward (sous le vent)** — the downwind side of a relief; where disturbed air forms.
- **Windward (au vent)** — the upwind side; generally smoother, often lift-producing.
- **Rotor** — a zone of recirculating, reversed, turbulent flow in the lee of a relief;
  dangerous to a wing. The primary thing Sillage exists to locate.
- **Separation** — the flow detaching from the terrain surface on the lee side; the
  viscous mechanism that creates the rotor. (Inviscid flow does not separate.)
- **Lee wave** — wave in the airflow downwind of a ridge under stable stratification;
  can be associated with rotors beneath wave crests.
- **Anabatic / katabatic wind** — upslope (daytime heating) / downslope (nighttime
  cooling) thermally-driven winds; part of *diurnal* mountain flow.
- **Combe** — a valley/hollow in the relief; a terrain feature relevant for Pass-2 sites.
- **Arête / épaule** — rock ridge / shoulder of a mountain; classic separation features.

## Wind / meteorology
- **Meteorological wind direction** — the direction wind comes *from*, degrees, 0° = N.
  Used throughout Sillage and by WindNinja.
- **Crest-height wind** — wind speed/direction at the elevation of the ridge crest; used
  to init Pass 1 and as the homogeneous BC for Pass 2.
- **Vertical (wind) profile** — wind as a function of altitude / pressure level.
- **Stability (atmospheric)** — tendency of the atmosphere to suppress (stable) or
  enhance (unstable) vertical motion; strongly modulates lee turbulence.
- **Diurnal winds** — time-of-day-driven flows (slope/valley winds).
- **AROME** — Météo-France high-resolution (~1.3 km) numerical weather model.
- **Open-Meteo** — free weather API providing wind by pressure level, hourly.
- **ERA5** — Copernicus reanalysis dataset (past), for validation/hindcast.

## CFD / solver
- **DEM (Digital Elevation Model)** — gridded terrain elevation; the cartography input.
- **Mass-consistent / diagnostic model** — flow forced to follow terrain and conserve
  mass *without solving momentum*; fast; **cannot represent reversed flow** (no rotor).
- **RANS** — Reynolds-Averaged Navier-Stokes; steady mean-flow CFD with a turbulence
  closure. WindNinja's momentum solver is RANS.
- **k-epsilon** — the turbulence closure model used by WindNinja's momentum solver.
- **simpleFoam** — OpenFOAM's steady incompressible RANS solver; the momentum engine.
- **OpenFOAM** — the open-source CFD toolbox underlying WindNinja's momentum solver.
- **NinjaFOAM** — WindNinja's OpenFOAM-based momentum solver.
- **Mesh / mesh_count** — the computational grid; cell count drives RAM and run time.
  **Computational** resolution (mesh), not DEM resolution, is the cost driver.
- **Iterations (number_of_iterations)** — solver steps for the momentum solver; lee /
  recirculation regions need more to converge than attached-flow regions.
- **Turbulence intensity** — normalized measure of turbulent velocity fluctuation; used
  as a primary "is-this-dangerous" field in Pass 2 (often better than speed alone).
- **Reversed-flow volume** — region where the along-mean-flow velocity component is
  negative; an operational definition of the recirculation/rotor volume.
- **Streamline** — a curve tangent to the velocity field; used to visualize circulation.
- **OpenFOAM case directory** — the folder of OpenFOAM input/output for a run; Sillage
  reads it **directly** (PyVista) to get the true 3D momentum field (ADR-0004).

## Geospatial
- **UTM** — Universal Transverse Mercator projection; WindNinja needs a **north-up UTM**
  DEM in **meters**.
- **North-up** — the grid's +y axis points to geographic north (required by WindNinja's
  wind convention).
- **No-data** — missing cells in a DEM; filled by interpolation before solving.
- **RGE ALTI** — IGN's high-resolution (1 m / 5 m) French national elevation product.
- **SRTM** — ~30 m near-global elevation dataset (fallback / prototyping).
- **Winstral shelter index (maxus)** — max upwind slope within a search distance, per
  wind direction; classifies a cell as sheltered or exposed. A no-solver pre-filter.

## Project terms
- **Pass 1 / screening** — fast mass-solver run over the whole domain, hourly → derived
  hazard indicator → **candidate** zones. Not a rotor map.
- **Pass 2 / detail** — momentum-solver run on a small feature with homogeneous wind →
  true 3D recirculation.
- **Candidate** — a (location, hour) flagged by Pass 1 as worth a Pass-2 run.
- **Handoff** — reading Pass-1 crest-height wind to drive Pass 2; and the buffered crop
  defining Pass-2's domain.
- **Sillage** — the project name; French for *wake*.
