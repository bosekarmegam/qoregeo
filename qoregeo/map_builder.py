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
import shlex
import sys
import warnings
from typing import Any
from urllib.parse import urlsplit

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
_OSM_FR = '<a href="https://openstreetmap.fr/">OpenStreetMap France</a>'
_OSM_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
# Browsers send the page's origin (never its path) as the Referer that the
# OpenStreetMap tile policy requires; Leaflet 1.9.4 doesn't set this itself.
_REFERRER = {"referrerPolicy": "strict-origin-when-cross-origin"}
_OSM_OPTIONS = {
    "attribution": _OSM_ATTR + ' | <a href="https://www.openstreetmap.org/fixthemap">Report a map issue</a>',
    "maxZoom": 19, **_REFERRER,
}

# Open, keyless basemaps.
# needsReferer: the tile server's policy requires a Referer, which a page
# opened straight from disk (file://) cannot send. Such pages show a notice
# explaining how to view the map instead of requesting tiles.
_TILES: dict[str, dict[str, Any]] = {
    "light": {   # OpenStreetMap, softened so the data stands out (the default)
        "label": "OpenStreetMap, light (muted colours)",
        "url": _OSM_URL,
        "options": {**_OSM_OPTIONS, "className": "qg-tiles-light"},
        "needsReferer": True,
    },
    "osm": {
        "label": "OpenStreetMap (standard)",
        "url": _OSM_URL,
        "options": dict(_OSM_OPTIONS),
        "needsReferer": True,
    },
    "dark": {
        "label": "OpenStreetMap, dark (inverted colours)",
        "url": _OSM_URL,
        "options": {**_OSM_OPTIONS, "className": "qg-tiles-dark"},
        "needsReferer": True,
        "background": "#0f111a",
    },
    "humanitarian": {
        "label": "Humanitarian OpenStreetMap (HOT)",
        "url": "https://tile-{s}.openstreetmap.fr/hot/{z}/{x}/{y}.png",
        "options": {
            "attribution": _OSM_ATTR + ', Tiles style by <a href="https://www.hotosm.org/">'
                           f"Humanitarian OpenStreetMap Team</a> hosted by {_OSM_FR}",
            "subdomains": "abc", "maxZoom": 20, **_REFERRER,
        },
        "needsReferer": True,
    },
    "cyclosm": {
        "label": "CyclOSM (cycling)",
        "url": "https://{s}.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png",
        "options": {
            "attribution": f'<a href="https://www.cyclosm.org">CyclOSM</a> hosted by {_OSM_FR} | '
                           "Map data " + _OSM_ATTR,
            "subdomains": "abc", "maxZoom": 20, **_REFERRER,
        },
        "needsReferer": True,
    },
    "osm-fr": {
        "label": "OpenStreetMap France style",
        "url": "https://{s}.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png",
        "options": {
            "attribution": f"&copy; {_OSM_FR} | " + _OSM_ATTR,
            "subdomains": "abc", "maxZoom": 20, **_REFERRER,
        },
        "needsReferer": True,
    },
    "osm-de": {  # FOSSGIS: free for non-commercial and small sites
        "label": "OpenStreetMap Germany style (non-commercial use)",
        "url": "https://tile.openstreetmap.de/{z}/{x}/{y}.png",
        "options": {
            "attribution": _OSM_ATTR + ', tiles by <a href="https://www.openstreetmap.de/">'
                           "OpenStreetMap Deutschland</a>",
            "maxZoom": 18, **_REFERRER,
        },
        "needsReferer": False,
    },
    "topo": {
        "label": "OpenTopoMap (topographic, up to zoom 17)",
        "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        "options": {
            "attribution": "Map data: " + _OSM_ATTR + ', <a href="http://viewfinderpanoramas.org">SRTM</a>'
                           ' | Map style: &copy; <a href="https://opentopomap.org">OpenTopoMap</a> '
                           '(<a href="https://creativecommons.org/licenses/by-sa/3.0/">CC-BY-SA</a>)',
            "subdomains": "abc", "maxZoom": 17, **_REFERRER,
        },
        "needsReferer": False,
    },
    "satellite": {  # public-domain NASA imagery; 500 m pixels, so an overview map
        "label": "NASA Blue Marble satellite imagery (overview, up to zoom 10)",
        "url": "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/BlueMarble_NextGeneration/default/"
               "GoogleMapsCompatible_Level8/{z}/{y}/{x}.jpeg",
        "options": {
            "attribution": 'Imagery: <a href="https://earthdata.nasa.gov/gibs">NASA GIBS</a> '
                           "(Blue Marble, ESDIS)",
            "maxNativeZoom": 8, "maxZoom": 10,
        },
        "needsReferer": False,
        "background": "#0b1a2e",
    },
    "none": {    # no basemap: nothing is fetched from a tile server
        "label": "No basemap (nothing is downloaded)",
        "url": None,
        "options": {},
        "needsReferer": False,
        "background": "#f4f5f7",
    },
}

# Other accepted spellings.
_TILE_ALIASES = {
    "osm-light": "light", "osm_light": "light", "osm-dark": "dark", "osm_dark": "dark",
    "hot": "humanitarian", "cycling": "cyclosm", "opentopomap": "topo",
    "osm_fr": "osm-fr", "osm_de": "osm-de", "nasa": "satellite", "bluemarble": "satellite",
    "blank": "none",
}

# Shown (instead of requesting tiles) when a page using a needsReferer
# basemap is opened from disk. {c} marks code (see _file_notice).
_FILE_NOTICE = (
    "OpenStreetMap only serves map tiles to web pages, not to files opened from disk, so "
    "the basemap is hidden. Run {c}qoregeo serve {file}{c} (or {c}geo.show(){c} in Python) "
    "to see it, or create the map with tiles='satellite', 'topo', 'osm-de' or 'none', which work "
    "from disk."
)

# File names that need no quoting in POSIX shells, cmd or PowerShell (a
# leading '=' is special in zsh), and characters that "…" leaves special in
# a POSIX shell (plus bash's history '!' and control characters).
_SHELL_PLAIN_RE = re.compile(r"[\w.,:@%+/-][\w.,:@%+=/-]*")
_SHELL_SPECIAL_RE = re.compile(r'["$`\\!\x00-\x1f\x7f]')

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
    Serialise ``obj`` as ``JSON.parse("…")``, safe inside an inline <script>.

    Values are made JSON-safe first (NaN → null, sets → lists, …, and
    integers beyond 2**53 → strings so no digits are lost in the browser).
    JSON.parse — unlike an object literal — keeps a property named
    ``__proto__`` as ordinary data instead of turning it into the prototype.
    """
    text = json.dumps(_json_safe(obj, js=True), ensure_ascii=False, allow_nan=False)
    literal = json.dumps(_escape_surrogates(text), ensure_ascii=False)
    return "JSON.parse(" + re.sub("[<>&\u2028\u2029]", lambda m: _JS_ESCAPES[m.group(0)], literal) + ")"


def _render(template: str, values: dict[str, str]) -> str:
    """Single-pass ``__NAME__`` substitution (inserted text is never re-scanned)."""
    return re.sub(r"__([A-Z_]+)__", lambda m: values.get(m.group(1), m.group(0)), template)


def _html_text(text: Any) -> str:
    """HTML-escape ``text``, replacing characters that cannot be encoded as UTF-8."""
    return html.escape(str(text).encode("utf-8", "replace").decode("utf-8"))


def tile_presets() -> dict[str, str]:
    """The built-in basemap names and what each one shows."""
    return {name: preset["label"] for name, preset in _TILES.items()}


def _osm_policy_credit(url: str) -> str | None:
    """
    The credit a custom tile URL needs if it points at a server that follows
    the OpenStreetMap tile policy (tile.openstreetmap.org, *.openstreetmap.fr),
    which requires a Referer and attribution; None for any other server.
    """
    try:
        host = (urlsplit(url).hostname or "").rstrip(".")
    except ValueError:   # e.g. an unbalanced '[' in the host
        return None
    if host == "openstreetmap.org" or host.endswith(".openstreetmap.org"):
        return str(_OSM_OPTIONS["attribution"])
    if host == "openstreetmap.fr" or host.endswith(".openstreetmap.fr"):
        return f"{_OSM_ATTR}, tiles hosted by {_OSM_FR}"
    return None


def _tiles_config(tiles: Any, attribution: str | None = None) -> dict[str, Any]:
    """
    Resolve a basemap name (or a custom ``https://…/{z}/{x}/{y}…`` URL
    template) to the page's tile config.

    Every preset is open and keyless. ``attribution`` is trusted HTML; for
    presets (and custom URLs on OpenStreetMap's own tile servers) it is
    appended to the provider's required credit.
    """
    presets = " | ".join(repr(k) for k in _TILES)
    if attribution is not None and not isinstance(attribution, str):
        raise InvalidArgumentError("attribution", attribution, "a string or None",
                                   "geo.map('out.html', attribution='&copy; Example')")
    if not isinstance(tiles, str):
        raise InvalidArgumentError(
            "tiles", tiles, f"{presets} or a custom https:// URL template",
            "geo.map('out.html', tiles='topo')",
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
            options["subdomains"] = "abc"
        credit = _osm_policy_credit(url)
        if credit is None:
            return {"name": "custom", "url": url, "options": options, "needsReferer": False}
        # OpenStreetMap's own servers get the same safeguards as the presets:
        # the Referer their policy requires (no tiles from disk) and the credit.
        options.update(_REFERRER, attribution=f"{credit}, {attribution}" if attribution else credit)
        return {"name": "custom", "url": url, "options": options, "needsReferer": True}

    name = tiles.strip().lower()
    name = _TILE_ALIASES.get(name, name)
    if name not in _TILES:
        hint = ""
        if name in ("voyager", "positron", "dark_matter", "carto", "cartodb"):
            hint = " (CARTO basemaps were replaced by open, keyless ones in 1.1.0)"
        raise InvalidArgumentError(
            "tiles", tiles, f"{presets} or a custom https:// URL template{hint}",
            "geo.map('out.html', tiles='light')",
        )
    preset = _TILES[name]
    config: dict[str, Any] = {
        "name": name,
        "url": preset["url"],
        "options": dict(preset["options"]),
        "needsReferer": preset["needsReferer"],
    }
    if "background" in preset:
        config["background"] = preset["background"]
    if attribution:
        current = config["options"].get("attribution")
        config["options"]["attribution"] = f"{current}, {attribution}" if current else attribution
    return config


def _view_config(zoom: int | None, center: Coord | None) -> dict[str, Any]:
    return {
        "center": list(center) if center is not None else None,
        "zoom": zoom if zoom is not None else _DEFAULT_CENTER_ZOOM,
        "fitMaxZoom": zoom if zoom is not None else _DEFAULT_FIT_MAX_ZOOM,
    }


def _paths(geometry: Any) -> list[Any]:
    """The lines and rings of ``geometry`` (as coordinate lists)."""
    if not isinstance(geometry, dict):
        return []
    gtype, coords = geometry.get("type"), geometry.get("coordinates")
    if gtype == "GeometryCollection":
        return [p for g in geometry.get("geometries") or [] for p in _paths(g)]
    if not isinstance(coords, list):
        return []
    if gtype == "LineString":
        return [coords]
    if gtype in ("MultiLineString", "Polygon"):
        return list(coords)
    if gtype == "MultiPolygon":
        return [ring for poly in coords if isinstance(poly, list) for ring in poly]
    return []


def _spans_world(geometries: list[Any]) -> bool:
    """
    True if a line or ring runs (all but) the whole way round the globe: its
    longitudes span 360° (e.g. -180° to 180°), or at least 359° with every
    vertex on one of its two extreme meridians — an extent box from
    -179.9999° to 179.9999°, whose edges are straight in lon/lat (RFC 7946).

    A path that merely crosses ±180° (a dateline-straddling island or track,
    with vertices spread on both sides) is not world-spanning.
    """
    for path in (p for g in geometries for p in _paths(g)):
        xs = [float(c[0]) for c in path if isinstance(c, list) and c and isinstance(c[0], (int, float))]
        if not xs:
            continue
        west, east = min(xs), max(xs)
        if east - west >= 360.0 or (
                east - west >= 359.0 and all(min(x - west, east - x) <= 1e-9 for x in xs)):
            return True
    return False


def _antimeridian_west(lngs: list[float], geometries: list[Any] | None = None) -> float | None:
    """
    If the data is one cluster across ±180° — its tightest longitude span
    crosses the antimeridian and is under 180° wide, and no shape runs round
    the whole globe (Antarctica, a world bbox) — return the span's western
    edge (longitudes below it belong east of 180°); else None.
    """
    if not lngs:
        return None
    west, east = _lng_span(lngs)
    if west <= east or east + 360.0 - west >= 180.0 or _spans_world(geometries or []):
        return None
    return west


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
  @media (max-width: 480px) {
    #brand { left:10px; padding:6px 10px; font-size:12px; max-width:calc(100vw - 20px); }
    #brand strong { font-size:13px; }
  }
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
  .qg-tiles-light { filter:grayscale(.7) brightness(1.04) contrast(.92); }
  .qg-tiles-dark { filter:invert(1) hue-rotate(180deg) brightness(.92) contrast(.9) saturate(.45); }
  #qg-notice { top:16px; left:56px; max-width:min(calc(100vw - 72px), 520px); padding:10px 34px 10px 14px;
               line-height:1.45; color:#fff; border-color:rgba(255,215,0,.5); }
  #qg-notice code { color:#FFD700; font-family:ui-monospace, SFMono-Regular, Menlo, monospace; }
  #qg-notice button { position:absolute; top:6px; right:8px; background:none; border:0; color:#fff;
                      font-size:18px; line-height:1; cursor:pointer; }
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

  function showNotice(text) {
    var box = document.createElement("div");
    box.id = "qg-notice";
    box.className = "qg-panel";
    box.setAttribute("role", "status");
    // `…` marks code; NUL does when the file name itself contains a backtick.
    text.split(text.indexOf("\\u0000") >= 0 ? "\\u0000" : "`").forEach(function (part, i) {
      var node = document.createElement(i % 2 ? "code" : "span");
      node.textContent = part;
      box.appendChild(node);
    });
    var close = document.createElement("button");
    close.type = "button";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "\u00d7";
    close.onclick = function () { box.remove(); };
    box.appendChild(close);
    document.body.appendChild(box);
  }

  // Whether tile requests carry the Referer (the page's origin) that the
  // OpenStreetMap policy requires. A page opened from disk sends none; nor
  // does a srcdoc iframe (notebook output) inside one, as its base URL is its
  // parent's, nor a sandboxed iframe with an opaque origin.
  function sendsReferer() {
    try {
      return self.origin !== "null" && /^https?:$/.test(new URL(document.baseURI).protocol);
    } catch (e) {
      return false;
    }
  }

  // Keep the title panel above the basemap credit, which must stay visible
  // and wraps onto several lines on narrow screens.
  function keepBrandClear(map) {
    var brand = document.getElementById("brand");
    var credit = map.attributionControl && map.attributionControl.getContainer();
    if (!brand || !credit) return;
    function place() {
      brand.style.bottom = "";
      var b = brand.getBoundingClientRect(), c = credit.getBoundingClientRect();
      if (c.height && b.right > c.left && b.bottom > c.top) {
        brand.style.bottom = (document.documentElement.clientHeight - c.top + 6) + "px";
      }
    }
    place();
    window.addEventListener("resize", place);
    if (window.ResizeObserver) new ResizeObserver(place).observe(credit);
  }

  function baseMap(L) {
    var map = L.map("map", { zoomControl: true, preferCanvas: true,
                             worldCopyJump: CONFIG.worldCopyJump });
    var t = CONFIG.tiles;
    var blocked = t.needsReferer && !sendsReferer();
    var background = blocked ? "#f4f5f7" : t.background;
    if (background) map.getContainer().style.background = background;
    if (blocked) showNotice(CONFIG.fileNotice);
    else if (t.url) L.tileLayer(t.url, t.options).addTo(map);
    keepBrandClear(map);
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


def _shell_arg(name: str) -> str:
    """
    ``name`` as one shell argument that can be pasted as printed: bare if it
    has only plain characters, else in double quotes (POSIX shells, cmd and
    PowerShell all accept them), else — when it holds a character "…" leaves
    special, such as $ ` " \\ or ! — single-quoted for POSIX shells.
    """
    if _SHELL_PLAIN_RE.fullmatch(name):
        return name
    if not _SHELL_SPECIAL_RE.search(name):
        return f'"{name}"'
    return shlex.quote(name)


def _file_notice(output_path: str | None) -> str:
    """
    The page's "how to view this map" notice, naming the file. Code is
    marked with backticks, or with NUL (which no file name can contain) if
    the quoted name has a backtick of its own.
    """
    name = (os.path.basename(output_path) if output_path else "") or "map.html"
    arg = _shell_arg("./" + name if name.startswith("-") else name)   # not an option
    return _FILE_NOTICE.format(file=arg, c="\0" if "`" in arg else "`")


def _shell(title: str, extra_html: str, config: dict[str, Any], init: str) -> str:
    return _render(_PAGE, {
        "VERSION": __version__,
        "TITLE": _html_text(title),
        "CSS_URL": _LEAFLET_CSS["urls"][0],
        "CSS_FALLBACK": _LEAFLET_CSS["urls"][1],
        "CSS_SRI": _LEAFLET_CSS["integrity"],
        "EXTRA_HTML": extra_html,
        "CONFIG": _to_js(config),
        "INIT": init,
    })


def map_page(
    features: list[Feature],
    title: str = "QOREgeo Map",
    zoom: int | None = None,
    center: Coord | None = None,
    tiles: str = "light",
    overlays: list[dict[str, Any]] | None = None,
    attribution: str | None = None,
    output_path: str | None = None,
) -> tuple[str, int]:
    """
    Build an interactive Leaflet map page of ``features``.

    Returns ``(html, located)`` where ``located`` is the number of features
    that have a location. ``output_path`` only names the file in the page's
    "how to view this map" notice.
    """
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

    tiles_cfg = _tiles_config(tiles, attribution)

    # Data spanning the antimeridian is drawn on one continuous longitude
    # range (e.g. 170°…190°) so it opens as one cluster, not a world view.
    lngs = [float(p[0]) for f in features for p in _iter_positions(f.get("geometry"))]
    lngs += [float(p[0]) for s in overlay_list for g in _overlay_geometries(s)
             for p in _iter_positions(g)]
    west = _antimeridian_west(lngs, [f.get("geometry") for f in features]
                              + [g for s in overlay_list for g in _overlay_geometries(s)])
    if west is not None:
        features = [{**f, "geometry": _shift_geometry(f.get("geometry"), west)} for f in features]
        overlay_list = [_shift_overlay(s, west) for s in overlay_list]

    config = {
        "data": {"type": "FeatureCollection", "features": features},
        "overlays": overlay_list,
        "tiles": tiles_cfg,
        "fileNotice": _file_notice(output_path),
        "view": _view_config(zoom, _view_center(center, west)),
        "worldCopyJump": west is None,
        "style": _STYLE,
        "scripts": [_LEAFLET_JS],
    }
    try:
        page = _shell(title, '<div id="count" class="qg-panel"><span id="n">0</span> features</div>',
                      config, _MAP_INIT)
    except RecursionError:
        error = _too_deep_error(features, overlay_list)
        if error is None:
            raise
        raise error from None
    located = sum(1 for f in features if _representative_point(f.get("geometry")) is not None)
    return page, located


# Values nested deeper than this are reported as the cause of a RecursionError
# while the page's data is serialised (Python's default limit is 1000 frames);
# depths are counted up to _DEPTH_CAP.
_REPORT_DEPTH = 100
_DEPTH_CAP = 1000


def _nesting_depth(value: Any, cap: int = _DEPTH_CAP) -> int:
    """
    How many dict / list levels deep ``value`` goes, counted without
    recursion and stopping above ``cap`` (so a value that contains itself
    returns ``cap + 1``). A container shared by several parents is walked
    again only when reached at a deeper level.
    """
    deepest = 0
    reached: dict[int, int] = {}   # id(container) -> deepest level it was reached at
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, level = stack.pop()
        if isinstance(item, dict):
            children: Any = item.values()
        elif isinstance(item, (list, tuple, set, frozenset)):
            children = item
        else:
            continue
        if level > cap:
            return cap + 1
        if reached.get(id(item), 0) >= level:
            continue
        reached[id(item)] = level
        deepest = max(deepest, level)
        stack.extend((child, level + 1) for child in children)
    return deepest


def _too_deep_error(features: list[Feature], overlays: list[Any]) -> InvalidArgumentError | None:
    """
    An error naming the most deeply nested value in ``features`` / ``overlays``
    (the reason the page data could not be serialised), or None if nothing is
    nested more than _REPORT_DEPTH levels.
    """
    worst: tuple[int, str, str] = (0, "", "")
    for i, feat in enumerate(features):
        if not isinstance(feat, dict):
            continue
        for key, value in feat.items():
            items = value.items() if key == "properties" and isinstance(value, dict) else [(None, value)]
            for prop, item in items:
                depth = _nesting_depth(item)
                if depth > worst[0]:
                    if prop is None:
                        where = f"features[{i}][{key!r}]"
                        fix = f"del geo.get_features()[{i}][{key!r}]"
                    else:
                        where = f"features[{i}]['properties'][{prop!r}]"
                        fix = f"del geo.get_features()[{i}]['properties'][{prop!r}]"
                    worst = (depth, where, fix)
    for i, shape in enumerate(overlays):
        depth = _nesting_depth(shape)
        if depth > worst[0]:
            worst = (depth, f"overlays[{i}]", "geo.map('out.html', overlays=[geo.buffer((28.61, 77.20), 10)])")
    depth, where, fix = worst
    if depth <= _REPORT_DEPTH:
        return None
    if depth > _DEPTH_CAP:
        nested = f"more than {_DEPTH_CAP} levels deep, or containing itself"
    else:
        nested = f"{depth} levels deep"
    return InvalidArgumentError(
        where, f"<a value nested {nested}>",
        "dicts / lists nested at most a few hundred levels deep, so the map page can embed "
        "them (flatten a deeper value, or leave it out)",
        f"{fix}   # then map again",
    )


def build_map(
    features: list[Feature],
    output_path: str,
    title: str = "QOREgeo Map",
    zoom: int | None = None,
    center: Coord | None = None,
    tiles: str = "light",
    overlays: list[dict[str, Any]] | None = None,
    verbose: bool = True,
    attribution: str | None = None,
) -> None:
    """Write an interactive Leaflet map of ``features`` to ``output_path``."""
    page, located = map_page(features, title, zoom, center, tiles, overlays, attribution, output_path)
    _write(output_path, page)
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


def heatmap_page(
    features: list[Feature],
    title: str = "QOREgeo Heatmap",
    intensity_col: str | None = None,
    zoom: int | None = None,
    center: Coord | None = None,
    tiles: str = "light",
    radius: int = 25,
    blur: int = 18,
    attribution: str | None = None,
    output_path: str | None = None,
    stacklevel: int = 3,
) -> tuple[str, int]:
    """
    Build a Leaflet heatmap page of ``features``.

    Returns ``(html, points)``. Rows that add no heat are reported with a
    warning attributed ``stacklevel`` frames up (3 = the caller's caller).
    """
    tiles_cfg = _tiles_config(tiles, attribution)
    dropped: list[Any] = []
    points = _heat_points(features, intensity_col, dropped)
    if dropped:
        examples = ", ".join(repr(v) for v in dropped[:3]) + (", …" if len(dropped) > 3 else "")
        warnings.warn(
            f"QOREgeo: {_plural(len(dropped), 'row')} with a missing, non-numeric or "
            f"non-positive '{intensity_col}' value ({examples}) "
            f"{'adds' if len(dropped) == 1 else 'add'} no heat"
            + (" — the heatmap is empty." if not points else "."),
            stacklevel=stacklevel,
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
        "fileNotice": _file_notice(output_path),
        "view": _view_config(zoom, _view_center(center, west)),
        "worldCopyJump": west is None,
        "scripts": [_LEAFLET_JS, _LEAFLET_HEAT_JS],
    }
    return _shell(title, "", config, _HEAT_INIT), len(points)


def build_heatmap(
    features: list[Feature],
    output_path: str,
    title: str = "QOREgeo Heatmap",
    intensity_col: str | None = None,
    zoom: int | None = None,
    center: Coord | None = None,
    tiles: str = "light",
    radius: int = 25,
    blur: int = 18,
    verbose: bool = True,
    attribution: str | None = None,
) -> None:
    """Write a Leaflet heatmap of ``features`` to ``output_path``."""
    page, n = heatmap_page(features, title, intensity_col, zoom, center, tiles, radius, blur,
                           attribution, output_path, stacklevel=4)
    _write(output_path, page)
    _announce(f"✅  Heatmap saved → {output_path}  ({_plural(n, 'point')})", verbose)
