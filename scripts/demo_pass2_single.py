"""Demo: a single Pass-2 momentum run on one feature, then the 3D recirculation view.

Takes a CROPPED DEM window around a feature (already buffered) plus a homogeneous wind
(in practice read from the Pass-1 field upstream of the feature) and:
  1. runs the WindNinja momentum solver,
  2. locates the OpenFOAM case directory,
  3. reads the true 3D field and renders reversed-flow / turbulence volumes.

Needs WindNinja with the momentum solver available. See docs/05 and roadmap M2 (T8-T10).
"""

from __future__ import annotations

from pathlib import Path

import click

from sillage.config import load_config, resolve_output_path
from sillage.flow.windninja import run_momentum
from sillage.viz import volume3d


@click.command()
@click.option("--dem", "dem_path", required=True, type=click.Path(exists=True),
              help="CROPPED + buffered DEM window around the feature (UTM north-up, m).")
@click.option("--wind-dir", "wind_from_deg", required=True, type=float,
              help="Homogeneous wind FROM direction (deg, met. convention).")
@click.option("--wind-speed", "wind_speed_ms", required=True, type=float,
              help="Homogeneous wind speed (m/s).")
@click.option("--mesh-count", default=500_000, show_default=True)
@click.option("--iterations", default=300, show_default=True,
              help="Lee/recirculation regions converge slowest; raise for lee accuracy.")
@click.option("--turbulence", is_flag=True,
              help="Also show the turbulence-intensity volume (busy; off by default).")
@click.option("--save", "save_path", default="",
              help="Render the 3D scene headless to this PNG (no interactive window).")
@click.option("--no-show", is_flag=True, help="Run + read only; skip interactive 3D.")
def main(dem_path, wind_from_deg, wind_speed_ms, mesh_count, iterations, turbulence,
         save_path, no_show):
    cfg = load_config()
    wd = cfg.cache_dir / "pass2_demo"

    click.echo("[1/3] Running WindNinja momentum solver (this can take minutes) ...")
    run = run_momentum(
        cli=cfg.windninja_cli, dem_path=str(Path(dem_path).resolve()),
        working_dir=str(wd), wind_speed_ms=wind_speed_ms, wind_from_deg=wind_from_deg,
        mesh_count=mesh_count, iterations=iterations, turbulence_output=True,
    )
    if run.returncode not in (0, None):
        raise SystemExit(
            f"WindNinja momentum failed (rc={run.returncode}). "
            f"See docs/support/troubleshooting.md.\n{run.stderr[:800]}"
        )

    click.echo(f"[2/3] OpenFOAM case dir: {run.openfoam_case_dir}")
    if run.openfoam_case_dir is None:
        raise SystemExit(
            "Could not locate the OpenFOAM case directory. "
            "Refine flow.windninja.locate_openfoam_case for your install (docs/05)."
        )

    mean_flow_dir = volume3d.mean_flow_vector(wind_from_deg)

    click.echo("[3/3] Reading field + building 3D recirculation scene ...")
    if no_show and not save_path:
        from sillage.flow import openfoam_reader as ofr
        mesh = ofr.read_case(run.openfoam_case_dir)
        along = ofr.along_flow_component(mesh, mean_flow_dir)
        click.echo(f"      cells={mesh.n_cells}, reversed-flow cells={int((along < 0).sum())}")
        return

    if save_path:
        out = resolve_output_path(save_path, cfg)
        volume3d.save_png(str(run.openfoam_case_dir), mean_flow_dir, out,
                          show_reversed_flow=True, show_turbulence=turbulence)
        click.echo(f"      saved 3D snapshot -> {out}")
    if not no_show:
        volume3d.show(str(run.openfoam_case_dir), mean_flow_dir,
                      show_reversed_flow=True, show_turbulence=turbulence)


if __name__ == "__main__":
    main()
