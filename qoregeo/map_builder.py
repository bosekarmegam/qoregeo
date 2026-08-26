"""
qoregeo.map_builder
===================
Standalone interactive HTML maps built on Leaflet.js.

Every function here writes one self-contained ``.html`` file. Open it in a
browser, mail it, drop it in a bucket — there is no server, no build step and
no Python running behind it.

Leaflet itself is pulled from a CDN with an automatic fallback to a second
CDN, and every map degrades to a readable error message rather than a blank
page if both are unreachable. For output that needs no network at all, use
:mod:`qoregeo.static_map` to render SVG or PNG.

Templates use ``/*__PLACEHOLDER__*/`` markers rather than ``str.format``,
because JavaScript is full of braces and doubling every one of them makes
the templates unreadable and easy to break.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

Feature = Dict[str, Any]
Coord = Tuple[float, float]

#: Basemap tile layers, keyed by the name you pass as ``basemap=``.
BASEMAPS: Dict[str, Dict[str, str]] = {
    "dark": {
        "url": "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        "attribution": '&copy; <a href="https://www.openstreetmap.org">OSM</a> '
                       '&copy; <a href="https://carto.com">CARTO</a>',
        "subdomains": "abcd",
    },
    "light": {
        "url": "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        "attribution": '&copy; <a href="https://www.openstreetmap.org">OSM</a> '
                       '&copy; <a href="https://carto.com">CARTO</a>',
        "subdomains": "abcd",
    },
    "streets": {
        "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": '&copy; <a href="https://www.openstreetmap.org">OpenStreetMap</a> contributors',
        "subdomains": "abc",
    },
    "terrain": {
        "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        "attribution": '&copy; <a href="https://opentopomap.org">OpenTopoMap</a> '
                       '&copy; OpenStreetMap contributors',
        "subdomains": "abc",
    },
    "satellite": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/"
               "World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "attribution": "Imagery &copy; Esri, Maxar, Earthstar Geographics",
        "subdomains": "",
    },
}

DEFAULT_BASEMAP = "dark"

#: Colour-blind-safe categorical palette, ordered for maximum separation.
CATEGORY_COLOURS = [
    "#00D4AA", "#FFB000", "#7B61FF", "#FF6B6B", "#4DA6FF",
    "#FF8FD9", "#9FE870", "#FFD700", "#00B7C3", "#E06C00",
]

#: Sequential ramp used by choropleths — perceptually ordered dark → bright.
SEQUENTIAL_RAMP = [
    "#08304B", "#00506B", "#00727F", "#00957F", "#2FB86F",
    "#8AD65A", "#E8E45A",
]

#: Above this many features, clustering is switched on unless told otherwise.
AUTO_CLUSTER_THRESHOLD = 750

LEAFLET_CSS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
LEAFLET_JS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
LEAFLET_JS_FALLBACK = "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"
HEAT_JS = "https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"
HEAT_JS_FALLBACK = "https://cdn.jsdelivr.net/npm/leaflet.heat@0.2.0/dist/leaflet-heat.js"
CLUSTER_CSS = "https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css"
CLUSTER_CSS_DEFAULT = (
    "https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"
)
CLUSTER_JS = (
    "https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"
)


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _write(path: str, content: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def _render(template: str, values: Dict[str, Any]) -> str:
    """Substitute ``/*__KEY__*/`` markers — brace-safe, unlike str.format."""
    out = template
    for key, value in values.items():
        out = out.replace(f"/*__{key}__*/", value if isinstance(value, str) else json.dumps(value))
    return out


def _escape_html(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _auto_center(features: Sequence[Feature]) -> Coord:
    """Centroid of every coordinate in the collection."""
    from .geometry import coords_of

    lats: List[float] = []
    lngs: List[float] = []
    for feat in features:
        for lng, lat in coords_of(feat.get("geometry") or {}):
            lats.append(lat)
            lngs.append(lng)
    if not lats:
        return (0.0, 0.0)
    return (sum(lats) / len(lats), sum(lngs) / len(lngs))


def _basemap_config(basemap: str, extra: Optional[Sequence[str]] = None) -> List[Dict[str, str]]:
    """Ordered basemap list — the requested one first, so it renders by default."""
    name = basemap if basemap in BASEMAPS else DEFAULT_BASEMAP
    names = [name]
    for other in (extra if extra is not None else BASEMAPS.keys()):
        if other in BASEMAPS and other not in names:
            names.append(other)
    return [dict(BASEMAPS[n], name=n) for n in names]


def _category_styles(
    features: Sequence[Feature],
    colour_by: Optional[str],
) -> Tuple[Dict[str, str], str]:
    """Map each distinct value of ``colour_by`` to a colour from the palette."""
    if not colour_by:
        return ({}, "")
    values: List[str] = []
    for feat in features:
        value = feat.get("properties", {}).get(colour_by)
        text = "—" if value is None else str(value)
        if text not in values:
            values.append(text)
    return (
        {v: CATEGORY_COLOURS[i % len(CATEGORY_COLOURS)] for i, v in enumerate(values)},
        colour_by,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Shared page chrome
# ─────────────────────────────────────────────────────────────────────────────

_STYLE = """
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0f111a; color: #fff;
  }
  #map { width: 100vw; height: 100vh; }
  .q-panel {
    position: fixed; z-index: 9999;
    background: rgba(10,12,20,.82); backdrop-filter: blur(12px);
    border: 1px solid rgba(0,212,170,.25); border-radius: 10px;
    padding: 10px 14px; font-size: 13px; color: rgba(255,255,255,.75);
    box-shadow: 0 6px 24px rgba(0,0,0,.35);
  }
  #brand { bottom: 16px; left: 16px; }
  #brand strong { color: #00D4AA; font-size: 14px; letter-spacing: .3px; }
  #count { top: 16px; right: 16px; }
  #count span { color: #00D4AA; font-weight: 600; }
  #legend { bottom: 16px; right: 16px; max-height: 46vh; overflow-y: auto; }
  #legend h4 {
    font-size: 11px; text-transform: uppercase; letter-spacing: .8px;
    color: rgba(255,255,255,.45); margin-bottom: 8px; font-weight: 600;
  }
  #legend .row { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
  #legend .swatch {
    width: 12px; height: 12px; border-radius: 3px; flex: none;
    border: 1px solid rgba(255,255,255,.25);
  }
  #search { top: 16px; left: 60px; padding: 0; }
  #search input {
    background: transparent; border: 0; outline: none; color: #fff;
    font-size: 13px; padding: 10px 14px; width: 220px;
    font-family: inherit;
  }
  #search input::placeholder { color: rgba(255,255,255,.35); }
  .q-error {
    position: fixed; top: 50%; left: 50%; transform: translate(-50%,-50%);
    background: #c0392b; color: #fff; padding: 22px 26px; border-radius: 10px;
    z-index: 99999; max-width: 420px; line-height: 1.5; font-size: 14px;
  }
  .leaflet-popup-content { margin: 10px 12px; }
  .q-pop { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
  .q-pop td { padding: 2px 10px 2px 0; vertical-align: top; }
  .q-pop td:first-child { color: #7c8299; font-weight: 600; }
"""

_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>/*__TITLE_HTML__*/</title>
<link rel="stylesheet" href="/*__LEAFLET_CSS__*/"/>
/*__EXTRA_HEAD__*/
<style>/*__STYLE__*/</style>
</head>
<body>
<div id="map"></div>
<div id="brand" class="q-panel"><strong>QOREgeo</strong> · /*__TITLE_HTML__*/</div>
/*__PANELS__*/
<script src="/*__LEAFLET_JS__*/"></script>
<script>
  // If unpkg is blocked or slow, pull Leaflet from the mirror before we need it.
  if (typeof L === 'undefined') {
    document.write('<script src="/*__LEAFLET_JS_FALLBACK__*/"><\\/script>');
  }
</script>
/*__EXTRA_SCRIPTS__*/
<script>
function qoreFail(message) {
  var box = document.createElement('div');
  box.className = 'q-error';
  box.innerHTML = '<b style="font-size:16px">Map could not load</b><br><br>' + message;
  document.body.appendChild(box);
}
function qoreBasemaps(map, specs) {
  var layers = {}, first = null;
  specs.forEach(function (spec) {
    var options = { attribution: spec.attribution, maxZoom: 19 };
    if (spec.subdomains) { options.subdomains = spec.subdomains; }
    var layer = L.tileLayer(spec.url, options);
    layers[spec.name] = layer;
    if (!first) { first = layer; layer.addTo(map); }
  });
  if (specs.length > 1) { L.control.layers(layers, null, { position: 'topleft' }).addTo(map); }
  return layers;
}
</script>
"""

_TAIL = """
</body>
</html>
"""


def _page(
    title: str,
    panels: str,
    body_script: str,
    extra_head: str = "",
    extra_scripts: str = "",
) -> str:
    head = _render(
        _HEAD,
        {
            "TITLE_HTML": _escape_html(title),
            "STYLE": _STYLE,
            "LEAFLET_CSS": LEAFLET_CSS,
            "LEAFLET_JS": LEAFLET_JS,
            "LEAFLET_JS_FALLBACK": LEAFLET_JS_FALLBACK,
            "PANELS": panels,
            "EXTRA_HEAD": extra_head,
            "EXTRA_SCRIPTS": extra_scripts,
        },
    )
    return head + "<script>\n" + body_script + "\n</script>" + _TAIL


# ─────────────────────────────────────────────────────────────────────────────
# Interactive feature map
# ─────────────────────────────────────────────────────────────────────────────

_MAP_SCRIPT = """
document.addEventListener('DOMContentLoaded', function () {
  try {
    if (typeof L === 'undefined') { throw new Error('Leaflet failed to load from both CDNs.'); }

    var DATA = /*__GEOJSON__*/;
    var COLOURS = /*__COLOURS__*/;
    var COLOUR_BY = /*__COLOUR_BY__*/;
    var CLUSTER = /*__CLUSTER__*/;
    var ZOOM = /*__ZOOM__*/;
    var CENTER = /*__CENTER__*/;
    var TOOLTIP_FIELD = /*__TOOLTIP_FIELD__*/;
    var POPUP_FIELDS = /*__POPUP_FIELDS__*/;
    var ACCENT = '#00D4AA';

    var map = L.map('map', { zoomControl: true, preferCanvas: true });
    qoreBasemaps(map, /*__BASEMAPS__*/);
    L.control.scale({ imperial: false }).addTo(map);

    function colourOf(feature) {
      if (!COLOUR_BY) { return ACCENT; }
      var raw = (feature.properties || {})[COLOUR_BY];
      return COLOURS[raw === null || raw === undefined ? '—' : String(raw)] || ACCENT;
    }

    function popupHtml(feature) {
      var props = feature.properties || {};
      var keys = POPUP_FIELDS || Object.keys(props);
      var rows = keys.filter(function (k) {
        return String(k).charAt(0) !== '_' && props[k] !== undefined;
      }).map(function (k) {
        return '<tr><td>' + k + '</td><td>' + props[k] + '</td></tr>';
      }).join('');
      return rows
        ? '<div class="q-pop"><table>' + rows + '</table></div>'
        : '<div class="q-pop" style="color:#7c8299">No properties</div>';
    }

    var shown = 0;
    function onEach(feature, layer) {
      layer.bindPopup(popupHtml(feature), { maxWidth: 340 });
      if (TOOLTIP_FIELD && (feature.properties || {})[TOOLTIP_FIELD] !== undefined) {
        layer.bindTooltip(String(feature.properties[TOOLTIP_FIELD]), { direction: 'top' });
      }
      shown++;
    }

    var geoLayer = L.geoJSON(DATA, {
      onEachFeature: onEach,
      pointToLayer: function (feature, latlng) {
        return L.circleMarker(latlng, {
          radius: 6, color: '#ffffff', weight: 1.2, opacity: 0.85,
          fillColor: colourOf(feature), fillOpacity: 0.9
        });
      },
      style: function (feature) {
        var colour = colourOf(feature);
        return { color: colour, weight: 2, opacity: 0.9, fillColor: colour, fillOpacity: 0.22 };
      }
    });

    // markercluster is optional: if its script did not load, show plain markers
    // rather than failing the whole map.
    var target = geoLayer;
    if (CLUSTER && typeof L.markerClusterGroup === 'function') {
      target = L.markerClusterGroup({ chunkedLoading: true, maxClusterRadius: 55 });
      target.addLayer(geoLayer);
    }
    target.addTo(map);

    var countEl = document.getElementById('n');
    if (countEl) { countEl.textContent = shown; }

    var bounds = geoLayer.getBounds();
    if (bounds && bounds.isValid()) {
      map.fitBounds(bounds.pad(0.12));
      if (map.getZoom() > ZOOM + 6) { map.setZoom(ZOOM); }
    } else {
      map.setView(CENTER, ZOOM);
    }

    var box = document.getElementById('q-search');
    if (box) {
      box.addEventListener('input', function () {
        var needle = box.value.trim().toLowerCase();
        var visible = 0;
        geoLayer.eachLayer(function (layer) {
          var text = JSON.stringify(layer.feature.properties || {}).toLowerCase();
          var hit = !needle || text.indexOf(needle) !== -1;
          if (hit) { visible++; }
          if (layer.setStyle) {
            layer.setStyle({ opacity: hit ? 0.85 : 0.08, fillOpacity: hit ? 0.9 : 0.05 });
          }
        });
        if (countEl) { countEl.textContent = visible; }
      });
    }
  } catch (err) {
    console.error('QOREgeo:', err);
    qoreFail(err.message);
  }
});
"""


def build_map(
    features: Sequence[Feature],
    output_path: str,
    title: str = "QOREgeo Map",
    zoom: int = 5,
    center: Optional[Coord] = None,
    basemap: str = DEFAULT_BASEMAP,
    cluster: Optional[bool] = None,
    colour_by: Optional[str] = None,
    tooltip_field: Optional[str] = None,
    popup_fields: Optional[Sequence[str]] = None,
    search: bool = True,
    legend: bool = True,
    quiet: bool = False,
) -> str:
    """
    Write an interactive map of any GeoJSON features.

    Points, lines and polygons all render — the map is driven by
    ``L.geoJSON``, so mixed collections work in one layer.

    Parameters
    ----------
    basemap       : ``dark``, ``light``, ``streets``, ``terrain`` or ``satellite``
    cluster       : group nearby markers; ``None`` turns it on automatically
                    above :data:`AUTO_CLUSTER_THRESHOLD` features
    colour_by     : property name to colour features by, with a legend
    tooltip_field : property shown on hover
    popup_fields  : restrict the popup to these properties, in this order
    search        : show a live filter box
    quiet         : suppress the "saved" message

    Returns
    -------
    The output path.
    """
    features = list(features)
    use_cluster = (
        len(features) >= AUTO_CLUSTER_THRESHOLD if cluster is None else bool(cluster)
    )
    colours, colour_key = _category_styles(features, colour_by)

    panels = ['<div id="count" class="q-panel"><span id="n">0</span> features</div>']
    if search:
        panels.append(
            '<div id="search" class="q-panel">'
            '<input id="q-search" type="search" placeholder="Filter features…" '
            'aria-label="Filter features"></div>'
        )
    if legend and colours:
        rows = "".join(
            f'<div class="row"><span class="swatch" style="background:{colour}"></span>'
            f"<span>{_escape_html(value)}</span></div>"
            for value, colour in colours.items()
        )
        panels.append(
            f'<div id="legend" class="q-panel"><h4>{_escape_html(colour_key)}</h4>{rows}</div>'
        )

    extra_head = ""
    extra_scripts = ""
    if use_cluster:
        extra_head = (
            f'<link rel="stylesheet" href="{CLUSTER_CSS}"/>\n'
            f'<link rel="stylesheet" href="{CLUSTER_CSS_DEFAULT}"/>'
        )
        extra_scripts = f'<script src="{CLUSTER_JS}"></script>'

    script = _render(
        _MAP_SCRIPT,
        {
            "GEOJSON": json.dumps(
                {"type": "FeatureCollection", "features": features}, ensure_ascii=False
            ),
            "COLOURS": json.dumps(colours),
            "COLOUR_BY": json.dumps(colour_key or None),
            "CLUSTER": json.dumps(use_cluster),
            "ZOOM": json.dumps(int(zoom)),
            "CENTER": json.dumps(list(center or _auto_center(features))),
            "TOOLTIP_FIELD": json.dumps(tooltip_field),
            "POPUP_FIELDS": json.dumps(list(popup_fields) if popup_fields else None),
            "BASEMAPS": json.dumps(_basemap_config(basemap)),
        },
    )

    _write(output_path, _page(title, "\n".join(panels), script, extra_head, extra_scripts))
    if not quiet:
        print(f"✅  Map saved → {output_path}  ({len(features)} features)")
    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# Heatmap
# ─────────────────────────────────────────────────────────────────────────────

_HEAT_SCRIPT = """
document.addEventListener('DOMContentLoaded', function () {
  try {
    if (typeof L === 'undefined') { throw new Error('Leaflet failed to load from both CDNs.'); }
    if (typeof L.heatLayer !== 'function') {
      throw new Error('The leaflet.heat plugin failed to load. Check your network, ' +
                      'or use geo.map() for a marker map instead.');
    }

    var POINTS = /*__POINTS__*/;
    var ZOOM = /*__ZOOM__*/;
    var CENTER = /*__CENTER__*/;

    var map = L.map('map', { preferCanvas: true });
    qoreBasemaps(map, /*__BASEMAPS__*/);
    L.control.scale({ imperial: false }).addTo(map);

    L.heatLayer(POINTS, {
      radius: /*__RADIUS__*/, blur: /*__BLUR__*/, maxZoom: 12, minOpacity: 0.25,
      gradient: { 0.1: '#00416A', 0.3: '#007A64', 0.6: '#00D4AA', 0.85: '#FFFFFF', 1: '#FFD700' }
    }).addTo(map);

    var countEl = document.getElementById('n');
    if (countEl) { countEl.textContent = POINTS.length; }

    if (POINTS.length > 0) {
      var lats = POINTS.map(function (p) { return p[0]; });
      var lngs = POINTS.map(function (p) { return p[1]; });
      map.fitBounds([[Math.min.apply(null, lats), Math.min.apply(null, lngs)],
                     [Math.max.apply(null, lats), Math.max.apply(null, lngs)]]);
    } else {
      map.setView(CENTER, ZOOM);
    }
  } catch (err) {
    console.error('QOREgeo:', err);
    qoreFail(err.message);
  }
});
"""


def build_heatmap(
    features: Sequence[Feature],
    output_path: str,
    title: str = "QOREgeo Heatmap",
    intensity_col: Optional[str] = None,
    zoom: int = 5,
    center: Optional[Coord] = None,
    basemap: str = DEFAULT_BASEMAP,
    radius: int = 25,
    blur: int = 18,
    quiet: bool = False,
) -> str:
    """
    Write a density heatmap.

    ``intensity_col`` weights each point by a property (sales, population,
    incident severity); values are normalised to 0–1 so the gradient always
    uses its full range.

    Parameters
    ----------
    radius : heat radius in pixels — raise it for sparse data
    blur   : blur radius in pixels
    """
    from .geometry import centroid_of

    features = list(features)
    points: List[List[float]] = []
    max_intensity = 0.0

    for feat in features:
        geom = feat.get("geometry") or {}
        if geom.get("type") == "Point":
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                continue
            lat, lng = float(coords[1]), float(coords[0])
        else:
            try:
                lat, lng = centroid_of(geom)
            except Exception:
                continue

        intensity = 1.0
        if intensity_col:
            raw = feat.get("properties", {}).get(intensity_col)
            try:
                intensity = float(raw)
            except (TypeError, ValueError):
                intensity = 1.0

        max_intensity = max(max_intensity, intensity)
        points.append([lat, lng, intensity])

    if max_intensity > 0:
        points = [[p[0], p[1], round(p[2] / max_intensity, 6)] for p in points]

    script = _render(
        _HEAT_SCRIPT,
        {
            "POINTS": json.dumps(points),
            "ZOOM": json.dumps(int(zoom)),
            "CENTER": json.dumps(list(center or _auto_center(features))),
            "BASEMAPS": json.dumps(_basemap_config(basemap)),
            "RADIUS": json.dumps(int(radius)),
            "BLUR": json.dumps(int(blur)),
        },
    )

    panels = '<div id="count" class="q-panel"><span id="n">0</span> points</div>'
    extra_scripts = (
        f'<script src="{HEAT_JS}"></script>\n'
        "<script>\n"
        "  if (typeof L !== 'undefined' && typeof L.heatLayer === 'undefined') {\n"
        f"    document.write('<script src=\"{HEAT_JS_FALLBACK}\"><\\/script>');\n"
        "  }\n"
        "</script>"
    )

    _write(output_path, _page(title, panels, script, "", extra_scripts))
    if not quiet:
        print(f"✅  Heatmap saved → {output_path}  ({len(points)} points)")
    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# Choropleth
# ─────────────────────────────────────────────────────────────────────────────

_CHORO_SCRIPT = """
document.addEventListener('DOMContentLoaded', function () {
  try {
    if (typeof L === 'undefined') { throw new Error('Leaflet failed to load from both CDNs.'); }

    var DATA = /*__GEOJSON__*/;
    var BREAKS = /*__BREAKS__*/;
    var RAMP = /*__RAMP__*/;
    var VALUE_COL = /*__VALUE_COL__*/;
    var LABEL_COL = /*__LABEL_COL__*/;
    var ZOOM = /*__ZOOM__*/;
    var CENTER = /*__CENTER__*/;

    var map = L.map('map', { preferCanvas: true });
    qoreBasemaps(map, /*__BASEMAPS__*/);
    L.control.scale({ imperial: false }).addTo(map);

    function shade(value) {
      if (value === null || value === undefined || isNaN(value)) { return '#3a3f52'; }
      for (var i = BREAKS.length - 1; i >= 0; i--) {
        if (value >= BREAKS[i]) { return RAMP[Math.min(i, RAMP.length - 1)]; }
      }
      return RAMP[0];
    }

    var layer = L.geoJSON(DATA, {
      style: function (feature) {
        var value = Number((feature.properties || {})[VALUE_COL]);
        return {
          fillColor: shade(value), fillOpacity: 0.78,
          color: 'rgba(255,255,255,.45)', weight: 1
        };
      },
      pointToLayer: function (feature, latlng) {
        var value = Number((feature.properties || {})[VALUE_COL]);
        return L.circleMarker(latlng, {
          radius: 8, fillColor: shade(value), fillOpacity: 0.9,
          color: 'rgba(255,255,255,.6)', weight: 1
        });
      },
      onEachFeature: function (feature, lyr) {
        var props = feature.properties || {};
        var name = LABEL_COL && props[LABEL_COL] !== undefined ? props[LABEL_COL] : '';
        lyr.bindPopup(
          '<div class="q-pop"><b>' + name + '</b><br>' +
          VALUE_COL + ': <b>' + props[VALUE_COL] + '</b></div>'
        );
        lyr.on('mouseover', function () { if (lyr.setStyle) { lyr.setStyle({ weight: 3 }); } });
        lyr.on('mouseout', function () { if (lyr.setStyle) { lyr.setStyle({ weight: 1 }); } });
      }
    }).addTo(map);

    var countEl = document.getElementById('n');
    if (countEl) { countEl.textContent = (DATA.features || []).length; }

    var bounds = layer.getBounds();
    if (bounds && bounds.isValid()) { map.fitBounds(bounds.pad(0.1)); }
    else { map.setView(CENTER, ZOOM); }
  } catch (err) {
    console.error('QOREgeo:', err);
    qoreFail(err.message);
  }
});
"""


def build_choropleth(
    features: Sequence[Feature],
    output_path: str,
    value_col: str,
    title: str = "QOREgeo Choropleth",
    label_col: Optional[str] = None,
    bins: int = 5,
    zoom: int = 5,
    center: Optional[Coord] = None,
    basemap: str = DEFAULT_BASEMAP,
    quiet: bool = False,
) -> str:
    """
    Write a choropleth — features shaded by a numeric property.

    Class breaks use quantiles rather than equal intervals, so each colour
    carries roughly the same number of features. Equal intervals collapse to
    one colour whenever the data is skewed, which spatial data almost always is.

    Parameters
    ----------
    value_col : numeric property driving the colour
    label_col : property shown as the popup heading
    bins      : number of classes (2–7)
    """
    features = list(features)
    values = []
    for feat in features:
        try:
            values.append(float(feat.get("properties", {}).get(value_col)))
        except (TypeError, ValueError):
            continue

    bins = max(2, min(7, int(bins)))
    breaks = _quantile_breaks(values, bins)
    # Tied data can yield fewer distinct breaks than requested; shrink the ramp
    # to match so the legend never shows an empty "5 – 5" class.
    ramp = _sample_ramp(SEQUENTIAL_RAMP, len(breaks))

    legend_rows = []
    for i, colour in enumerate(ramp):
        low = breaks[i]
        high = breaks[i + 1] if i + 1 < len(breaks) else (max(values) if values else low)
        legend_rows.append(
            f'<div class="row"><span class="swatch" style="background:{colour}"></span>'
            f"<span>{_fmt_number(low)} – {_fmt_number(high)}</span></div>"
        )

    panels = (
        '<div id="count" class="q-panel"><span id="n">0</span> features</div>'
        f'<div id="legend" class="q-panel"><h4>{_escape_html(value_col)}</h4>'
        + "".join(legend_rows)
        + "</div>"
    )

    script = _render(
        _CHORO_SCRIPT,
        {
            "GEOJSON": json.dumps(
                {"type": "FeatureCollection", "features": features}, ensure_ascii=False
            ),
            "BREAKS": json.dumps(breaks),
            "RAMP": json.dumps(ramp),
            "VALUE_COL": json.dumps(value_col),
            "LABEL_COL": json.dumps(label_col),
            "ZOOM": json.dumps(int(zoom)),
            "CENTER": json.dumps(list(center or _auto_center(features))),
            "BASEMAPS": json.dumps(_basemap_config(basemap)),
        },
    )

    _write(output_path, _page(title, panels, script))
    if not quiet:
        print(f"✅  Choropleth saved → {output_path}  ({len(features)} features)")
    return output_path


def _quantile_breaks(values: Sequence[float], bins: int) -> List[float]:
    """
    Lower bound of each quantile class, strictly ascending.

    Ties are dropped rather than repeated: a column where half the rows share
    one value simply gets fewer classes, instead of classes that can never
    contain anything.
    """
    if not values:
        return [0.0]

    ordered = sorted(values)
    breaks: List[float] = []
    for i in range(bins):
        index = int(i * (len(ordered) - 1) / bins)
        value = round(ordered[index], 6)
        if not breaks or value > breaks[-1]:
            breaks.append(value)
    return breaks


def _sample_ramp(ramp: Sequence[str], count: int) -> List[str]:
    """Pick ``count`` evenly spaced colours from a ramp, keeping both ends."""
    count = max(1, min(len(ramp), count))
    if count == 1:
        return [ramp[-1]]
    step = (len(ramp) - 1) / (count - 1)
    return [ramp[int(round(i * step))] for i in range(count)]


def _fmt_number(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}k"
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}"
