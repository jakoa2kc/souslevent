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
    METRICS = ("rotor", "horizontal", "vertical", "turbulence")
    METRICS_LABELS = ("Rotor (flux inversé)", "Vitesse horizontale (% vent)",
                      "Vitesse verticale (m/s)", "Turbulence")

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
        self._rotor_cache = {}   # (zone, hour) -> rotor mesh, so hour scrubs don't re-read cases
        self._rendered = False  # first 3D render resets the camera; later ones keep it
        self._opacity = 0.5      # rotor volume opacity (slider-controlled, see inside the thickness)
        self._metric = "rotor"          # 3D colour metric (default rotor)
        self._height_max_m = 300.0      # top of the height colour scale (AGL, m) — 2-D metrics
        # per-metric colour-scale max (display units) and volume floor (display units)
        self._scale_max = {"rotor": 15.0, "horizontal": 100.0, "vertical": 3.0, "turbulence": 30.0}
        self._vol_floor = {"horizontal": 50.0, "vertical": 1.0, "turbulence": 20.0}
        self._tex_cache = {}     # cached basemap textures so re-renders don't re-fetch tiles
        self._last_cfg = None    # AutoConfig of the shown result (for save)
        self._hour_labels = None  # {hour: absolute-date label} — from the run day, kept for reopen
        self._loaded_dir = None   # temp dir holding an opened .sillage bundle (cleaned on close)
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
        self._update_legend()  # show the 2-D rotor legend from the start
        self._set_wind_legend()  # static continuous wind-speed colourbar
        self.statusBar().showMessage(
            "Trace ton parcours (clic gauche = point · clic droit = annuler · « ＋ » = nouveau "
            "segment, pour sauter une vallée). Choisis le créneau puis « Valider » pour lancer.")

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

        pr = QtWidgets.QHBoxLayout()
        self.pave_check = QtWidgets.QCheckBox("Pavage aveugle (Pass-2 partout, sans criblage)")
        self.pave_check.setChecked(True)
        self.pave_check.setToolTip(
            "Calcule un secteur momentum tous les X km le long du parcours, sans détection de "
            "relief : couvre tout le trajet (plus de calculs). Décoché = détection de reliefs.")
        self.pave_check.toggled.connect(self._on_pave_toggle)
        pr.addWidget(self.pave_check)
        pr.addSpacing(12)
        pr.addWidget(QtWidgets.QLabel("Pas secteurs :"))
        self.step_spin = QtWidgets.QDoubleSpinBox()
        self.step_spin.setRange(0.5, 5.0)
        self.step_spin.setSingleStep(0.5)
        self.step_spin.setValue(1.5)
        self.step_spin.setSuffix(" km")
        self.step_spin.valueChanged.connect(lambda *_: self._refresh_cpu_plan())
        pr.addWidget(self.step_spin)
        pr.addSpacing(12)
        pr.addWidget(QtWidgets.QLabel("Topo :"))
        self.topo_combo = QtWidgets.QComboBox()
        self.topo_combo.addItems(["5 m", "10 m", "25 m"])
        self.topo_combo.setToolTip("Résolution du MNT. 5 m = max détail mais fetch IGN plus lourd "
                                   "(utilise des trajets courts pour les premiers tests).")
        pr.addWidget(self.topo_combo)
        pr.addStretch(1)
        lay.addLayout(pr)

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
        self._on_pave_toggle(self.pave_check.isChecked())
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
        outer = QtWidgets.QHBoxLayout(w)
        lay = QtWidgets.QVBoxLayout()
        self._viewport = QtWidgets.QWidget()
        self._viewport_lay = QtWidgets.QVBoxLayout(self._viewport)
        self._placeholder = QtWidgets.QLabel(
            "Le rendu 3D apparaît ici quand le calcul auto est terminé.")
        self._placeholder.setAlignment(QtCore.Qt.AlignCenter)
        self._viewport_lay.addWidget(self._placeholder)
        lay.addWidget(self._viewport, stretch=1)

        srow = QtWidgets.QHBoxLayout()
        self.btn_open = QtWidgets.QPushButton("📂 Ouvrir")
        self.btn_open.setToolTip("Ouvrir un résultat sauvegardé (.sillage)")
        self.btn_open.clicked.connect(self._on_open)
        srow.addWidget(self.btn_open)
        self.btn_save = QtWidgets.QPushButton("💾 Sauvegarder")
        self.btn_save.setToolTip("Sauvegarder le résultat affiché (zones sous le vent + parcours "
                                 "+ paramètres) dans un fichier .sillage")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._on_save)
        srow.addWidget(self.btn_save)
        srow.addSpacing(16)
        srow.addWidget(QtWidgets.QLabel("Créneau :"))
        self.hour_start_label = QtWidgets.QLabel("")
        self.hour_start_label.setStyleSheet("color:#888; font-size:10px;")
        srow.addWidget(self.hour_start_label)
        self.hour_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.hour_slider.setEnabled(False)
        self.hour_slider.valueChanged.connect(self._on_hour_change)
        self.hour_end_label = QtWidgets.QLabel("")
        self.hour_end_label.setStyleSheet("color:#888; font-size:10px;")
        srow.addWidget(self.hour_slider, stretch=1)
        srow.addWidget(self.hour_end_label)
        self.hour_label = QtWidgets.QLabel("")
        self.hour_label.setStyleSheet("font-weight:bold;")
        self.hour_label.setMinimumWidth(150)
        srow.addWidget(self.hour_label)
        srow.addSpacing(16)
        srow.addWidget(QtWidgets.QLabel("Opacité :"))
        self.opacity_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.opacity_slider.setRange(5, 100)
        self.opacity_slider.setValue(int(self._opacity * 100))
        self.opacity_slider.setFixedWidth(120)
        self.opacity_slider.setToolTip(
            "Transparence des volumes rotor — baisse pour voir l'intérieur de l'épaisseur.")
        self.opacity_slider.valueChanged.connect(self._on_opacity_change)
        srow.addWidget(self.opacity_slider)
        lay.addLayout(srow)
        outer.addLayout(lay, stretch=1)

        # Right panel: metric choice + the 2-D legend (height × intensity) + adjustable upper
        # thresholds (applied on a button, so spinbox edits don't trigger a reload each step).
        right = QtWidgets.QVBoxLayout()
        mrow = QtWidgets.QFormLayout()
        self.metric_combo = QtWidgets.QComboBox()
        self.metric_combo.addItems(self.METRICS_LABELS)
        self.metric_combo.setCurrentIndex(self.METRICS.index(self._metric))
        self.metric_combo.setToolTip(
            "Grandeur visualisée dans le volume sous le vent :\n"
            "• Rotor — flux inversé (recirculation)\n"
            "• Vitesse horizontale — % du vent amont (rouge = rotor, bleu = plein vent)\n"
            "• Vitesse verticale — m/s (vert = ascendance, rouge = dégueulante)\n"
            "• Turbulence — intensité de turbulence")
        self.metric_combo.currentIndexChanged.connect(self._on_metric_change)
        mrow.addRow("Représentation :", self.metric_combo)
        right.addLayout(mrow)
        self.legend_label = QtWidgets.QLabel()
        self.legend_label.setFixedWidth(250)
        self.legend_label.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignHCenter)
        right.addWidget(self.legend_label)
        form = QtWidgets.QFormLayout()
        self.height_max_spin = QtWidgets.QSpinBox()
        self.height_max_spin.setRange(50, 3000)
        self.height_max_spin.setSingleStep(50)
        self.height_max_spin.setValue(int(self._height_max_m))
        self.height_max_spin.setSuffix(" m")
        self.height_max_spin.setToolTip("Hauteur sol au sommet de l'échelle de couleur (violet/bleu).")
        self.height_max_spin.valueChanged.connect(self._on_spin_change)
        self.intensity_max_spin = QtWidgets.QDoubleSpinBox()
        self.intensity_max_spin.valueChanged.connect(self._on_spin_change)
        self.ti_floor_spin = QtWidgets.QDoubleSpinBox()  # range/units set per metric below
        self.ti_floor_spin.setValue(20.0)
        self.ti_floor_spin.valueChanged.connect(self._on_floor_change)
        form.addRow("Échelle max :", self.intensity_max_spin)
        form.addRow("Seuil volume :", self.ti_floor_spin)
        form.addRow("Hauteur max :", self.height_max_spin)
        right.addLayout(form)
        self.btn_apply_scale = QtWidgets.QPushButton("Appliquer l'échelle")
        self.btn_apply_scale.setToolTip("Recalcule le rendu 3D avec les seuils ci-dessus.")
        self.btn_apply_scale.clicked.connect(self._on_apply_scale)
        right.addWidget(self.btn_apply_scale)
        self.wind_legend_label = QtWidgets.QLabel()
        self.wind_legend_label.setAlignment(QtCore.Qt.AlignHCenter)
        right.addWidget(self.wind_legend_label)
        right.addStretch(1)
        outer.addLayout(right)
        self._apply_metric_to_spin()  # set the intensity spin's units/range for the default metric
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
        segs = [list(s) for s in (self._route or []) if len(s) >= 2]
        if not segs or self._wind_job is not None:
            if segs:
                self._wind_timer.start()  # a fetch is in flight — try again shortly
            return
        n_hours = self._fc.end_offset_h + 1

        def fn(on_progress, cancel):
            cells = []
            for s in segs:  # one wind series per segment so gaps carry no arrows
                cells += route_wind_series(s, n_hours)
            return segs, cells

        job = SolveJob(fn, self)
        job.finished.connect(self._on_winds_fetched)
        job.failed.connect(lambda _msg: setattr(self, "_wind_job", None))
        self._wind_job = job
        job.start()

    def _on_winds_fetched(self, payload) -> None:
        self._wind_job = None
        segs, cells = payload

        def _norm(ss):
            return tuple(tuple(map(tuple, s)) for s in ss)

        current = [list(s) for s in (self._route or []) if len(s) >= 2]
        if _norm(current) != _norm(segs):  # route changed while fetching -> refetch
            self._wind_timer.start()
            return
        self._route_cells = cells or []
        lo, _hi = self._window_hours()
        self._refresh_2d_wind(self._active_hour_2d if self._active_hour_2d is not None else lo)

    def _on_workers_change(self, *_args) -> None:
        self.workers_label.setText(
            f"{self.workers_slider.value()} calcul(s) max / {self._cores} cœurs")
        self._refresh_cpu_plan()

    def _on_pave_toggle(self, checked: bool) -> None:
        if hasattr(self, "features_spin"):
            self.features_spin.setEnabled(not checked)  # feature count is irrelevant when paving
        if hasattr(self, "step_spin"):
            self.step_spin.setEnabled(checked)
        self._refresh_cpu_plan()

    def _route_length_km(self) -> float:
        import math

        tot = 0.0
        for seg in (self._route or []):  # sum each segment (gaps between segments don't count)
            for (la0, lo0), (la1, lo1) in zip(seg, seg[1:]):
                ml = (la0 + la1) / 2.0
                dx = (lo1 - lo0) * 111.0 * math.cos(math.radians(ml))
                dy = (la1 - la0) * 111.0
                tot += math.hypot(dx, dy)
        return tot

    def _refresh_cpu_plan(self) -> None:
        if not hasattr(self, "cpu_plan_label"):
            return
        lo, hi = self._window_hours()
        hours = max(1, hi - lo)
        if getattr(self, "pave_check", None) is not None and self.pave_check.isChecked():
            step_km = self.step_spin.value()
            rlen = self._route_length_km()
            feats = max(1, int(rlen / max(0.1, step_km)) + 1) if rlen > 0 else 1
            tasks = (f"pavage : ~{feats} secteurs (trajet {rlen:.1f} km / pas {step_km:.1f} km) "
                     f"× {hours} h")
        else:
            feats = self.features_spin.value() if hasattr(self, "features_spin") else DEFAULT_MAX_FEATURES
            tasks = f"≤ {feats} features × {hours} h"
        max_tasks = max(1, hours * feats)
        requested = self.workers_slider.value()
        requested_plan = momentum_parallel_plan(requested, cores=self._cores)
        estimate = momentum_parallel_plan(requested, cores=self._cores, task_count=max_tasks)
        perfect = [w for w in requested_plan.perfect_workers if w <= self._cores]
        perfect_txt = ", ".join(str(w) for w in perfect) if perfect else "aucune"
        idle = "" if estimate.idle_cores == 0 else f", {estimate.idle_cores} au repos"
        self.cpu_plan_label.setText(
            f"Plan CPU : demandé {requested_plan.workers} calcul(s) max. "
            f"{tasks} ⇒ estimation {estimate.workers} en parallèle × "
            f"{estimate.threads_per_worker} thread(s) = {estimate.used_cores}/{estimate.cores} cœurs"
            f"{idle}. Le log de calcul affichera le plan exact après détection des features. "
            f"Divisions parfaites : {perfect_txt}.")

    def _on_route(self, segments) -> None:
        self._route = [list(s) for s in segments]  # list of segments [(lat, lon), ...]
        self._route_cells = []
        self.map_tab.show_wind([])
        nseg = len(self._route)
        npts = sum(len(s) for s in self._route)
        ready = "" if any(len(s) >= 2 for s in self._route) else "  (ajoute au moins 2 points)"
        self.info.setText(
            f"Parcours : {nseg} segment(s), {npts} point(s) · "
            f"corridor {self.margin_spin.value():.1f} km{ready}")
        if any(len(s) >= 2 for s in self._route):
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
        segs = [list(s) for s in (self._route or []) if len(s) >= 2]
        if not segs:
            QtWidgets.QMessageBox.information(
                self, "Aucun parcours",
                "Trace d'abord ton parcours (clic gauche = point, double-clic = terminer).")
            return
        if not self._route_cells:
            self._fetch_route_winds()  # so the 3D scene can show the route wind too
        lo, hi = self._window_hours()
        margin = self.margin_spin.value()
        flat = [p for s in segs for p in s]                  # all points -> DEM extent
        bbox = bbox_from_route(flat, margin)
        wind_source = "arome" if self._fc.source == "AROME" else "open_meteo"
        pave = self.pave_check.isChecked()
        topo_res = (5.0, 10.0, 25.0)[self.topo_combo.currentIndex()]
        cfg = AutoConfig(bbox_latlon=bbox, hours=tuple(range(lo, hi)),
                         route_latlon=tuple(flat), corridor_margin_km=margin,
                         route_segments=tuple(tuple(s) for s in segs),  # gaps between segs not paved
                         window_start_iso=self._fc.at(lo).isoformat(), wind_source=wind_source,
                         max_features=self.features_spin.value(),
                         domain_mode="corridor" if pave else "features",
                         target_res_m=topo_res, tile_step_m=self.step_spin.value() * 1000.0,
                         momentum_workers=self.workers_slider.value())
        self._last_cfg = cfg  # remember for "Sauvegarder"
        cli, cache = self.cfg.windninja_cli, self.cfg.cache_dir

        def fn(on_progress, cancel):  # worker thread
            return run_auto(cfg, cli=cli, cache_dir=cache, on_progress=on_progress, cancel=cancel)

        self.log.clear()
        self._log(f"Calcul auto — {len(segs)} segment(s), {len(flat)} pts, corridor {margin:.1f} km · "
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
        self._cleanup_loaded()
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
        self._hour_labels = {h: self._fc.label_at(h) for h in hours}  # keep this day's labels
        self._show_result_hours(hours)

    def _show_result_hours(self, hours) -> None:
        """Set up the hour slider (absolute-date labels, disabled for a single créneau) and render
        the first hour. Shared by a finished run and an opened ``.sillage`` result."""
        self._rotor_cache = {}
        self.hour_slider.blockSignals(True)
        self.hour_slider.setMinimum(0)
        self.hour_slider.setMaximum(max(0, len(hours) - 1))
        self.hour_slider.setValue(0)
        self.hour_slider.setEnabled(len(hours) > 1)  # nothing to scrub for a single créneau
        self.hour_slider.blockSignals(False)
        self.hour_start_label.setText(self._label_for_hour(hours[0]))
        self.hour_end_label.setText(self._label_for_hour(hours[-1]))
        self.btn_save.setEnabled(True)
        self.tabs.setCurrentWidget(self._render_tab)
        self._render_hour(0)

    # --- save / open results -------------------------------------------------
    def _on_save(self) -> None:
        if self._result is None or self._last_cfg is None:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Sauvegarder le résultat", "", "Résultat Sillage (*.sillage)")
        if not path:
            return
        from .store import save_result

        labels = self._hour_labels or {h: self._label_for_hour(h) for h in self._result.hours}
        try:
            out = save_result(path, self._result, cfg=self._last_cfg, hour_labels=labels,
                              route_cells=self._route_cells)
        except Exception as exc:  # pragma: no cover - surfaced to the UI
            QtWidgets.QMessageBox.critical(self, "Sauvegarde", str(exc))
            return
        self.statusBar().showMessage(f"Sauvegardé : {out}")
        self._log(f"Résultat sauvegardé → {out}")

    def _on_open(self) -> None:
        if self._job is not None:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Ouvrir un résultat", "", "Résultat Sillage (*.sillage)")
        if not path:
            return
        import tempfile
        from pathlib import Path

        from .store import load_result

        self._cleanup_loaded()
        dest = Path(tempfile.mkdtemp(prefix="sillage_open_"))
        try:
            loaded = load_result(path, dest)
            self._dem = load_dem(loaded.result.dem_path, max_domain_km=200.0)
        except Exception as exc:
            import shutil

            shutil.rmtree(str(dest), ignore_errors=True)
            QtWidgets.QMessageBox.critical(self, "Ouverture", str(exc))
            return
        self._loaded_dir = dest
        self._result = loaded.result
        self._hour_labels = loaded.hour_labels
        self._route = [list(s) for s in loaded.route_segments]
        self._route_cells = loaded.route_cells or []  # the RUN's winds (not today's) for the arrows
        self._last_cfg = self._cfg_from_dict(loaded.config, loaded.result.hours)
        self._restore_controls(loaded.config)
        self._rendered = False  # fit the camera to the opened scene
        hours = loaded.result.hours
        self._log(f"Ouvert : {Path(path).name} — {len(loaded.result.cases)} cas, "
                  f"{len(hours)} h, mode {loaded.config.get('domain_mode', '?')}")
        self.statusBar().showMessage(f"Ouvert : {Path(path).name}")
        if hours:
            self._show_result_hours(hours)

    def _cfg_from_dict(self, c: dict, hours):
        try:
            return AutoConfig(
                bbox_latlon=tuple(c.get("bbox_latlon", (0.0, 0.0, 0.0, 0.0))),
                hours=tuple(c.get("hours", hours)),
                route_latlon=tuple(tuple(p) for p in c.get("route_latlon", [])),
                route_segments=tuple(tuple(tuple(p) for p in seg)
                                     for seg in c.get("route_segments", [])),
                corridor_margin_km=float(c.get("corridor_margin_km", 2.0)),
                window_start_iso=c.get("window_start_iso", ""),
                target_res_m=float(c.get("target_res_m", 10.0)),
                max_features=int(c.get("max_features", DEFAULT_MAX_FEATURES)),
                domain_mode=c.get("domain_mode", "features"),
                tile_step_m=float(c.get("tile_step_m", 1500.0)),
                mesh_count=int(c.get("mesh_count", 300_000)),
                iterations=int(c.get("iterations", 300)),
                wind_source=c.get("wind_source", "open_meteo"))
        except Exception:
            return None

    def _restore_controls(self, c: dict) -> None:
        try:
            self.margin_spin.setValue(float(c.get("corridor_margin_km", 2.0)))
            self.features_spin.setValue(int(c.get("max_features", DEFAULT_MAX_FEATURES)))
            self.step_spin.setValue(float(c.get("tile_step_m", 1500.0)) / 1000.0)
            self.pave_check.setChecked(c.get("domain_mode") == "corridor")
            self.topo_combo.setCurrentIndex({5.0: 0, 10.0: 1, 25.0: 2}.get(
                float(c.get("target_res_m", 10.0)), 1))
        except Exception:
            pass

    def _cleanup_loaded(self) -> None:
        if self._loaded_dir is not None:
            import shutil

            shutil.rmtree(str(self._loaded_dir), ignore_errors=True)
            self._loaded_dir = None

    # --- 3D render -----------------------------------------------------------
    def _ensure_plotter(self) -> bool:
        if self._plotter is not None:
            return True
        try:
            from pyvistaqt import QtInteractor
            p = QtInteractor(self._viewport)
            try:
                p.enable_terrain_style(mouse_wheel_zooms=True, shift_pans=True)
                from ..viz.volume3d import enable_right_drag_pan, enable_wind_arrow_autoscale
                enable_right_drag_pan(p)            # right-drag = pan (translate)
                enable_wind_arrow_autoscale(p)      # wind arrows stay ~constant on screen on zoom
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

    def _native_intensity_max(self) -> float:
        """Colour-scale max in the units ``_add_rotor`` expects for the active metric."""
        m, v = self._metric, self._scale_max[self._metric]
        if m == "rotor":
            return v / 3.6        # km/h → m/s
        if m == "turbulence":
            return v / 100.0      # % → fraction
        return v                  # horizontal (%) / vertical (m/s): used directly

    def _native_vol_floor(self) -> float:
        """Volume threshold in native units for the active metric (0 for rotor — no floor)."""
        m = self._metric
        if m == "turbulence":
            return self._vol_floor[m] / 100.0
        if m in ("horizontal", "vertical"):
            return self._vol_floor[m]
        return 0.0

    def _apply_metric_to_spin(self) -> None:
        """Set the scale + volume-floor spinboxes' units/range/value for the active metric."""
        m = self._metric
        s = self.intensity_max_spin
        s.blockSignals(True)
        if m == "rotor":
            s.setRange(2.0, 60.0), s.setSingleStep(1.0), s.setSuffix(" km/h")
        elif m == "turbulence":
            s.setRange(2.0, 100.0), s.setSingleStep(5.0), s.setSuffix(" %")
        elif m == "horizontal":
            s.setRange(20.0, 200.0), s.setSingleStep(10.0), s.setSuffix(" %")
        else:  # vertical
            s.setRange(0.5, 15.0), s.setSingleStep(0.5), s.setSuffix(" m/s")
        s.setValue(self._scale_max[m])
        s.blockSignals(False)

        f = self.ti_floor_spin
        f.setEnabled(m != "rotor")  # rotor's volume = reversed flow (no floor)
        f.blockSignals(True)
        if m == "turbulence":
            f.setRange(2.0, 80.0), f.setSingleStep(2.0), f.setSuffix(" %")
            f.setToolTip("Seuil de turbulence qui définit le VOLUME affiché.")
        elif m == "horizontal":
            f.setRange(-100.0, 100.0), f.setSingleStep(5.0), f.setSuffix(" %")
            f.setToolTip("Affiche les cellules ralenties SOUS ce % du vent amont (incl. flux inversé).")
        elif m == "vertical":
            f.setRange(0.0, 10.0), f.setSingleStep(0.5), f.setSuffix(" m/s")
            f.setToolTip("Affiche les cellules où |vitesse verticale| ≥ ce seuil.")
        if m in self._vol_floor:
            f.setValue(self._vol_floor[m])
        f.blockSignals(False)

        self.height_max_spin.setEnabled(m in ("rotor", "turbulence"))  # height axis = 2-D metrics

    def _legend_image(self):
        from ..viz.volume3d import diverging_legend_image, rotor_legend_image

        m, vmax = self._metric, self._scale_max[self._metric]
        if m == "rotor":
            return rotor_legend_image(self._height_max_m, vmax, ylabel="Intensité (km/h)",
                                      title="Rotor : hauteur × intensité")
        if m == "turbulence":
            return rotor_legend_image(self._height_max_m, vmax, ylabel="Turbulence (%)",
                                      title="Turbulence : hauteur × intensité")
        if m == "horizontal":
            return diverging_legend_image(vmax, "RdBu", "Vit. horizontale (% vent)",
                                          "Rouge = rotor · bleu = plein vent")
        return diverging_legend_image(vmax, "RdYlGn", "Vit. verticale (m/s)",
                                      "Vert = ascendance · rouge = dégueulante")

    def _update_legend(self) -> None:
        """Refresh the legend image for the active metric/scales."""
        if not hasattr(self, "legend_label"):
            return
        try:
            from PySide6.QtGui import QImage, QPixmap

            buf = self._legend_image()
            h, w = buf.shape[:2]
            img = QImage(bytes(buf.data), w, h, 4 * w, QImage.Format_RGBA8888)
            self.legend_label.setPixmap(QPixmap.fromImage(img))
        except Exception:
            pass

    def _set_wind_legend(self) -> None:
        """Show the static continuous wind-speed colourbar (0–40 km/h) in the 3D panel."""
        if not hasattr(self, "wind_legend_label"):
            return
        try:
            from PySide6.QtGui import QImage, QPixmap

            from ..viz.volume3d import wind_legend_image

            buf = wind_legend_image()
            h, w = buf.shape[:2]
            img = QImage(bytes(buf.data), w, h, 4 * w, QImage.Format_RGBA8888)
            self.wind_legend_label.setPixmap(QPixmap.fromImage(img))
        except Exception:
            pass

    def _on_spin_change(self, *_args) -> None:
        """Spinbox edit: store + refresh the legend only (3D re-render waits for « Appliquer »)."""
        self._height_max_m = float(self.height_max_spin.value())
        self._scale_max[self._metric] = float(self.intensity_max_spin.value())
        self._update_legend()

    def _on_floor_change(self, val: float) -> None:
        if self._metric in self._vol_floor:  # changes the VOLUME -> applied on « Appliquer »
            self._vol_floor[self._metric] = float(val)

    def _on_apply_scale(self) -> None:
        if self._result is not None:  # recolour/re-extract at the new scales (texture cached)
            self._render_hour(self.hour_slider.value())

    def _on_metric_change(self, idx: int) -> None:
        self._metric = self.METRICS[idx] if 0 <= idx < len(self.METRICS) else "rotor"
        self._apply_metric_to_spin()
        self._update_legend()
        if self._result is not None:
            self._render_hour(self.hour_slider.value())

    def _on_opacity_change(self, val: int) -> None:
        """Set the rotor volumes' opacity live (actor-level, no scene rebuild / basemap refetch)."""
        self._opacity = max(0.02, val / 100.0)
        actors = getattr(self._plotter, "_rotor_actors", None) if self._plotter is not None else None
        if not actors:
            return
        for a in actors:
            try:
                a.GetProperty().SetOpacity(self._opacity)
            except Exception:
                pass
        try:
            self._plotter.render()
        except Exception:
            pass

    def _label_for_hour(self, hour: int) -> str:
        """Absolute date label for an hour offset — the saved labels for an opened result (its run
        day), else today's forecast window."""
        if self._hour_labels and hour in self._hour_labels:
            return self._hour_labels[hour]
        try:
            return self._fc.label_at(hour)
        except Exception:
            return f"{hour:02d}h"

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
        self.hour_label.setText(self._label_for_hour(hour))  # absolute date/hour, not just "15h"
        cam = self._plotter.camera_position if self._rendered else None
        self._plotter.clear()
        populate_auto_scene(self._plotter, self._dem, self._result.cases_for_hour(hour),
                            crs=self._dem.crs, basemap_source="IGN plan",
                            route_winds=self._route_winds_utm(hour),
                            rotor_cache=self._rotor_cache, rotor_opacity=self._opacity,
                            height_clim=(0.0, self._height_max_m),
                            intensity_max=self._native_intensity_max(), metric=self._metric,
                            vol_floor=self._native_vol_floor(),
                            texture_cache=self._tex_cache)
        if cam is not None:  # keep the viewpoint across hour scrubs
            self._plotter.camera_position = cam
        else:
            self._plotter.reset_camera()
            self._rendered = True
        try:  # baseline the wind-arrow autoscale at the rendered view
            from ..viz.volume3d import baseline_wind_autoscale
            baseline_wind_autoscale(self._plotter)
        except Exception:
            pass


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
