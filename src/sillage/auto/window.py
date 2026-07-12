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
from ..viz import map2d
from ..wind.directions import direction_label
from . import AutoConfig, cleanup_auto_artifacts, run_auto
from .arome import forecast_window
from .pipeline import (
    DEFAULT_MAX_FEATURES,
    WIND_MODE_MANUAL_GRID,
    bbox_from_route,
    default_momentum_workers,
    detect_cores,
    manual_wind_scenarios,
    momentum_parallel_plan,
    wind_label_for_case,
)
from .wind import _sample_route, arrows_at_hour, route_wind_series

GREEN = ("QPushButton { font-weight:bold; padding:8px 18px; border-radius:5px;"
         " background:#2d7d2d; color:white; } QPushButton:disabled { background:#a9c7a9; }")
RENDER_BUTTON_TEXT = "Recalculer la vue 3D"
RENDER_BUSY_TEXT = "Calcul en cours..."

TOPO_PRESETS = (1.0, 5.0, 10.0, 25.0)
TOPO_LABELS = ("1 m (IGN)", "5 m", "10 m", "25 m")
NO_BASEMAP = "Aucun"

# ADR-0008 / ADR-0035: Pass-2 momentum mesh as a quality/time knob. Each preset = (mesh_count,
# iterations); finer is heavier. Ported from the manual app so the unified app has the same control.
PASS2_MESH_PRESETS: dict[str, tuple[int, int]] = {
    "Grossier — rapide": (20_000, 100),
    "Moyen — défaut": (50_000, 200),
    "Fin — lent": (150_000, 300),
    "Max — très lent": (400_000, 400),
}
PASS2_MESH_DEFAULT = "Moyen — défaut"


def pass2_estimate_minutes(mesh_count: int) -> int:
    """Rough per-solve runtime proxy (CPU-bound), ~25k cells → ~2 min. Indicative only (ADR-0008)."""
    return max(1, round(mesh_count / 12_000))


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
        self._manual_render_speeds = []
        self._manual_render_dirs = []
        self._manual_render_case_by_pair = {}
        self._opacity = 0.5      # rotor volume opacity (slider-controlled, see inside the thickness)
        self._wind_size_factor = 1.0    # wind-arrow reference size (× zoom autoscale); slider-set
        self._wind_altitude_m = 20.0    # wind-arrow height above ground (m, AGL); slider-set
        self._metric = "rotor"          # 3D colour metric (default rotor)
        # Display units. Ranges drive both extraction (min/max shown) and colour clamping.
        self._metric_ranges = {
            "rotor": (0.0, 15.0),             # km/h reversed-flow speed
            "horizontal": (-100.0, 50.0),     # % upstream wind; values > max hidden
            "vertical_sink": (-3.0, -1.0),    # m/s; values below min clamp red, > max hidden
            "vertical_lift": (1.0, 3.0),      # m/s; values below min hidden, > max clamp green
            "turbulence": (1.0, 3.0),         # rms m/s; values below min hidden
        }
        self._tex_cache = {}     # cached basemap textures so re-renders don't re-fetch tiles
        self._terrain_cache = {}  # cached terrain mesh (id(dem)->mesh) so scrubs don't rebuild it
        self._last_cfg = None    # AutoConfig of the shown result (for save)
        self._hour_labels = None  # {hour/scenario: display label} — kept for reopen/save
        self._loaded_dir = None   # temp dir holding an opened .sillage bundle (cleaned on close)
        self._restoring = False   # True while restoring an opened bundle: don't refetch route winds
        self._route_gen = 0       # bumped on every route change / open → stale wind fetches are dropped
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
        # Debounce the legend redraw: a range slider fires many valueChanged/s while dragging and
        # each redraw builds a matplotlib figure — coalesce them so the slider doesn't stutter.
        self._legend_timer = QtCore.QTimer(self)
        self._legend_timer.setSingleShot(True)
        self._legend_timer.setInterval(120)
        self._legend_timer.timeout.connect(self._update_legend)

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
        self.topo_combo.addItems(TOPO_LABELS)
        self.topo_combo.setCurrentIndex(TOPO_PRESETS.index(10.0))
        self.topo_combo.setToolTip(
            "Résolution du MNT. 1 m utilise les données IGN haute résolution quand disponibles "
            "et peut être très lourd : à réserver aux trajets courts.")
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

    def _add_range_slider(self, form, key: str, title: str, raw_min: int, raw_max: int,
                          value: tuple[float, float], tooltip: str) -> None:
        """Add one labelled range slider. Decimal sliders are stored as ints with a scale."""
        scale = self._range_scales[key]
        row = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        slider = QRangeSlider(QtCore.Qt.Horizontal)
        slider.setRange(raw_min, raw_max)
        slider.setValue((round(value[0] * scale), round(value[1] * scale)))
        slider.setFixedWidth(190)
        slider.setToolTip(tooltip)
        slider.valueChanged.connect(lambda *_args, k=key: self._on_range_slider_change(k))
        lab = QtWidgets.QLabel("")
        lab.setMinimumWidth(86)
        lay.addWidget(slider)
        lay.addWidget(lab)
        title_lab = QtWidgets.QLabel(title)
        form.addRow(title_lab, row)
        self._range_sliders[key] = slider
        self._range_labels[key] = lab
        self._range_rows[key] = row
        self._range_row_labels[key] = title_lab
        self._refresh_range_label(key)

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
        self.btn_web3d = QtWidgets.QPushButton("🌐 Export 3D web")
        self.btn_web3d.setToolTip(
            "Exporter la vue affichée (heure/scénario, représentation et seuils courants) en page "
            "HTML 3D interactive autonome — ouvrable dans un navigateur, partageable sur un site.")
        self.btn_web3d.setEnabled(False)
        self.btn_web3d.clicked.connect(self._on_export_web3d)
        srow.addWidget(self.btn_web3d)
        srow.addStretch(1)
        lay.addLayout(srow)
        outer.addLayout(lay, stretch=1)

        # Right panel: basemap, render case, metric legend/sliders, then the wind legend.
        right = QtWidgets.QVBoxLayout()
        self._render_settings_layout = right

        basemap_row = QtWidgets.QFormLayout()
        self._basemap_form_layout = basemap_row
        self.render_basemap_combo = QtWidgets.QComboBox()
        self.render_basemap_combo.addItems([NO_BASEMAP, *map2d.BASEMAP_SOURCES.keys()])
        self.render_basemap_combo.setCurrentText("IGN plan")
        self.render_basemap_combo.setToolTip(
            "Fond drapé sur le relief 3D pour se repérer. Si les tuiles réseau sont indisponibles, "
            "le rendu retombe sur l'ombrage du relief.")
        basemap_row.addRow("Fond :", self.render_basemap_combo)  # applied on "Appliquer le rendu"
        right.addLayout(basemap_row)

        self.forecast_render_widget = QtWidgets.QWidget()
        forecast_form = QtWidgets.QFormLayout(self.forecast_render_widget)
        forecast_form.setContentsMargins(0, 0, 0, 0)
        forecast_row = QtWidgets.QWidget()
        forecast_lay = QtWidgets.QHBoxLayout(forecast_row)
        forecast_lay.setContentsMargins(0, 0, 0, 0)
        self.hour_title_label = QtWidgets.QLabel("Créneau :")
        self.hour_start_label = QtWidgets.QLabel("")
        self.hour_start_label.setStyleSheet("color:#888; font-size:10px;")
        forecast_lay.addWidget(self.hour_start_label)
        self.hour_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.hour_slider.setEnabled(False)
        self.hour_slider.valueChanged.connect(self._on_hour_change)
        forecast_lay.addWidget(self.hour_slider, stretch=1)
        self.hour_end_label = QtWidgets.QLabel("")
        self.hour_end_label.setStyleSheet("color:#888; font-size:10px;")
        forecast_lay.addWidget(self.hour_end_label)
        forecast_form.addRow(self.hour_title_label, forecast_row)
        right.addWidget(self.forecast_render_widget)

        self.manual_render_widget = QtWidgets.QWidget()
        manual_form = QtWidgets.QFormLayout(self.manual_render_widget)
        manual_form.setContentsMargins(0, 0, 0, 0)
        speed_row = QtWidgets.QWidget()
        speed_lay = QtWidgets.QHBoxLayout(speed_row)
        speed_lay.setContentsMargins(0, 0, 0, 0)
        self.render_speed_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.render_speed_slider.setFixedWidth(150)
        self.render_speed_slider.valueChanged.connect(self._on_manual_render_change)
        speed_lay.addWidget(self.render_speed_slider)
        self.render_speed_label = QtWidgets.QLabel("")
        self.render_speed_label.setMinimumWidth(80)
        speed_lay.addWidget(self.render_speed_label)
        manual_form.addRow("Vitesse :", speed_row)
        dir_row = QtWidgets.QWidget()
        dir_lay = QtWidgets.QHBoxLayout(dir_row)
        dir_lay.setContentsMargins(0, 0, 0, 0)
        self.render_dir_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.render_dir_slider.setFixedWidth(150)
        self.render_dir_slider.valueChanged.connect(self._on_manual_render_change)
        dir_lay.addWidget(self.render_dir_slider)
        self.render_dir_label = QtWidgets.QLabel("")
        self.render_dir_label.setMinimumWidth(110)
        dir_lay.addWidget(self.render_dir_label)
        manual_form.addRow("Orientation :", dir_row)
        self.manual_render_widget.setVisible(False)
        right.addWidget(self.manual_render_widget)

        self.hour_label = QtWidgets.QLabel("")
        self.hour_label.setStyleSheet("font-weight:bold;")
        self.hour_label.setWordWrap(True)
        self.hour_label.setMinimumWidth(150)
        right.addWidget(self.hour_label)
        right.addSpacing(10)

        mrow = QtWidgets.QFormLayout()
        self._metric_choice_layout = mrow
        self.metric_combo = QtWidgets.QComboBox()
        self.metric_combo.addItems(self.METRICS_LABELS)
        self.metric_combo.setCurrentIndex(self.METRICS.index(self._metric))
        self.metric_combo.setToolTip(
            "Grandeur visualisée dans le volume sous le vent :\n"
            "• Rotor — flux inversé (recirculation)\n"
            "• Vitesse horizontale — % du vent amont (rouge = flux inversé, vert = vent établi)\n"
            "• Vitesse verticale — m/s (vert = ascendance, rouge = dégueulante)\n"
            "• Turbulence — intensité de turbulence")
        self.metric_combo.currentIndexChanged.connect(self._on_metric_change)
        mrow.addRow("Représentation :", self.metric_combo)
        right.addLayout(mrow)

        self.legend_label = QtWidgets.QLabel()
        self.legend_label.setFixedWidth(250)
        self.legend_label.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignHCenter)
        right.addWidget(self.legend_label)

        self._range_sliders = {}
        self._range_labels = {}
        self._range_rows = {}
        self._range_row_labels = {}
        self._range_scales = {
            "rotor": 1.0, "horizontal": 1.0,
            "vertical_sink": 10.0, "vertical_lift": 10.0, "turbulence": 10.0,
        }
        self._range_suffixes = {
            "rotor": " km/h", "horizontal": " %",
            "vertical_sink": " m/s", "vertical_lift": " m/s", "turbulence": " m/s",
        }
        form = QtWidgets.QFormLayout()
        self._metric_slider_layout = form
        self._add_range_slider(form, "rotor", "Rotor :",
                               0, 60, self._metric_ranges["rotor"],
                               "Vitesse de flux inversé représentée. Sous le min : masqué; "
                               "au-dessus du max : couleur max.")
        self._add_range_slider(form, "horizontal", "Horizontale :",
                               -100, 100, self._metric_ranges["horizontal"],
                               "% du vent amont. Sous le min : rouge; au-dessus du max : masqué.")
        self._add_range_slider(form, "vertical_sink", "Dégueulantes :",
                               -100, 0, self._metric_ranges["vertical_sink"],
                               "Vitesse verticale négative. Sous le min : rouge; au-dessus du max : masqué.")
        self._add_range_slider(form, "vertical_lift", "Ascendances :",
                               0, 100, self._metric_ranges["vertical_lift"],
                               "Vitesse verticale positive. Sous le min : masqué; au-dessus du max : vert.")
        self._add_range_slider(form, "turbulence", "Turbulence :",
                               0, 150, self._metric_ranges["turbulence"],
                               "Turbulence rms. Sous le min : masquée; au-dessus du max : couleur max.")
        right.addLayout(form)
        self.btn_apply_scale = QtWidgets.QPushButton(RENDER_BUTTON_TEXT)
        self.btn_apply_scale.setStyleSheet(GREEN)
        self.btn_apply_scale.setToolTip(
            "Recalcule le rendu 3D avec le cas, la métrique, le fond et les seuils affichés.")
        self.btn_apply_scale.clicked.connect(self._on_apply_scale)
        right.addWidget(self.btn_apply_scale)

        # Opacity sits with the render params (live, actor-level — no rebuild).
        opacity_row = QtWidgets.QHBoxLayout()
        opacity_row.addWidget(QtWidgets.QLabel("Opacité volumes :"))
        self.opacity_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.opacity_slider.setRange(5, 100)
        self.opacity_slider.setValue(int(self._opacity * 100))
        self.opacity_slider.setToolTip(
            "Transparence des volumes sous le vent — baisse pour voir l'intérieur de l'épaisseur.")
        self.opacity_slider.valueChanged.connect(self._on_opacity_change)
        opacity_row.addWidget(self.opacity_slider, stretch=1)
        right.addLayout(opacity_row)

        right.addStretch(1)

        self.wind_legend_label = QtWidgets.QLabel()
        self.wind_legend_label.setAlignment(QtCore.Qt.AlignHCenter)
        right.addWidget(self.wind_legend_label)

        # Wind-arrow controls BELOW the wind-speed legend, side by side and compact: reference size
        # (stays proportional to zoom) + AGL altitude (0 → 300 m). Live (no scene rebuild).
        wind_row = QtWidgets.QHBoxLayout()
        wind_row.addWidget(QtWidgets.QLabel("Flèches — taille"))
        self.wind_size_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.wind_size_slider.setRange(20, 400)                 # 0.2× … 4.0× the reference size
        self.wind_size_slider.setValue(int(self._wind_size_factor * 100))
        self.wind_size_slider.setFixedWidth(70)
        self.wind_size_slider.setToolTip(
            "Taille de référence des flèches de vent. Elle reste ensuite proportionnelle au zoom.")
        self.wind_size_slider.valueChanged.connect(self._on_wind_style_change)
        wind_row.addWidget(self.wind_size_slider)
        wind_row.addSpacing(10)
        wind_row.addWidget(QtWidgets.QLabel("alt. sol"))
        self.wind_alt_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.wind_alt_slider.setRange(0, 300)                   # 0 … 300 m above ground
        self.wind_alt_slider.setValue(int(self._wind_altitude_m))
        self.wind_alt_slider.setFixedWidth(70)
        self.wind_alt_slider.setToolTip("Hauteur des flèches de vent au-dessus du sol (0 → 300 m).")
        self.wind_alt_slider.valueChanged.connect(self._on_wind_style_change)
        wind_row.addWidget(self.wind_alt_slider)
        self.wind_style_label = QtWidgets.QLabel(
            f"{int(self._wind_size_factor * 100)} % · {int(self._wind_altitude_m)} m")
        self.wind_style_label.setStyleSheet("color:#888; font-size:10px;")
        wind_row.addWidget(self.wind_style_label)
        wind_row.addStretch(1)
        right.addLayout(wind_row)
        outer.addLayout(right)
        self._apply_metric_controls()
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
        gen = self._route_gen  # tag this fetch; if the route changes / a bundle opens, drop the result

        def fn(on_progress, cancel):
            cells = []
            for s in segs:  # one wind series per segment so gaps carry no arrows
                cells += route_wind_series(s, n_hours)
            return gen, cells

        job = SolveJob(fn, self)
        job.finished.connect(self._on_winds_fetched)
        job.failed.connect(lambda _msg: setattr(self, "_wind_job", None))
        self._wind_job = job
        job.start()

    def _on_winds_fetched(self, payload) -> None:
        self._wind_job = None
        gen, cells = payload
        if gen != self._route_gen:  # route changed / bundle opened while fetching -> stale, drop it
            if any(len(s) >= 2 for s in (self._route or [])) and self._loaded_dir is None:
                self._wind_timer.start()  # refetch for the current route (unless a bundle is shown)
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
        nseg = len(self._route)
        npts = sum(len(s) for s in self._route)
        ready = "" if any(len(s) >= 2 for s in self._route) else "  (ajoute au moins 2 points)"
        self.info.setText(
            f"Parcours : {nseg} segment(s), {npts} point(s) · "
            f"corridor {self.margin_spin.value():.1f} km{ready}")
        self._refresh_cpu_plan()
        if self._restoring:  # restoring an opened bundle: keep the RUN's saved winds, don't refetch
            return
        self._route_gen += 1          # invalidate any wind fetch in flight for the old route
        self._route_cells = []
        self.map_tab.show_wind([])
        if (hasattr(self, "_wind_mode")
                and self._wind_mode() == WIND_MODE_MANUAL_GRID):
            return
        if any(len(s) >= 2 for s in self._route):
            self._wind_timer.start()  # (re)fetch AROME along the route once drawing pauses

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
        topo_res = TOPO_PRESETS[self.topo_combo.currentIndex()]
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
        # run_auto's start-cleanup deletes the PREVIOUS run's case dirs; drop the now-stale result
        # from the UI so the user can't scrub/save a result whose backing files are about to vanish.
        self._invalidate_shown_result()
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
            self._wind_job.shutdown(1000)  # read-only fetch: stop it (terminate if blocked) & close
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
        configured_hours = tuple(getattr(self._last_cfg, "hours", ()))
        expected = len(result.partition) * len(configured_hours)
        nfail = len(result.failures)
        done = (f"Terminé : {len(result.cases)}/{expected} cas réussis sur "
                f"{len(result.partition)} domaine(s) × {len(configured_hours)} "
                f"heure(s)/scénario(s){f', {nfail} échec(s)' if nfail else ''} — "
                f"{result.timings_summary}")
        self.statusBar().showMessage(done)
        self._log(done)
        self.avancement.setText(f"Terminé — {result.timings_summary}")
        if not hours:
            return
        self._hour_labels = self._labels_for_cfg(self._last_cfg, hours)
        self._show_result_hours(hours)

    def _invalidate_shown_result(self) -> None:
        """Drop the currently displayed result from the UI (used when a new run starts, since its
        backing case files are about to be deleted). Save/scrub are disabled until a result exists."""
        self._result = None
        self._rotor_cache = {}
        self.btn_save.setEnabled(False)
        if hasattr(self, "btn_web3d"):
            self.btn_web3d.setEnabled(False)
        self.hour_slider.setEnabled(False)
        if hasattr(self, "hour_label"):
            self.hour_label.setText("")
        if hasattr(self, "render_speed_slider"):
            self.render_speed_slider.setEnabled(False)
            self.render_dir_slider.setEnabled(False)
        if self._plotter is not None:
            try:
                self._plotter.clear()
                self._plotter.render()
            except Exception:
                pass

    def _show_result_hours(self, hours) -> None:
        """Set up the hour slider (absolute-date labels, disabled for a single créneau) and render
        the first hour. Shared by a finished run and an opened ``.sillage`` result."""
        self._rotor_cache = {}          # new result → different cases/DEM: drop the per-render caches
        self._tex_cache = {}            # (also avoids unbounded growth across runs/opens)
        self._terrain_cache = {}
        if self._is_manual_wind_result():
            self._setup_manual_render_controls(hours)
            self._set_forecast_render_controls_visible(False)
        else:
            self._set_forecast_render_controls_visible(True)
            self.hour_slider.blockSignals(True)
            self.hour_slider.setMinimum(0)
            self.hour_slider.setMaximum(max(0, len(hours) - 1))
            self.hour_slider.setValue(0)
            self.hour_slider.setEnabled(len(hours) > 1)  # nothing to scrub for a single créneau
            self.hour_slider.blockSignals(False)
            self.hour_start_label.setText(self._label_for_hour(hours[0]))
            self.hour_end_label.setText(self._label_for_hour(hours[-1]))
        self.btn_save.setEnabled(True)
        if hasattr(self, "btn_web3d"):
            self.btn_web3d.setEnabled(True)
        self.tabs.setCurrentWidget(self._render_tab)
        self._refresh_render_case_label()
        self._render_hour(self._current_render_index())

    def _on_export_web3d(self) -> None:
        """Export the DISPLAYED hour/representation as a standalone interactive HTML (vtk.js)."""
        if self._result is None or self._dem is None:
            return
        hours = self._result.hours
        idx = self._current_render_index()
        if not (0 <= idx < len(hours)):
            return
        hour = hours[idx]
        label = self._label_for_hour(hour)
        safe = "".join(ch if ch.isalnum() else "_" for ch in f"{self._metric}_{label}").strip("_")
        suggested = str(self.cfg.output_dir / f"souslevent_3d_{safe}.html")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Exporter la vue 3D en HTML interactif", suggested, "Page web (*.html)")
        if not path:
            return
        from pathlib import Path

        from .scene import export_web_html

        self.statusBar().showMessage("Export 3D web en cours…")
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            basemap = (self.render_basemap_combo.currentText()
                       if hasattr(self, "render_basemap_combo") else "IGN plan")
            out = export_web_html(
                self._dem, self._result.cases_for_hour(hour), path,
                metric=self._metric, metric_range=self._native_metric_range(),
                route_winds=self._route_winds_utm(hour), rotor_opacity=self._opacity,
                wind_size_factor=self._wind_size_factor, wind_altitude_m=self._wind_altitude_m,
                crs=self._dem.crs, basemap_source=basemap,  # baked into vertex colours for the web
                title=f"SousLeVent — {label}")
        except Exception as exc:  # pragma: no cover - surfaced to the UI
            QtWidgets.QMessageBox.critical(self, "Export 3D web", str(exc))
            return
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        size_mb = Path(out).stat().st_size / (1024 * 1024)
        self.statusBar().showMessage(f"Export 3D web : {out} ({size_mb:.1f} Mo)")
        self._log(f"Vue 3D exportée en HTML interactif ({size_mb:.1f} Mo) → {out}")

    # --- save / open results -------------------------------------------------
    def _on_save(self) -> None:
        if self._result is None or self._last_cfg is None:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Sauvegarder le résultat", "", "Résultat Sillage (*.sillage)")
        if not path:
            return
        choice = QtWidgets.QMessageBox.question(
            self, "Type de sauvegarde",
            "Sauvegarde ré-analysable ?\n\n"
            "Oui : stocke les valeurs sources compactes pour pouvoir changer les seuils volume "
            "après réouverture (fichier plus lourd).\n"
            "Non : sauvegarde compacte, les seuils volume restent figés.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Yes)
        if choice == QtWidgets.QMessageBox.Cancel:
            return
        include_sources = choice == QtWidgets.QMessageBox.Yes
        from .store import save_result

        labels = self._hour_labels or {h: self._label_for_hour(h) for h in self._result.hours}
        try:
            out = save_result(path, self._result, cfg=self._last_cfg, hour_labels=labels,
                              route_cells=self._route_cells, include_sources=include_sources,
                              temp_dir=self.cfg.temp_dir)
        except Exception as exc:  # pragma: no cover - surfaced to the UI
            QtWidgets.QMessageBox.critical(self, "Sauvegarde", str(exc))
            return
        self.statusBar().showMessage(f"Sauvegardé : {out}")
        mode = "ré-analysable" if include_sources else "compact"
        self._log(f"Résultat sauvegardé ({mode}) → {out}")

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

        import shutil

        self._cleanup_loaded()
        open_tmp = self.cfg.temp_dir / "sillage_open"
        open_tmp.mkdir(parents=True, exist_ok=True)
        for stale in open_tmp.glob("sillage_open_*"):  # sweep dirs leaked by crashed/killed sessions
            shutil.rmtree(str(stale), ignore_errors=True)
        dest = Path(tempfile.mkdtemp(prefix="sillage_open_", dir=str(open_tmp)))
        try:
            loaded = load_result(path, dest)
            self._dem = load_dem(loaded.result.dem_path, max_domain_km=200.0)
        except Exception as exc:
            shutil.rmtree(str(dest), ignore_errors=True)
            QtWidgets.QMessageBox.critical(self, "Ouverture", str(exc))
            return
        self._loaded_dir = dest
        self._result = loaded.result
        self._hour_labels = loaded.hour_labels
        self._route = [list(s) for s in loaded.route_segments]
        self._route_cells = loaded.route_cells or []  # the RUN's winds (not today's) for the arrows
        self._route_gen += 1  # any wind fetch in flight is now stale (keep the bundle's saved winds)
        self._last_cfg = self._cfg_from_dict(loaded.config, loaded.result.hours)
        self._restoring = True  # restore controls WITHOUT the setValue chain refetching today's wind
        try:
            self._restore_controls(loaded.config)
        finally:
            self._restoring = False
        self._rendered = False  # fit the camera to the opened scene
        hours = loaded.result.hours
        self._log(f"Ouvert : {Path(path).name} — {len(loaded.result.cases)} cas, "
                  f"{len(hours)} h, mode {loaded.config.get('domain_mode', '?')}, "
                  f"stockage {loaded.storage_mode}")
        if any(getattr(c, "source_path", "") for c in loaded.result.cases):
            self._log("Seuils volume ré-extractibles : les sources compactes sont présentes.")
        else:
            self._log("Seuils volume figés : ce fichier ne contient que des volumes déjà extraits.")
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
                wind_source=c.get("wind_source", "open_meteo"),
                wind_mode=c.get("wind_mode", "forecast"),
                manual_wind_speeds_kmh=tuple(
                    int(v) for v in c.get("manual_wind_speeds_kmh", ())),
                manual_wind_dirs_deg=tuple(
                    int(v) for v in c.get("manual_wind_dirs_deg", ())))
        except Exception:
            return None

    def _restore_controls(self, c: dict) -> None:
        try:
            self.margin_spin.setValue(float(c.get("corridor_margin_km", 2.0)))
            self.features_spin.setValue(int(c.get("max_features", DEFAULT_MAX_FEATURES)))
            self.step_spin.setValue(float(c.get("tile_step_m", 1500.0)) / 1000.0)
            self.pave_check.setChecked(c.get("domain_mode") == "corridor")
            target = float(c.get("target_res_m", 10.0))
            self.topo_combo.setCurrentIndex(
                min(range(len(TOPO_PRESETS)), key=lambda i: abs(TOPO_PRESETS[i] - target)))
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

    def _refresh_render_case_label(self) -> None:
        if not hasattr(self, "hour_label"):
            return
        if self._is_manual_wind_result():
            self._refresh_manual_render_labels()
            return
        if self._result is None or not self._result.hours:
            self.hour_label.setText("")
            return
        idx = min(max(int(self.hour_slider.value()), 0), len(self._result.hours) - 1)
        self.hour_label.setText(self._label_for_hour(self._result.hours[idx]))

    def _on_hour_change(self, _idx: int) -> None:
        self._refresh_render_case_label()

    def _is_manual_wind_result(self) -> bool:
        return self._last_cfg is not None and getattr(
            self._last_cfg, "wind_mode", "") == WIND_MODE_MANUAL_GRID

    def _set_forecast_render_controls_visible(self, visible: bool) -> None:
        forecast_widget = getattr(self, "forecast_render_widget", None)
        if forecast_widget is not None:
            forecast_widget.setVisible(visible)
        for widget in (
            getattr(self, "hour_title_label", None),
            getattr(self, "hour_start_label", None),
            getattr(self, "hour_slider", None),
            getattr(self, "hour_end_label", None),
        ):
            if widget is not None:
                widget.setVisible(visible)
        if hasattr(self, "manual_render_widget"):
            self.manual_render_widget.setVisible(not visible)

    def _setup_manual_render_controls(self, hours) -> None:
        available = set(hours)
        scenarios = [
            (sid, spd, drc) for sid, spd, drc in manual_wind_scenarios(self._last_cfg)
            if sid in available
        ]
        speeds = []
        dirs = []
        case_by_pair = {}
        for sid, spd, drc in scenarios:
            if spd not in speeds:
                speeds.append(spd)
            if drc not in dirs:
                dirs.append(drc)
            case_by_pair[(spd, drc)] = sid
        self._manual_render_speeds = speeds
        self._manual_render_dirs = dirs
        self._manual_render_case_by_pair = case_by_pair
        for slider, values in (
            (self.render_speed_slider, speeds),
            (self.render_dir_slider, dirs),
        ):
            slider.blockSignals(True)
            slider.setMinimum(0)
            slider.setMaximum(max(0, len(values) - 1))
            slider.setValue(0)
            slider.setEnabled(len(values) > 1)
            slider.blockSignals(False)
        self.hour_slider.blockSignals(True)
        self.hour_slider.setMinimum(0)
        self.hour_slider.setMaximum(max(0, len(hours) - 1))
        self.hour_slider.setValue(0)
        self.hour_slider.setEnabled(False)
        self.hour_slider.blockSignals(False)
        self._refresh_manual_render_labels()

    def _selected_manual_case_id(self) -> int | None:
        if not (self._manual_render_speeds and self._manual_render_dirs):
            return None
        spd = self._manual_render_speeds[
            min(self.render_speed_slider.value(), len(self._manual_render_speeds) - 1)]
        drc = self._manual_render_dirs[
            min(self.render_dir_slider.value(), len(self._manual_render_dirs) - 1)]
        return self._manual_render_case_by_pair.get((spd, drc))

    def _refresh_manual_render_labels(self) -> None:
        sid = self._selected_manual_case_id()
        if sid is None:
            self.render_speed_label.setText("")
            self.render_dir_label.setText("")
            self.hour_label.setText("")
            return
        spd = self._manual_render_speeds[
            min(self.render_speed_slider.value(), len(self._manual_render_speeds) - 1)]
        drc = self._manual_render_dirs[
            min(self.render_dir_slider.value(), len(self._manual_render_dirs) - 1)]
        self.render_speed_label.setText(f"{spd} km/h")
        self.render_dir_label.setText(direction_label(drc))
        self.hour_label.setText(self._label_for_hour(sid))

    def _current_render_index(self) -> int:
        if self._is_manual_wind_result() and self._result is not None:
            sid = self._selected_manual_case_id()
            if sid is not None:
                try:
                    return self._result.hours.index(sid)
                except ValueError:
                    return 0
        return int(self.hour_slider.value())

    def _on_manual_render_change(self, *_args) -> None:
        self._refresh_manual_render_labels()

    def _coerce_slider_range(self, key: str, raw: tuple[int, int]) -> tuple[int, int]:
        lo, hi = sorted((int(raw[0]), int(raw[1])))
        if key == "horizontal":
            lo = min(lo, -1)
            hi = max(hi, 1)
        elif key == "vertical_sink":
            hi = min(hi, 0)
        elif key == "vertical_lift":
            lo = max(lo, 0)
        if lo == hi:
            hi = min(hi + 1, self._range_sliders[key].maximum())
            lo = min(lo, hi - 1)
        return lo, hi

    def _range_value(self, key: str) -> tuple[float, float]:
        return self._metric_ranges[key]

    def _format_value(self, value: float, key: str) -> str:
        if key in ("vertical_sink", "vertical_lift", "turbulence"):
            return f"{value:+.1f}" if key.startswith("vertical") else f"{value:.1f}"
        return f"{value:+.0f}" if key == "horizontal" else f"{value:.0f}"

    def _refresh_range_label(self, key: str) -> None:
        if key not in getattr(self, "_range_labels", {}):
            return
        lo, hi = self._range_value(key)
        suffix = self._range_suffixes[key]
        self._range_labels[key].setText(
            f"{self._format_value(lo, key)} → {self._format_value(hi, key)}{suffix}")

    def _on_range_slider_change(self, key: str) -> None:
        slider = self._range_sliders[key]
        raw = self._coerce_slider_range(key, tuple(slider.value()))
        if tuple(slider.value()) != raw:
            slider.blockSignals(True)
            slider.setValue(raw)
            slider.blockSignals(False)
        scale = self._range_scales[key]
        self._metric_ranges[key] = (raw[0] / scale, raw[1] / scale)
        self._refresh_range_label(key)
        self._legend_timer.start()  # debounced legend redraw (coalesce drag ticks)

    def _apply_metric_controls(self) -> None:
        visible = {
            "rotor": {"rotor"},
            "horizontal": {"horizontal"},
            "vertical": {"vertical_sink", "vertical_lift"},
            "turbulence": {"turbulence"},
        }.get(self._metric, {"rotor"})
        for key, row in self._range_rows.items():
            row.setVisible(key in visible)
            self._range_row_labels[key].setVisible(key in visible)

    def _native_metric_range(self) -> dict:
        """Return the active slider ranges in the units stored in the VTK arrays."""
        if self._metric == "rotor":
            lo, hi = self._metric_ranges["rotor"]
            return {"min": lo / 3.6, "max": hi / 3.6}  # UI km/h, array m/s
        if self._metric == "horizontal":
            lo, hi = self._metric_ranges["horizontal"]
            return {"min": lo, "max": hi}
        if self._metric == "vertical":
            slo, shi = self._metric_ranges["vertical_sink"]
            llo, lhi = self._metric_ranges["vertical_lift"]
            return {"sink_min": slo, "sink_max": shi, "lift_min": llo, "lift_max": lhi}
        lo, hi = self._metric_ranges["turbulence"]
        return {"min": lo, "max": hi}

    def _legend_image(self):
        from ..viz.volume3d import (
            _rotor_intensity_cmap,
            _vertical_motion_cmap,
            _wind_balance_cmap,
            range_legend_image,
        )

        m = self._metric
        if m == "rotor":
            lo, hi = self._metric_ranges["rotor"]
            return range_legend_image(lo, hi, _rotor_intensity_cmap(), "Intensité rotor (km/h)",
                                      "Sous min masqué · au-dessus = max")
        if m == "turbulence":
            lo, hi = self._metric_ranges["turbulence"]
            return range_legend_image(lo, hi, _rotor_intensity_cmap(), "Turbulence rms (m/s)",
                                      "Sous min masqué · au-dessus = max")
        if m == "horizontal":
            lo, hi = self._metric_ranges["horizontal"]
            return range_legend_image(lo, hi, _wind_balance_cmap(), "Vitesse horizontale (% vent)",
                                      "Rouge · jaune = 0 · vert", center=0.0)
        slo, _shi = self._metric_ranges["vertical_sink"]
        _llo, lhi = self._metric_ranges["vertical_lift"]
        return range_legend_image(slo, lhi, _vertical_motion_cmap(), "Vitesse verticale (m/s)",
                                  "Rouge · jaune pâle = 0 · vert", center=0.0)

    def _update_legend(self) -> None:
        """Refresh the legend image for the active metric/scales."""
        if not hasattr(self, "legend_label"):
            return
        try:
            from ..app.qt_image import set_label_image
            set_label_image(self.legend_label, self._legend_image())
        except Exception:
            pass

    def _set_wind_legend(self) -> None:
        """Show the static continuous wind-speed colourbar (0–40 km/h) in the 3D panel."""
        if not hasattr(self, "wind_legend_label"):
            return
        try:
            from ..app.qt_image import set_label_image
            from ..viz.volume3d import wind_legend_image
            set_label_image(self.wind_legend_label, wind_legend_image())
        except Exception:
            pass

    def _on_apply_scale(self) -> None:
        if self._result is None:
            return
        # Recolour/re-extract at the new scales (texture cached) and give Qt one event pass so
        # the disabled/busy state is visible before the heavier scene rebuild starts.
        self.btn_apply_scale.setEnabled(False)
        self.btn_apply_scale.setText(RENDER_BUSY_TEXT)
        self.statusBar().showMessage(RENDER_BUSY_TEXT)
        QtWidgets.QApplication.processEvents()
        try:
            self._render_hour(self._current_render_index())
        finally:
            self.btn_apply_scale.setText(RENDER_BUTTON_TEXT)
            self.btn_apply_scale.setEnabled(True)
            self.statusBar().showMessage("Vue 3D recalculée")

    def _on_metric_change(self, idx: int) -> None:
        self._metric = self.METRICS[idx] if 0 <= idx < len(self.METRICS) else "rotor"
        self._apply_metric_controls()
        self._update_legend()

    def _on_opacity_change(self, val: int) -> None:
        """Set the rotor volumes' opacity live (actor-level, no scene rebuild / basemap refetch)."""
        self._opacity = max(0.02, val / 100.0)
        if self._plotter is not None:
            from ..viz.volume3d import set_rotor_opacity
            set_rotor_opacity(self._plotter, self._opacity)

    def _on_wind_style_change(self, *_a) -> None:
        """Live-adjust the wind arrows' reference size + AGL altitude (no scene rebuild)."""
        self._wind_size_factor = self.wind_size_slider.value() / 100.0
        self._wind_altitude_m = float(self.wind_alt_slider.value())
        if hasattr(self, "wind_style_label"):
            self.wind_style_label.setText(
                f"{self.wind_size_slider.value()} % · {int(self._wind_altitude_m)} m")
        if self._plotter is not None:
            from ..viz.volume3d import set_wind_arrow_style
            set_wind_arrow_style(self._plotter, size_factor=self._wind_size_factor,
                                 altitude_m=self._wind_altitude_m)

    def _label_for_hour(self, hour: int) -> str:
        """Absolute date label for an hour offset — the saved labels for an opened result (its run
        day), else today's forecast window."""
        if self._hour_labels and hour in self._hour_labels:
            return self._hour_labels[hour]
        if self._last_cfg is not None and getattr(self._last_cfg, "wind_mode", "") == WIND_MODE_MANUAL_GRID:
            return wind_label_for_case(self._last_cfg, hour)
        try:
            return self._fc.label_at(hour)
        except Exception:
            return f"{hour:02d}h"

    def _labels_for_cfg(self, cfg, hours) -> dict:
        if cfg is not None and getattr(cfg, "wind_mode", "") == WIND_MODE_MANUAL_GRID:
            return {h: wind_label_for_case(cfg, h) for h in hours}
        return {h: self._fc.label_at(h) for h in hours}

    def _route_winds_utm(self, hour: int):
        """AROME route arrows for ``hour`` as ``[(x, y, speed_ms, from_deg), …]`` in the DEM CRS."""
        if self._dem is None:
            return []
        if self._last_cfg is not None and getattr(self._last_cfg, "wind_mode", "") == WIND_MODE_MANUAL_GRID:
            scenarios = {sid: (spd / 3.6, drc) for sid, spd, drc in manual_wind_scenarios(self._last_cfg)}
            if int(hour) not in scenarios:
                return []
            spd, drc = scenarios[int(hour)]
            pts, seen = [], set()
            for seg in (self._route or []):
                for lat, lon in _sample_route(seg, spacing_km=1.5):
                    key = (round(lat, 5), round(lon, 5))
                    if key not in seen:
                        seen.add(key)
                        pts.append((lat, lon, spd, drc))
            arrows = pts
        else:
            if not self._route_cells:
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
        basemap_source = (
            self.render_basemap_combo.currentText()
            if hasattr(self, "render_basemap_combo") else "IGN plan"
        )
        populate_auto_scene(self._plotter, self._dem, self._result.cases_for_hour(hour),
                            crs=self._dem.crs, basemap_source=basemap_source,
                            route_winds=self._route_winds_utm(hour),
                            rotor_cache=self._rotor_cache, rotor_opacity=self._opacity,
                            metric=self._metric, metric_range=self._native_metric_range(),
                            texture_cache=self._tex_cache, terrain_cache=self._terrain_cache,
                            wind_size_factor=self._wind_size_factor,
                            wind_altitude_m=self._wind_altitude_m)
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
