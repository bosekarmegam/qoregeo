"""
qoregeo.render
==============
Static map images (SVG and PNG) in pure Python, with no dependencies.

    from qoregeo.render import render_png, render_svg

    svg = render_svg(features, title="Delhi stores")          # -> str
    png = render_png(features, width=1200, height=800)        # -> bytes
    open("map.png", "wb").write(png)

Both functions take a list of GeoJSON Feature dicts (any geometry type,
including GeometryCollection; a ``None`` geometry is skipped) and optional
``overlays``, drawn as dashed gold outlines. Overlays are GeoJSON geometries,
Features or FeatureCollections, such as a ``geo.buffer()`` zone.

Drawing
-------
* Web Mercator projection (latitude clamped to ±85.0511°), fitted to the
  bounds of the features and overlays with the aspect ratio preserved.
* Data that crosses the antimeridian is drawn as one cluster, using the same
  rule as the HTML maps: longitudes west of the data's western edge move +360°
  (when the data then fits in a hemisphere; world-wide data stays a world map).
* The style matches the HTML maps: teal (#00D4AA) points with a white outline,
  2 px lines, polygons with an 18 % fill and holes cut out (even-odd), and
  overlays as dashed gold (#FFD700) outlines with a faint fill.
* No basemap: the images are self-contained and render offline. Instead, so
  that a reader (or a vision model) can tell where on Earth the data is and
  what it shows, the map gets:

  - a light latitude / longitude graticule (``grid=True``) with labels on
    the left and bottom edges, e.g. ``28.6°N`` and ``77.2°E`` (``179°W`` on
    an antimeridian map), at a "nice" step giving about 3 to 7 lines;
  - a scale bar in the bottom-left corner (``scale_bar=True``), e.g.
    ``500 m`` or ``20 km``, for views up to ~60° across;
  - optional feature labels (``labels="name"``): the value of that property
    next to each point (centred on other geometries), dark grey with a white
    halo, skipped where they would overlap one another, at most 300.

  The layout is computed once, so the SVG and the PNG of the same map place
  every line, label and scale bar at the same pixels.

The PNG is rasterised into a ``bytearray`` (anti-aliased strokes and markers,
scanline polygon fill, an embedded 5x7 bitmap font for the text) and encoded
with ``zlib`` as 8-bit RGBA. No external libraries are needed.
"""

from __future__ import annotations

import bisect
import functools
import math
import re
import struct
import unicodedata
import zlib
from collections.abc import Callable, Iterator
from typing import Any

from ._version import __version__
from .exceptions import InvalidArgumentError
from .utils import (
    GEOMETRY_TYPES,
    _geometry_problem,
    _lng_span,
    _normalize_lng,
    _representative_point,
)

_Pt = tuple[float, float]
_RGB = tuple[int, int, int]
_Tables = tuple[bytes, bytes, bytes]
_Box = tuple[int, int, int, int]       # x0, y0, x1, y1 (pixels, end-exclusive)


# ─────────────────────────────────────────────────────────────────────────────
# Style (kept in step with map_builder._STYLE)
# ─────────────────────────────────────────────────────────────────────────────

_TEAL: _RGB = (0x00, 0xD4, 0xAA)
_GOLD: _RGB = (0xFF, 0xD7, 0x00)
_WHITE: _RGB = (0xFF, 0xFF, 0xFF)
_CREDIT: _RGB = (0x8A, 0x94, 0xA6)
_DARK_TEXT: _RGB = (0x1F, 0x29, 0x33)
_LIGHT_TEXT: _RGB = (0xF5, 0xF7, 0xFA)

_POLYGON_FILL_OPACITY = 0.18
_OVERLAY_FILL_OPACITY = 0.06
_LINE_WIDTH = 2.0
_DASH = (6.0, 4.0)                 # on, off (px), the same as the HTML overlay dashArray
_POINT_STROKE = 1.5                # white marker outline (thinner on tiny markers)

_TITLE_BAND = 28                   # px reserved above the map when there is a title
_TITLE_MIN_HEIGHT = 2 * _TITLE_BAND  # shorter images put a small title over the map instead
_CREDIT_TEXT = "QOREgeo"
_CREDIT_MIN = (96, 48)             # smallest canvas (w, h) that shows the credit
_FONT_STACK = "Helvetica, Arial, sans-serif"

# Annotations: feature labels, graticule and scale bar.
_LABEL_TEXT: _RGB = (0x33, 0x33, 0x33)
_GRID_LINE: _RGB = (0xE6, 0xE8, 0xEB)
_GRID_TEXT: _RGB = (0x8A, 0x8F, 0x98)
_LABEL_MAX_CHARS = 40
_LABEL_LIMIT = 300                 # labels drawn at most (the rest are skipped)
_LABEL_FONT = (11, 13)             # SVG font-size at text scale 1 / 2
_ANNOTATION_MIN = (96, 64)         # smallest map (width, height) with a grid / scale bar
_GRID_STEPS = (0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5,
               1, 2, 5, 10, 15, 30, 45, 60, 90)
_GRID_MAX_LINES = 7                # the finest step giving at most this many lines
_SCALE_BAR_PX = (60, 160)          # shortest and longest scale bar
_SCALE_BAR_MAX_DEG = 60.0          # no scale bar on wider (or taller) views
_EARTH_RADIUS_M = 6378137.0        # Web Mercator sphere

_MAX_LAT = 85.0511287798           # Web Mercator limit
_MIN_SPAN = math.radians(0.01)     # smallest extent shown (~1 km), e.g. for a single point
_SIZE_RANGE = (16, 8192)
_RADIUS_RANGE = (0.5, 50.0)
_LEVELS = 64                       # alpha quantisation for blending lookup tables

_HEX_COLOR = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})")
# Characters XML 1.0 cannot contain (control characters, lone surrogates, U+FFFE/F).
_XML_INVALID = re.compile("[^\t\n\r\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────

def _preview(obj: Any, limit: int = 120) -> Any:
    """Keep huge values out of error messages (and survive unprintable ones)."""
    try:
        text = repr(obj)
    except Exception:        # e.g. an int beyond Python's int-to-str digit limit
        return f"<{type(obj).__name__} too large to show>"
    return obj if len(text) <= limit else text[: limit - 3] + "..."


def _check_size(fn: str, name: str, value: Any) -> int:
    lo, hi = _SIZE_RANGE
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise InvalidArgumentError(
            name, _preview(value), f"a whole number of pixels from {lo} to {hi}",
            f"{fn}(features, width=1200, height=800)",
        )
    return value


def _check_padding(fn: str, padding: Any, width: int, height: int) -> int:
    if isinstance(padding, bool) or not isinstance(padding, int) or padding < 0 \
            or 2 * padding >= min(width, height):
        # The default (24) does not fit images under 49 px: suggest one that does.
        most = (min(width, height) - 1) // 2
        raise InvalidArgumentError(
            "padding", _preview(padding),
            f"a whole number of pixels >= 0 and less than half of the image "
            f"({width}x{height} allows at most {most})",
            f"{fn}(features, width={width}, height={height}, padding={min(24, most // 2)})",
        )
    return padding


def _check_radius(fn: str, radius: Any) -> float:
    lo, hi = _RADIUS_RANGE
    ok = isinstance(radius, (int, float)) and not isinstance(radius, bool)
    # Plain comparisons: NaN and ±inf fail them, and a huge int cannot overflow.
    if not ok or not lo <= radius <= hi:
        raise InvalidArgumentError(
            "point_radius", _preview(radius), f"a number of pixels from {lo:g} to {hi:g}",
            f"{fn}(features, point_radius=4)",
        )
    return float(radius)


def _parse_color(fn: str, color: Any) -> _RGB:
    """'#rrggbb' or '#rgb' → (r, g, b)."""
    if not isinstance(color, str) or not _HEX_COLOR.fullmatch(color):
        raise InvalidArgumentError(
            "background", _preview(color), "a hex colour string '#rrggbb' or '#rgb'",
            f"{fn}(features, background='#0f111a')",
        )
    digits = color[1:]
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def _check_labels(fn: str, labels: Any) -> str | None:
    if labels is None or isinstance(labels, str):
        return labels
    raise InvalidArgumentError(
        "labels", _preview(labels),
        "the name of a feature property to label features with (a string), or None",
        f'{fn}(features, labels="name")',
    )


def _check_flag(fn: str, name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise InvalidArgumentError(
            name, _preview(value), "True or False", f"{fn}(features, {name}=False)",
        )
    return value


# A drawable geometry and the properties of the Feature it came from (None
# for a bare geometry or an overlay).
_Item = tuple[dict[str, Any], Any]


def _collect(fn: str, name: str, items: Any) -> list[_Item]:
    """
    The drawable geometries in ``items`` (a list of Features, geometries or
    FeatureCollections, or one such dict), each with its Feature's
    properties. Null geometries are skipped; anything else that is not valid
    GeoJSON raises InvalidArgumentError.
    """
    if items is None:
        return []
    if isinstance(items, dict):
        items = [items]
    example = (
        f"{fn}(geo.features, overlays=[geo.buffer((28.61, 77.20), 10)])"
        if name == "overlays"
        else f"{fn}([{{'type': 'Feature', 'properties': {{}}, "
             f"'geometry': {{'type': 'Point', 'coordinates': [77.2, 28.6]}}}}])"
    )
    if not isinstance(items, (list, tuple)):
        raise InvalidArgumentError(
            name, _preview(items),
            "a list of GeoJSON Feature / geometry dicts (or a FeatureCollection)", example,
        )
    out: list[_Item] = []
    for i, item in enumerate(items):
        _gather(item, f"{name}[{i}]", out, example, allow_collection=True)
    return out


def _gather(obj: Any, where: str, out: list[_Item], example: str,
            allow_collection: bool) -> None:
    if not isinstance(obj, dict):
        raise InvalidArgumentError(
            where, _preview(obj), "a GeoJSON Feature or geometry dict", example,
        )
    otype = obj.get("type")
    if otype == "FeatureCollection" and allow_collection:
        feats = obj.get("features")
        if not isinstance(feats, (list, tuple)):
            raise InvalidArgumentError(
                f"{where}['features']", _preview(feats), "a list of GeoJSON Features", example,
            )
        for j, feat in enumerate(feats):
            _gather(feat, f"{where}['features'][{j}]", out, example, allow_collection=False)
        return
    properties = None
    if otype == "Feature" or (otype is None and "geometry" in obj):
        geometry = obj.get("geometry")
        if geometry is None:
            return
        properties = obj.get("properties")
        where, obj = f"{where}['geometry']", geometry
    elif otype not in GEOMETRY_TYPES:
        raise InvalidArgumentError(
            where, _preview(obj),
            f"a GeoJSON Feature or geometry (unknown GeoJSON type {otype!r})", example,
        )
    problem = _geometry_problem(obj)
    if problem:
        raise InvalidArgumentError(
            where, _preview(obj), f"a valid GeoJSON geometry ({problem})", example,
        )
    out.append((obj, properties))


# ─────────────────────────────────────────────────────────────────────────────
# Scene: geometries → drawing primitives → pixel coordinates
# ─────────────────────────────────────────────────────────────────────────────

class _Layer:
    """Points, polylines and polygons (lists of rings) in one coordinate space."""

    __slots__ = ("points", "lines", "polygons")

    def __init__(self) -> None:
        self.points: list[_Pt] = []
        self.lines: list[list[_Pt]] = []
        self.polygons: list[list[list[_Pt]]] = []

    def positions(self) -> Iterator[_Pt]:
        yield from self.points
        for line in self.lines:
            yield from line
        for rings in self.polygons:
            for ring in rings:
                yield from ring

    def mapped(self, fn: Callable[[_Pt], _Pt]) -> _Layer:
        out = _Layer()
        out.points = [fn(p) for p in self.points]
        out.lines = [[fn(p) for p in line] for line in self.lines]
        out.polygons = [[[fn(p) for p in ring] for ring in rings] for rings in self.polygons]
        return out


def _pos(p: Any) -> _Pt:
    return float(p[0]), float(p[1])


def _add_geometry(geometry: dict[str, Any], layer: _Layer) -> None:
    """Append the primitives of a validated geometry (lng, lat) to ``layer``."""
    gtype = geometry.get("type")
    if gtype == "GeometryCollection":
        for g in geometry.get("geometries") or []:
            _add_geometry(g, layer)
        return
    coords = geometry.get("coordinates") or []
    if gtype == "Point":
        layer.points.append(_pos(coords))
    elif gtype == "MultiPoint":
        layer.points.extend(_pos(p) for p in coords)
    elif gtype in ("LineString", "MultiLineString"):
        for line in [coords] if gtype == "LineString" else coords:
            if len(line) >= 2:
                layer.lines.append([_pos(p) for p in line])
    elif gtype in ("Polygon", "MultiPolygon"):
        for poly in [coords] if gtype == "Polygon" else coords:
            rings = [[_pos(p) for p in ring] for ring in poly if len(ring) >= 3]
            if rings:
                layer.polygons.append(rings)


def _spans_world(layer: _Layer) -> bool:
    """
    True if a line or ring runs (all but) the whole way round the globe. This
    is the same rule as map_builder._spans_world: its longitudes span 360°
    (-180° to 180°, 0° to 360° …), or at least 359° with every vertex on one
    of its two extreme meridians (an extent box from -179.9999° to 179.9999°).
    """
    for path in [*layer.lines, *(ring for rings in layer.polygons for ring in rings)]:
        xs = [p[0] for p in path]
        if not xs:
            continue
        west, east = min(xs), max(xs)
        if east - west >= 360.0 or (
                east - west >= 359.0 and all(min(x - west, east - x) <= 1e-9 for x in xs)):
            return True
    return False


def _whole_turns(rings: list[list[_Pt]]) -> list[list[_Pt]]:
    """
    A shape lying wholly beyond ±180° (e.g. 200°..210°), moved by whole turns
    into the normal range in one piece. Anything else keeps its longitudes, so
    a ring drawn across 180° (170°..190°) or round the world (0°..360°) is
    neither torn apart nor folded flat, as normalising each vertex would do.
    """
    lo = min(p[0] for ring in rings for p in ring)
    hi = max(p[0] for ring in rings for p in ring)
    if lo > 180.0:
        shift = -360.0 * math.ceil((lo - 180.0) / 360.0)
    elif hi < -180.0:
        shift = 360.0 * math.ceil((-180.0 - hi) / 360.0)
    else:
        return rings
    return [[(x + shift, y) for x, y in ring] for ring in rings]


def _mercator_y(lat: float) -> float:
    lat = -_MAX_LAT if lat < -_MAX_LAT else _MAX_LAT if lat > _MAX_LAT else lat
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def _clean_title(fn: str, title: Any) -> str | None:
    if title is None:
        return None
    try:
        text = str(title)
    except ValueError:       # e.g. an int beyond Python's int-to-str digit limit
        raise InvalidArgumentError(
            "title", _preview(title), "a string (or None for no title)",
            f'{fn}(features, title="Stores in Delhi")',
        ) from None
    return text if text.strip() else None


class _View:
    """
    The fitted projection: (unwrapped) longitude / latitude <-> canvas pixels.
    ``px`` does the same arithmetic as the projection of the features, so a
    label anchored on a point lands on exactly the pixel of its marker.
    """

    __slots__ = ("scale", "mid_x", "mid_y", "ox", "oy", "point_lng", "unwrap")

    def __init__(self, scale: float, mid_x: float, mid_y: float, ox: float, oy: float,
                 point_lng: Callable[[float], float],
                 unwrap: Callable[[_Layer], _Layer]) -> None:
        self.scale, self.mid_x, self.mid_y, self.ox, self.oy = scale, mid_x, mid_y, ox, oy
        self.point_lng = point_lng        # a point's longitude on this map (unwrapped)
        self.unwrap = unwrap              # the same for a whole layer of shapes

    def px(self, lng: float, lat: float) -> _Pt:
        return (self.ox + (math.radians(lng) - self.mid_x) * self.scale,
                self.oy - (_mercator_y(lat) - self.mid_y) * self.scale)

    def lng(self, x: float) -> float:
        """The (unwrapped) longitude at canvas column ``x``."""
        return math.degrees(self.mid_x + (x - self.ox) / self.scale)

    def lat(self, y: float) -> float:
        """The latitude at canvas row ``y``."""
        m = self.mid_y - (y - self.oy) / self.scale
        m = -20.0 if m < -20.0 else 20.0 if m > 20.0 else m    # beyond ±89.9999...°
        return math.degrees(2 * math.atan(math.exp(m)) - math.pi / 2)


class _Label:
    """A line of text laid out once, for both the SVG and the PNG."""

    __slots__ = ("x", "y", "width", "text", "ascii", "anchor")

    def __init__(self, x: int, y: int, width: int, text: str, ascii_text: str,
                 anchor: str = "start") -> None:
        self.x, self.y, self.width = x, y, width   # top-left of the glyphs, width in px
        self.text = text                           # the SVG text (any Unicode)
        self.ascii = ascii_text                    # the PNG text (bitmap font)
        self.anchor = anchor                       # SVG text-anchor: 'start', 'middle', 'end'


class _ScaleBar:
    """A bar ``x0..x1`` (end-exclusive) whose 2 rows start at ``y``, with end ticks."""

    __slots__ = ("x0", "x1", "y", "label")

    def __init__(self, x0: int, x1: int, y: int, label: _Label) -> None:
        self.x0, self.x1, self.y, self.label = x0, x1, y, label

    def rects(self) -> list[_Box]:
        """The bar and its two ticks as (x, y, width, height) rectangles."""
        return [(self.x0, self.y, self.x1 - self.x0, 2),
                (self.x0, self.y - 4, 2, 6), (self.x1 - 2, self.y - 4, 2, 6)]


class _Placer:
    """
    Greedy collision check for text: a box is placed only if it lies on the
    canvas and clear of every box placed (or blocked) before it. A spatial
    hash keeps this fast with thousands of candidates.
    """

    _CELL = 64
    GAP = 1                                       # px kept clear between two boxes

    def __init__(self, width: int, height: int) -> None:
        self.w, self.h = width, height
        self.cells: dict[tuple[int, int], list[_Box]] = {}

    def _keys(self, box: _Box) -> list[tuple[int, int]]:
        c, g = self._CELL, self.GAP
        x0, y0, x1, y1 = box
        return [(cx, cy) for cx in range((x0 - g) // c, (x1 + g) // c + 1)
                for cy in range((y0 - g) // c, (y1 + g) // c + 1)]

    def block(self, box: _Box) -> None:
        """Reserve ``box`` (the title, the credit...) unconditionally."""
        for key in self._keys(box):
            self.cells.setdefault(key, []).append(box)

    def place(self, box: _Box) -> bool:
        x0, y0, x1, y1 = box
        if x0 < 0 or y0 < 0 or x1 > self.w or y1 > self.h:
            return False
        g = self.GAP
        for key in self._keys(box):
            for b in self.cells.get(key, ()):
                if x0 < b[2] + g and b[0] < x1 + g and y0 < b[3] + g and b[1] < y1 + g:
                    return False
        self.block(box)
        return True


class _Markers:
    """The pixels of the point markers, row by row: does a box touch any marker?"""

    def __init__(self, points: list[_Pt], radius: float, stroke: float) -> None:
        # A marker stamped on pixel (ix, iy) covers ix - e .. ix + e (see _marker_stamp).
        self.reach = math.ceil(radius + stroke / 2 + 0.5) - 1
        rows: dict[int, list[int]] = {}
        for ix, iy in dict.fromkeys((math.floor(x), math.floor(y)) for x, y in points):
            rows.setdefault(iy, []).append(ix)
        for xs in rows.values():
            xs.sort()
        self.rows = rows

    def touch(self, box: _Box, gap: int) -> bool:
        """True if a marker lies within ``gap`` px of ``box``."""
        e = self.reach + gap
        x0, y0, x1, y1 = box[0] - e, box[1] - e, box[2] + e, box[3] + e
        rows = self.rows
        for y in range(y0, y1):
            xs = rows.get(y)
            if xs:
                i = bisect.bisect_left(xs, x0)
                if i < len(xs) and xs[i] < x1:
                    return True
        return False


def _text_px(chars: int, scale: int) -> int:
    """Width in pixels of ``chars`` characters of the 5x7 font (6 px advance)."""
    return 6 * scale * chars - scale


def _label_texts(value: Any) -> tuple[str, str, int] | None:
    """
    ``(svg_text, png_text, cells)`` for a property value, or None for no
    label. Both texts are cut to 40 characters (with '…' / '...'); ``cells``
    is the width in character cells, wide (CJK) characters counting double.
    """
    try:
        raw = str(value)
    except Exception:        # e.g. an int beyond Python's int-to-str digit limit
        return None
    raw = " ".join(raw[:4 * _LABEL_MAX_CHARS].split())
    if not raw:
        return None
    text = raw
    if len(text) > _LABEL_MAX_CHARS:
        text = text[:_LABEL_MAX_CHARS - 1].rstrip() + "…"
    ascii_text = _ascii(raw)
    if len(ascii_text) > _LABEL_MAX_CHARS:
        ascii_text = ascii_text[:_LABEL_MAX_CHARS - 3].rstrip() + "..."
    cells = len(ascii_text)
    if not text.isascii():
        cells = max(cells, sum(
            0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in "WF"
            else 1 for ch in text))
    return (text, ascii_text, cells) if cells else None


def _decimals(step: float) -> int:
    """Decimals needed to write multiples of ``step`` (0.005 → 3, 0.5 → 1, 15 → 0)."""
    return next((d for d in range(4) if abs(step * 10 ** d - round(step * 10 ** d)) < 1e-9), 3)


def _degrees(value: float, decimals: int, positive: str, negative: str) -> str:
    """28.6 → '28.6°N', -179 → '179°W'; 0° and 180° have no hemisphere letter."""
    v = round(value, decimals)
    if v == 0:
        return "0°"
    if abs(v) == 180:
        return "180°"
    return f"{abs(v):.{decimals}f}°{positive if v > 0 else negative}"


class _Scene:
    """
    Validated options, the features and overlays in pixel coordinates, and
    the layout of the annotations (graticule, scale bar and labels) that
    the SVG and the PNG writers both draw from.
    """

    def __init__(self, fn: str, features: Any, width: Any, height: Any, title: Any,
                 overlays: Any, background: Any, padding: Any, point_radius: Any,
                 labels: Any, grid: Any, scale_bar: Any) -> None:
        self.width = _check_size(fn, "width", width)
        self.height = _check_size(fn, "height", height)
        self.padding = _check_padding(fn, padding, self.width, self.height)
        self.radius = _check_radius(fn, point_radius)
        self.background = _parse_color(fn, background)
        self.title = _clean_title(fn, title)
        self.label_key = _check_labels(fn, labels)
        show_grid = _check_flag(fn, "grid", grid)
        show_scale_bar = _check_flag(fn, "scale_bar", scale_bar)

        # A thick outline would swallow a tiny marker, so it scales down with it.
        self.point_stroke = min(_POINT_STROKE, self.radius / 2)
        self.text_color = _contrast_text(self.background)
        self.credit = self.width >= _CREDIT_MIN[0] and self.height >= _CREDIT_MIN[1]
        # A short image has no room for the title band (the map would be pushed
        # off the bottom): its title is drawn small, over the top of the map.
        self.big_title = self.height >= _TITLE_MIN_HEIGHT
        self.title_band = _TITLE_BAND if self.title and self.big_title else 0
        self.title_x = max(8, min(self.padding, 24)) if self.big_title else 4

        # Annotation style: the grid recedes, the scale bar reads like text.
        light = _is_light(self.background)
        self.grid_line = _GRID_LINE if light else _mix(self.background, _WHITE, 0.14)
        self.grid_text = _GRID_TEXT
        self.ink = _LABEL_TEXT if light else _LIGHT_TEXT
        # Labels: dark grey with a white halo; light with a dark halo on dark maps.
        self.label_ink, self.label_halo = ((_LABEL_TEXT, _WHITE) if light
                                           else (_LIGHT_TEXT, self.background))
        self.label_scale = 1 if self.width < 900 else 2     # feature labels
        self.small_scale = 1 if self.width < 1800 else 2    # grid and scale bar text

        geo_features, geo_overlays = _Layer(), _Layer()
        labelled: list[tuple[dict[str, Any], Any]] = []
        for g, props in _collect(fn, "features", features):
            _add_geometry(g, geo_features)
            if self.label_key is not None and isinstance(props, dict):
                value = props.get(self.label_key)
                if value is not None:
                    labelled.append((g, value))
        for g, _ in _collect(fn, "overlays", overlays):
            _add_geometry(g, geo_overlays)
        self.view: _View | None = None
        self.features, self.overlays = self._project(geo_features, geo_overlays)

        self.grid_x: list[int] = []               # meridians: pixel columns
        self.grid_y: list[int] = []               # parallels: pixel rows
        self.grid_labels: list[_Label] = []
        self.scale_bar: _ScaleBar | None = None
        self.labels: list[_Label] = []
        if self.view is not None:                 # an empty map gets no annotations
            self._layout(self.view, labelled, show_grid, show_scale_bar)

    def _project(self, feats: _Layer, overs: _Layer) -> tuple[_Layer, _Layer]:
        """Web Mercator, unwrapped across the antimeridian, fitted into the canvas."""
        lngs = [p[0] for layer in (feats, overs) for p in layer.positions()]
        if not lngs:
            return _Layer(), _Layer()

        # Same rule as map_builder._unwrap_lng: if the tightest longitude span
        # crosses ±180°, longitudes below its western edge move +360°. Only for
        # data that then fits in a hemisphere and has no shape running all the
        # way round (Antarctica, a world bbox): world-wide data stays a plain
        # world map instead of being folded or torn at an arbitrary meridian,
        # and each of its lines / polygons keeps its own longitudes in one piece.
        west, east = _lng_span(lngs)
        crossing = (west > east and east + 360.0 - west < 180.0
                    and not _spans_world(feats) and not _spans_world(overs))

        def point_lng(x: float) -> float:
            n = _normalize_lng(x)
            return n + 360.0 if crossing and n < west else n

        def shape(rings: list[list[_Pt]]) -> list[list[_Pt]]:
            if crossing:
                return [[(point_lng(x), y) for x, y in ring] for ring in rings]
            return _whole_turns(rings)          # all rings of a polygon move together

        def unwrap(layer: _Layer) -> _Layer:
            out = _Layer()
            out.points = [(point_lng(x), y) for x, y in layer.points]
            out.lines = [shape([line])[0] for line in layer.lines]
            out.polygons = [shape(rings) for rings in layer.polygons]
            return out

        def to_merc(p: _Pt) -> _Pt:
            return math.radians(p[0]), _mercator_y(p[1])

        mf, mo = unwrap(feats).mapped(to_merc), unwrap(overs).mapped(to_merc)
        xs = [p[0] for layer in (mf, mo) for p in layer.positions()]
        ys = [p[1] for layer in (mf, mo) for p in layer.positions()]
        min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)

        # Keep markers and line caps at the edge of the data inside the image.
        has_points = bool(mf.points or mo.points)
        has_strokes = bool(mf.lines or mf.polygons or mo.lines or mo.polygons)
        inset = self.padding + max(
            self.radius + self.point_stroke / 2 if has_points else 0.0,
            _LINE_WIDTH / 2 if has_strokes else 0.0,
        )
        avail_w = max(1.0, self.width - 2 * inset)
        avail_h = max(1.0, self.height - self.title_band - 2 * inset)
        span_x = max(max_x - min_x, _MIN_SPAN)
        span_y = max(max_y - min_y, _MIN_SPAN)
        scale = min(avail_w / span_x, avail_h / span_y)

        mid_x, mid_y = (min_x + max_x) / 2, (min_y + max_y) / 2
        ox = self.width / 2
        oy = self.title_band + (self.height - self.title_band) / 2
        self.view = _View(scale, mid_x, mid_y, ox, oy, point_lng, unwrap)

        def to_px(p: _Pt) -> _Pt:
            return ox + (p[0] - mid_x) * scale, oy - (p[1] - mid_y) * scale

        return mf.mapped(to_px), mo.mapped(to_px)

    # Annotation layout --------------------------------------------------------

    def _layout(self, view: _View, labelled: list[tuple[dict[str, Any], Any]],
                show_grid: bool, show_scale_bar: bool) -> None:
        """
        Place the scale bar, then the feature labels, then the grid labels,
        each clear of the title, the credit and everything placed before it.
        """
        w, h, top = self.width, self.height, self.title_band
        placer = _Placer(w, h)
        if self.title:
            if self.big_title:
                placer.block((0, 0, w, top))
            else:
                chars = len(_fit_text(_ascii(self.title), w - 2 * self.title_x, 1))
                placer.block((self.title_x - 1, 1, self.title_x + 6 * chars, 10))
        if self.credit:                           # (with room to its left: no run-on text)
            placer.block((w - 12 - _text_px(len(_CREDIT_TEXT), 1), h - 14, w - 5, h - 5))
        # Thumbnails get neither: a few lines and labels would bury the data.
        roomy = w >= _ANNOTATION_MIN[0] and h - top >= _ANNOTATION_MIN[1]
        if show_scale_bar and roomy:
            self.scale_bar = self._place_scale_bar(view, placer)
        if labelled:
            self.labels = self._place_labels(view, labelled, placer)
        if show_grid and roomy:
            self._place_grid(view, placer)

    def _place_scale_bar(self, view: _View, placer: _Placer) -> _ScaleBar | None:
        w, h, top = self.width, self.height, self.title_band
        # Over a continent the Mercator scale changes too much to mean anything.
        if view.lng(w) - view.lng(0) > _SCALE_BAR_MAX_DEG \
                or view.lat(top) - view.lat(h) > _SCALE_BAR_MAX_DEG:
            return None
        metres_per_px = math.cos(math.radians(view.lat(view.oy))) * _EARTH_RADIUS_M / view.scale
        shortest, longest = _SCALE_BAR_PX
        lengths = [m * 10 ** e for e in range(9) for m in (1, 2, 5)]
        fitting = [m for m in lengths if m / metres_per_px <= longest]
        if not fitting or fitting[-1] / metres_per_px < shortest:
            return None
        metres = fitting[-1]
        length = round(metres / metres_per_px)
        text = f"{metres} m" if metres < 1000 else f"{metres // 1000} km"
        s = self.small_scale
        x0, y = 8, h - 8                          # bar bottom level with the credit's baseline
        tw = _text_px(len(text), s)
        label = _Label(x0 + (length - tw) // 2, y - 3 - 7 * s, tw, text, text, "middle")
        if not placer.place((x0 - 1, label.y - 1, x0 + length + 1, y + 3)):
            return None
        return _ScaleBar(x0, x0 + length, y, label)

    def _place_labels(self, view: _View, labelled: list[tuple[dict[str, Any], Any]],
                      placer: _Placer) -> list[_Label]:
        """
        Greedy, in feature order. A point's label goes to the right of its
        marker, or to the left when the right is taken; a label is skipped if
        both would overlap a label placed before it, cover a marker or leave
        the canvas. Other geometries get one label centred on their
        representative point. A spot that already has a label gets no second.
        """
        w, h = self.width, self.height
        s = self.label_scale
        th = 7 * s
        gap = _Placer.GAP
        # From the marker's pixel to the text: radius + outline + a small gap
        # (so a label clears its own marker by the same gap as any other).
        beside = math.ceil(self.radius + self.point_stroke / 2 + 0.5 + 2 * s)
        markers = _Markers(self.features.points + self.overlays.points, self.radius,
                           self.point_stroke)
        spots: set[tuple[int, int]] = set()
        out: list[_Label] = []
        for geometry, value in labelled:
            texts: tuple[str, str, int] | None = None
            for px, py, is_point in self._anchors(view, geometry):
                ix, iy = math.floor(px), math.floor(py)
                if not (0 <= px < w and 0 <= py < h) or (ix, iy) in spots:
                    continue
                if texts is None:
                    texts = _label_texts(value) or ("", "", 0)
                text, ascii_text, cells = texts
                if not cells:
                    break
                tw = _text_px(cells, s)
                y = iy - th // 2
                # (x, SVG text-anchor, clearance to the left): a label on the
                # left must not sit where it would read as another point's label.
                options = ([(ix + beside, "start", 0), (ix + 1 - beside - tw, "end", beside)]
                           if is_point else [(round(px - tw / 2), "middle", 0)])
                for x, anchor, clear in options:
                    box = (x - 1, y - 1, x + tw + 1, y + th + 1)
                    if markers.touch((box[0] - clear, box[1], box[2], box[3]), gap) \
                            or not placer.place(box):
                        continue
                    out.append(_Label(x, y, tw, text, ascii_text, anchor))
                    spots.add((ix, iy))
                    if len(out) >= _LABEL_LIMIT:
                        return out
                    break
        return out

    @staticmethod
    def _anchors(view: _View, geometry: dict[str, Any]) -> list[tuple[float, float, bool]]:
        """
        Where a feature's label goes: ``(x, y, is_point)`` for each point
        (of a Point, a MultiPoint or a GeometryCollection holding points),
        else one anchor at the geometry's representative point, moved by
        whole turns onto the shape as it is drawn on this map.
        """
        layer = _Layer()
        _add_geometry(geometry, layer)
        if layer.points:
            return [(*view.px(view.point_lng(x), y), True) for x, y in layer.points]
        xs = [p[0] for p in view.unwrap(layer).positions()]
        rep = _representative_point(geometry) if xs else None
        if rep is None:
            return []
        lat, lng = rep
        lo, hi = min(xs), max(xs)
        mid = (lo + hi) / 2
        lng += 360.0 * round((mid - lng) / 360.0)
        if not lo <= lng <= hi:
            lng = mid
        return [(*view.px(lng, lat), False)]

    def _place_grid(self, view: _View, placer: _Placer) -> None:
        """
        Meridians and parallels at a 'nice' step (3-7 lines across the larger
        extent), labelled along the bottom and left edges where there is room.
        """
        w, h, top = self.width, self.height, self.title_band
        s = self.small_scale
        th = 7 * s
        west, east = view.lng(0), view.lng(w)
        centre = (west + east) / 2
        west, east = max(west, centre - 180.0), min(east, centre + 180.0)
        north, south = min(_MAX_LAT, view.lat(top)), max(-_MAX_LAT, view.lat(h))
        extent = max(east - west, north - south)
        step = next((st for st in _GRID_STEPS if extent / st <= _GRID_MAX_LINES), _GRID_STEPS[-1])
        decimals = _decimals(step)

        lng_labels: list[_Label] = []
        for k in range(math.ceil(west / step - 1e-9), math.floor(east / step + 1e-9) + 1):
            lng = round(k * step, decimals)
            x = math.floor(view.px(lng, 0.0)[0])
            if lng >= centre + 180.0 or not 0 <= x < w:   # (no meridian drawn twice)
                continue
            self.grid_x.append(x)
            # Unwrapped longitudes (181°) are written as they are known (179°W).
            text = _degrees(_normalize_lng(lng), decimals, "E", "W")
            tw = _text_px(len(text), s)
            lng_labels.append(_Label(x + 3, h - 6 - th, tw, text, text))
        lat_labels: list[_Label] = []
        for k in range(math.ceil(south / step - 1e-9), math.floor(north / step + 1e-9) + 1):
            lat = round(k * step, decimals)
            y = math.floor(view.px(0.0, lat)[1])
            if not top <= y < h:
                continue
            self.grid_y.append(y)
            text = _degrees(lat, decimals, "N", "S")
            tw = _text_px(len(text), s)
            lat_labels.append(_Label(5, y - 2 - th, tw, text, text))   # just above its line
        for label in lng_labels + lat_labels:
            if placer.place((label.x - 1, label.y - 1, label.x + label.width + 1,
                             label.y + th + 1)):
                self.grid_labels.append(label)


def _is_light(bg: _RGB) -> bool:
    return 0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2] >= 140


def _contrast_text(bg: _RGB) -> _RGB:
    """Dark text on light backgrounds, light text on dark ones."""
    return _DARK_TEXT if _is_light(bg) else _LIGHT_TEXT


def _mix(a: _RGB, b: _RGB, t: float) -> _RGB:
    """``a`` moved a fraction ``t`` of the way to ``b``."""
    return (round(a[0] + (b[0] - a[0]) * t), round(a[1] + (b[1] - a[1]) * t),
            round(a[2] + (b[2] - a[2]) * t))


def _hex(rgb: _RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


# ─────────────────────────────────────────────────────────────────────────────
# SVG
# ─────────────────────────────────────────────────────────────────────────────

def _num(v: float) -> str:
    """Round to 2 decimals and drop trailing zeros (12.50 → 12.5, -0.00 → 0)."""
    s = f"{v:.2f}"
    if s.endswith("00"):
        s = s[:-3]
    elif s.endswith("0"):
        s = s[:-1]
    return "0" if s == "-0" else s


def _xml_text(text: str) -> str:
    """
    XML-escaped ``text`` in pure ASCII: characters XML cannot hold become
    U+FFFD and every non-ASCII character a numeric reference (``&#26481;``),
    so the SVG stays valid with any ASCII-compatible save encoding (e.g. a
    plain ``open(path, "w")`` on Windows, which is not UTF-8).
    """
    text = _XML_INVALID.sub("\ufffd", text)
    text = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;").replace("'", "&#39;"))
    return text.encode("ascii", "xmlcharrefreplace").decode("ascii")


def _path_data(rings: list[list[_Pt]], closed: bool) -> str:
    parts: list[str] = []
    for ring in rings:
        coords: list[str] = []
        last = ""
        for x, y in ring:
            pair = f"{_num(x)} {_num(y)}"
            if pair != last:        # drop vertices that round onto the previous one
                coords.append(pair)
                last = pair
        if closed and len(coords) > 1 and coords[-1] == coords[0]:
            coords.pop()
        if not coords:
            continue
        path = "M" + coords[0]
        if len(coords) > 1:
            path += "L" + " ".join(coords[1:])
        parts.append(path + ("Z" if closed else ""))
    return "".join(parts)


def _svg_points(points: list[_Pt], scene: _Scene, gid: str) -> str:
    r = _num(scene.radius)
    circles = "".join(f'<circle cx="{_num(x)}" cy="{_num(y)}" r="{r}"/>' for x, y in points)
    return (f'<g id="{gid}" fill="{_hex(_TEAL)}" stroke="{_hex(_WHITE)}" '
            f'stroke-width="{_num(scene.point_stroke)}">{circles}</g>')


def _svg_shapes(layer: _Layer, gid: str, stroke: _RGB, fill_opacity: float,
                dashed: bool) -> str:
    paths = [f'<path fill-rule="evenodd" d="{d}"/>'
             for d in (_path_data(rings, closed=True) for rings in layer.polygons) if d]
    paths += [f'<path fill="none" d="{d}"/>'
              for d in (_path_data([line], closed=False) for line in layer.lines) if d]
    if not paths:
        return ""
    dash = f' stroke-dasharray="{_num(_DASH[0])} {_num(_DASH[1])}"' if dashed else ""
    color = _hex(stroke)
    return (f'<g id="{gid}" fill="{color}" fill-opacity="{fill_opacity:g}" stroke="{color}" '
            f'stroke-width="{_num(_LINE_WIDTH)}"{dash} stroke-linejoin="round" '
            f'stroke-linecap="round">{"".join(paths)}</g>')


def _svg_text_style(size: float, fill: _RGB, halo: _RGB) -> str:
    """Text attributes with a halo: a ``halo``-coloured stroke painted under the fill."""
    return (f'font-family="{_FONT_STACK}" font-size="{_num(size)}" fill="{_hex(fill)}" '
            f'stroke="{_hex(halo)}" stroke-width="3" stroke-linejoin="round" '
            f'paint-order="stroke"')


def _svg_texts(labels: list[_Label], baseline: float) -> str:
    """``<text>`` elements; ``baseline`` is measured from the top of the glyph box."""
    parts = []
    for label in labels:
        if label.anchor == "middle":
            x, anchor = label.x + label.width / 2, ' text-anchor="middle"'
        elif label.anchor == "end":
            x, anchor = label.x + label.width, ' text-anchor="end"'
        else:
            x, anchor = label.x, ""
        parts.append(f'<text x="{_num(x)}" y="{_num(label.y + baseline)}"{anchor}>'
                     f"{_xml_text(label.text)}</text>")
    return "".join(parts)


def _svg_grid(scene: _Scene) -> str:
    """Graticule lines (crisp 1 px, on the same pixels as the PNG) and their labels."""
    if not scene.grid_x and not scene.grid_y:
        return ""
    w, h, top = scene.width, scene.height, scene.title_band
    d = "".join(f"M{_num(x + 0.5)} {top}L{_num(x + 0.5)} {h}" for x in scene.grid_x)
    d += "".join(f"M0 {_num(y + 0.5)}L{w} {_num(y + 0.5)}" for y in scene.grid_y)
    out = (f'<g id="qg-grid" fill="none" stroke="{_hex(scene.grid_line)}" stroke-width="1" '
           f'shape-rendering="crispEdges"><path d="{d}"/></g>')
    if scene.grid_labels:
        s = scene.small_scale
        out += (f'\n<g id="qg-grid-labels" '
                f'{_svg_text_style(10 * s, scene.grid_text, scene.background)}>'
                f"{_svg_texts(scene.grid_labels, 7 * s)}</g>")
    return out


def _svg_scale_bar(scene: _Scene) -> str:
    bar = scene.scale_bar
    if bar is None:
        return ""
    s = scene.small_scale
    halo = "".join(f'<rect x="{x - 1}" y="{y - 1}" width="{rw + 2}" height="{rh + 2}"/>'
                   for x, y, rw, rh in bar.rects())
    ink = "".join(f'<rect x="{x}" y="{y}" width="{rw}" height="{rh}"/>'
                  for x, y, rw, rh in bar.rects())
    return (f'<g id="qg-scale-bar"><g fill="{_hex(scene.background)}">{halo}</g>'
            f'<g fill="{_hex(scene.ink)}">{ink}</g>'
            f'<g {_svg_text_style(10 * s, scene.ink, scene.background)}>'
            f"{_svg_texts([bar.label], 7 * s)}</g></g>")


def _svg_labels(scene: _Scene) -> str:
    if not scene.labels:
        return ""
    s = scene.label_scale
    size = _LABEL_FONT[s - 1]
    # Capitals centred on the glyph box (and so on the marker): cap height ≈ 0.72 em.
    baseline = 3.5 * s + 0.36 * size
    return (f'<g id="qg-labels" {_svg_text_style(size, scene.label_ink, scene.label_halo)}>'
            f"{_svg_texts(scene.labels, baseline)}</g>")


def _svg(scene: _Scene) -> str:
    w, h = scene.width, scene.height
    title = scene.title or "QOREgeo map"
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}">\n',
        f"<title>{_xml_text(title)}</title>\n",
        f"<desc>Generated by QOREgeo {_xml_text(__version__)}</desc>\n",
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="{_hex(scene.background)}"/>\n',
    ]
    feats, overs = scene.features, scene.overlays
    layers = [
        _svg_grid(scene),
        _svg_shapes(feats, "qg-shapes", _TEAL, _POLYGON_FILL_OPACITY, dashed=False),
        _svg_points(feats.points, scene, "qg-points") if feats.points else "",
        _svg_shapes(overs, "qg-overlays", _GOLD, _OVERLAY_FILL_OPACITY, dashed=True),
        _svg_points(overs.points, scene, "qg-overlay-points") if overs.points else "",
        _svg_scale_bar(scene),
        _svg_labels(scene),
    ]
    out += [layer + "\n" for layer in layers if layer]
    if scene.title:
        y, size = (20, 16) if scene.big_title else (9, 9)
        out.append(
            f'<text id="qg-title" x="{scene.title_x}" y="{y}" font-family="{_FONT_STACK}" '
            f'font-size="{size}" font-weight="600" fill="{_hex(scene.text_color)}">'
            f"{_xml_text(scene.title)}</text>\n"
        )
    if scene.credit:
        out.append(
            f'<text id="qg-credit" x="{w - 6}" y="{h - 6}" text-anchor="end" '
            f'font-family="{_FONT_STACK}" font-size="10" fill="{_hex(_CREDIT)}">'
            f"{_CREDIT_TEXT}</text>\n"
        )
    out.append("</svg>\n")
    return "".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# PNG rasteriser
# ─────────────────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=2048)
def _tables(rgb: _RGB, level: int) -> _Tables:
    """Per-channel lookup tables blending ``rgb`` at ``level / _LEVELS`` opacity."""
    keep = _LEVELS - level
    half = _LEVELS // 2
    r, g, b = (bytes((v * keep + c * level + half) // _LEVELS for v in range(256)) for c in rgb)
    return r, g, b


def _level(alpha: float) -> int:
    return max(0, min(_LEVELS, int(alpha * _LEVELS + 0.5)))


class _Canvas:
    """An opaque RGBA image in a bytearray, blended through lookup tables."""

    def __init__(self, width: int, height: int, background: _RGB) -> None:
        self.w, self.h = width, height
        self.buf = bytearray(bytes((*background, 255))) * (width * height)

    # Blending ---------------------------------------------------------------

    def blend_span(self, y: int, x0: int, x1: int, t: _Tables) -> None:
        """Blend pixels x0..x1 (inclusive) of row y with three C-level translates."""
        buf = self.buf
        s = (y * self.w + x0) * 4
        e = (y * self.w + x1 + 1) * 4
        buf[s:e:4] = buf[s:e:4].translate(t[0])
        buf[s + 1:e:4] = buf[s + 1:e:4].translate(t[1])
        buf[s + 2:e:4] = buf[s + 2:e:4].translate(t[2])

    def blend_coverage(self, cov: dict[int, float], rgb: _RGB, alpha: float = 1.0) -> None:
        """Blend ``rgb`` into each pixel index of ``cov`` at coverage × alpha."""
        buf = self.buf
        cache: dict[int, _Tables] = {}
        for idx, c in cov.items():
            level = _level(c * alpha)
            if not level:
                continue
            t = cache.get(level)
            if t is None:
                t = cache[level] = _tables(rgb, level)
            o = idx * 4
            buf[o] = t[0][buf[o]]
            buf[o + 1] = t[1][buf[o + 1]]
            buf[o + 2] = t[2][buf[o + 2]]

    def fill_rect(self, x: int, y: int, w: int, h: int, rgb: _RGB) -> None:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.w, x + w), min(self.h, y + h)
        if x0 >= x1 or y0 >= y1:
            return
        run = bytes((*rgb, 255)) * (x1 - x0)
        for yy in range(y0, y1):
            o = (yy * self.w + x0) * 4
            self.buf[o:o + len(run)] = run

    # Shapes -----------------------------------------------------------------

    def fill_polygon(self, rings: list[list[_Pt]], rgb: _RGB, alpha: float) -> None:
        """Scanline fill with the even-odd rule over all rings together."""
        level = _level(alpha)
        if not level:
            return
        t = _tables(rgb, level)
        edges: list[tuple[float, float, float, float]] = []
        for ring in rings:
            for i in range(len(ring)):              # ring[-1] → ring[0] closes it
                x0, y0 = ring[i - 1]
                x1, y1 = ring[i]
                if y0 == y1:
                    continue
                if y0 > y1:
                    x0, y0, x1, y1 = x1, y1, x0, y0
                edges.append((y0, y1, x0, (x1 - x0) / (y1 - y0)))
        if not edges:
            return
        edges.sort()
        top = max(0, math.ceil(edges[0][0] - 0.5))
        bottom = min(self.h - 1, math.ceil(max(e[1] for e in edges) - 0.5) - 1)
        active: list[tuple[float, float, float, float]] = []
        k, n, last_x = 0, len(edges), self.w - 1
        for py in range(top, bottom + 1):
            cy = py + 0.5                           # sample at pixel centres
            while k < n and edges[k][0] <= cy:
                active.append(edges[k])
                k += 1
            active = [e for e in active if e[1] > cy]
            xs = sorted(e[2] + (cy - e[0]) * e[3] for e in active)
            for j in range(0, len(xs) - 1, 2):
                xa = max(0, math.ceil(xs[j] - 0.5))
                xb = min(last_x, math.ceil(xs[j + 1] - 0.5) - 1)
                if xa <= xb:
                    self.blend_span(py, xa, xb, t)

    def stroke_coverage(self, paths: list[list[_Pt]], closed: bool, width: float,
                        dash: tuple[float, float] | None = None) -> dict[int, float]:
        """
        Anti-aliased coverage of thick polylines (round caps and joins), keyed
        by pixel index. Each pixel keeps its highest coverage, so joins and
        overlapping segments are not blended twice.
        """
        cov: dict[int, float] = {}
        reach = width / 2 + 0.5                     # coverage reaches 0 at this distance
        w, h = self.w, self.h
        period = dash[0] + dash[1] if dash else 0.0
        on = dash[0] if dash else 0.0
        for pts in paths:
            seq = pts + [pts[0]] if closed and pts[0] != pts[-1] else pts
            travelled = 0.0
            for i in range(1, len(seq)):
                x0, y0 = seq[i - 1]
                x1, y1 = seq[i]
                dx, dy = x1 - x0, y1 - y0
                l2 = dx * dx + dy * dy
                if l2 < 1e-12:
                    continue
                length = math.sqrt(l2)
                lo_x, hi_x = min(x0, x1) - reach, max(x0, x1) + reach
                row0 = max(0, math.floor(min(y0, y1) - reach))
                row1 = min(h - 1, math.floor(max(y0, y1) + reach))
                # Within a row, the stroke lies inside the band around the
                # infinite line: |x - x_line| <= reach * length / |dy|.
                ext = reach * length / abs(dy) if abs(dy) > 1e-9 else math.inf
                for py in range(row0, row1 + 1):
                    cy = py + 0.5
                    if ext != math.inf:
                        xc = x0 + (cy - y0) * dx / dy
                        xa, xb = max(lo_x, xc - ext), min(hi_x, xc + ext)
                    else:
                        xa, xb = lo_x, hi_x
                    col0 = max(0, math.ceil(xa - 0.5))
                    col1 = min(w - 1, math.floor(xb - 0.5))
                    ry = cy - y0
                    base = py * w
                    for px in range(col0, col1 + 1):
                        rx = px + 0.5 - x0
                        t = (rx * dx + ry * dy) / l2
                        t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
                        ex, ey = rx - t * dx, ry - t * dy
                        c = reach - math.sqrt(ex * ex + ey * ey)
                        if c <= 0.0:
                            continue
                        if dash and (travelled + t * length) % period >= on:
                            continue
                        if c > 1.0:
                            c = 1.0
                        idx = base + px
                        if cov.get(idx, 0.0) < c:
                            cov[idx] = c
                travelled += length
        return cov

    def markers(self, points: list[_Pt], radius: float, stroke: float) -> None:
        """Teal discs with a white outline, stamped at whole-pixel centres."""
        reach, runs, edges = _marker_stamp(radius, stroke)
        w, h, buf = self.w, self.h, self.buf
        row = w * 4
        run_offsets = [((dy * w + dx0) * 4, data) for dy, dx0, data in runs]
        edge_offsets = [((dy * w + dx) * 4, t) for dy, dx, t in edges]
        # Points that land on the same pixel draw identically: stamp once.
        centres = dict.fromkeys((math.floor(x), math.floor(y)) for x, y in points)
        for ix, iy in centres:
            if reach <= ix < w - reach and reach <= iy < h - reach:
                base = iy * row + ix * 4
                for off, data in run_offsets:
                    o = base + off
                    buf[o:o + len(data)] = data
                for off, t in edge_offsets:
                    o = base + off
                    buf[o] = t[0][buf[o]]
                    buf[o + 1] = t[1][buf[o + 1]]
                    buf[o + 2] = t[2][buf[o + 2]]
                continue
            # Near the border: clip each run to the canvas (still one slice copy).
            for dy, dx0, data in runs:
                y = iy + dy
                if not 0 <= y < h:
                    continue
                x0 = ix + dx0
                a, b = max(0, x0), min(w, x0 + len(data) // 4)
                if a < b:
                    o = (y * w + a) * 4
                    buf[o:o + (b - a) * 4] = data[(a - x0) * 4:(b - x0) * 4]
            for dy, dx, t in edges:
                x, y = ix + dx, iy + dy
                if 0 <= x < w and 0 <= y < h:
                    o = (y * w + x) * 4
                    buf[o] = t[0][buf[o]]
                    buf[o + 1] = t[1][buf[o + 1]]
                    buf[o + 2] = t[2][buf[o + 2]]

    def text(self, x: int, y: int, text: str, scale: int, rgb: _RGB) -> None:
        """Draw ASCII ``text`` with the 5x7 bitmap font; top-left at (x, y)."""
        for ch in text:
            glyph = _glyph(ch)
            for col in range(5):
                bits = glyph[col]
                for r in range(7):
                    if bits >> r & 1:
                        self.fill_rect(x + col * scale, y + r * scale, scale, scale, rgb)
            x += 6 * scale

    def label(self, x: int, y: int, text: str, scale: int, rgb: _RGB, halo: _RGB) -> None:
        """
        ``text`` in the bitmap font with a 1 px ``halo`` all round (the glyph
        mask grown by one pixel in every direction, drawn first); top-left
        of the glyphs at (x, y). Rows are written as runs, one slice each.
        """
        rows = _text_rows(text, scale)
        if not rows:
            return
        width = len(rows[0])
        bits = [0, *(int(row, 2) << 1 for row in rows), 0]     # 1 px margin all round
        grown = []
        for i in range(len(bits)):
            v = bits[i] | (bits[i - 1] if i else 0) | (bits[i + 1] if i + 1 < len(bits) else 0)
            grown.append(format(v | v << 1 | v >> 1, f"0{width + 2}b"))
        self._runs(x - 1, y - 1, grown, halo)
        self._runs(x, y, rows, rgb)

    def _runs(self, x: int, y: int, rows: list[str], rgb: _RGB) -> None:
        """Paint the '1' runs of ``rows`` (one string per pixel row) from (x, y)."""
        pixel = bytes((*rgb, 255))
        w, buf = self.w, self.buf
        for j, row in enumerate(rows):
            yy = y + j
            if not 0 <= yy < self.h:
                continue
            base = yy * w
            for m in _ONES.finditer(row):
                a, b = max(0, x + m.start()), min(w, x + m.end())
                if a < b:
                    buf[(base + a) * 4:(base + b) * 4] = pixel * (b - a)

    def png(self, title: str | None) -> bytes:
        # Filter byte 0 + each row, compressed in ~1 MB batches: no second
        # full-size copy of the image (an 8192x8192 canvas is 256 MB).
        stride = self.w * 4
        comp = zlib.compressobj(6)
        idat: list[bytes] = []
        batch = bytearray()
        with memoryview(self.buf) as view:
            for y in range(self.h):
                batch += b"\x00"
                batch += view[y * stride:(y + 1) * stride]
                if len(batch) >= 1 << 20:
                    idat.append(comp.compress(batch))
                    batch.clear()
        idat.append(comp.compress(batch))
        idat.append(comp.flush())
        chunks = [
            _chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 6, 0, 0, 0)),
            _chunk(b"tEXt", b"Software\x00QOREgeo " + __version__.encode("latin-1", "replace")),
        ]
        if title:
            text = title.replace("\x00", "").encode("utf-8", "replace")
            chunks.append(_chunk(b"iTXt", b"Title\x00\x00\x00\x00\x00" + text))
        chunks.append(_chunk(b"IDAT", b"".join(idat)))
        chunks.append(_chunk(b"IEND", b""))
        return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(data, zlib.crc32(kind)) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


@functools.lru_cache(maxsize=32)
def _marker_stamp(
    radius: float, stroke: float,
) -> tuple[int, list[tuple[int, int, bytes]], list[tuple[int, int, _Tables]]]:
    """
    Pre-rendered marker: ``(reach, runs, edges)``. ``runs`` are opaque pixel
    runs ``(dy, dx_start, rgba_bytes)`` copied straight into the canvas;
    ``edges`` are the anti-aliased rim pixels ``(dy, dx, tables)`` to blend.
    The outline straddles the circle edge, as an SVG stroke does.
    """
    outer, inner = radius + stroke / 2, radius - stroke / 2
    reach = math.ceil(outer + 0.5)
    runs: list[tuple[int, int, bytes]] = []
    edges: list[tuple[int, int, _Tables]] = []
    for dy in range(-reach, reach + 1):
        start, data = 0, bytearray()
        for dx in range(-reach, reach + 1):
            d = math.hypot(dx, dy)
            c_out = min(1.0, max(0.0, outer + 0.5 - d))
            if c_out <= 0.0:
                continue
            c_in = min(1.0, max(0.0, inner + 0.5 - d)) if inner > 0 else 0.0
            # Premultiplied mix: teal fill under a white outline.
            mix = [(t * c_in + wv * (c_out - c_in)) / c_out
                   for t, wv in zip(_TEAL, _WHITE, strict=True)]
            rgb: _RGB = (round(mix[0]), round(mix[1]), round(mix[2]))
            if c_out >= 1.0:
                if not data:
                    start = dx
                data += bytes((*rgb, 255))
            else:
                level = _level(c_out)
                if level:
                    edges.append((dy, dx, _tables(rgb, level)))
        if data:
            runs.append((dy, start, bytes(data)))
    return reach, runs, edges


# ─────────────────────────────────────────────────────────────────────────────
# Bitmap font: classic 5x7, printable ASCII (0x20-0x7E), 5 column bytes per
# glyph, bit 0 = top row.
# ─────────────────────────────────────────────────────────────────────────────

_FONT = bytes.fromhex(
    "000000000000005f00000007000700147f147f14242a7f2a12231308646236495522500005030000"
    "001c2241000041221c0014083e081408083e08080050300000080808080800606000002010080402"
    "3e5149453e00427f400042615149462141454b311814127f1027454545393c4a4949300171090503"
    "3649494936064949291e003636000000563600000814224100141414141400412214080201510906"
    "324979413e7e1111117e7f494949363e414141227f4141221c7f494949417f090909013e4149497a"
    "7f0808087f00417f41002040413f017f081422417f404040407f020c027f7f0408107f3e4141413e"
    "7f090909063e4151215e7f09192946464949493101017f01013f4040403f1f2040201f3f4038403f"
    "631408146307087008076151494543007f41410002040810200041417f0004020102044040404040"
    "000102040020545454787f484444383844444420384444487f3854545418087e0901020c5252523e"
    "7f0804047800447d40002040443d007f1028440000417f40007c041804787c080404783844444438"
    "7c14141408081414187c7c080404084854545420043f4440203c4040207c1c2040201c3c4030403c"
    "44281028440c5050503c4464544c44000836410000007f000000413608001008081008"
)


_DEGREE = bytes((0x02, 0x05, 0x05, 0x02, 0x00))    # '°' for the grid labels
_ONES = re.compile("1+")


def _glyph(ch: str) -> bytes:
    if ch == "°":
        return _DEGREE
    code = ord(ch)
    if not 0x20 <= code <= 0x7E:
        code = 0x3F                                 # '?'
    i = (code - 0x20) * 5
    return _FONT[i:i + 5]


@functools.lru_cache(maxsize=1024)
def _glyph_rows(ch: str, scale: int) -> tuple[str, ...]:
    """A glyph and its 1-column gap as '0'/'1' strings, one per pixel row."""
    glyph = _glyph(ch)
    rows: list[str] = []
    for r in range(7):
        row = "".join(("1" if glyph[c] >> r & 1 else "0") * scale for c in range(5))
        rows += [row + "0" * scale] * scale
    return tuple(rows)


def _text_rows(text: str, scale: int) -> list[str]:
    """The pixel rows of ``text`` (without the gap after the last glyph)."""
    if not text:
        return []
    glyphs = [_glyph_rows(ch, scale) for ch in text]
    return ["".join(g[r] for g in glyphs)[:-scale] for r in range(7 * scale)]


# Typographic punctuation and letters NFKD leaves alone, with plain-ASCII
# stand-ins (so '\u0141\u00f3d\u017a', 'Troms\u00f8' and 'Gie\u00dfen' keep their letters).
_PUNCTUATION = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u2018": "'", "\u2019": "'", "\u201a": ",", "\u201c": '"', "\u201d": '"', "\u201e": '"',
    "\u00ab": '"', "\u00bb": '"', "\u2032": "'", "\u2033": '"',
    "\u2022": "*", "\u00b7": ".", "\u00d7": "x", "\u00a0": " ",
    "\u00df": "ss", "\u1e9e": "SS", "\u00e6": "ae", "\u00c6": "AE", "\u0153": "oe", "\u0152": "OE",
    "\u00f8": "o", "\u00d8": "O", "\u0142": "l", "\u0141": "L", "\u0111": "d", "\u0110": "D",
    "\u00f0": "d", "\u00d0": "D", "\u00fe": "th", "\u00de": "Th", "\u0131": "i",
})


def _ascii(text: str) -> str:
    """Fold to printable ASCII for the bitmap font: 'Zürich' → 'Zurich', '東京' → '??'."""
    text = " ".join(text.translate(_PUNCTUATION).split())   # tabs / newlines → spaces
    folded = unicodedata.normalize("NFKD", text)
    out = []
    for ch in folded:
        if " " <= ch <= "~":
            out.append(ch)
        elif not unicodedata.combining(ch):
            out.append("?")
    return "".join(out)


def _fit_text(text: str, max_width: int, scale: int) -> str:
    advance = 6 * scale
    max_chars = max(0, (max_width + scale) // advance)
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..." if max_chars > 3 else text[:max_chars]


def _png(scene: _Scene) -> bytes:
    canvas = _Canvas(scene.width, scene.height, scene.background)
    feats, overs = scene.features, scene.overlays

    # The graticule goes under the data; its labels get a background-coloured halo.
    top, gs = scene.title_band, scene.small_scale
    for x in scene.grid_x:
        canvas.fill_rect(x, top, 1, scene.height - top, scene.grid_line)
    for y in scene.grid_y:
        canvas.fill_rect(0, y, scene.width, 1, scene.grid_line)
    for label in scene.grid_labels:
        canvas.label(label.x, label.y, label.ascii, gs, scene.grid_text, scene.background)

    for rings in feats.polygons:
        canvas.fill_polygon(rings, _TEAL, _POLYGON_FILL_OPACITY)
        canvas.blend_coverage(canvas.stroke_coverage(rings, True, _LINE_WIDTH), _TEAL)
    if feats.lines:
        canvas.blend_coverage(canvas.stroke_coverage(feats.lines, False, _LINE_WIDTH), _TEAL)
    if feats.points:
        canvas.markers(feats.points, scene.radius, scene.point_stroke)

    for rings in overs.polygons:
        canvas.fill_polygon(rings, _GOLD, _OVERLAY_FILL_OPACITY)
        canvas.blend_coverage(canvas.stroke_coverage(rings, True, _LINE_WIDTH, _DASH), _GOLD)
    if overs.lines:
        canvas.blend_coverage(canvas.stroke_coverage(overs.lines, False, _LINE_WIDTH, _DASH),
                              _GOLD)
    if overs.points:
        canvas.markers(overs.points, scene.radius, scene.point_stroke)

    bar = scene.scale_bar
    if bar is not None:
        for x, y, rw, rh in bar.rects():
            canvas.fill_rect(x - 1, y - 1, rw + 2, rh + 2, scene.background)
        for x, y, rw, rh in bar.rects():
            canvas.fill_rect(x, y, rw, rh, scene.ink)
        canvas.label(bar.label.x, bar.label.y, bar.label.ascii, gs, scene.ink, scene.background)
    for label in scene.labels:
        canvas.label(label.x, label.y, label.ascii, scene.label_scale, scene.label_ink,
                     scene.label_halo)

    if scene.title:
        x, scale = scene.title_x, 2 if scene.big_title else 1
        top = (_TITLE_BAND - 7 * scale) // 2 if scene.big_title else 2
        text = _fit_text(_ascii(scene.title), scene.width - 2 * x, scale)
        canvas.text(x, top, text, scale, scene.text_color)
    if scene.credit:
        width = len(_CREDIT_TEXT) * 6 - 1
        canvas.text(scene.width - 6 - width, scene.height - 6 - 7, _CREDIT_TEXT, 1, _CREDIT)
    return canvas.png(scene.title)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def render_svg(
    features: list[dict[str, Any]],
    width: int = 800,
    height: int = 600,
    title: str | None = None,
    overlays: list[dict[str, Any]] | None = None,
    background: str = "#ffffff",
    padding: int = 24,
    point_radius: float = 4.0,
    labels: str | None = None,
    grid: bool = True,
    scale_bar: bool = True,
) -> str:
    """
    Render GeoJSON features as a standalone SVG 1.1 document.

    Args:
        features    : GeoJSON Feature dicts (any geometry type; null geometries
                      are skipped). Bare geometries and a FeatureCollection
                      are accepted too.
        width       : Image width in pixels (16 to 8192).
        height      : Image height in pixels (16 to 8192).
        title       : Optional title drawn above the map (XML-escaped). On
                      images under 56 px tall it is set small over the map.
        overlays    : GeoJSON geometries / Features / FeatureCollections drawn
                      as dashed gold outlines, e.g. ``[geo.buffer(p, 10)]``.
        background  : Background colour, ``'#rrggbb'`` or ``'#rgb'``.
        padding     : Space in pixels between the data and the image edge.
        point_radius: Radius of point markers in pixels (0.5 to 50).
        labels      : Name of a feature property to label the features with,
                      e.g. ``"name"``: its value is written to the right of
                      each point (centred on lines and polygons). Labels that
                      would overlap one another or leave the image are
                      skipped (first come, first placed), long ones are cut
                      to 40 characters, and at most 300 are drawn. Features
                      without the property (or with ``None``) get no label.
        grid        : Draw a light latitude / longitude graticule with its
                      values on the left and bottom edges (e.g. ``28.6°N``,
                      ``77.2°E``) so the reader can tell where the map is.
        scale_bar   : Draw a scale bar (e.g. ``500 m``, ``20 km``) in the
                      bottom-left corner; left out on views wider or taller
                      than about 60°, where a single scale is meaningless.

    Returns:
        The SVG document as a string. It is pure ASCII (other characters
        are written as ``&#...;`` references), so it stays valid UTF-8 XML
        when saved with any ASCII-compatible encoding, e.g. a plain
        ``open(path, "w")`` on Windows.

    Raises:
        InvalidArgumentError: if an option is out of range or a feature /
            overlay is not valid GeoJSON.

    Example:
        >>> svg = render_svg(geo.features, title="Stores")
        >>> open("stores.svg", "w", encoding="utf-8").write(svg)
    """
    scene = _Scene("render_svg", features, width, height, title, overlays,
                   background, padding, point_radius, labels, grid, scale_bar)
    return _svg(scene)


def render_png(
    features: list[dict[str, Any]],
    width: int = 800,
    height: int = 600,
    title: str | None = None,
    overlays: list[dict[str, Any]] | None = None,
    background: str = "#ffffff",
    padding: int = 24,
    point_radius: float = 4.0,
    labels: str | None = None,
    grid: bool = True,
    scale_bar: bool = True,
) -> bytes:
    """
    Render GeoJSON features as a PNG image (8-bit RGBA), in pure Python.

    Takes the same arguments as :func:`render_svg` and lays the map out
    identically. The title and labels are drawn with a built-in ASCII bitmap
    font (accents are dropped, other non-ASCII characters become ``?``); the
    title is also stored in the PNG's metadata.

    Returns:
        The PNG file contents as bytes.

    Raises:
        InvalidArgumentError: if an option is out of range or a feature /
            overlay is not valid GeoJSON.

    Example:
        >>> png = render_png(geo.features, width=1200, height=800, title="Stores",
        ...                  labels="name")
        >>> open("stores.png", "wb").write(png)
    """
    scene = _Scene("render_png", features, width, height, title, overlays,
                   background, padding, point_radius, labels, grid, scale_bar)
    return _png(scene)


__all__ = ["render_svg", "render_png"]
