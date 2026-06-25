"""Interactive map tab (ADR-0012): a Leaflet slippy-map in a QWebEngineView.

Lets the user pan/zoom (drag + scroll, world-wide) over IGN / OSM / OpenTopoMap tiles and
draw a **rectangle** that defines the Pass-1 area of interest (AOI). The rectangle's
lat/lon bounds are sent back to Python over a QWebChannel and re-emitted as a Qt signal.

The web view is created defensively: if QtWebEngine can't initialize (e.g. headless / no GL),
a placeholder label is shown instead so the rest of the app still works.
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

# Ancelle, Hautes-Alpes (the requested initial centre).
ANCELLE_LATLON = (44.5547, 6.2031)

_IGN_PLAN = ("https://data.geopf.fr/wmts?layer=GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2"
             "&style=normal&tilematrixset=PM&Service=WMTS&Request=GetTile&Version=1.0.0"
             "&Format=image/png&TileMatrix={z}&TileCol={x}&TileRow={y}")
_IGN_ORTHO = ("https://data.geopf.fr/wmts?layer=ORTHOIMAGERY.ORTHOPHOTOS"
              "&style=normal&tilematrixset=PM&Service=WMTS&Request=GetTile&Version=1.0.0"
              "&Format=image/jpeg&TileMatrix={z}&TileCol={x}&TileRow={y}")


def _is_headless() -> bool:
    import os

    app = QtWidgets.QApplication.instance()
    if app is not None and app.platformName() == "offscreen":
        return True
    return os.environ.get("QT_QPA_PLATFORM", "") == "offscreen"


class _MapBridge(QtCore.QObject):
    """JS <-> Python bridge exposed over the QWebChannel as ``bridge``."""

    rectangleSelected = QtCore.Signal(float, float, float, float)  # south, west, north, east

    @QtCore.Slot(float, float, float, float)
    def on_rectangle(self, south: float, west: float, north: float, east: float) -> None:
        self.rectangleSelected.emit(south, west, north, east)


class MapTab(QtWidgets.QWidget):
    """First tab: the interactive selection map. Emits ``aoiSelected(s, w, n, e)`` (lat/lon)."""

    aoiSelected = QtCore.Signal(float, float, float, float)

    def __init__(self, center=ANCELLE_LATLON, radius_km: float = 30.0, parent=None):
        super().__init__(parent)
        self._bbox = None
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)  # let the map fill the whole tab

        if _is_headless():
            # QtWebEngine spawns a Chromium process that can't render (and crashes at exit)
            # under the offscreen platform — skip it in headless/CI runs.
            self.view = None
            lay.addWidget(QtWidgets.QLabel("Carte interactive désactivée (mode headless)."))
            return

        try:
            from PySide6.QtWebChannel import QWebChannel
            from PySide6.QtWebEngineWidgets import QWebEngineView

            self.view = QWebEngineView()
            self.bridge = _MapBridge()
            self.channel = QWebChannel()
            self.channel.registerObject("bridge", self.bridge)
            self.view.page().setWebChannel(self.channel)
            self.bridge.rectangleSelected.connect(self._on_rectangle)
            self.view.setHtml(_build_html(center, radius_km),
                              QtCore.QUrl("https://data.geopf.fr/"))
            lay.addWidget(self.view)
        except Exception as exc:  # QtWebEngine unavailable (headless / no GL)
            self.view = None
            lay.addWidget(QtWidgets.QLabel(f"Carte interactive indisponible ici :\n{exc}"))

    def selected_bbox(self):
        """Return the last AOI as (south, west, north, east) in lat/lon, or None."""
        return self._bbox

    def _on_rectangle(self, s: float, w: float, n: float, e: float) -> None:
        self._bbox = (s, w, n, e)
        self.aoiSelected.emit(s, w, n, e)


def _km_width(s, w, n, e):
    import math

    return (e - w) * 111.0 * math.cos(math.radians((s + n) / 2.0))


def _build_html(center, radius_km: float) -> str:
    import math

    lat, lon = center
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    south, west, north, east = lat - dlat, lon - dlon, lat + dlat, lon + dlon
    return _HTML_TEMPLATE.format(
        ign_plan=_IGN_PLAN, ign_ortho=_IGN_ORTHO,
        south=south, west=west, north=north, east=east,
    )


_HTML_TEMPLATE = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.css"/>
<style>html,body,#map{{height:100%;margin:0}}
/* GIMP-style dashed "marquee" rectangle-select icon for the only draw button. */
.leaflet-draw-toolbar a.leaflet-draw-draw-rectangle {{
  background-image:url("data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20width='18'%20height='18'%3E%3Crect%20x='2.5'%20y='3.5'%20width='13'%20height='11'%20fill='none'%20stroke='%23222'%20stroke-width='1.6'%20stroke-dasharray='2.4%201.6'/%3E%3C/svg%3E") !important;
  background-position:4px 4px !important; background-size:18px 18px !important;
  background-repeat:no-repeat !important;
}}</style>
</head><body>
<div id="map"></div>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.js"></script>
<script>
var map = L.map('map');
var ign = L.tileLayer('{ign_plan}', {{attribution:'IGN-Géoplateforme', maxZoom:19}});
var ortho = L.tileLayer('{ign_ortho}', {{attribution:'IGN-Géoplateforme', maxZoom:19}});
var osm = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
                      {{attribution:'OpenStreetMap', maxZoom:19}});
var topo = L.tileLayer('https://{{s}}.tile.opentopomap.org/{{z}}/{{x}}/{{y}}.png',
                       {{attribution:'OpenTopoMap', maxZoom:17}});
ign.addTo(map);
L.control.layers({{'IGN plan':ign,'IGN ortho':ortho,'OpenStreetMap':osm,'OpenTopoMap':topo}}).addTo(map);
map.fitBounds([[{south},{west}],[{north},{east}]]);

var drawn = new L.FeatureGroup();
map.addLayer(drawn);
L.drawLocal.draw.toolbar.buttons.rectangle = 'Sélectionner la zone (rectangle)';
// Only the CREATE (rectangle) tool — no edit/delete buttons; draw again to redo the selection.
var drawControl = new L.Control.Draw({{
  draw: {{rectangle:{{shapeOptions:{{color:'#e6194b',weight:2}}}},
         polygon:false, polyline:false, circle:false, marker:false, circlemarker:false}},
  edit: false
}});
map.addControl(drawControl);

function emit(layer) {{
  var b = layer.getBounds();
  if (window.bridge) window.bridge.on_rectangle(b.getSouth(), b.getWest(), b.getNorth(), b.getEast());
}}
map.on(L.Draw.Event.CREATED, function(e) {{ drawn.clearLayers(); drawn.addLayer(e.layer); emit(e.layer); }});

new QWebChannel(qt.webChannelTransport, function(channel) {{ window.bridge = channel.objects.bridge; }});
</script>
</body></html>
"""
