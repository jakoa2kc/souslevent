# SousLeVent

**Leeward turbulence / rotor mapping for mountain paragliding.**

SousLeVent computes and visualizes the disturbed air — the *wake* (French: *sillage*) — that
forms downwind of mountain terrain when wind blows over it, so a pilot can see **where**
and **when** the dangerous, rotor-prone zones will be before flying. Inputs: a fine
terrain model (DEM) + wind forecasts by altitude, hour by hour.

> ⚠️ **Decision-support tool, not a guarantee.** Outputs are approximations. The
> screening map shows *likelihood of disturbed air*, **not** certified rotor boundaries.
> Never substitute it for training, judgement, or an official weather briefing.

---

## The core idea: two passes, two solvers

No single solver does both jobs, so Sillage uses two, each where it is physically valid.

| | **Pass 1 — screening** | **Pass 2 — detail** |
|---|---|---|
| Solver | WindNinja *conservation of mass* | WindNinja *momentum* (OpenFOAM RANS) |
| Domain | whole flying area | one small feature (arête, summit, shoulder…) |
| Wind | spatially-varying forecast, hour by hour | a single homogeneous wind |
| Captures rotors? | **No** (cannot represent reversed flow) | **Yes** (resolves recirculation) |
| Output | a *derived hazard indicator* → **candidates** | the true **3D recirculation** volume |

You sweep a time slider over the Pass-1 map, a hotspot lights up, you click it, and that
launches a Pass-2 run for the detailed 3D view. **Why this design (and why not simpler
approaches like potential flow) is documented in `docs/`** — start with `CLAUDE.md`.

## Repository layout

```
CLAUDE.md              ← read this first (orientation for humans & AI tools)
docs/                  ← the full reasoning trail + technical & support docs
  00_project_overview  01_theory_and_physics  02_architecture  03_decisions (ADRs)
  04_data_sources      05_windninja_integration  06_dev_log     07_roadmap  08_glossary
  support/             environment.md  troubleshooting.md
  10_auto_pipeline     ← the one-click automatic mode
prompts/               ← paste-in context for AI assistants (context_primer, coding_agent_brief)
src/sillage/           ← the package
  terrain/  wind/  flow/  screening/  viz/   config.py
  souslevent/           ← unified desktop app (rectangle/route + 3 calculation modes)
  app/                 ← manual desktop app (main_window) + shared map_tab + jobs
  auto/                ← automatic pipeline (route → corridor → Pass-2 ×hours → 3D) + save/open
scripts/               ← sillage_gui.py (manual), sillage_auto.py (auto), demo_pass1.py, …
tests/
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .                 # core deps (numpy, scipy, rasterio, pyproj, pyvista, ...)
cp .env.example .env             # then set WINDNINJA_CLI etc. (see docs/support/environment.md)

# Pass-1 screening, geometry-only (no WindNinja, no network) on your DEM:
python scripts/demo_pass1.py --dem path/to/dem.tif --wind-dir 270 --wind-speed 12

# add a real WindNinja mass run (needs WindNinja_cli) and/or a live forecast:
python scripts/demo_pass1.py --dem path/to/dem.tif --run-windninja --fetch-forecast
```

Desktop apps (need WindNinja installed + a display):

```bash
python scripts/souslevent.py       # unified: rectangle or route, then one of the 2 workflows
python scripts/sillage_gui.py     # manual: draw a zone, Pass-1 map, draw a Pass-2 rectangle → 3D
python scripts/sillage_auto.py    # automatic: draw a flight route + window → corridor wake (3D)
```

After `pip install .`, the console command is `souslevent` (the Qt/VTK GUI stack ships in the base
install). The two older apps stay runnable locally via `python scripts/sillage_gui.py` /
`scripts/sillage_auto.py` as legacy backups.

You also need **WindNinja** installed separately (provides `WindNinja_cli` and the
momentum solver). See `docs/support/environment.md`.

## Status

The **SousLeVent** app is the global UI: select a **rectangle or a route**, then choose one of two
workflows — **Pass-1 then manual candidate selection** (browse the hourly hazard, pick the zones worth
solving, Pass-2 on all hours for them), or **Pass-2 everywhere** (blind corridor paving, no Pass-1).
Wind is the hourly AROME forecast **or** a manual homogeneous speed × direction grid. The two older
desktop apps are kept as backups. Roadmap in `docs/07_roadmap.md`; the chronological reasoning trail
in `docs/06_dev_log.md`; the automatic mode in `docs/10_auto_pipeline.md`.

## For AI tools / new contributors

Paste `prompts/context_primer.md` into your assistant, then point it at `CLAUDE.md` and
the `docs/` tree. The docs are written specifically so a third party or AI can recover the
full context and the *why* behind every major decision.

## Hard facts (violating them causes silent wrong results)

- DEM must be **north-up UTM, meters (H+V), domain < ~50 km**.
- WindNinja simulates **one instant** → the flight window is an **hourly loop**.
- For 3D, read the **OpenFOAM case directory**, not the momentum `write_vtk_output` (that
  export is the mass mesh).
- OpenFOAM is **CPU-bound**; the GPU accelerates **rendering** only.
- **Pass-1 output is candidates, not rotors.**

## License

TBD. Respect the licenses of IGN (RGE ALTI), Météo-France (AROME), Open-Meteo, and
Copernicus/ERA5 as those integrations are used.
