"""Sillage main window (ADR-0009).

First IHM vertical slice: a controls panel + two tabs — Pass-1 screening (2D, embedded
matplotlib) and Pass-2 detail (3D, embedded pyvistaqt). It reuses the exact rendering of
the headless code (viz.map2d.draw_indicator, viz.volume3d.populate_plotter), so the app and
the saved artefacts look identical.

This slice runs the FAST paths synchronously (geometry-only Pass-1; loading an existing
Pass-2 case). The long WindNinja/OpenFOAM solves will move to a worker thread in the next
increment (ADR-0008 mesh knob + progress/cancel). Pass-1 (triage) and Pass-2 (detail) are
kept as distinct tabs on purpose (ADR-0005).
"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtWidgets
from matplotlib.backends.backend_qtagg import (
    FigureCanvasQTAgg,
    NavigationToolbar2QT,
)
from matplotlib.figure import Figure

from ..config import load_config, resolve_cache_path
from ..flow.windninja import locate_openfoam_case
from ..screening import indicator as ind
from ..screening.pass1 import hourly_indicator
from ..terrain.dem import load_dem
from ..viz import map2d, volume3d
from .jobs import SolveJob

DEFAULT_DEM = "cache/champsaur/ign/champsaur_rgealti_50m_prepared_utm.tif"


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Sillage — leeward turbulence screening")
        self.cfg = load_config()
        self._dem = None

        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        split.addWidget(self._build_controls())
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_pass1_tab(), "Pass 1 — Screening (2D)")
        self.tabs.addTab(self._build_pass2_tab(), "Pass 2 — Detail (3D)")
        split.addWidget(self.tabs)
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)
        self.statusBar().showMessage("Ready")

    # --- UI construction -------------------------------------------------------
    def _build_controls(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(w)

        self.dem_edit = QtWidgets.QLineEdit(str(resolve_cache_path(DEFAULT_DEM, self.cfg)))
        self.wind_dir = QtWidgets.QDoubleSpinBox()
        self.wind_dir.setRange(0.0, 360.0)
        self.wind_dir.setValue(320.0)
        self.wind_spd = QtWidgets.QDoubleSpinBox()
        self.wind_spd.setRange(0.0, 60.0)
        self.wind_spd.setValue(8.0)

        self.btn_geom = QtWidgets.QPushButton("Compute Pass-1 (geometry)")
        self.btn_geom.clicked.connect(self.on_compute_pass1)
        self.btn_mass = QtWidgets.QPushButton("Run WindNinja mass (Pass-1)")
        self.btn_mass.clicked.connect(self.on_run_mass)

        self.case_edit = QtWidgets.QLineEdit("")
        self.case_edit.setPlaceholderText("(auto-detect cached NINJAFOAM_* case)")
        self.btn_load_p2 = QtWidgets.QPushButton("Load Pass-2 case (3D)")
        self.btn_load_p2.clicked.connect(self.on_load_pass2)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        self.btn_cancel = QtWidgets.QPushButton("Cancel")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self.on_cancel)

        form.addRow("DEM:", self.dem_edit)
        form.addRow("Wind FROM (deg):", self.wind_dir)
        form.addRow("Wind speed (m/s):", self.wind_spd)
        form.addRow(self.btn_geom)
        form.addRow(self.btn_mass)
        form.addRow(QtWidgets.QLabel("———"))
        form.addRow("Pass-2 case:", self.case_edit)
        form.addRow(self.btn_load_p2)
        form.addRow(self.progress)
        form.addRow(self.btn_cancel)

        note = QtWidgets.QLabel(map2d.DISCLAIMER)
        note.setWordWrap(True)
        note.setStyleSheet("color: #a33; font-style: italic;")
        form.addRow(note)

        self._run_buttons = [self.btn_geom, self.btn_mass, self.btn_load_p2]
        self._job: SolveJob | None = None
        self._cancelling = False
        w.setMaximumWidth(380)
        return w

    def _build_pass1_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        self.fig = Figure(figsize=(6, 5))
        self.canvas = FigureCanvasQTAgg(self.fig)
        lay.addWidget(NavigationToolbar2QT(self.canvas, w))
        lay.addWidget(self.canvas)
        return w

    def _build_pass2_tab(self) -> QtWidgets.QWidget:
        # The VTK/OpenGL viewport is created lazily (on first Pass-2 use) so the window
        # starts cleanly even without a GL context (headless), and we don't pay VTK init
        # until the 3D view is actually needed.
        w = QtWidgets.QWidget()
        self._p2_widget = w
        self._p2_layout = QtWidgets.QVBoxLayout(w)
        self.plotter = None
        self._p2_placeholder = QtWidgets.QLabel(
            "3D viewport initializes on first use.\nClick “Load Pass-2 case (3D)”."
        )
        self._p2_placeholder.setAlignment(QtCore.Qt.AlignCenter)
        self._p2_layout.addWidget(self._p2_placeholder)
        return w

    def _ensure_plotter(self) -> bool:
        """Create the embedded QtInteractor on demand. Returns True if the 3D view is ready."""
        if self.plotter is not None:
            return True
        try:
            from pyvistaqt import QtInteractor

            plotter = QtInteractor(self._p2_widget)
            if self._p2_placeholder is not None:
                self._p2_layout.removeWidget(self._p2_placeholder)
                self._p2_placeholder.deleteLater()
                self._p2_placeholder = None
            self._p2_layout.addWidget(plotter.interactor)
            self.plotter = plotter
            return True
        except Exception as exc:  # no GL context available
            QtWidgets.QMessageBox.critical(
                self, "3D init failed",
                f"Could not initialize the 3D viewport (OpenGL):\n{exc}",
            )
            return False

    # --- actions ---------------------------------------------------------------
    def on_compute_pass1(self) -> None:
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            dem = load_dem(self.dem_edit.text(), max_domain_km=self.cfg.max_domain_km)
            self._dem = dem
            hazard = ind.hazard_indicator(dem, self.wind_dir.value())
            self.fig.clear()
            ax = self.fig.add_subplot(111)
            im = map2d.draw_indicator(ax, dem, hazard)
            self.fig.colorbar(im, ax=ax, label="leeward hazard indicator (0–1)")
            ax.set_title(f"Pass-1 geometry-only — wind from {self.wind_dir.value():.0f}°")
            self.canvas.draw()
            self.statusBar().showMessage(
                f"Pass-1 geometry on {dem.shape} grid, res {dem.resolution_m:.0f} m"
            )
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Pass-1 error", str(exc))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def on_run_mass(self) -> None:
        """Run the real WindNinja mass solver on a worker thread (progress + cancel)."""
        if self._job is not None:
            return
        cfg = self.cfg
        dem_path = self.dem_edit.text()
        wind_dir = self.wind_dir.value()
        wind_spd = self.wind_spd.value()
        resolution_m = 100.0
        cli = cfg.windninja_cli
        max_km = cfg.max_domain_km
        work = cfg.cache_dir / "champsaur" / "ihm_mass" / (
            f"{wind_dir:.0f}_{wind_spd:.0f}_{resolution_m:.0f}m"
        )

        def fn(on_progress, cancel):  # runs on the worker thread — no Qt here
            dem = load_dem(dem_path, max_domain_km=max_km)
            hazard, _ = hourly_indicator(
                dem=dem, cli=cli, dem_path=dem_path, work_dir=work,
                wind_speed_ms=wind_spd, wind_from_deg=wind_dir, resolution_m=resolution_m,
                force_run=True, on_progress=on_progress, cancel=cancel,
            )
            return dem, hazard, wind_dir

        self._cancelling = False
        self._set_running(True, "WindNinja mass running…")
        job = SolveJob(fn, self)
        job.progress.connect(self._on_job_progress)
        job.finished.connect(self._on_mass_finished)
        job.failed.connect(self._on_job_failed)
        self._job = job
        job.start()

    def on_cancel(self) -> None:
        if self._job is not None:
            self._cancelling = True
            self.statusBar().showMessage("Cancelling…")
            self._job.cancel()

    def _set_running(self, running: bool, msg: str = "") -> None:
        for b in self._run_buttons:
            b.setEnabled(not running)
        self.progress.setVisible(running)
        self.btn_cancel.setVisible(running)
        if running:
            self.progress.setValue(0)
            if msg:
                self.statusBar().showMessage(msg)

    def _finish_job(self, status: str) -> None:
        self._job = None
        self._cancelling = False
        self._set_running(False)
        self.statusBar().showMessage(status)

    def _on_job_progress(self, pct: int, msg: str) -> None:
        self.progress.setValue(pct)
        self.statusBar().showMessage(msg)

    def _on_mass_finished(self, result) -> None:
        dem, hazard, wind_dir = result
        self._dem = dem
        self.fig.clear()
        ax = self.fig.add_subplot(111)
        im = map2d.draw_indicator(ax, dem, hazard)
        self.fig.colorbar(im, ax=ax, label="leeward hazard indicator (0–1)")
        ax.set_title(f"Pass-1 WindNinja mass — wind from {wind_dir:.0f}°")
        self.canvas.draw()
        self._finish_job("Pass-1 mass done")

    def _on_job_failed(self, msg: str) -> None:
        if self._cancelling:
            self._finish_job("Run cancelled")
        else:
            self._finish_job("Run failed")
            QtWidgets.QMessageBox.critical(self, "WindNinja error", msg)

    def on_load_pass2(self) -> None:
        case = self.case_edit.text().strip()
        if not case:
            root = self.cfg.cache_dir / "champsaur" / "pass2"
            found = locate_openfoam_case(root, "", extra_roots=[root])
            if found is not None:
                case = str(found)
                self.case_edit.setText(case)
        if not case:
            QtWidgets.QMessageBox.information(
                self, "No Pass-2 case",
                "No cached NINJAFOAM_* case found. Run a momentum solve first "
                "(scripts/pass2_smoke_test.py or demo_pass2_single.py).",
            )
            return
        if not self._ensure_plotter():
            return

        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            mfd = volume3d.mean_flow_vector(self.wind_dir.value())
            self.plotter.clear()
            volume3d.populate_plotter(self.plotter, case, mfd, show_turbulence=False)
            self.plotter.reset_camera()
            self.tabs.setCurrentWidget(self._p2_widget)
            self.statusBar().showMessage(f"Loaded Pass-2 case {Path(case).name}")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Pass-2 error", str(exc))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
