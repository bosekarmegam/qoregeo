"""
qoregeo.engine
==============
Core GeoEngine class: the single entry point for all spatial operations.

Usage:
    from qoregeo import GeoEngine

    geo = GeoEngine()
    geo.load("cities.csv")
    geo.distance((28.61, 77.20), (19.07, 72.87), unit="km")
"""

from __future__ import annotations

import codecs
import csv
import difflib
import heapq
import html
import io
import json
import math
import os
import re
import sys
import warnings
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any, Literal, overload

from ._version import __version__
from .exceptions import (
    ColumnNotFoundError,
    EmptyDatasetError,
    FileReadError,
    GeoFileNotFoundError,
    InvalidArgumentError,
    InvalidCoordinateError,
    InvalidGeoJSONError,
    InvalidRadiusError,
    NoDataError,
    UnsupportedFormatError,
)
from .map_builder import _announce, _plural, _tiles_config, _write, heatmap_page, map_page
from .utils import (
    _LAT_CANDIDATES,
    _LNG_CANDIDATES,
    EARTH_RADIUS_KM,
    GEOMETRY_TYPES,
    _atomic_write,
    _bearing_to_compass,
    _coerce_point,
    _copy_geometry,
    _destination_point,
    _detect_delimiter,
    _detect_lat_lng_columns,
    _distance_km,
    _escape_surrogates,
    _find_column,
    _generate_circle_polygon,
    _geohash_bounds,
    _geohash_encode,
    _geometries_intersect,
    _geometry_problem,
    _infer_value,
    _initial_bearing_deg,
    _iter_positions,
    _json_safe,
    _km_per_unit,
    _large_csv_fields,
    _lng_span,
    _m2_per_unit,
    _midpoint,
    _normalize_lng,
    _path_length_km,
    _point_in_geometry,
    _point_line_km,
    _polygon_area_m2,
    _polygon_tester,
    _prepare_shape,
    _representative_point,
    _resolve_method,
    _shapes_intersect,
    _shell_quote,
    _spherical_mean,
    _validate_coord,
)

# ─────────────────────────────────────────────────────────────────────────────
# Type aliases
# ─────────────────────────────────────────────────────────────────────────────
Coord = tuple[float, float]   # (lat, lng): what methods return
Feature = dict[str, Any]      # GeoJSON Feature dict
PathLike = str | os.PathLike[str]
# What methods accept as a point: a (lat, lng) or (lat, lng, alt) sequence,
# a {"lat": …, "lng": …} mapping ("lon" / "latitude" / "longitude" work too),
# or a GeoJSON Point geometry / Point Feature.
PointLike = Sequence[float] | Mapping[str, Any]

_CSV_EXTS = (".csv", ".tsv", ".txt")
_JSON_EXTS = (".geojson", ".json")
_SAVE_EXTS = [".geojson", ".json", ".csv", ".tsv"]
_SEP_PREAMBLE_RE = re.compile(r'^\s*"?sep=(.)"?\s*$')   # Excel's "sep=;" first line
_SNIFF_ROWS = 20
_CSV_FIELD_LIMIT = min(sys.maxsize, 2**31 - 1)   # polygons in a CSV cell can be large
_FILTER_OPS = ("==", "!=", ">", ">=", "<", "<=", "in", "not in", "contains")


def _positive_radius(radius: Any) -> float:
    """Return radius as a float, or raise InvalidRadiusError."""
    if isinstance(radius, bool):
        raise InvalidRadiusError(radius)
    try:
        r = float(radius)
    except (TypeError, ValueError):
        raise InvalidRadiusError(radius) from None
    if not math.isfinite(r) or r <= 0:
        raise InvalidRadiusError(radius)
    return r


def _finite_number(name: str, value: Any, example: str) -> float:
    if isinstance(value, bool):
        raise InvalidArgumentError(name, value, "a finite number", example)
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise InvalidArgumentError(name, value, "a finite number", example) from None
    if not math.isfinite(v):
        raise InvalidArgumentError(name, value, "a finite number", example)
    return v


def _positive_int(name: str, value: Any, minimum: int, example: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InvalidArgumentError(name, value, f"an integer >= {minimum}", example)
    return value


_INT_TEXT_RE = re.compile(r"^[+-]?\d+$")


def _as_number(value: Any) -> int | float | None:
    """
    Numeric view of a property value (None if not numeric). Integers and
    integer strings stay ``int`` so large IDs compare exactly (Python
    compares ``int`` with ``float`` exactly too).
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip()
        try:
            if _INT_TEXT_RE.match(text):
                return int(text)
            f = float(text)
        except (ValueError, OverflowError):
            return None
        return f if math.isfinite(f) else None
    return None


def _norm_str(value: Any) -> str:
    return str(value).strip().casefold()


def _values_equal(prop: Any, value: Any) -> bool:
    """Equality used by filter(): numeric when ``value`` is a number, else case-insensitive."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        p = _as_number(prop)
        return p is not None and p == value
    return _norm_str(prop) == _norm_str(value)


def _json_cell(value: Any) -> Any:
    """Make a property value CSV-friendly (nested structures become JSON)."""
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        return json.dumps(_json_safe(value), ensure_ascii=False, allow_nan=False)
    return value


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _formula_safe(value: Any) -> Any:
    """Neutralise a CSV cell a spreadsheet would evaluate as a formula (OWASP)."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES) and _as_number(value) is None:
        return "'" + value
    return value


def _fspath(path: Any, example: str) -> str:
    """Return ``path`` as a str (accepts str, bytes and os.PathLike)."""
    try:
        return os.fsdecode(os.fspath(path))
    except TypeError:
        raise InvalidArgumentError(
            "path", path, "a file path (str or pathlib.Path)", example
        ) from None


def serve_html(*args: Any, **kwargs: Any) -> str:
    """qoregeo.serve.serve_html, imported on first use (keeps ``import qoregeo`` light)."""
    from .serve import serve_html as serve

    return serve(*args, **kwargs)


def _same_file(a: str, b: str) -> bool:
    """True if paths ``a`` and ``b`` name the same file (existing or not)."""
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        def norm(p: str) -> str:
            return os.path.normcase(os.path.realpath(os.path.abspath(p)))
        return norm(a) == norm(b)


def _view_hint(path: str, tiles: str) -> str:
    """How to open a saved map whose basemap won't load from file://."""
    if not _tiles_config(tiles)["needsReferer"]:
        return ""
    return f". View it with: qoregeo serve {_shell_quote(path)}"


def _check_encoding(encoding: Any) -> str:
    try:
        if not codecs.lookup(encoding)._is_text_encoding:
            raise LookupError(encoding)
    except (LookupError, TypeError):
        raise InvalidArgumentError(
            "encoding", encoding, "a Python codec name such as 'utf-8', 'utf-8-sig' or 'cp1252'",
            "geo.load('data.csv', encoding='cp1252')",
        ) from None
    return str(encoding)


def _check_method(method: Any) -> str:
    """Validate ``method`` up front, so a typo fails even when there is nothing to measure."""
    return _resolve_method(method)


def _line_parts(line: Any, name: str = "line") -> list[list[Coord]]:
    """
    Normalise a path argument to a list of parts, each a list of (lat, lng).

    Accepts a list of (lat, lng) points, or a GeoJSON LineString /
    MultiLineString (or a Feature wrapping one, positions in [lng, lat]).
    """
    expected = "a list of (lat, lng) points or a LineString / MultiLineString"
    example = "geo.length([(28.61, 77.20), (19.07, 72.87)])"
    if isinstance(line, dict):
        geom: Any = line.get("geometry") if line.get("type") == "Feature" else line
        gtype = geom.get("type") if isinstance(geom, dict) else None
        coords = geom.get("coordinates") if isinstance(geom, dict) else None
        if gtype == "LineString":
            raw_parts = [coords]
        elif gtype == "MultiLineString":
            raw_parts = coords if isinstance(coords, list) else []
        else:
            raise InvalidArgumentError(name, gtype, expected, example)
        parts: list[list[Coord]] = []
        for part in raw_parts:
            if not isinstance(part, list):
                raise InvalidArgumentError(name, part, "a list of [lng, lat] positions", example)
            try:
                parts.append([_coerce_point((pos[1], pos[0]), "LineString position") for pos in part])
            except (TypeError, IndexError, KeyError):
                raise InvalidArgumentError(name, part, "a list of [lng, lat] positions", example) from None
        return parts
    if isinstance(line, (str, bytes)) or not isinstance(line, Iterable):
        raise InvalidArgumentError(name, line, expected, example)
    return [[_coerce_point(pt) for pt in line]]


_NUMBER_TYPES = (int, float)


def _is_plain_point(geom: Any) -> bool:
    """Cheap check for a valid ``Point`` (the vast majority of features)."""
    if type(geom) is not dict or geom.get("type") != "Point":
        return False
    c = geom.get("coordinates")
    return (
        isinstance(c, (list, tuple)) and len(c) >= 2
        and type(c[0]) in _NUMBER_TYPES and type(c[1]) in _NUMBER_TYPES
        and -180.0 <= c[0] <= 180.0 and -90.0 <= c[1] <= 90.0   # NaN fails these too
    )


def _skip_summary(items: list[str]) -> str:
    return ", ".join(items[:5]) + (f", … (+{len(items) - 5} more)" if len(items) > 5 else "")


class GeoEngine:
    """
    Quantum-ready spatial data engine for Python.

    Loading methods (``load``, ``load_data``, ``add_point``) and output
    methods (``save``, ``map``, ``heatmap``) return ``self``; filters return a
    *new* GeoEngine. Both allow Pandas-style chaining::

        GeoEngine().load("data.csv").filter("city", "Mumbai").map("out.html")

    Parameters
    ----------
    verbose : print a confirmation line after writing maps (default True)
    """

    VERSION = __version__

    def __init__(self, verbose: bool = True) -> None:
        self._data: dict[str, Any] | None = None   # GeoJSON FeatureCollection
        self._source_path: str | None = None   # as the caller spelled it (for messages)
        self._source_real: str | None = None   # absolute real path (for the overwrite guard)
        self._columns: list[str] | None = None     # columns inherited from the parent engine
        self.verbose = verbose

    # ──────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────

    def _require_data(self, method_name: str) -> None:
        """Raise NoDataError if nothing has been loaded."""
        if self._data is None:
            raise NoDataError(method_name)

    def _require_features(self, method_name: str) -> None:
        """Raise NoDataError if nothing is loaded or the dataset is empty."""
        self._require_data(method_name)
        if not self._features():
            raise NoDataError(method_name, empty=True)

    def _features(self) -> list[Feature]:
        return self._data["features"] if self._data else []

    def _located(self) -> Iterator[tuple[int, Feature, Coord]]:
        """Yield ``(index, feature, (lat, lng))`` for features that have a location."""
        for i, feat in enumerate(self._features()):
            point = _representative_point(feat.get("geometry"))
            if point is not None:
                yield i, feat, point

    def _all_positions(self) -> tuple[list[float], list[float]]:
        """Latitudes and longitudes of every position of every geometry."""
        lats: list[float] = []
        lngs: list[float] = []
        for feat in self._features():
            geom = feat.get("geometry")
            if isinstance(geom, dict) and geom.get("type") == "Point":   # fast path
                coords = geom.get("coordinates")
                if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                    lngs.append(float(coords[0]))
                    lats.append(float(coords[1]))
                continue
            for pos in _iter_positions(geom):
                lngs.append(float(pos[0]))
                lats.append(float(pos[1]))
        return lats, lngs

    def _known_columns(self) -> list[str]:
        """
        Property names of this dataset: those of its features plus any it
        inherited from the engine it was filtered from (so a filter that
        matched nothing still knows the columns).
        """
        seen: dict[str, None] = dict.fromkeys(self._columns or [])
        for feat in self._features():
            seen.update(dict.fromkeys(feat.get("properties") or {}))
        return list(seen)

    def _clone_with(self, features: list[Feature], columns: list[str] | None = None) -> GeoEngine:
        """
        Return a new GeoEngine with the given features. Each feature dict and
        its properties dict are copied (a shallow copy, like pandas), so
        setting a property on a filtered result never changes the engine it
        came from; geometries and nested property values are shared.
        """
        new = GeoEngine(verbose=self.verbose)
        extra = {k: v for k, v in (self._data or {}).items() if k not in ("type", "features", "bbox")}
        new._data = {
            "type": "FeatureCollection",
            **extra,
            "features": [{**f, "properties": dict(f.get("properties") or {})} for f in features],
        }
        new._source_path = self._source_path
        new._source_real = self._source_real
        new._columns = columns if columns is not None else self._known_columns()
        return new

    @staticmethod
    def _normalise_features(
        raw: Iterable[Any],
        source: str,
        copy: bool = False,
        stacklevel: int = 5,
    ) -> list[Feature]:
        """
        Validate and normalise a sequence of GeoJSON objects into Features.

        * bare geometries are wrapped in Features
        * ``"properties": null`` becomes ``{}`` (RFC 7946 allows null)
        * ``"geometry": null`` features are kept but have no location
        * features whose geometry is malformed (unknown type, wrong nesting,
          non-numeric or out-of-range positions) are skipped with a single
          summary warning
        * with ``copy=True`` geometries are copied so later changes to the
          caller's objects don't leak into the engine
        """
        features: list[Feature] = []
        skipped: list[str] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                raise InvalidGeoJSONError(source, f"item #{i} is a {type(item).__name__}, not a dict")
            itype = item.get("type")
            if itype in GEOMETRY_TYPES:
                item = {"type": "Feature", "geometry": item, "properties": {}}
            elif itype != "Feature":
                # Accept Feature-shaped dicts with a missing or mis-cased type
                # ("feature"), which v1.0 loaded too.
                lenient = itype is None or (isinstance(itype, str) and itype.lower() == "feature")
                if not (lenient and ("geometry" in item or "properties" in item)):
                    raise InvalidGeoJSONError(
                        source, f"item #{i} has type {itype!r}, expected 'Feature'"
                    )
                item = {**item, "type": "Feature"}

            geom = item.get("geometry")
            if geom is not None:
                if not _is_plain_point(geom):   # fast path for the common case
                    problem = _geometry_problem(geom)
                    if problem:
                        skipped.append(f"#{i} ({problem})")
                        continue
                if copy:
                    geom = _copy_geometry(geom)

            # Objects parsed from a file are ours to keep; caller objects are copied.
            feat = dict(item) if copy else item
            props = item.get("properties")
            if copy or not isinstance(props, dict):
                feat["properties"] = dict(props) if isinstance(props, dict) else {}
            feat["geometry"] = geom
            features.append(feat)

        if skipped:
            warnings.warn(
                f"QOREgeo: skipped {len(skipped)} feature(s) with invalid geometry in "
                f"'{source}': {_skip_summary(skipped)}",
                stacklevel=stacklevel,
            )
        return features

    @classmethod
    def _to_feature_list(
        cls,
        data: Any,
        source: str,
        copy: bool = False,
        stacklevel: int = 5,
    ) -> tuple[list[Feature], dict[str, Any]]:
        """Accept any GeoJSON-ish object; return (features, extra top-level members)."""
        extra: dict[str, Any] = {}
        if isinstance(data, dict):
            dtype = data.get("type")
            if dtype == "FeatureCollection":
                raw = data.get("features")
                if not isinstance(raw, list):
                    raise InvalidGeoJSONError(source, "'features' is missing or not a list")
                extra = {k: v for k, v in data.items() if k not in ("type", "features", "bbox")}
            elif dtype == "Feature" or dtype in GEOMETRY_TYPES:
                raw = [data]
            else:
                raise InvalidGeoJSONError(
                    source, f"top-level type is {dtype!r}; expected a FeatureCollection, Feature or Geometry"
                )
        elif isinstance(data, (list, tuple)):
            raw = list(data)
        else:
            raise InvalidGeoJSONError(source, f"expected a JSON object or list, got {type(data).__name__}")
        return cls._normalise_features(raw, source, copy, stacklevel), extra

    # ──────────────────────────────────────────────────────────────────────
    # I/O
    # ──────────────────────────────────────────────────────────────────────

    def load(
        self,
        path: PathLike,
        lat_col: str | None = None,
        lng_col: str | None = None,
        encoding: str = "utf-8-sig",
        delimiter: str | None = None,
        infer_types: bool = False,
    ) -> GeoEngine:
        """
        Load spatial data from a CSV/TSV or GeoJSON file.

        Auto-detects latitude/longitude column names (latitude, lat, y, LAT …)
        and the delimiter (comma, semicolon, tab or pipe).
        Raises helpful errors for common mistakes.

        Parameters
        ----------
        path        : path to a .csv / .tsv / .txt or .geojson / .json file
        lat_col     : override latitude column name
        lng_col     : override longitude column name
        encoding    : file encoding (default utf-8-sig handles BOM)
        delimiter   : CSV delimiter; auto-detected if None (comma, semicolon,
                      tab or pipe). Quoting is honoured, and an Excel
                      ``sep=;`` first line is understood.
        infer_types : convert CSV values to int / float / bool / None where
                      unambiguous (values with leading zeros or a leading
                      '+' stay strings)

        Rows with blank coordinates are skipped silently; rows with
        unparseable or out-of-range coordinates are skipped with one
        summary warning. A ``geometry`` column holding GeoJSON (as written
        by ``save()``) restores non-Point geometries.

        Returns
        -------
        self, for method chaining
        """
        path_str = _fspath(path, "geo.load('cities.csv')")
        if os.path.isdir(path_str):
            raise GeoFileNotFoundError(path_str, is_dir=True)
        if not os.path.isfile(path_str):
            raise GeoFileNotFoundError(path_str)
        encoding = _check_encoding(encoding)
        if delimiter is not None and (not isinstance(delimiter, str) or len(delimiter) != 1
                                      or delimiter in '\r\n"'):
            raise InvalidArgumentError("delimiter", delimiter, "a single character such as ',' or ';'",
                                       "geo.load('data.csv', delimiter=';')")

        ext = os.path.splitext(path_str)[1].lower()

        if ext in _CSV_EXTS:
            self._data = self._load_csv(path_str, lat_col, lng_col, encoding, delimiter, infer_types)
        elif ext in _JSON_EXTS:
            self._data = self._load_geojson(path_str, encoding)
        else:
            raise UnsupportedFormatError(ext or "(no extension)")

        self._source_path = path_str
        # Resolved now, so the guard against overwriting this file still works
        # after os.chdir() (e.g. %cd in a notebook).
        self._source_real = os.path.realpath(path_str)
        self._columns = None
        return self

    @staticmethod
    def _resolve_coord_columns(
        columns: list[str],
        lat_col: str | None,
        lng_col: str | None,
        path: str,
    ) -> tuple[str, str]:
        """Find the latitude / longitude columns, honouring explicit overrides."""
        if lat_col is None and lng_col is None:
            return _detect_lat_lng_columns(columns, path)

        guess_lat = _find_column(columns, _LAT_CANDIDATES)
        guess_lng = _find_column(columns, _LNG_CANDIDATES)

        def suggestion(user: str | None, guess: str | None, label: str) -> str:
            if user is not None:
                close = difflib.get_close_matches(str(user), columns, n=1, cutoff=0.6)
                if close:
                    return close[0]
            return guess or f"<your {label} column>"

        def resolve(user: str | None, guess: str | None, label: str) -> str:
            if user is None:
                if guess is None:
                    raise ColumnNotFoundError(label, columns, path, hint=hint())
                return guess
            if user in columns:
                return user
            found = _find_column(columns, [str(user).strip().lower()])
            if found is None:
                raise ColumnNotFoundError(str(user), columns, path, hint=hint())
            return found

        def hint() -> str:
            lat = suggestion(lat_col, guess_lat, "lat")
            lng = suggestion(lng_col, guess_lng, "lng")
            return f"geo.load({path!r}, lat_col={lat!r}, lng_col={lng!r})"

        lat_c = resolve(lat_col, guess_lat, "latitude")
        lng_c = resolve(lng_col, guess_lng, "longitude")
        if lat_c == lng_c:
            raise InvalidArgumentError(
                "lng_col", lng_col, f"a different column from the latitude column ({lat_c!r})",
                hint(),
            )
        return lat_c, lng_c

    def _load_csv(
        self,
        path: str,
        lat_col: str | None,
        lng_col: str | None,
        encoding: str,
        delimiter: str | None = None,
        infer_types: bool = False,
    ) -> dict[str, Any]:
        features: list[Feature] = []
        problems: list[str] = []
        bad_geometry: list[str] = []
        ragged_rows = 0

        try:
            with _large_csv_fields(_CSV_FIELD_LIMIT), \
                    open(path, newline="", encoding=encoding) as f:
                # Skip leading blank lines and Excel's "sep=;" preamble.
                skipped_lines = 0
                header_pos = f.tell()
                first = f.readline()
                while first and not first.strip():
                    skipped_lines += 1
                    header_pos = f.tell()
                    first = f.readline()
                preamble = _SEP_PREAMBLE_RE.match(first)
                if preamble:
                    if delimiter is None:
                        delimiter = preamble.group(1)
                    skipped_lines += 1
                    header_pos = f.tell()
                    first = f.readline()
                    while first and not first.strip():
                        skipped_lines += 1
                        header_pos = f.tell()
                        first = f.readline()

                if delimiter is None:
                    if path.lower().endswith(".tsv"):
                        delimiter = "\t"
                    else:
                        sample = [first] + [f.readline() for _ in range(_SNIFF_ROWS)]
                        delimiter = _detect_delimiter(
                            [ln for ln in sample if ln.strip()],
                            prefer_tab=path.lower().endswith(".txt"),
                        )
                f.seek(header_pos)

                reader = csv.DictReader(f, delimiter=delimiter)
                if not reader.fieldnames:
                    raise EmptyDatasetError(path)
                names = [h.strip() if isinstance(h, str) else h for h in reader.fieldnames]
                dupes = sorted(n for n, k in Counter(names).items() if k > 1)
                if dupes:
                    warnings.warn(
                        f"QOREgeo: duplicate column name(s) {dupes} in '{path}'; only the last "
                        f"column with each name is kept.",
                        stacklevel=3,
                    )
                reader.fieldnames = names
                columns = list(dict.fromkeys(names))

                lat_c, lng_c = self._resolve_coord_columns(columns, lat_col, lng_col, path)
                geom_c = "geometry" if "geometry" in columns and "geometry" not in (lat_c, lng_c) else None

                for row in reader:
                    line = reader.line_num + skipped_lines
                    geometry: dict[str, Any] | None = None
                    cell = row.get(geom_c) if geom_c else None
                    if geom_c:
                        if isinstance(cell, str) and cell.lstrip().startswith("{"):
                            try:
                                parsed = json.loads(cell)
                            except ValueError:
                                parsed = None
                            if isinstance(parsed, dict) and _geometry_problem(parsed) is None:
                                geometry = parsed
                            else:
                                bad_geometry.append(f"line {line}")

                    if geometry is None:
                        raw_lat = row.get(lat_c)
                        raw_lng = row.get(lng_c)
                        if raw_lat is None or raw_lng is None or not str(raw_lat).strip() \
                                or not str(raw_lng).strip():
                            continue  # skip rows with missing coords
                        try:
                            lat, lng = _validate_coord(raw_lat, raw_lng)
                        except InvalidCoordinateError:
                            problems.append(f"line {line} (lat={raw_lat!r}, lng={raw_lng!r})")
                            continue
                        geometry = {"type": "Point", "coordinates": [lng, lat]}
                        exclude = {lat_c, lng_c}
                        if geom_c and not (cell or "").strip():
                            exclude.add(geom_c)   # empty geometry cell (a Point row from save())
                    else:
                        exclude = {lat_c, lng_c, "geometry"}

                    if None in row:
                        ragged_rows += 1
                    props = {
                        k: (_infer_value(v) if infer_types else v)
                        for k, v in row.items()
                        if k is not None and k not in exclude
                    }
                    features.append({"type": "Feature", "geometry": geometry, "properties": props})
        except UnicodeDecodeError as e:
            raise FileReadError(
                path, f"the file is not valid {encoding} text ({e.reason} at byte {e.start})",
                f"geo.load({path!r}, encoding='cp1252')   # or 'latin-1', 'utf-16', …",
            ) from e
        except csv.Error as e:
            raise FileReadError(path, f"CSV parse error: {e}") from e

        if problems:
            warnings.warn(
                f"QOREgeo: skipped {len(problems)} row(s) in '{path}' with non-numeric or "
                f"out-of-range coordinates (lat must be -90..90 and lng -180..180; check "
                f"whether the columns are swapped): {_skip_summary(problems)}",
                stacklevel=3,
            )
        if bad_geometry:
            warnings.warn(
                f"QOREgeo: {len(bad_geometry)} row(s) in '{path}' have an invalid GeoJSON "
                f"'geometry' cell; their latitude/longitude was used instead: "
                f"{_skip_summary(bad_geometry)}",
                stacklevel=3,
            )
        if ragged_rows:
            warnings.warn(
                f"QOREgeo: {ragged_rows} row(s) in '{path}' had more values than header "
                f"columns; the extra values were dropped.",
                stacklevel=3,
            )
        if not features:
            raise EmptyDatasetError(path)

        return {"type": "FeatureCollection", "features": features}

    def _load_geojson(self, path: str, encoding: str) -> dict[str, Any]:
        try:
            with open(path, encoding=encoding) as f:
                data = json.load(f)
        except UnicodeDecodeError as e:
            raise FileReadError(
                path, f"the file is not valid {encoding} text ({e.reason} at byte {e.start})",
                f"geo.load({path!r}, encoding='utf-8')   # GeoJSON should be UTF-8",
            ) from e
        except ValueError as e:   # JSONDecodeError, or an integer with too many digits
            raise InvalidGeoJSONError(path, f"not valid JSON ({e})") from e
        except RecursionError:
            raise InvalidGeoJSONError(path, "the JSON is nested too deeply") from None

        features, extra = self._to_feature_list(data, path, stacklevel=5)
        if not features:
            raise EmptyDatasetError(path)
        return {"type": "FeatureCollection", **extra, "features": features}

    def load_data(self, features: Any) -> GeoEngine:
        """
        Load GeoJSON directly from Python objects (no file needed).

        Parameters
        ----------
        features : a list of GeoJSON Feature (or Geometry) dicts, a
                   FeatureCollection dict, a single Feature, or a Geometry

        Returns
        -------
        self
        """
        if features is None:
            raise EmptyDatasetError("<in-memory data>")
        if not isinstance(features, (dict, list, tuple)):
            try:
                features = list(features)
            except TypeError:
                raise InvalidGeoJSONError(
                    "<in-memory data>", f"expected a list or dict, got {type(features).__name__}"
                ) from None
        feats, extra = self._to_feature_list(features, "<in-memory data>", copy=True, stacklevel=4)
        if not feats:
            raise EmptyDatasetError("<in-memory data>")
        self._data = {"type": "FeatureCollection", **extra, "features": feats}
        self._source_path = None
        self._source_real = None
        self._columns = None
        return self

    def add_point(self, lat: float, lng: float, **properties: Any) -> GeoEngine:
        """
        Append a single Point feature (creates the dataset if needed).

        Examples
        --------
        >>> geo = GeoEngine().add_point(28.6139, 77.2090, name="Delhi")
        >>> geo.add_point(19.0760, 72.8777, name="Mumbai").count()
        2
        """
        lat_f, lng_f = _validate_coord(lat, lng)
        if self._data is None:
            self._data = {"type": "FeatureCollection", "features": []}
        self._columns = None
        self._data["features"].append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lng_f, lat_f]},
                "properties": dict(properties),
            }
        )
        return self

    def save(self, path: PathLike, encoding: str = "utf-8", csv_safe: bool = False) -> GeoEngine:
        """
        Save current features to a GeoJSON or CSV file.

        CSV output has ``latitude`` / ``longitude`` columns (for non-Point
        features: their representative point, plus a ``geometry`` column
        holding the full GeoJSON geometry, which ``load()`` reads back).
        Nested property values are written as JSON. NaN / Infinity become
        null in GeoJSON. The file is written atomically, so a failed save
        never truncates an existing file.

        Parameters
        ----------
        path     : output path (.geojson, .json, .csv or .tsv)
        encoding : file encoding
        csv_safe : for CSV/TSV, prefix ``'`` to each text cell that a
                   spreadsheet app would run as a formula (one starting with
                   ``= + - @``, tab or CR), in values and in header (property
                   name) cells. This protects files you share from
                   CSV/formula injection. Real numbers such as ``-12.5`` are
                   left alone.

        Returns
        -------
        self
        """
        self._require_data("save")
        path_str = _fspath(path, "geo.save('output.geojson')")
        encoding = _check_encoding(encoding)
        ext = os.path.splitext(path_str)[1].lower()
        if ext not in _SAVE_EXTS:
            raise UnsupportedFormatError(ext or "(no extension)", supported=_SAVE_EXTS)

        if ext in _JSON_EXTS:
            text = json.dumps(_json_safe(self._data), indent=2, ensure_ascii=False, allow_nan=False)
            _atomic_write(path_str, _escape_surrogates(text) + "\n", encoding=encoding)
            return self

        features = self._features()
        non_point = any(
            isinstance(feat.get("geometry"), dict) and feat["geometry"].get("type") != "Point"
            for feat in features
        )
        reserved = {"latitude", "longitude"} | ({"geometry"} if non_point else set())

        # Gather all property keys (first-seen order)
        all_keys: list[str] = []
        seen = set()
        dropped = set()
        for feat in features:
            for k in feat.get("properties", {}).keys():
                if k in reserved:
                    dropped.add(k)
                elif k not in seen:
                    all_keys.append(k)
                    seen.add(k)
        if dropped:
            warnings.warn(
                f"QOREgeo: properties {sorted(dropped)} clash with CSV coordinate columns "
                f"and were not written; the geometry is used instead.",
                stacklevel=2,
            )

        fieldnames = ["latitude", "longitude"] + (["geometry"] if non_point else []) + all_keys

        buffer = io.StringIO()
        delimiter = "\t" if ext == ".tsv" else ","
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, delimiter=delimiter)
        # Header cells are property names, which can be formulas too.
        csv.writer(buffer, delimiter=delimiter).writerow(
            [_formula_safe(k) if csv_safe else k for k in fieldnames]
        )
        for feat in features:
            geom = feat.get("geometry")
            point = _representative_point(geom)
            row: dict[str, Any] = {
                "latitude": point[0] if point else "",
                "longitude": point[1] if point else "",
            }
            if non_point:
                row["geometry"] = (
                    json.dumps(_json_safe(geom), ensure_ascii=False, allow_nan=False)
                    if isinstance(geom, dict) and geom.get("type") != "Point" else ""
                )
            for k, v in feat.get("properties", {}).items():
                if k not in reserved:
                    row[k] = _json_cell(v)
                    if csv_safe:
                        row[k] = _formula_safe(row[k])
            writer.writerow(row)

        _atomic_write(path_str, buffer.getvalue(), encoding=encoding, errors="backslashreplace")
        return self

    # ──────────────────────────────────────────────────────────────────────
    # Geometry operations
    # ──────────────────────────────────────────────────────────────────────

    def distance(
        self,
        point_a: PointLike,
        point_b: PointLike,
        unit: str = "km",
        method: str = "haversine",
    ) -> float:
        """
        Calculate the distance between two coordinates.

        Parameters
        ----------
        point_a : (lat, lng) tuple (or a ``{'lat': …, 'lng': …}`` dict, a
                  GeoJSON Point or a Point Feature)
        point_b : (lat, lng) tuple
        unit    : 'km' | 'm' | 'miles' | 'mi' | 'ft' | 'yd' | 'nmi'
        method  : 'haversine' (sphere, default) or 'vincenty' (WGS-84
                  ellipsoid, ~0.5 mm accuracy)

        Returns
        -------
        Distance as a float in the requested unit, rounded to 4 decimals.

        Examples
        --------
        >>> geo.distance((28.6139, 77.2090), (19.0760, 72.8777))
        1148.0965
        >>> geo.distance((28.6139, 77.2090), (19.0760, 72.8777), unit='miles')
        713.3941
        """
        a = _coerce_point(point_a)
        b = _coerce_point(point_b)
        per_unit = _km_per_unit(unit)
        km = _distance_km(a, b, _check_method(method))
        return round(km / per_unit, 4)

    @overload
    def bearing(self, point_a: PointLike, point_b: PointLike,
                as_degrees: Literal[False] = ...) -> str: ...
    @overload
    def bearing(self, point_a: PointLike, point_b: PointLike,
                as_degrees: Literal[True]) -> float: ...
    @overload
    def bearing(self, point_a: PointLike, point_b: PointLike,
                as_degrees: bool) -> str | float: ...

    def bearing(
        self,
        point_a: PointLike,
        point_b: PointLike,
        as_degrees: bool = False,
    ) -> str | float:
        """
        Calculate the initial compass bearing from point_a to point_b.

        Parameters
        ----------
        point_a    : (lat, lng) origin
        point_b    : (lat, lng) destination
        as_degrees : if True, return numeric degrees (0 to 360) instead of a
                     16-point compass string

        Returns
        -------
        Compass direction string e.g. 'South-Southwest' or float degrees.
        Identical points return 0.0 / 'North'.

        Examples
        --------
        >>> geo.bearing((28.6139, 77.2090), (19.0760, 72.8777))
        'South-Southwest'
        >>> geo.bearing((28.6139, 77.2090), (19.0760, 72.8777), as_degrees=True)
        203.47
        """
        a = _coerce_point(point_a)
        b = _coerce_point(point_b)
        deg = _initial_bearing_deg(a, b)
        return round(deg, 2) % 360.0 if as_degrees else _bearing_to_compass(deg)

    def destination(
        self,
        point: PointLike,
        distance: float,
        bearing: float,
        unit: str = "km",
    ) -> Coord:
        """
        Find the point reached by travelling ``distance`` from ``point`` on
        an initial compass ``bearing`` (degrees, 0 = north, 90 = east).

        Returns
        -------
        (lat, lng) tuple, rounded to 6 decimals (~0.1 m).

        Examples
        --------
        >>> geo.destination((28.6139, 77.2090), 10, 90)
        (28.613861, 77.311444)
        """
        origin = _coerce_point(point)
        dist = _finite_number("distance", distance, "geo.destination(point, 10, 90)")
        brng = _finite_number("bearing", bearing, "geo.destination(point, 10, 90)")
        lat, lng = _destination_point(origin, dist * _km_per_unit(unit), brng)
        return round(lat, 6), round(lng, 6)

    def midpoint(self, point_a: PointLike, point_b: PointLike) -> Coord:
        """
        Great-circle midpoint between two coordinates.

        Examples
        --------
        >>> geo.midpoint((28.6139, 77.2090), (19.0760, 72.8777))
        (23.86007, 74.963456)
        """
        lat, lng = _midpoint(_coerce_point(point_a), _coerce_point(point_b))
        return round(lat, 6), round(lng, 6)

    def buffer(
        self,
        center: PointLike,
        radius: float,
        unit: str = "km",
        num_points: int = 64,
    ) -> dict[str, Any]:
        """
        Create a circular buffer polygon around a center point.

        Parameters
        ----------
        center     : (lat, lng) center of the circle
        radius     : radius of the circle (> 0)
        unit       : 'km' | 'm' | 'miles' | 'mi' | 'ft' | 'yd' | 'nmi'
        num_points : polygon resolution, >= 3 (higher = smoother)

        Returns
        -------
        A GeoJSON Polygon dict representing the buffer zone. It also carries
        ``_center`` and ``_radius_km`` so ``point_in_polygon`` can test it
        exactly, even across the poles or the antimeridian.

        Examples
        --------
        >>> zone = geo.buffer((28.61, 77.20), radius=10, unit='km')
        >>> geo.point_in_polygon((28.65, 77.22), zone)
        True
        """
        lat, lng = _coerce_point(center)
        radius_f = _positive_radius(radius)
        per_unit = _km_per_unit(unit)
        _positive_int("num_points", num_points, 3, "geo.buffer(center, radius=10, num_points=64)")

        radius_km = radius_f * per_unit
        half_circumference = math.pi * EARTH_RADIUS_KM
        if radius_km > half_circumference:
            raise InvalidArgumentError(
                "radius", radius, f"at most {half_circumference:.0f} km (half the Earth's "
                "circumference), since a larger circle covers the whole globe",
                "geo.buffer(center, radius=500)",
            )
        coords = _generate_circle_polygon((lat, lng), radius_km, num_points)

        return {
            "type": "Polygon",
            "coordinates": [coords],
            "_center": (lat, lng),
            "_radius_km": radius_km,
        }

    def point_in_polygon(
        self,
        point: PointLike,
        polygon: dict[str, Any],
    ) -> bool:
        """
        Test whether a point falls inside a polygon.

        Supports ``buffer()`` results (exact circle test, also across the
        poles and the antimeridian), GeoJSON Polygons (holes respected),
        MultiPolygons, and Features wrapping either. Polygon edges are
        straight lines in lng/lat; for a shape crossing 180° use continuous
        longitudes (177 → 182) or split it into a MultiPolygon at ±180°.

        Parameters
        ----------
        point   : (lat, lng)
        polygon : GeoJSON Polygon / MultiPolygon / Feature or buffer() result

        Returns
        -------
        True if inside, False otherwise.

        Examples
        --------
        >>> zone = geo.buffer((28.61, 77.20), 10)
        >>> geo.point_in_polygon((28.65, 77.22), zone)
        True
        >>> geo.point_in_polygon((19.07, 72.87), zone)
        False
        """
        lat, lng = _coerce_point(point)
        return _point_in_geometry(lat, lng, polygon)

    def area(self, polygon: dict[str, Any], unit: str = "km2") -> float:
        """
        Area of a Polygon / MultiPolygon / Feature / buffer on a sphere of
        mean Earth radius, not the WGS-84 ellipsoid (Chamberlain & Duquette,
        as in Turf.js). Results are within about 0.5% of ellipsoidal areas.

        Parameters
        ----------
        polygon : GeoJSON polygon-like dict (holes are subtracted); positions
                  must be [lng, lat] in range, else InvalidBufferError
        unit    : 'km2' | 'm2' | 'ha' | 'acres' | 'mi2' | 'ft2'

        Examples
        --------
        >>> geo.area(geo.buffer((28.61, 77.20), radius=1))   # ≈ π km²
        3.1416
        """
        per_unit = _m2_per_unit(unit)
        return round(_polygon_area_m2(polygon) / per_unit, 4)

    def length(self, line: Any, unit: str = "km", method: str = "haversine") -> float:
        """
        Length of a path.

        Parameters
        ----------
        line   : a list of (lat, lng) points, or a GeoJSON LineString /
                 MultiLineString (or a Feature wrapping one)
        unit   : distance unit (see distance())
        method : 'haversine' or 'vincenty'

        Examples
        --------
        >>> geo.length([(28.6139, 77.2090), (26.9124, 75.7873), (19.0760, 72.8777)])
        1156.0342
        """
        per_unit = _km_per_unit(unit)
        method = _check_method(method)
        total = sum(_path_length_km(part, method) for part in _line_parts(line))
        return round(total / per_unit, 4)

    def distance_to_line(self, point: PointLike, line: Any, unit: str = "km") -> float:
        """
        Shortest great-circle distance from ``point`` to a path (e.g. a road,
        river or pipeline).

        Parameters
        ----------
        point : (lat, lng)
        line  : a list of (lat, lng) points, or a GeoJSON LineString /
                MultiLineString (or a Feature wrapping one)
        unit  : distance unit (see distance())

        Examples
        --------
        >>> geo.distance_to_line((53.2611, -0.7972), [(53.3206, -1.7297), (53.1887, 0.1334)])
        0.3075
        """
        p = _coerce_point(point)
        per_unit = _km_per_unit(unit)
        parts = [part for part in _line_parts(line) if part]
        if not parts:
            raise InvalidArgumentError("line", line, "at least one point",
                                       "geo.distance_to_line(p, [(28.61, 77.20), (19.07, 72.87)])")
        return round(_point_line_km(p, parts) / per_unit, 4)

    def geohash(self, point: PointLike, precision: int = 9) -> str:
        """
        Encode a point as a geohash string (precision 1 to 12 characters).

        Examples
        --------
        >>> geo.geohash((57.64911, 10.40744), precision=11)
        'u4pruydqqvj'
        """
        lat, lng = _coerce_point(point)
        if isinstance(precision, bool) or not isinstance(precision, int) or not 1 <= precision <= 12:
            raise InvalidArgumentError("precision", precision, "an integer from 1 to 12",
                                       "geo.geohash((28.61, 77.20), precision=7)")
        return _geohash_encode(lat, lng, precision)

    def geohash_decode(self, geohash: str) -> Coord:
        """
        Decode a geohash to the (lat, lng) centre of its cell.

        Examples
        --------
        >>> geo.geohash_decode('u4pruydqqvj')
        (57.649111, 10.40744)
        """
        if not isinstance(geohash, str) or not geohash.strip():
            raise InvalidArgumentError("geohash", geohash, "a non-empty geohash string",
                                       "geo.geohash_decode('ttnfv2u')")
        min_lat, min_lng, max_lat, max_lng = _geohash_bounds(geohash.strip())
        return round((min_lat + max_lat) / 2, 6), round((min_lng + max_lng) / 2, 6)

    def nearest(
        self,
        point: PointLike,
        unit: str = "km",
        method: str = "haversine",
    ) -> dict[str, Any]:
        """
        Find the nearest feature to a given point.

        Non-Point features are measured from their representative point
        (polygon centroid / vertex mean); features without geometry are
        ignored.

        Parameters
        ----------
        point  : (lat, lng)
        unit   : distance unit for the result
        method : 'haversine' or 'vincenty'

        Returns
        -------
        Dict with keys ``'feature'``, ``'distance'``, ``'index'``.

        Examples
        --------
        >>> result = geo.nearest((28.61, 77.20))
        >>> result['feature']['properties']['name']
        'Delhi'
        """
        self._require_data("nearest")
        origin = _coerce_point(point)
        per_unit = _km_per_unit(unit)
        method = _check_method(method)
        self._require_features("nearest")

        best: tuple[float, int, Feature] | None = None
        for i, feat, loc in self._located():
            d = _distance_km(origin, loc, method)
            if best is None or d < best[0]:
                best = (d, i, feat)

        if best is None:
            raise NoDataError("nearest", empty=True)
        return {"feature": best[2], "distance": round(best[0] / per_unit, 4), "index": best[1]}

    def k_nearest(
        self,
        point: PointLike,
        k: int = 5,
        unit: str = "km",
        method: str = "haversine",
    ) -> GeoEngine:
        """
        Return the ``k`` features closest to ``point``, nearest first, with a
        ``_distance`` property injected (in ``unit``).

        Examples
        --------
        >>> geo.k_nearest((28.61, 77.20), k=3).count()
        3
        """
        self._require_data("k_nearest")
        origin = _coerce_point(point)
        per_unit = _km_per_unit(unit)
        method = _check_method(method)
        _positive_int("k", k, 1, "geo.k_nearest(point, k=5)")

        scored = (
            (_distance_km(origin, loc, method), i, feat) for i, feat, loc in self._located()
        )
        best = heapq.nsmallest(k, scored, key=lambda t: (t[0], t[1]))
        return self._clone_with([
            {**feat, "properties": {**feat.get("properties", {}), "_distance": round(d / per_unit, 4)}}
            for d, _, feat in best
        ])

    # ──────────────────────────────────────────────────────────────────────
    # Filtering
    # ──────────────────────────────────────────────────────────────────────

    def filter(self, column: str, value: Any, op: str = "==") -> GeoEngine:
        """
        Filter features by a property value.

        Parameters
        ----------
        column : property key
        value  : value to compare against
        op     : '==' (default) | '!=' | '>' | '>=' | '<' | '<=' |
                 'in' | 'not in' | 'contains'

        Equality is case-insensitive for strings, and numeric when ``value``
        is a number (so ``filter('pop', 5)`` matches ``"5"`` and ``5.0``).
        Ordering operators compare numerically when ``value`` is a number
        and as case-insensitive strings otherwise; features whose property
        is missing or not comparable are excluded. ``'in'`` takes a list.

        Returns
        -------
        New GeoEngine with matching features.

        Examples
        --------
        >>> geo.filter("state", "Maharashtra").count()
        2
        >>> geo.filter("population", 15_000_000, op=">").count()
        2
        """
        self._require_data("filter")
        if op not in _FILTER_OPS:
            raise InvalidArgumentError("op", op, " | ".join(repr(o) for o in _FILTER_OPS),
                                       "geo.filter('population', 1000000, op='>')")
        features = self._features()

        # The column must exist in this dataset (or the one it was filtered
        # from), so a typo is caught even when an earlier filter left 0 rows.
        keys = self._known_columns()
        if column not in keys:
            raise ColumnNotFoundError(
                column, keys, self._source_path or "dataset",
                hint=f"geo.filter('{keys[0] if keys else 'column_name'}', value)",
            )
        if not features:
            return self._clone_with([], keys)

        if op in ("in", "not in"):
            if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
                raise InvalidArgumentError("value", value, f"a list of values for op={op!r}",
                                           f"geo.filter('{column}', ['a', 'b'], op='{op}')")
            options = list(value)

        def match(props: dict[str, Any]) -> bool:
            present = column in props
            prop = props.get(column, "")
            if op == "==":
                return _values_equal(prop, value)
            if op == "!=":
                return not _values_equal(prop, value)
            if op == "in":
                return any(_values_equal(prop, v) for v in options)
            if op == "not in":
                return not any(_values_equal(prop, v) for v in options)
            if not present or prop is None:
                return False
            if op == "contains":
                return _norm_str(value) in _norm_str(prop)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                left: Any = _as_number(prop)
                right: Any = value
                if left is None:
                    return False
            else:
                left, right = _norm_str(prop), _norm_str(value)
            if op == ">":
                return bool(left > right)
            if op == ">=":
                return bool(left >= right)
            if op == "<":
                return bool(left < right)
            return bool(left <= right)

        return self._clone_with([f for f in features if match(f.get("properties", {}))], keys)

    def where(self, predicate: Callable[[dict[str, Any]], Any]) -> GeoEngine:
        """
        Filter features with a custom function of their properties.

        Examples
        --------
        >>> big = geo.where(lambda p: int(p["population"]) > 10_000_000)
        """
        self._require_data("where")
        if not callable(predicate):
            raise InvalidArgumentError("predicate", predicate, "a function taking a properties dict",
                                       "geo.where(lambda p: p['state'] == 'Kerala')")
        return self._clone_with([f for f in self._features() if predicate(f.get("properties", {}))])

    def filter_by_radius(
        self,
        lat: float,
        lng: float,
        radius: float,
        unit: str = "km",
        method: str = "haversine",
    ) -> GeoEngine:
        """
        Filter features within a given radius of a point.

        Results are sorted by distance (nearest first) and a ``_distance``
        property (in ``unit``) is injected into each feature.

        Parameters
        ----------
        lat    : center latitude
        lng    : center longitude
        radius : search radius (> 0)
        unit   : distance unit (see distance())
        method : 'haversine' or 'vincenty'

        Returns
        -------
        New GeoEngine with nearby features sorted nearest-first.

        Examples
        --------
        >>> nearby = geo.filter_by_radius(28.61, 77.20, radius=50)
        >>> nearby.count()
        1
        """
        self._require_data("filter_by_radius")
        center = _validate_coord(lat, lng)
        radius_f = _positive_radius(radius)
        per_unit = _km_per_unit(unit)
        method = _check_method(method)

        results = []
        for i, feat, loc in self._located():
            d = _distance_km(center, loc, method) / per_unit
            if d <= radius_f:
                f2 = {**feat, "properties": {**feat.get("properties", {}), "_distance": round(d, 4)}}
                results.append((d, i, f2))

        results.sort(key=lambda x: (x[0], x[1]))
        return self._clone_with([r[2] for r in results])

    def filter_by_bbox(
        self,
        min_lat: float,
        min_lng: float,
        max_lat: float,
        max_lng: float,
    ) -> GeoEngine:
        """
        Keep features whose location lies inside a bounding box
        (south, west, north, east). If ``min_lng > max_lng`` the box is taken
        to cross the antimeridian.

        Accepts the output of ``bounds()`` directly::

            other_area = geo.filter_by_bbox(**other.bounds())

        Examples
        --------
        >>> geo.filter_by_bbox(18.0, 72.0, 20.0, 74.0).count()   # Mumbai + Pune
        2
        """
        self._require_data("filter_by_bbox")
        # Longitudes up to ±360 are accepted so the bounds() of shapes drawn
        # continuously across the antimeridian (e.g. buffers) can be passed in.
        s, _ = _validate_coord(min_lat, 0)
        n, _ = _validate_coord(max_lat, 0)
        w = _finite_number("min_lng", min_lng, "geo.filter_by_bbox(18, 72, 20, 74)")
        e = _finite_number("max_lng", max_lng, "geo.filter_by_bbox(18, 72, 20, 74)")
        for name, value in (("min_lng", w), ("max_lng", e)):
            if not -360.0 <= value <= 360.0:
                raise InvalidCoordinateError(min_lat if name == "min_lng" else max_lat, value)
        if s > n:
            raise InvalidArgumentError("min_lat", min_lat, f"a value <= max_lat ({max_lat})",
                                       "geo.filter_by_bbox(min_lat=18, min_lng=72, max_lat=20, max_lng=74)")
        whole_world = w <= e and e - w >= 360.0
        w, e = _normalize_lng(w), _normalize_lng(e)
        crosses = w > e

        def inside(lat: float, lng: float) -> bool:
            if not s <= lat <= n:
                return False
            if whole_world:
                return True
            return (lng >= w or lng <= e) if crosses else (w <= lng <= e)

        return self._clone_with([feat for _, feat, (la, lo) in self._located() if inside(la, lo)])

    def filter_by_polygon(self, polygon: dict[str, Any]) -> GeoEngine:
        """
        Keep features whose location lies inside ``polygon`` (a buffer(),
        Polygon, MultiPolygon or Feature), for example all customers inside
        a delivery zone.

        Examples
        --------
        >>> zone = geo.buffer((19.07, 72.87), radius=200)
        >>> geo.filter_by_polygon(zone).count()   # Mumbai + Pune
        2
        """
        self._require_data("filter_by_polygon")
        inside = _polygon_tester(polygon)   # validates up front, even for empty datasets
        return self._clone_with([feat for _, feat, (la, lo) in self._located() if inside(la, lo)])

    def intersects(self, geometry_a: Any, geometry_b: Any) -> bool:
        """
        True if two shapes share at least one point (touching counts).

        Accepts GeoJSON geometries of any type, Features, FeatureCollections
        and ``buffer()`` results (tested as exact circles on the sphere, so
        also around the poles). Edges are straight lines in lng/lat, as in
        ``point_in_polygon()``; shapes meeting at ±180° touch.

        Examples
        --------
        >>> zone_a = geo.buffer((28.61, 77.20), 10)
        >>> zone_b = geo.buffer((28.70, 77.21), 5)
        >>> geo.intersects(zone_a, zone_b)
        True
        """
        return _geometries_intersect(geometry_a, geometry_b)

    def filter_intersects(self, geometry: Any) -> GeoEngine:
        """
        Keep features whose *whole geometry* intersects ``geometry``, for
        example roads crossing a flood zone, or districts touching a delivery
        area.
        (``filter_by_polygon`` instead tests each feature's single
        representative point.)

        Examples
        --------
        >>> zone = geo.buffer((19.07, 72.87), radius=200)
        >>> geo.filter_intersects(zone).count()
        2
        """
        self._require_data("filter_intersects")
        query = _prepare_shape(geometry, "geometry")   # validated and indexed once
        return self._clone_with([
            f for i, f in enumerate(self._features())
            if f.get("geometry") is not None
            and _shapes_intersect(_prepare_shape(f["geometry"], f"features[{i}].geometry"), query)
        ])

    def filter_by_line(self, line: Any, distance: float, unit: str = "km") -> GeoEngine:
        """
        Keep features within ``distance`` of a path (a "line buffer"), e.g.
        every store within 2 km of a highway. Results are sorted nearest-first
        with a ``_distance`` property (in ``unit``) injected.

        Parameters
        ----------
        line     : a list of (lat, lng) points, or a GeoJSON LineString /
                   MultiLineString (or a Feature wrapping one)
        distance : maximum distance from the line (> 0)
        unit     : distance unit (see distance())

        Examples
        --------
        >>> highway = [(28.6139, 77.2090), (26.9124, 75.7873)]   # Delhi → Jaipur
        >>> geo.filter_by_line(highway, distance=50).count()
        1
        """
        self._require_data("filter_by_line")
        limit = _positive_radius(distance)
        per_unit = _km_per_unit(unit)
        parts = [part for part in _line_parts(line) if part]
        if not parts:
            raise InvalidArgumentError("line", line, "at least one point",
                                       "geo.filter_by_line([(28.61, 77.20), (26.91, 75.79)], 5)")

        results = []
        for i, feat, loc in self._located():
            d = _point_line_km(loc, parts) / per_unit
            if d <= limit:
                f2 = {**feat, "properties": {**feat.get("properties", {}), "_distance": round(d, 4)}}
                results.append((d, i, f2))
        results.sort(key=lambda x: (x[0], x[1]))
        return self._clone_with([r[2] for r in results])

    def sort_by(self, column: str, descending: bool = False) -> GeoEngine:
        """
        Sort features by a property. Numbers sort numerically (numeric
        strings included), other values case-insensitively; features
        missing the property go last. A column no feature has raises
        ColumnNotFoundError (like ``filter()``), so a typo can't silently
        leave the data unsorted.

        Examples
        --------
        >>> geo.sort_by("population", descending=True).head(3)
        """
        self._require_data("sort_by")
        keys = self._known_columns()
        if column not in keys:
            raise ColumnNotFoundError(
                column, keys, self._source_path or "dataset",
                hint=f"geo.sort_by('{keys[0] if keys else 'column_name'}', descending=True)",
            )
        present: list[Feature] = []
        missing: list[Feature] = []
        for feat in self._features():
            v = feat.get("properties", {}).get(column)
            (missing if v is None or v == "" else present).append(feat)

        def key(feat: Feature) -> tuple[int, Any]:
            v = feat["properties"][column]
            num = _as_number(v)
            return (0, num) if num is not None else (1, _norm_str(v))

        present.sort(key=key, reverse=descending)
        return self._clone_with(present + missing)

    def head(self, n: int = 5) -> GeoEngine:
        """Return a new GeoEngine with the first ``n`` features."""
        self._require_data("head")
        _positive_int("n", n, 0, "geo.head(10)")
        return self._clone_with(list(self._features()[:n]))

    # ──────────────────────────────────────────────────────────────────────
    # Data access
    # ──────────────────────────────────────────────────────────────────────

    def count(self) -> int:
        """Return the number of loaded features."""
        return len(self._features())

    def get_features(self) -> list[Feature]:
        """Return the raw list of GeoJSON Feature dicts (empty list if 0 features)."""
        self._require_data("get_features")
        return self._features()

    def get_geojson(self) -> dict[str, Any]:
        """Return the full GeoJSON FeatureCollection dict."""
        self._require_data("get_geojson")
        return self._data  # type: ignore[return-value]

    def bounds(self) -> dict[str, float]:
        """
        Return the bounding box of all loaded geometries.

        Returns
        -------
        Dict with keys ``min_lat``, ``max_lat``, ``min_lng``, ``max_lng``.
        """
        self._require_features("bounds")
        lats, lngs = self._all_positions()
        if not lats:
            raise NoDataError("bounds", empty=True)
        return {
            "min_lat": min(lats),
            "max_lat": max(lats),
            "min_lng": min(lngs),
            "max_lng": max(lngs),
        }

    def bbox(self) -> list[float]:
        """
        Bounding box in GeoJSON (RFC 7946 §5) order: ``[west, south, east, north]``.

        Unlike ``bounds()``, data spanning the antimeridian gets the tight box
        with ``west > east`` (e.g. Fiji: ``[177.0, -20.0, -178.0, -16.0]``).
        """
        self._require_features("bbox")
        lats, lngs = self._all_positions()
        if not lats:
            raise NoDataError("bbox", empty=True)
        west, east = _lng_span(lngs)
        return [west, min(lats), east, max(lats)]

    def centroid(self) -> Coord:
        """
        Geographic centre of all features (spherical mean of their
        locations, safe across the antimeridian), rounded to 6 decimals.
        """
        self._require_features("centroid")
        points = [loc for _, _, loc in self._located()]
        if not points:
            raise NoDataError("centroid", empty=True)
        lat, lng = _spherical_mean(points)
        return round(lat, 6), round(lng, 6)

    def __len__(self) -> int:
        return self.count()

    def __iter__(self) -> Iterator[Feature]:
        return iter(list(self._features()))

    def __repr__(self) -> str:
        status = f"{self.count()} features" if self._data is not None else "no data loaded"
        return f"<GeoEngine [{status}]>"

    # ──────────────────────────────────────────────────────────────────────
    # Visualisation
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _check_zoom(zoom: int | None) -> int | None:
        if zoom is None:
            return None
        if isinstance(zoom, bool) or not isinstance(zoom, int) or not 0 <= zoom <= 22:
            raise InvalidArgumentError("zoom", zoom, "an integer from 0 to 22, or None",
                                       "geo.map('out.html', zoom=6)")
        return zoom

    def _output_path(self, output_path: Any, example: str) -> str:
        """``output_path`` as a str, refusing the file the data was loaded from."""
        path = _fspath(output_path, example)
        if self._source_real is not None and _same_file(path, self._source_real):
            raise InvalidArgumentError(
                "output_path", path,
                "a different file (this is the data file that was loaded, and writing "
                "here would overwrite it)",
                example,
            )
        return path

    @staticmethod
    def _overlay_list(overlays: Any) -> list[dict[str, Any]] | None:
        if overlays is None or isinstance(overlays, dict):
            return [overlays] if overlays else None
        if isinstance(overlays, (str, bytes)) or not isinstance(overlays, Iterable):
            raise InvalidArgumentError(
                "overlays", overlays, "a list of GeoJSON dicts (e.g. geo.buffer() results)",
                "geo.map('out.html', overlays=[geo.buffer((28.61, 77.20), 10)])",
            )
        return list(overlays)

    def _map_page(
        self,
        title: str,
        zoom: int | None,
        center: PointLike | None,
        tiles: str,
        overlays: Any,
        attribution: str | None,
        output_path: str | None,
    ) -> tuple[str, int]:
        self._require_data("map")
        return map_page(
            self._features(),
            title=title,
            zoom=self._check_zoom(zoom),
            center=_coerce_point(center) if center is not None else None,
            tiles=tiles,
            overlays=self._overlay_list(overlays),
            attribution=attribution,
            output_path=output_path,
        )

    def map(
        self,
        output_path: PathLike = "map.html",
        title: str = "QOREgeo Map",
        zoom: int | None = None,
        center: PointLike | None = None,
        tiles: str = "light",
        overlays: Sequence[dict[str, Any]] | dict[str, Any] | None = None,
        attribution: str | None = None,
    ) -> GeoEngine:
        """
        Export an interactive Leaflet.js map to an HTML file.

        Points, lines and polygons are all drawn; property popups are
        HTML-escaped. The basemap is OpenStreetMap, whose tile servers only
        serve web pages, so view the file with ``geo.show()`` or
        ``qoregeo serve map.html`` (or pick ``tiles='topo'``, ``'satellite'``,
        ``'osm-de'`` or ``'none'``, which also work when opened from disk).

        Parameters
        ----------
        output_path : where to write the HTML file (never the loaded data file)
        title       : map title shown in the browser tab
        zoom        : with ``center``: the initial zoom (default 10);
                      without: the maximum zoom used when fitting the data
        center      : (lat, lng) initial center; fits all features if None
        tiles       : basemap, one of 'light' (OpenStreetMap, softened; default) |
                      'osm' | 'dark' | 'humanitarian' | 'topo' | 'satellite' |
                      'cyclosm' | 'osm-de' | 'osm-fr' | 'none', or a custom
                      ``https://…/{z}/{x}/{y}.png`` URL template.
                      All presets are open and need no API key.
        overlays    : extra shapes drawn as outlines, e.g. ``[geo.buffer(...)]``
        attribution : attribution HTML (required for a custom tile URL)

        Returns
        -------
        self

        Examples
        --------
        >>> geo.load("cities.csv").map("cities.html", title="India Cities")
        """
        path = self._output_path(output_path, "geo.map('out.html')")
        page, located = self._map_page(title, zoom, center, tiles, overlays, attribution, path)
        _write(path, page)
        _announce(f"✅  Map saved → {path}  ({_plural(located, 'feature')})"
                  + _view_hint(path, tiles), self.verbose)
        return self

    def map_html(
        self,
        title: str = "QOREgeo Map",
        zoom: int | None = None,
        center: PointLike | None = None,
        tiles: str = "light",
        overlays: Sequence[dict[str, Any]] | dict[str, Any] | None = None,
        attribution: str | None = None,
    ) -> str:
        """
        The interactive map as an HTML string (same options as ``map()``),
        e.g. to return from a web framework or embed in a report.
        """
        return self._map_page(title, zoom, center, tiles, overlays, attribution, None)[0]

    def _heatmap_page(
        self,
        title: str,
        intensity_col: str | None,
        zoom: int | None,
        center: PointLike | None,
        tiles: str,
        radius: int,
        blur: int,
        attribution: str | None,
        output_path: str | None,
    ) -> tuple[str, int]:
        self._require_data("heatmap")
        zoom = self._check_zoom(zoom)
        view_center = _coerce_point(center) if center is not None else None
        _positive_int("radius", radius, 1, "geo.heatmap('heat.html', radius=25)")
        _positive_int("blur", blur, 0, "geo.heatmap('heat.html', blur=15)")
        if intensity_col is not None:
            keys = set()
            for feat in self._features():
                keys.update(feat.get("properties", {}))
            if self._features() and intensity_col not in keys:
                raise ColumnNotFoundError(
                    intensity_col, sorted(keys), self._source_path or "dataset",
                    hint="geo.heatmap('heat.html', intensity_col='<numeric column>')",
                )
        # Warnings about rows without heat point at the user's call
        # (heatmap_page → _heatmap_page → heatmap()/heatmap_html() → caller).
        return heatmap_page(
            self._features(), title, intensity_col, zoom, view_center, tiles, radius, blur,
            attribution, output_path, stacklevel=4,
        )

    def heatmap(
        self,
        output_path: PathLike = "heatmap.html",
        title: str = "QOREgeo Heatmap",
        intensity_col: str | None = None,
        zoom: int | None = None,
        center: PointLike | None = None,
        tiles: str = "light",
        radius: int = 25,
        blur: int = 18,
        attribution: str | None = None,
    ) -> GeoEngine:
        """
        Export a density heatmap to an HTML file.

        Parameters
        ----------
        output_path   : where to write the HTML file (never the loaded data file)
        title         : page title
        intensity_col : numeric property used as heat weight (normalised to
                        the largest value); rows with missing, negative or
                        non-numeric values get no weight
        zoom          : see map()
        center        : (lat, lng) initial center; fits all points if None
        tiles         : basemap (see map())
        radius, blur  : heat point radius and blur in pixels
        attribution   : attribution HTML (required for a custom tile URL)

        Returns
        -------
        self

        Examples
        --------
        >>> geo.load("stores.csv").heatmap("heat.html", intensity_col="sales")
        """
        path = self._output_path(output_path, "geo.heatmap('heat.html')")
        page, n = self._heatmap_page(title, intensity_col, zoom, center, tiles, radius, blur,
                                     attribution, path)
        _write(path, page)
        _announce(f"✅  Heatmap saved → {path}  ({_plural(n, 'point')})"
                  + _view_hint(path, tiles), self.verbose)
        return self

    def heatmap_html(
        self,
        title: str = "QOREgeo Heatmap",
        intensity_col: str | None = None,
        zoom: int | None = None,
        center: PointLike | None = None,
        tiles: str = "light",
        radius: int = 25,
        blur: int = 18,
        attribution: str | None = None,
    ) -> str:
        """The heatmap as an HTML string (same options as ``heatmap()``)."""
        return self._heatmap_page(title, intensity_col, zoom, center, tiles, radius, blur,
                                  attribution, None)[0]

    def _render_options(
        self,
        title: str | None,
        width: int,
        height: int,
        overlays: Any,
        background: str,
        labels: str | None,
        grid: bool,
        scale_bar: bool,
    ) -> dict[str, Any]:
        return {"title": title, "width": width, "height": height,
                "overlays": self._overlay_list(overlays), "background": background,
                "labels": labels, "grid": grid, "scale_bar": scale_bar}

    def to_png(
        self,
        title: str | None = None,
        width: int = 1200,
        height: int = 800,
        overlays: Sequence[dict[str, Any]] | dict[str, Any] | None = None,
        background: str = "#ffffff",
        labels: str | None = None,
        grid: bool = True,
        scale_bar: bool = True,
    ) -> bytes:
        """
        A static PNG map of the features (no basemap, no browser, no
        dependencies) for reports, e-mails, notebooks and AI vision models.

        ``labels`` names a property drawn next to each feature (e.g. "name");
        ``grid`` draws a lat/lng graticule and ``scale_bar`` a distance scale.
        """
        from .render import render_png

        self._require_data("to_png")
        return render_png(self._features(), **self._render_options(
            title, width, height, overlays, background, labels, grid, scale_bar))

    def to_svg(
        self,
        title: str | None = None,
        width: int = 1200,
        height: int = 800,
        overlays: Sequence[dict[str, Any]] | dict[str, Any] | None = None,
        background: str = "#ffffff",
        labels: str | None = None,
        grid: bool = True,
        scale_bar: bool = True,
    ) -> str:
        """A static SVG map of the features (same options as ``to_png()``)."""
        from .render import render_svg

        self._require_data("to_svg")
        return render_svg(self._features(), **self._render_options(
            title, width, height, overlays, background, labels, grid, scale_bar))

    def image(
        self,
        output_path: PathLike = "map.png",
        title: str | None = None,
        width: int = 1200,
        height: int = 800,
        overlays: Sequence[dict[str, Any]] | dict[str, Any] | None = None,
        background: str = "#ffffff",
        labels: str | None = None,
        grid: bool = True,
        scale_bar: bool = True,
    ) -> GeoEngine:
        """
        Save a static map image: ``.png`` or ``.svg``, chosen by file extension.

        Unlike ``map()`` it needs no browser or internet: points, lines and
        polygons are drawn on a plain background in the same colours, with a
        lat/lng grid, a scale bar and optional ``labels`` (a property name).

        Examples
        --------
        >>> geo.load("stores.csv").filter_by_radius(28.61, 77.21, 10).image("stores.png", labels="name")
        """
        path = self._output_path(output_path, "geo.image('map.png')")
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".png", ".svg"):
            raise InvalidArgumentError("output_path", path, "a file name ending in .png or .svg",
                                       "geo.image('map.png')")
        options = (title, width, height, overlays, background, labels, grid, scale_bar)
        if ext == ".png":
            _atomic_write(path, self.to_png(*options))
        else:
            _atomic_write(path, self.to_svg(*options))
        _announce(f"✅  Image saved → {path}  ({_plural(self.count(), 'feature')})", self.verbose)
        return self

    def show(
        self,
        heatmap: bool = False,
        port: int = 0,
        block: bool | None = None,
        open_browser: bool = True,
        **options: Any,
    ) -> str:
        """
        Open the map (or heatmap) in the browser via a local web server.

        The page is served from ``http://127.0.0.1``, never from disk,
        because OpenStreetMap's tile servers only serve web pages. Nothing is
        written to disk and only this machine can connect.

        Parameters
        ----------
        heatmap      : show ``heatmap_html(**options)`` instead of ``map_html(**options)``
        port         : TCP port (0 = pick a free one)
        block        : keep serving until Ctrl+C. Default: block in scripts,
                       serve in the background in interactive sessions/notebooks
        open_browser : open the default browser
        **options    : passed to map_html() / heatmap_html() (title, tiles, …)

        Returns
        -------
        The URL being served.

        Examples
        --------
        >>> geo.load("cities.csv").show(title="Cities", tiles="topo")
        """
        page = self.heatmap_html(**options) if heatmap else self.map_html(**options)
        name = "heatmap.html" if heatmap else "map.html"
        return serve_html(page, name, port=port, block=block, open_browser=open_browser,
                          verbose=self.verbose)

    # Above this many features a notebook shows the text repr instead of a map
    # (the whole dataset would be embedded in the notebook file).
    _NOTEBOOK_MAP_LIMIT = 20_000

    def _repr_html_(self) -> str | None:
        """Jupyter / IPython display: an interactive map of the features."""
        if self._data is None or self.count() > self._NOTEBOOK_MAP_LIMIT:
            return None
        try:   # display must never raise in a notebook
            page = self.map_html(title=f"QOREgeo · {self.count()} features")
        except Exception:   # noqa: BLE001
            return None
        return (
            '<iframe srcdoc="' + html.escape(page, quote=True) + '" '
            'style="width:100%;height:460px;border:0;border-radius:8px" '
            'title="QOREgeo map" loading="lazy" allowfullscreen></iframe>'
        )
