# 01 — Theory & physics

This document records *why* the modelling choices are what they are. It is the
theoretical backbone; the decisions distilled from it live in `03_decisions.md`.

## The phenomenon: leeward separation and rotor

When a stably-stratified or neutral airstream crosses a relief, the flow can **separate**
on the downwind (lee) side. Behind the crest a **recirculation zone** forms: a region of
**reversed, low-momentum, turbulent flow** — the **rotor**. Its extent depends on:

- **wind speed** and **direction** relative to the ridge orientation,
- the **height and steepness** of the relief (especially the lee slope),
- **atmospheric stability** (stable layers can trap and amplify lee waves/rotors),
- **humidity** (latent heat release modifies buoyancy; secondary, later versions).

Rule-of-thumb pilot heuristics put the disturbed lee zone at roughly **5–7× the relief
height** in downwind extent, growing with wind strength and with stability. These
heuristics are crude but valuable as a sanity layer and as a screening pre-filter.

## Why NOT potential flow / "perfect gas" inviscid flow

An early idea was to start simple with potential (inviscid, irrotational) flow. **This
does not work for our problem.** Potential flow has **no boundary layer and never
separates** — by construction it produces **no wake and no rotor**. It would predict
smooth attached flow draping over the terrain everywhere, which is precisely the wrong
answer in the lee. The phenomenon we care about is a *viscous separation* phenomenon.
So potential flow was rejected before any code was written. (ADR-0003.)

## The solver landscape (from simplest to most physical)

1. **Empirical heuristics** — terrain geometry + ratio rules. Free, instant, crude.
   Used as a screening pre-filter and a sanity overlay, never as the answer.
2. **Mass-consistent / diagnostic models** — the flow is forced to follow the terrain
   and conserve mass, *without solving the momentum equations*. Fast (seconds). This is
   WindNinja's native **conservation-of-mass** solver.
3. **RANS CFD (steady-state)** — solves conservation of mass **and momentum** with a
   turbulence closure. Resolves separation and recirculation. This is WindNinja's
   **momentum** solver (NinjaFOAM), built on OpenFOAM's `simpleFoam` with a k-epsilon
   closure, terrain-following hexahedral mesh.
4. **LES / unsteady CFD / mesoscale NWP (WRF etc.)** — most physical, far too expensive
   and specialised for interactive use. Out of scope.

## The pivotal constraint that shapes the whole architecture

The two WindNinja solvers answer **different questions**:

- The **conservation-of-mass** solver, *because of how it represents momentum, cannot
  capture eddies (reversed flow) at all*. In a lee eddy it reports **very low wind
  speed, but never a reversal of direction.** → **It does not, and cannot, show the
  rotor.**
- The **conservation-of-mass-and-momentum** solver **does** capture eddies and lee-side
  recirculation. → **This is the one that answers our question.**

Therefore:

> The fast solver is blind to the exact thing we care about. The solver that sees it is
> the expensive one. We cannot run the expensive one over the whole area every hour.

This is the origin of the **two-pass** design. (ADR-0003.)

## What Pass 1 actually measures (and what it does not)

Because Pass 1 (mass solver) cannot show rotors, we do **not** read its velocity field
as a rotor map. Instead we build a **derived hazard indicator** — an *estimate of the
likelihood and severity of disturbed lee air* — from signals that the mass field *can*
legitimately provide, combined with terrain geometry and heuristics:

1. **Terrain geometry (DEM only, time-independent):** lee-slope steepness *relative to
   the hour's wind direction*; crest/arête/shoulder detection. Separation forms where
   the lee slope is steep relative to the incoming flow.
2. **Mass-field signals:** the **downwind velocity deficit** (a wake leaves a
   low-speed shadow even in the mass solver) and the **strong velocity gradient just
   below the crest** (a proxy for where separation begins).
3. **Empirical rules:** crest-wind / obstacle-height ratio; ~5–7×H downwind extent.

A threshold on the combination → "go run the momentum solver here." This indicator is
the **handoff signal** between passes. See `screening/indicator.py`.

### An even cheaper pre-filter: terrain shelter (Winstral)

Before running even Pass 1, a purely geometric **shelter index** (after Winstral) can be
computed from the DEM alone: the **maximum upwind slope within a search distance**
determines whether a cell is **sheltered or exposed** for a given wind direction. This
pre-screens candidates with no solver call at all. Used to focus where Pass 1 detail
matters. See `terrain/geometry.py`.

## What Pass 2 produces and its limits

Pass 2 (momentum / RANS) yields a 3D field on a small domain from which we extract:

- **streamlines** of the recirculating flow,
- the **reversed-flow volume** (where the velocity component along the mean flow is
  negative) and/or
- a **turbulence-intensity** threshold volume (WindNinja can emit turbulence output);
  turbulence intensity is arguably a *more directly meaningful* "is this dangerous"
  proxy than speed alone.

**Known limitations to keep honest about:**

- It is **steady-state RANS**: it gives a mean recirculation, not the unsteady gusting
  structure a real rotor has. The mean is informative but not the full story.
- Accuracy is **lowest exactly in the recirculation zone** — the tool itself trades
  accuracy for speed there. Treat extents as indicative.
- Stability and diurnal effects: supported on the **mass** solver (diurnal slope winds,
  non-neutral stability). Their availability on the **momentum** solver is **to be
  verified** — flagged as an open question, see `06_dev_log.md` / roadmap. Do not assume.

## Future physics (v2+)

Stability, humidity, diurnal anabatic/katabatic winds materially change mountain flow.
Diurnal + non-neutral stability are available in the mass solver and are the natural
next enrichment of **Pass 1**. Humidity / latent effects are a later research direction.
Sequenced deliberately *after* the end-to-end skeleton works, not before.
