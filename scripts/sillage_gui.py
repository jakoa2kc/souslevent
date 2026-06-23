"""Launch the Sillage desktop IHM (ADR-0009: PySide6 + pyvistaqt).

    python scripts/sillage_gui.py
    # or, after `pip install -e .[gui]`:  sillage-gui
"""

from __future__ import annotations

import sys


def main() -> None:
    from PySide6 import QtCore, QtWidgets

    # WebEngine (map tab) and VTK (3D viewport) both use OpenGL — share contexts. Must be set
    # before the QApplication is created.
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

    from sillage.app.main_window import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Sillage")
    # French locale for Qt's built-in strings (dialog Yes/No, toolbar tooltips, etc.).
    translator = QtCore.QTranslator()
    tpath = QtCore.QLibraryInfo.path(QtCore.QLibraryInfo.LibraryPath.TranslationsPath)
    if translator.load("qtbase_fr", tpath):
        app.installTranslator(translator)
        app._fr_translator = translator  # keep a reference so it isn't garbage-collected
    win = MainWindow()
    win.resize(1320, 840)
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
