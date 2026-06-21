# 00 — Project overview

## The problem (from the pilot)

Flying paragliders in mountainous terrain, one of the recurring safety problems is
**placement relative to the leeward (downwind) zone of the relief**. The air behind a
ridge or summit, when the wind blows over it, becomes turbulent and can contain
**rotors** — zones of recirculating, reversed, chaotic flow that are dangerous to a
wing. The difficulty is *spatial imagination*: it is hard to picture the **size and
shape** of these zones as a function of **wind direction and strength**, which makes
the trade-off between **flying efficiency** and **safety** hard to judge in the field.

## The goal

A desktop application that **computes and represents in 3D** the leeward (and windward)
zones over a chosen flying area, from:

- fine **cartography** of the flying area (a high-resolution DEM), and
- fine **wind forecasts** by **altitude and position**, **hour by hour**, across the
  planned flight window.

So the pilot can, before flying, see where the dangerous air will be and when.

## How we actually deliver it (the two-pass idea)

A full, accurate CFD simulation of the whole flying area at fine resolution is too
expensive to run interactively. Instead Sillage uses an **adaptive, multi-resolution**
approach that mirrors how these problems are handled in practice:

1. **Pass 1 — coarse screening over the whole domain, every hour.** A fast solver
   driven by the spatially-varying wind forecast. It does *not* draw rotors (it
   physically cannot — see `01_theory_and_physics.md`); instead it produces a **derived
   hazard indicator** that flags **candidate** zones: where, and when, trouble is
   likely. This is the triage layer, and it is already useful on its own.

2. **Pass 2 — precise local detail on demand.** At a candidate feature and hour
   identified in Pass 1 (a rock arête, a summit, a shoulder, a combe), a true CFD run
   on a small sub-domain with a homogeneous upstream wind. This produces the **real 3D
   recirculating volume** the pilot wants to see.

The user balances the slider over time in Pass 1, a hotspot lights up, they click it,
and that queues a Pass-2 run for the detailed 3D view.

## Scope (what Sillage is)

- A **decision-support** and **visualization** tool for pre-flight analysis.
- A **desktop** application (developed in VSCode), targeting a workstation with a
  capable GPU for 3D rendering.
- Built around **existing, validated solvers** (WindNinja) rather than re-deriving CFD.

## Non-goals (what Sillage is *not*, at least initially)

- **Not** a real-time / in-flight instrument. It is a pre-flight planning tool.
- **Not** a replacement for pilot judgement, training, or official weather briefings.
  It is an aid to imagination, explicitly labelled as approximate.
- **Not** a from-scratch CFD code. We wrap a proven solver. (See ADR-0002.)
- **Not** a time-stepping atmospheric model. Each hour is an independent steady-state
  snapshot.

## Intended audience for the codebase & docs

The pilot-developer, plus **third parties and AI tools** brought in later for
extension, support, or debugging. Hence the heavy emphasis on traceable reasoning
(`06_dev_log.md`, `03_decisions.md`) and support material (`docs/support/`).

## Safety framing (must persist into the UI)

Every output is an **approximation**. Pass-1 maps show *likelihood of disturbed air*,
not certified rotor boundaries. Pass-2 volumes are steady-state RANS results with known
limitations in exactly the recirculation regions of interest. The UI must communicate
uncertainty rather than imply precision. This is a flying-safety tool; overconfidence is
the failure mode to design against.
