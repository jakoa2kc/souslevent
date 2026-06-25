"""Auto full-resolution Pass-2 orchestrator.

``run_auto`` ties the existing building blocks into one job:

  fine DEM (terrain.acquire)  ->  relief-adaptive partition (auto.partition)
    ->  for each (sub-zone × hour): local crest wind (auto.wind) + momentum solve
        (flow.windninja.run_momentum on a buffered crop)   [bounded concurrency]
    ->  AutoResult: the OpenFOAM case per (zone, hour), for the time-sliderable 3D scene.

Momentum is CPU-bound (ADR-0006) and its temp env can't be redirected (it crashes OpenFOAM —
Entry 38), so the default is **sequential** (``momentum_workers=1``); raise it to overlap solves
where the machine allows. Failures use the same **parallel-then-sequential retry** as the Pass-1
loops. Progress + ETA come from auto.progress. See docs/10_auto_pipeline.md / ADR-0022.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from ..flow.windninja import format_run_failure, run_momentum
from ..terrain.acquire import prepare_dem
from ..terrain.dem import crop_dem, load_dem, write_dem
from ..timing import RunTimings
from .partition import SubZone, partition_zone
from .progress import ProgressTracker
from .wind import local_wind_provider

AUTO_EDGE_BUFFER_M = 700.0  # grow each momentum crop so the lateral BCs sit off the sub-zone


def detect_cores(fallback: int = 14) -> int:
    """Physical CPU cores (psutil) if detectable, else logical (os.cpu_count), else ``fallback``.
    Used as the default concurrency: one momentum solve per core."""
    try:
        import psutil

        n = psutil.cpu_count(logical=False)
        if n:
            return int(n)
    except Exception:
        pass
    import os

    return os.cpu_count() or fallback


@dataclass(frozen=True)
class AutoConfig:
    """Inputs for one automatic full-resolution run."""

    bbox_latlon: tuple[float, float, float, float]  # south, west, north, east
    hours: tuple[int, ...]                          # clock hours to compute over the window
    window_start_iso: str = ""                      # provenance only (the flight day midnight)
    target_res_m: float = 10.0                      # finest topo scale for the momentum DEM
    max_cells: int = 600_000                        # per-sub-zone mesh budget (partition)
    max_relief_m: float = 400.0                     # per-sub-zone relief cap (upstream-wind validity)
    mesh_count: int = 150_000                       # WindNinja momentum mesh (ADR-0008)
    iterations: int = 300
    # Concurrent momentum solves — defaults to the machine's core count (one solve per core).
    momentum_workers: int = field(default_factory=detect_cores)
    dem_source: str = "auto"                        # IGN / world / auto (ADR-0014)
    wind_source: str = "open_meteo"                 # "arome" falls back to Open-Meteo for now


@dataclass(frozen=True)
class CaseResult:
    """One solved (sub-zone, hour): the OpenFOAM case + the wind + the zone bounds to clip to."""

    zone_index: int
    hour: int
    case_dir: str
    wind_speed_ms: float
    wind_from_deg: float
    crs: object
    aoi_bounds: tuple[float, float, float, float]  # xmin, xmax, ymin, ymax (the un-buffered zone)
    elapsed_s: float


@dataclass
class AutoResult:
    dem_path: str
    crs: object
    partition: list[SubZone]
    cases: list[CaseResult] = field(default_factory=list)
    failures: list[tuple[int, int, str]] = field(default_factory=list)  # (zone, hour, error)
    timings_summary: str = ""

    def cases_for_hour(self, hour: int) -> list[CaseResult]:
        return [c for c in self.cases if c.hour == hour]

    @property
    def hours(self) -> list[int]:
        return sorted({c.hour for c in self.cases})


def _expand_bbox(bbox_latlon, buffer_m):
    s, w, n, e = bbox_latlon
    dlat = buffer_m / 111_320.0
    dlon = buffer_m / (111_320.0 * max(0.05, math.cos(math.radians((s + n) / 2.0))))
    return (s - dlat, w - dlon, n + dlat, e + dlon)


def run_auto(
    cfg: AutoConfig,
    *,
    cli: str,
    cache_dir,
    on_progress=None,
    cancel=None,
) -> AutoResult:
    """Run the full-resolution auto pipeline. ``on_progress(percent, message)`` carries the ETA;
    ``cancel()`` aborts between tasks. Returns the per-(zone, hour) cases for the 3D scene."""
    timings = RunTimings()
    cache_dir = Path(cache_dir)
    work_root = cache_dir / "auto"
    work_root.mkdir(parents=True, exist_ok=True)

    s, w, n, e = cfg.bbox_latlon
    out = work_root / f"dem_{s:.3f}_{w:.3f}_{n:.3f}_{e:.3f}_{cfg.target_res_m:.0f}m_{cfg.dem_source}.tif"
    with timings.measure("MNT"):
        if on_progress is not None:
            on_progress(0, "Préparation du MNT fin…")
        dem_path, _used = prepare_dem(
            _expand_bbox(cfg.bbox_latlon, AUTO_EDGE_BUFFER_M), out,
            target_res_m=cfg.target_res_m, source=cfg.dem_source,
            on_progress=lambda p, m: on_progress(0, f"MNT : {m}") if on_progress else None,
            cancel=cancel,
        )
        dem = load_dem(str(dem_path), max_domain_km=200.0)

    with timings.measure("partition"):
        zones = partition_zone(
            dem, target_res_m=cfg.target_res_m, max_cells=cfg.max_cells,
            max_relief_m=cfg.max_relief_m)

    # One wind provider for the window, at the zone-median crest altitude (Open-Meteo doesn't vary
    # spatially below ~11 km; the AROME upgrade resolves it per sub-zone — auto.wind).
    crest = sorted(z.crest_alt_m for z in zones)[len(zones) // 2] if zones else 0.0
    make = local_wind_provider(dem, crest, n_hours=max(cfg.hours) + 1, source=cfg.wind_source)

    tasks = [(zi, h) for zi in range(len(zones)) for h in cfg.hours]
    tracker = ProgressTracker(total=len(tasks))
    cases: list[CaseResult] = []
    nz, nh = len(zones), len(cfg.hours)
    workers = max(1, int(cfg.momentum_workers))
    per_run_threads = max(1, detect_cores() // workers)  # ~one core per concurrent solve
    if on_progress is not None:
        on_progress(0, f"{nz} sous-zones × {nh} h = {len(tasks)} calculs Pass-2 "
                       f"(×{workers} en parallèle, {per_run_threads} thr/solve, "
                       f"maillage ~{cfg.mesh_count:,})")

    def _solve(zi: int, h: int) -> CaseResult:
        if cancel is not None and cancel():
            raise RuntimeError("cancelled")
        zone = zones[zi]
        cx, cy = zone.center
        zlabel = f"zone {zi + 1}/{nz} · {h:02d}h"
        if on_progress is not None:
            on_progress(tracker.percent, f"{zlabel} · récupération du vent amont…")
        spd, drc = make(h)(cx, cy)
        half_w = (zone.bbox[2] - zone.bbox[0]) / 2.0 + AUTO_EDGE_BUFFER_M
        half_h = (zone.bbox[3] - zone.bbox[1]) / 2.0 + AUTO_EDGE_BUFFER_M
        crop = crop_dem(dem, cx, cy, half_w, half_h)
        crop_path = work_root / f"z{zi:02d}_h{h:02d}.tif"
        write_dem(crop, crop_path)
        if on_progress is not None:
            on_progress(tracker.percent,
                        f"{zlabel} · vent {spd:.0f} m/s de {drc:.0f}° · maillage + solveur…")
        t0 = perf_counter()
        run = run_momentum(
            cli=cli, dem_path=str(crop_path),
            working_dir=str(work_root / f"z{zi:02d}_h{h:02d}_run"),
            wind_speed_ms=spd, wind_from_deg=drc,
            mesh_count=cfg.mesh_count, iterations=cfg.iterations,
            num_threads=per_run_threads, cancel=cancel,
            on_progress=(lambda p, m: on_progress(tracker.percent, f"{zlabel} · {m}"))
            if on_progress is not None else None,
        )
        if run.returncode not in (0, None):
            raise RuntimeError(format_run_failure(run, f"auto z{zi} h{h} momentum"))
        if run.openfoam_case_dir is None:
            raise RuntimeError(f"auto z{zi} h{h}: aucun case OpenFOAM localisé")
        return CaseResult(
            zone_index=zi, hour=h, case_dir=str(run.openfoam_case_dir),
            wind_speed_ms=spd, wind_from_deg=drc, crs=dem.crs,
            aoi_bounds=(zone.bbox[0], zone.bbox[2], zone.bbox[1], zone.bbox[3]),
            elapsed_s=perf_counter() - t0,
        )

    def _after(case: CaseResult) -> None:
        cases.append(case)
        tracker.record(case.elapsed_s)
        if on_progress is not None:
            on_progress(tracker.percent, tracker.summary("Pass-2 auto"))

    failed: list[int] = []
    with timings.measure("Pass-2"):
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_solve, zi, h): k for k, (zi, h) in enumerate(tasks)}
            for fut in as_completed(futs):
                try:
                    _after(fut.result())
                except Exception:
                    if cancel is not None and cancel():
                        for f in futs:
                            f.cancel()
                        raise RuntimeError("cancelled")
                    failed.append(futs[fut])  # retry alone once the pool drains

        result = AutoResult(dem_path=str(dem_path), crs=dem.crs, partition=zones)
        for k in sorted(failed):  # sequential fallback (rules out cross-process contention)
            if cancel is not None and cancel():
                raise RuntimeError("cancelled")
            zi, h = tasks[k]
            try:
                _after(_solve(zi, h))
            except Exception as exc:  # a task that fails even alone -> record, keep the rest
                result.failures.append((zi, h, str(exc)[:300]))
                tracker.record(0.0)

    result.cases = sorted(cases, key=lambda c: (c.hour, c.zone_index))
    result.timings_summary = timings.summary()
    return result
