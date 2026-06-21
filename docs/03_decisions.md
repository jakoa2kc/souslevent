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

## Open questions tracked as future ADRs

- **Stability / diurnal winds on the momentum solver.** Available on the mass solver
  (diurnal slope winds, non-neutral stability); availability on momentum is **to verify**.
  Will become an ADR once confirmed. Affects how much of the physics enrichment lands in
  Pass 1 vs Pass 2.
- **Batch engine choice for the hourly loop** — native `WindNinja_cli` subprocess vs the
  Docker/Katana packaging. Decide once Pass-1 volumes/time-ranges are real.
- **2D map / UI toolkit** — matplotlib for the first map vs a richer Qt/web stack as the
  app grows.
