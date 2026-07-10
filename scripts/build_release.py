"""Build the SousLeVent wheel + Windows exe INTO the generated root (``C:\\A2K\\SousLeVent\\build``),
never the drive-synced dev tree.

    python scripts/build_release.py            # wheel + exe
    python scripts/build_release.py --wheel    # wheel only
    python scripts/build_release.py --exe      # exe only

Every output (wheel, PyInstaller dist + work dirs) is redirected under the generated root, and any
stray ``build/`` / ``dist/`` / ``*.egg-info`` that setuptools/PyInstaller drop in the repo are removed
at the end — so the synchronised dev folder stays clean. The root follows ``SILLAGE_GENERATED_ROOT``
(same as the app), defaulting to ``C:\\A2K\\SousLeVent`` on Windows.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _out_root() -> Path:
    root = os.environ.get("SILLAGE_GENERATED_ROOT") or (
        r"C:\A2K\SousLeVent" if os.name == "nt" else str(REPO / ".generated"))
    return Path(root) / "build"


def _clean_repo_artifacts() -> None:
    """Remove build leftovers the tools drop in the repo (so Drive doesn't sync them)."""
    targets = [REPO / "build", REPO / "dist"]
    targets += list(REPO.glob("*.egg-info")) + list((REPO / "src").glob("*.egg-info"))
    for p in targets:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()


def build_wheel(out: Path) -> None:
    dist = out / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(dist)],
        cwd=str(REPO), check=True)


def build_exe(out: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(REPO / "packaging" / "souslevent.spec"),
         "--noconfirm", "--distpath", str(out / "dist"), "--workpath", str(out / "work")],
        cwd=str(REPO), check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build SousLeVent artefacts into the generated root.")
    ap.add_argument("--wheel", action="store_true", help="build only the wheel")
    ap.add_argument("--exe", action="store_true", help="build only the exe")
    args = ap.parse_args()
    do_all = not (args.wheel or args.exe)

    out = _out_root()
    out.mkdir(parents=True, exist_ok=True)
    try:
        if do_all or args.wheel:
            build_wheel(out)
        if do_all or args.exe:
            build_exe(out)
    finally:
        _clean_repo_artifacts()  # keep the (synced) dev folder clean even on failure
    print(f"\nBuild outputs in: {out}")


if __name__ == "__main__":
    main()
