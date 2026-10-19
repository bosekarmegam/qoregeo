"""
qoregeo.map_builder
===================
Generates standalone HTML files containing Leaflet.js interactive maps.
No server required — just open in a browser.

Safety
------
* The title is HTML-escaped and all data is embedded as JSON with ``<``,
  ``>``, ``&`` and U+2028/U+2029 escaped, so property values can never
  break out of the ``<script>`` block.
* Popups are built with DOM ``textContent`` — property keys and values are
  never interpreted as HTML.
* Leaflet and leaflet.heat are loaded with Subresource Integrity hashes and
  fall back to a second CDN if the first one is unreachable.
"""

from __future__ import annotations

import datetime
import decimal
import html
import json
import math
import os
import re
import warnings
from typing import Any
from urllib.parse import quote

from ._version import __version__
from .exceptions import InvalidArgumentError
from .utils import _representative_point

Feature = dict[str, Any]
Coord   = tuple[float, float]


# ─────────────────────────────────────────────────────────────────────────────
# Assets (versions pinned, SRI hashes computed from the npm-published files)
# ─────────────────────────────────────────────────────────────────────────────

_LEAFLET_CSS: dict[str, Any] = {
    "urls": [
        "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
        "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css",
    ],
    "integrity": "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=",
}
_LEAFLET_JS: dict[str, Any] = {
    "urls": [
        "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
        "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js",
    ],
    "integrity": "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=",
}
_LEAFLET_HEAT_JS: dict[str, Any] = {
    "urls": [
        "https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js",
        "https://cdn.jsdelivr.net/npm/leaflet.heat@0.2.0/dist/leaflet-heat.js",
    ],
    "integrity": "sha256-65UqrlgGoRAnKfKRuriH3eeDrOhZgZo1SCenduc+SGo=",
}

_OSM_ATTR = (
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
)
_CARTO_ATTR = _OSM_ATTR + ', &copy; <a href="https://carto.com/attribution/">CARTO</a>'

# Environment variable consulted when no tile_api_key is passed.
CARTO_KEY_ENV = "QOREGEO_CARTO_API_KEY"
_CARTO_KEY_URL = "https://carto.com/basemaps/apikey/"

_TILES: dict[str, dict[str, Any]] = {
    "dark": {
        "url": "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        "options": {"attribution": _CARTO_ATTR, "subdomains": "abcd", "maxZoom": 20},
        "carto": True,
    },
    "light": {
        "url": "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        "options": {"attribution": _CARTO_ATTR, "subdomains": "abcd", "maxZoom": 20},
        "carto": True,
    },
    "voyager": {
        "url": "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
        "options": {"attribution": _CARTO_ATTR, "subdomains": "abcd", "maxZoom": 20},
        "carto": True,
    },
    "osm": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "options": {"attribution": _OSM_ATTR, "maxZoom": 19},
        "carto": False,
    },
}

_STYLE = {
    "point": {
        "radius": 6, "color": "#FFFFFF", "weight": 1.5, "opacity": 0.8,
        "fillColor": "#00D4AA", "fillOpacity": 0.9,
    },
    "shape": {"color": "#00D4AA", "weight": 2, "opacity": 0.9, "fillColor": "#00D4AA", "fillOpacity": 0.15},
    "overlay": {"color": "#FFD700", "weight": 2, "dashArray": "6 4", "fillColor": "#FFD700", "fillOpacity": 0.06},
}

_DEFAULT_FIT_MAX_ZOOM = 15
_DEFAULT_CENTER_ZOOM = 10


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _json_default(o: Any) -> Any:
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=str)
    if isinstance(o, (datetime.date, datetime.time)):
        return o.isoformat()
    if isinstance(o, decimal.Decimal):
        return float(o)
    return str(o)


_JS_ESCAPES = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    " ": "\\u2028",
    " ": "\\u2029",
}


def _json_safe(obj: Any) -> Any:
    """Recursively replace NaN / ±Infinity (not valid JSON) with None."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _to_js(obj: Any) -> str:
    """Serialise ``obj`` as a JS literal that is safe inside an inline <script>."""
    text = json.dumps(_json_safe(obj), ensure_ascii=False, default=_json_default, allow_nan=False)
    return re.sub("[<>&  ]", lambda m: _JS_ESCAPES[m.group(0)], text)


def _render(template: str, values: dict[str, str]) -> str:
    """Single-pass ``__NAME__`` substitution (inserted text is never re-scanned)."""
    return re.sub(r"__([A-Z_]+)__", lambda m: values.get(m.group(1), m.group(0)), template)


def _tiles_config(
    tiles: str,
    api_key: str | None = None,
    attribution: str | None = None,
) -> dict[str, Any]:
    """
    Resolve a basemap name (or a custom ``https://…/{z}/{x}/{y}…`` URL
    template) to a Leaflet tileLayer config.

    CARTO basemaps require a free per-user API key since August 2026; it is
    taken from ``api_key`` or the QOREGEO_CARTO_API_KEY environment variable.
    Without one CARTO serves tiles with an "API key required" watermark.
    """
    if isinstance(tiles, str) and tiles.strip().lower().startswith(("https://", "http://")):
        url = tiles.strip()
        if not all(token in url for token in ("{z}", "{x}", "{y}")):
            raise InvalidArgumentError(
                "tiles", tiles, "a URL template containing {z}, {x} and {y}",
                "geo.map('out.html', tiles='https://tile.example.com/{z}/{x}/{y}.png', "
                "attribution='&copy; Example')",
            )
        options: dict[str, Any] = {"maxZoom": 19, "attribution": attribution or ""}
        if "{s}" in url:
            options["subdomains"] = "abc"
        return {"url": url, "options": options}

    name = tiles.strip().lower() if isinstance(tiles, str) else tiles
    if name not in _TILES:
        raise InvalidArgumentError(
            "tiles", tiles,
            " | ".join(repr(k) for k in _TILES) + " or a custom https:// URL template",
            "geo.map('out.html', tiles='light')",
        )
    preset = _TILES[name]
    config = {"url": preset["url"], "options": dict(preset["options"])}
    if attribution:
        config["options"]["attribution"] = attribution

    if preset["carto"]:
        key = api_key or os.environ.get(CARTO_KEY_ENV, "").strip()
        if key:
            config["url"] += "?key=" + quote(key, safe="")
        else:
            warnings.warn(
                f"QOREgeo: CARTO basemaps ('{name}') need a free API key since August 2026 — "
                f"without one the tiles show an 'API key required' watermark. Get a key at "
                f"{_CARTO_KEY_URL} and pass tile_api_key='…' (or set {CARTO_KEY_ENV}), "
                f"or use tiles='osm'.",
                stacklevel=4,
            )
    return config


def _view_config(zoom: int | None, center: Coord | None) -> dict[str, Any]:
    return {
        "center": list(center) if center is not None else None,
        "zoom": zoom if zoom is not None else _DEFAULT_CENTER_ZOOM,
        "fitMaxZoom": zoom if zoom is not None else _DEFAULT_FIT_MAX_ZOOM,
    }


def _write(path: str, content: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _announce(message: str, verbose: bool) -> None:
    """Print a status line without crashing on consoles that can't show emoji."""
    if not verbose:
        return
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.replace("✅", "[OK]").encode("ascii", "replace").decode("ascii"))


# ─────────────────────────────────────────────────────────────────────────────
# Page shell
# ─────────────────────────────────────────────────────────────────────────────

_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="generator" content="QOREgeo __VERSION__">
<title>__TITLE__</title>
<link rel="stylesheet" href="__CSS_URL__" integrity="__CSS_SRI__" crossorigin="anonymous"
      onerror="this.onerror=null;this.href='__CSS_FALLBACK__';">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#0f111a; color:#fff; }
  #map { width:100vw; height:100vh; }
  .qg-panel {
    position:fixed; z-index:1000;
    background:rgba(0,0,0,.7); backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px);
    border:1px solid rgba(0,212,170,.25); border-radius:10px;
    font-size:13px; color:rgba(255,255,255,.75);
  }
  #brand { bottom:24px; left:16px; padding:10px 16px; max-width:calc(100vw - 32px);
           overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #brand strong { color:#00D4AA; font-size:14px; }
  #count { top:16px; right:16px; padding:8px 16px; }
  #count span { color:#00D4AA; font-weight:600; }
  #qg-error {
    position:fixed; top:50%; left:50%; transform:translate(-50%,-50%); z-index:2000;
    max-width:min(90vw, 480px); background:#e74c3c; color:#fff; padding:20px; border-radius:8px;
    box-shadow:0 4px 12px rgba(0,0,0,.5); font-size:14px; line-height:1.4;
  }
  #qg-error b { display:block; font-size:16px; margin-bottom:8px; }
  .qg-popup { font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:12px; min-width:140px; color:#222; }
  .qg-popup td { padding:2px 0; vertical-align:top; word-break:break-word; }
  .qg-popup td.qg-k { padding-right:8px; color:#6b6b7b; font-weight:600; white-space:nowrap; }
  .qg-empty { color:#6b6b7b; }
</style>
</head>
<body>
<div id="map" role="region" aria-label="__TITLE__"></div>
<div id="brand" class="qg-panel"><strong>QOREgeo</strong> · __TITLE__</div>
__EXTRA_HTML__
<script>
(function () {
  "use strict";
  var CONFIG = __CONFIG__;

  function showError(message) {
    var box = document.createElement("div");
    box.id = "qg-error";
    var head = document.createElement("b");
    head.textContent = "Map Loading Error";
    var body = document.createElement("div");
    body.textContent = message;
    box.appendChild(head);
    box.appendChild(body);
    document.body.appendChild(box);
  }

  function loadScript(asset) {
    return new Promise(function (resolve, reject) {
      var i = 0;
      (function next() {
        if (i >= asset.urls.length) {
          reject(new Error("Could not load " + asset.urls[0] + " (are you offline?)"));
          return;
        }
        var s = document.createElement("script");
        s.src = asset.urls[i++];
        s.integrity = asset.integrity;
        s.crossOrigin = "anonymous";
        s.onload = resolve;
        s.onerror = function () { s.remove(); next(); };
        document.head.appendChild(s);
      })();
    });
  }

  CONFIG.scripts.reduce(function (chain, asset) {
    return chain.then(function () { return loadScript(asset); });
  }, Promise.resolve()).then(function () {
    if (typeof L === "undefined") throw new Error("Leaflet failed to load from CDNs");
    init(L);
  }).catch(function (err) {
    console.error("QOREgeo Error:", err);
    showError(err && err.message ? err.message : String(err));
  });

  function baseMap(L) {
    var map = L.map("map", { zoomControl: true, preferCanvas: true, worldCopyJump: true });
    L.tileLayer(CONFIG.tiles.url, CONFIG.tiles.options).addTo(map);
    return map;
  }

  function setView(map, bounds) {
    var v = CONFIG.view;
    if (v.center) map.setView(v.center, v.zoom);
    else if (bounds && bounds.isValid()) map.fitBounds(bounds.pad(0.1), { maxZoom: v.fitMaxZoom });
    else map.setView([20, 0], 2);
  }

__INIT__
})();
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Interactive feature map
# ─────────────────────────────────────────────────────────────────────────────

_MAP_INIT = """\
  function cellText(v) {
    if (v === null || v === undefined) return "";
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
  }

  function popupFor(props) {
    var wrap = document.createElement("div");
    wrap.className = "qg-popup";
    var keys = Object.keys(props || {}).filter(function (k) { return k.charAt(0) !== "_"; });
    if (!keys.length) {
      wrap.className += " qg-empty";
      wrap.textContent = "No properties";
      return wrap;
    }
    var table = document.createElement("table");
    keys.forEach(function (k) {
      var row = table.insertRow();
      var kc = row.insertCell();
      kc.className = "qg-k";
      kc.textContent = k;
      row.insertCell().textContent = cellText(props[k]);
    });
    wrap.appendChild(table);
    return wrap;
  }

  function init(L) {
    var map = baseMap(L);
    var count = 0;
    var layer = L.geoJSON(CONFIG.data, {
      pointToLayer: function (f, latlng) { return L.circleMarker(latlng, CONFIG.style.point); },
      style: function () { return CONFIG.style.shape; },
      onEachFeature: function (f, l) {
        count++;
        l.bindPopup(function () { return popupFor(f.properties); }, { maxWidth: 320 });
      }
    }).addTo(map);

    var bounds = layer.getBounds();
    if (CONFIG.overlays.length) {
      var overlay = L.geoJSON(CONFIG.overlays, {
        style: function () { return CONFIG.style.overlay; },
        pointToLayer: function (f, latlng) { return L.circleMarker(latlng, CONFIG.style.point); },
        interactive: false
      }).addTo(map);
      bounds.extend(overlay.getBounds());
    }

    document.getElementById("n").textContent = count;
    setView(map, bounds);
    window.qoregeo = { map: map, layer: layer, count: count };
  }
"""


def build_map(
    features: list[Feature],
    output_path: str,
    title: str = "QOREgeo Map",
    zoom: int | None = None,
    center: Coord | None = None,
    tiles: str = "dark",
    overlays: list[dict[str, Any]] | None = None,
    verbose: bool = True,
    tile_api_key: str | None = None,
    attribution: str | None = None,
) -> None:
    """Write an interactive Leaflet map of ``features`` to ``output_path``."""
    overlay_list = []
    for i, shape in enumerate(overlays or []):
        if not isinstance(shape, dict) or "type" not in shape:
            raise InvalidArgumentError(
                f"overlays[{i}]", shape, "a GeoJSON geometry / Feature dict (e.g. a geo.buffer() result)",
                "geo.map('out.html', overlays=[geo.buffer((28.61, 77.20), 10)])",
            )
        overlay_list.append(shape)

    config = {
        "data": {"type": "FeatureCollection", "features": features},
        "overlays": overlay_list,
        "tiles": _tiles_config(tiles, tile_api_key, attribution),
        "view": _view_config(zoom, center),
        "style": _STYLE,
        "scripts": [_LEAFLET_JS],
    }
    page = _render(_PAGE, {
        "VERSION": __version__,
        "TITLE": html.escape(str(title)),
        "CSS_URL": _LEAFLET_CSS["urls"][0],
        "CSS_FALLBACK": _LEAFLET_CSS["urls"][1],
        "CSS_SRI": _LEAFLET_CSS["integrity"],
        "EXTRA_HTML": '<div id="count" class="qg-panel"><span id="n">0</span> features</div>',
        "CONFIG": _to_js(config),
        "INIT": _MAP_INIT,
    })
    _write(output_path, page)
    _announce(f"✅  Map saved → {output_path}  ({len(features)} features)", verbose)


# ─────────────────────────────────────────────────────────────────────────────
# Heatmap
# ─────────────────────────────────────────────────────────────────────────────

_HEAT_INIT = """\
  function init(L) {
    if (typeof L.heatLayer !== "function") throw new Error("leaflet.heat failed to load from CDNs");
    var map = baseMap(L);
    L.heatLayer(CONFIG.points, CONFIG.heat).addTo(map);
    var bounds = CONFIG.bounds ? L.latLngBounds(CONFIG.bounds) : null;
    setView(map, bounds);
    window.qoregeo = { map: map, count: CONFIG.points.length };
  }
"""

_HEAT_GRADIENT = {0.1: "#00416A", 0.3: "#007A64", 0.6: "#00D4AA", 0.85: "#FFFFFF", 1.0: "#FFD700"}


def _heat_points(features: list[Feature], intensity_col: str | None) -> list[list[float]]:
    """Return ``[lat, lng, weight]`` triples with weights normalised to (0, 1]."""
    points: list[list[float]] = []
    for feat in features:
        loc = _representative_point(feat.get("geometry"))
        if loc is None:
            continue
        weight = 1.0
        if intensity_col:
            raw: Any = (feat.get("properties") or {}).get(intensity_col)
            try:
                weight = float(raw) if not isinstance(raw, bool) else float("nan")
            except (TypeError, ValueError):
                weight = float("nan")
            if not math.isfinite(weight) or weight <= 0:
                continue   # no (or negative) weight contributes no heat
        points.append([loc[0], loc[1], weight])

    max_w = max((p[2] for p in points), default=0.0)
    if max_w > 0:
        for p in points:
            p[2] = p[2] / max_w
    return points


def build_heatmap(
    features: list[Feature],
    output_path: str,
    title: str = "QOREgeo Heatmap",
    intensity_col: str | None = None,
    zoom: int | None = None,
    center: Coord | None = None,
    tiles: str = "dark",
    radius: int = 25,
    blur: int = 18,
    verbose: bool = True,
    tile_api_key: str | None = None,
    attribution: str | None = None,
) -> None:
    """Write a Leaflet heatmap of ``features`` to ``output_path``."""
    points = _heat_points(features, intensity_col)
    bounds = None
    if points:
        lats = [p[0] for p in points]
        lngs = [p[1] for p in points]
        bounds = [[min(lats), min(lngs)], [max(lats), max(lngs)]]

    config = {
        "points": points,
        "bounds": bounds,
        "heat": {
            "radius": radius, "blur": blur, "maxZoom": 12, "max": 1.0,
            "gradient": _HEAT_GRADIENT,
        },
        "tiles": _tiles_config(tiles, tile_api_key, attribution),
        "view": _view_config(zoom, center),
        "scripts": [_LEAFLET_JS, _LEAFLET_HEAT_JS],
    }
    page = _render(_PAGE, {
        "VERSION": __version__,
        "TITLE": html.escape(str(title)),
        "CSS_URL": _LEAFLET_CSS["urls"][0],
        "CSS_FALLBACK": _LEAFLET_CSS["urls"][1],
        "CSS_SRI": _LEAFLET_CSS["integrity"],
        "EXTRA_HTML": "",
        "CONFIG": _to_js(config),
        "INIT": _HEAT_INIT,
    })
    _write(output_path, page)
    _announce(f"✅  Heatmap saved → {output_path}  ({len(points)} points)", verbose)
