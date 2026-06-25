"""Champsaur Pass-1 HOURLY loop -> time-sliderable hazard map (+ animated GIF).

Closes roadmap M1: instead of a single fixed wind, run the WindNinja mass solver once per
hour over a flight window and assemble an hourly stack with an interactive slider and a
saved GIF. Still CANDIDATE zones (likelihood of disturbed lee air), NOT rotors.

Wind source:
  * --source forecast : Open-Meteo crest-height wind per hour (needs network).
  * --source synthetic: deterministic NW sweep, for offline plumbing/demo (clearly
                        labelled; not a real forecast).

Per-hour WindNinja outputs are cached under <cache>/champsaur/hourly/h{NN}_... so reruns
are cheap; pass --force-run to recompute.
"""

from __future__ import annotations

import click
import numpy as np

from sillage.config import load_config, resolve_cache_path, resolve_output_path
from sillage.screening import indicator as ind
from sillage.screening.pass1 import hourly_indicator_stack, hourly_worker_plan
from sillage.terrain.dem import load_dem
from sillage.timing import RunTimings

DEFAULT_DEM = "cache/champsaur/ign/champsaur_rgealti_50m_prepared_utm.tif"


@click.command()
@click.option("--dem", "dem_path", default=DEFAULT_DEM, show_default=True,
              help="Prepared UTM DEM for WindNinja/Sillage.")
@click.option("--source", type=click.Choice(["forecast", "synthetic"]), default="synthetic",
              show_default=True, help="Hourly wind source.")
@click.option("--hours", default=6, show_default=True, help="Number of hours in the window.")
@click.option("--crest-alt", "crest_alt_m", default=2500.0, show_default=True,
              help="Crest altitude (m) for forecast reduction.")
@click.option("--resolution", "resolution_m", default=100.0, show_default=True,
              help="WindNinja mass mesh / ASCII output resolution (m).")
@click.option("--vegetation", default="grass", show_default=True,
              type=click.Choice(["grass", "brush", "trees"], case_sensitive=False))
@click.option("--edge-buffer", "edge_buffer_m", default=1500.0, show_default=True,
              help="Mask this border width (m) to reduce DEM edge artifacts.")
@click.option("--force-run", is_flag=True, help="Recompute every hour even if cached.")
@click.option("--workers", default=0, show_default=True,
              help="Concurrent hourly WindNinja runs; 0 = auto conservative.")
@click.option("--no-gif", is_flag=True, help="Skip saving the animated GIF.")
@click.option("--show", is_flag=True, help="Open the interactive slider window.")
@click.option("--save", "save_path", default="outputs/champsaur/champsaur_pass1_hourly.gif",
              show_default=True, help="Animated GIF output path.")
def main(dem_path, source, hours, crest_alt_m, resolution_m, vegetation, edge_buffer_m,
         force_run, workers, no_gif, show, save_path):
    """Run the hourly Pass-1 loop over Champsaur and build a time-sliderable map."""
    cfg = load_config()
    dem_file = resolve_cache_path(dem_path, cfg)
    if not dem_file.exists():
        raise SystemExit(f"DEM not found: {dem_file}")

    click.echo(f"[1/4] Loading DEM {dem_file}")
    dem = load_dem(str(dem_file), max_domain_km=cfg.max_domain_km)
    ex, ey = dem.extent_km
    click.echo(f"      grid {dem.shape}, res {dem.resolution_m:.1f} m, domain {ex:.1f} x {ey:.1f} km")

    click.echo(f"[2/4] Building hourly wind series (source={source}, hours={hours}) ...")
    series = _wind_series(dem, source, hours, crest_alt_m)
    if not series:
        raise SystemExit("Empty wind series (forecast returned no crest-height samples?).")
    click.echo(f"      {len(series)} hour(s): "
               + ", ".join(f"{s:.0f}m/s@{d:.0f}deg" for _, s, d in series))

    max_workers = None if workers <= 0 else workers
    worker_count, per_run_threads = hourly_worker_plan(len(series), max_workers=max_workers)
    click.echo("[3/4] Per-hour WindNinja mass + indicator "
               f"(parallel x{worker_count}, {per_run_threads} threads/run) ...")
    timings = RunTimings()
    display_series = [(f"{t}  —  {spd:.0f} m/s @ {drc:.0f}°", spd, drc)
                      for t, spd, drc in series]

    def work_dir_for(idx, _label, spd, drc):
        return cfg.cache_dir / "champsaur" / "hourly" / (
            f"h{idx:02d}_{drc:.0f}_{spd:.0f}_{resolution_m:.0f}m"
        )

    last_bucket = {"value": -1}

    def progress(pct, msg):
        bucket = int(pct) // 10
        if bucket != last_bucket["value"] or pct >= 100:
            last_bucket["value"] = bucket
            click.echo(f"      {int(pct):3d}% {msg}")

    with timings.measure("Pass-1 hourly"):
        results = hourly_indicator_stack(
            dem=dem, cli=cfg.windninja_cli, dem_path=str(dem_file), series=display_series,
            work_dir_for=work_dir_for, resolution_m=resolution_m, vegetation=vegetation,
            edge_buffer_m=edge_buffer_m, force_run=force_run, max_workers=max_workers,
            on_progress=progress,
        )
    stack: list[np.ndarray] = []
    labels: list[str] = []
    for idx, result in enumerate(results):
        hazard = result.hazard
        stack.append(hazard)
        labels.append(result.label)
        top = ind.find_candidates(dem, hazard, n=1)
        top_txt = (f"top ({top[0].x:.0f}, {top[0].y:.0f}) score={top[0].score:.2f}"
                   if top else "no candidate")
        _label, spd, drc = display_series[idx]
        click.echo(f"      h{idx:02d} {spd:.0f}m/s@{drc:.0f}deg"
                   f" ({result.elapsed_s:.1f}s) -> {top_txt}")
    click.echo(f"      timings: {timings.summary()}")

    click.echo("[4/4] Rendering timeline ...")
    if not no_gif:
        from sillage.viz.map2d import save_timeline_gif
        out = resolve_output_path(save_path, cfg)
        save_timeline_gif(dem, stack, labels, out,
                          title=f"Sillage Pass-1 Champsaur — hourly ({source})", fps=2)
        click.echo(f"      saved GIF {out}")
    if show:
        import matplotlib.pyplot as plt
        from sillage.viz.map2d import show_timeline
        show_timeline(dem, stack, labels,
                      title=f"Sillage Pass-1 Champsaur — hourly ({source})")
        plt.show()


def _wind_series(dem, source, hours, crest_alt_m):
    """Return a list of (label, speed_ms, from_deg), one per hour."""
    if source == "synthetic":
        from sillage.screening.pass1 import synthetic_series
        return synthetic_series(hours)
    from sillage.wind.forecast import fetch_open_meteo
    from sillage.wind.profile import crest_height_series
    lon, lat = _center_lonlat(dem)
    profiles = fetch_open_meteo(lat, lon, hours=hours)
    return crest_height_series(profiles, crest_alt_m)[:hours]


def _center_lonlat(dem):
    from rasterio.crs import CRS
    from rasterio.warp import transform as warp_xy

    left, bottom, right, top = dem.bounds
    cx, cy = (left + right) / 2.0, (bottom + top) / 2.0
    lon, lat = warp_xy(dem.crs, CRS.from_epsg(4326), [cx], [cy])
    return lon[0], lat[0]


if __name__ == "__main__":
    main()
