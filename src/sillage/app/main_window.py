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

DEFAULT_DEM = "cache/champsaur/ign/champsaur_rgealti_50m_prepared_utm.tif"

PASS2_HALF_WIDTH_M = 2500.0  # ~5 km feature window around the clicked hotspot

# ADR-0008: Pass-2 mesh resolution is a quality/time knob. Each preset = (mesh_count,
# iterations). Default = Medium; "refine on doubt" by picking a finer preset.
PASS2_MESH_PRESETS: dict[str, tuple[int, int]] = {
    "Coarse — fastest": (20_000, 100),
    "Medium — default": (50_000, 200),
    "Fine — slow": (150_000, 300),
    "Max — very slow": (400_000, 400),
}
PASS2_MESH_DEFAULT = "Medium — default"


def _estimate_minutes(mesh_count: int) -> int:
    """Rough runtime proxy (CPU-bound), calibrated on the Champsaur smoke run
    (~25k cells -> ~2 min). Indicative only — bounds the 'refine' choice (ADR-0008)."""
    return max(1, round(mesh_count / 12_000))


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Sillage — leeward turbulence screening")
        self.cfg = load_config()
        self._dem = None
        # Pass-1 wind field from the last mass run, for upstream-crest Pass-2 BC (M3).
        self._pass1_vel_path = None
        self._pass1_ang_path = None
        # Hourly stack: list of (label, hazard, vel_path, ang_path) for the time slider.
        self._hourly: list[tuple] = []
        # Last rendered (dem, hazard, title) so toggling the basemap can redraw it.
        self._last_map = None

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

        self.basemap_combo = QtWidgets.QComboBox()
        self.basemap_combo.addItems(["None", *map2d.BASEMAP_SOURCES.keys()])
        self.basemap_combo.setCurrentText("IGN plan")
        self.basemap_combo.currentTextChanged.connect(self._on_basemap_change)

        self.btn_geom = QtWidgets.QPushButton("Compute Pass-1 (geometry)")
        self.btn_geom.clicked.connect(self.on_compute_pass1)
        self.btn_mass = QtWidgets.QPushButton("Run WindNinja mass (Pass-1)")
        self.btn_mass.clicked.connect(self.on_run_mass)
        self.hours_spin = QtWidgets.QSpinBox()
        self.hours_spin.setRange(1, 24)
        self.hours_spin.setValue(6)
        self.btn_hourly = QtWidgets.QPushButton("Run hourly (Pass-1, synthetic)")
        self.btn_hourly.clicked.connect(self.on_run_hourly)
        self.btn_subzones = QtWidgets.QPushButton("Run sub-zones (Pass-1, spatial)")
        self.btn_subzones.clicked.connect(self.on_run_subzones)

        self.case_edit = QtWidgets.QLineEdit("")
        self.case_edit.setPlaceholderText("(auto-detect cached NINJAFOAM_* case)")
        self.btn_load_p2 = QtWidgets.QPushButton("Load Pass-2 case (3D)")
        self.btn_load_p2.clicked.connect(self.on_load_pass2)

        self.mesh_combo = QtWidgets.QComboBox()
        self.mesh_combo.addItems(list(PASS2_MESH_PRESETS))
        self.mesh_hint = QtWidgets.QLabel("")
        self.mesh_hint.setStyleSheet("color: #555;")
        self.mesh_combo.currentTextChanged.connect(self._update_mesh_hint)
        self.mesh_combo.setCurrentText(PASS2_MESH_DEFAULT)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        self.btn_cancel = QtWidgets.QPushButton("Cancel")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self.on_cancel)

        form.addRow("DEM:", self.dem_edit)
        form.addRow("Wind FROM (deg):", self.wind_dir)
        form.addRow("Wind speed (m/s):", self.wind_spd)
        form.addRow("Basemap:", self.basemap_combo)
        form.addRow(self.btn_geom)
        form.addRow(self.btn_mass)
        form.addRow("Hours:", self.hours_spin)
        form.addRow(self.btn_hourly)
        form.addRow(self.btn_subzones)
        form.addRow(QtWidgets.QLabel("———"))
        form.addRow("Pass-2 mesh:", self.mesh_combo)
        form.addRow(self.mesh_hint)
        form.addRow("Pass-2 case:", self.case_edit)
        form.addRow(self.btn_load_p2)
        form.addRow(self.progress)
        form.addRow(self.btn_cancel)
        self._update_mesh_hint()

        note = QtWidgets.QLabel(map2d.DISCLAIMER)
        note.setWordWrap(True)
        note.setStyleSheet("color: #a33; font-style: italic;")
        form.addRow(note)

        self._run_buttons = [self.btn_geom, self.btn_mass, self.btn_hourly,
                             self.btn_subzones, self.btn_load_p2]
        self._job: SolveJob | None = None
        self._cancelling = False
        w.setMaximumWidth(380)
        return w

    def _build_pass1_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        self.fig = Figure(figsize=(6, 5))
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.nav = NavigationToolbar2QT(self.canvas, w)
        lay.addWidget(self.nav)
        lay.addWidget(self.canvas)

        slider_row = QtWidgets.QHBoxLayout()
        self.hour_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.hour_slider.setMinimum(0)
        self.hour_slider.setMaximum(0)
        self.hour_slider.valueChanged.connect(self._on_slider_change)
        self.hour_label = QtWidgets.QLabel("")
        slider_row.addWidget(QtWidgets.QLabel("Hour:"))
        slider_row.addWidget(self.hour_slider)
        slider_row.addWidget(self.hour_label)
        self.hour_widget = QtWidgets.QWidget()
        self.hour_widget.setLayout(slider_row)
        self.hour_widget.setVisible(False)
        lay.addWidget(self.hour_widget)

        hint = QtWidgets.QLabel(
            "Tip: left-click a hotspot on the map to launch a Pass-2 momentum solve there."
        )
        hint.setStyleSheet("color: #555;")
        lay.addWidget(hint)
        self.canvas.mpl_connect("button_press_event", self.on_map_click)
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

    # --- Pass-2 mesh quality/time knob (ADR-0008) --------------------------------
    def _selected_mesh(self) -> tuple[int, int, str]:
        name = self.mesh_combo.currentText()
        mesh_count, iterations = PASS2_MESH_PRESETS[name]
        return mesh_count, iterations, name

    def _update_mesh_hint(self, *_args) -> None:
        mesh_count, iters, _name = self._selected_mesh()
        self.mesh_hint.setText(
            f"~{mesh_count:,} cells, {iters} iters - "
            f"~{_estimate_minutes(mesh_count)} min (rough)"
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
        if source != "None":
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
                self.statusBar().showMessage(f"Basemap unavailable ({exc}); using hillshade")
            ax.set_xlabel("Easting (m)")
            ax.set_ylabel("Northing (m)")
        else:
            im = map2d.draw_indicator(ax, dem, hazard)
        self.fig.colorbar(im, ax=ax, label="leeward hazard indicator (0–1)")
        ax.set_title(f"{title}\n{map2d.DISCLAIMER}")
        self.canvas.draw()

    def _on_basemap_change(self, *_args) -> None:
        if self._last_map is not None:
            self._render_map(*self._last_map)

    # --- actions ---------------------------------------------------------------
    def on_compute_pass1(self) -> None:
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            dem = load_dem(self.dem_edit.text(), max_domain_km=self.cfg.max_domain_km)
            self._dem = dem
            self._set_single_map_mode(True)
            self._pass1_vel_path = None  # geometry-only has no Pass-1 wind field
            self._pass1_ang_path = None
            hazard = ind.hazard_indicator(dem, self.wind_dir.value())
            self._render_map(
                dem, hazard, f"Pass-1 geometry-only — wind from {self.wind_dir.value():.0f}°"
            )
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
            hazard, vel_path = hourly_indicator(
                dem=dem, cli=cli, dem_path=dem_path, work_dir=work,
                wind_speed_ms=wind_spd, wind_from_deg=wind_dir, resolution_m=resolution_m,
                force_run=True, on_progress=on_progress, cancel=cancel,
            )
            ang_path = find_direction_grid(work)
            return dem, hazard, wind_dir, vel_path, ang_path

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
        dem, hazard, wind_dir, vel_path, ang_path = result
        self._dem = dem
        self._pass1_vel_path = vel_path
        self._pass1_ang_path = ang_path
        self._render_map(dem, hazard, f"Pass-1 WindNinja mass — wind from {wind_dir:.0f}°")
        self._set_single_map_mode(True)
        self._finish_job("Pass-1 mass done")

    # --- hourly Pass-1 + time slider -------------------------------------------
    def _set_single_map_mode(self, single: bool) -> None:
        """Single-map mode hides the hour slider; hourly mode shows it."""
        self.hour_widget.setVisible(not single)
        if single:
            self._hourly = []

    def on_run_hourly(self) -> None:
        """Run a synthetic hourly Pass-1 loop on the worker; populate the time slider."""
        if self._job is not None:
            return
        cfg = self.cfg
        dem_path = self.dem_edit.text()
        hours = int(self.hours_spin.value())
        resolution_m = 100.0
        cli = cfg.windninja_cli
        max_km = cfg.max_domain_km
        cache_dir = cfg.cache_dir

        def fn(on_progress, cancel):  # worker thread — no Qt here
            dem = load_dem(dem_path, max_domain_km=max_km)
            series = synthetic_series(hours)
            n = len(series)
            out = []
            for i, (label, spd, drc) in enumerate(series):
                if cancel():
                    raise RuntimeError("cancelled")
                work = cache_dir / "champsaur" / "ihm_hourly" / (
                    f"h{i:02d}_{drc:.0f}_{spd:.0f}_{resolution_m:.0f}m"
                )

                def hp(pct, msg, i=i):  # map per-hour progress to overall 0..100
                    on_progress(int((i + pct / 100.0) / n * 100), f"hour {i + 1}/{n}: {msg}")

                hazard, vel = hourly_indicator(
                    dem=dem, cli=cli, dem_path=dem_path, work_dir=work,
                    wind_speed_ms=spd, wind_from_deg=drc, resolution_m=resolution_m,
                    force_run=False, on_progress=hp, cancel=cancel,
                )
                out.append((label, hazard, vel, find_direction_grid(work)))
            return dem, out

        self._cancelling = False
        self._set_running(True, f"Pass-1 hourly ({hours} h)…")
        job = SolveJob(fn, self)
        job.progress.connect(self._on_job_progress)
        job.finished.connect(self._on_hourly_finished)
        job.failed.connect(self._on_job_failed)
        self._job = job
        job.start()

    def _on_hourly_finished(self, result) -> None:
        dem, stack = result
        self._dem = dem
        self._hourly = stack
        self.hour_widget.setVisible(True)
        self.hour_slider.blockSignals(True)
        self.hour_slider.setMaximum(max(0, len(stack) - 1))
        self.hour_slider.setValue(0)
        self.hour_slider.blockSignals(False)
        self._show_hour(0)
        self._finish_job(f"Pass-1 hourly: {len(stack)} hours")

    def _on_slider_change(self, val: int) -> None:
        if self._hourly:
            self._show_hour(int(val))

    def _show_hour(self, i: int) -> None:
        if not (0 <= i < len(self._hourly)):
            return
        label, hazard, vel, ang = self._hourly[i]
        self._pass1_vel_path = vel
        self._pass1_ang_path = ang
        self._render_map(self._dem, hazard, f"Pass-1 hourly — {label}")
        self.hour_label.setText(label)

    # --- sub-zone spatial Pass-1 (ADR-0007) ------------------------------------
    def on_run_subzones(self) -> None:
        """Run a 2x2 sub-zone Pass-1 with a synthetic *spatial* wind on the worker.

        Demonstrates ADR-0007 offline: each tile gets its own wind (direction sweeps W->E),
        the tiles are mosaicked into one spatially-varying screening map. Swap the synthetic
        provider for wind.profile.crest_wind_provider(source="arome") for real AROME winds.
        """
        if self._job is not None:
            return
        from ..screening.pass1 import mask_edge_buffer
        from ..screening.subzones import subzone_speed_field

        cfg = self.cfg
        dem_path = self.dem_edit.text()
        cli = cfg.windninja_cli
        max_km = cfg.max_domain_km
        rep_dir = self.wind_dir.value()
        base_spd = self.wind_spd.value()
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
        self._set_running(True, "Pass-1 sub-zones (spatial)…")
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
            dem, hazard, f"Pass-1 sub-zones (spatial wind) — base from {rep_dir:.0f}°"
        )
        self._finish_job("Pass-1 sub-zones done")

    def _on_job_failed(self, msg: str) -> None:
        if self._cancelling:
            self._finish_job("Run cancelled")
        else:
            self._finish_job("Run failed")
            QtWidgets.QMessageBox.critical(self, "WindNinja error", msg)

    # --- M3 handoff: click a Pass-1 hotspot -> Pass-2 momentum -> 3D --------------
    def on_map_click(self, event) -> None:
        """Left-click on the 2D map launches a Pass-2 momentum solve at that feature."""
        if event.inaxes is None or event.xdata is None or event.button != 1:
            return
        if getattr(self.nav, "mode", ""):  # pan/zoom tool active -> not a hotspot pick
            return
        if self._dem is None:
            self.statusBar().showMessage("Compute a Pass-1 map first, then click a hotspot.")
            return
        if self._job is not None:
            return

        x, y = float(event.xdata), float(event.ydata)
        mesh_count, _iters, mesh_name = self._selected_mesh()
        bc_spd, bc_dir, wind_src = self._pass2_wind_at(x, y)
        resp = QtWidgets.QMessageBox.question(
            self, "Launch Pass-2",
            f"Run a momentum solve around ({x:.0f}, {y:.0f})?\n\n"
            f"~{PASS2_HALF_WIDTH_M * 2 / 1000:.0f} km window, mesh '{mesh_name}' "
            f"({mesh_count:,} cells, ~{_estimate_minutes(mesh_count)} min).\n"
            f"Wind ({wind_src}): {bc_spd:.0f} m/s from {bc_dir:.0f} deg.",
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
        available, else the controls' domain wind (M3 refinement)."""
        ctrl_dir = self.wind_dir.value()
        ctrl_spd = self.wind_spd.value()
        if self._pass1_vel_path and self._pass1_ang_path:
            bc = upstream_crest_wind(
                self._pass1_vel_path, self._pass1_ang_path, x, y, ctrl_dir
            )
            if bc is not None:
                return bc[0], bc[1], "Pass-1 upstream"
        return ctrl_spd, ctrl_dir, "controls"

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
                    f"momentum failed rc={run.returncode}\n{run.stdout[-800:]}"
                )
            if run.openfoam_case_dir is None:
                raise RuntimeError("momentum ran but no OpenFOAM case was located")
            return str(run.openfoam_case_dir), bc_dir, (x, y)

        self._cancelling = False
        self._set_running(True, f"Pass-2 ({wind_src}) at ({x:.0f}, {y:.0f})…")
        job = SolveJob(fn, self)
        job.progress.connect(self._on_job_progress)
        job.finished.connect(self._on_pass2_finished)
        job.failed.connect(self._on_job_failed)
        self._job = job
        job.start()

    def _on_pass2_finished(self, result) -> None:
        case_dir, wind_dir, xy = result
        if not self._ensure_plotter():
            self._finish_job("3D viewport unavailable")
            return
        mfd = volume3d.mean_flow_vector(wind_dir)
        self.plotter.clear()
        volume3d.populate_plotter(self.plotter, case_dir, mfd, show_turbulence=False)
        self.plotter.reset_camera()
        self.tabs.setCurrentWidget(self._p2_widget)
        self._finish_job(f"Pass-2 rotor at ({xy[0]:.0f}, {xy[1]:.0f})")

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
