"""Save / open auto-run results as a single portable bundle (a ``.sillage`` zip).

We persist **only the lee-zone meshes** (the small reversed-flow rotor ``.vtu`` per feature/hour —
NOT the full OpenFOAM mesh field), plus the terrain DEM, the planned route, the per-hour date labels
and the run parameters. That is everything the 3D scene needs to redraw the wake without recomputing.

Bundle layout (inside the zip):
  manifest.json   — config, route (segments), hours + absolute-date labels, per-case metadata
  dem.tif         — the corridor terrain (drape + relief)
  rotor_XXX.vtu   — one clipped rotor mesh per case (skipped when a case has no reversed flow)
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


def save_result(zip_path, result, *, cfg, hour_labels: dict, route_cells=None) -> str:
    """Write an auto run to a ``.sillage`` bundle. ``hour_labels`` maps clock-hour offset → the
    absolute date label shown on the slider (kept so reopening shows the right day, not today's).
    ``route_cells`` is the **run's** AROME route wind (``[(lat, lon, series), …]``) — saved so the
    reopened arrows match the computed lee zones, NOT today's forecast. Persists BOTH lee volumes
    per case (rotor + turbulence) so either metric works on reopen."""
    from .scene import extract_volume

    zip_path = Path(zip_path)
    if zip_path.suffix != SUFFIX:
        zip_path = zip_path.with_suffix(SUFFIX)
    tmp = Path(tempfile.mkdtemp(prefix="sillage_save_"))
    try:
        if result.dem_path and Path(result.dem_path).exists():
            shutil.copyfile(result.dem_path, tmp / "dem.tif")

        cases_meta = []
        for i, c in enumerate(result.cases):
            files = {"rotor": "", "turbulence": ""}
            for metric, attr, suffix in (("rotor", "rotor_path", "rotor"),
                                         ("turbulence", "turb_path", "turb")):
                vtu = f"{suffix}_{i:03d}.vtu"
                src = getattr(c, attr, "")
                if src and Path(src).exists():           # already persisted -> copy
                    shutil.copyfile(src, tmp / vtu)
                    files[metric] = vtu
                elif c.case_dir:                         # not compacted -> extract this volume now
                    try:
                        vol = extract_volume(c.case_dir, c.wind_from_deg, c.aoi_bounds,
                                             metric=metric, ref_speed_ms=c.wind_speed_ms)
                        if vol is not None and vol.n_cells:
                            vol.save(str(tmp / vtu))
                            files[metric] = vtu
                    except Exception:
                        pass
            cases_meta.append({
                "zone_index": c.zone_index, "hour": c.hour,
                "wind_speed_ms": c.wind_speed_ms, "wind_from_deg": c.wind_from_deg,
                "aoi_bounds": list(c.aoi_bounds), "elapsed_s": c.elapsed_s,
                "rotor_file": files["rotor"], "turb_file": files["turbulence"],
            })

        manifest = {
            "format": _FORMAT, "version": 1,
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
    cases = [
        CaseResult(
            zone_index=int(m["zone_index"]), hour=int(m["hour"]), case_dir="",
            wind_speed_ms=float(m["wind_speed_ms"]), wind_from_deg=float(m["wind_from_deg"]),
            crs=crs, aoi_bounds=tuple(m["aoi_bounds"]), elapsed_s=float(m.get("elapsed_s", 0.0)),
            rotor_path=str(dest / m["rotor_file"]) if m.get("rotor_file") else "",
            turb_path=str(dest / m["turb_file"]) if m.get("turb_file") else "",
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
                        config=manifest.get("config", {}))
