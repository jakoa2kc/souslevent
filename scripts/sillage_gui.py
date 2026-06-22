"""Launch the Sillage desktop IHM (ADR-0009: PySide6 + pyvistaqt).

    python scripts/sillage_gui.py
    # or, after `pip install -e .[gui]`:  sillage-gui
"""

from __future__ import annotations

import sys


def main() -> None:
    from PySide6 import QtWidgets

    from sillage.app.main_window import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.resize(1320, 840)
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
