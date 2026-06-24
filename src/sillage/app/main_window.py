"""Sillage main window (ADR-0009).

Controls panel + two tabs — Pass-1 screening (2D, embedded matplotlib) and Pass-2 detail
(3D, embedded pyvistaqt). It reuses the headless rendering (viz.map2d, viz.volume3d) and runs
long WindNinja/OpenFOAM solves on a worker thread (jobs.SolveJob). Pass-1 (triage) and Pass-2
(detail) are kept as distinct tabs on purpose (ADR-0005). User-facing strings are in French.
"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtWidgets
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from superqt import QRangeSlider

from ..config import load_config, resolve_cache_path
from ..flow.windninja import locate_openfoam_case, run_momentum
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

# MNT (DEM) resolution presets for "Valider la zone" -> target_res_m (terrarium zoom).
# Finer is heavier (the geometry indicator scales with cells); resolution still adapts down
# for very large zones (the acquire max_px cap). Labels are approximate for typical zones.
DEM_RES_PRESETS = {
    "Grossier (~110 m)": 90.0,
    "Moyen (~55 m)": 50.0,
    "Fin (~27 m)": 30.0,
    "Très fin (~14 m)": 15.0,
}
DEM_RES_DEFAULT = "Moyen (~55 m)"

# MNT source: IGN RGE ALTI (real 1-5 m over France) vs the worldwide ~30 m terrarium, or
# auto (IGN where covered). See ADR-0014.
DEM_SOURCES = {
    "Auto (IGN en France)": "auto",
    "IGN France (fin)": "ign",
    "Monde (terrarium ~30 m)": "world",
}
DEM_SOURCE_DEFAULT = "Auto (IGN en France)"

# Spatial-refinement mesh resolution (WindNinja mass mesh of the sub-zone tiles). Finer = more
# local detail but slower; bounded by the prepared DEM. -> resolution_m for subzone_speed_field.
REFINE_RES_PRESETS = {
    "Standard (150 m)": 150.0,
    "Fin (75 m)": 75.0,
    "Très fin (40 m)": 40.0,
    "Maximum (25 m)": 25.0,
}
REFINE_RES_DEFAULT = "Standard (150 m)"

# Prominent green button. The explicit :disabled rule is required because a custom
# stylesheet otherwise overrides Qt's default greyed-out look while running.
GREEN_BTN_QSS = (
    "QPushButton { font-weight:bold; padding:8px 18px; border-radius:5px;"
    " background:#2d7d2d; color:white; }"
    " QPushButton:disabled { background:#a9c7a9; color:#eef; }"
)

PASS2_HALF_WIDTH_M = 2500.0  # ~5 km feature window around the clicked hotspot

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
        # Last rendered (dem, hazard, title) so toggling the basemap can redraw it.
        self._last_map = None
        # AOI selected on the zone tab (south, west, north, east) in lat/lon.
        self.selected_bbox = None
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

        self._run_buttons = [self.btn_validate, self.btn_creneau, self.btn_load_p2]
        self.statusBar().showMessage("Prêt")

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
        from ..terrain.acquire import prepare_dem

        bbox = self.selected_bbox
        s, west, n, e = bbox
        target_res = DEM_RES_PRESETS[self.dem_res_combo.currentText()]
        source = DEM_SOURCES[self.dem_source_combo.currentText()]
        out = (self.cfg.cache_dir / "aoi" /
               f"dem_{s:.3f}_{west:.3f}_{n:.3f}_{e:.3f}_{target_res:.0f}m_{source}_utm.tif")

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
        try:
            self._dem = load_dem(dem_path, max_domain_km=200.0)
            self._render_terrain(self._dem)  # show the MNT, no hazard overlay yet
        except Exception as exc:
            self._dem = None
            QtWidgets.QMessageBox.warning(self, "MNT", f"MNT préparé, affichage impossible : {exc}")
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

        win = QtWidgets.QHBoxLayout()
        win.addWidget(QtWidgets.QLabel("Créneau de vol :"))
        self.window_slider = QRangeSlider(QtCore.Qt.Horizontal)
        self.window_label = QtWidgets.QLabel("")
        # Cap the slider at the forecast horizon (now + ~48 h). Configure before connecting
        # so the initial setValue doesn't fire _on_window_change before the labels exist.
        max_h = self._forecast_horizon_max()
        self.window_slider.setRange(0, max_h)
        self.window_slider.setValue((min(9, max_h - 1), min(15, max_h)))
        self.window_slider.valueChanged.connect(self._on_window_change)
        win.addWidget(self.window_slider, stretch=1)
        win.addWidget(self.window_label)
        lay.addLayout(win)
        self.horizon_label = QtWidgets.QLabel("")
        self.horizon_label.setStyleSheet("color: #777;")
        lay.addWidget(self.horizon_label)

        self.btn_creneau = QtWidgets.QPushButton(
            "Valider le créneau horaire (criblage temporel)")
        self.btn_creneau.setStyleSheet(GREEN_BTN_QSS)
        self.btn_creneau.clicked.connect(self.on_run_hourly)
        lay.addWidget(self.btn_creneau)

        opt = QtWidgets.QHBoxLayout()
        self.basemap_combo = QtWidgets.QComboBox()
        self.basemap_combo.addItems([NO_BASEMAP, *map2d.BASEMAP_SOURCES.keys()])
        self.basemap_combo.setCurrentText("IGN plan")
        self.basemap_combo.currentTextChanged.connect(self._on_basemap_change)
        opt.addWidget(QtWidgets.QLabel("Fond :"))
        opt.addWidget(self.basemap_combo)
        opt.addStretch(1)
        lay.addLayout(opt)

        self.fig = Figure(figsize=(6, 5))
        self.canvas = FigureCanvasQTAgg(self.fig)
        lay.addWidget(self.canvas)
        self._pan = None
        self._pan_moved = False
        self._home_extent = None
        self.canvas.mpl_connect("scroll_event", self._on_canvas_scroll)
        self.canvas.mpl_connect("button_press_event", self._on_canvas_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_canvas_motion)
        self.canvas.mpl_connect("button_release_event", self._on_canvas_release)

        slider_row = QtWidgets.QHBoxLayout()
        self.hour_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.hour_slider.setMinimum(0)
        self.hour_slider.setMaximum(0)
        self.hour_slider.valueChanged.connect(self._on_slider_change)
        self.hour_label = QtWidgets.QLabel("")
        slider_row.addWidget(QtWidgets.QLabel("Heure affichée :"))
        slider_row.addWidget(self.hour_slider)
        slider_row.addWidget(self.hour_label)
        self.hour_widget = QtWidgets.QWidget()
        self.hour_widget.setLayout(slider_row)
        self.hour_widget.setVisible(False)
        lay.addWidget(self.hour_widget)

        refine_row = QtWidgets.QHBoxLayout()
        refine_row.addWidget(QtWidgets.QLabel("Échelle d'affinage :"))
        self.refine_res_combo = QtWidgets.QComboBox()
        self.refine_res_combo.addItems(list(REFINE_RES_PRESETS))
        self.refine_res_combo.setCurrentText(REFINE_RES_DEFAULT)
        refine_row.addWidget(self.refine_res_combo)
        self.btn_refine = QtWidgets.QPushButton("Affiner spatialement l'heure affichée")
        self.btn_refine.setEnabled(False)
        self.btn_refine.clicked.connect(self.on_refine_spatial)
        refine_row.addWidget(self.btn_refine)
        refine_row.addStretch(1)
        lay.addLayout(refine_row)

        hint = QtWidgets.QLabel(
            "Glisser = déplacer · molette = zoom · double-clic = vue complète · "
            "clic = analyse Pass-2.   " + map2d.DISCLAIMER)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #a33; font-style: italic;")
        lay.addWidget(hint)
        self._on_window_change()
        return w

    # --- result-map navigation (drag pan + scroll zoom + click analysis) --------
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
        self._pan = (event.x, event.y, event.inaxes.get_xlim(), event.inaxes.get_ylim())
        self._pan_moved = False

    def _on_canvas_motion(self, event) -> None:
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
        was_pan = self._pan is not None and self._pan_moved
        self._pan = None
        if event.button == 1 and not was_pan:
            self._handle_hotspot(event)

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
        """Show the bare MNT hillshade. No basemap overlay here: the IGN plan's contour lines
        clash with the relief (they look like defects on the MNT). The basemap returns on the
        criblage result maps, where orientation matters."""
        self._last_map = None
        left, bottom, right, top = dem.bounds
        self.fig.clear()
        ax = self.fig.add_subplot(111)
        map2d.draw_hillshade(ax, dem)
        ax.set_title("MNT — zone de vol  (choisis un créneau, puis lance le criblage)")
        self._home_extent = (left, right, bottom, top)
        self.canvas.draw()

    def _build_analyse_tab(self) -> QtWidgets.QWidget:
        """Tab 3 — local lee-zone analysis: Pass-2 mesh/case controls + the 3D viewport."""
        w = QtWidgets.QWidget()
        self._analyse_tab = w
        lay = QtWidgets.QVBoxLayout(w)

        ctl = QtWidgets.QHBoxLayout()
        self.mesh_combo = QtWidgets.QComboBox()
        self.mesh_combo.addItems(list(PASS2_MESH_PRESETS))
        self.mesh_hint = QtWidgets.QLabel("")
        self.mesh_hint.setStyleSheet("color: #555;")
        self.mesh_combo.currentTextChanged.connect(self._update_mesh_hint)
        self.mesh_combo.setCurrentText(PASS2_MESH_DEFAULT)
        self.case_edit = QtWidgets.QLineEdit("")
        self.case_edit.setPlaceholderText("(détection auto d'un case NINJAFOAM_* en cache)")
        self.btn_load_p2 = QtWidgets.QPushButton("Charger un case")
        self.btn_load_p2.clicked.connect(self.on_load_pass2)
        ctl.addWidget(QtWidgets.QLabel("Maillage :"))
        ctl.addWidget(self.mesh_combo)
        ctl.addWidget(self.mesh_hint)
        ctl.addWidget(QtWidgets.QLabel("Case :"))
        ctl.addWidget(self.case_edit)
        ctl.addWidget(self.btn_load_p2)
        ctl.addStretch(1)
        lay.addLayout(ctl)
        self._update_mesh_hint()

        # The VTK/OpenGL viewport is created lazily (on first analysis) so the window starts
        # cleanly even without a GL context (headless).
        self._p2_widget = QtWidgets.QWidget()
        self._p2_layout = QtWidgets.QVBoxLayout(self._p2_widget)
        self.plotter = None
        self._p2_placeholder = QtWidgets.QLabel(
            "Le viewport 3D s'initialise à la première analyse.\n"
            "Clique un point chaud sur la carte du créneau, ou « Charger un case »."
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
    def _render_map(self, dem, hazard, title: str) -> None:
        """Draw a Pass-1 hazard map on the embedded canvas, with an optional web basemap."""
        self._last_map = (dem, hazard, title)
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
        self._home_extent = extent
        self.canvas.draw()

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
        wind_spd, wind_dir = self._representative_wind()
        resolution_m = 100.0
        cli = cfg.windninja_cli
        max_km = cfg.max_domain_km
        work = cfg.cache_dir / "champsaur" / "ihm_mass" / (
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
                work = cache_dir / "aoi" / "creneau" / f"{day_tag}_h{h:02d}_t"

                def hp(pct, msg, i=i):
                    on_progress(int((i + pct / 100.0) / count * 100),
                                f"{src} {i + 1}/{count} : {msg}")

                hazard, vel = hourly_indicator(
                    dem=dem, cli=cli, dem_path=dem_path, work_dir=work,
                    wind_speed_ms=spd, wind_from_deg=drc, resolution_m=200.0,
                    force_run=False, on_progress=hp, cancel=cancel,
                )
                out.append((labels[i], hazard, vel, find_direction_grid(work)))
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
        self._hourly = stack
        self.hour_widget.setVisible(True)
        self.hour_slider.blockSignals(True)
        self.hour_slider.setMaximum(max(0, len(stack) - 1))
        self.hour_slider.setValue(0)
        self.hour_slider.blockSignals(False)
        self._show_hour(0)
        self._finish_job(f"Criblage temporel ({src}) : {len(stack)} heures")

    def on_refine_spatial(self) -> None:
        """Refine the currently displayed hour with the SPATIAL sub-zone criblage and store
        it back in the hourly stack (re-shown when scrubbing back to that hour)."""
        if self._job is not None or not self._hourly:
            return
        i = int(self.hour_slider.value())
        if not (0 <= i < len(self._hourly)):
            return
        cfg = self.cfg
        dem_path = self._dem_path
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
        refine_res = REFINE_RES_PRESETS[self.refine_res_combo.currentText()]
        _ls, s_spd, s_drc = synthetic_series(count, start=start)[i]

        def fn(on_progress, cancel):  # worker thread — no Qt here
            import numpy as np

            from ..screening import indicator as ind2
            from ..screening.pass1 import mask_edge_buffer
            from ..screening.subzones import subzone_speed_field
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
                dem=dem, cli=cli, wind_at_center=provider, nx=2, ny=2,
                work_root=cache_dir / "aoi" / "creneau" / f"{day_tag}_h{h:02d}_s{refine_res:.0f}",
                resolution_m=refine_res, on_progress=on_progress, cancel=cancel,
            )
            hazard = ind2.hazard_indicator(dem, geo_dir, speed_grid=field)
            hazard = mask_edge_buffer(hazard, dem.resolution_m, 1500.0)
            return i, label, hazard

        self._cancelling = False
        self._set_running(True, f"Affinage spatial — {label}…")
        job = SolveJob(fn, self)
        job.progress.connect(self._on_job_progress)
        job.finished.connect(self._on_refine_finished)
        job.failed.connect(self._on_job_failed)
        self._job = job
        job.start()

    def _on_refine_finished(self, result) -> None:
        i, label, hazard = result
        if 0 <= i < len(self._hourly):
            self._hourly[i] = (f"{label}  (spatial)", hazard, None, None)
        self._finish_job(f"Heure affinée spatialement — {label}")
        if int(self.hour_slider.value()) == i:
            self._show_hour(i)

    def _on_slider_change(self, val: int) -> None:
        if self._hourly:
            self._show_hour(int(val))

    def _show_hour(self, i: int) -> None:
        if not (0 <= i < len(self._hourly)):
            return
        label, hazard, vel, ang = self._hourly[i]
        self._pass1_vel_path = vel
        self._pass1_ang_path = ang
        self._render_map(self._dem, hazard, f"Pass-1 horaire — {label}")
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
        cli = cfg.windninja_cli
        max_km = cfg.max_domain_km
        base_spd, rep_dir = self._representative_wind()
        work_root = cfg.cache_dir / "champsaur" / "ihm_subzones"

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

    # --- M3 handoff: click a Pass-1 hotspot -> Pass-2 momentum -> 3D --------------
    def _handle_hotspot(self, event) -> None:
        """A (non-drag) left-click on the 2D map launches a Pass-2 solve at that feature."""
        if event.inaxes is None or event.xdata is None:
            return
        if self._dem is None:
            self.statusBar().showMessage(
                "Prépare une zone et lance un criblage, puis clique un point chaud.")
            return
        if self._job is not None:
            return

        x, y = float(event.xdata), float(event.ydata)
        mesh_count, _iters, mesh_name = self._selected_mesh()
        bc_spd, bc_dir, wind_src = self._pass2_wind_at(x, y)
        resp = QtWidgets.QMessageBox.question(
            self, "Lancer Pass-2",
            f"Lancer un calcul momentum autour de ({x:.0f}, {y:.0f}) ?\n\n"
            f"Fenêtre ~{PASS2_HALF_WIDTH_M * 2 / 1000:.0f} km, maillage « {mesh_name} » "
            f"({mesh_count:,} mailles, ~{_estimate_minutes(mesh_count)} min).\n"
            f"Vent ({wind_src}) : {bc_spd:.0f} m/s de {bc_dir:.0f}°.",
        )
        if resp != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        if self.fig.axes:  # mark the picked spot on the map
            self.fig.axes[0].plot(
                x, y, marker="*", markersize=15, color="cyan", markeredgecolor="k"
            )
            self.canvas.draw_idle()
        self._launch_pass2_at(x, y, bc_spd, bc_dir, wind_src)

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

    def _launch_pass2_at(self, x: float, y: float, bc_spd: float, bc_dir: float,
                         wind_src: str) -> None:
        cfg = self.cfg
        dem = self._dem
        cli = cfg.windninja_cli
        half_m = PASS2_HALF_WIDTH_M
        mesh_count, iterations, _name = self._selected_mesh()
        pass2_dir = cfg.cache_dir / "champsaur" / "pass2"

        def fn(on_progress, cancel):  # worker thread — no Qt here
            crop = crop_dem(dem, x, y, half_m)
            crop_path = pass2_dir / f"ihm_crop_{x:.0f}_{y:.0f}_{2 * half_m:.0f}m.tif"
            write_dem(crop, crop_path)
            run = run_momentum(
                cli=cli, dem_path=str(crop_path), working_dir=str(pass2_dir / "ihm_run"),
                wind_speed_ms=bc_spd, wind_from_deg=bc_dir,
                mesh_count=mesh_count, iterations=iterations,
                on_progress=on_progress, cancel=cancel,
            )
            if run.returncode not in (0, None):
                raise RuntimeError(
                    f"échec momentum rc={run.returncode}\n{run.stdout[-800:]}"
                )
            if run.openfoam_case_dir is None:
                raise RuntimeError("momentum terminé mais aucun case OpenFOAM localisé")
            return str(run.openfoam_case_dir), bc_dir, (x, y)

        self._cancelling = False
        self._set_running(True, f"Pass-2 ({wind_src}) en ({x:.0f}, {y:.0f})…")
        job = SolveJob(fn, self)
        job.progress.connect(self._on_job_progress)
        job.finished.connect(self._on_pass2_finished)
        job.failed.connect(self._on_job_failed)
        self._job = job
        job.start()

    def _on_pass2_finished(self, result) -> None:
        case_dir, wind_dir, xy = result
        if not self._ensure_plotter():
            self._finish_job("Viewport 3D indisponible")
            return
        mfd = volume3d.mean_flow_vector(wind_dir)
        self.plotter.clear()
        volume3d.populate_plotter(self.plotter, case_dir, mfd, show_turbulence=False)
        self.plotter.reset_camera()
        self.tabs.setCurrentWidget(self._analyse_tab)
        self._finish_job(f"Rotor Pass-2 en ({xy[0]:.0f}, {xy[1]:.0f})")

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
                self, "Aucun case Pass-2",
                "Aucun case NINJAFOAM_* en cache. Lance d'abord un calcul momentum "
                "(clic sur la carte, ou scripts/pass2_smoke_test.py).",
            )
            return
        if not self._ensure_plotter():
            return

        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            mfd = volume3d.mean_flow_vector(self._representative_wind()[1])
            self.plotter.clear()
            volume3d.populate_plotter(self.plotter, case, mfd, show_turbulence=False)
            self.plotter.reset_camera()
            self.tabs.setCurrentWidget(self._analyse_tab)
            self.statusBar().showMessage(f"Case Pass-2 chargé : {Path(case).name}")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Erreur Pass-2", str(exc))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
