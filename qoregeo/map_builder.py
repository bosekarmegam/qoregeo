"""
qoregeo.map_builder
===================
Generates standalone HTML files containing Leaflet.js interactive maps.
No server required — just open in a browser.

Safety
------
* The title is HTML-escaped and all data is embedded as JSON with ``<``,
  ``>``, ``&``, U+2028/U+2029 and lone surrogates escaped, so property
  values can never break out of the ``<script>`` block.
* Popups are built with DOM ``textContent`` — property keys and values are
  never interpreted as HTML.
* Leaflet and leaflet.heat are loaded with Subresource Integrity hashes and
  fall back to a second CDN if the first one is unreachable.
* Files are written atomically, so a failure never truncates an existing map.
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import sys
import warnings
from typing import Any
from urllib.parse import quote

from ._version import __version__
from .exceptions import InvalidArgumentError
from .utils import (
    GEOMETRY_TYPES,
    _atomic_write,
    _copy_geometry,
    _escape_surrogates,
    _geometry_problem,
    _iter_positions,
    _json_safe,
    _lng_span,
    _normalize_lng,
    _representative_point,
)

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
        # OSM's tile policy requires a Referer; this sends the page's origin
        # when the map is served over http(s).
        "options": {"attribution": _OSM_ATTR, "maxZoom": 19,
                    "referrerPolicy": "strict-origin-when-cross-origin"},
        "carto": False,
    },
}

# Placeholders Leaflet's TileLayer understands in a URL template.
_TILE_PLACEHOLDERS = {"s", "z", "x", "y", "-y", "r"}
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")

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

_JS_ESCAPES = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    " ": "\\u2028",
    " ": "\\u2029",
}


def _to_js(obj: Any) -> str:
    """
    Serialise ``obj`` as a JS literal that is safe inside an inline <script>.

    Values are made JSON-safe first (NaN → null, sets → lists, …, and
    integers beyond 2**53 → strings so no digits are lost in the browser).
    """
    text = json.dumps(_json_safe(obj, js=True), ensure_ascii=False, allow_nan=False)
    text = _escape_surrogates(text)
    return re.sub("[<>&  ]", lambda m: _JS_ESCAPES[m.group(0)], text)


def _render(template: str, values: dict[str, str]) -> str:
    """Single-pass ``__NAME__`` substitution (inserted text is never re-scanned)."""
    return re.sub(r"__([A-Z_]+)__", lambda m: values.get(m.group(1), m.group(0)), template)


def _html_text(text: Any) -> str:
    """HTML-escape ``text``, replacing characters that cannot be encoded as UTF-8."""
    return html.escape(str(text).encode("utf-8", "replace").decode("utf-8"))


def _tiles_config(
    tiles: Any,
    api_key: str | None = None,
    attribution: str | None = None,
) -> dict[str, Any]:
    """
    Resolve a basemap name (or a custom ``https://…/{z}/{x}/{y}…`` URL
    template) to a Leaflet tileLayer config.

    CARTO basemaps require a free per-user API key since August 2026; it is
    taken from ``api_key`` or the QOREGEO_CARTO_API_KEY environment variable.
    Without one CARTO serves tiles with an "API key required" watermark.
    ``attribution`` is trusted HTML; for presets it is appended to the
    required OpenStreetMap / CARTO credit.
    """
    presets = " | ".join(repr(k) for k in _TILES)
    for name, value in (("tile_api_key", api_key), ("attribution", attribution)):
        if value is not None and not isinstance(value, str):
            raise InvalidArgumentError(name, value, "a string or None",
                                       f"geo.map('out.html', {name}='…')")
    if not isinstance(tiles, str):
        raise InvalidArgumentError(
            "tiles", tiles, f"{presets} or a custom https:// URL template",
            "geo.map('out.html', tiles='light')",
        )

    if tiles.strip().lower().startswith(("https://", "http://")):
        url = tiles.strip()
        names = set(_PLACEHOLDER_RE.findall(url))
        unknown = sorted(names - _TILE_PLACEHOLDERS)
        if not {"z", "x"} <= names or not names & {"y", "-y"} or unknown:
            problem = (f"unknown placeholder(s) {', '.join('{' + n + '}' for n in unknown)} — "
                       "substitute their values into the URL" if unknown
                       else "a URL template containing {z}, {x} and {y} (or {-y})")
            raise InvalidArgumentError(
                "tiles", tiles, problem,
                "geo.map('out.html', tiles='https://tile.example.com/{z}/{x}/{y}.png', "
                "attribution='&copy; Example')",
            )
        options: dict[str, Any] = {"maxZoom": 19, "attribution": attribution or ""}
        if "s" in names:
            options["subdomains"] = "abcd" if ".basemaps.cartocdn.com/" in url else "abc"
        if ".basemaps.cartocdn.com/" in url and "key=" not in url:
            # Other CARTO styles (e.g. light_nolabels) need the same key.
            key = (api_key or os.environ.get(CARTO_KEY_ENV, "")).strip()
            if key:
                url += ("&" if "?" in url else "?") + "key=" + quote(key, safe="")
        elif api_key:
            warnings.warn(
                "QOREgeo: tile_api_key is only added to CARTO tile URLs; put your provider's "
                "key in the tiles= URL template instead.",
                stacklevel=4,
            )
        return {"url": url, "options": options}

    name = tiles.strip().lower()
    if name not in _TILES:
        raise InvalidArgumentError(
            "tiles", tiles, f"{presets} or a custom https:// URL template",
            "geo.map('out.html', tiles='light')",
        )
    preset = _TILES[name]
    config = {"url": preset["url"], "options": dict(preset["options"])}
    if attribution:
        config["options"]["attribution"] += ", " + attribution

    if preset["carto"]:
        key = (api_key or os.environ.get(CARTO_KEY_ENV, "")).strip()
        if key:
            config["url"] += "?key=" + quote(key, safe="")
        else:
            warnings.warn(
                f"QOREgeo: CARTO basemaps ('{name}') need a free API key since August 2026 — "
                f"without one the tiles show an 'API key required' watermark. Get a key at "
                f"{_CARTO_KEY_URL} and pass tile_api_key='…' (or set {CARTO_KEY_ENV}), or pass "
                f"your own tiles='https://…/{{z}}/{{x}}/{{y}}.png'. (tiles='osm' also works, but "
                f"only when the page is served over http(s): OpenStreetMap rejects tile requests "
                f"from local files.)",
                stacklevel=4,
            )
    return config


def _view_config(zoom: int | None, center: Coord | None) -> dict[str, Any]:
    return {
        "center": list(center) if center is not None else None,
        "zoom": zoom if zoom is not None else _DEFAULT_CENTER_ZOOM,
        "fitMaxZoom": zoom if zoom is not None else _DEFAULT_FIT_MAX_ZOOM,
    }


def _antimeridian_west(lngs: list[float]) -> float | None:
    """
    If the tightest longitude span of ``lngs`` crosses ±180°, return its
    western edge (longitudes below it belong east of 180°); else None.
    """
    if not lngs:
        return None
    west, east = _lng_span(lngs)
    return west if west > east else None


def _unwrap_lng(lng: float, west: float) -> float:
    """Place ``lng`` on the continuous range that starts at ``west``."""
    n = _normalize_lng(lng)
    return n + 360.0 if n < west else n


def _shift_geometry(geometry: Any, west: float) -> Any:
    """Copy of ``geometry`` with every longitude placed on the range starting at ``west``."""
    def shift(c: Any) -> Any:
        if isinstance(c, list) and len(c) >= 2 and not isinstance(c[0], list):
            return [_unwrap_lng(c[0], west), *c[1:]]
        return [shift(x) for x in c] if isinstance(c, list) else c

    out = _copy_geometry(geometry)
    if isinstance(out, dict):
        if "coordinates" in out:
            out["coordinates"] = shift(out["coordinates"])
        if isinstance(out.get("geometries"), list):
            out["geometries"] = [_shift_geometry(g, west) for g in out["geometries"]]
    return out


def _overlay_geometries(shape: dict[str, Any]) -> list[Any]:
    """The geometries inside an overlay (geometry, Feature or FeatureCollection)."""
    stype = shape.get("type")
    if stype == "FeatureCollection":
        return [g for f in shape.get("features") or [] for g in _overlay_geometries(f)]
    if stype == "Feature":
        return [shape.get("geometry")]
    return [shape]


def _overlay_problem(shape: Any) -> str | None:
    """Why ``shape`` cannot be drawn as an overlay (None if it can)."""
    if not isinstance(shape, dict):
        return f"a {type(shape).__name__}, not a GeoJSON dict"
    stype = shape.get("type")
    if stype == "FeatureCollection":
        feats = shape.get("features")
        if not isinstance(feats, list):
            return "FeatureCollection 'features' must be a list"
        for f in feats:
            problem = _overlay_problem(f)
            if problem:
                return problem
        return None
    if stype == "Feature":
        geom = shape.get("geometry")
        return None if geom is None else _geometry_problem(geom)
    if stype in GEOMETRY_TYPES:
        return _geometry_problem(shape)
    return f"unknown GeoJSON type {stype!r}"


def _write(path: str, content: str) -> None:
    _atomic_write(path, content, encoding="utf-8")


def _announce(message: str, verbose: bool) -> None:
    """Print a status line without crashing on consoles that can't show emoji."""
    if not verbose:
        return
    try:
        print(message)
    except UnicodeEncodeError:
        fallback = message.replace("✅", "[OK]").replace("→", "->")
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            fallback = fallback.encode(encoding, "replace").decode(encoding, "replace")
        except LookupError:
            fallback = fallback.encode("ascii", "replace").decode("ascii")
        print(fallback)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


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
          reject(new Error("Could not load " + asset.urls.join(" or ") +
                           " (offline, blocked, or failed its integrity check)."));
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
    var map = L.map("map", { zoomControl: true, preferCanvas: true,
                             worldCopyJump: CONFIG.worldCopyJump });
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
    var keys = Object.keys(props || {});
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

  function addEach(layer, items) {
    var failed = 0;
    items.forEach(function (item) {
      try { layer.addData(item); } catch (e) { failed++; }
    });
    if (failed) console.warn("QOREgeo: " + failed + " shape(s) could not be drawn");
  }

  function init(L) {
    var map = baseMap(L);
    var count = 0;
    var layer = L.geoJSON(null, {
      pointToLayer: function (f, latlng) { return L.circleMarker(latlng, CONFIG.style.point); },
      style: function () { return CONFIG.style.shape; },
      onEachFeature: function (f, l) {
        count++;
        l.bindPopup(function () { return popupFor(f.properties); }, { maxWidth: 320 });
      }
    }).addTo(map);
    addEach(layer, CONFIG.data.features);

    var bounds = layer.getBounds();
    if (CONFIG.overlays.length) {
      var overlay = L.geoJSON(null, {
        style: function () { return CONFIG.style.overlay; },
        pointToLayer: function (f, latlng) { return L.circleMarker(latlng, CONFIG.style.point); },
        interactive: false
      }).addTo(map);
      addEach(overlay, CONFIG.overlays);
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
        problem = _overlay_problem(shape)
        if problem:
            raise InvalidArgumentError(
                f"overlays[{i}]", shape,
                f"a GeoJSON geometry / Feature / FeatureCollection dict ({problem})",
                "geo.map('out.html', overlays=[geo.buffer((28.61, 77.20), 10)])",
            )
        overlay_list.append(shape)

    tiles_cfg = _tiles_config(tiles, tile_api_key, attribution)

    # Data spanning the antimeridian is drawn on one continuous longitude
    # range (e.g. 170°…190°) so it opens as one cluster, not a world view.
    lngs = [float(p[0]) for f in features for p in _iter_positions(f.get("geometry"))]
    lngs += [float(p[0]) for s in overlay_list for g in _overlay_geometries(s)
             for p in _iter_positions(g)]
    west = _antimeridian_west(lngs)
    if west is not None:
        features = [{**f, "geometry": _shift_geometry(f.get("geometry"), west)} for f in features]
        overlay_list = [_shift_overlay(s, west) for s in overlay_list]

    config = {
        "data": {"type": "FeatureCollection", "features": features},
        "overlays": overlay_list,
        "tiles": tiles_cfg,
        "view": _view_config(zoom, _view_center(center, west)),
        "worldCopyJump": west is None,
        "style": _STYLE,
        "scripts": [_LEAFLET_JS],
    }
    page = _render(_PAGE, {
        "VERSION": __version__,
        "TITLE": _html_text(title),
        "CSS_URL": _LEAFLET_CSS["urls"][0],
        "CSS_FALLBACK": _LEAFLET_CSS["urls"][1],
        "CSS_SRI": _LEAFLET_CSS["integrity"],
        "EXTRA_HTML": '<div id="count" class="qg-panel"><span id="n">0</span> features</div>',
        "CONFIG": _to_js(config),
        "INIT": _MAP_INIT,
    })
    _write(output_path, page)
    located = sum(1 for f in features if _representative_point(f.get("geometry")) is not None)
    _announce(f"✅  Map saved → {output_path}  ({_plural(located, 'feature')})", verbose)


def _shift_overlay(shape: dict[str, Any], west: float) -> dict[str, Any]:
    stype = shape.get("type")
    if stype == "FeatureCollection":
        return {**shape, "features": [_shift_overlay(f, west) for f in shape.get("features", [])]}
    if stype == "Feature":
        return {**shape, "geometry": _shift_geometry(shape.get("geometry"), west)}
    shifted: dict[str, Any] = _shift_geometry(shape, west)
    return shifted


def _view_center(center: Coord | None, west: float | None) -> Coord | None:
    """Move a user-supplied centre onto the same longitude range as shifted data."""
    if center is None or west is None:
        return center
    return center[0], _unwrap_lng(center[1], west)


# ─────────────────────────────────────────────────────────────────────────────
# Heatmap
# ─────────────────────────────────────────────────────────────────────────────

_HEAT_INIT = """\
  function init(L) {
    if (typeof L.heatLayer !== "function") throw new Error("leaflet.heat failed to load from CDNs");
    var map = baseMap(L);
    var heat = L.heatLayer(CONFIG.points, CONFIG.heat).addTo(map);
    var bounds = CONFIG.bounds ? L.latLngBounds(CONFIG.bounds) : null;
    setView(map, bounds);
    // leaflet.heat fades points below its maxZoom; make the opening view full strength.
    heat.setOptions({ maxZoom: map.getZoom() });
    window.qoregeo = { map: map, heat: heat, count: CONFIG.points.length };
  }
"""

_HEAT_GRADIENT = {0.1: "#00416A", 0.3: "#007A64", 0.6: "#00D4AA", 0.85: "#FFFFFF", 1.0: "#FFD700"}


def _heat_points(
    features: list[Feature],
    intensity_col: str | None,
    dropped: list[Any] | None = None,
) -> list[list[float]]:
    """
    Return ``[lat, lng, weight]`` triples with weights normalised to (0, 1].

    Rows whose intensity is missing, non-numeric, non-finite or <= 0 are left
    out; their raw values are appended to ``dropped`` when it is given.
    """
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
            except (TypeError, ValueError, OverflowError):
                weight = float("nan")
            if not math.isfinite(weight) or weight <= 0:
                if dropped is not None:
                    dropped.append(raw)
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
    tiles_cfg = _tiles_config(tiles, tile_api_key, attribution)
    dropped: list[Any] = []
    points = _heat_points(features, intensity_col, dropped)
    if dropped:
        examples = ", ".join(repr(v) for v in dropped[:3]) + (", …" if len(dropped) > 3 else "")
        warnings.warn(
            f"QOREgeo: {_plural(len(dropped), 'row')} with a missing, non-numeric or "
            f"non-positive '{intensity_col}' value ({examples}) "
            f"{'adds' if len(dropped) == 1 else 'add'} no heat"
            + (" — the heatmap is empty." if not points else "."),
            stacklevel=3,
        )

    west = _antimeridian_west([p[1] for p in points])
    if west is not None:
        for p in points:
            p[1] = _unwrap_lng(p[1], west)

    bounds = None
    if points:
        lats = [p[0] for p in points]
        lngs = [p[1] for p in points]
        bounds = [[min(lats), min(lngs)], [max(lats), max(lngs)]]

    config = {
        "points": points,
        "bounds": bounds,
        "heat": {
            "radius": radius, "blur": blur, "maxZoom": 12, "max": 1.0, "minOpacity": 0.3,
            "gradient": _HEAT_GRADIENT,
        },
        "tiles": tiles_cfg,
        "view": _view_config(zoom, _view_center(center, west)),
        "worldCopyJump": west is None,
        "scripts": [_LEAFLET_JS, _LEAFLET_HEAT_JS],
    }
    page = _render(_PAGE, {
        "VERSION": __version__,
        "TITLE": _html_text(title),
        "CSS_URL": _LEAFLET_CSS["urls"][0],
        "CSS_FALLBACK": _LEAFLET_CSS["urls"][1],
        "CSS_SRI": _LEAFLET_CSS["integrity"],
        "EXTRA_HTML": "",
        "CONFIG": _to_js(config),
        "INIT": _HEAT_INIT,
    })
    _write(output_path, page)
    _announce(f"✅  Heatmap saved → {output_path}  ({_plural(len(points), 'point')})", verbose)
