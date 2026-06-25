"""Sillage main window (ADR-0009).

Controls panel + two tabs — Pass-1 screening (2D, embedded matplotlib) and Pass-2 detail
(3D, embedded pyvistaqt). It reuses the headless rendering (viz.map2d, viz.volume3d) and runs
long WindNinja/OpenFOAM solves on a worker thread (jobs.SolveJob). Pass-1 (triage) and Pass-2
(detail) are kept as distinct tabs on purpose (ADR-0005). User-facing strings are in French.
"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from superqt import QRangeSlider

from ..config import load_config, resolve_cache_path
from ..flow.windninja import format_run_failure, run_momentum
from ..screening import indicator as ind
from ..screening.pass1 import (
    find_direction_grid,
    hourly_indicator,
    synthetic_series,
    upstream_crest_wind,
)
from ..terrain.dem import crop_dem, load_dem, write_dem
from ..viz import map2d, volume3d
from .jobs import SolveJob
from .map_tab import MapTab

DEFAULT_DEM = "cache/champsaur/ign/champsaur_rgealti_50m_prepared_utm.tif"
NO_BASEMAP = "Aucun"
# AROME-class fine-forecast horizon (h) from now; caps the flight-window slider. When the
# real Météo-France AROME GRIB is wired, replace with the actual run's last valid hour.
FORECAST_HORIZON_H = 48

# MNT (DEM) resolution presets for "Valider la zone" -> target_res_m. These are exact
# block-average factors of the IGN ~5 m native fetch (×1, ×2, ×5, ×10) — clean, fast pooling,
# no resampling artifacts. Honest for IGN at every step; on the worldwide source, real detail
# floors at ~30 m (SRTM class), so 5/10 m there only upsample the grid (no new detail).
# Finer is heavier (the geometry indicator scales with cells); resolution still adapts down for
# very large zones (the acquire max_px / zoom caps).
DEM_RES_PRESETS = {
    "5 m (IGN natif, lent)": 5.0,
    "10 m": 10.0,
    "25 m": 25.0,
    "50 m (rapide)": 50.0,
}
DEM_RES_DEFAULT = "25 m"

# MNT source: IGN RGE ALTI (real 1-5 m over France) vs the worldwide ~30 m terrarium, or
# auto (IGN where covered). See ADR-0014.
DEM_SOURCES = {
    "Auto (IGN en France)": "auto",
    "IGN France (fin)": "ign",
    "Monde (terrarium ~30 m)": "world",
}
DEM_SOURCE_DEFAULT = "Auto (IGN en France)"

# Spatial refinement is adaptive (no manual mesh selector):
#  - number of wind sub-zones ~ AOI size / forecast cell (capped) — sampling the forecast's
#    spatial variation; finer than the forecast is pointless (adjacent tiles ~identical wind).
#    We use the Open-Meteo crest wind (~11 km effective), NOT AROME's native 1.3 km (which we
#    don't have and which would mean hundreds of WindNinja runs). Intra-tile detail comes from
#    WindNinja downscaling on the terrain (so few zones + a fine mesh on a fine MNT is right).
#  - WindNinja mass mesh ~ the prepared MNT resolution (a finer mesh than the DEM is moot),
#    floored for compute and per-tile cell count.
FORECAST_CELL_KM = 11.0       # effective horizontal resolution of the Open-Meteo crest wind
MAX_SUBZONES = 4              # cap per side -> at most MAX_SUBZONES^2 WindNinja runs / hour
REFINE_MESH_FLOOR_M = 25.0    # don't mesh finer than this (Pass-1 is screening; keep it fast)
REFINE_MAX_MESH_PX = 600      # cap cells per tile side -> raises the mesh on big single tiles

# The screening masks a border to drop WindNinja crop-edge artifacts. To still cover the WHOLE
# selected zone, the prepared DEM is grown by this buffer on every side, then the view is cropped
# back to the selection (so valid results == the drawn zone). Keep == the mask width below.
EDGE_BUFFER_M = 1500.0

# Prominent green button. The explicit :disabled rule is required because a custom
# stylesheet otherwise overrides Qt's default greyed-out look while running.
GREEN_BTN_QSS = (
    "QPushButton { font-weight:bold; padding:8px 18px; border-radius:5px;"
    " background:#2d7d2d; color:white; }"
    " QPushButton:disabled { background:#a9c7a9; color:#eef; }"
)

# Thicker, more legible sliders (groove + handle). Applied to both the flight-window range
# slider (superqt) and the hour slider; the filled part is green to read at a glance.
SLIDER_QSS = (
    "QSlider::groove:horizontal { height: 10px; background:#d8d8d8; border-radius:5px; }"
    " QSlider::sub-page:horizontal { background:#2d7d2d; border-radius:5px; }"
    " QSlider::add-page:horizontal { background:#d8d8d8; border-radius:5px; }"
    " QSlider::handle:horizontal { background:#2d7d2d; border:2px solid #1c5a1c;"
    " width:18px; height:18px; margin:-6px 0; border-radius:9px; }"
    " QSlider::handle:horizontal:hover { background:#3a9a3a; }"
)

# The Pass-2 feature window is now set by the rectangle drawn on the map; we only floor it
# so an over-small rectangle still yields a usable momentum domain.
PASS2_MIN_HALF_WIDTH_M = 500.0  # >= 1 km square minimum
# Momentum is solved on the drawn zone GROWN by this buffer so the lateral boundary conditions
# sit away from the feature; the 3D rotor is then clipped back to the drawn zone (the downwind
# edge artifact lives in the buffer). Same idea as the Pass-1 edge buffer (ADR-0020/0021).
PASS2_EDGE_BUFFER_M = 700.0

# ADR-0008: Pass-2 mesh resolution is a quality/time knob. Each preset = (mesh_count,
# iterations). Default = "Moyen"; "refine on doubt" by picking a finer preset.
PASS2_MESH_PRESETS: dict[str, tuple[int, int]] = {
    "Grossier — rapide": (20_000, 100),
    "Moyen — défaut": (50_000, 200),
    "Fin — lent": (150_000, 300),
    "Max — très lent": (400_000, 400),
}
PASS2_MESH_DEFAULT = "Moyen — défaut"


def _estimate_minutes(mesh_count: int) -> int:
    """Rough runtime proxy (CPU-bound), calibrated on the Champsaur smoke run
    (~25k cells -> ~2 min). Indicative only — bounds the 'refine' choice (ADR-0008)."""
    return max(1, round(mesh_count / 12_000))


class _TickRuler(QtWidgets.QWidget):
    """Tick marks + labels under a slider, painted at the slider's REAL value positions.

    A plain evenly-spaced label row can't line up with the groove (the handle margin insets the
    usable track). MUST be stacked **directly under its slider in a vertical column** so it
    shares the slider's width and x-origin: then each tick value maps to ``handle/2 + frac *
    (width - handle)`` in this widget's own coordinates — no cross-widget translation, which was
    fragile when the slider sat in a row with other widgets. Works for QSlider and the superqt
    QRangeSlider (same value→pixel model)."""

    def __init__(self, slider, ticks=(), parent=None):
        super().__init__(parent)
        self._slider = slider
        self._ticks = list(ticks)  # (value, text)
        self.setFixedHeight(16)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

    def set_ticks(self, ticks) -> None:
        self._ticks = list(ticks)
        self.update()

    def _value_x(self, value) -> int | None:
        s = self._slider
        lo, hi = s.minimum(), s.maximum()
        if hi <= lo or self.width() <= 0:
            return None
        hl = s.style().pixelMetric(QtWidgets.QStyle.PM_SliderLength, None, s) or 16
        track = max(1, self.width() - hl)  # ruler shares the slider width (stacked column)
        return int(round(hl / 2.0 + (value - lo) / (hi - lo) * track))

    def paintEvent(self, _event) -> None:
        if not self._ticks:
            return
        p = QtGui.QPainter(self)
        f = p.font()
        f.setPointSize(8)
        p.setFont(f)
        fm = p.fontMetrics()
        p.setPen(QtGui.QColor("#888"))
        w = self.width()
        for value, text in self._ticks:
            x = self._value_x(value)
            if x is None:
                continue
            p.drawLine(x, 0, x, 3)
            tw = fm.horizontalAdvance(str(text))
            p.drawText(int(min(max(0, x - tw / 2), w - tw)), 13, str(text))
        p.end()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Sillage — turbulences sous le vent")
        self.cfg = load_config()
        self._dem = None
        # Active prepared DEM path (default Champsaur; replaced when a zone is validated).
        self._dem_path = str(resolve_cache_path(DEFAULT_DEM, self.cfg))
        # Pass-1 wind field from the last mass run, for upstream-crest Pass-2 BC (M3).
        self._pass1_vel_path = None
        self._pass1_ang_path = None
        # Hourly stack: list of (label, hazard, vel_path, ang_path) for the time slider.
        self._hourly: list[tuple] = []
        self._creneau_lo = 0          # window start hour, for spatial refinement
        self._creneau_start = None
        # Last rendered (dem, hazard, title, winds) so toggling the basemap can redraw it.
        self._last_map = None
        # Current hour's hazard + per-zone winds, for the optional 3D view of the créneau tab.
        self._cur_hazard = None
        self._cur_winds = None
        self._c3d_plotter = None
        self._c3d_rendered = False
        # The selected zone in UTM (the prepared DEM is grown by EDGE_BUFFER_M; the view crops
        # back to this so the masked border isn't shown). Set when a zone DEM is ready.
        self._aoi_inner_extent = None
        # AOI selected on the zone tab (south, west, north, east) in lat/lon.
        self.selected_bbox = None
        # Pass-2 analysis window drawn as a rectangle on the créneau map (UTM
        # xmin, ymin, xmax, ymax), plus the transient drawing state. Replaces the
        # old single-click hotspot handoff (the selection is now a rectangle, like Pass-1).
        self._pass2_rect = None
        self._rect_mode = False
        self._rect_patch = None
        self._rect_start = None
        self._job: SolveJob | None = None
        self._cancelling = False

        # Global job progress + cancel live in the status bar (a run can start from any tab).
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setMaximumWidth(220)
        self.progress.setVisible(False)
        self.btn_cancel = QtWidgets.QPushButton("Annuler")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self.on_cancel)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().addPermanentWidget(self.btn_cancel)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_zone_tab(), "Sélection de la zone de vol")
        self.tabs.addTab(self._build_creneau_tab(), "Sélection du créneau de vol")
        self.tabs.addTab(self._build_analyse_tab(), "Analyse locale des zones sous le vent")
        self.setCentralWidget(self.tabs)

        self._run_buttons = [self.btn_validate, self.btn_creneau, self.btn_pass2]
        self.statusBar().showMessage("Prêt")
        # Validate the Météo-France AROME key once the event loop is running (deferred so the
        # window is shown first, and so headless tests — no loop — never block on a modal).
        QtCore.QTimer.singleShot(0, self._check_meteofrance_key)

    def _check_meteofrance_key(self) -> None:
        """Warn (popup) if the AROME API key is configured but invalid/expired/expiring.

        A *missing* key is silent — AROME is optional (Open-Meteo is the default forecast).
        The popup carries the renewal procedure + login (docs/support/meteofrance_arome.md).
        """
        from ..wind.meteofrance import check_arome_key, renewal_text

        status = check_arome_key(self.cfg.meteofrance_api_key)
        if status.reason == "missing":
            return
        if status.ok and status.reason == "ok":
            self.statusBar().showMessage(status.message)
            return
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle("Clé API AROME Météo-France")
        box.setText(status.message)
        box.setInformativeText(renewal_text())
        box.exec()

    def _on_aoi_selected(self, s: float, w: float, n: float, e: float) -> None:
        import math

        self.selected_bbox = (s, w, n, e)
        wkm = (e - w) * 111.0 * math.cos(math.radians((s + n) / 2.0))
        hkm = (n - s) * 111.0
        txt = (f"Zone : S {s:.4f}, O {w:.4f} → N {n:.4f}, E {e:.4f}  "
               f"(~{wkm:.0f} × {hkm:.0f} km). Clique « Valider »…")
        self.zone_info.setText(txt)
        self.statusBar().showMessage(txt)

    def on_validate_zone(self) -> None:
        """Prepare a coarse DEM for the drawn rectangle (worker thread), then go to tab 2."""
        if self._job is not None:
            return
        if self.selected_bbox is None:
            QtWidgets.QMessageBox.information(
                self, "Aucune zone",
                "Dessine d'abord un rectangle sur la carte pour définir la zone de vol.",
            )
            return
        import math

        from ..terrain.acquire import prepare_dem

        s, west, n, e = self.selected_bbox
        target_res = DEM_RES_PRESETS[self.dem_res_combo.currentText()]
        source = DEM_SOURCES[self.dem_source_combo.currentText()]
        # Grow the fetched bbox by the edge-mask buffer so the criblage covers the WHOLE drawn
        # zone after masking (the view is cropped back to the selection in _on_dem_ready).
        dlat = EDGE_BUFFER_M / 111320.0
        dlon = EDGE_BUFFER_M / (111320.0 * max(0.05, math.cos(math.radians((s + n) / 2.0))))
        bbox = (s - dlat, west - dlon, n + dlat, e + dlon)
        out = (self.cfg.cache_dir / "aoi" /
               f"dem_{s:.3f}_{west:.3f}_{n:.3f}_{e:.3f}_{target_res:.0f}m_{source}"
               f"_b{int(EDGE_BUFFER_M)}_utm.tif")

        def fn(on_progress, cancel):  # worker thread — no Qt here
            path, used = prepare_dem(
                bbox, out, target_res_m=target_res, source=source,
                on_progress=on_progress, cancel=cancel,
            )
            return str(path), used

        self._cancelling = False
        self._set_running(True, "Préparation du MNT…")
        job = SolveJob(fn, self)
        job.progress.connect(self._on_job_progress)
        job.finished.connect(self._on_dem_ready)
        job.failed.connect(self._on_job_failed)
        self._job = job
        job.start()

    def _on_dem_ready(self, result) -> None:
        dem_path, used = result if isinstance(result, tuple) else (result, "")
        self._dem_path = dem_path
        self._set_single_map_mode(True)
        self._pass1_vel_path = None
        self._pass1_ang_path = None
        self._clear_pass2_rect()  # the old rectangle was in the previous zone's coordinates
        self._update_pass2_button()
        try:
            self._dem = load_dem(dem_path, max_domain_km=200.0)
            left, bottom, right, top = self._dem.bounds
            b = EDGE_BUFFER_M
            # The DEM was grown by the buffer: crop the view back to the selected zone (if the
            # zone is big enough that a margin remains).
            if (right - left) > 2.5 * b and (top - bottom) > 2.5 * b:
                self._aoi_inner_extent = (left + b, right - b, bottom + b, top - b)
            else:
                self._aoi_inner_extent = None
            self._c3d_rendered = False  # new zone -> let the 3D view reset its camera once
            self._render_terrain(self._dem)  # show the MNT, no hazard overlay yet
        except Exception as exc:
            self._dem = None
            QtWidgets.QMessageBox.warning(self, "MNT", f"MNT préparé, affichage impossible : {exc}")
        self._update_refine_info()
        tag = f" (source {used})" if used else ""
        self._finish_job(f"MNT prêt{tag} — sélectionne le créneau de vol.")
        self.tabs.setCurrentWidget(self._creneau_tab)

    # --- UI construction (controls live in their own tabs) ---------------------
    def _build_zone_tab(self) -> QtWidgets.QWidget:
        """Tab 1 — flight-zone selection: a maximized interactive map, an info line bottom
        left, and a prominent "Valider" button that prepares the DEM, then moves to tab 2."""
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        self.map_tab = MapTab()
        self.map_tab.aoiSelected.connect(self._on_aoi_selected)
        lay.addWidget(self.map_tab, stretch=1)  # the map takes all available space

        bar = QtWidgets.QHBoxLayout()
        self.zone_info = QtWidgets.QLabel(
            "Navigue (glisser / molette), puis dessine un rectangle (outil ▭ en haut à "
            "gauche de la carte) pour définir la zone de vol."
        )
        self.zone_info.setWordWrap(True)
        bar.addWidget(self.zone_info, stretch=1)  # info bottom-left
        bar.addWidget(QtWidgets.QLabel("Source :"))
        self.dem_source_combo = QtWidgets.QComboBox()
        self.dem_source_combo.addItems(list(DEM_SOURCES))
        self.dem_source_combo.setCurrentText(DEM_SOURCE_DEFAULT)
        bar.addWidget(self.dem_source_combo)
        bar.addWidget(QtWidgets.QLabel("Résolution MNT :"))
        self.dem_res_combo = QtWidgets.QComboBox()
        self.dem_res_combo.addItems(list(DEM_RES_PRESETS))
        self.dem_res_combo.setCurrentText(DEM_RES_DEFAULT)
        bar.addWidget(self.dem_res_combo)
        self.btn_validate = QtWidgets.QPushButton("✓  Valider la zone et préparer le terrain")
        self.btn_validate.setStyleSheet(GREEN_BTN_QSS)
        self.btn_validate.clicked.connect(self.on_validate_zone)
        bar.addWidget(self.btn_validate)
        lay.addLayout(bar)
        return w

    def _build_creneau_tab(self) -> QtWidgets.QWidget:
        """Tab 2 — flight-slot selection. A multi-day range slider sets the flight window
        (clock hours, Europe/Paris); "Valider le créneau" runs the per-hour screening. The
        result map is navigated by drag (pan) + scroll (zoom), double-click resets the view,
        and a left-click analyses a hotspot (Pass-2). Wind comes from the window."""
        w = QtWidgets.QWidget()
        self._creneau_tab = w
        lay = QtWidgets.QVBoxLayout(w)

        # Flight window: range slider + "Valider" on ONE compact line.
        win = QtWidgets.QHBoxLayout()
        win.setContentsMargins(0, 0, 0, 0)
        win.addWidget(QtWidgets.QLabel("Créneau :"))
        self.window_slider = QRangeSlider(QtCore.Qt.Horizontal)
        self.window_slider.setStyleSheet(SLIDER_QSS)
        self.window_label = QtWidgets.QLabel("")
        # Cap the slider at the forecast horizon (now + ~48 h). Configure before connecting
        # so the initial setValue doesn't fire _on_window_change before the labels exist.
        max_h = self._forecast_horizon_max()
        self.window_slider.setRange(0, max_h)
        self.window_slider.setValue((min(9, max_h - 1), min(15, max_h)))
        self.window_slider.valueChanged.connect(self._on_window_change)
        # Stack the slider and its tick ruler in a column so the ticks share the slider's exact
        # width + x-origin (the slider sits in a row with a label and the Valider button).
        self._window_ruler = _TickRuler(self.window_slider, self._window_ticks())
        wcol = QtWidgets.QVBoxLayout()
        wcol.setContentsMargins(0, 0, 0, 0)
        wcol.setSpacing(0)
        wcol.addWidget(self.window_slider)
        wcol.addWidget(self._window_ruler)
        win.addLayout(wcol, stretch=1)
        win.addWidget(self.window_label)
        self.btn_creneau = QtWidgets.QPushButton("Valider le créneau ▶")
        self.btn_creneau.setStyleSheet(GREEN_BTN_QSS)
        self.btn_creneau.setToolTip("Criblage temporel : un Pass-1 rapide par heure du créneau.")
        self.btn_creneau.clicked.connect(self.on_run_hourly)
        win.addWidget(self.btn_creneau)
        lay.addLayout(win)
        self.horizon_label = QtWidgets.QLabel("")
        self.horizon_label.setStyleSheet("color: #777; font-size: 11px;")
        lay.addWidget(self.horizon_label)

        opt = QtWidgets.QHBoxLayout()
        self.basemap_combo = QtWidgets.QComboBox()
        self.basemap_combo.addItems([NO_BASEMAP, *map2d.BASEMAP_SOURCES.keys()])
        self.basemap_combo.setCurrentText("IGN plan")
        self.basemap_combo.currentTextChanged.connect(self._on_basemap_change)
        opt.addWidget(QtWidgets.QLabel("Fond :"))
        opt.addWidget(self.basemap_combo)
        self.wind_arrows_check = QtWidgets.QCheckBox("Flèches vent")
        self.wind_arrows_check.setChecked(True)
        self.wind_arrows_check.setToolTip(
            "Affiche une flèche de vent (vitesse + direction) par zone WindNinja, pour l'heure affichée.")
        self.wind_arrows_check.toggled.connect(self._on_wind_arrows_toggle)
        opt.addWidget(self.wind_arrows_check)
        self.view3d_check = QtWidgets.QCheckBox("Vue 3D")
        self.view3d_check.setToolTip(
            "Drape le criblage de l'heure affichée sur le relief 3D (rotation à la souris). La "
            "sélection rectangle Pass-2 reste en vue 2D.")
        self.view3d_check.toggled.connect(self._on_view3d_toggle)
        opt.addWidget(self.view3d_check)
        opt.addStretch(1)
        lay.addLayout(opt)

        # The result map takes the lion's share of the tab and grows on resize (stretch=1 +
        # an expanding canvas with a sane minimum); the control rows stay compact.
        self.fig = Figure(figsize=(6, 5))
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.canvas.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.canvas.setMinimumHeight(360)
        # 2D / 3D stack: the matplotlib screening map (page 0) + a lazy 3D viewport (page 1).
        self._creneau_stack = QtWidgets.QStackedWidget()
        self._creneau_stack.addWidget(self.canvas)
        self._c3d_widget = QtWidgets.QWidget()
        self._c3d_layout = QtWidgets.QVBoxLayout(self._c3d_widget)
        self._c3d_layout.setContentsMargins(0, 0, 0, 0)
        self._c3d_placeholder = QtWidgets.QLabel("La vue 3D s'initialise au premier affichage.")
        self._c3d_placeholder.setAlignment(QtCore.Qt.AlignCenter)
        self._c3d_layout.addWidget(self._c3d_placeholder)
        self._creneau_stack.addWidget(self._c3d_widget)
        lay.addWidget(self._creneau_stack, stretch=1)
        self._pan = None
        self._pan_moved = False
        self._home_extent = None
        self.canvas.mpl_connect("scroll_event", self._on_canvas_scroll)
        self.canvas.mpl_connect("button_press_event", self._on_canvas_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_canvas_motion)
        self.canvas.mpl_connect("button_release_event", self._on_canvas_release)

        slider_row = QtWidgets.QHBoxLayout()
        slider_row.addWidget(QtWidgets.QLabel("Heure affichée :"))
        self.hour_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.hour_slider.setStyleSheet(SLIDER_QSS)
        self.hour_slider.setMinimum(0)
        self.hour_slider.setMaximum(0)
        self.hour_slider.valueChanged.connect(self._on_slider_change)
        self._hour_ruler = _TickRuler(self.hour_slider, [])  # filled once the stack exists
        hcol = QtWidgets.QVBoxLayout()
        hcol.setContentsMargins(0, 0, 0, 0)
        hcol.setSpacing(0)
        hcol.addWidget(self.hour_slider)
        hcol.addWidget(self._hour_ruler)
        slider_row.addLayout(hcol, stretch=1)
        self.hour_label = QtWidgets.QLabel("")
        slider_row.addWidget(self.hour_label)
        self.hour_widget = QtWidgets.QWidget()
        self.hour_widget.setLayout(slider_row)
        self.hour_widget.setVisible(False)
        lay.addWidget(self.hour_widget)

        refine_row = QtWidgets.QHBoxLayout()
        self.btn_refine = QtWidgets.QPushButton("Affiner spatialement l'heure affichée")
        self.btn_refine.setEnabled(False)
        self.btn_refine.clicked.connect(self.on_refine_spatial)
        refine_row.addWidget(self.btn_refine)
        self.refine_info = QtWidgets.QLabel("")  # auto grid (zones x mesh) from the MNT
        self.refine_info.setStyleSheet("color: #555;")
        refine_row.addWidget(self.refine_info)
        refine_row.addStretch(1)
        lay.addLayout(refine_row)

        # Pass-2 (3D detail): select the analysis window as a RECTANGLE on the map (like the
        # Pass-1 AOI), pick the mesh quality, and launch from here. The 3D tab only displays
        # the result; come back here to relaunch with other parameters.
        p2_row = QtWidgets.QHBoxLayout()
        self.btn_rect = QtWidgets.QPushButton("▭ Définir la zone Pass-2")
        self.btn_rect.setCheckable(True)
        self.btn_rect.toggled.connect(self._on_rect_toggle)
        p2_row.addWidget(self.btn_rect)
        self.rect_info = QtWidgets.QLabel("aucune zone")
        self.rect_info.setStyleSheet("color: #555;")
        p2_row.addWidget(self.rect_info)
        p2_row.addSpacing(12)
        p2_row.addWidget(QtWidgets.QLabel("Maillage :"))
        self.mesh_combo = QtWidgets.QComboBox()
        self.mesh_combo.addItems(list(PASS2_MESH_PRESETS))
        self.mesh_hint = QtWidgets.QLabel("")  # created before setCurrentText fires the hint
        self.mesh_hint.setStyleSheet("color: #555;")
        self.mesh_combo.currentTextChanged.connect(self._update_mesh_hint)
        self.mesh_combo.setCurrentText(PASS2_MESH_DEFAULT)
        p2_row.addWidget(self.mesh_combo)
        p2_row.addWidget(self.mesh_hint)
        p2_row.addSpacing(12)
        self.pass2_fine_check = QtWidgets.QCheckBox("MNT fin 5 m (IGN)")
        self.pass2_fine_check.setChecked(True)
        self.pass2_fine_check.setToolTip(
            "Re-télécharge la fenêtre Pass-2 en IGN 5 m natif (repli mondial hors France) avant "
            "le calcul 3D, au lieu de réutiliser le MNT de la zone. Décocher = plus rapide / hors-ligne.")
        p2_row.addWidget(self.pass2_fine_check)
        p2_row.addStretch(1)
        self.btn_pass2 = QtWidgets.QPushButton("▶  Lancer l'analyse Pass-2 (3D)")
        self.btn_pass2.setStyleSheet(GREEN_BTN_QSS)
        self.btn_pass2.setEnabled(False)
        self.btn_pass2.clicked.connect(self.on_launch_pass2)
        p2_row.addWidget(self.btn_pass2)
        lay.addLayout(p2_row)
        self._update_mesh_hint()

        hint = QtWidgets.QLabel(
            "Glisser = déplacer · molette = zoom · double-clic = vue complète · "
            "« ▭ Définir la zone Pass-2 » puis tracer un rectangle pour l'analyse 3D.   "
            + map2d.DISCLAIMER)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #a33; font-style: italic;")
        lay.addWidget(hint)
        self._on_window_change()
        return w

    # --- slider tick labels (value-aligned marks under a slider) -----------------
    def _today_midnight(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Europe/Paris")).replace(
            hour=0, minute=0, second=0, microsecond=0)

    def _window_ticks(self, n: int = 6) -> list[tuple[int, str]]:
        """~n (value, day/hour-label) marks along the flight-window slider (0..horizon)."""
        from datetime import timedelta

        from ..screening.pass1 import _FR_DAYS

        base = self._today_midnight()
        max_h = max(1, self._forecast_horizon_max())
        out = []
        for k in range(n):
            v = round(k / (n - 1) * max_h)
            t = base + timedelta(hours=v)
            out.append((v, f"{_FR_DAYS[t.weekday()]} {t:%Hh}"))
        return out

    def _set_hour_ticks(self) -> None:
        """Refresh the hour-slider ticks from the current hourly stack (≤6 marks)."""
        n = len(self._hourly)
        if n == 0:
            self._hour_ruler.set_ticks([])
            return
        idx = range(n) if n <= 6 else [round(k / 5 * (n - 1)) for k in range(6)]
        self._hour_ruler.set_ticks([(i, self._hourly[i][0].split()[-1]) for i in idx])

    # --- result-map navigation (drag pan + scroll zoom + rectangle selection) ----
    def _on_canvas_scroll(self, event) -> None:
        if event.inaxes is None or event.xdata is None:
            return
        ax = event.inaxes
        factor = 1 / 1.2 if event.button == "up" else 1.2
        x, y = event.xdata, event.ydata
        x0, x1 = ax.get_xlim()
        y0, y1 = ax.get_ylim()
        ax.set_xlim(x - (x - x0) * factor, x + (x1 - x) * factor)
        ax.set_ylim(y - (y - y0) * factor, y + (y1 - y) * factor)
        self.canvas.draw_idle()

    def _on_canvas_press(self, event) -> None:
        if event.inaxes is None or event.button != 1:
            return
        if getattr(event, "dblclick", False):
            self._reset_view()
            return
        if self._rect_mode:  # drawing the Pass-2 analysis rectangle (no pan)
            self._begin_rect(event)
            return
        self._pan = (event.x, event.y, event.inaxes.get_xlim(), event.inaxes.get_ylim())
        self._pan_moved = False

    def _on_canvas_motion(self, event) -> None:
        if self._rect_mode and self._rect_start is not None:
            self._update_rect(event)
            return
        if self._pan is None or event.inaxes is None or event.x is None:
            return
        x0, y0, xlim, ylim = self._pan
        inv = event.inaxes.transData.inverted()
        p0 = inv.transform((x0, y0))
        p1 = inv.transform((event.x, event.y))
        if abs(event.x - x0) + abs(event.y - y0) > 3:
            self._pan_moved = True
        dx, dy = p0[0] - p1[0], p0[1] - p1[1]
        event.inaxes.set_xlim(xlim[0] + dx, xlim[1] + dx)
        event.inaxes.set_ylim(ylim[0] + dy, ylim[1] + dy)
        self.canvas.draw_idle()

    def _on_canvas_release(self, event) -> None:
        if self._rect_mode and self._rect_start is not None:
            self._finish_rect(event)
            return
        self._pan = None

    # --- Pass-2 analysis rectangle (drawn on the créneau map) --------------------
    def _on_rect_toggle(self, checked: bool) -> None:
        self._rect_mode = bool(checked)
        if checked:
            self.statusBar().showMessage(
                "Trace un rectangle sur la carte pour délimiter la zone d'analyse Pass-2…")

    def _begin_rect(self, event) -> None:
        from matplotlib.patches import Rectangle

        if event.xdata is None or event.ydata is None:
            return
        if self._rect_patch is not None:
            try:
                self._rect_patch.remove()
            except Exception:
                pass
        self._rect_start = (float(event.xdata), float(event.ydata))
        self._rect_patch = Rectangle(
            self._rect_start, 0.0, 0.0, fill=False, edgecolor="cyan", linewidth=2,
            linestyle="--", zorder=5)
        event.inaxes.add_patch(self._rect_patch)
        self.canvas.draw_idle()

    def _update_rect(self, event) -> None:
        if self._rect_patch is None or event.xdata is None or event.ydata is None:
            return
        x0, y0 = self._rect_start
        x1, y1 = float(event.xdata), float(event.ydata)
        self._rect_patch.set_bounds(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
        self.canvas.draw_idle()

    def _finish_rect(self, event) -> None:
        x0, y0 = self._rect_start
        self._rect_start = None
        x1 = float(event.xdata) if event.xdata is not None else x0
        y1 = float(event.ydata) if event.ydata is not None else y0
        xmin, xmax = sorted((x0, x1))
        ymin, ymax = sorted((y0, y1))
        # Leave selection mode so the user can pan/zoom to verify, then launch. Clear the
        # flag explicitly (don't rely on the toggled signal — it won't fire if unchanged).
        self._rect_mode = False
        self.btn_rect.setChecked(False)
        if (xmax - xmin) < 50.0 or (ymax - ymin) < 50.0:  # accidental click -> cancel
            self._clear_pass2_rect()
            self.canvas.draw_idle()
            self._update_pass2_button()
            return
        self._pass2_rect = (xmin, ymin, xmax, ymax)
        if self._rect_patch is not None:
            self._rect_patch.set_bounds(xmin, ymin, xmax - xmin, ymax - ymin)
        self.canvas.draw_idle()
        self._update_pass2_button()

    def _clear_pass2_rect(self) -> None:
        self._pass2_rect = None
        self._rect_start = None
        if self._rect_patch is not None:
            try:
                self._rect_patch.remove()
            except Exception:
                pass
            self._rect_patch = None

    def _draw_wind_arrows(self, ax, winds) -> None:
        """Overlay one wind arrow per WindNinja zone for the displayed hour: the arrow points
        where the wind blows TO (direction is meteorological, FROM), coloured by speed with a
        speed label. ``winds`` = list of (x, y, speed_ms, from_deg) in CRS metres."""
        if ax is None or not winds or not self._home_extent:
            return
        if not self.wind_arrows_check.isChecked():
            return
        import matplotlib
        import numpy as np
        from matplotlib import colors

        left, right, bottom, top = self._home_extent
        domain = min(right - left, top - bottom)
        side = max(1, round(len(winds) ** 0.5))
        alen = min(0.16 * domain, 0.42 * domain / side)  # shrink as the zone grid densifies
        norm = colors.Normalize(0.0, 20.0)               # 0..20 m/s -> colour
        cmap = matplotlib.colormaps["turbo"]
        for x, y, spd, drc in winds:
            blow_to = np.deg2rad((float(drc) + 180.0) % 360.0)
            dx, dy = alen * np.sin(blow_to), alen * np.cos(blow_to)
            col = cmap(norm(float(spd)))
            ax.annotate("", xy=(x + dx / 2, y + dy / 2), xytext=(x - dx / 2, y - dy / 2),
                        arrowprops=dict(arrowstyle="-|>", color=col, lw=2.2,
                                        shrinkA=0, shrinkB=0), zorder=6)
            ax.text(x - dx / 2, y - dy / 2, f"{float(spd):.0f} m/s", fontsize=7, color="black",
                    ha="center", va="center", zorder=7,
                    bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.6))

    def _on_wind_arrows_toggle(self, *_args) -> None:
        if self._hourly:
            self._show_hour(int(self.hour_slider.value()))
        elif self._last_map is not None:
            self._render_map(*self._last_map)

    # --- optional 3D view of the créneau screening (2D map stays for selection) -------
    def _on_view3d_toggle(self, checked: bool) -> None:
        if not checked:
            self._creneau_stack.setCurrentWidget(self.canvas)
            return
        if self._dem is None:
            self.view3d_check.setChecked(False)
            self.statusBar().showMessage("Prépare une zone (onglet 1) pour activer la vue 3D.")
            return
        if not self._ensure_creneau_plotter():
            self.view3d_check.setChecked(False)
            return
        self._creneau_stack.setCurrentWidget(self._c3d_widget)
        self._render_creneau_3d()

    @staticmethod
    def _lock_terrain_rotation(plotter) -> None:
        """Constrain 3D rotation to azimuth + elevation (VTK 'terrain' interaction style) so the
        relief stays upright — no roll/tilt. Keeps wheel-zoom and shift-pan."""
        try:
            plotter.enable_terrain_style(mouse_wheel_zooms=True, shift_pans=True)
        except Exception:
            pass

    def _ensure_creneau_plotter(self) -> bool:
        if self._c3d_plotter is not None:
            return True
        try:
            from pyvistaqt import QtInteractor

            plotter = QtInteractor(self._c3d_widget)
            self._lock_terrain_rotation(plotter)
            if self._c3d_placeholder is not None:
                self._c3d_layout.removeWidget(self._c3d_placeholder)
                self._c3d_placeholder.deleteLater()
                self._c3d_placeholder = None
            self._c3d_layout.addWidget(plotter.interactor)
            self._c3d_plotter = plotter
            return True
        except Exception as exc:  # no GL context
            QtWidgets.QMessageBox.critical(
                self, "Échec init 3D", f"Impossible d'initialiser le viewport 3D :\n{exc}")
            return False

    def _render_creneau_3d(self) -> None:
        if self._c3d_plotter is None or self._dem is None:
            return
        try:
            cam = self._c3d_plotter.camera_position if self._c3d_rendered else None
            self._c3d_plotter.clear()
            volume3d.populate_pass1_3d(
                self._c3d_plotter, self._dem, self._cur_hazard, winds=self._cur_winds,
                crs=self._dem.crs, basemap_source=self.basemap_combo.currentText())
            if cam is not None:  # keep the user's viewpoint across hour scrubs / re-renders
                self._c3d_plotter.camera_position = cam
            else:
                self._c3d_plotter.view_isometric()
                self._c3d_rendered = True
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Erreur vue 3D", f"{type(exc).__name__}: {exc}")

    def _draw_pass2_rect(self) -> None:
        """Redraw the stored Pass-2 rectangle after a map re-render (hour scrub, basemap)."""
        if self._pass2_rect is None or not self.fig.axes:
            return
        from matplotlib.patches import Rectangle

        xmin, ymin, xmax, ymax = self._pass2_rect
        self._rect_patch = Rectangle(
            (xmin, ymin), xmax - xmin, ymax - ymin, fill=False, edgecolor="cyan",
            linewidth=2, linestyle="--", zorder=5)
        self.fig.axes[0].add_patch(self._rect_patch)

    def _update_pass2_button(self) -> None:
        has_rect = self._pass2_rect is not None
        self.btn_pass2.setEnabled(self._job is None and has_rect)
        if not has_rect:
            self.rect_info.setText("aucune zone")
        else:
            xmin, ymin, xmax, ymax = self._pass2_rect
            self.rect_info.setText(
                f"zone {(xmax - xmin) / 1000:.1f} × {(ymax - ymin) / 1000:.1f} km")

    def _reset_view(self) -> None:
        if self._home_extent and self.fig.axes:
            left, right, bottom, top = self._home_extent
            ax = self.fig.axes[0]
            ax.set_xlim(left, right)
            ax.set_ylim(bottom, top)
            self.canvas.draw_idle()

    # --- flight window (drives the per-hour wind, replacing manual fields) ------
    def _forecast_horizon_max(self) -> int:
        """Slider max = current hour + the forecast horizon (clock hours from today 00:00)."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Europe/Paris")).hour + FORECAST_HORIZON_H

    def _window_hours(self) -> tuple[int, int]:
        lo, hi = self.window_slider.value()
        lo, hi = int(lo), int(hi)
        return lo, max(hi, lo + 1)

    def _window_start(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        # Slider = clock hours of the flight day (today, Europe/Paris): 0..24 -> 00h..24h.
        midnight = datetime.now(ZoneInfo("Europe/Paris")).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return midnight + timedelta(hours=self._window_hours()[0])

    def _window_series(self):
        lo, hi = self._window_hours()
        return synthetic_series(hi - lo, start=self._window_start())

    def _representative_wind(self) -> tuple[float, float]:
        """(speed, from_deg) for single-shot Pass-1 actions: the window's first hour."""
        _label, spd, drc = self._window_series()[0]
        return spd, drc

    def _on_window_change(self, *_args) -> None:
        from datetime import timedelta

        from ..screening.pass1 import _FR_DAYS

        start = self._window_start()
        lo, hi = self._window_hours()
        end = start + timedelta(hours=hi - lo)

        def _fmt(t):
            return f"{_FR_DAYS[t.weekday()]} {t:%d/%m} {t:%Hh}"

        self.window_label.setText(f"{_fmt(start)} → {_fmt(end)}  ({hi - lo} h)")
        limit = (start - timedelta(hours=lo)) + timedelta(hours=self._forecast_horizon_max())
        self.horizon_label.setText(
            f"Prévision disponible jusqu'à ~ {_fmt(limit)}  (AROME ~{FORECAST_HORIZON_H} h)")

    def _render_terrain(self, dem) -> None:
        """Show the MNT hillshade over the selected basemap (orientation context)."""
        self._last_map = None
        source = self.basemap_combo.currentText()
        left, bottom, right, top = dem.bounds
        extent = (left, right, bottom, top)
        self.fig.clear()
        ax = self.fig.add_subplot(111)
        if source != NO_BASEMAP:
            ax.imshow(map2d.hillshade(dem), cmap="gray", extent=extent, origin="upper",
                      alpha=0.55, zorder=2)
            ax.set_xlim(left, right)
            ax.set_ylim(bottom, top)
            try:
                map2d.add_basemap(ax, dem.crs, source=source, attribution=False, zorder=0)
            except Exception:
                ax.clear()
                map2d.draw_hillshade(ax, dem)
            ax.set_xlabel("Est (m)")
            ax.set_ylabel("Nord (m)")
        else:
            map2d.draw_hillshade(ax, dem)
        ax.set_title("MNT — zone de vol  (choisis un créneau, puis lance le criblage)")
        view = self._aoi_inner_extent or extent  # crop the masked buffer out of the view
        ax.set_xlim(view[0], view[1])
        ax.set_ylim(view[2], view[3])
        self._home_extent = view
        self._cur_hazard, self._cur_winds = None, None  # bare terrain (no criblage yet)
        self._draw_pass2_rect()
        self.canvas.draw()
        if getattr(self, "view3d_check", None) is not None and self.view3d_check.isChecked():
            self._render_creneau_3d()

    def _build_analyse_tab(self) -> QtWidgets.QWidget:
        """Tab 3 — DISPLAY ONLY: the 3D recirculation result of the last Pass-2 run. The
        selection (rectangle) and parameters (mesh) live on the créneau tab; come back here
        only to view the 3D. Relaunch with other parameters from the créneau tab."""
        w = QtWidgets.QWidget()
        self._analyse_tab = w
        lay = QtWidgets.QVBoxLayout(w)

        # The VTK/OpenGL viewport is created lazily (on first analysis) so the window starts
        # cleanly even without a GL context (headless).
        self._p2_widget = QtWidgets.QWidget()
        self._p2_layout = QtWidgets.QVBoxLayout(self._p2_widget)
        self.plotter = None
        self._p2_placeholder = QtWidgets.QLabel(
            "Le viewport 3D s'initialise à la première analyse.\n"
            "Trace la zone Pass-2 et lance l'analyse depuis l'onglet « créneau »."
        )
        self._p2_placeholder.setAlignment(QtCore.Qt.AlignCenter)
        self._p2_layout.addWidget(self._p2_placeholder)
        lay.addWidget(self._p2_widget)
        return w

    def _ensure_plotter(self) -> bool:
        """Create the embedded QtInteractor on demand. Returns True if the 3D view is ready."""
        if self.plotter is not None:
            return True
        try:
            from pyvistaqt import QtInteractor

            plotter = QtInteractor(self._p2_widget)
            self._lock_terrain_rotation(plotter)
            if self._p2_placeholder is not None:
                self._p2_layout.removeWidget(self._p2_placeholder)
                self._p2_placeholder.deleteLater()
                self._p2_placeholder = None
            self._p2_layout.addWidget(plotter.interactor)
            self.plotter = plotter
            return True
        except Exception as exc:  # no GL context available
            QtWidgets.QMessageBox.critical(
                self, "Échec init 3D",
                f"Impossible d'initialiser le viewport 3D (OpenGL) :\n{exc}",
            )
            return False

    # --- Pass-2 mesh quality/time knob (ADR-0008) --------------------------------
    def _selected_mesh(self) -> tuple[int, int, str]:
        name = self.mesh_combo.currentText()
        mesh_count, iterations = PASS2_MESH_PRESETS[name]
        return mesh_count, iterations, name

    def _update_mesh_hint(self, *_args) -> None:
        mesh_count, iters, _name = self._selected_mesh()
        self.mesh_hint.setText(
            f"~{mesh_count:,} mailles, {iters} itér. - "
            f"~{_estimate_minutes(mesh_count)} min (approx.)"
        )

    # --- map rendering (shared by all Pass-1 views) ----------------------------
    def _render_map(self, dem, hazard, title: str, winds=None) -> None:
        """Draw a Pass-1 hazard map on the embedded canvas, with an optional web basemap.

        ``winds`` (list of (x, y, speed_ms, from_deg) in CRS metres) overlays one wind arrow
        per WindNinja zone for the displayed hour."""
        self._last_map = (dem, hazard, title, winds)
        self._cur_hazard, self._cur_winds = hazard, winds
        source = self.basemap_combo.currentText()
        left, bottom, right, top = dem.bounds
        extent = (left, right, bottom, top)
        self.fig.clear()
        ax = self.fig.add_subplot(111)
        if source != NO_BASEMAP:
            from matplotlib import colors

            im = ax.imshow(hazard, cmap="inferno", extent=extent, origin="upper",
                           alpha=0.5, norm=colors.Normalize(0, 1), zorder=2)
            ax.set_xlim(left, right)
            ax.set_ylim(bottom, top)
            try:
                map2d.add_basemap(ax, dem.crs, source=source, attribution=False, zorder=0)
            except Exception as exc:  # offline / tiles unavailable -> hillshade fallback
                ax.clear()
                im = map2d.draw_indicator(ax, dem, hazard)
                self.statusBar().showMessage(f"Fond de carte indisponible ({exc}) ; ombrage")
            ax.set_xlabel("Est (m)")
            ax.set_ylabel("Nord (m)")
        else:
            im = map2d.draw_indicator(ax, dem, hazard)
        self.fig.colorbar(im, ax=ax, label="indicateur de danger sous le vent (0–1)")
        ax.set_title(f"{title}\n{map2d.DISCLAIMER}")
        view = self._aoi_inner_extent or extent  # crop the masked buffer out of the view
        ax.set_xlim(view[0], view[1])
        ax.set_ylim(view[2], view[3])
        self._home_extent = view
        self._draw_wind_arrows(ax, winds)
        self._draw_pass2_rect()
        self.canvas.draw()
        if getattr(self, "view3d_check", None) is not None and self.view3d_check.isChecked():
            self._render_creneau_3d()

    def _on_basemap_change(self, *_args) -> None:
        if self._last_map is not None:
            self._render_map(*self._last_map)

    # --- actions ---------------------------------------------------------------
    def on_compute_pass1(self) -> None:
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            dem = load_dem(self._dem_path, max_domain_km=self.cfg.max_domain_km)
            self._dem = dem
            self._set_single_map_mode(True)
            self._pass1_vel_path = None  # geometry-only has no Pass-1 wind field
            self._pass1_ang_path = None
            _spd, drc = self._representative_wind()
            hazard = ind.hazard_indicator(dem, drc)
            self._render_map(dem, hazard, f"Pass-1 géométrie — vent de {drc:.0f}°")
            self.statusBar().showMessage(
                f"Pass-1 géométrie sur grille {dem.shape}, rés. {dem.resolution_m:.0f} m"
            )
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Erreur Pass-1", str(exc))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def on_run_mass(self) -> None:
        """Run the real WindNinja mass solver on a worker thread (progress + cancel)."""
        if self._job is not None:
            return
        cfg = self.cfg
        dem_path = self._dem_path
        dem_tag = Path(dem_path).stem
        wind_spd, wind_dir = self._representative_wind()
        resolution_m = 100.0
        cli = cfg.windninja_cli
        max_km = cfg.max_domain_km
        work = cfg.cache_dir / "aoi" / "ihm_mass" / dem_tag / (
            f"{wind_dir:.0f}_{wind_spd:.0f}_{resolution_m:.0f}m"
        )

        def fn(on_progress, cancel):  # runs on the worker thread — no Qt here
            dem = load_dem(dem_path, max_domain_km=max_km)
            hazard, vel_path = hourly_indicator(
                dem=dem, cli=cli, dem_path=dem_path, work_dir=work,
                wind_speed_ms=wind_spd, wind_from_deg=wind_dir, resolution_m=resolution_m,
                force_run=True, on_progress=on_progress, cancel=cancel,
            )
            ang_path = find_direction_grid(work)
            return dem, hazard, wind_dir, vel_path, ang_path

        self._cancelling = False
        self._set_running(True, "WindNinja masse en cours…")
        job = SolveJob(fn, self)
        job.progress.connect(self._on_job_progress)
        job.finished.connect(self._on_mass_finished)
        job.failed.connect(self._on_job_failed)
        self._job = job
        job.start()

    def on_cancel(self) -> None:
        if self._job is not None:
            self._cancelling = True
            self.statusBar().showMessage("Annulation…")
            self._job.cancel()

    def _set_running(self, running: bool, msg: str = "") -> None:
        for b in self._run_buttons:
            b.setEnabled(not running)
        self.btn_refine.setEnabled(not running and bool(self._hourly))
        self.btn_rect.setEnabled(not running)
        if not running:
            self._update_pass2_button()  # re-enable Pass-2 only if a rectangle is set
        self.progress.setVisible(running)
        self.btn_cancel.setVisible(running)
        if running:
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            if msg:
                self.statusBar().showMessage(msg)

    def _finish_job(self, status: str) -> None:
        self._job = None
        self._cancelling = False
        self._set_running(False)
        self.statusBar().showMessage(status)

    def _on_job_progress(self, pct: int, msg: str) -> None:
        # >= 99% but not done: the long WindNinja post-solve (mass-mesh sampling, output
        # writing). Switch the bar to "busy" so it pulses instead of looking frozen.
        if pct >= 99:
            if self.progress.maximum() != 0:
                self.progress.setRange(0, 0)
        else:
            if self.progress.maximum() == 0:
                self.progress.setRange(0, 100)
            self.progress.setValue(pct)
        self.statusBar().showMessage(msg)

    def _on_mass_finished(self, result) -> None:
        dem, hazard, wind_dir, vel_path, ang_path = result
        self._dem = dem
        self._pass1_vel_path = vel_path
        self._pass1_ang_path = ang_path
        self._render_map(dem, hazard, f"Pass-1 WindNinja masse — vent de {wind_dir:.0f}°")
        self._set_single_map_mode(True)
        self._finish_job("Pass-1 masse terminé")

    # --- hourly Pass-1 + time slider -------------------------------------------
    def _set_single_map_mode(self, single: bool) -> None:
        """Single-map mode hides the hour slider; hourly mode shows it."""
        self.hour_widget.setVisible(not single)
        if single:
            self._hourly = []

    def on_run_hourly(self) -> None:
        """TEMPORAL criblage: a fast single-domain Pass-1 per hour over the flight window,
        using the forecast wind at the domain centre (synthetic fallback). Spatial detail is
        added per hour on demand with "Affiner spatialement"."""
        if self._job is not None:
            return
        from datetime import timedelta

        from ..screening.pass1 import _FR_DAYS

        cfg = self.cfg
        dem_path = self._dem_path
        dem_tag = Path(dem_path).stem
        cli = cfg.windninja_cli
        max_km = cfg.max_domain_km
        cache_dir = cfg.cache_dir
        lo, hi = self._window_hours()
        count = hi - lo
        window_start = self._window_start()
        self._creneau_lo = lo            # remembered for spatial refinement of a shown hour
        self._creneau_start = window_start
        syn = synthetic_series(count, start=window_start)

        def _lab(t):
            return f"{_FR_DAYS[t.weekday()]} {t:%d/%m} {t:%Hh}"

        labels = [_lab(window_start + timedelta(hours=i)) for i in range(count)]
        day_tag = window_start.strftime("%Y%m%d")

        def fn(on_progress, cancel):  # worker thread — no Qt here
            import numpy as np

            from ..wind.profile import window_forecast_provider

            dem = load_dem(dem_path, max_domain_km=max_km)
            left, bottom, right, top = dem.bounds
            cx, cy = (left + right) / 2.0, (bottom + top) / 2.0
            crest = float(np.nanpercentile(dem.elevation, 80))
            make = window_forecast_provider(dem, crest, n_hours=hi, source="open_meteo")
            try:
                make(lo)(cx, cy)  # probe: real crest forecast available?
                src = "prévision"
            except Exception:
                make = None
                src = "synthétique"

            out = []
            for i in range(count):
                if cancel():
                    raise RuntimeError("cancelled")
                h = lo + i
                if make is not None:
                    spd, drc = make(h)(cx, cy)  # one domain-average wind per hour
                else:
                    _l, spd, drc = syn[i]
                work = cache_dir / "aoi" / "creneau" / dem_tag / f"{day_tag}_h{h:02d}_t"

                def hp(pct, msg, i=i):
                    on_progress(int((i + pct / 100.0) / count * 100),
                                f"{src} {i + 1}/{count} : {msg}")

                hazard, vel = hourly_indicator(
                    dem=dem, cli=cli, dem_path=dem_path, work_dir=work,
                    wind_speed_ms=spd, wind_from_deg=drc, resolution_m=200.0,
                    force_run=False, on_progress=hp, cancel=cancel,
                )
                out.append((labels[i], hazard, vel, find_direction_grid(work),
                            [(cx, cy, float(spd), float(drc))]))  # one domain wind / hour
            return dem, src, out

        self._cancelling = False
        self._set_running(True, f"Criblage temporel ({count} h)…")
        job = SolveJob(fn, self)
        job.progress.connect(self._on_job_progress)
        job.finished.connect(self._on_hourly_finished)
        job.failed.connect(self._on_job_failed)
        self._job = job
        job.start()

    def _on_hourly_finished(self, result) -> None:
        dem, src, stack = result
        self._dem = dem
        self._update_refine_info()
        self._hourly = stack
        self.hour_widget.setVisible(True)
        self.hour_slider.blockSignals(True)
        self.hour_slider.setMaximum(max(0, len(stack) - 1))
        self.hour_slider.setValue(0)
        self.hour_slider.blockSignals(False)
        self._set_hour_ticks()
        self._show_hour(0)
        self._finish_job(f"Criblage temporel ({src}) : {len(stack)} heures")

    def _refine_grid(self) -> tuple[int, int, int]:
        """(nx, ny, mesh_m) for the spatial refine. Wind sub-zones ~ AOI / forecast cell
        (capped), WindNinja mesh ~ the MNT resolution (floored and capped per tile)."""
        ex, ey = self._dem.extent_km
        nx = max(1, min(MAX_SUBZONES, round(ex / FORECAST_CELL_KM)))
        ny = max(1, min(MAX_SUBZONES, round(ey / FORECAST_CELL_KM)))
        tile_km = max(ex / nx, ey / ny)
        mesh = max(REFINE_MESH_FLOOR_M, self._dem.resolution_m,
                   tile_km * 1000.0 / REFINE_MAX_MESH_PX)
        return nx, ny, int(round(mesh))

    def _update_refine_info(self) -> None:
        if self._dem is None:
            self.refine_info.setText("")
            return
        nx, ny, mesh = self._refine_grid()
        self.refine_info.setText(f"→ {nx}×{ny} zones de vent · maille WindNinja ~{mesh} m")

    def on_refine_spatial(self) -> None:
        """Refine the currently displayed hour with the SPATIAL sub-zone criblage and store
        it back in the hourly stack (re-shown when scrubbing back to that hour). The sub-zone
        count and the WindNinja mesh are derived from the AOI size and the MNT resolution."""
        if self._job is not None or not self._hourly:
            return
        i = int(self.hour_slider.value())
        if not (0 <= i < len(self._hourly)):
            return
        cfg = self.cfg
        dem_path = self._dem_path
        dem_tag = Path(dem_path).stem
        cli = cfg.windninja_cli
        max_km = cfg.max_domain_km
        cache_dir = cfg.cache_dir
        lo = getattr(self, "_creneau_lo", 0)
        start = getattr(self, "_creneau_start", None) or self._window_start()
        count = len(self._hourly)
        n_hours = lo + count
        h = lo + i
        day_tag = start.strftime("%Y%m%d")
        label = self._hourly[i][0].replace("  (spatial)", "")
        nx, ny, refine_res = self._refine_grid()
        _ls, s_spd, s_drc = synthetic_series(count, start=start)[i]

        def fn(on_progress, cancel):  # worker thread — no Qt here
            import numpy as np

            from ..screening import indicator as ind2
            from ..screening.pass1 import mask_edge_buffer
            from ..screening.subzones import subzone_bboxes, subzone_speed_field
            from ..wind.profile import window_forecast_provider

            dem = load_dem(dem_path, max_domain_km=max_km)
            left, bottom, right, top = dem.bounds
            cx, cy = (left + right) / 2.0, (bottom + top) / 2.0
            crest = float(np.nanpercentile(dem.elevation, 80))
            make = window_forecast_provider(dem, crest, n_hours=n_hours, source="open_meteo")
            try:
                provider = make(h)
                geo_dir = provider(cx, cy)[1]
            except Exception:
                provider = lambda x, y: (s_spd, s_drc)
                geo_dir = s_drc
            field = subzone_speed_field(
                dem=dem, cli=cli, wind_at_center=provider, nx=nx, ny=ny,
                work_root=(cache_dir / "aoi" / "creneau" /
                           dem_tag / f"{day_tag}_h{h:02d}_s{nx}x{ny}_{refine_res}"),
                resolution_m=float(refine_res), on_progress=on_progress, cancel=cancel,
            )
            winds = []  # one input wind per sub-zone (for the on-map arrows)
            for _bb, (tcx, tcy) in subzone_bboxes(dem, nx, ny):
                try:
                    ws, wd = provider(tcx, tcy)
                except Exception:
                    ws, wd = s_spd, s_drc
                winds.append((tcx, tcy, float(ws), float(wd)))
            hazard = ind2.hazard_indicator(dem, geo_dir, speed_grid=field)
            hazard = mask_edge_buffer(hazard, dem.resolution_m, 1500.0)
            return i, label, hazard, winds

        self._cancelling = False
        self._set_running(True, f"Affinage spatial {nx}×{ny} (maille {refine_res} m) — {label}…")
        job = SolveJob(fn, self)
        job.progress.connect(self._on_job_progress)
        job.finished.connect(self._on_refine_finished)
        job.failed.connect(self._on_job_failed)
        self._job = job
        job.start()

    def _on_refine_finished(self, result) -> None:
        i, label, hazard, winds = result
        if 0 <= i < len(self._hourly):
            self._hourly[i] = (f"{label}  (spatial)", hazard, None, None, winds)
        self._finish_job(f"Heure affinée spatialement — {label}")
        if int(self.hour_slider.value()) == i:
            self._show_hour(i)

    def _on_slider_change(self, val: int) -> None:
        if self._hourly:
            self._show_hour(int(val))

    def _show_hour(self, i: int) -> None:
        if not (0 <= i < len(self._hourly)):
            return
        label, hazard, vel, ang, winds = self._hourly[i]
        self._pass1_vel_path = vel
        self._pass1_ang_path = ang
        self._render_map(self._dem, hazard, f"Pass-1 horaire — {label}", winds=winds)
        self.hour_label.setText(label)

    # --- sub-zone spatial Pass-1 (ADR-0007) ------------------------------------
    def on_run_subzones(self) -> None:
        """Run a 2x2 sub-zone Pass-1 with a synthetic *spatial* wind on the worker.

        Demonstrates ADR-0007 offline: each tile gets its own wind (direction sweeps W->E),
        mosaicked into one spatially-varying screening map. Swap the synthetic provider for
        wind.profile.crest_wind_provider(source="arome") for real AROME winds.
        """
        if self._job is not None:
            return
        from ..screening.pass1 import mask_edge_buffer
        from ..screening.subzones import subzone_speed_field

        cfg = self.cfg
        dem_path = self._dem_path
        dem_tag = Path(dem_path).stem
        cli = cfg.windninja_cli
        max_km = cfg.max_domain_km
        base_spd, rep_dir = self._representative_wind()
        work_root = cfg.cache_dir / "aoi" / "ihm_subzones" / dem_tag

        def fn(on_progress, cancel):  # worker thread — no Qt here
            dem = load_dem(dem_path, max_domain_km=max_km)
            left, _b, right, _t = dem.bounds

            def provider(x, y):  # synthetic spatial wind: direction sweeps W->E
                frac = (x - left) / (right - left)
                return base_spd, (rep_dir - 20.0 + 40.0 * frac) % 360.0

            field = subzone_speed_field(
                dem=dem, cli=cli, wind_at_center=provider, nx=2, ny=2,
                work_root=work_root, resolution_m=150.0,
                on_progress=on_progress, cancel=cancel,
            )
            hazard = ind.hazard_indicator(dem, rep_dir, speed_grid=field)
            hazard = mask_edge_buffer(hazard, dem.resolution_m, 1500.0)
            return dem, hazard, rep_dir

        self._cancelling = False
        self._set_running(True, "Pass-1 sous-zones (spatial)…")
        job = SolveJob(fn, self)
        job.progress.connect(self._on_job_progress)
        job.finished.connect(self._on_subzones_finished)
        job.failed.connect(self._on_job_failed)
        self._job = job
        job.start()

    def _on_subzones_finished(self, result) -> None:
        dem, hazard, rep_dir = result
        self._dem = dem
        self._set_single_map_mode(True)
        # The mosaic has no single vel/ang grid, so the click handoff falls back to controls.
        self._pass1_vel_path = None
        self._pass1_ang_path = None
        self._render_map(
            dem, hazard, f"Pass-1 sous-zones (vent spatial) — base de {rep_dir:.0f}°"
        )
        self._finish_job("Pass-1 sous-zones terminé")

    def _on_job_failed(self, msg: str) -> None:
        if self._cancelling:
            self._finish_job("Calcul annulé")
        else:
            self._finish_job("Échec du calcul")
            QtWidgets.QMessageBox.critical(self, "Erreur WindNinja", msg)

    # --- Pass-2 handoff: draw a rectangle -> momentum solve -> 3D ----------------
    def on_launch_pass2(self) -> None:
        """Launch a Pass-2 momentum solve on the rectangle drawn on the créneau map. The
        rectangle (centre + half-extent) sets the cropped feature window; the mesh combo on
        this tab sets the quality/time. The 3D tab only displays the result."""
        if self._job is not None:
            return
        if self._dem is None:
            QtWidgets.QMessageBox.information(
                self, "Pass-2",
                "Prépare une zone et lance un criblage, puis trace la zone Pass-2.")
            return
        if self._pass2_rect is None:
            QtWidgets.QMessageBox.information(
                self, "Zone Pass-2",
                "Trace d'abord un rectangle (« ▭ Définir la zone Pass-2 ») sur la carte.")
            return

        xmin, ymin, xmax, ymax = self._pass2_rect
        cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
        half_m = max(max(xmax - xmin, ymax - ymin) / 2.0, PASS2_MIN_HALF_WIDTH_M)
        mesh_count, _iters, mesh_name = self._selected_mesh()
        bc_spd, bc_dir, wind_src = self._pass2_wind_at(cx, cy)
        resp = QtWidgets.QMessageBox.question(
            self, "Lancer Pass-2",
            f"Lancer un calcul momentum sur la zone tracée (~{half_m * 2 / 1000:.1f} km) ?\n\n"
            f"Centre ({cx:.0f}, {cy:.0f}), maillage « {mesh_name} » "
            f"({mesh_count:,} mailles, ~{_estimate_minutes(mesh_count)} min).\n"
            f"Vent ({wind_src}) : {bc_spd:.0f} m/s de {bc_dir:.0f}°.",
        )
        if resp != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._launch_pass2_at(cx, cy, half_m, bc_spd, bc_dir, wind_src)

    def _pass2_wind_at(self, x: float, y: float) -> tuple[float, float, str]:
        """Pass-2 BC wind: the Pass-1 field sampled just upstream of (x, y) if a mass run is
        available, else the wind of the selected flight window (M3 refinement)."""
        rep_spd, rep_dir = self._representative_wind()
        if self._pass1_vel_path and self._pass1_ang_path:
            bc = upstream_crest_wind(
                self._pass1_vel_path, self._pass1_ang_path, x, y, rep_dir
            )
            if bc is not None:
                return bc[0], bc[1], "Pass-1 amont"
        return rep_spd, rep_dir, "créneau"

    def _launch_pass2_at(self, x: float, y: float, half_m: float, bc_spd: float,
                         bc_dir: float, wind_src: str) -> None:
        cfg = self.cfg
        dem = self._dem
        cli = cfg.windninja_cli
        mesh_count, iterations, _name = self._selected_mesh()
        pass2_dir = cfg.cache_dir / "aoi" / "pass2" / Path(self._dem_path).stem
        fine = self.pass2_fine_check.isChecked()  # re-fetch the window at IGN 5 m?
        # Solve on the drawn zone GROWN by the buffer (boundaries away from the feature); the 3D
        # rotor is later clipped back to the drawn zone (aoi_bounds).
        comp_half = half_m + PASS2_EDGE_BUFFER_M
        aoi_bounds = (x - half_m, x + half_m, y - half_m, y + half_m)

        def fn(on_progress, cancel):  # worker thread — no Qt here
            import rasterio

            if fine:
                # Re-fetch JUST the (buffered) Pass-2 window at IGN 5 m native (terrarium fallback
                # outside France) so the 3D terrain is at full detail regardless of the zone's MNT.
                from ..terrain.acquire import bbox_latlon_from_utm_window, prepare_dem

                bbox_ll = bbox_latlon_from_utm_window(dem.crs, x, y, comp_half)
                out = pass2_dir / f"ihm_fine_{x:.0f}_{y:.0f}_{2 * comp_half:.0f}m_5m.tif"

                def fetch_prog(p, m):
                    on_progress(int(p * 0.25), f"MNT fin : {m}")

                crop_path, _used = prepare_dem(
                    bbox_ll, out, target_res_m=5.0, source="auto",
                    on_progress=fetch_prog, cancel=cancel)
                crop_path = str(crop_path)

                def solve_prog(p, m):
                    on_progress(int(25 + p * 0.75), m)
            else:
                crop = crop_dem(dem, x, y, comp_half)
                crop_path = str(pass2_dir / f"ihm_crop_{x:.0f}_{y:.0f}_{2 * comp_half:.0f}m.tif")
                write_dem(crop, crop_path)
                solve_prog = on_progress

            with rasterio.open(crop_path) as ds:  # the crop's own CRS, for the 3D drape
                crop_crs = ds.crs

            run = run_momentum(
                cli=cli, dem_path=crop_path, working_dir=str(pass2_dir / "ihm_run"),
                wind_speed_ms=bc_spd, wind_from_deg=bc_dir,
                mesh_count=mesh_count, iterations=iterations,
                on_progress=solve_prog, cancel=cancel,
            )
            if run.returncode not in (0, None):
                raise RuntimeError(format_run_failure(run, "WindNinja momentum"))
            if run.openfoam_case_dir is None:
                raise RuntimeError("momentum terminé mais aucun case OpenFOAM localisé")
            return str(run.openfoam_case_dir), bc_spd, bc_dir, (x, y), crop_crs, aoi_bounds

        self._cancelling = False
        detail = "MNT fin 5 m" if fine else "MNT zone"
        self._set_running(True, f"Pass-2 ({wind_src}, {detail}) en ({x:.0f}, {y:.0f})…")
        job = SolveJob(fn, self)
        job.progress.connect(self._on_job_progress)
        job.finished.connect(self._on_pass2_finished)
        job.failed.connect(self._on_job_failed)
        self._job = job
        job.start()

    def _on_pass2_finished(self, result) -> None:
        case_dir, wind_spd, wind_dir, xy, crop_crs, aoi_bounds = result
        if not self._ensure_plotter():
            self._finish_job("Viewport 3D indisponible")
            return
        mfd = volume3d.mean_flow_vector(wind_dir)
        self.plotter.clear()
        crs = crop_crs or (self._dem.crs if self._dem is not None else None)
        volume3d.populate_plotter(self.plotter, case_dir, mfd, show_turbulence=False,
                                  crs=crs, basemap_source=self.basemap_combo.currentText(),
                                  wind_speed_ms=wind_spd, wind_from_deg=wind_dir,
                                  aoi_bounds=aoi_bounds)
        self.plotter.reset_camera()
        self.tabs.setCurrentWidget(self._analyse_tab)
        self._finish_job(f"Rotor Pass-2 en ({xy[0]:.0f}, {xy[1]:.0f})")
