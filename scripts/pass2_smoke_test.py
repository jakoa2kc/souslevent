"""Pass-2 momentum SMOKE TEST: can THIS WindNinja build run the momentum solver here?

Crops a small window around a Pass-1 candidate, runs a deliberately tiny momentum solve,
and prints a verdict:
  * OK        : momentum ran and an OpenFOAM case directory was produced -> M2 is unblocked
                natively on this machine.
  * NO-FOAM   : the build/binary cannot run NinjaFOAM/OpenFOAM here -> use the Docker route.
  * FLAG/OTHER: ran but failed for another reason (likely a CLI flag to fix in
                flow.windninja for this version).

Does NOT render 3D. It only answers the Windows-vs-Docker question (docs/05, roadmap M2).
"""

from __future__ import annotations

import click

from sillage.config import load_config, resolve_cache_path
from sillage.flow.windninja import run_momentum
from sillage.terrain.dem import crop_dem, load_dem, write_dem

DEFAULT_DEM = "cache/champsaur/ign/champsaur_rgealti_50m_prepared_utm.tif"
# Top candidate from the 50 m mass run (docs/09): around here.
DEFAULT_X, DEFAULT_Y = 269829.0, 4958344.0

_FOAM_HINTS = ("ninjafoam", "openfoam", "simplefoam", "foam", "mpiexec", "blockmesh",
               "not available", "not supported", "could not", "no such file")


@click.command()
@click.option("--dem", "dem_path", default=DEFAULT_DEM, show_default=True,
              help="Prepared UTM DEM to crop the feature window from.")
@click.option("--x", "center_x", default=DEFAULT_X, show_default=True, help="Window center easting (m).")
@click.option("--y", "center_y", default=DEFAULT_Y, show_default=True, help="Window center northing (m).")
@click.option("--half-width", "half_m", default=2500.0, show_default=True,
              help="Half window size (m); 2500 -> ~5x5 km feature window.")
@click.option("--wind-dir", "wind_from_deg", default=320.0, show_default=True)
@click.option("--wind-speed", "wind_speed_ms", default=8.0, show_default=True)
@click.option("--mesh-count", default=25_000, show_default=True, help="Tiny mesh for a fast smoke test.")
@click.option("--iterations", default=100, show_default=True)
def main(dem_path, center_x, center_y, half_m, wind_from_deg, wind_speed_ms, mesh_count, iterations):
    cfg = load_config()
    dem_file = resolve_cache_path(dem_path, cfg)
    if not dem_file.exists():
        raise SystemExit(f"DEM not found: {dem_file}")

    click.echo(f"[1/3] Cropping ~{2 * half_m / 1000:.1f} km window around ({center_x:.0f}, {center_y:.0f}) ...")
    dem = load_dem(str(dem_file), max_domain_km=cfg.max_domain_km)
    crop = crop_dem(dem, center_x, center_y, half_m)
    crop_dir = cfg.cache_dir / "champsaur" / "pass2"
    crop_path = crop_dir / f"champsaur_crop_{center_x:.0f}_{center_y:.0f}_{2 * half_m:.0f}m.tif"
    write_dem(crop, crop_path)
    click.echo(f"      crop grid {crop.shape}, res {crop.resolution_m:.1f} m -> {crop_path}")

    work = cfg.cache_dir / "champsaur" / "pass2" / "smoke_run"
    click.echo(f"[2/3] Running momentum solver (mesh={mesh_count}, iters={iterations}) — may take a while ...")
    run = run_momentum(
        cli=cfg.windninja_cli, dem_path=str(crop_path), working_dir=str(work),
        wind_speed_ms=wind_speed_ms, wind_from_deg=wind_from_deg,
        mesh_count=mesh_count, iterations=iterations, turbulence_output=True,
    )

    click.echo("[3/3] Verdict")
    click.echo(f"      return code      : {run.returncode}")
    click.echo(f"      openfoam case dir : {run.openfoam_case_dir}")
    blob = f"{run.stdout}\n{run.stderr}".lower()
    if run.returncode == 0 and run.openfoam_case_dir is not None:
        click.echo("      => OK: momentum ran natively on this Windows build. M2 unblocked.")
    elif run.returncode == 0:
        click.echo("      => RAN but no OpenFOAM case located. Refine locate_openfoam_case "
                   "(flow.windninja) and inspect the working dir below.")
    elif any(h in blob for h in _FOAM_HINTS):
        click.echo("      => NO-FOAM: this build cannot run NinjaFOAM/OpenFOAM here. "
                   "Use the Docker route for Pass 2 (docs/05).")
    else:
        click.echo("      => OTHER failure (likely a CLI flag for this WindNinja version). "
                   "Check the STDERR tail and adjust flow.windninja.")
    click.echo(f"\n      working dir       : {work}")
    if run.stdout:
        click.echo("      --- STDOUT tail ---\n" + _tail(run.stdout))
    if run.stderr:
        click.echo("      --- STDERR tail ---\n" + _tail(run.stderr))


def _tail(s: str, n: int = 1500) -> str:
    return s[-n:]


if __name__ == "__main__":
    main()
