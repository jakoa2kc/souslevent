"""Auto-mode IHM: select a zone + window, validate, watch the run, browse the global 3D wake.

Two tabs:
  1. **Sélection** — a Leaflet IGN map (rectangle AOI, reused `MapTab`) + a multi-day window
     range slider + « Valider » → launches `run_auto` on a worker thread (`SolveJob`); the status
     bar shows a progress bar + ETA.
  2. **Rendu 3D** — an embedded `QtInteractor` (terrain-locked rotation) + an hour slider over the
     computed range → `auto.scene.populate_auto_scene` for the selected hour.

This is the integration layer over the tested `auto` engine; it reuses the existing widgets and
patterns from `sillage.app`. Launch: ``python -m sillage.auto.window`` (or embed the widget).
See docs/10_auto_pipeline.md / ADR-0022.
"""

from __future__ import annotations

import time

from PySide6 import QtCore, QtWidgets
from superqt import QRangeSlider

from ..app.jobs import SolveJob
from ..app.map_tab import MapTab
from ..config import load_config
from ..terrain.dem import load_dem
from ..timing import format_seconds
from . import AutoConfig, run_auto
from .arome import forecast_window
from .pipeline import detect_cores

GREEN = ("QPushButton { font-weight:bold; padding:8px 18px; border-radius:5px;"
         " background:#2d7d2d; color:white; } QPushButton:disabled { background:#a9c7a9; }")


class AutoWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Sillage — mode automatique")
        self.cfg = load_config()
        self.selected_bbox = None     # (s, w, n, e)
        self._job: SolveJob | None = None
        self._result = None
        self._dem = None
        self._plotter = None
        self._rendered = False  # first 3D render resets the camera; later ones keep it
        # Available forecast window (absolute dates) + whether the run will use AROME.
        self._fc = forecast_window(self.cfg.meteofrance_api_key)
        self._cores = detect_cores()  # default concurrency = one momentum solve per core
        self._run_started = None
        self._last_pct = 0
        self._elapsed_timer = QtCore.QTimer(self)  # ticks the "écoulé/reste" even between steps
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._update_avancement)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.setVisible(False)
        self.btn_cancel = QtWidgets.QPushButton("Annuler")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self._on_cancel)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().addPermanentWidget(self.btn_cancel)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_select_tab(), "Sélection zone + créneau")
        self.tabs.addTab(self._build_render_tab(), "Rendu 3D global")
        self.setCentralWidget(self.tabs)
        self.statusBar().showMessage("Trace une zone, choisis le créneau, puis « Valider ».")

    # --- tab 1: selection ----------------------------------------------------
    def _build_select_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        self.map_tab = MapTab()
        self.map_tab.aoiSelected.connect(self._on_aoi)
        lay.addWidget(self.map_tab, stretch=1)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Créneau :"))
        self.window_slider = QRangeSlider(QtCore.Qt.Horizontal)
        lo0, hi0 = self._fc.start_offset_h, self._fc.end_offset_h
        self.window_slider.setRange(lo0, hi0)
        self.window_slider.setValue((min(lo0 + 1, hi0 - 1), min(lo0 + 6, hi0)))
        self.window_slider.valueChanged.connect(self._on_window_change)
        row.addWidget(self.window_slider, stretch=1)
        self.window_label = QtWidgets.QLabel("")
        row.addWidget(self.window_label)
        self.btn_validate = QtWidgets.QPushButton("✓  Valider — lancer le calcul auto")
        self.btn_validate.setStyleSheet(GREEN)
        self.btn_validate.clicked.connect(self.on_validate)
        row.addWidget(self.btn_validate)
        lay.addLayout(row)
        lay.addWidget(self._make_tick_strip())  # absolute-date graduations
        self.avail_label = QtWidgets.QLabel(f"Prévision : {self._fc.source} · {self._fc.note}")
        self.avail_label.setStyleSheet("color:#555;")
        lay.addWidget(self.avail_label)

        wr = QtWidgets.QHBoxLayout()
        wr.addWidget(QtWidgets.QLabel("Calculs simultanés :"))
        self.workers_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.workers_slider.setRange(1, max(1, self._cores))
        self.workers_slider.setValue(self._cores)
        self.workers_slider.setFixedWidth(220)
        self.workers_slider.valueChanged.connect(self._on_workers_change)
        wr.addWidget(self.workers_slider)
        self.workers_label = QtWidgets.QLabel("")
        wr.addWidget(self.workers_label)
        wr.addStretch(1)
        lay.addLayout(wr)
        self._on_workers_change()

        self.info = QtWidgets.QLabel("")
        self.info.setStyleSheet("color:#555;")
        lay.addWidget(self.info)

        # Live run feedback: a bold % / elapsed / remaining line + a scrolling step log.
        self.avancement = QtWidgets.QLabel("")
        self.avancement.setStyleSheet("font-weight:bold; color:#2d7d2d;")
        lay.addWidget(self.avancement)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        self.log.setFixedHeight(120)
        self.log.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        lay.addWidget(self.log)
        self._on_window_change()
        return w

    def _make_tick_strip(self, n: int = 6) -> QtWidgets.QWidget:
        """A row of evenly-spaced ABSOLUTE date/hour labels under the window slider."""
        w = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(w)
        row.setContentsMargins(56, 0, 8, 0)  # ~align under the slider (after the "Créneau :" label)
        row.setSpacing(0)
        lo0, hi0 = self._fc.start_offset_h, self._fc.end_offset_h
        for k in range(n):
            off = round(lo0 + k / (n - 1) * (hi0 - lo0))
            lab = QtWidgets.QLabel(self._fc.label_at(off))
            lab.setStyleSheet("color:#888; font-size:10px;")
            lab.setAlignment(QtCore.Qt.AlignLeft if k == 0
                             else QtCore.Qt.AlignRight if k == n - 1 else QtCore.Qt.AlignHCenter)
            row.addWidget(lab, 1)
        return w

    def _build_render_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        self._render_tab = w
        lay = QtWidgets.QVBoxLayout(w)
        self._viewport = QtWidgets.QWidget()
        self._viewport_lay = QtWidgets.QVBoxLayout(self._viewport)
        self._placeholder = QtWidgets.QLabel(
            "Le rendu 3D apparaît ici quand le calcul auto est terminé.")
        self._placeholder.setAlignment(QtCore.Qt.AlignCenter)
        self._viewport_lay.addWidget(self._placeholder)
        lay.addWidget(self._viewport, stretch=1)

        srow = QtWidgets.QHBoxLayout()
        srow.addWidget(QtWidgets.QLabel("Heure :"))
        self.hour_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.hour_slider.setEnabled(False)
        self.hour_slider.valueChanged.connect(self._on_hour_change)
        self.hour_label = QtWidgets.QLabel("")
        srow.addWidget(self.hour_slider, stretch=1)
        srow.addWidget(self.hour_label)
        lay.addLayout(srow)
        return w

    # --- window helpers ------------------------------------------------------
    def _window_hours(self) -> tuple[int, int]:
        lo, hi = self.window_slider.value()
        lo, hi = int(lo), int(hi)
        return lo, max(hi, lo + 1)

    def _on_window_change(self, *_args) -> None:
        lo, hi = self._window_hours()
        self.window_label.setText(
            f"{self._fc.label_at(lo)} → {self._fc.label_at(hi)}  ({hi - lo} h)")

    def _on_workers_change(self, *_args) -> None:
        self.workers_label.setText(f"{self.workers_slider.value()} / {self._cores} cœurs")

    def _on_aoi(self, s, w, n, e) -> None:
        self.selected_bbox = (s, w, n, e)
        self.info.setText(f"Zone : S {s:.3f}, O {w:.3f} → N {n:.3f}, E {e:.3f}")

    # --- run feedback --------------------------------------------------------
    def _log(self, msg: str) -> None:
        from datetime import datetime

        self.log.appendPlainText(f"{datetime.now():%H:%M:%S}  {msg}")

    def _update_avancement(self) -> None:
        if self._run_started is None:
            self.avancement.setText("")
            return
        elapsed = time.monotonic() - self._run_started
        pct = self._last_pct
        eta = elapsed * (100 - pct) / pct if pct > 0 else None
        eta_txt = "?" if eta is None else format_seconds(eta)
        self.avancement.setText(
            f"Avancement {pct}% · écoulé {format_seconds(elapsed)} · reste ~{eta_txt}")

    # --- run -----------------------------------------------------------------
    def on_validate(self) -> None:
        if self._job is not None:
            return
        if self.selected_bbox is None:
            QtWidgets.QMessageBox.information(self, "Aucune zone", "Trace d'abord un rectangle.")
            return
        lo, hi = self._window_hours()
        wind_source = "arome" if self._fc.source == "AROME" else "open_meteo"
        cfg = AutoConfig(bbox_latlon=self.selected_bbox, hours=tuple(range(lo, hi)),
                         window_start_iso=self._fc.at(lo).isoformat(), wind_source=wind_source,
                         momentum_workers=self.workers_slider.value())
        cli, cache = self.cfg.windninja_cli, self.cfg.cache_dir

        def fn(on_progress, cancel):  # worker thread
            return run_auto(cfg, cli=cli, cache_dir=cache, on_progress=on_progress, cancel=cancel)

        self.log.clear()
        self._log(f"Calcul auto — créneau {self._fc.label_at(lo)} → {self._fc.label_at(hi)} "
                  f"· vent {self._fc.source}")
        self._run_started = time.monotonic()
        self._last_pct = 0
        self._set_running(True)
        job = SolveJob(fn, self)
        job.progress.connect(self._on_progress)
        job.finished.connect(self._on_finished)
        job.failed.connect(self._on_failed)
        self._job = job
        job.start()

    def _set_running(self, running: bool) -> None:
        self.btn_validate.setEnabled(not running)
        self.workers_slider.setEnabled(not running)
        self.progress.setVisible(running)
        self.btn_cancel.setVisible(running)
        if running:
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self._elapsed_timer.start()
        else:
            self._elapsed_timer.stop()
            self._run_started = None

    def _on_progress(self, pct: int, msg: str) -> None:
        if pct <= 0:
            self.progress.setRange(0, 0)  # indeterminate while the DEM/first solve warms up
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(pct)
            self._last_pct = pct
        self._log(msg)
        self._update_avancement()
        self.statusBar().showMessage(msg)

    def _on_cancel(self) -> None:
        if self._job is not None:
            self.statusBar().showMessage("Annulation…")
            self._log("Annulation demandée…")
            self._job.cancel()

    def _on_failed(self, msg: str) -> None:
        self._job = None
        self._set_running(False)
        self._log(f"ÉCHEC : {msg.splitlines()[0] if msg else ''}")
        self.statusBar().showMessage("Échec / annulé")
        QtWidgets.QMessageBox.critical(self, "Erreur calcul auto", msg)

    def _on_finished(self, result) -> None:
        self._job = None
        self._set_running(False)
        self._last_pct = 100
        self._result = result
        try:
            self._dem = load_dem(result.dem_path, max_domain_km=200.0)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "MNT", f"MNT illisible : {exc}")
            return
        hours = result.hours
        nfail = len(result.failures)
        done = (f"Terminé : {len(result.partition)} zones × {len(hours)} h "
                f"({len(result.cases)} cas{f', {nfail} échec(s)' if nfail else ''}) — "
                f"{result.timings_summary}")
        self.statusBar().showMessage(done)
        self._log(done)
        self.avancement.setText(f"Terminé — {result.timings_summary}")
        if not hours:
            return
        self.hour_slider.blockSignals(True)
        self.hour_slider.setMinimum(0)
        self.hour_slider.setMaximum(len(hours) - 1)
        self.hour_slider.setValue(0)
        self.hour_slider.setEnabled(True)
        self.hour_slider.blockSignals(False)
        self.tabs.setCurrentWidget(self._render_tab)
        self._render_hour(0)

    # --- 3D render -----------------------------------------------------------
    def _ensure_plotter(self) -> bool:
        if self._plotter is not None:
            return True
        try:
            from pyvistaqt import QtInteractor
            p = QtInteractor(self._viewport)
            try:
                p.enable_terrain_style(mouse_wheel_zooms=True, shift_pans=True)
            except Exception:
                pass
            if self._placeholder is not None:
                self._viewport_lay.removeWidget(self._placeholder)
                self._placeholder.deleteLater()
                self._placeholder = None
            self._viewport_lay.addWidget(p.interactor)
            self._plotter = p
            return True
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Échec init 3D", str(exc))
            return False

    def _on_hour_change(self, idx: int) -> None:
        self._render_hour(int(idx))

    def _render_hour(self, idx: int) -> None:
        if self._result is None or self._dem is None or not self._ensure_plotter():
            return
        from .scene import populate_auto_scene

        hours = self._result.hours
        if not (0 <= idx < len(hours)):
            return
        hour = hours[idx]
        self.hour_label.setText(f"{hour:02d}h")
        cam = self._plotter.camera_position if self._rendered else None
        self._plotter.clear()
        populate_auto_scene(self._plotter, self._dem, self._result.cases_for_hour(hour),
                            crs=self._dem.crs, basemap_source="IGN plan")
        if cam is not None:  # keep the viewpoint across hour scrubs
            self._plotter.camera_position = cam
        else:
            self._plotter.reset_camera()
            self._rendered = True


def main() -> None:  # pragma: no cover
    import sys

    # WebEngine (map tab) + VTK (3D viewport) share OpenGL — must be set BEFORE the QApplication.
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Sillage (auto)")
    translator = QtCore.QTranslator()
    tpath = QtCore.QLibraryInfo.path(QtCore.QLibraryInfo.LibraryPath.TranslationsPath)
    if translator.load("qtbase_fr", tpath):
        app.installTranslator(translator)
        app._fr_translator = translator  # keep a reference so it isn't garbage-collected
    win = AutoWindow()
    win.resize(1200, 850)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":  # pragma: no cover
    main()
