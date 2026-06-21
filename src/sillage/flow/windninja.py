"""The ONLY place that shells out to WindNinja_cli.

All CLI flag names live here so a WindNinja version change is a one-file fix. Confirm
flags against the installed version with ``WindNinja_cli --help`` and the CLI source
(``src/ninja/cli.cpp`` in the WindNinja repo). See docs/05_windninja_integration.md.

Two entry points:
  * run_mass(...)     -> Pass 1: conservation-of-mass solver, weather-model OR
                         domain-average init, ASCII u,v output. Captures the spatially-
                         varying forecast. CANNOT show rotors (by design).
  * run_momentum(...) -> Pass 2: momentum solver (NinjaFOAM/OpenFOAM), DOMAIN-AVERAGE
                         wind only. Captures recirculation. Records the OpenFOAM CASE
                         directory so flow.openfoam_reader can read the true 3D field.

Both support dry_run=True to build and return the command without executing, so the
wrapper is testable without the binary installed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# --- Centralized CLI flag names (verify against installed version) ---
FLAG = {
    "momentum": "momentum_flag",
    "iterations": "number_of_iterations",
    "mesh_count": "mesh_count",
    "turbulence_out": "turbulence_output_flag",
    "write_vtk": "write_vtk_output",  # NOTE: momentum VTK = mass mesh, not foam field
    "ascii_uv": "ascii_out_uv",
    "ascii_geog": "ascii_out_geog",
    "ascii_res": "ascii_out_resolution",
}


@dataclass
class WindNinjaRun:
    """Result of a WindNinja invocation."""

    command: list[str]
    working_dir: Path
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    output_paths: list[Path] = field(default_factory=list)
    openfoam_case_dir: Path | None = None  # set for momentum runs once located


def _run(cmd: list[str], cwd: Path, dry_run: bool) -> tuple[int | None, str, str]:
    if dry_run:
        return None, "", ""
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def run_mass(
    *,
    cli: str,
    dem_path: str,
    working_dir: str,
    wind_speed_ms: float | None = None,
    wind_from_deg: float | None = None,
    input_height_m: float = 10.0,
    output_resolution_m: float = 50.0,
    weather_model_init: dict | None = None,
    dry_run: bool = False,
) -> WindNinjaRun:
    """Pass 1: conservation-of-mass solver.

    Provide EITHER a domain-average wind (wind_speed_ms + wind_from_deg) OR a
    weather-model initialization config (weather_model_init) -- the mass solver is the
    only solver that accepts weather-model init. Requests ASCII u,v output for the
    screening velocity-deficit computation.

    `weather_model_init` is intentionally a passthrough dict of CLI key->value pairs;
    its exact schema depends on the WindNinja build (NOMADS/forecast options). Wire it
    when implementing the hourly loop (roadmap M4).
    """
    wd = Path(working_dir)
    wd.mkdir(parents=True, exist_ok=True)

    cmd = [cli, f"--elevation_file={dem_path}", f"--{FLAG['momentum']}=false"]

    if weather_model_init is not None:
        cmd.append("--initialization_method=wxModelInitialization")
        for k, v in weather_model_init.items():
            cmd.append(f"--{k}={v}")
    else:
        if wind_speed_ms is None or wind_from_deg is None:
            raise ValueError(
                "run_mass needs either weather_model_init or "
                "(wind_speed_ms and wind_from_deg)."
            )
        cmd += [
            "--initialization_method=domainAverageInitialization",
            f"--input_speed={wind_speed_ms}",
            "--input_speed_units=mps",
            f"--input_direction={wind_from_deg}",
            f"--input_wind_height={input_height_m}",
            "--units_input_wind_height=m",
        ]

    cmd += [
        "--output_wind_height=10.0",
        "--units_output_wind_height=m",
        f"--{FLAG['ascii_uv']}=true",
        f"--{FLAG['ascii_res']}={output_resolution_m}",
        "--units_ascii_out_resolution=m",
        "--write_ascii_output=true",
        f"--output_path={wd}",
    ]

    rc, out, err = _run(cmd, wd, dry_run)
    run = WindNinjaRun(command=cmd, working_dir=wd, returncode=rc, stdout=out, stderr=err)
    if not dry_run:
        run.output_paths = sorted(wd.glob("*.asc"))
    return run


def run_momentum(
    *,
    cli: str,
    dem_path: str,
    working_dir: str,
    wind_speed_ms: float,
    wind_from_deg: float,
    input_height_m: float = 10.0,
    mesh_count: int = 500_000,
    iterations: int = 300,
    turbulence_output: bool = True,
    dry_run: bool = False,
) -> WindNinjaRun:
    """Pass 2: momentum solver (NinjaFOAM / OpenFOAM RANS).

    DOMAIN-AVERAGE wind only (no weather-model / point init). `dem_path` should already
    be the CROPPED + BUFFERED feature window (see flow.momentum_pass / docs/05). Enables
    turbulence output (turbulence intensity is a primary danger proxy in Pass 2).

    After a real run, locate the OpenFOAM CASE directory and store it on the result so
    flow.openfoam_reader can read the true 3D field. Do NOT rely on write_vtk_output for
    3D -- for momentum runs that VTK is the mass mesh, not the foam field (ADR-0004).
    """
    wd = Path(working_dir)
    wd.mkdir(parents=True, exist_ok=True)

    cmd = [
        cli,
        f"--elevation_file={dem_path}",
        f"--{FLAG['momentum']}=true",
        f"--{FLAG['mesh_count']}={mesh_count}",
        f"--{FLAG['iterations']}={iterations}",
        f"--{FLAG['turbulence_out']}={'true' if turbulence_output else 'false'}",
        "--initialization_method=domainAverageInitialization",
        f"--input_speed={wind_speed_ms}",
        "--input_speed_units=mps",
        f"--input_direction={wind_from_deg}",
        f"--input_wind_height={input_height_m}",
        "--units_input_wind_height=m",
        "--output_wind_height=10.0",
        "--units_output_wind_height=m",
        f"--output_path={wd}",
    ]

    rc, out, err = _run(cmd, wd, dry_run)
    run = WindNinjaRun(command=cmd, working_dir=wd, returncode=rc, stdout=out, stderr=err)
    if not dry_run:
        run.openfoam_case_dir = locate_openfoam_case(wd, out)
    return run


def locate_openfoam_case(working_dir: Path, stdout: str = "") -> Path | None:
    """Best-effort discovery of the temporary OpenFOAM case directory for a momentum run.

    NinjaFOAM creates a temp case (with constant/, system/, and time directories). The
    exact location varies by build; strategy: scan the working dir tree for an OpenFOAM
    signature, and/or parse the console output. Refine once tested on the target install
    (docs/05_windninja_integration.md, roadmap M2/T8).
    """
    candidates = []
    for p in Path(working_dir).rglob("system"):
        case = p.parent
        if (case / "constant").is_dir():
            candidates.append(case)
    if candidates:
        # most-recently modified case
        return max(candidates, key=lambda c: c.stat().st_mtime)
    # TODO: parse `stdout` for the temp dir path printed by WindNinja/OpenFOAM.
    return None
