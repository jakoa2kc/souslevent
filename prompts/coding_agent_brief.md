# Prompt — Coding-agent brief & task templates

Briefs for an AI coding agent (or a developer) to pick up concrete work. Use together
with `context_primer.md` (paste that first) and the `docs/` tree.

## Operating rules for any agent on this repo
1. **Read `CLAUDE.md` first**, then the relevant `docs/` file for the area you touch.
2. **Respect the two-pass contract** and the "Hard facts" list — most bugs here are
   violations of those.
3. **Keep WindNinja behind `flow/windninja.py`.** Verify flags against the installed
   version (`WindNinja_cli --help`); update flags in that one file only.
4. **Keep modules separable and mockable.** Network (`wind/forecast.py`) and subprocess
   (`flow/windninja.py`) are isolated so tests can stub them.
5. **Units:** meters, m/s, meteorological wind direction. Convert only at edges.
6. **Document as you go:** append to `docs/06_dev_log.md`; add an ADR to
   `docs/03_decisions.md` for any significant decision; update `docs/07_roadmap.md`
   checkboxes.
7. **Safety framing is non-negotiable:** Pass-1 output is *candidates, not rotors*; UI and
   docstrings must say so. Don't imply false precision.
8. **Ask before** adding heavy dependencies or changing the architecture.

## Definition of done (per task)
- Code runs in the editable install; touched module importable.
- Docstrings explain *why*, with cross-refs to the relevant `docs/` section.
- A test or a runnable demo snippet exercises the new path (mock the solver/network).
- Docs updated (dev log + roadmap; ADR if a decision was made).

---

## Task template
```
TASK: <one line>
CONTEXT: <which pass/module, link docs section>
CONSTRAINTS: respect two-pass contract + Hard facts (CLAUDE.md). Units m / m/s / met. dir.
INPUTS: <files, data, params>
OUTPUT: <function/CLI/visual, with type contracts>
DONE WHEN: <observable result> + docs updated (dev log/roadmap/ADR) + test or demo.
DO NOT: present Pass-1 output as rotors; call WindNinja outside flow/windninja.py;
        add heavy deps without asking.
```

---

## Ready-to-use tasks (M1 — Pass-1 pipeline)

### T1 — DEM loading & validation (`terrain/dem.py`)
Load a GeoTIFF DEM, reproject to **best-fit UTM, north-up**, ensure **meters H+V**,
validate **domain < ~50 km**, fill no-data. Return an object carrying the array, the
affine transform, CRS, and resolution. *Done when:* a real IGN/SRTM tile loads and
validates; a unit test checks CRS is projected/north-up and units are meters.

### T2 — Terrain morphometry (`terrain/geometry.py`)
From a loaded DEM compute **slope**, **aspect**, **ridge/crest** mask, and the
**Winstral shelter index** (max upwind slope within a search radius, given a wind
direction). Pure array ops, no solver. *Done when:* fields compute on the T1 DEM; a test
checks shapes/ranges and that shelter responds to wind direction.

### T3 — WindNinja mass wrapper (`flow/windninja.py::run_mass`)
Build args for `WindNinja_cli` (mass solver, ASCII u,v output at a chosen resolution),
shell out, return output paths. Centralize all flag names. *Done when:* a dry-run builds
the correct command (testable without the binary via a `dry_run` flag); with the binary
present it produces grids on the T1 DEM.

### T4 — Wind forecast + profile (`wind/forecast.py`, `wind/profile.py`)
Fetch hourly wind by pressure level (Open-Meteo) for the area; reduce to **crest-height
wind** per hour. Cache responses. *Done when:* returns a per-hour (speed, dir) series;
network mocked in tests; one hour can be hard-coded to unblock downstream work.

### T5 — Screening indicator (`screening/indicator.py`)
Combine T2 geometry (lee-slope vs wind dir, ridges) + the mass field's **downwind
velocity deficit** + empirical ratios into a normalized **hazard indicator** per cell per
hour; emit **ranked candidates**. *Done when:* given a DEM + a (mock) mass field + wind,
returns an indicator grid and a candidate list; a test checks normalization and that a
steep lee slope facing the wind scores high.

### T6 — 2D screening map (`viz/map2d.py`)
Render the indicator over the domain (matplotlib first), then add a **time slider** over
hours; expose a hook that, on clicking a hotspot, yields `(feature_bbox, hour)`. *Done
when:* a single-hour map renders from T5 output; slider iterates hours; click hook returns
a region+hour. **Label clearly: candidates, not rotors.**

### T7 — Demo (`scripts/demo_pass1.py`)
Wire T1–T6 on one known relief into a single runnable command. *Done when:* one command
produces a time-sliderable hazard map from a real DEM + (real or stubbed) hourly winds.

---

## Ready-to-use tasks (M2 — Pass-2)

### T8 — Momentum wrapper (`flow/windninja.py::run_momentum`)
Crop+buffer the DEM, set `momentum_flag`, `turbulence_output_flag`, `mesh_count`,
`number_of_iterations`; **capture the OpenFOAM case directory path**. *Done when:* dry-run
builds the right command and records where the case dir will be.

### T9 — OpenFOAM reader (`flow/openfoam_reader.py`)
Read the OpenFOAM **case directory** with PyVista → 3D field (velocity, turbulence).
**Not** the VTK export. *Done when:* given a case dir, returns a PyVista mesh with the
velocity field; handles missing fields gracefully.

### T10 — 3D volumes (`viz/volume3d.py`)
Terrain surface + streamlines + **reversed-flow volume** (threshold on along-mean-flow
velocity sign) and/or **turbulence-intensity volume**; windward green / leeward
red-orange by severity. *Done when:* a hand-picked arête + wind yields a 3D recirculation
scene.
```
```
