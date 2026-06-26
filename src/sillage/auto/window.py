"""Auto-mode IHM: select a route + window, validate, watch the run, browse the global 3D wake.

Two tabs:
  1. **Sélection** — a Leaflet IGN map (route corridor, reused `MapTab`) + a multi-day window
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
from . import AutoConfig, cleanup_auto_artifacts, run_auto
from .arome import forecast_window
from .pipeline import (
    DEFAULT_MAX_FEATURES,
    bbox_from_route,
    default_momentum_workers,
    detect_cores,
    momentum_parallel_plan,
)
from .wind import arrows_at_hour, route_wind_series

GREEN = ("QPushButton { font-weight:bold; padding:8px 18px; border-radius:5px;"
         " background:#2d7d2d; color:white; } QPushButton:disabled { background:#a9c7a9; }")


class AutoWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Sillage — mode automatique")
        self.cfg = load_config()
        self._route = None            # [(lat, lon), ...] planned flight route
        self._route_cells = []        # AROME wind along the route: [(lat, lon, series), ...]
        self._wind_job: SolveJob | None = None
        self._prev_window = None      # last (lo, hi) — to tell which créneau handle is moving
        self._active_hour_2d = None   # clock-hour offset of the handle last moved (drives 2D arrows)
        self._job: SolveJob | None = None
        self._result = None
        self._dem = None
        self._plotter = None
        self._rendered = False  # first 3D render resets the camera; later ones keep it
        # Available forecast window (absolute dates) + whether the run will use AROME.
        self._fc = forecast_window(self.cfg.meteofrance_api_key)
        self._cores = detect_cores()
        self._default_workers = default_momentum_workers()
        self._run_started = None
        self._last_pct = 0
        self._eta_s = None            # last ETA (s) from the worker; ticked down between updates
        self._eta_anchor = None       # monotonic time the ETA was last refreshed
        self._elapsed_timer = QtCore.QTimer(self)  # ticks the "écoulé/reste" even between steps
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._update_avancement)
        # Debounce the AROME route-wind fetch: refetch only once the user pauses drawing.
        self._wind_timer = QtCore.QTimer(self)
        self._wind_timer.setSingleShot(True)
        self._wind_timer.setInterval(600)
        self._wind_timer.timeout.connect(self._fetch_route_winds)

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
        self.statusBar().showMessage(
            "Trace ton parcours (clic gauche = point · clic droit = annuler le dernier) — "
            "le corridor s'affiche en direct. Choisis le créneau puis « Valider » pour lancer.")

    # --- tab 1: selection ----------------------------------------------------
    def _build_select_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        self.map_tab = MapTab(mode="route")
        self.map_tab.routeSelected.connect(self._on_route)
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
        self.workers_slider.setValue(self._default_workers)
        self.workers_slider.setFixedWidth(220)
        self.workers_slider.valueChanged.connect(self._on_workers_change)
        wr.addWidget(self.workers_slider)
        self.workers_label = QtWidgets.QLabel("")
        wr.addWidget(self.workers_label)
        wr.addSpacing(16)
        wr.addWidget(QtWidgets.QLabel("Marge corridor :"))
        self.margin_spin = QtWidgets.QDoubleSpinBox()
        self.margin_spin.setRange(0.5, 10.0)
        self.margin_spin.setSingleStep(0.5)
        self.margin_spin.setValue(2.0)
        self.margin_spin.setSuffix(" km")
        self.margin_spin.valueChanged.connect(self._on_margin_change)
        wr.addWidget(self.margin_spin)
        wr.addSpacing(16)
        wr.addWidget(QtWidgets.QLabel("Features max :"))
        self.features_spin = QtWidgets.QSpinBox()
        self.features_spin.setRange(1, 32)
        self.features_spin.setValue(DEFAULT_MAX_FEATURES)
        self.features_spin.setToolTip(
            "Nombre maximum de reliefs candidats analysés en Pass-2 le long du parcours. "
            "Plus de features = meilleure parallélisation (OpenFOAM passe mieux en calculs "
            "séparés qu'en multi-thread), mais plus de calculs au total.")
        self.features_spin.valueChanged.connect(lambda *_: self._refresh_cpu_plan())
        wr.addWidget(self.features_spin)
        wr.addStretch(1)
        lay.addLayout(wr)

        self.cpu_plan_label = QtWidgets.QLabel("")
        self.cpu_plan_label.setWordWrap(True)
        self.cpu_plan_label.setStyleSheet("color:#555;")
        lay.addWidget(self.cpu_plan_label)

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
        self._on_workers_change()
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
        self._refresh_cpu_plan()
        self._refresh_2d_wind(self._active_window_hour(lo, hi))

    def _active_window_hour(self, lo: int, hi: int) -> int:
        """The clock-hour offset of the créneau handle currently being moved (min or max), so the
        2D arrows track the handle the user is dragging. Defaults to the start on first call."""
        prev = self._prev_window
        self._prev_window = (lo, hi)
        hour = lo if prev is None else (hi if hi != prev[1] and lo == prev[0] else lo)
        self._active_hour_2d = hour
        return hour

    def _refresh_2d_wind(self, hour: int) -> None:
        """Redraw the route's AROME arrows on the 2D map for the given clock-hour offset."""
        if self._route_cells:
            self.map_tab.show_wind(arrows_at_hour(self._route_cells, hour))

    # --- AROME route wind (2D arrows + 3D arrows) ----------------------------
    def _fetch_route_winds(self) -> None:
        """Fetch AROME HD wind along the current route over the whole forecast window (one call,
        on a worker thread) so slider/hour scrubbing is then instant."""
        if not self._route or len(self._route) < 2 or self._wind_job is not None:
            if self._route and len(self._route) >= 2:
                self._wind_timer.start()  # a fetch is in flight — try again shortly
            return
        route = list(self._route)
        n_hours = self._fc.end_offset_h + 1

        def fn(on_progress, cancel):
            return tuple(route), route_wind_series(route, n_hours)

        job = SolveJob(fn, self)
        job.finished.connect(self._on_winds_fetched)
        job.failed.connect(lambda _msg: setattr(self, "_wind_job", None))
        self._wind_job = job
        job.start()

    def _on_winds_fetched(self, payload) -> None:
        self._wind_job = None
        route, cells = payload
        if tuple(self._route or ()) != tuple(route):
            self._wind_timer.start()
            return
        self._route_cells = cells or []
        lo, _hi = self._window_hours()
        self._refresh_2d_wind(self._active_hour_2d if self._active_hour_2d is not None else lo)

    def _on_workers_change(self, *_args) -> None:
        self.workers_label.setText(
            f"{self.workers_slider.value()} calcul(s) max / {self._cores} cœurs")
        self._refresh_cpu_plan()

    def _refresh_cpu_plan(self) -> None:
        if not hasattr(self, "cpu_plan_label"):
            return
        lo, hi = self._window_hours()
        hours = max(1, hi - lo)
        feats = self.features_spin.value() if hasattr(self, "features_spin") else DEFAULT_MAX_FEATURES
        max_tasks = max(1, hours * feats)
        requested = self.workers_slider.value()
        requested_plan = momentum_parallel_plan(requested, cores=self._cores)
        estimate = momentum_parallel_plan(requested, cores=self._cores, task_count=max_tasks)
        perfect = [w for w in requested_plan.perfect_workers if w <= self._cores]
        perfect_txt = ", ".join(str(w) for w in perfect) if perfect else "aucune"
        idle = "" if estimate.idle_cores == 0 else f", {estimate.idle_cores} au repos"
        tasks = f"prévision haute : ≤ {feats} features × {hours} h"
        self.cpu_plan_label.setText(
            f"Plan CPU : demandé {requested_plan.workers} calcul(s) max. "
            f"{tasks} ⇒ estimation {estimate.workers} en parallèle × "
            f"{estimate.threads_per_worker} thread(s) = {estimate.used_cores}/{estimate.cores} cœurs"
            f"{idle}. Le log de calcul affichera le plan exact après détection des features. "
            f"Divisions parfaites : {perfect_txt}.")

    def _on_route(self, pts) -> None:
        self._route = list(pts)
        self._route_cells = []
        self.map_tab.show_wind([])
        n = len(self._route)
        ready = "" if n >= 2 else "  (ajoute au moins 2 points)"
        self.info.setText(
            f"Parcours : {n} point(s) · corridor {self.margin_spin.value():.1f} km{ready}")
        if n >= 2:
            self._wind_timer.start()  # (re)fetch AROME along the route once drawing pauses
        self._refresh_cpu_plan()

    def _on_margin_change(self, val: float) -> None:
        self.map_tab.set_margin_km(val)  # redraw the live corridor
        if self._route is not None:
            self._on_route(self._route)

    # --- run feedback --------------------------------------------------------
    def _log(self, msg: str) -> None:
        from datetime import datetime

        self.log.appendPlainText(f"{datetime.now():%H:%M:%S}  {msg}")

    def _update_avancement(self) -> None:
        if self._run_started is None:
            self.avancement.setText("")
            return
        elapsed = time.monotonic() - self._run_started
        if self._eta_s is None or self._eta_anchor is None:
            eta_txt = "?"
        else:  # tick the last worker ETA down between updates so it counts down smoothly
            rem = max(0.0, self._eta_s - (time.monotonic() - self._eta_anchor))
            eta_txt = format_seconds(rem)
        self.avancement.setText(
            f"Avancement {self._last_pct}% · écoulé {format_seconds(elapsed)} · reste ~{eta_txt}")

    # --- run -----------------------------------------------------------------
    def on_validate(self) -> None:
        if self._job is not None:
            return
        if not self._route or len(self._route) < 2:
            QtWidgets.QMessageBox.information(
                self, "Aucun parcours",
                "Trace d'abord ton parcours (clic gauche = point, double-clic = terminer).")
            return
        if not self._route_cells:
            self._fetch_route_winds()  # so the 3D scene can show the route wind too
        lo, hi = self._window_hours()
        margin = self.margin_spin.value()
        bbox = bbox_from_route(self._route, margin)
        wind_source = "arome" if self._fc.source == "AROME" else "open_meteo"
        cfg = AutoConfig(bbox_latlon=bbox, hours=tuple(range(lo, hi)),
                         route_latlon=tuple(self._route), corridor_margin_km=margin,
                         window_start_iso=self._fc.at(lo).isoformat(), wind_source=wind_source,
                         max_features=self.features_spin.value(),
                         momentum_workers=self.workers_slider.value())
        cli, cache = self.cfg.windninja_cli, self.cfg.cache_dir

        def fn(on_progress, cancel):  # worker thread
            return run_auto(cfg, cli=cli, cache_dir=cache, on_progress=on_progress, cancel=cancel)

        self.log.clear()
        self._log(f"Calcul auto — parcours {len(self._route)} pts, corridor {margin:.1f} km · "
                  f"créneau {self._fc.label_at(lo)} → {self._fc.label_at(hi)} · vent {self._fc.source}")
        self._log(f"CPU demandé : {self.workers_slider.value()} calcul(s) simultané(s) max "
                  f"sur {self._cores} cœurs; effectif plafonné par features × heures après criblage.")
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
        self.margin_spin.setEnabled(not running)
        self.progress.setVisible(running)
        self.btn_cancel.setVisible(running)
        if running:
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self._eta_s = None
            self._eta_anchor = None
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
            if self._run_started is not None:  # refresh the ETA anchor from this update
                elapsed = time.monotonic() - self._run_started
                self._eta_s = elapsed * (100 - pct) / pct
                self._eta_anchor = time.monotonic()
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

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._job is not None:
            self._on_cancel()
            QtWidgets.QMessageBox.information(
                self, "Calcul en cours",
                "J'ai demandé l'annulation. Ferme la fenêtre quand le calcul est arrêté.")
            event.ignore()
            return
        self._wind_timer.stop()
        if self._wind_job is not None:
            self._wind_job.cancel()
            if self._wind_job.is_running() and not self._wind_job.wait(3000):
                self.statusBar().showMessage(
                    "Fermeture en attente : récupération du vent en cours…")
                event.ignore()
                return
            self._wind_job = None
        if self._plotter is not None:
            try:
                self._plotter.close()
            except Exception:
                pass
            self._plotter = None
        cleanup_auto_artifacts(self.cfg.cache_dir)
        event.accept()

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
        done = (f"Terminé : {len(result.partition)} features × {len(hours)} h "
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
                from ..viz.volume3d import enable_right_drag_pan
                enable_right_drag_pan(p)  # right-drag = pan (translate)
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

    def _route_winds_utm(self, hour: int):
        """AROME route arrows for ``hour`` as ``[(x, y, speed_ms, from_deg), …]`` in the DEM CRS."""
        if not self._route_cells or self._dem is None:
            return []
        arrows = arrows_at_hour(self._route_cells, hour)
        if not arrows:
            return []
        from rasterio.crs import CRS
        from rasterio.warp import transform as warp_xy

        lons = [a[1] for a in arrows]
        lats = [a[0] for a in arrows]
        xs, ys = warp_xy(CRS.from_epsg(4326), self._dem.crs, lons, lats)
        return [(float(x), float(y), a[2], a[3]) for x, y, a in zip(xs, ys, arrows)]

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
                            crs=self._dem.crs, basemap_source="IGN plan",
                            route_winds=self._route_winds_utm(hour))
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
