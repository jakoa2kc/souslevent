"""Demo: the Pass-1 screening pipeline, end to end, on one relief.

Wires terrain -> (wind) -> mass solver -> derived hazard indicator -> 2D map. Designed to
run with progressively more of the stack live:

  * geometry-only (no DEM solver, no network):   --wind-dir 270 --wind-speed 12  (uses a
        hard-coded wind; skips WindNinja; indicator from terrain geometry alone)
  * with WindNinja mass run:                      add --run-windninja  (needs WindNinja_cli)
  * with Open-Meteo forecast:                     add --fetch-forecast  (needs network)

This is intentionally CLI-driven (click) so it doubles as the `sillage-pass1` entry point.
See docs/07_roadmap.md (M1) and prompts/coding_agent_brief.md (T7).
"""

from __future__ import annotations

from pathlib import Path

import click

from sillage.config import load_config, resolve_output_path
from sillage.terrain.dem import load_dem
from sillage.screening import indicator as ind


@click.command()
@click.option("--dem", "dem_path", required=True, type=click.Path(exists=True),
              help="Path to a DEM (GeoTIFF). Will be reprojected to UTM north-up, meters.")
@click.option("--wind-dir", "wind_from_deg", default=270.0, show_default=True,
              help="Wind FROM direction (meteorological, deg, 0=N).")
@click.option("--wind-speed", "wind_speed_ms", default=12.0, show_default=True,
              help="Wind speed (m/s) at input height (used if not fetching a forecast).")
@click.option("--resolution", "resolution_m", default=50.0, show_default=True,
              help="Screening computational resolution (m).")
@click.option("--run-windninja", is_flag=True, help="Run the WindNinja mass solver.")
@click.option("--fetch-forecast", is_flag=True,
              help="Fetch crest-height wind from Open-Meteo (overrides --wind-*).")
@click.option("--crest-alt", "crest_alt_m", default=2500.0, show_default=True,
              help="Crest altitude (m) for forecast reduction.")
@click.option("--save", "save_path", default="", help="Save the map PNG to this path.")
def main(dem_path, wind_from_deg, wind_speed_ms, resolution_m, run_windninja,
         fetch_forecast, crest_alt_m, save_path):
    cfg = load_config()

    click.echo(f"[1/4] Loading DEM {dem_path} -> UTM north-up, meters ...")
    dem = load_dem(dem_path, max_domain_km=cfg.max_domain_km)
    ex, ey = dem.extent_km
    click.echo(f"      grid {dem.shape}, res {dem.resolution_m:.1f} m, "
               f"domain {ex:.1f} x {ey:.1f} km, CRS {dem.crs.to_string()}")

    # --- optional forecast ---
    if fetch_forecast:
        click.echo("[2/4] Fetching Open-Meteo crest-height wind ...")
        from sillage.wind.forecast import fetch_open_meteo
        from sillage.wind.profile import crest_height_series
        lon, lat = _center_lonlat(dem)
        profiles = fetch_open_meteo(lat, lon, hours=24)
        series = crest_height_series(profiles, crest_alt_m)
        if series:
            _, wind_speed_ms, wind_from_deg = series[0]
            click.echo(f"      hour 0: {wind_speed_ms:.1f} m/s from {wind_from_deg:.0f} deg")
    else:
        click.echo("[2/4] Using provided wind (no forecast fetch).")

    # --- optional WindNinja mass run for the velocity-deficit term ---
    speed_grid = None
    if run_windninja:
        click.echo("[3/4] Running WindNinja mass solver ...")
        from sillage.flow.windninja import format_run_failure, run_mass
        work_dir = cfg.cache_dir / "pass1_demo"
        run = run_mass(
            cli=cfg.windninja_cli, dem_path=str(Path(dem_path).resolve()),
            working_dir=str(work_dir),
            wind_speed_ms=wind_speed_ms, wind_from_deg=wind_from_deg,
            output_resolution_m=resolution_m,
            tmp_dir=work_dir / "_tmp",
        )
        if run.returncode not in (0, None):
            click.echo(format_run_failure(run, "WindNinja mass"))
        else:
            speed_grid = _load_speed_grid(run.output_paths, dem.shape)
            click.echo(f"      outputs: {[p.name for p in run.output_paths]}")
    else:
        click.echo("[3/4] Skipping WindNinja (geometry-only indicator).")

    click.echo("[4/4] Computing hazard indicator + candidates ...")
    indicator = ind.hazard_indicator(dem, wind_from_deg, speed_grid=speed_grid)
    candidates = ind.find_candidates(dem, indicator, n=10)
    click.echo("      top candidates (x, y, score):")
    for c in candidates[:10]:
        click.echo(f"        ({c.x:.0f}, {c.y:.0f})  score={c.score:.2f}")

    # render
    from sillage.viz.map2d import show_static
    fig = show_static(dem, indicator,
                      title=f"Sillage Pass-1 — wind {wind_speed_ms:.0f} m/s "
                            f"from {wind_from_deg:.0f}°")
    if save_path:
        out = resolve_output_path(save_path, cfg)
        out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out, dpi=130, bbox_inches="tight")
        click.echo(f"Saved map -> {out}")
    else:
        import matplotlib.pyplot as plt
        plt.show()


def _center_lonlat(dem):
    from rasterio.warp import transform as warp_xy
    from rasterio.crs import CRS
    left, bottom, right, top = dem.bounds
    cx, cy = (left + right) / 2, (bottom + top) / 2
    lon, lat = warp_xy(dem.crs, CRS.from_epsg(4326), [cx], [cy])
    return lon[0], lat[0]


def _load_speed_grid(asc_paths, shape):
    """Build a speed grid from WindNinja ASCII u,v outputs (best-effort)."""
    import rasterio
    us = [p for p in asc_paths if "_vel" in p.name or "_spd" in p.name]
    if us:
        with rasterio.open(us[0]) as s:
            arr = s.read(1).astype("float64")
        from sillage.screening.indicator import _resample_to
        return _resample_to(arr, shape) if arr.shape != shape else arr
    return None


if __name__ == "__main__":
    main()


