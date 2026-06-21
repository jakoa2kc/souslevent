"""Sillage — leeward turbulence / rotor mapping for mountain paragliding.

Two-pass design (see CLAUDE.md and docs/02_architecture.md):

* Pass 1 — WindNinja *mass* solver over the whole domain, hour by hour, driven by the
  spatially-varying forecast. Produces a DERIVED HAZARD INDICATOR (candidates), NOT a
  rotor map: the mass solver physically cannot represent reversed flow.
* Pass 2 — WindNinja *momentum* solver (OpenFOAM RANS) on a small feature with a single
  homogeneous wind. Produces the TRUE 3D recirculation. Read the OpenFOAM case directory
  directly (not the VTK export).

Conventions (project-wide):
  - lengths in meters, speeds in m/s (convert only at the edges)
  - wind direction = meteorological convention (from-direction, degrees, 0 deg = North)
"""

__version__ = "0.0.1"
