"""Auto full-resolution Pass-2 orchestrator.

``run_auto`` ties the existing building blocks into one job:

  fine DEM (terrain.acquire)  ->  Pass-1 screening  -> feature domains (auto.partition)
    ->  for each (feature × hour): local crest wind (auto.wind) + momentum solve
        (flow.windninja.run_momentum on a buffered crop)   [bounded concurrency]
    ->  AutoResult: full case per (feature, hour), for the time-sliderable 3D scene.

Momentum is CPU-bound (ADR-0006) and its temp env can't be redirected (it crashes OpenFOAM —
Entry 38), so the default asks for all detected cores; the exact concurrent solves are still capped
by the number of detected feature/hour tasks.
Failures use the same **parallel-then-sequential retry** as the Pass-1 loops. Progress + ETA come
from auto.progress. See docs/10_auto_pipeline.md / ADR-0022.
"""

from __future__ import annotations

import math
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import perf_counter

from ..flow.windninja import format_run_failure, run_momentum
from ..terrain.acquire import prepare_dem
from ..terrain.dem import crop_dem, load_dem, write_dem
from ..timing import RunTimings
import numpy as np

from .partition import SubZone, corridor_mask, corridor_tiles, feature_domains
from .progress import ProgressTracker
from .wind import local_wind_provider

AUTO_EDGE_BUFFER_M = 1200.0  # grow each momentum crop so the outlet/lateral BCs sit WELL off the
# feature's lee — the recirculation must die out before the boundary, else it "climbs" the edge
# (the inlet/outlet BC clamps reverse flow at the boundary; that artifact lives in this buffer ring
# and is clipped out of the displayed rotor — see auto.scene + viz._clip_domain_boundary, ADR-0021).
MIN_FREE_GB = 3.0  # last-resort abort if the disk is already dangerously low
DEFAULT_MAX_FEATURES = 12


def _free_gb(path) -> float:
    """Free space (GB) on the volume holding ``path``; +inf if it can't be read (never blocks)."""
    try:
        return shutil.disk_usage(str(path)).free / (1024.0 ** 3)
    except Exception:
        return float("inf")


def _safe_rmtree(path) -> None:
    try:
        shutil.rmtree(str(path), ignore_errors=True)
    except Exception:
        pass


def _safe_unlink(path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def _clean_stale(work_root: Path) -> None:
    """Remove the previous auto run's bulky leftovers (OpenFOAM cases, run dirs, crops, rotor
    meshes) so they don't pile up across runs — the #1 cause of the cache ballooning. Keeps the
    reusable fine DEM (``dem_*.tif``) and the ``screening/`` Pass-1 cache."""
    for d in work_root.glob("NINJAFOAM_*"):
        _safe_rmtree(d)
    for d in work_root.glob("z*_run"):
        _safe_rmtree(d)
    for f in work_root.glob("z*.tif"):
        _safe_unlink(f)
    for f in work_root.glob("z*_rotor.vtu"):
        _safe_unlink(f)
    for f in work_root.glob("z*_turb.vtu"):
        _safe_unlink(f)


def cleanup_auto_artifacts(cache_dir) -> None:
    """Remove transient auto-run artifacts from ``<cache_dir>/auto``.

    This is the normal disk policy for the UI: keep full OpenFOAM cases while the program is open
    so rendering/debugging stays simple, then clean them on close (and at the next run start).
    Reusable DEMs and the keyed Pass-1 screening cache are kept.
    """
    _clean_stale(Path(cache_dir) / "auto")


def _screening_work_dir(
    work_root: Path,
    dem_path: str | Path,
    wind_speed_ms: float,
    wind_from_deg: float,
    resolution_m: float,
) -> Path:
    """Stable Pass-1 screening cache key for the auto run.

    The auto pipeline may keep ``screening/`` across runs, so this MUST include the prepared DEM
    identity and the representative wind. Otherwise a new route/bbox can silently reuse a stale
    ``*_vel.asc`` from another area and place Pass-2 domains in the wrong locations.
    """
    return (
        Path(work_root)
        / "screening"
        / Path(dem_path).stem
        / f"{wind_from_deg:.0f}_{wind_speed_ms:.0f}_{resolution_m:.0f}m"
    )


def _compact_case(case: "CaseResult", work_root: Path) -> "CaseResult":
    """Persist a solved case's small volumes (rotor + turbulence) and **delete the bulky OpenFOAM
    case**.

    Runs in the main thread (serialized — VTK reads aren't thread-safe) as each solve completes, so
    the disk only ever holds the few cases currently being solved. On a read failure it returns the
    case unchanged (keeps ``case_dir``) — never lose a result to a cleanup hiccup."""
    from .scene import extract_volume

    stem = f"z{case.zone_index:02d}_h{case.hour:02d}"
    paths = {"rotor": "", "turbulence": ""}
    read_ok = False
    for metric, suffix in (("rotor", "rotor"), ("turbulence", "turb")):
        try:
            vol = extract_volume(case.case_dir, case.wind_from_deg, case.aoi_bounds,
                                 metric=metric, ref_speed_ms=case.wind_speed_ms)
            read_ok = True  # the case was readable (turbulence may still be None if no TKE)
        except Exception:
            vol = None
        if vol is not None and getattr(vol, "n_cells", 0):
            p = work_root / f"{stem}_{suffix}.vtu"
            try:
                vol.save(str(p))
                paths[metric] = str(p)
            except Exception:
                pass
    if not read_ok:
        return case  # couldn't read the case at all -> keep it for a later attempt
    _safe_rmtree(case.case_dir)              # the heavy NINJAFOAM_* OpenFOAM case
    _safe_rmtree(work_root / f"{stem}_run")  # WindNinja run dir (kmz/sampled)
    _safe_unlink(work_root / f"{stem}.tif")  # the crop DEM
    return replace(case, case_dir="", rotor_path=paths["rotor"], turb_path=paths["turbulence"])


def bbox_from_route(route_latlon, margin_km: float):
    """(south, west, north, east) bounding box of a flight route grown by ``margin_km`` —
    the DEM/screening extent for a route-based run."""
    if not route_latlon:
        raise ValueError("route vide")
    lats = [p[0] for p in route_latlon]
    lons = [p[1] for p in route_latlon]
    s, n = min(lats), max(lats)
    w, e = min(lons), max(lons)
    dlat = margin_km / 111.0
    dlon = margin_km / (111.0 * max(0.05, math.cos(math.radians((s + n) / 2.0))))
    return (s - dlat, w - dlon, n + dlat, e + dlon)


def detect_cores(fallback: int = 14) -> int:
    """Physical CPU cores (psutil) if detectable, else logical (os.cpu_count), else ``fallback``.
    Used to size the worker slider and per-run thread caps."""
    try:
        import psutil

        n = psutil.cpu_count(logical=False)
        if n:
            return int(n)
    except Exception:
        pass
    import os

    return os.cpu_count() or fallback


def default_momentum_workers() -> int:
    """Default concurrent-solve request: all detected physical cores."""
    return max(1, detect_cores())


@dataclass(frozen=True)
class MomentumParallelPlan:
    """Integer-division CPU plan for parallel NinjaFOAM solves."""

    requested_workers: int
    workers: int
    threads_per_worker: int
    cores: int
    used_cores: int
    idle_cores: int
    perfect_workers: tuple[int, ...]


def momentum_parallel_plan(
    requested_workers: int,
    *,
    cores: int | None = None,
    task_count: int | None = None,
) -> MomentumParallelPlan:
    """Plan concurrent momentum solves using integer core division.

    WindNinja gets one ``--num_threads`` value per solve. If ``workers`` does not divide the CPU
    count, ``cores // workers`` leaves a few cores idle; surfacing that makes the UI slider honest.
    """
    total_cores = max(1, int(cores if cores is not None else detect_cores()))
    requested = max(1, int(requested_workers))
    workers = min(requested, total_cores)
    if task_count is not None:
        workers = min(workers, max(1, int(task_count)))
    threads = max(1, total_cores // workers)
    used = workers * threads
    return MomentumParallelPlan(
        requested_workers=requested,
        workers=workers,
        threads_per_worker=threads,
        cores=total_cores,
        used_cores=used,
        idle_cores=max(0, total_cores - used),
        perfect_workers=tuple(w for w in range(1, total_cores + 1) if total_cores % w == 0),
    )


@dataclass(frozen=True)
class AutoConfig:
    """Inputs for one automatic full-resolution run."""

    bbox_latlon: tuple[float, float, float, float]  # south, west, north, east (DEM/screen extent)
    hours: tuple[int, ...]                          # clock hours to compute over the window
    route_latlon: tuple = ()                        # flattened flight route [(lat, lon), ...]
    route_segments: tuple = ()                       # disjoint segments [[(lat, lon), ...], ...];
    # the gaps between segments (valley crossings) are NOT paved/screened. Empty ⇒ route_latlon
    # is treated as one segment.
    corridor_margin_km: float = 2.0                 # restrict features to this band around the route
    window_start_iso: str = ""                      # provenance only (the flight day midnight)
    target_res_m: float = 10.0                      # finest topo scale for the momentum DEM
    max_features: int = DEFAULT_MAX_FEATURES        # momentum domains placed on the top-N features
    feature_separation_m: float = 1200.0            # min spacing between feature domains
    # "features" = Pass-1 screening then one domain per candidate relief; "corridor" = blind paving
    # of the whole route (no Pass-1), domains every tile_step_m of half-size tile_half_m.
    domain_mode: str = "features"
    tile_step_m: float = 1500.0                     # corridor mode: spacing between sectors
    tile_half_m: float = 0.0                        # corridor mode: 0 ⇒ derive from corridor margin
    mesh_count: int = 300_000                       # WindNinja momentum mesh (ADR-0008) — finer now
    # that domains are tighter; raise for precision after live benchmarks
    iterations: int = 300
    # Concurrent momentum solves requested; the effective workers are capped by feature × hour tasks.
    momentum_workers: int = field(default_factory=default_momentum_workers)
    # Optional low-disk mode. Normal UI runs keep full cases until the window closes, then clean.
    compact_cases_during_run: bool = False
    dem_source: str = "auto"                        # IGN / world / auto (ADR-0014)
    wind_source: str = "open_meteo"                 # "arome" falls back to Open-Meteo for now


@dataclass(frozen=True)
class CaseResult:
    """One solved (feature, hour): the OpenFOAM case + the wind + the zone bounds to clip to."""

    zone_index: int
    hour: int
    case_dir: str  # OpenFOAM case (emptied only if compact_cases_during_run is enabled)
    wind_speed_ms: float
    wind_from_deg: float
    crs: object
    aoi_bounds: tuple[float, float, float, float]  # xmin, xmax, ymin, ymax (the un-buffered zone)
    elapsed_s: float
    rotor_path: str = ""  # persisted reversed-flow volume (.vtu) — compaction / save
    turb_path: str = ""   # persisted turbulence volume (.vtu) — compaction / save


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
    if not cfg.hours:
        return AutoResult(dem_path="", crs=None, partition=[],
                          timings_summary="aucune heure sélectionnée")

    timings = RunTimings()
    cache_dir = Path(cache_dir)
    work_root = cache_dir / "auto"
    work_root.mkdir(parents=True, exist_ok=True)
    cleanup_auto_artifacts(cache_dir)  # drop previous session/run artifacts before starting

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

    # Local wind (AROME 1.5 km per point) for the window, at the zone crest altitude.
    crest = float(np.nanpercentile(dem.elevation, 80))
    make = local_wind_provider(dem, crest, n_hours=max(cfg.hours) + 1, source=cfg.wind_source)
    left, bottom, right, top = dem.bounds
    cx0, cy0 = (left + right) / 2.0, (bottom + top) / 2.0

    # The route as disjoint segments (gaps between them are never paved/screened).
    segments_ll = list(cfg.route_segments) or ([list(cfg.route_latlon)] if cfg.route_latlon else [])

    def _seg_xy(seg):
        from rasterio.crs import CRS
        from rasterio.warp import transform as warp_xy

        lons = [p[1] for p in seg]
        lats = [p[0] for p in seg]
        xs, ys = warp_xy(CRS.from_epsg(4326), dem.crs, lons, lats)
        return list(zip(xs, ys))

    if cfg.domain_mode == "corridor" and segments_ll:
        # BLIND PAVING (ADR-0029): no Pass-1 — tile each route segment with momentum domains.
        margin_m = cfg.corridor_margin_km * 1000.0
        half_m = cfg.tile_half_m if cfg.tile_half_m > 0 else max(margin_m, 900.0)
        step_m = max(300.0, min(cfg.tile_step_m, 2.0 * half_m))  # overlap, no gaps along a segment
        with timings.measure("features"):
            if on_progress is not None:
                on_progress(0, "Pavage aveugle des secteurs le long du parcours (sans criblage)…")
            zones = []
            for seg in segments_ll:
                if len(seg) >= 1:
                    zones += corridor_tiles(dem, _seg_xy(seg), step_m=step_m, half_m=half_m,
                                            target_res_m=cfg.target_res_m)
            if on_progress is not None:
                on_progress(0, f"Pavage : {len(zones)} secteurs sur {len(segments_ll)} segment(s) "
                               f"(pas {step_m:.0f} m, demi-largeur {half_m:.0f} m, "
                               f"topo {cfg.target_res_m:.0f} m)")
    else:
        # FEATURE-BASED domains (ADR-0023): screen the WHOLE zone once (continuous Pass-1 mass) to
        # find the relief features, then place ONE momentum domain per feature — no grid, no
        # internal seams to reconcile (why tiled momentum showed flow "climbing" at every joint).
        with timings.measure("criblage"):
            if on_progress is not None:
                on_progress(0, "Criblage Pass-1 sur toute la zone (détection des reliefs)…")
            from ..screening.pass1 import hourly_indicator

            try:
                rep_spd, rep_dir = make(cfg.hours[0])(cx0, cy0)  # representative screening wind
            except Exception:
                rep_spd, rep_dir = 8.0, 270.0  # geometry-driven screening if the forecast is down
            if on_progress is not None:
                on_progress(0, f"Criblage : vent {rep_spd * 3.6:.0f} km/h de {rep_dir:.0f}°")
            screen_work = _screening_work_dir(work_root, dem_path, rep_spd, rep_dir, 150.0)
            hazard, _vel = hourly_indicator(
                dem=dem, cli=cli, dem_path=str(dem_path), work_dir=screen_work,
                wind_speed_ms=rep_spd, wind_from_deg=rep_dir, resolution_m=150.0,
                edge_buffer_m=300.0, force_run=False, cancel=cancel,
                on_progress=lambda p, m: on_progress(0, f"Criblage : {m}") if on_progress else None)

        if segments_ll:  # keep only features within the corridor around each route segment
            mask = np.zeros(dem.shape, dtype=bool)
            for seg in segments_ll:
                if len(seg) >= 1:
                    mask |= corridor_mask(dem, _seg_xy(seg), cfg.corridor_margin_km * 1000.0)
            hazard = np.where(mask, hazard, 0.0)
            if on_progress is not None:
                on_progress(0, f"Corridor : marge {cfg.corridor_margin_km:.1f} km autour du parcours")

        with timings.measure("features"):
            # Finer corridor ⇒ finer-grained features along the route.
            sep = cfg.feature_separation_m
            if cfg.route_latlon:
                sep = max(800.0, min(sep, cfg.corridor_margin_km * 1000.0))
            zones = feature_domains(
                dem, hazard, max_features=cfg.max_features,
                min_separation_m=sep, target_res_m=cfg.target_res_m)
            if on_progress is not None:
                on_progress(0, f"{len(zones)} feature(s) détectée(s) (espacement ≥ {sep:.0f} m)")

    if not zones:
        if on_progress is not None:
            on_progress(100, "Aucun relief marquant détecté dans la zone.")
        return AutoResult(dem_path=str(dem_path), crs=dem.crs, partition=[],
                          timings_summary=timings.summary())

    tasks = [(zi, h) for zi in range(len(zones)) for h in cfg.hours]
    nz, nh = len(zones), len(cfg.hours)
    plan = momentum_parallel_plan(cfg.momentum_workers, task_count=len(tasks))
    workers = plan.workers
    per_run_threads = plan.threads_per_worker
    tracker = ProgressTracker(total=len(tasks), workers=workers)  # parallelism-aware ETA
    cases: list[CaseResult] = []
    if on_progress is not None:
        on_progress(0, f"{nz} features × {nh} h = {len(tasks)} calculs Pass-2 "
                       f"(×{workers} en parallèle, {per_run_threads} thr/solve, "
                       f"{plan.used_cores}/{plan.cores} cœurs utilisés, "
                       f"maillage ~{cfg.mesh_count:,})")

    disk_abort = {"hit": False, "msg": ""}

    def _solve(zi: int, h: int) -> CaseResult:
        def should_cancel() -> bool:
            return disk_abort["hit"] or (cancel is not None and cancel())

        if should_cancel():
            raise RuntimeError("cancelled")
        free = _free_gb(work_root)
        if free < MIN_FREE_GB:  # last-resort protection, not the primary cleanup strategy
            disk_abort["hit"] = True
            disk_abort["msg"] = (f"Disque presque plein ({free:.1f} Go libres < "
                                 f"{MIN_FREE_GB:.0f} Go) — calcul stoppé pour protéger le disque.")
            raise RuntimeError(disk_abort["msg"])
        zone = zones[zi]
        cx, cy = zone.center
        zlabel = f"feature {zi + 1}/{nz} · {h:02d}h"
        if on_progress is not None:
            on_progress(tracker.display_percent, f"{zlabel} · récupération du vent amont…")
        spd, drc = make(h)(cx, cy)
        half_w = (zone.bbox[2] - zone.bbox[0]) / 2.0 + AUTO_EDGE_BUFFER_M
        half_h = (zone.bbox[3] - zone.bbox[1]) / 2.0 + AUTO_EDGE_BUFFER_M
        crop = crop_dem(dem, cx, cy, half_w, half_h)
        crop_path = work_root / f"z{zi:02d}_h{h:02d}.tif"
        write_dem(crop, crop_path)
        if on_progress is not None:
            on_progress(tracker.display_percent,
                        f"{zlabel} · vent {spd * 3.6:.0f} km/h de {drc:.0f}° · maillage + solveur…")
        t0 = perf_counter()
        run = run_momentum(
            cli=cli, dem_path=str(crop_path),
            working_dir=str(work_root / f"z{zi:02d}_h{h:02d}_run"),
            wind_speed_ms=spd, wind_from_deg=drc,
            mesh_count=cfg.mesh_count, iterations=cfg.iterations,
            num_threads=per_run_threads, cancel=should_cancel,
            on_progress=(lambda p, m: on_progress(tracker.display_percent, f"{zlabel} · {m}"))
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
        if cfg.compact_cases_during_run:
            case = _compact_case(case, work_root)  # optional low-disk mode
        cases.append(case)
        tracker.record(case.elapsed_s)
        if on_progress is not None:
            on_progress(tracker.display_percent, tracker.summary("Pass-2 auto"))

    failed: list[int] = []
    with timings.measure("Pass-2"):
        tracker.start()  # anchor the wall clock at the first solve, for the ETA
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_solve, zi, h): k for k, (zi, h) in enumerate(tasks)}
            for fut in as_completed(futs):
                try:
                    _after(fut.result())
                except Exception:
                    if disk_abort["hit"]:  # disk too low: stop launching, keep what we have
                        for f in futs:
                            f.cancel()
                        break
                    if cancel is not None and cancel():
                        for f in futs:
                            f.cancel()
                        raise RuntimeError("cancelled")
                    failed.append(futs[fut])  # retry alone once the pool drains

        result = AutoResult(dem_path=str(dem_path), crs=dem.crs, partition=zones)
        if disk_abort["hit"]:
            result.failures.append((-1, -1, disk_abort["msg"]))
            if on_progress is not None:
                on_progress(tracker.display_percent, disk_abort["msg"])
        else:
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
