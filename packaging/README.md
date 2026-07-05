# Packaging SousLeVent

Two deliverables: a **wheel** (for `pip install`) and a **Windows one-folder exe** (for users
without Python). Build both from a clean checkout with the GUI deps installed.

```bat
python -m pip install -e .[dev]        :: app + pytest/ruff/build/pyinstaller
python -m pytest -q                    :: sanity (offscreen ok)
```

## 1. Wheel

```bat
python -m build --wheel                :: -> dist/souslevent-<ver>-py3-none-any.whl
```

Verify in a **fresh** virtualenv:

```bat
py -m venv C:\tmp\slv-test && C:\tmp\slv-test\Scripts\pip install dist\souslevent-*.whl
C:\tmp\slv-test\Scripts\souslevent          :: the GUI must launch
```

## 2. Windows exe (PyInstaller, one-folder)

```bat
pyinstaller packaging\souslevent.spec --noconfirm
:: -> dist\SousLeVent\SousLeVent.exe  (ship the whole dist\SousLeVent\ folder, zipped)
```

- The spec collects VTK / GDAL (rasterio) / PROJ (pyproj) native data — required, or basemaps /
  DEM IO / 3D silently break.
- The spec builds **windowed** (`console=False`) — no terminal window. If a first run on a new
  machine misbehaves, temporarily set `console=True` in the spec to see import/DLL tracebacks.
- **WindNinja is NOT bundled** — it is a separate install; point `WINDNINJA_CLI` at it (see below).

## Configuration on a user machine

`sillage.config` looks for a `.env` in this order: next to the exe, the dev project root, then
`%APPDATA%\SousLeVent\.env`. Minimal `.env`:

```ini
WINDNINJA_CLI=C:\Program Files\WindNinja\bin\WindNinja_cli.exe
METEOFRANCE_API_KEY=...        # optional; without it, wind falls back to Open-Meteo
SILLAGE_GENERATED_ROOT=C:\A2K\SousLeVent   # where DEMs / cases / outputs are written
```

## Notes / caveats

- **Python version:** PyInstaller must support the interpreter you build with. If the build fails on
  a very new Python, build the exe from a 3.11–3.13 venv (the wheel is `py3-none-any`, so it stays
  compatible regardless).
- Third-party licenses (WindNinja, IGN RGE ALTI, Météo-France AROME, Open-Meteo, Copernicus) apply to
  their tools/data — see the root `README.md` and `LICENSE`.
