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
from typing import Callable
from time import perf_counter

from ..flow.windninja import format_run_failure, run_momentum
from ..terrain.acquire import prepare_dem
from ..terrain.dem import crop_dem, load_dem, write_dem
from ..timing import RunTimings
from ..wind.directions import direction_label
import numpy as np

from .partition import (
    ZONE_HALF_FLOOR_M,
    SubZone,
    corridor_grid_tiles,
    corridor_mask,
    feature_domains,
    ninjafoam_resolution_m,
    partition_zone,
    zone_side_for_resolution,
)
from .progress import ProgressTracker
from .wind import local_wind_provider

AUTO_EDGE_BUFFER_M = 1200.0  # grow each momentum crop so the outlet/lateral BCs sit WELL off the
# feature's lee — the recirculation must die out before the boundary, else it "climbs" the edge
# (the inlet/outlet BC clamps reverse flow at the boundary; that artifact lives in this buffer ring
# and is clipped out of the displayed rotor — see auto.scene + viz._clip_domain_boundary, ADR-0021).
LOW_DISK_WARNING_GB = 3.0  # informational only: a fixed threshold must not truncate a batch
DEFAULT_MAX_FEATURES = 12
WIND_MODE_FORECAST = "forecast"
WIND_MODE_MANUAL_GRID = "manual_grid"


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
    for f in work_root.glob("z*.vtu"):  # compact lee volumes and re-analysable sources
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
    """Persist a solved case's re-analysable source (or compact fallback volumes) and **delete the
    bulky OpenFOAM case**.

    Runs in the main thread (serialized — VTK reads aren't thread-safe) as each solve completes, so
    the disk only ever holds the few cases currently being solved. On a read failure it returns the
    case unchanged (keeps ``case_dir``) — never lose a result to a cleanup hiccup."""
    from .scene import extract_case_source, extract_case_volumes

    stem = f"z{case.zone_index:02d}_h{case.hour:02d}"
    try:
        source = extract_case_source(case.case_dir, case.wind_from_deg, case.aoi_bounds,
                                     ref_speed_ms=case.wind_speed_ms)
    except Exception:
        return case  # couldn't read the case at all -> keep it for a later attempt
    source_path = ""
    if source is not None and source.n_cells:
        p = work_root / f"{stem}_source.vtu"
        try:
            source.save(str(p))
            source_path = str(p)
        except Exception:
            source_path = ""
    paths = {}
    if not source_path:
        try:
            vols = extract_case_volumes(case.case_dir, case.wind_from_deg, case.aoi_bounds,
                                        ref_speed_ms=case.wind_speed_ms)  # one read, all metrics
        except Exception:
            return case
        for metric, vol in vols.items():
            p = work_root / f"{stem}_{metric}.vtu"
            try:
                vol.save(str(p))
                paths[metric] = str(p)
            except Exception:
                pass
    _safe_rmtree(case.case_dir)              # the heavy NINJAFOAM_* OpenFOAM case
    _safe_rmtree(work_root / f"{stem}_run")  # WindNinja run dir (kmz/sampled)
    _safe_unlink(work_root / f"{stem}.tif")  # the crop DEM
    return replace(case, case_dir="", vtu_paths=paths, source_path=source_path)


def bbox_from_route(route_latlon, margin_km: float):
    """(south, west, north, east) bounding box of a flight route grown by ``margin_km`` —
    the DEM/screening extent for a route-based run."""
    if not route_latlon:
        raise ValueError("route vide")
    lats = [p[0] for p in route_latlon]
    lons = [p[1] for p in route_latlon]
    # reuse the single km-per-degree expansion (accurate 111.32) instead of a second 111.0 copy
    return _expand_bbox((min(lats), min(lons), max(lats), max(lons)), margin_km * 1000.0)


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
    # "manual" = domains were selected by the UI after a screening-only pass.
    domain_mode: str = "features"
    manual_zones: tuple[SubZone, ...] = ()          # UI-selected candidates for domain_mode="manual"
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
    wind_mode: str = WIND_MODE_FORECAST             # forecast or homogeneous manual grid
    manual_wind_speeds_kmh: tuple[int, ...] = ()    # e.g. (10, 15, 20); 5 km/h UI steps
    manual_wind_dirs_deg: tuple[int, ...] = ()       # e.g. (225, 270, 315); 45° UI steps


def _unique_sorted_ints(values) -> tuple[int, ...]:
    return tuple(sorted({int(round(float(v))) for v in values}))


def _unique_ints(values) -> tuple[int, ...]:
    out, seen = [], set()
    for value in values:
        ivalue = int(round(float(value)))
        if ivalue not in seen:
            seen.add(ivalue)
            out.append(ivalue)
    return tuple(out)


def manual_wind_scenarios(cfg: AutoConfig) -> list[tuple[int, int, int]]:
    """Manual homogeneous wind scenarios as ``(case_id, speed_kmh, from_deg)``.

    ``case_id`` deliberately reuses the historical ``hour`` field in result objects. In forecast
    mode that field remains a clock-hour offset; in manual mode it is a scenario index whose label
    is carried by the UI/save bundle.
    """
    if cfg.wind_mode != WIND_MODE_MANUAL_GRID:
        return []
    speeds = _unique_sorted_ints(cfg.manual_wind_speeds_kmh)
    dirs = _unique_ints(d % 360 for d in cfg.manual_wind_dirs_deg)
    scenarios: list[tuple[int, int, int]] = []
    idx = 0
    for drc in dirs:
        for spd in speeds:
            scenarios.append((idx, spd, drc))
            idx += 1
    return scenarios


def wind_label_for_case(cfg: AutoConfig | None, case_id: int) -> str:
    """Display label for a result slider position."""
    if cfg is not None and cfg.wind_mode == WIND_MODE_MANUAL_GRID:
        for sid, spd, drc in manual_wind_scenarios(cfg):
            if sid == int(case_id):
                return f"{spd} km/h · {direction_label(drc)}"
        return f"vent #{int(case_id) + 1}"
    return f"{int(case_id):02d}h"


def manual_wind_provider(cfg: AutoConfig):
    scenarios = manual_wind_scenarios(cfg)
    if not scenarios:
        raise ValueError("Aucun scénario de vent manuel sélectionné.")
    by_id = {sid: (spd / 3.6, float(drc)) for sid, spd, drc in scenarios}

    def make(case_id: int):
        if int(case_id) not in by_id:
            raise ValueError(f"Scénario de vent manuel inconnu : {case_id}")
        spd_ms, drc = by_id[int(case_id)]

        def wind_at_center(_x: float, _y: float) -> tuple[float, float]:
            return spd_ms, drc

        return wind_at_center

    return make


@dataclass(frozen=True, eq=False)
class CaseResult:
    """One solved (feature, hour): the OpenFOAM case + the wind + the zone bounds to clip to.

    ``eq=False`` (identity equality/hash): a frozen dataclass with value-eq would auto-generate
    ``__hash__`` over all fields — including the mutable ``vtu_paths`` dict — which raises
    ``TypeError: unhashable type: dict`` the moment an instance is hashed (set/dict key)."""

    zone_index: int
    hour: int
    case_dir: str  # OpenFOAM case (emptied only if compact_cases_during_run is enabled)
    wind_speed_ms: float
    wind_from_deg: float
    crs: object
    aoi_bounds: tuple[float, float, float, float]  # xmin, xmax, ymin, ymax (the un-buffered zone)
    elapsed_s: float
    vtu_paths: dict = field(default_factory=dict)  # metric -> persisted .vtu (compaction / save)
    source_path: str = ""  # threshold-independent source .vtu for re-analysable .sillage


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


@dataclass
class ScreeningResult:
    """Pass-1-only output for the unified UI: candidate domains, no momentum cases yet."""

    dem_path: str
    crs: object
    partition: list[SubZone]
    hours: list[int]
    hazard: np.ndarray | None = None            # aggregate (max over hours) hazard, for candidates
    hazard_stack: list | None = None            # per-hour hazard arrays aligned to `hours` (browse)
    timings_summary: str = ""


@dataclass
class _DomainPlan:
    dem_path: str
    dem: object
    wind_provider: Callable
    zones: list[SubZone]
    timings: RunTimings
    hazard: np.ndarray | None = None
    hazard_stack: list | None = None


def _expand_bbox(bbox_latlon, buffer_m):
    s, w, n, e = bbox_latlon
    dlat = buffer_m / 111_320.0
    dlon = buffer_m / (111_320.0 * max(0.05, math.cos(math.radians((s + n) / 2.0))))
    return (s - dlat, w - dlon, n + dlat, e + dlon)


def _prepare_domain_plan(
    cfg: AutoConfig,
    *,
    cli: str,
    cache_dir,
    on_progress=None,
    cancel=None,
    cleanup: bool = True,
    hourly_hazard: bool = False,
) -> _DomainPlan:
    """Prepare the fine DEM, wind provider, and momentum domains for any unified workflow.

    The old auto app needs the full plan then immediately solves all zones. The new global UI can
    stop after this step for a Pass-1-only/manual-candidate workflow, then later pass selected zones
    back through ``domain_mode="manual"``.
    """
    timings = RunTimings()
    cache_dir = Path(cache_dir)
    work_root = cache_dir / "auto"
    work_root.mkdir(parents=True, exist_ok=True)
    if cleanup:
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

    crest = float(np.nanpercentile(dem.elevation, 80))
    if cfg.wind_mode == WIND_MODE_MANUAL_GRID:
        make = manual_wind_provider(cfg)
    else:
        make = local_wind_provider(dem, crest, n_hours=max(cfg.hours) + 1, source=cfg.wind_source)
    left, bottom, right, top = dem.bounds
    cx0, cy0 = (left + right) / 2.0, (bottom + top) / 2.0

    segments_ll = list(cfg.route_segments) or ([list(cfg.route_latlon)] if cfg.route_latlon else [])

    def _seg_xy(seg):
        from rasterio.crs import CRS
        from rasterio.warp import transform as warp_xy

        lons = [p[1] for p in seg]
        lats = [p[0] for p in seg]
        xs, ys = warp_xy(CRS.from_epsg(4326), dem.crs, lons, lats)
        return list(zip(xs, ys))

    zones: list[SubZone]
    hazard_map: np.ndarray | None = None
    hazard_stack: list | None = None

    # Zone sizing so the MOMENTUM MESH matches the terrain resolution (ADR-0037): with mesh_count
    # spread over (zone + buffer)², bigger zones mean a coarser mesh than the topo — so cap the zone
    # side at what reaches cfg.target_res_m, floored at a physically-sane lee-domain size. When the
    # floor wins, the topo resolution is NOT reachable with this mesh preset (reported to the log).
    cap_side = zone_side_for_resolution(cfg.target_res_m, cfg.mesh_count, AUTO_EDGE_BUFFER_M)
    cap_half = max(ZONE_HALF_FLOOR_M, cap_side / 2.0)

    def _res_note(side_m: float) -> str:
        eff = ninjafoam_resolution_m(side_m, cfg.mesh_count, AUTO_EDGE_BUFFER_M)
        ok = eff <= cfg.target_res_m * 1.15
        return (f"maillage effectif ≈ {eff:.0f} m pour topo {cfg.target_res_m:.0f} m"
                + ("" if ok else " (topo non atteignable avec ce maillage : affine le maillage "
                               "ou réduis les zones)"))

    if cfg.domain_mode == "manual" and cfg.manual_zones:
        with timings.measure("features"):
            zones = list(cfg.manual_zones)
            if on_progress is not None:
                widest = max((z.bbox[2] - z.bbox[0]) for z in zones)
                on_progress(0, f"Sélection manuelle : {len(zones)} domaine(s) Pass-2 — "
                               + _res_note(widest))
    elif cfg.domain_mode == "corridor":
        # BLIND PAVING (ADR-0029): no Pass-1. Route selection uses overlapping corridor tiles;
        # rectangle selection uses the older relief-adaptive quadtree to cover the full AOI.
        with timings.measure("features"):
            if segments_ll:
                margin_m = cfg.corridor_margin_km * 1000.0
                half_m = cfg.tile_half_m if cfg.tile_half_m > 0 else max(margin_m, 900.0)
                half_m = min(half_m, cap_half)  # shrink tiles until the mesh matches the topo
                step_m = max(300.0, min(cfg.tile_step_m, 2.0 * half_m))
                if on_progress is not None:
                    on_progress(
                        0, "Pavage aveugle des secteurs le long du parcours (sans criblage)…")
                # ONE regular grid over the union corridor band — the route may double back or
                # cross itself; thinking per-segment stacked tilings, thinking per-SURFACE doesn't.
                band = np.zeros(dem.shape, dtype=bool)
                for seg in segments_ll:
                    if len(seg) >= 1:
                        band |= corridor_mask(dem, _seg_xy(seg), margin_m)
                zones = corridor_grid_tiles(dem, band, step_m=step_m, half_m=half_m,
                                            target_res_m=cfg.target_res_m)
                if on_progress is not None:
                    on_progress(0, f"Pavage : {len(zones)} secteurs sur {len(segments_ll)} "
                                   f"segment(s) (grille pas {step_m:.0f} m, "
                                   f"demi-tuile {half_m:.0f} m, corridor ±{margin_m:.0f} m, "
                                   f"topo {cfg.target_res_m:.0f} m) — "
                                   + _res_note(2.0 * half_m))
            else:
                step_m = max(600.0, float(cfg.tile_step_m))
                if on_progress is not None:
                    on_progress(0, "Pavage aveugle de la zone rectangle (sans criblage)…")
                # cap the leaf tile SIDE via the topo-cell budget: est_cells = (side/res)², so a
                # max_cells of (cap_side/res)² caps leaves at the mesh-matched size.
                cells_cap = int(min(600_000, max((2 * cap_half / cfg.target_res_m) ** 2, 4)))
                zones = partition_zone(
                    dem, target_res_m=cfg.target_res_m, max_cells=cells_cap,
                    max_relief_m=400.0, min_tile_m=min(step_m, 2 * cap_half))
                if on_progress is not None:
                    widest = max((z.bbox[2] - z.bbox[0]) for z in zones) if zones else 0.0
                    on_progress(0, f"Pavage rectangle : {len(zones)} secteur(s), "
                                   f"tuile min {step_m:.0f} m, topo {cfg.target_res_m:.0f} m — "
                                   + _res_note(widest))
    else:
        # FEATURE-BASED domains (ADR-0023): screen the WHOLE zone once (continuous Pass-1 mass) to
        # find the relief features, then place ONE momentum domain per feature.
        from ..screening.pass1 import hourly_indicator, hourly_indicator_stack
        with timings.measure("criblage"):
            if on_progress is not None:
                on_progress(0, "Criblage Pass-1 sur toute la zone (détection des reliefs)…")

            def _wind_at(h):
                try:
                    return make(h)(cx0, cy0)  # representative screening wind for hour/scenario h
                except Exception:
                    return 8.0, 270.0  # geometry-driven screening if the forecast is down

            if hourly_hazard and len(cfg.hours) > 1:
                # Screen EVERY hour/scenario so the map can be browsed; detect features on the
                # element-wise-max AGGREGATE so candidates cover the whole window (ADR-0036).
                series = [(wind_label_for_case(cfg, h), *_wind_at(h)) for h in cfg.hours]
                results = hourly_indicator_stack(
                    dem=dem, cli=cli, dem_path=str(dem_path), series=series,
                    work_dir_for=(lambda i, label, spd, drc:
                                  _screening_work_dir(work_root, dem_path, spd, drc, 150.0)),
                    resolution_m=150.0, edge_buffer_m=300.0, force_run=False,
                    max_workers=min(len(series), 4), cancel=cancel,
                    on_progress=(lambda p, m: on_progress(p, f"Criblage horaire : {m}")
                                 if on_progress else None))
                hazard_stack = [r.hazard for r in results]
                hazard = (np.maximum.reduce(hazard_stack) if hazard_stack
                          else np.zeros(dem.shape, dtype="float64"))
            else:
                rep_spd, rep_dir = _wind_at(cfg.hours[0])
                if on_progress is not None:
                    on_progress(0, f"Criblage : vent {rep_spd * 3.6:.0f} km/h de "
                                   f"{direction_label(rep_dir)}")
                screen_work = _screening_work_dir(work_root, dem_path, rep_spd, rep_dir, 150.0)
                hazard, _vel = hourly_indicator(
                    dem=dem, cli=cli, dem_path=str(dem_path), work_dir=screen_work,
                    wind_speed_ms=rep_spd, wind_from_deg=rep_dir, resolution_m=150.0,
                    edge_buffer_m=300.0, force_run=False, cancel=cancel,
                    on_progress=(lambda p, m: on_progress(0, f"Criblage : {m}")
                                 if on_progress else None))

        if segments_ll:  # keep only features within the corridor around each route segment
            mask = np.zeros(dem.shape, dtype=bool)
            for seg in segments_ll:
                if len(seg) >= 1:
                    mask |= corridor_mask(dem, _seg_xy(seg), cfg.corridor_margin_km * 1000.0)
            hazard = np.where(mask, hazard, 0.0)
            if hazard_stack is not None:
                hazard_stack = [np.where(mask, hz, 0.0) for hz in hazard_stack]
            if on_progress is not None:
                on_progress(0, f"Corridor : marge {cfg.corridor_margin_km:.1f} km autour du parcours")

        with timings.measure("features"):
            sep = cfg.feature_separation_m
            if cfg.route_latlon:
                sep = max(800.0, min(sep, cfg.corridor_margin_km * 1000.0))
            # Preserve the physical lee-domain size. Resolution is arbitrated in the preview before
            # Pass-2; shrinking a candidate to fit the selected mesh silently clips the wake.
            zones = feature_domains(
                dem, hazard, max_features=cfg.max_features,
                min_separation_m=sep, target_res_m=cfg.target_res_m)
            hazard_map = hazard
            if on_progress is not None:
                widest = max((z.bbox[2] - z.bbox[0]) for z in zones) if zones else 2 * cap_half
                on_progress(0, f"{len(zones)} feature(s) détectée(s) (espacement ≥ {sep:.0f} m) — "
                               + _res_note(widest))

    return _DomainPlan(
        dem_path=str(dem_path), dem=dem, wind_provider=make, zones=zones, timings=timings,
        hazard=hazard_map, hazard_stack=hazard_stack)


def screen_candidates(
    cfg: AutoConfig,
    *,
    cli: str,
    cache_dir,
    on_progress=None,
    cancel=None,
) -> ScreeningResult:
    """Run only the shared domain-preparation/Pass-1 stage and return selectable candidates."""
    if not cfg.hours:
        return ScreeningResult(dem_path="", crs=None, partition=[], hours=[],
                               timings_summary="aucune heure sélectionnée")
    plan = _prepare_domain_plan(cfg, cli=cli, cache_dir=cache_dir,
                                on_progress=on_progress, cancel=cancel, cleanup=True,
                                hourly_hazard=True)
    if on_progress is not None:
        on_progress(100, f"Criblage terminé : {len(plan.zones)} candidat(s).")
    return ScreeningResult(
        dem_path=plan.dem_path, crs=plan.dem.crs, partition=plan.zones,
        hours=list(cfg.hours), hazard=plan.hazard, hazard_stack=plan.hazard_stack,
        timings_summary=plan.timings.summary())


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

    cache_dir = Path(cache_dir)
    work_root = cache_dir / "auto"
    plan0 = _prepare_domain_plan(cfg, cli=cli, cache_dir=cache_dir,
                                 on_progress=on_progress, cancel=cancel, cleanup=True)
    timings = plan0.timings
    dem_path, dem, make, zones = plan0.dem_path, plan0.dem, plan0.wind_provider, plan0.zones

    if not zones:
        if on_progress is not None:
            on_progress(100, "Aucun relief marquant détecté dans la zone.")
        return AutoResult(dem_path=str(dem_path), crs=dem.crs, partition=[],
                          timings_summary=timings.summary())

    tasks = [(zi, h) for zi in range(len(zones)) for h in cfg.hours]
    nz, nh = len(zones), len(cfg.hours)
    time_unit = "scénario(s) vent" if cfg.wind_mode == WIND_MODE_MANUAL_GRID else "h"
    plan = momentum_parallel_plan(cfg.momentum_workers, task_count=len(tasks))
    workers = plan.workers
    per_run_threads = plan.threads_per_worker
    tracker = ProgressTracker(total=len(tasks), workers=workers)  # parallelism-aware ETA
    cases: list[CaseResult] = []
    domain_label = "secteurs" if cfg.domain_mode in ("corridor", "manual") else "reliefs"
    if on_progress is not None:
        on_progress(0, f"{nz} {domain_label} × {nh} {time_unit} = {len(tasks)} calculs Pass-2 "
                       f"(×{workers} en parallèle, {per_run_threads} thr/solve, "
                       f"{plan.used_cores}/{plan.cores} cœurs utilisés, "
                       f"maillage ~{cfg.mesh_count:,})")

    free = _free_gb(work_root)
    if free < LOW_DISK_WARNING_GB and on_progress is not None:
        on_progress(
            0,
            f"Avertissement disque : {free:.1f} Go libres. Le lot continue ; "
            "les cas transitoires seront nettoyés à la fermeture.",
        )

    def _solve(zi: int, h: int) -> CaseResult:
        def should_cancel() -> bool:
            return cancel is not None and cancel()

        if should_cancel():
            raise RuntimeError("cancelled")
        zone = zones[zi]
        cx, cy = zone.center
        zlabel = f"zone {zi + 1}/{nz} · {wind_label_for_case(cfg, h)}"
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
                        f"{zlabel} · vent {spd * 3.6:.0f} km/h de {direction_label(drc)} "
                        "· maillage + solveur…")
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

        # Defensive accounting: a completed batch must never silently omit tasks. This would have
        # made the former "18 cases + 1 global disk failure" truncation immediately visible.
        completed = {(c.zone_index, c.hour) for c in cases}
        recorded_failures = {(zi, h) for zi, h, _msg in result.failures}
        for zi, h in tasks:
            if (zi, h) not in completed and (zi, h) not in recorded_failures:
                result.failures.append((zi, h, "calcul terminé sans résultat ni erreur enregistrée"))
                tracker.record(0.0)

    result.cases = sorted(cases, key=lambda c: (c.hour, c.zone_index))
    result.timings_summary = timings.summary()
    return result
