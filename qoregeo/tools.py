"""
qoregeo.tools
=============
QOREgeo's operations as AI tools.

One registry drives every integration: the MCP server (``qoregeo mcp``),
Claude API tool definitions, OpenAI function-calling schemas and the CLI's
``qoregeo tools`` command. Tools take JSON arguments and return JSON — plus a
PNG for ``preview_map``, so a vision model can look at the data it is
working on.

Safety
------
* File paths are resolved inside a sandbox root (default: the current
  directory); ``..`` and symlinks cannot escape it.
* Tools that write files can be switched off (``allow_write=False``) and
  never overwrite the data file they read from.
* Geocoding sends queries to a public server, so it is off unless
  ``allow_network=True``.
* Results are capped (``max_features`` and ``MAX_RESULT_CHARS``) so a large
  file cannot flood the model's context: a feature too big to show is
  summarised, and the full data can always be written with export_features.

Usage
-----
    from qoregeo.tools import ToolContext, call_tool, tool_definitions

    tools = tool_definitions("anthropic")          # pass to client.messages.create(tools=...)
    result = call_tool("distance", {"from": {"lat": 28.61, "lng": 77.21},
                                    "to": {"lat": 19.08, "lng": 72.88}})
    result.data      # {'distance': 1147.2349, 'unit': 'km', 'method': 'haversine'}
"""

from __future__ import annotations

import base64
import json
import math
import os
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .engine import GeoEngine, _same_file
from .exceptions import InvalidArgumentError, QOREgeoError
from .map_builder import tile_presets
from .utils import _geometry_problem, _json_safe

Json = dict[str, Any]

# ─────────────────────────────────────────────────────────────────────────────
# Context and results
# ─────────────────────────────────────────────────────────────────────────────


class ToolError(Exception):
    """A tool call failed in a way the model can fix (bad input, missing file …)."""


@dataclass
class ToolContext:
    """
    Where and how tools may act.

    root          : directory that every file path is confined to
    allow_write   : let tools write maps and data files (inside ``root``)
    allow_network : let tools call a geocoding server
    max_features  : most features a tool returns in one result
    max_file_mb   : largest input file a tool will read
    geocoder      : a ``qoregeo.geocode.Geocoder`` (created on first use)
    """

    root: str = field(default_factory=os.getcwd)
    allow_write: bool = True
    allow_network: bool = False
    max_features: int = 200
    max_file_mb: float = 200.0
    geocoder: Any = None

    def __post_init__(self) -> None:
        self.root = os.path.realpath(os.path.abspath(self.root))
        if not os.path.isdir(self.root):
            raise InvalidArgumentError("root", self.root, "an existing directory",
                                       "ToolContext(root='.')")
        limit = self.max_features
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise InvalidArgumentError("max_features", limit,
                                       "a whole number >= 1 (most features in one result)",
                                       "ToolContext(root='.', max_features=200)")

    def contains(self, real_path: str) -> bool:
        """Whether the (already resolved) ``real_path`` is root or inside it."""
        try:
            return os.path.commonpath([real_path, self.root]) == self.root
        except ValueError:   # another drive (Windows) or a mix of absolute and relative
            return False

    def resolve(self, path: Any, *, must_exist: bool, name: str = "path") -> str:
        """
        ``path`` (relative to root, or absolute inside it) as a real path inside
        root. '~' is not expanded: '~data/x.csv' is a folder named '~data' in root.
        """
        if not isinstance(path, str) or not path.strip() or "\0" in path:
            raise ToolError(f"'{name}' must be a non-empty file path, e.g. \"data/cities.csv\".")
        candidate = os.path.realpath(os.path.join(self.root, path.strip()))
        if not self.contains(candidate):
            raise ToolError(f"'{path}' is outside the allowed directory {self.root}. "
                            "Use a path inside it (relative paths are resolved from it).")
        if must_exist and not os.path.isfile(candidate):
            tilde = (" '~' is not expanded: paths are relative to the allowed directory."
                     if path.strip().startswith("~") else "")
            raise ToolError(f"No file at '{path}' (looked in {self.root}).{tilde} "
                            "Use list_files to see what is available.")
        return candidate

    def display(self, real_path: str) -> str:
        """``real_path`` relative to root, with forward slashes."""
        return os.path.relpath(real_path, self.root).replace(os.sep, "/")


def _valid_text(text: str) -> str:
    """``text`` with lone UTF-16 surrogates replaced by U+FFFD (valid pairs are joined)."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _text_safe(obj: Any) -> Any:
    """
    ``obj`` with every string — keys and values — encodable as UTF-8. Lone
    surrogates come from undecodable file names (``os.listdir`` on bytes that
    aren't UTF-8) or from ``"\\ud800"`` escapes in data files; serialised, they
    are JSON that strict parsers (the MCP SDKs, the Claude API) reject.
    """
    if isinstance(obj, str):
        return obj if obj.isascii() else _valid_text(obj)
    if isinstance(obj, dict):
        return {_text_safe(k) if isinstance(k, str) else k: _text_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_text_safe(v) for v in obj]
    return obj


@dataclass
class ToolResult:
    """What a tool returns: JSON ``data``, optional PNG/SVG images, and an error flag."""

    data: Json
    images: list[tuple[str, bytes]] = field(default_factory=list)   # (mime type, bytes)
    is_error: bool = False

    @property
    def text(self) -> str:
        """``data`` as compact JSON text (valid UTF-8: lone surrogates become U+FFFD)."""
        return json.dumps(_text_safe(self.data), ensure_ascii=False, separators=(",", ":"))

    def to_mcp(self) -> Json:
        """An MCP ``tools/call`` result."""
        content: list[Json] = [{"type": "text", "text": self.text}]
        for mime, blob in self.images:
            content.append({"type": "image", "data": base64.b64encode(blob).decode("ascii"),
                            "mimeType": mime})
        result: Json = {"content": content, "isError": self.is_error}
        if not self.is_error:
            result["structuredContent"] = _text_safe(self.data)
        return result

    def to_anthropic(self, tool_use_id: str) -> Json:
        """A Claude API ``tool_result`` content block."""
        content: list[Json] = [{"type": "text", "text": self.text}]
        for mime, blob in self.images:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": mime,
                "data": base64.b64encode(blob).decode("ascii"),
            }})
        block: Json = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
        if self.is_error:
            block["is_error"] = True
        return block


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    input_schema: Json
    handler: Callable[[ToolContext, Json], ToolResult]
    read_only: bool = True
    destructive: bool = False
    open_world: bool = False

    def annotations(self) -> Json:
        return {
            "title": self.title,
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": True,
            "openWorldHint": self.open_world,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Schema building blocks
# ─────────────────────────────────────────────────────────────────────────────

_DISTANCE_UNITS = ["km", "m", "miles", "ft", "yd", "nmi"]
_AREA_UNITS = ["km2", "m2", "ha", "acres", "mi2", "ft2"]
_TILES = list(tile_presets())


def _point(description: str) -> Json:
    return {
        "type": "object",
        "description": description,
        "properties": {
            "lat": {"type": "number", "minimum": -90, "maximum": 90, "description": "Latitude in degrees"},
            "lng": {"type": "number", "minimum": -180, "maximum": 180,
                    "description": "Longitude in degrees"},
        },
        "required": ["lat", "lng"],
        "additionalProperties": False,
    }


def _unit(default: str = "km") -> Json:
    return {"type": "string", "enum": _DISTANCE_UNITS, "default": default}


_GEOJSON = {
    "type": "object",
    "description": "A GeoJSON geometry, Feature or FeatureCollection (coordinates are [lng, lat]).",
}

_SOURCE_PROPS: Json = {
    "path": {"type": "string",
             "description": "CSV/TSV/GeoJSON file, relative to the allowed directory. "
                            "CSV latitude/longitude columns are detected automatically."},
    "geojson": {"type": "object",
                "description": "Inline GeoJSON FeatureCollection / Feature list instead of a file."},
    "lat_column": {"type": "string", "description": "CSV latitude column, if not auto-detected"},
    "lng_column": {"type": "string", "description": "CSV longitude column, if not auto-detected"},
}

_FILTER_PROPS: Json = {
    "near": {
        "type": "object",
        "description": "Keep features within 'radius' of a point.",
        "properties": {
            "lat": {"type": "number"}, "lng": {"type": "number"},
            "radius": {"type": "number", "exclusiveMinimum": 0},
            "unit": _unit(),
        },
        "required": ["lat", "lng", "radius"],
        "additionalProperties": False,
    },
    "bbox": {
        "type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "number"},
        "description": "Keep features inside [west, south, east, north] (west > east crosses 180°).",
    },
    "within": {**_GEOJSON, "description": "Keep features that intersect this GeoJSON area."},
    "where": {
        "type": "array",
        "description": "Attribute conditions, all of which must hold.",
        "items": {
            "type": "object",
            "properties": {
                "column": {"type": "string"},
                "op": {"type": "string", "enum": ["==", "!=", ">", ">=", "<", "<=", "in", "not in",
                                                  "contains"], "default": "=="},
                "value": {"description": "Value to compare with (a list for 'in' / 'not in')"},
            },
            "required": ["column", "value"],
            "additionalProperties": False,
        },
    },
    "sort_by": {"type": "string", "description": "Column to sort by (missing values last)"},
    "descending": {"type": "boolean", "default": False},
    "limit": {"type": "integer", "minimum": 1, "description": "Keep at most this many features"},
}

_PAGE_PROPS: Json = {
    "offset": {"type": "integer", "minimum": 0, "default": 0,
               "description": "Skip this many results (use next_offset from a truncated page)"},
}
_IMAGE_PROPS: Json = {
    "labels": {"type": "string",
               "description": "Property drawn next to each feature, e.g. \"name\" (previews of up "
                              "to 60 features pick a name-like column automatically)"},
    "grid": {"type": "boolean", "default": True, "description": "Draw a lat/lng grid"},
    "scale_bar": {"type": "boolean", "default": True, "description": "Draw a distance scale bar"},
}
_OVERWRITE: Json = {"overwrite": {"type": "boolean", "default": False,
                                  "description": "Replace 'output' if it already exists"}}


def _schema(properties: Json, required: list[str] | None = None) -> Json:
    return {"type": "object", "properties": properties, "required": required or [],
            "additionalProperties": False}


# ─────────────────────────────────────────────────────────────────────────────
# Argument helpers
# ─────────────────────────────────────────────────────────────────────────────

def _arg(args: Json, key: str, default: Any = None) -> Any:
    return args.get(key, default)


def _get_point(args: Json, key: str) -> tuple[float, float]:
    value = args.get(key)
    if isinstance(value, dict) and "lat" in value and "lng" in value:
        return value["lat"], value["lng"]
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return value[0], value[1]
    raise ToolError(f"'{key}' must be an object like {{\"lat\": 28.61, \"lng\": 77.21}}.")


def _engine() -> GeoEngine:
    return GeoEngine(verbose=False)


def _load_source(ctx: ToolContext, args: Json) -> tuple[GeoEngine, str | None]:
    """Load ``path`` or ``geojson`` from the arguments; returns (engine, real path or None)."""
    path, data = args.get("path"), args.get("geojson")
    if (path is None) == (data is None):
        raise ToolError("Give exactly one of 'path' (a CSV/GeoJSON file) or 'geojson' (inline data).")
    geo = _engine()
    if data is not None:
        geo.load_data(data)
        return geo, None
    real = ctx.resolve(path, must_exist=True)
    size_mb = os.path.getsize(real) / 1e6
    if size_mb > ctx.max_file_mb:
        raise ToolError(f"'{path}' is {size_mb:.0f} MB; the limit is {ctx.max_file_mb:.0f} MB.")
    geo.load(real, lat_col=args.get("lat_column"), lng_col=args.get("lng_column"), infer_types=True)
    return geo, real


def _apply_filters(geo: GeoEngine, args: Json) -> GeoEngine:
    near = args.get("near")
    if near is not None:
        if not isinstance(near, dict):
            raise ToolError("'near' must be an object with lat, lng and radius.")
        geo = geo.filter_by_radius(_arg(near, "lat"), _arg(near, "lng"), _arg(near, "radius"),
                                   unit=_arg(near, "unit", "km"))
    bbox = args.get("bbox")
    if bbox is not None:
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ToolError("'bbox' must be [west, south, east, north].")
        west, south, east, north = bbox
        geo = geo.filter_by_bbox(south, west, north, east)
    within = args.get("within")
    if within is not None:
        geo = geo.filter_intersects(within)
    for cond in args.get("where") or []:
        if not isinstance(cond, dict) or "column" not in cond:
            raise ToolError("Each 'where' item needs 'column', 'op' and 'value'.")
        geo = geo.filter(cond["column"], cond.get("value"), op=cond.get("op", "=="))
    if args.get("sort_by") is not None:
        geo = geo.sort_by(args["sort_by"], descending=bool(args.get("descending", False)))
    if args.get("limit") is not None:
        geo = geo.head(args["limit"])
    return geo


def _clean(obj: Any) -> Any:
    return _json_safe(obj)


# Largest JSON text a successful result may take: Claude Code warns above ~10k
# tokens and moves results over 50,000 characters out of the conversation.
MAX_RESULT_CHARS = 40_000
# What a result's main payload (features, files, columns …) may use, leaving
# room for its other keys and a note.
_BUDGET = MAX_RESULT_CHARS - 2_000
# Strings are cut to this many characters when a result has to be shortened.
_MAX_TEXT = 500


def _full_data(ctx: ToolContext) -> str:
    """How the model can get data a result had to leave out."""
    if ctx.allow_write:
        return "Use export_features to write the complete data to a file"
    return ("The complete data can be written to a file with export_features, which this "
            "read-only server does not offer")


def _json_len(obj: Any) -> int:
    """Characters ``obj`` takes as compact JSON text."""
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def _short(value: Any) -> Any:
    """A string longer than _MAX_TEXT cut down (with its full length noted); others as-is."""
    if isinstance(value, str) and len(value) > _MAX_TEXT:
        return f"{value[:_MAX_TEXT]}… [{len(value):,} characters]"
    return value


def _fit_dict(values: Json, budget: int) -> tuple[Json, bool]:
    """
    ``values`` cut to fit ``budget`` JSON characters: long strings shortened,
    then trailing keys left out. Returns (the dict, whether anything was cut).
    """
    if _json_len(values) <= budget:
        return values, False
    out: Json = {}
    used = 2
    for key, value in values.items():
        value = _short(value)
        size = _json_len(key) + _json_len(value) + 2
        if used + size > budget:
            break
        out[key] = value
        used += size
    return out, True


def _positions(geometry: Any) -> Iterator[list[Any]]:
    """Every [lng, lat, …] position in a GeoJSON geometry (any depth, collections too)."""
    stack: list[Any] = [geometry]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.append(item.get("geometries") if item.get("type") == "GeometryCollection"
                         else item.get("coordinates"))
        elif isinstance(item, list) and item:
            if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in item[:2]):
                yield item
            else:
                stack.extend(reversed(item))


def _geometry_summary(geometry: Json) -> Json:
    """Type, vertex count and bounding box of a geometry too large to return."""
    positions = [p for p in _positions(geometry) if len(p) >= 2]
    summary: Json = {"type": geometry.get("type"), "vertices": len(positions)}
    if positions:
        lngs, lats = [p[0] for p in positions], [p[1] for p in positions]
        summary["bbox"] = [min(lngs), min(lats), max(lngs), max(lats)]
    return summary


def _compact_feature(feature: Json, budget: int, ctx: ToolContext) -> tuple[Json, str]:
    """A feature too large for one result, cut down to fit ``budget``; plus a note saying how."""
    out = dict(feature)
    cuts: list[str] = []
    props = feature.get("properties")
    if isinstance(props, dict):
        fitted, cut = _fit_dict(props, budget // 2)
        if cut:
            out["properties"] = fitted
            if len(fitted) < len(props):
                cuts.append(f"only {len(fitted):,} of {len(props):,} properties are shown")
            if any(fitted[k] is not props[k] for k in fitted):
                cuts.append(f"property values longer than {_MAX_TEXT} characters are cut short")
    geometry = feature.get("geometry")
    if isinstance(geometry, dict) and _json_len(out) > budget:
        summary = _geometry_summary(geometry)
        out["geometry"] = None
        out["geometry_summary"] = summary
        cuts.append(f"its {summary['type']} geometry ({summary['vertices']:,} vertices) is "
                    "replaced by 'geometry_summary' (type, vertex count, bbox)")
    note = ("This feature is too large to show in full: " + "; ".join(cuts or ["it is shortened"])
            + f". {_full_data(ctx)}; preview_map shows it as an image.")
    return out, note


def _feature_page(geo: GeoEngine, ctx: ToolContext, offset: Any = 0) -> Json:
    """One page of features: at most max_features and MAX_RESULT_CHARS, from ``offset``."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ToolError("'offset' must be a non-negative integer.")
    features = geo.get_features()
    shown = _clean(features[offset: offset + ctx.max_features])
    sizes = [_json_len(f) + 1 for f in shown]
    total = sum(sizes)
    while len(shown) > 1 and total > _BUDGET:   # always make progress
        total -= sizes.pop()
        shown.pop()
    note = None
    if shown and total > _BUDGET:   # one feature, too large on its own: summarise it
        shown[0], note = _compact_feature(shown[0], _BUDGET, ctx)
    page: Json = {
        "count": len(features),
        "offset": offset,
        "returned": len(shown),
        "truncated": offset + len(shown) < len(features),
        "features": shown,
    }
    if page["truncated"]:
        page["next_offset"] = offset + len(shown)
    if note:
        page["note"] = note
    return page


def _output_path(ctx: ToolContext, args: Json, suffixes: tuple[str, ...], source: str | None) -> str:
    if not ctx.allow_write:
        raise ToolError("Writing files is disabled for this server (started without write access).")
    real = ctx.resolve(args.get("output"), must_exist=False, name="output")
    if not real.lower().endswith(suffixes):
        raise ToolError(f"'output' must end with {' or '.join(suffixes)}.")
    if source is not None and _same_file(real, source):
        raise ToolError("'output' is the input data file; choose a different file name.")
    if os.path.isdir(real):
        raise ToolError(f"'{args.get('output')}' is a directory.")
    if os.path.exists(real) and args.get("overwrite") is not True:
        raise ToolError(f"'{args.get('output')}' already exists. Choose a new file name, or pass "
                        "\"overwrite\": true to replace it.")
    os.makedirs(os.path.dirname(real), exist_ok=True)
    return real


def _round(x: float, digits: int = 6) -> float:
    return round(x, digits) if math.isfinite(x) else x


# ─────────────────────────────────────────────────────────────────────────────
# Tool handlers
# ─────────────────────────────────────────────────────────────────────────────

def _distance(ctx: ToolContext, a: Json) -> ToolResult:
    unit, method = a.get("unit", "km"), a.get("method", "haversine")
    geo, p, q = _engine(), _get_point(a, "from"), _get_point(a, "to")
    if method != "vincenty":
        return ToolResult({"distance": geo.distance(p, q, unit=unit, method=method),
                           "unit": unit, "method": method})
    # For nearly antipodal points Vincenty's iteration does not converge and the
    # engine falls back to haversine with a warning (on stderr, unseen by the
    # model): report the method actually used.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        d = geo.distance(p, q, unit=unit, method="vincenty")
    out: Json = {"distance": d, "unit": unit, "method": "vincenty"}
    if any("converge" in str(w.message) for w in caught) and \
            d == geo.distance(p, q, unit=unit, method="haversine"):
        out["method"] = "haversine"
        out["note"] = ("Vincenty's ellipsoidal formula does not converge for nearly antipodal "
                       "points, so this is the spherical (haversine) distance, accurate to "
                       "about 0.5%.")
    return ToolResult(out)


def _bearing(ctx: ToolContext, a: Json) -> ToolResult:
    geo = _engine()
    p, q = _get_point(a, "from"), _get_point(a, "to")
    return ToolResult({"degrees": geo.bearing(p, q, as_degrees=True), "compass": geo.bearing(p, q)})


def _destination(ctx: ToolContext, a: Json) -> ToolResult:
    lat, lng = _engine().destination(_get_point(a, "start"), _arg(a, "distance"), _arg(a, "bearing"),
                                     unit=_arg(a, "unit", "km"))
    return ToolResult({"lat": lat, "lng": lng})


def _midpoint(ctx: ToolContext, a: Json) -> ToolResult:
    lat, lng = _engine().midpoint(_get_point(a, "a"), _get_point(a, "b"))
    return ToolResult({"lat": lat, "lng": lng})


# Most vertices a buffer ring may have: with positions rounded to 9 decimals
# (0.1 mm) the ring stays well under MAX_RESULT_CHARS.
_MAX_BUFFER_POINTS = 1024


def _buffer(ctx: ToolContext, a: Json) -> ToolResult:
    zone = _engine().buffer(_get_point(a, "center"), _arg(a, "radius"), unit=_arg(a, "unit", "km"),
                            num_points=a.get("num_points", 64))
    rings = [[[_round(x, 9) for x in pos] for pos in ring] for ring in zone["coordinates"]]
    geometry = {"type": zone["type"], "coordinates": rings}
    return ToolResult({"geometry": _clean(geometry), "radius": a.get("radius"),
                       "unit": a.get("unit", "km")})


def _contains(ctx: ToolContext, a: Json) -> ToolResult:
    inside = _engine().point_in_polygon(_get_point(a, "point"), _arg(a, "area"))
    return ToolResult({"inside": inside})


def _intersects(ctx: ToolContext, a: Json) -> ToolResult:
    return ToolResult({"intersects": _engine().intersects(a.get("a"), a.get("b"))})


def _measure(ctx: ToolContext, a: Json) -> ToolResult:
    geo = _engine()
    geometry = _arg(a, "geometry")
    area_unit, length_unit = a.get("area_unit", "km2"), a.get("length_unit", "km")
    gtype = geometry.get("type") if isinstance(geometry, dict) else None
    if gtype == "Feature" and isinstance(geometry, dict):
        geometry = geometry.get("geometry")
        gtype = geometry.get("type") if isinstance(geometry, dict) else None
    out: Json = {"type": gtype}
    if gtype in ("Polygon", "MultiPolygon", "LineString", "MultiLineString"):
        problem = _geometry_problem(geometry)
        if problem:
            raise ToolError(f"Invalid Coordinate in 'geometry': {problem}. GeoJSON positions are "
                            "[lng, lat] (longitude first), e.g. {\"type\": \"Polygon\", "
                            "\"coordinates\": [[[77.1, 28.5], [77.3, 28.5], [77.3, 28.7], "
                            "[77.1, 28.5]]]}.")
    if gtype in ("Polygon", "MultiPolygon"):
        out["area"], out["area_unit"] = geo.area(geometry, unit=area_unit), area_unit
    elif gtype in ("LineString", "MultiLineString"):
        out["length"], out["length_unit"] = geo.length(geometry, unit=length_unit), length_unit
    else:
        raise ToolError("'geometry' must be a GeoJSON Polygon/MultiPolygon (area) or "
                        "LineString/MultiLineString (length).")
    return ToolResult(out)


def _geohash(ctx: ToolContext, a: Json) -> ToolResult:
    geo = _engine()
    h = geo.geohash(_get_point(a, "point"), precision=a.get("precision", 9))
    lat, lng = geo.geohash_decode(h)
    return ToolResult({"geohash": h, "center": {"lat": lat, "lng": lng}})


_MAX_FILES = 500


def _list_files(ctx: ToolContext, a: Json) -> ToolResult:
    sub = a.get("directory", ".")
    base = ctx.resolve(sub, must_exist=False)
    if not os.path.isdir(base):
        raise ToolError(f"'{sub}' is not a directory inside {ctx.root}.")
    found: list[Json] = []
    used = _json_len(ctx.root)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in
                             ("node_modules", "__pycache__", "venv", ".venv"))
        for name in sorted(filenames):
            if not name.lower().endswith((".csv", ".tsv", ".txt", ".geojson", ".json")):
                continue
            path = os.path.join(dirpath, name)
            real = os.path.realpath(path)
            # Skip what the other tools would refuse: dangling symlinks (editor lock
            # files) and links to files outside the root.
            if not ctx.contains(real) or not os.path.isfile(real):
                continue
            try:
                entry: Json = {"path": ctx.display(path), "bytes": os.path.getsize(real)}
            except OSError:
                continue
            used += _json_len(entry) + 1
            if len(found) >= _MAX_FILES or used > _BUDGET:
                return ToolResult({"root": ctx.root, "files": found, "truncated": True,
                                   "note": f"Only the first {len(found)} files are listed. Pass "
                                           "'directory' to list a sub-directory."})
            found.append(entry)
    return ToolResult({"root": ctx.root, "files": found, "truncated": False})


def _describe(ctx: ToolContext, a: Json) -> ToolResult:
    geo, _ = _load_source(ctx, a)
    features = geo.get_features()
    columns: dict[str, str] = {}
    for f in features:
        for k, v in (f.get("properties") or {}).items():
            kind = type(v).__name__ if v is not None else "null"
            columns[k] = kind if columns.get(k) in (None, "null", kind) else "mixed"
    types: dict[str, int] = {}
    for f in features:
        g = f.get("geometry")
        t = g.get("type", "?") if isinstance(g, dict) else "none"
        types[t] = types.get(t, 0) + 1
    listed, columns_cut = _fit_dict(columns, _BUDGET // 3)
    out: Json = {"count": len(features), "columns": listed, "geometry_types": types}
    if columns_cut:
        out["columns_total"] = len(columns)
    try:
        out["bbox"] = geo.bbox()
        lat, lng = geo.centroid()
        out["centroid"] = {"lat": lat, "lng": lng}
    except QOREgeoError:
        out["bbox"] = out["centroid"] = None
    sample = _clean([f.get("properties") for f in features[:5]])
    rows = [_fit_dict(row, _BUDGET // 10) if isinstance(row, dict) else (row, False)
            for row in sample]
    out["sample"] = [row for row, _ in rows]
    cuts = [f"only {len(listed):,} of {len(columns):,} columns are listed"] if columns_cut else []
    if any(cut for _, cut in rows):
        cuts.append("the sample rows are shortened")
    if cuts:
        out["note"] = (f"This dataset is too large to describe in full: {' and '.join(cuts)}. "
                       f"Use query_features to read particular features. {_full_data(ctx)}.")
    return ToolResult(out)


def _query(ctx: ToolContext, a: Json) -> ToolResult:
    geo, _ = _load_source(ctx, a)
    return ToolResult(_feature_page(_apply_filters(geo, a), ctx, a.get("offset", 0)))


def _nearest(ctx: ToolContext, a: Json) -> ToolResult:
    geo, _ = _load_source(ctx, a)
    geo = _apply_filters(geo, {k: v for k, v in a.items() if k in ("near", "bbox", "within", "where")})
    k = a.get("k", 5)
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ToolError("'k' must be a positive integer.")
    result = geo.k_nearest(_get_point(a, "point"), k=k, unit=a.get("unit", "km"))
    return ToolResult({"unit": a.get("unit", "km"), **_feature_page(result, ctx, a.get("offset", 0))})


# Property names tried, in order, when a preview isn't told which one to label.
_LABEL_CANDIDATES = ("name", "title", "label", "city", "place", "store", "site", "id")
_AUTO_LABEL_LIMIT = 60


def _auto_label(features: list[Json]) -> str | None:
    """A name-like property to label a small dataset's preview with (or None)."""
    if not features or len(features) > _AUTO_LABEL_LIMIT:
        return None
    keys: dict[str, str] = {}
    for f in features:
        for k in f.get("properties") or {}:
            keys.setdefault(k.lower(), k)
    return next((keys[c] for c in _LABEL_CANDIDATES if c in keys), None)


def _render_args(a: Json) -> Json:
    out: Json = {}
    for key in ("width", "height", "title", "labels", "grid", "scale_bar"):
        if a.get(key) is not None:
            out[key] = a[key]
    overlays = a.get("overlays")
    if overlays is not None:
        out["overlays"] = overlays if isinstance(overlays, list) else [overlays]
    return out


def _preview(ctx: ToolContext, a: Json) -> ToolResult:
    from .render import render_png

    geo, _ = _load_source(ctx, a)
    geo = _apply_filters(geo, a)
    features = geo.get_features()
    options = {"width": 800, "height": 600, **_render_args(a)}
    if "labels" not in a:
        options["labels"] = _auto_label(features)
    png = render_png(features, **options)
    return ToolResult({"count": geo.count(), "format": "png", "bytes": len(png)},
                      images=[("image/png", png)])


def _make_map(ctx: ToolContext, a: Json) -> ToolResult:
    geo, source = _load_source(ctx, a)
    geo = _apply_filters(geo, a)
    real = _output_path(ctx, a, (".html", ".htm", ".png", ".svg"), source)
    kind = a.get("kind", "map")
    title = a.get("title") or ("QOREgeo Heatmap" if kind == "heatmap" else "QOREgeo Map")
    if real.lower().endswith((".png", ".svg")):
        geo.image(real, **_render_args({**a, "title": title}))
    elif kind == "heatmap":
        geo.heatmap(real, title=title, intensity_col=a.get("intensity_column"),
                    tiles=a.get("tiles", "light"))
    elif kind == "map":
        geo.map(real, title=title, tiles=a.get("tiles", "light"),
                overlays=_render_args(a).get("overlays"))
    else:
        raise ToolError("'kind' must be 'map' or 'heatmap'.")
    return ToolResult({"path": ctx.display(real), "features": geo.count(),
                       "view": "Open the .html file through `qoregeo serve <file>` (OpenStreetMap "
                               "tiles need a web page, not a file:// URL)."
                       if real.lower().endswith((".html", ".htm")) else "Static image."})


def _export(ctx: ToolContext, a: Json) -> ToolResult:
    geo, source = _load_source(ctx, a)
    geo = _apply_filters(geo, a)
    real = _output_path(ctx, a, (".csv", ".tsv", ".geojson", ".json"), source)
    geo.save(real, csv_safe=a.get("csv_safe", True))
    return ToolResult({"path": ctx.display(real), "features": geo.count()})


NOMINATIM_POLICY = "https://operations.osmfoundation.org/policies/nominatim/"


def _geocoder(ctx: ToolContext) -> Any:
    if not ctx.allow_network:
        raise ToolError("Geocoding is disabled. It needs a deliberate opt-in by whoever runs this "
                        "server: --geocoder-url for a self-hosted Nominatim/Photon server, or "
                        f"--allow-network for the public OpenStreetMap service ({NOMINATIM_POLICY}).")
    if ctx.geocoder is None:
        from .geocode import Geocoder
        ctx.geocoder = Geocoder()
    return ctx.geocoder


def _geocode(ctx: ToolContext, a: Json) -> ToolResult:
    query = a.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("'query' must be a non-empty address or place name.")
    results = _clean(_geocoder(ctx).search(query, limit=a.get("limit", 5),
                                           country_codes=a.get("country_codes")))
    out: Json = {"query": query, "results": results}
    used = _json_len(query)
    for i, item in enumerate(results):
        used += _json_len(item) + 1
        if used > _BUDGET:   # a server returning very long results: keep what fits
            out["results"] = results[:i]
            out["note"] = (f"Only the first {i} of {len(results)} results fit in one reply; "
                           "ask for a lower 'limit' or a more specific query.")
            break
    return ToolResult(out)


def _reverse(ctx: ToolContext, a: Json) -> ToolResult:
    lat, lng = _get_point(a, "point")
    return ToolResult({"result": _clean(_geocoder(ctx).reverse(lat, lng))})


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────

_SOURCE_NOTE = " Data comes from 'path' (a CSV/GeoJSON file) or inline 'geojson'."

TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("distance", "Distance between two points",
         "Great-circle distance between two lat/lng points (haversine, or the more exact "
         "vincenty ellipsoid formula).",
         _schema({"from": _point("Start point"), "to": _point("End point"), "unit": _unit(),
                  "method": {"type": "string", "enum": ["haversine", "vincenty"],
                             "default": "haversine"}}, ["from", "to"]),
         _distance),
    Tool("bearing", "Compass bearing", "Initial compass bearing from one point to another, in "
         "degrees (0 = north, clockwise) and as a 16-point compass direction.",
         _schema({"from": _point("Start point"), "to": _point("Target point")}, ["from", "to"]),
         _bearing),
    Tool("destination", "Destination point", "The point reached by travelling 'distance' from "
         "'start' on the given compass bearing.",
         _schema({"start": _point("Start point"),
                  "bearing": {"type": "number", "description": "Degrees clockwise from north"},
                  "distance": {"type": "number", "minimum": 0}, "unit": _unit()},
                 ["start", "bearing", "distance"]),
         _destination),
    Tool("midpoint", "Midpoint", "The geographic midpoint of two points along the great circle.",
         _schema({"a": _point("First point"), "b": _point("Second point")}, ["a", "b"]),
         _midpoint),
    Tool("buffer", "Circular zone", "A GeoJSON polygon approximating a circle of 'radius' around a "
         "point — use it as a geofence or delivery zone with contains / query_features.within.",
         _schema({"center": _point("Circle centre"), "radius": {"type": "number", "exclusiveMinimum": 0},
                  "unit": _unit(), "num_points": {"type": "integer", "minimum": 3,
                                                  "maximum": _MAX_BUFFER_POINTS, "default": 64}},
                 ["center", "radius"]),
         _buffer),
    Tool("contains", "Point in area", "Whether a point lies inside a GeoJSON Polygon/MultiPolygon "
         "(holes respected) or a buffer() zone.",
         _schema({"point": _point("Point to test"), "area": {**_GEOJSON, "description":
                  "GeoJSON Polygon / MultiPolygon / Feature"}}, ["point", "area"]),
         _contains),
    Tool("intersects", "Shapes intersect", "Whether two GeoJSON shapes (points, lines, polygons, "
         "features or collections) touch or overlap.",
         _schema({"a": _GEOJSON, "b": _GEOJSON}, ["a", "b"]),
         _intersects),
    Tool("measure", "Area or length", "Area of a polygon (spherical, mean Earth radius — as in "
         "Turf.js) or great-circle length of a line.",
         _schema({"geometry": _GEOJSON,
                  "area_unit": {"type": "string", "enum": _AREA_UNITS, "default": "km2"},
                  "length_unit": _unit()}, ["geometry"]),
         _measure),
    Tool("geohash", "Geohash", "Encode a point as a geohash string (precision 1–12) and return the "
         "cell centre.",
         _schema({"point": _point("Point to encode"),
                  "precision": {"type": "integer", "minimum": 1, "maximum": 12, "default": 9}},
                 ["point"]),
         _geohash),
    Tool("list_files", "List data files", "List CSV/TSV/GeoJSON files in the allowed directory "
         "(or a sub-directory of it).",
         _schema({"directory": {"type": "string", "default": "."}}),
         _list_files),
    Tool("describe_dataset", "Describe dataset", "Row count, columns and their types, geometry "
         "types, bounding box, centre and 5 sample rows of a dataset." + _SOURCE_NOTE,
         _schema(dict(_SOURCE_PROPS)),
         _describe),
    Tool("query_features", "Query features", "Filter a dataset by distance ('near'), bounding box, "
         "area ('within') and attribute conditions ('where'), then sort and limit. Returns GeoJSON "
         "features (capped)." + _SOURCE_NOTE,
         _schema({**_SOURCE_PROPS, **_FILTER_PROPS, **_PAGE_PROPS}),
         _query),
    Tool("nearest", "Nearest features", "The k features closest to a point, nearest first, each "
         "with a '_distance' property." + _SOURCE_NOTE,
         _schema({**_SOURCE_PROPS, "point": _point("Reference point"),
                  "k": {"type": "integer", "minimum": 1, "default": 5}, "unit": _unit(),
                  **{k: _FILTER_PROPS[k] for k in ("near", "bbox", "within", "where")}, **_PAGE_PROPS},
                 ["point"]),
         _nearest),
    Tool("preview_map", "Preview map image", "Render the (optionally filtered) data as a PNG map "
         "image you can look at — points, lines and polygons on a plain background, plus "
         "optional overlay shapes." + _SOURCE_NOTE,
         _schema({**_SOURCE_PROPS, **_FILTER_PROPS,
                  "title": {"type": "string"},
                  "overlays": {"type": "array", "items": _GEOJSON,
                               "description": "Extra shapes drawn as dashed outlines"},
                  "width": {"type": "integer", "minimum": 64, "maximum": 2048, "default": 800},
                  "height": {"type": "integer", "minimum": 64, "maximum": 2048, "default": 600},
                  **_IMAGE_PROPS}),
         _preview),
    Tool("make_map", "Create map file", "Write an interactive HTML map or heatmap (OpenStreetMap "
         "basemap, popups) — or a static .png/.svg image — of the (optionally filtered) data."
         + _SOURCE_NOTE,
         _schema({**_SOURCE_PROPS, **_FILTER_PROPS,
                  "output": {"type": "string", "description": "File to write: .html, .png or .svg"},
                  "kind": {"type": "string", "enum": ["map", "heatmap"], "default": "map"},
                  "title": {"type": "string"},
                  "tiles": {"type": "string", "enum": _TILES, "default": "light"},
                  "intensity_column": {"type": "string",
                                       "description": "Heatmap weight column (kind='heatmap')"},
                  "overlays": {"type": "array", "items": _GEOJSON},
                  "width": {"type": "integer", "minimum": 16, "maximum": 8192},
                  "height": {"type": "integer", "minimum": 16, "maximum": 8192},
                  **_IMAGE_PROPS, **_OVERWRITE},
                 ["output"]),
         _make_map, read_only=False, destructive=True),
    Tool("export_features", "Export features", "Write the (optionally filtered) data to a CSV "
         "or GeoJSON file. CSV cells that a spreadsheet would run as formulas are escaped."
         + _SOURCE_NOTE,
         _schema({**_SOURCE_PROPS, **_FILTER_PROPS,
                  "output": {"type": "string", "description": "File to write: .csv, .tsv or .geojson"},
                  "csv_safe": {"type": "boolean", "default": True}, **_OVERWRITE},
                 ["output"]),
         _export, read_only=False, destructive=True),
    Tool("geocode", "Geocode address", "Look up coordinates for an address or place name with "
         "OpenStreetMap Nominatim (or the server's own geocoder). The public service allows at most "
         "1 request per second, no bulk or autocomplete use, and requires OpenStreetMap "
         f"attribution — see {NOMINATIM_POLICY}. Don't send personal data.",
         _schema({"query": {"type": "string"},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5},
                  "country_codes": {"type": "array", "items": {"type": "string"},
                                    "description": "ISO 3166-1 alpha-2 codes, e.g. [\"in\"]"}},
                 ["query"]),
         _geocode, open_world=True),
    Tool("reverse_geocode", "Reverse geocode", "Find the address / place at a point with "
         f"OpenStreetMap Nominatim (same usage policy as geocode: {NOMINATIM_POLICY}).",
         _schema({"point": _point("Point to look up")}, ["point"]),
         _reverse, open_world=True),
]}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

_FORMATS = ("mcp", "anthropic", "openai", "openai-responses")


def tool_definitions(
    format: str = "mcp",
    names: list[str] | None = None,
    include_network: bool = True,
) -> list[Json]:
    """
    Tool definitions in the shape a platform expects.

    format          : 'mcp' (tools/list entries), 'anthropic' (Claude Messages API
                      ``tools``), 'openai' (Chat Completions ``tools``) or
                      'openai-responses' (Responses API ``tools``)
    names           : only these tools (default: all)
    include_network : include geocode / reverse_geocode
    """
    if format not in _FORMATS:
        raise InvalidArgumentError("format", format, " | ".join(repr(f) for f in _FORMATS),
                                   "tool_definitions('anthropic')")
    unknown = [n for n in names or [] if n not in TOOLS]
    if unknown:
        raise InvalidArgumentError("names", unknown, f"tool names from: {', '.join(TOOLS)}",
                                   "tool_definitions('anthropic', names=['distance', 'nearest'])")
    selected = [TOOLS[n] for n in names] if names is not None else list(TOOLS.values())
    if not include_network:
        selected = [t for t in selected if not t.open_world]
    out: list[Json] = []
    for t in selected:
        if format == "mcp":
            out.append({"name": t.name, "title": t.title, "description": t.description,
                        "inputSchema": t.input_schema, "annotations": t.annotations()})
        elif format == "anthropic":
            out.append({"name": t.name, "description": t.description, "input_schema": t.input_schema})
        elif format == "openai":
            out.append({"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.input_schema}})
        else:
            out.append({"type": "function", "name": t.name, "description": t.description,
                        "parameters": t.input_schema})
    return out


def call_tool(name: str, arguments: Json | None = None, context: ToolContext | None = None) -> ToolResult:
    """
    Run tool ``name`` with JSON ``arguments``.

    Never raises for a bad call: problems the model can fix (unknown tool,
    invalid arguments, missing file, …) come back as ``ToolResult`` with
    ``is_error=True`` and an explanation in ``data['error']``.
    """
    ctx = context if context is not None else ToolContext()
    tool = TOOLS.get(name)
    if tool is None:
        return ToolResult({"error": f"Unknown tool '{name}'. Available: {', '.join(TOOLS)}."},
                          is_error=True)
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return ToolResult({"error": "Arguments must be a JSON object."}, is_error=True)
    # An explicit null means "not given" for optional arguments.
    required = set(tool.input_schema.get("required", []))
    arguments = {k: v for k, v in arguments.items() if v is not None or k in required}
    # JSON Schema counts 5.0 as an integer: accept it, and hand the tool an int.
    props = tool.input_schema.get("properties", {})
    arguments = {k: int(v) if _whole_float(v) and props.get(k, {}).get("type") == "integer" else v
                 for k, v in arguments.items()}
    problem = _schema_problem(tool.input_schema, arguments)
    if problem:
        return ToolResult({"error": problem}, is_error=True)
    try:
        result = tool.handler(ctx, arguments)
    except ToolError as exc:
        return ToolResult({"error": str(exc)}, is_error=True)
    except QOREgeoError as exc:
        return ToolResult({"error": _plain(str(exc))}, is_error=True)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        return ToolResult({"error": f"{type(exc).__name__}: {exc}"}, is_error=True)
    return result if result.is_error else _checked(name, result, ctx)


def _checked(name: str, result: ToolResult, ctx: ToolContext) -> ToolResult:
    """``result`` if it is valid JSON within MAX_RESULT_CHARS; otherwise an explanatory error."""
    try:
        size = len(json.dumps(_text_safe(result.data), ensure_ascii=False,
                              separators=(",", ":"), allow_nan=False))
    except ValueError:   # NaN / Infinity: not JSON
        return ToolResult({"error": f"{name} produced a non-finite number (NaN or Infinity); "
                                    "check that the input coordinates and values are valid."},
                          is_error=True)
    if size > MAX_RESULT_CHARS:
        return ToolResult({"error": f"The {name} result is too large to return ({size:,} "
                                    f"characters; the limit is {MAX_RESULT_CHARS:,}). Narrow the "
                                    "request (filters, 'limit', a smaller area). "
                                    f"{_full_data(ctx)}."},
                          is_error=True)
    return result


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
    "array": (list,), "object": (dict,),
}


def _whole_float(value: Any) -> bool:
    """A float with no fractional part (e.g. 5.0), which JSON Schema counts as an integer."""
    return isinstance(value, float) and value.is_integer()


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(value) and abs(value) < 1e300
    except OverflowError:   # an int too large for a float
        return False


def _schema_problem(schema: Json, arguments: Json) -> str | None:
    """
    Why ``arguments`` don't fit the top level of ``schema`` (None if they do):
    unknown or missing keys, wrong types, values outside enum / bounds.
    Nested structures are checked by the handlers.
    """
    props: Json = schema.get("properties", {})
    unknown = sorted(k for k in arguments if k not in props)
    if unknown and schema.get("additionalProperties") is False:
        return (f"Unknown argument(s): {', '.join(unknown)}. "
                f"Valid arguments: {', '.join(props) or 'none'}.")
    missing = [k for k in schema.get("required", []) if k not in arguments]
    if missing:
        return f"Missing required argument(s): {', '.join(missing)}."
    for key, value in arguments.items():
        spec = props.get(key, {})
        kind = spec.get("type")
        if kind in _JSON_TYPES:
            if not isinstance(value, _JSON_TYPES[kind]) or (kind != "boolean" and isinstance(value, bool)):
                return f"'{key}' must be a{'n' if kind[0] in 'aeiou' else ''} {kind}, got {value!r:.60}."
            if kind in ("number", "integer") and not _finite(arguments[key]):
                return f"'{key}' must be a finite number of reasonable size."
        if "enum" in spec and value not in spec["enum"]:
            return f"'{key}' must be one of {', '.join(map(str, spec['enum']))}; got {value!r:.60}."
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                return f"'{key}' must be at least {spec['minimum']}."
            if "maximum" in spec and value > spec["maximum"]:
                return f"'{key}' must be at most {spec['maximum']}."
            if "exclusiveMinimum" in spec and value <= spec["exclusiveMinimum"]:
                return f"'{key}' must be greater than {spec['exclusiveMinimum']}."
        if isinstance(value, list):
            if "minItems" in spec and len(value) < spec["minItems"]:
                return f"'{key}' needs at least {spec['minItems']} item(s)."
            if "maxItems" in spec and len(value) > spec["maxItems"]:
                return f"'{key}' takes at most {spec['maxItems']} item(s)."
    return None


def _plain(message: str) -> str:
    """QOREgeo's multi-line console errors, condensed for a model."""
    lines = [ln.strip() for ln in message.splitlines()]
    return " ".join(ln for ln in lines if ln and set(ln) - set("─━-=")).replace("❌  ", "")


SERVER_INSTRUCTIONS = (
    "QOREgeo: offline geospatial tools. Points are {\"lat\": …, \"lng\": …} objects; GeoJSON "
    "coordinates are [lng, lat]. Datasets are CSV/TSV/GeoJSON files inside the allowed "
    "directory (see list_files) or inline GeoJSON. Typical flow: describe_dataset → "
    "query_features / nearest → preview_map to look at the result → make_map or "
    "export_features to save it. Distances default to km. Use buffer for radius zones."
)

__all__ = [
    "SERVER_INSTRUCTIONS",
    "TOOLS",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolResult",
    "call_tool",
    "tool_definitions",
]
