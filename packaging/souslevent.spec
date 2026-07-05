# PyInstaller spec for the SousLeVent desktop app.
#
#   pip install -e .[dev]            # pulls pyinstaller (or: pip install pyinstaller)
#   pyinstaller packaging/souslevent.spec --noconfirm
#   -> dist/SousLeVent/SousLeVent.exe   (one-dir bundle; ship the whole SousLeVent/ folder)
#
# Notes
# - VTK / GDAL (rasterio) / PROJ (pyproj) carry native data that MUST be collected — done below.
# - console=True keeps a terminal so first-run errors are visible; flip to False for the release
#   build once it runs clean.
# - The user's .env is read from next to the exe or %APPDATA%\SousLeVent (see sillage.config).

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))  # noqa: F821 (SPECPATH is injected)

datas, binaries, hiddenimports = [], [], []

# The whole sillage package: it uses many lazy (in-function) imports, so pull it all in.
hiddenimports += collect_submodules("sillage")

# Heavy / data-carrying third-party deps that PyInstaller's static analysis can miss.
for pkg in (
    "pyvista", "pyvistaqt", "vtkmodules",   # 3D (VTK)
    "rasterio", "pyproj",                    # DEM IO + reprojection (GDAL / PROJ data)
    "contextily", "xyzservices", "mercantile",  # basemap tiles
    "matplotlib", "superqt", "py7zr", "scipy",
):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:  # a missing optional package must not break the whole build
        print(f"[souslevent.spec] collect_all({pkg!r}) skipped: {exc}")

a = Analysis(
    [os.path.join(ROOT, "scripts", "souslevent.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SousLeVent",
    debug=False,
    strip=False,
    upx=False,
    console=False,      # windowed release build; set True to debug first-run import errors
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SousLeVent",
)
