# 06 — Development log (reasoning trail)

A chronological journal of the *thinking*, not just the code. Append newest entries at
the bottom. The point is that a third party or AI can reconstruct **how** we arrived here,
including ideas we tried and dropped. Keep entries dated and honest about dead ends.

---

## Entry 1 — Initial concept

**Idea.** Build an app that computes and shows, in 3D, the leeward/windward zones over a
flying area from fine cartography + fine wind forecasts by altitude and hour. Imagined a
"méca flu" core for airflow around terrain — starting simple (perfect-gas / inviscid
style), later adding stability, humidity, etc. — plus a 3D particle/volume viewer, fine
DEM ingestion, and fine wind-by-altitude forecast ingestion. Develop in VSCode.

**First assessment.**
- The need is real; visual estimation of lee zones is a genuine gap in paragliding
  practice, so a dedicated tool has real value.
- **Big trap identified:** true Navier-Stokes CFD over real mountain terrain at fine
  resolution is *not* an interactive-laptop computation (WRF/OpenFOAM/SU2 class: fine 3D
  mesh, cluster hours, calibration expertise). Going straight there risks 18 months of
  convergence work and never flying with the tool.
- **Potential/inviscid "perfect gas" flow rejected:** it never separates, so by
  construction it produces **no rotor** — exactly the phenomenon of interest. Seductive
  but wrong for this problem.
- **Language:** Python — the work is ~80% data/geo/viz integration; ecosystem is
  unrivalled there. Drop to C++/Rust only if a real perf wall appears later.
- Proposed gradation: empirical heuristics → mass-consistent diagnostic (e.g. WindNinja)
  → RANS later. Start simple.

**Tooling sketch.** terrain (rasterio, IGN RGE ALTI 1 m), wind (Open-Meteo / AROME),
flow (WindNinja or mass-consistent), viz (PyVista/VTK).

---

## Entry 2 — User direction

**Decisions from the pilot:**
- Flow core: **wrap WindNinja** to start (fast, proven) rather than build from scratch.
- Target: **desktop PC app**, workstation has an **NVIDIA RTX 5060 Ti** available; first
  goal is to *see results and explore possibilities*.
- Programming comfort: **advanced**.

**Implication.** Lean into wrapping + Python glue; can be technical. GPU available → note
where it actually helps.

---

## Entry 3 — The pivotal WindNinja finding (reshaped the architecture)

Investigated WindNinja's solvers. **Key discovery that changed the plan:**
- WindNinja has **two** solvers. The fast **conservation-of-mass** solver, by how it
  represents momentum, **cannot capture eddies (reversed flow) at all** — in a lee eddy it
  shows only very low speed, never reversal. → **It cannot show the rotor.**
- The **conservation-of-mass-and-momentum** solver (**NinjaFOAM**, built on OpenFOAM
  `simpleFoam`, k-epsilon, terrain-following hex mesh) **does** capture lee eddies. → this
  is the one that answers our question. It is, in effect, real RANS CFD — so the "v2 =
  write real CFD" idea is unnecessary; it's already here.

**Two consequences:**
1. The "simple potential core then real CFD" plan collapses into "use the momentum solver
   for the real thing." (ADR-0002, ADR-0003.)
2. **Constraint:** the momentum solver **does not support weather-model or point
   initialization — only a single domain-average wind.** So the spatially-varying forecast
   *cannot* drive it directly.

Also learned:
- WindNinja simulates **one instant** → flight window = **hourly loop**, one run per hour.
- DEM must be **north-up UTM, meters H+V, < ~50 km**.
- `turbulence_output_flag` exists → turbulence intensity as a danger proxy.
- **Gotcha:** momentum `write_vtk_output` writes the **mass-mesh**, not the OpenFOAM
  field. Real 3D must come from reading the **OpenFOAM case directory** (PyVista). (ADR-0004.)
- **GPU reality:** OpenFOAM `simpleFoam` is **CPU-bound**; the RTX accelerates
  **rendering**, not the solve. (ADR-0006.)

---

## Entry 4 — The two-pass architecture (pilot's refinement, adopted)

**Pilot proposal (accepted as the core design):** first a *fast mass* computation over the
**whole map and the whole route**, with the local winds (altitude, hour by hour), for a
first **coarse** visualization/analysis; then a *more precise NinjaFOAM* computation at
**key identified places/moments** (rock arêtes, summits, shoulders, combes at a given
hour, hence with a homogeneous wind over that limited zone).

**Why this is the right call:**
- It is textbook **adaptive multi-resolution**: cheap broad screening + expensive local
  refinement.
- It uses each solver **exactly where it is valid**: Pass 1 is the **only** place the
  spatially-varying forecast can enter (weather-model init is mass-only); Pass 2 is in its
  natural regime (small domain, one upstream wind = sound BC).

**The crucial correction recorded so we don't fool ourselves in flight:**
- The mass solver **cannot draw rotors**. So **Pass 1 is a *candidate detector*, not a
  rotor map.** Its real signal is a **derived indicator** built from: (a) terrain geometry
  (lee-slope steepness vs the hour's wind direction; crest/arête/shoulder detection),
  (b) mass-field signals (downwind **velocity deficit**; strong sub-crest velocity
  gradient as a separation proxy), and (c) empirical rules (crest-wind/height ratio;
  ~5–7×H downwind extent). Threshold on the combination → "run momentum here." (ADR-0005.)
- **Even cheaper pre-filter:** a purely geometric **Winstral shelter index** (max upwind
  slope within a search distance → sheltered vs exposed per wind direction), computed from
  the DEM with no solver call.

**Pass-2 refinements noted:**
- **Buffer** the crop: upstream fetch for flow to establish + generous downwind margin so
  the recirculation isn't truncated by the outlet boundary.
- **Resolution:** Pass 1 coarse (~30–100 m), Pass 2 fine (~10–30 m, toward ~10^6 cells);
  the IGN 1 m DEM is the shared source. Computational (mesh) resolution drives cost, not
  DEM resolution.
- **Handoff:** Pass 2's homogeneous wind = the wind **read from the Pass-1 field** at
  crest height upstream of the feature, that hour. Pass 1 manufactures Pass 2's BC.

**Two distinct quantities → two distinct views:** a 2D screening map + time slider (Pass
1, triage) and a separate 3D recirculation scene (Pass 2, detail). Don't blend them.
(ADR-0005.)

**Physics axis flagged:** anabatic/katabatic + stability matter a lot in mountains.
Diurnal slope winds + non-neutral stability are available in the **mass** solver → natural
enrichment of **Pass 1**, *after* the skeleton works. Their availability on the
**momentum** solver is **to verify** (open question).

---

## Entry 5 — Scaffolding the project (this commit)

Created the repository skeleton: docs (overview, theory, architecture, ADRs, data
sources, WindNinja integration, this log, roadmap, glossary), support docs
(troubleshooting, environment), AI/third-party prompts, src-layout package with module
stubs and real starter code for `terrain` and `screening` plus the WindNinja wrapper and
OpenFOAM reader contracts, a Pass-1 demo script, and packaging files.

**Next:** stand up the Pass-1 pipeline end to end on a known relief (real DEM, one wind,
mass run, indicator, 2D map). Then close the momentum/stability open question and wire the
Pass-1→Pass-2 click handoff. See `07_roadmap.md`.

---

<!-- TEMPLATE for new entries — copy below the line
## Entry N — <short title>  (YYYY-MM-DD)
**What changed / what I tried.**
**Why.**
**Result / decision.** (link any new ADR)
**Open questions raised.**
-->
