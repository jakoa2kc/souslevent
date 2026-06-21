# Support — Environment & installation

Setup for developers and for AI tools reproducing the environment.

## System requirements
- **OS:** Linux recommended (matches WindNinja/OpenFOAM tooling best); Windows/macOS
  possible. The Docker/Katana route normalizes the OS for the solver.
- **CPU:** the solver (OpenFOAM `simpleFoam`) is **CPU-bound**; more cores = faster /
  more concurrent runs. This is the lever for solve performance.
- **GPU:** used for **3D rendering** (PyVista/VTK) only — *not* for solving. A capable
  GPU (e.g. the project's RTX 5060 Ti) helps dense streamlines and large meshes.
- **RAM:** driven by the **computational mesh** (cell count), not DEM resolution. Fine
  Pass-2 meshes (~10^6-cell class) are the demanding case.
- **Disk:** cache for DEMs + forecasts + OpenFOAM temp cases; keep some headroom.

## External software (not pip-installable)
- **WindNinja** — provides `WindNinja_cli` and `WINDNINJA_DATA` (incl. `ninjafoam.zip`
  templates for the momentum solver). Install natively, **or** use the Docker image
  (Katana bundles WindNinja + GDAL + wgrib2). Verify the binary:
  ```
  WindNinja_cli --help        # confirm flag names against docs/05
  ```
- If using Docker: ensure the container can read the DEM and write outputs to a mounted
  volume the Python side can also read.

## Python environment
- **Python ≥ 3.11**, virtual environment recommended.
- Install the package (editable) with dependencies:
  ```
  python -m venv .venv && source .venv/bin/activate
  pip install -e .
  ```
- Key Python dependencies (declared in `pyproject.toml`):
  - `numpy`, `scipy` — arrays / numerics
  - `rasterio`, `pyproj` — DEM IO + reprojection (GDAL-backed)
  - `requests` — weather APIs (Open-Meteo / AROME)
  - `pyvista` — OpenFOAM case reading + 3D rendering (VTK-backed)
  - `matplotlib` — first-pass 2D screening map
  - `click` — CLI for demo scripts
- `rasterio`/`pyproj` wrap **GDAL/PROJ**; on a bare system you may need system GDAL/PROJ.
  Prefer conda-forge or a GDAL-provisioned base image if wheels give trouble.

## Configuration
- Copy `.env.example` → `.env` and set paths/keys:
  - `WINDNINJA_CLI` — path to the `WindNinja_cli` binary (or how to invoke the Docker run).
  - `WINDNINJA_DATA` — path to WindNinja data dir if needed.
  - `SILLAGE_CACHE_DIR` — where prepared DEMs/forecasts/cases are cached.
  - `METEOFRANCE_API_KEY` — only if using AROME via the Météo-France API.
- `src/sillage/config.py` reads these and centralizes settings.

## Sanity check sequence
1. `python -c "import rasterio, pyproj, pyvista, numpy, scipy; print('deps ok')"`
2. `WindNinja_cli --help` prints (or the Docker run responds).
3. `python scripts/demo_pass1.py --help` runs (wiring intact).

## Reproducibility
Record, alongside any saved analysis: DEM provenance, forecast source + model run time,
WindNinja version, and the exact run parameters. This lets a third party or AI reproduce
a past result. See `docs/04_data_sources.md` (caching).
