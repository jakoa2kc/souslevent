"""Launch the unified SousLeVent desktop application.

Legacy backups remain available through `scripts/sillage_gui.py` / `sillage-gui` and
`scripts/sillage_auto.py` / `sillage-auto`.
"""

from __future__ import annotations


def main() -> None:
    from sillage.souslevent.window import main as _main

    _main()


if __name__ == "__main__":
    main()
