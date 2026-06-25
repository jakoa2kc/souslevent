"""Launch the Sillage AUTOMATIC full-resolution IHM (sillage.auto, ADR-0022).

    python scripts/sillage_auto.py
    # or, after `pip install -e .[gui]`:  sillage-auto

One-click mode: pick a flight zone + window on the IGN map, « Valider », and the whole zone is
solved at the finest topo scale (relief-adaptive sub-zones, Pass-2 momentum per sub-zone × hour),
then browse the time-sliderable global 3D wake. The manual app stays `scripts/sillage_gui.py`.
"""

from __future__ import annotations


def main() -> None:
    from sillage.auto.window import main as _main

    _main()


if __name__ == "__main__":
    main()
