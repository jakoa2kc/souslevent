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

import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path


_BASE_TEMP_ENV = {
    key: os.environ.get(key)
    for key in ("TMP", "TEMP", "TMPDIR", "CPL_TMPDIR", "PROJ_USER_WRITABLE_DIRECTORY")
}


def _subprocess_env(tmp_dir=None) -> dict:
    """Environment for WindNinja subprocesses.

    Two safeguards for **parallel** runs (sub-zones, ADR-0017):
    - ``PROJ_NETWORK=OFF`` so PROJ/GDAL never fetch datum grids over the network (those fetches
      surface as "ERROR 1: HTTP error code : 500" and trip under concurrent load).
    - An **isolated temp dir** when requested (``TMP``/``TEMP``/``TMPDIR``/``CPL_TMPDIR``).
      Concurrent WindNinja/GDAL processes can race on same-named scratch files → intermittent
      ``rc=-1`` (4294967295). A per-run temp dir removes that collision. Momentum/OpenFOAM
      intentionally keeps the system temp environment unless a caller explicitly opts in.
    """
    env = os.environ.copy()
    env["PROJ_NETWORK"] = "OFF"
    if tmp_dir is not None:
        tmp = Path(tmp_dir)
        tmp.mkdir(parents=True, exist_ok=True)
        for key in ("TMP", "TEMP", "TMPDIR", "CPL_TMPDIR"):
            env[key] = str(tmp)
        # Isolate PROJ's writable cache (proj cache.db) per run too: concurrent processes
        # otherwise contend on the shared SQLite cache → "database is locked" → rc=-1.
        env["PROJ_USER_WRITABLE_DIRECTORY"] = str(tmp)
    else:
        # Momentum/OpenFOAM is sensitive to project TMP redirection. Restore temp-related
        # variables captured before load_config() can pin them to the generated root.
        for key, value in _BASE_TEMP_ENV.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
    return env

# WindNinja prints phase progress like "Run 0: (solver) 36% complete...".
_PROGRESS_RE = re.compile(r"(\d+)\s*%\s*complete", re.IGNORECASE)
# Momentum phases that print NO percentage (the long post-solver mass-mesh sampling lives
# here) — surface them so the UI shows activity instead of looking frozen at 99%.
_PHASE_RE = re.compile(
    r"meshing|solving|sampling|generating|writing|renumber|refine|blockmesh|toposet|"
    r"initial conditions|applyinit|movedynamicmesh|stl|conversion|run number",
    re.IGNORECASE,
)


def _parse_progress(line: str) -> int | None:
    m = _PROGRESS_RE.search(line)
    return int(m.group(1)) if m else None


# --- Centralized CLI flag names (verify against installed version) ---
FLAG = {
    "momentum": "momentum_flag",
    "iterations": "number_of_iterations",
    "mesh_count": "mesh_count",
    "num_threads": "num_threads",
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


def format_run_failure(run: WindNinjaRun, label: str, tail: int = 1200) -> str:
    """Human-readable WindNinja failure with enough context for an IHM error box."""
    cmd = " ".join(str(part) for part in run.command)
    return (
        f"{label} failed rc={run.returncode}\n"
        f"CWD: {run.working_dir}\n"
        f"CMD:\n{cmd}\n"
        f"--- stderr tail ---\n{run.stderr[-tail:]}\n"
        f"--- stdout tail ---\n{run.stdout[-tail:]}"
    )


def _run(cmd, cwd, dry_run, on_progress=None, cancel=None, tmp_dir=None):
    """Execute a WindNinja command. Returns (returncode, stdout, stderr).

    With no callbacks this is a plain blocking ``subprocess.run`` (unchanged behavior).
    If ``on_progress`` or ``cancel`` is given, switch to a streaming ``Popen`` that parses
    ``% complete`` lines for progress and terminates the subprocess when ``cancel()`` turns
    True — this is what the IHM worker thread uses to stay responsive (ADR-0009).

    ``tmp_dir`` isolates the run's temp + PROJ cache (only needed for **concurrent** runs, e.g.
    parallel sub-zones). It is intentionally NOT used for the single momentum run — redirecting
    OpenFOAM's temp env there destabilises it (access-violation while writing output).
    """
    if dry_run:
        return None, "", ""
    env = _subprocess_env(tmp_dir)
    if on_progress is None and cancel is None:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, env=env)
        return proc.returncode, proc.stdout, proc.stderr

    popen = subprocess.Popen(
        cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=env,
    )
    err_chunks: list[str] = []

    def _drain_err():
        assert popen.stderr is not None
        for line in popen.stderr:
            err_chunks.append(line)

    et = threading.Thread(target=_drain_err, daemon=True)
    et.start()

    out_chunks: list[str] = []
    cancelled = False
    last_pct = 0
    assert popen.stdout is not None
    for line in popen.stdout:
        out_chunks.append(line)
        if on_progress is not None:
            pct = _parse_progress(line)
            if pct is not None:
                last_pct = pct
                on_progress(pct, line.strip())
            elif _PHASE_RE.search(line):
                # phase line without a %: keep the bar, update the text so it isn't "frozen"
                on_progress(last_pct, line.strip())
        if cancel is not None and cancel():
            cancelled = True
            popen.terminate()
            try:
                popen.wait(timeout=5)
            except subprocess.TimeoutExpired:
                popen.kill()
            break

    rc = popen.wait()
    et.join(timeout=2)
    err = "".join(err_chunks)
    if cancelled:
        err = (err + "\n[cancelled by user]").strip()
    return rc, "".join(out_chunks), err


def run_mass(
    *,
    cli: str,
    dem_path: str,
    working_dir: str,
    wind_speed_ms: float | None = None,
    wind_from_deg: float | None = None,
    input_height_m: float = 10.0,
    output_resolution_m: float = 50.0,
    vegetation: str = "grass",
    weather_model_init: dict | None = None,
    num_threads: int | None = None,
    tmp_dir=None,
    dry_run: bool = False,
    on_progress=None,
    cancel=None,
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
    if not dry_run:
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
        "--output_speed_units=mps",
        f"--vegetation={vegetation}",
        f"--mesh_resolution={output_resolution_m}",
        "--units_mesh_resolution=m",
        f"--{FLAG['ascii_uv']}=true",
        f"--{FLAG['ascii_res']}={output_resolution_m}",
        "--units_ascii_out_resolution=m",
        "--write_ascii_output=true",
        f"--output_path={wd}",
    ]
    if num_threads is not None:
        # Cap per-run threads so parallel sub-zone solves don't oversubscribe the CPU.
        cmd.append(f"--{FLAG['num_threads']}={int(num_threads)}")

    rc, out, err = _run(cmd, wd, dry_run, on_progress=on_progress, cancel=cancel, tmp_dir=tmp_dir)
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
    vegetation: str = "grass",
    dry_run: bool = False,
    on_progress=None,
    cancel=None,
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
    if not dry_run:
        wd.mkdir(parents=True, exist_ok=True)

    cmd = [
        cli,
        f"--elevation_file={dem_path}",
        f"--{FLAG['momentum']}=true",
        f"--{FLAG['mesh_count']}={mesh_count}",
        f"--{FLAG['iterations']}={iterations}",
        f"--{FLAG['turbulence_out']}={'true' if turbulence_output else 'false'}",
        # WindNinja 3.12 validates: turbulence_output_flag REQUIRES write_goog_output.
        # We read the OpenFOAM case directly (ADR-0004), but the binary still demands an
        # output format alongside turbulence, so enable the (small) Google Earth kmz.
        *(["--write_goog_output=true"] if turbulence_output else []),
        "--initialization_method=domainAverageInitialization",
        f"--input_speed={wind_speed_ms}",
        "--input_speed_units=mps",
        f"--input_direction={wind_from_deg}",
        f"--input_wind_height={input_height_m}",
        "--units_input_wind_height=m",
        "--output_wind_height=10.0",
        "--units_output_wind_height=m",
        "--output_speed_units=mps",
        f"--vegetation={vegetation}",
        f"--output_path={wd}",
    ]

    rc, out, err = _run(cmd, wd, dry_run, on_progress=on_progress, cancel=cancel)
    run = WindNinjaRun(command=cmd, working_dir=wd, returncode=rc, stdout=out, stderr=err)
    if not dry_run:
        run.openfoam_case_dir = locate_openfoam_case(
            wd, out, extra_roots=[Path(dem_path).parent]
        )
    return run


def _is_openfoam_case(path: Path) -> bool:
    return (path / "system").is_dir() and (path / "constant").is_dir()


def locate_openfoam_case(
    working_dir: Path, stdout: str = "", extra_roots=None
) -> Path | None:
    """Best-effort discovery of the OpenFOAM case directory for a momentum run.

    Verified against WindNinja 3.12 on Windows: NinjaFOAM writes the case as a
    ``NINJAFOAM_<dem>_<pid>_<n>`` directory in the **DEM's parent directory** (NOT in the
    run working dir, which only gets the kmz/sampled outputs). We therefore search both
    the working dir and any ``extra_roots`` (pass the DEM's parent), preferring explicit
    ``NINJAFOAM_*`` dirs, then any dir carrying constant/ + system/, newest first.
    See docs/05_windninja_integration.md, roadmap M2/T8.
    """
    roots = [Path(working_dir)]
    if extra_roots:
        roots += [Path(r) for r in extra_roots]

    candidates: list[Path] = []
    seen: set[Path] = set()

    def _add(case: Path):
        case = case.resolve()
        if case not in seen and _is_openfoam_case(case):
            candidates.append(case)
            seen.add(case)

    for root in roots:
        if not root.exists():
            continue
        for d in sorted(root.glob("NINJAFOAM_*")):  # NinjaFOAM's own naming
            if d.is_dir():
                _add(d)
        for p in root.rglob("system"):              # generic OpenFOAM signature
            _add(p.parent)

    if candidates:
        return max(candidates, key=lambda c: c.stat().st_mtime)
    # TODO: parse `stdout` for the temp dir path printed by WindNinja/OpenFOAM.
    return None


