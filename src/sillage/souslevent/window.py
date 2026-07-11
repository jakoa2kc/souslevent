"""Unified SousLeVent desktop UI.

The goal is to keep one first step (select either a rectangle or a flight route), then choose one
of the three workflows that existed across the two older windows:

* Pass-1 only, then manually select candidate domains;
* Pass-1 + automatic multiple candidate domains;
* Pass-2 everywhere (blind paving).

The implementation deliberately reuses the automatic window's render/save/open machinery. The two
old windows remain untouched and launchable as backups.
"""

from __future__ import annotations

import math
import time
from dataclasses import replace

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
from PySide6 import QtCore, QtWidgets
from superqt import QRangeSlider

from ..app.jobs import SolveJob
from ..app.map_tab import MapTab
from ..auto.pipeline import (
    DEFAULT_MAX_FEATURES,
    WIND_MODE_FORECAST,
    WIND_MODE_MANUAL_GRID,
    AutoConfig,
    ScreeningResult,
    bbox_from_route,
    momentum_parallel_plan,
    run_auto,
    screen_candidates,
)
from ..auto.partition import SubZone, estimate_cells
from ..auto.window import (
    GREEN,
    NO_BASEMAP,
    PASS2_MESH_DEFAULT,
    PASS2_MESH_PRESETS,
    TOPO_LABELS,
    TOPO_PRESETS,
    AutoWindow,
    pass2_estimate_minutes,
)
from ..terrain.dem import load_dem
from ..viz import map2d
from ..wind.directions import direction_label


class SousLeVentWindow(AutoWindow):
    """New global window: one selection tab, three calculation workflows."""

    CALC_PASS1_MANUAL = "pass1_manual"     # Pass-1 → manually pick candidates → Pass-2 (all hours)
    CALC_PASS2_EVERYWHERE = "corridor"     # blind paving, no Pass-1

    def __init__(self) -> None:
        self._selection_mode = "route"
        self._selected_bbox = None
        self._screening_result: ScreeningResult | None = None
        self._screening_cfg: AutoConfig | None = None
        self._candidate_dem = None
        self._candidate_ax = None
        self._candidate_syncing = False
        self._candidate_auto_count = 0
        self._candidate_hour = 0          # index into the screening hazard stack being displayed
        self._manual_mesh_override = None  # mesh_count chosen via the manual-zone popup (ADR-0037)
        self._manual_topo_override = None  # target_res_m chosen via the manual-zone popup
        self._candidate_press = None
        self._candidate_drag_patch = None
        super().__init__()
        self.setWindowTitle("SousLeVent")
        self.tabs.setTabText(0, "Sélection + calcul")
        self.tabs.insertTab(1, self._build_candidates_tab(), "Candidats Pass-1")

    # --- tab 1: unified selection ------------------------------------------
    def _build_select_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setSpacing(8)

        selection_row = QtWidgets.QHBoxLayout()
        selection_row.addWidget(QtWidgets.QLabel("Sélection :"))
        self.selection_combo = QtWidgets.QComboBox()
        self.selection_combo.addItem("Parcours", "route")
        self.selection_combo.addItem("Rectangle", "rectangle")
        self.selection_combo.currentIndexChanged.connect(self._on_selection_mode_change)
        selection_row.addWidget(self.selection_combo)
        selection_row.addStretch(1)
        lay.addLayout(selection_row)

        self.info = QtWidgets.QLabel("")
        self.info.setStyleSheet("color:#555;")

        self._map_host = QtWidgets.QWidget()
        self._map_lay = QtWidgets.QVBoxLayout(self._map_host)
        self._map_lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._map_host, stretch=1)
        lay.addWidget(self.info)
        self._install_map("route")

        wind_mode_row = QtWidgets.QHBoxLayout()
        wind_mode_row.addWidget(QtWidgets.QLabel("Vent :"))
        self.wind_mode_combo = QtWidgets.QComboBox()
        self.wind_mode_combo.addItem("Météo du créneau", WIND_MODE_FORECAST)
        self.wind_mode_combo.addItem("Homogène manuel", WIND_MODE_MANUAL_GRID)
        self.wind_mode_combo.currentIndexChanged.connect(self._on_wind_mode_change)
        wind_mode_row.addWidget(self.wind_mode_combo)
        wind_mode_row.addStretch(1)
        lay.addLayout(wind_mode_row)

        self.wind_forecast_widget = QtWidgets.QWidget()
        forecast_lay = QtWidgets.QVBoxLayout(self.wind_forecast_widget)
        forecast_lay.setContentsMargins(0, 0, 0, 0)
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
        forecast_lay.addLayout(row)
        self._tick_strip = self._make_tick_strip()
        forecast_lay.addWidget(self._tick_strip)
        self.avail_label = QtWidgets.QLabel(f"Prévision : {self._fc.source} · {self._fc.note}")
        self.avail_label.setStyleSheet("color:#555;")
        forecast_lay.addWidget(self.avail_label)
        lay.addWidget(self.wind_forecast_widget)

        self.wind_manual_widget = QtWidgets.QWidget()
        wind = QtWidgets.QHBoxLayout(self.wind_manual_widget)
        wind.setContentsMargins(0, 0, 0, 0)
        self.speed_label_title = QtWidgets.QLabel("Vitesses :")
        wind.addWidget(self.speed_label_title)
        self.speed_slider = QRangeSlider(QtCore.Qt.Horizontal)
        self.speed_slider.setRange(5, 80)
        self.speed_slider.setValue((10, 30))
        self.speed_slider.setSingleStep(5)
        self.speed_slider.setFixedWidth(220)
        self.speed_slider.valueChanged.connect(self._on_manual_wind_change)
        wind.addWidget(self.speed_slider)
        self.speed_label = QtWidgets.QLabel("")
        self.speed_label.setMinimumWidth(120)
        wind.addWidget(self.speed_label)
        wind.addSpacing(16)
        self.dir_label_title = QtWidgets.QLabel("Directions :")
        wind.addWidget(self.dir_label_title)
        self.dir_slider = QRangeSlider(QtCore.Qt.Horizontal)
        self.dir_slider.setRange(0, 360)
        self.dir_slider.setValue((225, 315))
        self.dir_slider.setSingleStep(45)
        self.dir_slider.setFixedWidth(220)
        self.dir_slider.valueChanged.connect(self._on_manual_wind_change)
        wind.addWidget(self.dir_slider)
        self.dir_label = QtWidgets.QLabel("")
        self.dir_label.setMinimumWidth(120)
        wind.addWidget(self.dir_label)
        wind.addStretch(1)
        lay.addWidget(self.wind_manual_widget)

        calc_row = QtWidgets.QHBoxLayout()
        calc_row.addWidget(QtWidgets.QLabel("Calcul :"))
        self.calc_combo = QtWidgets.QComboBox()
        self.calc_combo.addItem("Pass-1 puis sélection des candidats", self.CALC_PASS1_MANUAL)
        self.calc_combo.addItem("Pass-2 partout (pavage aveugle)", self.CALC_PASS2_EVERYWHERE)
        self.calc_combo.currentIndexChanged.connect(self._on_calc_mode_change)
        calc_row.addWidget(self.calc_combo, stretch=1)
        lay.addLayout(calc_row)

        self.common_params_widget = QtWidgets.QWidget()
        wr = QtWidgets.QHBoxLayout(self.common_params_widget)
        wr.setContentsMargins(0, 0, 0, 0)
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
        wr.addWidget(QtWidgets.QLabel("Topo :"))
        self.topo_combo = QtWidgets.QComboBox()
        self.topo_combo.addItems(TOPO_LABELS)
        self.topo_combo.setCurrentIndex(TOPO_PRESETS.index(10.0))
        self.topo_combo.setToolTip(
            "Résolution du MNT. 1 m utilise les données IGN haute résolution quand disponibles "
            "et peut être très lourd : à réserver aux trajets courts.")
        wr.addWidget(self.topo_combo)
        wr.addSpacing(16)
        wr.addWidget(QtWidgets.QLabel("Maillage Pass-2 :"))
        self.mesh_combo = QtWidgets.QComboBox()
        self.mesh_combo.addItems(list(PASS2_MESH_PRESETS))
        self.mesh_combo.setCurrentText(PASS2_MESH_DEFAULT)
        self.mesh_combo.setToolTip(
            "Finesse du maillage momentum (qualité/temps, ADR-0008). Plus fin = plus précis mais "
            "beaucoup plus lent, par calcul Pass-2. « Affiner en cas de doute ».")
        self.mesh_combo.currentIndexChanged.connect(lambda *_: self._refresh_cpu_plan())
        wr.addWidget(self.mesh_combo)
        wr.addStretch(1)
        lay.addWidget(self.common_params_widget)

        self.margin_row_widget = QtWidgets.QWidget()
        mr = QtWidgets.QHBoxLayout(self.margin_row_widget)
        mr.setContentsMargins(0, 0, 0, 0)
        mr.addWidget(QtWidgets.QLabel("Marge corridor :"))
        self.margin_spin = QtWidgets.QDoubleSpinBox()
        self.margin_spin.setRange(0.5, 10.0)
        self.margin_spin.setSingleStep(0.5)
        self.margin_spin.setValue(2.0)
        self.margin_spin.setSuffix(" km")
        self.margin_spin.valueChanged.connect(self._on_margin_change)
        mr.addWidget(self.margin_spin)
        mr.addStretch(1)
        lay.addWidget(self.margin_row_widget)

        self.features_row_widget = QtWidgets.QWidget()
        fr = QtWidgets.QHBoxLayout(self.features_row_widget)
        fr.setContentsMargins(0, 0, 0, 0)
        fr.addWidget(QtWidgets.QLabel("Candidats max :"))
        self.features_spin = QtWidgets.QSpinBox()
        self.features_spin.setRange(1, 64)
        self.features_spin.setValue(DEFAULT_MAX_FEATURES)
        self.features_spin.setToolTip(
            "Nombre maximum de candidats Pass-1 proposés ou calculés automatiquement.")
        self.features_spin.valueChanged.connect(lambda *_: self._refresh_cpu_plan())
        fr.addWidget(self.features_spin)
        fr.addStretch(1)
        lay.addWidget(self.features_row_widget)

        self.step_row_widget = QtWidgets.QWidget()
        sr = QtWidgets.QHBoxLayout(self.step_row_widget)
        sr.setContentsMargins(0, 0, 0, 0)
        sr.addWidget(QtWidgets.QLabel("Pas secteurs :"))
        self.step_spin = QtWidgets.QDoubleSpinBox()
        self.step_spin.setRange(0.5, 5.0)
        self.step_spin.setSingleStep(0.5)
        self.step_spin.setValue(1.5)
        self.step_spin.setSuffix(" km")
        self.step_spin.valueChanged.connect(lambda *_: self._refresh_cpu_plan())
        sr.addWidget(self.step_spin)
        sr.addStretch(1)
        lay.addWidget(self.step_row_widget)

        self.cpu_plan_label = QtWidgets.QLabel("")
        self.cpu_plan_label.setWordWrap(True)
        self.cpu_plan_label.setStyleSheet("color:#555;")
        lay.addWidget(self.cpu_plan_label)

        validate_row = QtWidgets.QHBoxLayout()
        validate_row.addStretch(1)
        self.btn_validate = QtWidgets.QPushButton("")
        self.btn_validate.setMinimumWidth(380)
        self.btn_validate.setMinimumHeight(40)
        self.btn_validate.setStyleSheet(GREEN)
        self.btn_validate.clicked.connect(self.on_validate)
        validate_row.addWidget(self.btn_validate)
        validate_row.addStretch(1)
        lay.addLayout(validate_row)

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
        self._on_wind_mode_change()
        self._apply_workflow_controls()
        return w

    def _build_candidates_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        self._candidates_tab = w
        lay = QtWidgets.QVBoxLayout(w)
        header = QtWidgets.QHBoxLayout()
        self.candidate_summary = QtWidgets.QLabel(
            "Lance d'abord « Pass-1 seul puis sélection manuelle ».")
        self.candidate_summary.setStyleSheet("color:#555;")
        header.addWidget(self.candidate_summary, stretch=1)
        header.addWidget(QtWidgets.QLabel("Fond :"))
        self.candidate_basemap_combo = QtWidgets.QComboBox()
        self.candidate_basemap_combo.addItems([NO_BASEMAP, *map2d.BASEMAP_SOURCES.keys()])
        self.candidate_basemap_combo.setCurrentText("IGN plan")
        self.candidate_basemap_combo.setToolTip(
            "Fond de carte sous le résultat Pass-1. En cas d'indisponibilité réseau, "
            "la carte retombe sur l'ombrage du MNT.")
        self.candidate_basemap_combo.currentTextChanged.connect(self._on_candidate_basemap_change)
        header.addWidget(self.candidate_basemap_combo)
        lay.addLayout(header)

        # Browse the Pass-1 hazard hour by hour (or scenario by scenario) — hidden for a single one.
        self.candidate_hour_row = QtWidgets.QWidget()
        chr_lay = QtWidgets.QHBoxLayout(self.candidate_hour_row)
        chr_lay.setContentsMargins(0, 0, 0, 0)
        chr_lay.addWidget(QtWidgets.QLabel("Heure / scénario :"))
        self.candidate_hour_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.candidate_hour_slider.setMinimum(0)
        self.candidate_hour_slider.setMaximum(0)
        self.candidate_hour_slider.valueChanged.connect(self._on_candidate_hour_change)
        chr_lay.addWidget(self.candidate_hour_slider, stretch=1)
        self.candidate_hour_label = QtWidgets.QLabel("")
        chr_lay.addWidget(self.candidate_hour_label)
        self.candidate_hour_row.setVisible(False)
        lay.addWidget(self.candidate_hour_row)

        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.candidate_fig = Figure(figsize=(7, 5), tight_layout=True)
        self.candidate_canvas = FigureCanvasQTAgg(self.candidate_fig)
        self.candidate_canvas.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.candidate_canvas.mpl_connect("button_press_event", self._on_candidate_map_press)
        self.candidate_canvas.mpl_connect("motion_notify_event", self._on_candidate_map_motion)
        self.candidate_canvas.mpl_connect("button_release_event", self._on_candidate_map_release)
        split.addWidget(self.candidate_canvas)

        side = QtWidgets.QWidget()
        side_lay = QtWidgets.QVBoxLayout(side)
        side_lay.setContentsMargins(0, 0, 0, 0)
        self.candidate_list = QtWidgets.QListWidget()
        self.candidate_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.candidate_list.itemSelectionChanged.connect(self._on_candidate_selection)
        side_lay.addWidget(self.candidate_list)
        split.addWidget(side)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        lay.addWidget(split, stretch=1)

        row = QtWidgets.QHBoxLayout()
        self.btn_run_selected = QtWidgets.QPushButton("▶  Lancer Pass-2 sur la sélection")
        self.btn_run_selected.setStyleSheet(GREEN)
        self.btn_run_selected.setEnabled(False)
        self.btn_run_selected.clicked.connect(self._on_run_selected_candidates)
        row.addWidget(self.btn_run_selected)
        row.addStretch(1)
        lay.addLayout(row)
        self._draw_candidate_placeholder()
        return w

    def _install_map(self, mode: str) -> None:
        while self._map_lay.count():
            item = self._map_lay.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.map_tab = MapTab(mode=mode)
        if mode == "route":
            self.map_tab.routeSelected.connect(self._on_route)
        else:
            self.map_tab.aoiSelected.connect(self._on_rectangle)
        self._map_lay.addWidget(self.map_tab)

    def _on_selection_mode_change(self, *_args) -> None:
        self._selection_mode = self.selection_combo.currentData() or "route"
        self._selected_bbox = None
        self._route = None
        self._route_cells = []
        self._route_gen += 1
        self._install_map(self._selection_mode)
        self.info.setText(
            "Trace un parcours." if self._selection_mode == "route"
            else "Dessine un rectangle.")
        self._apply_workflow_controls()

    def _on_calc_mode_change(self, *_args) -> None:
        self._apply_workflow_controls()

    def _on_rectangle(self, south: float, west: float, north: float, east: float) -> None:
        self._selected_bbox = (float(south), float(west), float(north), float(east))
        self._route = None
        self._route_cells = []
        self._route_gen += 1
        self.info.setText(
            f"Rectangle : {self._bbox_label(self._selected_bbox)} · "
            f"topo {TOPO_PRESETS[self.topo_combo.currentIndex()]:.0f} m")
        self._refresh_cpu_plan()

    def _apply_workflow_controls(self) -> None:
        if not hasattr(self, "calc_combo"):
            return
        calc = self._calc_mode()
        is_route = self._selection_mode == "route"
        is_pass2_everywhere = calc == self.CALC_PASS2_EVERYWHERE
        self.margin_spin.setEnabled(is_route)
        self.features_spin.setEnabled(calc == self.CALC_PASS1_MANUAL)
        self.step_spin.setEnabled(is_pass2_everywhere)
        if hasattr(self, "margin_row_widget"):
            self.margin_row_widget.setVisible(is_route)
        if hasattr(self, "features_row_widget"):
            self.features_row_widget.setVisible(calc == self.CALC_PASS1_MANUAL)
        if hasattr(self, "step_row_widget"):
            self.step_row_widget.setVisible(is_pass2_everywhere)
        labels = {
            self.CALC_PASS1_MANUAL: "✓  Valider — lancer Pass-1",
            self.CALC_PASS2_EVERYWHERE: "✓  Valider — Pass-2 partout",
        }
        self.btn_validate.setText(labels.get(calc, "✓  Valider"))
        self._refresh_cpu_plan()

    def _calc_mode(self) -> str:
        return self.calc_combo.currentData() if hasattr(self, "calc_combo") else self.CALC_PASS1_MANUAL

    def _wind_mode(self) -> str:
        if not hasattr(self, "wind_mode_combo"):
            return WIND_MODE_FORECAST
        return self.wind_mode_combo.currentData() or WIND_MODE_FORECAST

    def _coerce_step_range(self, slider, step: int) -> tuple[int, int]:
        lo, hi = sorted((int(slider.value()[0]), int(slider.value()[1])))
        lo = int(round(lo / step) * step)
        hi = int(round(hi / step) * step)
        lo = max(slider.minimum(), min(slider.maximum(), lo))
        hi = max(slider.minimum(), min(slider.maximum(), hi))
        if tuple(slider.value()) != (lo, hi):
            slider.blockSignals(True)
            slider.setValue((lo, hi))
            slider.blockSignals(False)
        return lo, hi

    def _manual_speed_values(self) -> tuple[int, ...]:
        lo, hi = self._coerce_step_range(self.speed_slider, 5)
        return tuple(range(lo, hi + 1, 5))

    def _manual_direction_values(self) -> tuple[int, ...]:
        lo, hi = self._coerce_step_range(self.dir_slider, 45)
        values, seen = [], set()
        for deg in range(lo, hi + 1, 45):
            norm = deg % 360
            if norm not in seen:
                seen.add(norm)
                values.append(norm)
        return tuple(values)

    def _wind_case_count(self) -> int:
        if self._wind_mode() == WIND_MODE_MANUAL_GRID:
            return max(1, len(self._manual_speed_values()) * len(self._manual_direction_values()))
        lo, hi = self._window_hours()
        return max(1, hi - lo)

    def _wind_case_ids(self) -> tuple[int, ...]:
        if self._wind_mode() == WIND_MODE_MANUAL_GRID:
            return tuple(range(self._wind_case_count()))
        lo, hi = self._window_hours()
        return tuple(range(lo, hi))

    def _wind_summary(self) -> str:
        if self._wind_mode() != WIND_MODE_MANUAL_GRID:
            lo, hi = self._window_hours()
            return f"météo {self._fc.label_at(lo)} → {self._fc.label_at(hi)} ({hi - lo} h)"
        speeds = self._manual_speed_values()
        dirs = self._manual_direction_values()
        dlo, dhi = self._coerce_step_range(self.dir_slider, 45)
        return (
            f"manuel homogène : {len(speeds) * len(dirs)} scénario(s), "
            f"{speeds[0]}→{speeds[-1]} km/h par 5, "
            f"{direction_label(dlo)}→{direction_label(dhi)} par 45°")

    def _refresh_manual_wind_labels(self) -> None:
        if not hasattr(self, "speed_label"):
            return
        speeds = self._manual_speed_values()
        dlo, dhi = self._coerce_step_range(self.dir_slider, 45)
        self.speed_label.setText(f"{speeds[0]} → {speeds[-1]} km/h")
        self.dir_label.setText(f"{direction_label(dlo)} → {direction_label(dhi)}")

    def _on_manual_wind_change(self, *_args) -> None:
        self._refresh_manual_wind_labels()
        self._refresh_cpu_plan()

    def _on_wind_mode_change(self, *_args) -> None:
        manual = self._wind_mode() == WIND_MODE_MANUAL_GRID
        if hasattr(self, "wind_forecast_widget"):
            self.wind_forecast_widget.setVisible(not manual)
        if hasattr(self, "wind_manual_widget"):
            self.wind_manual_widget.setVisible(manual)
        if hasattr(self, "window_slider"):
            self.window_slider.setEnabled(not manual)
        if manual:
            self._wind_timer.stop()
            self._route_cells = []
            if hasattr(self, "map_tab"):
                self.map_tab.show_wind([])
        elif any(len(s) >= 2 for s in (self._route or [])) and not self._route_cells:
            self._wind_timer.start()
        self._refresh_manual_wind_labels()
        self._refresh_cpu_plan()

    # --- estimates / selection ---------------------------------------------
    def _current_selection(self):
        if self._selection_mode == "route":
            segs = [list(s) for s in (self._route or []) if len(s) >= 2]
            if not segs:
                return None, [], []
            flat = [p for s in segs for p in s]
            return bbox_from_route(flat, self.margin_spin.value()), flat, segs
        if self._selected_bbox is None:
            return None, [], []
        return self._selected_bbox, [], []

    def _bbox_size_km(self, bbox) -> tuple[float, float]:
        if not bbox:
            return 0.0, 0.0
        s, w, n, e = bbox
        mid = (s + n) / 2.0
        width = max(0.0, (e - w) * 111.320 * math.cos(math.radians(mid)))
        height = max(0.0, (n - s) * 111.320)
        return width, height

    def _bbox_label(self, bbox) -> str:
        width, height = self._bbox_size_km(bbox)
        return f"{width:.1f} × {height:.1f} km"

    def _refresh_cpu_plan(self) -> None:
        if not hasattr(self, "cpu_plan_label"):
            return
        cases = self._wind_case_count()
        wind_unit = "scénario(s)" if self._wind_mode() == WIND_MODE_MANUAL_GRID else "h"
        calc = self._calc_mode()
        bbox, _flat, _segs = self._current_selection()
        if calc == self.CALC_PASS2_EVERYWHERE:
            if self._selection_mode == "route":
                step_km = self.step_spin.value()
                rlen = self._route_length_km()
                domains = max(1, int(rlen / max(0.1, step_km)) + 1) if rlen > 0 else 1
                tasks = (f"pavage parcours : ~{domains} secteurs "
                         f"({rlen:.1f} km / pas {step_km:.1f} km) × {cases} {wind_unit}")
            else:
                width, height = self._bbox_size_km(bbox)
                step = max(0.1, self.step_spin.value())
                domains = max(1, math.ceil(width / step) * math.ceil(height / step))
                tasks = (f"pavage rectangle : ~{domains} secteurs "
                         f"({width:.1f} × {height:.1f} km) × {cases} {wind_unit}")
        else:
            domains = 1
            tasks = (f"Pass-1 seul : jusqu'à {self.features_spin.value()} candidats proposés. "
                     f"Le Pass-2 calculera ensuite {cases} {wind_unit} par zone sélectionnée.")
        max_tasks = 1 if calc == self.CALC_PASS1_MANUAL else max(1, cases * max(1, int(domains)))
        requested = self.workers_slider.value()
        requested_plan = momentum_parallel_plan(requested, cores=self._cores)
        estimate = momentum_parallel_plan(requested, cores=self._cores, task_count=max_tasks)
        perfect = ", ".join(str(w) for w in requested_plan.perfect_workers) or "aucune"
        idle = "" if estimate.idle_cores == 0 else f", {estimate.idle_cores} au repos"
        mesh_txt = ""
        if calc != self.CALC_PASS1_MANUAL:  # Pass-2 mesh only matters when a Pass-2 will run
            mc, _it = self._mesh_preset()
            per = pass2_estimate_minutes(mc)
            waves = max(1, math.ceil(max_tasks / max(1, estimate.workers)))
            mesh_txt = (f" Maillage {self.mesh_combo.currentText()} (~{mc // 1000}k mailles, "
                        f"~{per} min/calcul ⇒ ~{per * waves} min au total, indicatif).")
        self.cpu_plan_label.setText(
            f"Plan CPU : demandé {requested_plan.workers} calcul(s) max. {tasks} ⇒ "
            f"estimation {estimate.workers} en parallèle × {estimate.threads_per_worker} thread(s) "
            f"= {estimate.used_cores}/{estimate.cores} cœurs{idle}. "
            f"Divisions parfaites : {perfect}.{mesh_txt}")

    # --- run ----------------------------------------------------------------
    def _build_cfg(self, *, domain_mode: str) -> AutoConfig | None:
        bbox, flat, segs = self._current_selection()
        if bbox is None:
            msg = "Trace d'abord un parcours." if self._selection_mode == "route" else "Dessine d'abord un rectangle."
            QtWidgets.QMessageBox.information(self, "Sélection manquante", msg)
            return None
        lo, _hi = self._window_hours()
        wind_mode = self._wind_mode()
        if wind_mode == WIND_MODE_MANUAL_GRID:
            speeds = self._manual_speed_values()
            dirs = self._manual_direction_values()
            hours = tuple(range(len(speeds) * len(dirs)))
            wind_source = "manual"
            window_start_iso = ""
        else:
            speeds = ()
            dirs = ()
            hours = self._wind_case_ids()
            wind_source = "arome" if self._fc.source == "AROME" else "open_meteo"
            window_start_iso = self._fc.at(lo).isoformat()
        mesh_count, iterations = self._mesh_preset()
        return AutoConfig(
            bbox_latlon=bbox, hours=hours,
            route_latlon=tuple(flat), route_segments=tuple(tuple(s) for s in segs),
            corridor_margin_km=self.margin_spin.value(),
            window_start_iso=window_start_iso, wind_source=wind_source, wind_mode=wind_mode,
            manual_wind_speeds_kmh=speeds, manual_wind_dirs_deg=dirs,
            max_features=self.features_spin.value(), domain_mode=domain_mode,
            target_res_m=TOPO_PRESETS[self.topo_combo.currentIndex()],
            tile_step_m=self.step_spin.value() * 1000.0, mesh_count=mesh_count, iterations=iterations,
            momentum_workers=self.workers_slider.value())

    def _mesh_preset(self) -> tuple[int, int]:
        """(mesh_count, iterations) for the selected Pass-2 mesh quality preset (ADR-0008)."""
        return PASS2_MESH_PRESETS.get(self.mesh_combo.currentText(),
                                      PASS2_MESH_PRESETS[PASS2_MESH_DEFAULT])

    def on_validate(self) -> None:
        if self._job is not None:
            return
        calc = self._calc_mode()
        domain_mode = "features" if calc != self.CALC_PASS2_EVERYWHERE else "corridor"
        cfg = self._build_cfg(domain_mode=domain_mode)
        if cfg is None:
            return
        if (self._selection_mode == "route" and self._wind_mode() != WIND_MODE_MANUAL_GRID
                and not self._route_cells):
            self._fetch_route_winds()
        self._last_cfg = cfg
        cli, cache = self.cfg.windninja_cli, self.cfg.cache_dir
        if calc == self.CALC_PASS2_EVERYWHERE:
            def fn(on_progress, cancel):
                return run_auto(
                    cfg, cli=cli, cache_dir=cache, on_progress=on_progress, cancel=cancel)
        else:  # PASS1_MANUAL: screen (hourly hazard) → pick candidates → Pass-2 (all hours)
            def fn(on_progress, cancel):
                return screen_candidates(
                    cfg, cli=cli, cache_dir=cache, on_progress=on_progress, cancel=cancel)

        self.log.clear()
        label = self.calc_combo.currentText()
        self._log(f"SousLeVent — {label} · sélection {self.selection_combo.currentText().lower()} · "
                  f"vent {self._wind_summary()}")
        self._run_started = time.monotonic()
        self._last_pct = 0
        self._invalidate_shown_result()
        self._set_running(True)
        job = SolveJob(fn, self)
        job.progress.connect(self._on_progress)
        job.finished.connect(self._on_finished)
        job.failed.connect(self._on_failed)
        self._job = job
        job.start()

    def _on_finished(self, result) -> None:
        if isinstance(result, ScreeningResult):
            self._job = None
            self._set_running(False)
            self._last_pct = 100
            self._screening_result = result
            self._screening_cfg = self._last_cfg
            self._populate_candidates(result)
            msg = f"Pass-1 terminé : {len(result.partition)} candidat(s) — {result.timings_summary}"
            self.statusBar().showMessage(msg)
            self._log(msg)
            self.avancement.setText(msg)
            self.tabs.setCurrentWidget(self._candidates_tab)
            return
        super()._on_finished(result)

    def _populate_candidates(self, result: ScreeningResult) -> None:
        self._candidate_auto_count = len(result.partition)
        self._candidate_press = None
        self._candidate_drag_patch = None
        self._candidate_hour = 0
        self._manual_mesh_override = None   # new screening → back to the mesh-combo preset
        self._manual_topo_override = None
        self._candidate_syncing = True
        try:
            self.candidate_list.clear()
            for i, z in enumerate(result.partition):
                self._add_candidate_item(i, z)
        finally:
            self._candidate_syncing = False
        # hazard hour/scenario slider (hidden when there is a single Pass-1 map)
        stack = getattr(result, "hazard_stack", None)
        has_stack = bool(stack) and len(stack) > 1
        self.candidate_hour_slider.blockSignals(True)
        self.candidate_hour_slider.setMaximum(max(0, len(stack) - 1) if stack else 0)
        self.candidate_hour_slider.setValue(0)
        self.candidate_hour_slider.blockSignals(False)
        self.candidate_hour_row.setVisible(has_stack)
        self.candidate_hour_label.setText(self._candidate_hour_text(0) if has_stack else "")
        self.candidate_summary.setText(
            f"{len(result.partition)} candidat(s) Pass-1. Clique les rectangles sur la carte "
            "ou trace un nouveau rectangle manuel, puis lance Pass-2.")
        self.btn_run_selected.setEnabled(False)
        try:
            self._candidate_dem = load_dem(result.dem_path, max_domain_km=200.0)
        except Exception as exc:
            self._candidate_dem = None
            self._draw_candidate_placeholder(f"MNT illisible pour la carte candidats : {exc}")
            return
        self._draw_candidate_map()
        self._on_candidate_selection()  # refresh the Pass-2 button + summary

    def _on_candidate_hour_change(self, value: int) -> None:
        self._candidate_hour = int(value)
        self.candidate_hour_label.setText(self._candidate_hour_text(self._candidate_hour))
        self._draw_candidate_map()

    def _candidate_hour_text(self, idx: int) -> str:
        result = self._screening_result
        if result is None or not result.hours or not (0 <= idx < len(result.hours)):
            return ""
        hour = result.hours[idx]
        return self._labels_for_cfg(self._screening_cfg, result.hours).get(hour, str(hour))

    def _candidate_label(self, idx: int) -> str:
        if idx < self._candidate_auto_count:
            return f"{idx + 1:02d}"
        return f"M{idx - self._candidate_auto_count + 1:02d}"

    def _add_candidate_item(self, idx: int, z: SubZone) -> QtWidgets.QListWidgetItem:
        width = (z.bbox[2] - z.bbox[0]) / 1000.0
        height = (z.bbox[3] - z.bbox[1]) / 1000.0
        kind = "candidat Pass-1" if idx < self._candidate_auto_count else "rectangle manuel"
        txt = (f"{self._candidate_label(idx)} · {kind} · centre "
               f"({z.center[0]:.0f}, {z.center[1]:.0f}) · {width:.1f} × {height:.1f} km · "
               f"relief {z.relief_m:.0f} m · ~{z.est_cells:,} cellules topo")
        item = QtWidgets.QListWidgetItem(txt)
        item.setData(QtCore.Qt.UserRole, idx)
        self.candidate_list.addItem(item)
        return item

    def _on_candidate_selection(self) -> None:
        rows = self._selected_candidate_indices()
        self.btn_run_selected.setEnabled(bool(rows)
                                         and self._screening_result is not None
                                         and self._job is None)
        if self._screening_result is not None:
            total_manual = len(self._screening_result.partition) - self._candidate_auto_count
            base = (
                f"{self._candidate_auto_count} candidat(s) Pass-1"
                + (f" + {total_manual} rectangle(s) manuel(s)." if total_manual else ".")
            )
            if rows:
                self.candidate_summary.setText(
                    base + " " + self._manual_pass2_plan_text(len(rows)))
            else:
                self.candidate_summary.setText(
                    base + " Clique les rectangles sur la carte ou trace un nouveau rectangle manuel, "
                    "puis lance Pass-2.")
        if not self._candidate_syncing:
            self._draw_candidate_map()

    def _on_candidate_basemap_change(self, *_args) -> None:
        if self._screening_result is not None and self._candidate_dem is not None:
            self._draw_candidate_map()

    def _selected_candidate_indices(self) -> list[int]:
        return sorted({
            int(item.data(QtCore.Qt.UserRole)) for item in self.candidate_list.selectedItems()
        })

    def _manual_pass2_cfg(self, zones) -> AutoConfig:
        """The Pass-2 config for manually selected zones: current mesh preset + the popup overrides
        (mesh forced to match the terrain, or terrain adapted to the mesh — ADR-0037)."""
        mesh_count, iterations = self._mesh_preset()  # mesh is a Pass-2 knob (screening ignores it)
        if self._manual_mesh_override is not None:    # popup choice: match the terrain resolution
            mesh_count = int(self._manual_mesh_override)
        cfg = replace(self._screening_cfg, domain_mode="manual", manual_zones=zones,
                      mesh_count=mesh_count, iterations=iterations,
                      momentum_workers=self.workers_slider.value())
        if self._manual_topo_override is not None:    # popup choice: match the mesh preset
            cfg = replace(cfg, target_res_m=float(self._manual_topo_override))
        return cfg

    def _manual_pass2_plan_text(self, selected_count: int, cfg: AutoConfig | None = None) -> str:
        cfg = cfg or self._screening_cfg
        if cfg is None:
            return ""
        cases = max(1, len(cfg.hours))
        tasks = max(1, int(selected_count) * cases)
        plan = momentum_parallel_plan(self.workers_slider.value(), cores=self._cores,
                                      task_count=tasks)
        unit = "scénario(s) vent" if cfg.wind_mode == WIND_MODE_MANUAL_GRID else "h"
        idle = "" if plan.idle_cores == 0 else f", {plan.idle_cores} au repos"
        mesh_count = (int(self._manual_mesh_override) if self._manual_mesh_override is not None
                      else self._mesh_preset()[0])
        mesh_txt = (f"{mesh_count:,}".replace(",", " ") + " mailles (topo)"
                    if self._manual_mesh_override is not None else self.mesh_combo.currentText())
        if self._manual_topo_override is not None:
            mesh_txt += f" · topo adaptée {self._manual_topo_override:.0f} m"
        per = pass2_estimate_minutes(mesh_count)      # per-solve minutes for the effective mesh
        waves = max(1, math.ceil(tasks / max(1, plan.workers)))
        return (
            f"{selected_count} domaine(s) × {cases} {unit} (tous calculés) = {tasks} calcul(s) "
            f"Pass-2. {plan.workers} en parallèle × {plan.threads_per_worker} thread(s) "
            f"= {plan.used_cores}/{plan.cores} cœurs{idle}. "
            f"Maillage {mesh_txt} ≈ {per} min/calcul ⇒ ~{per * waves} min au total (indicatif).")

    def _draw_candidate_placeholder(self, text: str | None = None) -> None:
        if not hasattr(self, "candidate_fig"):
            return
        self.candidate_fig.clear()
        ax = self.candidate_fig.add_subplot(111)
        ax.axis("off")
        ax.text(0.5, 0.5, text or "La carte des candidats Pass-1 apparaîtra ici.",
                ha="center", va="center", color="#555", transform=ax.transAxes)
        self.candidate_canvas.draw_idle()

    def _draw_candidate_route(self, ax) -> None:
        cfg = self._screening_cfg
        dem = self._candidate_dem
        if cfg is None or dem is None or not cfg.route_segments:
            return
        try:
            from rasterio.crs import CRS
            from rasterio.warp import transform as warp_xy

            for seg in cfg.route_segments:
                if len(seg) < 2:
                    continue
                lons = [p[1] for p in seg]
                lats = [p[0] for p in seg]
                xs, ys = warp_xy(CRS.from_epsg(4326), dem.crs, lons, lats)
                ax.plot(xs, ys, color="white", linewidth=4.0, alpha=0.85, zorder=6)
                ax.plot(xs, ys, color="#1565c0", linewidth=2.0, alpha=0.95, zorder=7)
        except Exception:
            pass

    def _draw_candidate_map(self) -> None:
        if not hasattr(self, "candidate_fig"):
            return
        result = self._screening_result
        dem = self._candidate_dem
        if result is None or dem is None:
            self._draw_candidate_placeholder()
            return

        self.candidate_fig.clear()
        ax = self.candidate_fig.add_subplot(111)
        self._candidate_ax = ax
        stack = getattr(result, "hazard_stack", None)  # browse per hour/scenario when available
        if stack and 0 <= self._candidate_hour < len(stack):
            hazard = stack[self._candidate_hour]
        else:
            hazard = result.hazard
        valid_hazard = hazard is not None and getattr(hazard, "shape", None) == dem.shape
        source = (
            self.candidate_basemap_combo.currentText()
            if hasattr(self, "candidate_basemap_combo") else "IGN plan"
        )
        im = None
        if source != NO_BASEMAP:
            from matplotlib import colors

            left, bottom, right, top = dem.bounds
            extent = (left, right, bottom, top)
            try:
                ax.imshow(map2d.hillshade(dem), cmap="gray", extent=extent, origin="upper",
                          alpha=0.22, zorder=1)
                if valid_hazard:
                    im = ax.imshow(
                        hazard, cmap="inferno", extent=extent, origin="upper", alpha=0.55,
                        norm=colors.Normalize(0, 1), zorder=2)
                map2d.add_basemap(ax, dem.crs, source=source, attribution=False, zorder=0)
                ax.set_xlabel("Est (m)")
                ax.set_ylabel("Nord (m)")
            except Exception as exc:
                ax.clear()
                self.statusBar().showMessage(
                    f"Fond de carte Pass-1 indisponible ({exc}) : ombrage MNT affiché.")
                source = NO_BASEMAP
        if source == NO_BASEMAP:
            if valid_hazard:
                im = map2d.draw_indicator(ax, dem, hazard)
            else:
                map2d.draw_hillshade(ax, dem)
        if im is not None:
            self.candidate_fig.colorbar(im, ax=ax, label="Danger Pass-1 (0-1)", shrink=0.82)
        self._draw_candidate_route(ax)

        selected = set(self._selected_candidate_indices())
        for i, z in enumerate(result.partition):
            x0, y0, x1, y1 = z.bbox
            is_selected = i in selected
            is_manual = i >= self._candidate_auto_count
            edge = "#00c853" if is_selected else "#d81b60" if is_manual else "#00a8ff"
            face = "#00c85322" if is_selected else "#d81b6018" if is_manual else "#00a8ff16"
            lw = 3.0 if is_selected else 1.8
            ax.add_patch(Rectangle(
                (x0, y0), x1 - x0, y1 - y0, facecolor=face, edgecolor=edge,
                linewidth=lw, linestyle="-" if is_selected else "--", zorder=8))
            ax.text(
                z.center[0], z.center[1], self._candidate_label(i), ha="center", va="center",
                color="white", fontsize=9, fontweight="bold", zorder=9,
                bbox=dict(boxstyle="circle,pad=0.25", facecolor=edge, edgecolor="white",
                          alpha=0.95))

        ax.set_aspect("equal", adjustable="box")
        ax.set_title("Candidats Pass-1 — zones de calcul Pass-2 sélectionnables")
        self.candidate_canvas.draw_idle()

    def _candidate_index_at(self, x: float, y: float) -> int | None:
        result = self._screening_result
        if result is None:
            return None
        hits = []
        for i, z in enumerate(result.partition):
            x0, y0, x1, y1 = z.bbox
            if x0 <= x <= x1 and y0 <= y <= y1:
                cx, cy = z.center
                hits.append((math.hypot(x - cx, y - cy), i))
        if hits:
            return min(hits)[1]
        nearest = []
        for i, z in enumerate(result.partition):
            cx, cy = z.center
            half = max((z.bbox[2] - z.bbox[0]), (z.bbox[3] - z.bbox[1])) / 2.0
            dist = math.hypot(x - cx, y - cy)
            if dist <= max(350.0, half * 0.35):
                nearest.append((dist, i))
        return min(nearest)[1] if nearest else None

    def _event_data_xy(self, event) -> tuple[float, float] | None:
        if event.inaxes is not self._candidate_ax:
            return None
        if event.xdata is None or event.ydata is None:
            return None
        return float(event.xdata), float(event.ydata)

    def _on_candidate_map_press(self, event) -> None:
        xy = self._event_data_xy(event)
        if xy is None:
            return
        if event.button == 3:
            self.candidate_list.clearSelection()
            return
        if event.button != 1:
            return
        x, y = xy
        idx = self._candidate_index_at(x, y)
        self._candidate_press = {
            "data": (x, y),
            "pixel": (float(event.x), float(event.y)),
            "idx": idx,
            "dragging": False,
        }

    def _on_candidate_map_motion(self, event) -> None:
        if self._candidate_press is None or event.button not in (1, None):
            return
        xy = self._event_data_xy(event)
        if xy is None:
            return
        sx, sy = self._candidate_press["data"]
        px, py = self._candidate_press["pixel"]
        moved_px = abs(float(event.x) - px) + abs(float(event.y) - py)
        if moved_px < 8 and not self._candidate_press["dragging"]:
            return
        self._candidate_press["dragging"] = True
        x, y = xy
        if self._candidate_drag_patch is None:
            self._candidate_drag_patch = Rectangle(
                (sx, sy), 0.0, 0.0, facecolor="#d81b6022", edgecolor="#d81b60",
                linewidth=2.2, linestyle="-", zorder=10)
            self._candidate_ax.add_patch(self._candidate_drag_patch)
        self._candidate_drag_patch.set_bounds(min(sx, x), min(sy, y), abs(x - sx), abs(y - sy))
        self.candidate_canvas.draw_idle()

    def _on_candidate_map_release(self, event) -> None:
        press = self._candidate_press
        self._candidate_press = None
        if press is None:
            return
        xy = self._event_data_xy(event)
        if xy is None:
            if self._candidate_drag_patch is not None:
                try:
                    self._candidate_drag_patch.remove()
                except Exception:
                    pass
                self._candidate_drag_patch = None
                self.candidate_canvas.draw_idle()
            return
        if press["dragging"]:
            sx, sy = press["data"]
            self._finish_manual_candidate_rect(sx, sy, xy[0], xy[1])
            return
        idx = press["idx"]
        if idx is None:
            return
        item = self.candidate_list.item(idx)
        if item is None:
            return
        item.setSelected(not item.isSelected())
        self.candidate_list.scrollToItem(item)

    def _finish_manual_candidate_rect(self, x0: float, y0: float, x1: float, y1: float) -> None:
        if self._candidate_drag_patch is not None:
            try:
                self._candidate_drag_patch.remove()
            except Exception:
                pass
            self._candidate_drag_patch = None
        xmin, xmax = sorted((float(x0), float(x1)))
        ymin, ymax = sorted((float(y0), float(y1)))
        zone = self._manual_zone_from_bbox((xmin, ymin, xmax, ymax))
        if zone is None:
            self._draw_candidate_map()
            self.statusBar().showMessage("Rectangle manuel trop petit ou hors MNT.")
            return
        result = self._screening_result
        if result is None:
            return
        result.partition.append(zone)
        idx = len(result.partition) - 1
        item = self._add_candidate_item(idx, zone)
        item.setSelected(True)
        self.candidate_list.scrollToItem(item)
        total_manual = len(result.partition) - self._candidate_auto_count
        self.candidate_summary.setText(
            f"{self._candidate_auto_count} candidat(s) Pass-1 + {total_manual} rectangle(s) manuel(s). "
            "Clique ou trace d'autres zones, puis lance Pass-2.")
        self._draw_candidate_map()
        if self.isVisible():  # modal dialog — skipped in offscreen tests (window never shown)
            self._ask_manual_mesh_choice(zone)

    def _ask_manual_mesh_choice(self, zone: SubZone) -> None:
        """Mesh ↔ terrain-resolution trade-off for a hand-drawn zone (ADR-0037): tell the pilot how
        many mesh cells matching the terrain resolution would take, and offer either that mesh count
        or a terrain resolution adapted to the selected mesh preset."""
        from ..auto.partition import mesh_count_for_resolution, ninjafoam_resolution_m
        from ..auto.pipeline import AUTO_EDGE_BUFFER_M

        cfg = self._screening_cfg
        topo = float(getattr(cfg, "target_res_m", 10.0) if cfg is not None else 10.0)
        if self._manual_topo_override is not None:
            topo = float(self._manual_topo_override)
        side = max(zone.bbox[2] - zone.bbox[0], zone.bbox[3] - zone.bbox[1])
        preset_count = (self._manual_mesh_override
                        if self._manual_mesh_override is not None else self._mesh_preset()[0])
        needed = mesh_count_for_resolution(side, topo, AUTO_EDGE_BUFFER_M)
        eff = ninjafoam_resolution_m(side, preset_count, AUTO_EDGE_BUFFER_M)
        if needed <= preset_count * 1.1:  # the current mesh already matches the terrain — no dilemma
            return
        topo_adapted = max(topo, min(TOPO_PRESETS, key=lambda p: abs(p - eff))
                           if eff <= max(TOPO_PRESETS) else eff)

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Maillage / résolution terrain")
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setText(
            f"Zone {side / 1000:.1f} km (+ tampon {AUTO_EDGE_BUFFER_M / 1000:.1f} km de chaque côté).\n\n"
            f"Coller à la résolution terrain ({topo:.0f} m) demande ≈ {needed:,} mailles "
            f"(~{pass2_estimate_minutes(needed)} min PAR calcul Pass-2).\n"
            f"Le maillage sélectionné ({preset_count:,}) donne ≈ {eff:.0f} m effectifs.".replace(",", " "))
        b_mesh = box.addButton(
            f"Mailler à {needed:,} (topo {topo:.0f} m)".replace(",", " "),
            QtWidgets.QMessageBox.AcceptRole)
        b_topo = box.addButton(
            f"Adapter la topo → {topo_adapted:.0f} m", QtWidgets.QMessageBox.ActionRole)
        box.addButton("Garder tel quel", QtWidgets.QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_mesh:
            self._manual_mesh_override = int(needed)
            self._manual_topo_override = None
            self._log(f"Pass-2 manuel : maillage forcé à {needed:,} mailles "
                      f"(résolution terrain {topo:.0f} m).".replace(",", " "))
        elif clicked is b_topo:
            self._manual_topo_override = float(topo_adapted)
            self._manual_mesh_override = None
            self._log(f"Pass-2 manuel : résolution terrain adaptée à {topo_adapted:.0f} m "
                      f"pour le maillage {preset_count:,}.".replace(",", " "))
        self._on_candidate_selection()  # refresh the plan text with the choice

    def _manual_zone_from_bbox(self, bbox: tuple[float, float, float, float]) -> SubZone | None:
        dem = self._candidate_dem
        cfg = self._screening_cfg
        if dem is None:
            return None
        left, bottom, right, top = dem.bounds
        x0, y0, x1, y1 = bbox
        x0, x1 = max(left, x0), min(right, x1)
        y0, y1 = max(bottom, y0), min(top, y1)
        if (x1 - x0) < 150.0 or (y1 - y0) < 150.0:
            return None
        res = float(dem.resolution_m)
        h_px, w_px = dem.shape

        def clamp_index(value: float, upper: int) -> int:
            return min(max(0, int(value)), upper)

        r0 = clamp_index((top - y1) / res, h_px)
        r1 = clamp_index((top - y0) / res, h_px)
        c0 = clamp_index((x0 - left) / res, w_px)
        c1 = clamp_index((x1 - left) / res, w_px)
        if r1 - r0 < 2 or c1 - c0 < 2:
            return None
        elev = self._candidate_dem.elevation[r0:r1, c0:c1]
        finite = elev[np.isfinite(elev)]
        relief = (float(finite.max()) - float(finite.min())) if finite.size else 0.0
        crest = float(np.nanpercentile(elev, 80)) if finite.size else 0.0
        target_res = float(getattr(cfg, "target_res_m", 10.0) if cfg is not None else 10.0)
        return SubZone(
            bbox=(x0, y0, x1, y1),
            center=((x0 + x1) / 2.0, (y0 + y1) / 2.0),
            crest_alt_m=crest,
            relief_m=relief,
            est_cells=estimate_cells(x1 - x0, y1 - y0, target_res),
            pixel_window=(r0, r1, c0, c1),
        )

    def _on_run_selected_candidates(self) -> None:
        if self._screening_result is None or self._screening_cfg is None or self._job is not None:
            return
        rows = self._selected_candidate_indices()
        if not rows:
            QtWidgets.QMessageBox.information(self, "Candidats", "Sélectionne au moins un candidat.")
            return
        zones = tuple(self._screening_result.partition[i] for i in rows)
        cfg = self._manual_pass2_cfg(zones)
        self._last_cfg = cfg
        cli, cache = self.cfg.windninja_cli, self.cfg.cache_dir

        def fn(on_progress, cancel):
            return run_auto(
                cfg, cli=cli, cache_dir=cache, on_progress=on_progress, cancel=cancel)

        unit = "scénario(s) vent" if cfg.wind_mode == WIND_MODE_MANUAL_GRID else "h"
        self._log(f"Pass-2 manuel — {len(zones)} candidat(s) sélectionné(s) × {len(cfg.hours)} {unit}")
        self._log("Plan Pass-2 manuel — " + self._manual_pass2_plan_text(len(zones), cfg))
        self._run_started = time.monotonic()
        self._last_pct = 0
        self._invalidate_shown_result()
        self._set_running(True)
        job = SolveJob(fn, self)
        job.progress.connect(self._on_progress)
        job.finished.connect(self._on_finished)
        job.failed.connect(self._on_failed)
        self._job = job
        job.start()

    def _set_running(self, running: bool) -> None:
        super()._set_running(running)
        for widget in (getattr(self, "selection_combo", None), getattr(self, "calc_combo", None),
                       getattr(self, "features_spin", None), getattr(self, "step_spin", None),
                       getattr(self, "topo_combo", None), getattr(self, "wind_mode_combo", None),
                       getattr(self, "speed_slider", None), getattr(self, "dir_slider", None),
                       getattr(self, "window_slider", None)):
            if widget is not None:
                widget.setEnabled(not running)
        if hasattr(self, "btn_run_selected"):
            self.btn_run_selected.setEnabled(
                not running and bool(self.candidate_list.selectedItems())
                and self._screening_result is not None)
        if not running:
            self._apply_workflow_controls()
            self._on_wind_mode_change()

    def _restore_controls(self, c: dict) -> None:
        try:
            self.margin_spin.setValue(float(c.get("corridor_margin_km", 2.0)))
            self.features_spin.setValue(int(c.get("max_features", DEFAULT_MAX_FEATURES)))
            self.step_spin.setValue(float(c.get("tile_step_m", 1500.0)) / 1000.0)
            mode = c.get("domain_mode", "features")
            idx = 2 if mode == "corridor" else 1 if mode == "features" else 0
            self.calc_combo.setCurrentIndex(idx)
            target = float(c.get("target_res_m", 10.0))
            self.topo_combo.setCurrentIndex(
                min(range(len(TOPO_PRESETS)), key=lambda i: abs(TOPO_PRESETS[i] - target)))
            mc = int(c.get("mesh_count", PASS2_MESH_PRESETS[PASS2_MESH_DEFAULT][0]))
            self.mesh_combo.setCurrentText(  # nearest preset to the saved mesh_count
                min(PASS2_MESH_PRESETS, key=lambda k: abs(PASS2_MESH_PRESETS[k][0] - mc)))
            wind_mode = c.get("wind_mode", WIND_MODE_FORECAST)
            wind_idx = 1 if wind_mode == WIND_MODE_MANUAL_GRID else 0
            self.wind_mode_combo.setCurrentIndex(wind_idx)
            speeds = [int(v) for v in c.get("manual_wind_speeds_kmh", [])]
            dirs = [int(v) for v in c.get("manual_wind_dirs_deg", [])]
            if speeds:
                self.speed_slider.setValue((min(speeds), max(speeds)))
            if dirs:
                raw_dirs = [360 if int(v) == 0 and max(dirs) > 270 else int(v) for v in dirs]
                self.dir_slider.setValue((min(raw_dirs), max(raw_dirs)))
            self._refresh_manual_wind_labels()
        except Exception:
            pass


def main() -> None:  # pragma: no cover
    import sys

    QtCore.QCoreApplication.setAttribute(QtCore.Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName("SousLeVent")
    translator = QtCore.QTranslator()
    tpath = QtCore.QLibraryInfo.path(QtCore.QLibraryInfo.LibraryPath.TranslationsPath)
    if translator.load("qtbase_fr", tpath):
        app.installTranslator(translator)
        app._fr_translator = translator
    win = SousLeVentWindow()
    win.resize(1400, 900)
    win.show()
    sys.exit(app.exec())
