# Prompt — Context primer (paste into any AI assistant)

> Paste this whole block at the start of a session with an AI assistant that has **not**
> read the repo, to bring it up to speed fast. For deeper context, also point it at
> `CLAUDE.md` and the `docs/` tree.

---

You are helping with **Sillage**, a Python desktop tool that computes and visualizes the
**leeward turbulence / rotor zones** that form when wind flows over mountain terrain, to
help a paraglider pilot decide where it is safe to fly. Inputs: a fine terrain model (DEM)
+ wind forecasts by altitude, hour by hour. The developer is an experienced programmer
flying in the French Alps; this is a serious personal project developed in VSCode.

**The architecture you must respect — two passes, two solvers, because no single solver
can do both jobs:**

1. **Pass 1 — screening.** WindNinja's **conservation-of-mass** solver, run over the
   **whole area, hour by hour**, driven by the spatially-varying forecast (the *only*
   solver that accepts weather-model init). This solver **physically cannot represent
   reversed flow** — in a lee eddy it shows only low speed, never reversal. So **Pass 1
   does NOT draw rotors; it produces a derived hazard indicator that flags candidate
   zones.** The indicator combines: terrain geometry (lee-slope steepness vs wind
   direction, ridge detection), the mass field's downwind velocity deficit, and empirical
   rules (~5–7×relief-height downwind extent).

2. **Pass 2 — detail.** WindNinja's **momentum** solver (NinjaFOAM = OpenFOAM `simpleFoam`,
   k-epsilon RANS), run on a **small sub-domain** around a candidate feature, with a
   **single homogeneous wind** (it does *not* accept weather-model or point init). This
   produces the **true 3D recirculation**. Get the 3D field by **reading the OpenFOAM case
   directory directly with PyVista** — NOT WindNinja's momentum `write_vtk_output`, which
   is the mass mesh, not the real field.

**Hard facts (violating them causes silent wrong results):**
- DEM must be **north-up UTM, meters in H and V, domain < ~50 km**.
- WindNinja simulates **one instant**; the flight window is a **loop**, one run per hour.
- OpenFOAM is **CPU-bound**; a strong GPU accelerates **rendering**, not solving.
- **Pass-1 output is candidates, not rotors** — never present it as a rotor map.
- Wind direction = **meteorological** convention (from-direction, 0°=N). Lengths in
  meters, speeds in m/s.

**Handoffs:** Pass 2's homogeneous wind is **read from the Pass-1 field** at crest height
upstream of the feature; the Pass-2 crop is the candidate bbox **buffered** (upstream
fetch + generous downwind margin so the eddy isn't truncated).

**Module map (`src/sillage/`):** `terrain/{dem,geometry}`, `wind/{forecast,profile}`,
`flow/{windninja,openfoam_reader}`, `screening/indicator`, `viz/{map2d,volume3d}`.
WindNinja is shelled out to **only** in `flow/windninja.py` (flags centralized there).

**Current state:** scaffold; building the Pass-1 pipeline end to end first (M1 in
`docs/07_roadmap.md`). Pass 2 is stubbed with clear contracts.

When you propose code or changes: keep modules separable/mockable, keep units consistent,
verify any WindNinja flag against the installed version, update `docs/06_dev_log.md` and
add an ADR to `docs/03_decisions.md` for any significant decision, and **never** let
Pass-1 output be framed as a rotor map. Ask before introducing heavy new dependencies.
