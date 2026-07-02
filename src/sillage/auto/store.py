"""Save / open auto-run results as a single portable bundle (a ``.sillage`` zip).

Compact bundles persist the already-thresholded lee-zone meshes. Re-analysable bundles additionally
persist one threshold-independent ``source_XXX.vtu`` per case: clipped geometry + derived scalars,
not the full OpenFOAM case. That lets the UI re-extract volumes with new thresholds after reopening.

Bundle layout (inside the zip):
  manifest.json   — config, route (segments), hours + absolute-date labels, per-case metadata
  dem.tif         — the corridor terrain (drape + relief)
  source_XXX.vtu  — optional re-analysable source per case
  rotor_XXX.vtu   — compact fallback: one clipped metric mesh per case
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

SUFFIX = ".sillage"
_FORMAT = "sillage-auto-result"


def _cfg_to_dict(cfg) -> dict:
    return {
        "bbox_latlon": list(cfg.bbox_latlon),
        "hours": list(cfg.hours),
        "route_latlon": [list(p) for p in cfg.route_latlon],
        "route_segments": [[list(p) for p in seg] for seg in cfg.route_segments],
        "corridor_margin_km": cfg.corridor_margin_km,
        "window_start_iso": cfg.window_start_iso,
        "target_res_m": cfg.target_res_m,
        "max_features": cfg.max_features,
        "domain_mode": cfg.domain_mode,
        "tile_step_m": cfg.tile_step_m,
        "mesh_count": cfg.mesh_count,
        "iterations": cfg.iterations,
        "wind_source": cfg.wind_source,
    }


def save_result(zip_path, result, *, cfg, hour_labels: dict, route_cells=None,
                include_sources: bool = False, temp_dir=None) -> str:
    """Write an auto run to a ``.sillage`` bundle. ``hour_labels`` maps clock-hour offset → the
    absolute date label shown on the slider (kept so reopening shows the right day, not today's).
    ``route_cells`` is the **run's** AROME route wind (``[(lat, lon, series), …]``) — saved so the
    reopened arrows match the computed lee zones, NOT today's forecast.

    ``include_sources=True`` stores a re-analysable source per case when the OpenFOAM case (or a
    previous source) is available. If a source cannot be produced, the function falls back to the
    compact metric volumes for that case. ``temp_dir`` keeps the staging directory under the
    configured Sillage generated root instead of the OS-global temp.
    """
    from .scene import extract_case_source, extract_case_volumes

    zip_path = Path(zip_path)
    if zip_path.suffix != SUFFIX:
        zip_path = zip_path.with_suffix(SUFFIX)
    temp_root = Path(temp_dir) if temp_dir is not None else zip_path.parent
    temp_root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="sillage_save_", dir=str(temp_root)))
    try:
        if result.dem_path and Path(result.dem_path).exists():
            shutil.copyfile(result.dem_path, tmp / "dem.tif")

        cases_meta = []
        for i, c in enumerate(result.cases):
            files = {}
            source_file = ""
            if include_sources:
                src = getattr(c, "source_path", "")
                if src and Path(src).exists():
                    source_file = f"source_{i:03d}.vtu"
                    shutil.copyfile(src, tmp / source_file)
                elif c.case_dir:
                    try:
                        source = extract_case_source(
                            c.case_dir, c.wind_from_deg, c.aoi_bounds,
                            ref_speed_ms=c.wind_speed_ms)
                        if source is not None and source.n_cells:
                            source_file = f"source_{i:03d}.vtu"
                            source.save(str(tmp / source_file))
                    except Exception:
                        source_file = ""
            if not source_file:
                for metric, src in (getattr(c, "vtu_paths", {}) or {}).items():  # already persisted
                    if src and Path(src).exists():
                        vtu = f"{metric}_{i:03d}.vtu"
                        shutil.copyfile(src, tmp / vtu)
                        files[metric] = vtu
            if not source_file and not files and c.case_dir:
                try:
                    for metric, vol in extract_case_volumes(
                            c.case_dir, c.wind_from_deg, c.aoi_bounds,
                            ref_speed_ms=c.wind_speed_ms).items():
                        vtu = f"{metric}_{i:03d}.vtu"
                        vol.save(str(tmp / vtu))
                        files[metric] = vtu
                except Exception:
                    pass
            if not source_file and not files:
                # No case_dir and no persisted volumes, but a re-analysable source exists (e.g. a
                # reopened v2 bundle re-saved as compact): derive the compact metric volumes from the
                # source so we NEVER write a case with zero meshes (silent data loss). If even that
                # fails, copy the source itself so nothing is lost.
                src = getattr(c, "source_path", "")
                if src and Path(src).exists():
                    try:
                        import pyvista as pv

                        from ..viz import volume3d as v3

                        source = pv.read(src)
                        for metric in v3.LEE_METRICS:
                            vol = v3.threshold_lee_source(
                                source, metric=metric,
                                vol_floor=v3.DEFAULT_VOL_FLOORS.get(metric, 0.0))
                            if vol is not None and vol.n_cells:
                                vtu = f"{metric}_{i:03d}.vtu"
                                vol.save(str(tmp / vtu))
                                files[metric] = vtu
                    except Exception:
                        files = {}
                    if not files:  # last resort: keep the source so the case is not lost
                        source_file = f"source_{i:03d}.vtu"
                        shutil.copyfile(src, tmp / source_file)
            cases_meta.append({
                "zone_index": c.zone_index, "hour": c.hour,
                "wind_speed_ms": c.wind_speed_ms, "wind_from_deg": c.wind_from_deg,
                "aoi_bounds": list(c.aoi_bounds), "elapsed_s": c.elapsed_s,
                "vtu_files": files,
                "source_file": source_file,
            })

        manifest = {
            "format": _FORMAT, "version": 2 if include_sources else 1,
            "storage_mode": "reanalyzable" if include_sources else "compact",
            "crs": result.crs.to_wkt() if result.crs is not None else "",
            "dem": "dem.tif" if (tmp / "dem.tif").exists() else "",
            "hour_labels": {str(h): lbl for h, lbl in hour_labels.items()},
            "timings_summary": result.timings_summary,
            "failures": [list(f) for f in result.failures],
            "config": _cfg_to_dict(cfg),
            "cases": cases_meta,
            # the run's route wind (lat, lon, [(time, speed_ms, from_deg), …]) per ~1.5 km cell
            "route_cells": [[la, lo, [list(s) for s in series]]
                            for (la, lo, series) in (route_cells or [])],
        }
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(tmp.iterdir()):
                z.write(p, p.name)
        return str(zip_path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@dataclass
class LoadedResult:
    result: object              # auto.pipeline.AutoResult (cases point at the extracted .vtu)
    hours: list                 # clock-hour offsets
    hour_labels: dict           # {hour: absolute-date label}
    route_segments: list        # [[(lat, lon), ...], ...]
    route_cells: list           # the run's AROME route wind [(lat, lon, series), ...]
    config: dict                # the saved AutoConfig fields (for display / restore)
    storage_mode: str           # "compact" or "reanalyzable"


def load_result(zip_path, dest_dir) -> LoadedResult:
    """Open a ``.sillage`` bundle, extracting its files under ``dest_dir`` (kept alive while the
    window renders from them). Rebuilds an ``AutoResult`` whose cases read the bundled rotor ``.vtu``."""
    from rasterio.crs import CRS

    from .pipeline import AutoResult, CaseResult

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(zip_path), "r") as z:
        z.extractall(str(dest))
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != _FORMAT:
        raise ValueError("Fichier non reconnu (format Sillage attendu).")

    crs = CRS.from_wkt(manifest["crs"]) if manifest.get("crs") else None

    def _vtu_files(m: dict) -> dict:
        vf = m.get("vtu_files")
        if vf:
            return vf
        legacy = {}  # bundles from the intermediate build stored per-metric rotor_file / turb_file
        if m.get("rotor_file"):
            legacy["rotor"] = m["rotor_file"]
        if m.get("turb_file"):
            legacy["turbulence"] = m["turb_file"]
        return legacy

    cases = [
        CaseResult(
            zone_index=int(m["zone_index"]), hour=int(m["hour"]), case_dir="",
            wind_speed_ms=float(m["wind_speed_ms"]), wind_from_deg=float(m["wind_from_deg"]),
            crs=crs, aoi_bounds=tuple(m["aoi_bounds"]), elapsed_s=float(m.get("elapsed_s", 0.0)),
            vtu_paths={metric: str(dest / fn) for metric, fn in _vtu_files(m).items()},
            source_path=str(dest / m["source_file"]) if m.get("source_file") else "",
        )
        for m in manifest["cases"]
    ]
    result = AutoResult(
        dem_path=str(dest / manifest["dem"]) if manifest.get("dem") else "",
        crs=crs, partition=[], cases=cases,
        failures=[tuple(f) for f in manifest.get("failures", [])],
        timings_summary=manifest.get("timings_summary", ""),
    )
    hour_labels = {int(h): lbl for h, lbl in manifest.get("hour_labels", {}).items()}
    segments = [[(float(p[0]), float(p[1])) for p in seg]
                for seg in manifest.get("config", {}).get("route_segments", [])]
    route_cells = [(float(c[0]), float(c[1]),
                    [(s[0], float(s[1]), float(s[2])) for s in c[2]])
                   for c in manifest.get("route_cells", [])]
    return LoadedResult(result=result, hours=result.hours, hour_labels=hour_labels,
                        route_segments=segments, route_cells=route_cells,
                        config=manifest.get("config", {}),
                        storage_mode=manifest.get("storage_mode", "compact"))
