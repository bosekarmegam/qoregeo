"""
qoregeo.engine
==============
``GeoEngine``, the single entry point for every spatial operation.

The design goal is that spatial work should read like data work. If you know
how to chain Pandas operations, you already know how to use this::

    (GeoEngine()
        .load("stores.csv")
        .query("revenue > 500000 and state == 'Maharashtra'")
        .filter_by_radius(19.07, 72.87, radius=25)
        .sort_by("revenue", reverse=True)
        .head(20)
        .map("top_stores.html", colour_by="segment"))

Every operation that narrows or transforms data returns a **new** engine, so
the original stays untouched and pipelines never surprise you. Operations that
only write a file (``map``, ``save``, ``png``) return ``self`` so the chain
continues.
"""

from __future__ import annotations

import csv
import json
import os
import random as _random
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from . import analysis as _analysis
from . import formats as _formats
from . import geometry as _geom
from .exceptions import (
    ColumnNotFoundError,
    EmptyDatasetError,
    InvalidBufferError,
    InvalidRadiusError,
    InvalidUnitError,
    NoDataError,
    UnsupportedFormatError,
)
from .exceptions import FileNotFoundError as QFileNotFoundError
from .index import AUTO_INDEX_THRESHOLD, SpatialIndex
from .map_builder import build_choropleth, build_heatmap, build_map
from .query import compile_query
from .static_map import build_png, build_svg
from .utils import (
    _bearing_to_compass,
    _detect_lat_lng_columns,
    _haversine_km,
    _safe_float,
    _validate_coord,
)

# ─────────────────────────────────────────────────────────────────────────────
# Type aliases
# ─────────────────────────────────────────────────────────────────────────────
Coord = Tuple[float, float]        # (lat, lng)
Feature = Dict[str, Any]           # GeoJSON Feature dict

#: Distance units accepted by ``distance()`` and friends.
DISTANCE_UNITS = {"km": 1.0, "miles": 0.621371, "mi": 0.621371, "m": 1000.0, "ft": 3280.84,
                  "nm": 0.539957, "nmi": 0.539957}

#: Units accepted where a radius is converted *into* km.
RADIUS_UNITS = {"km": 1.0, "miles": 1.60934, "mi": 1.60934, "m": 0.001, "ft": 0.0003048,
                "nm": 1.852, "nmi": 1.852}


class GeoEngine:
    """
    Spatial data engine, load, analyse, visualise.

    Parameters
    ----------
    None. Everything is configured through method calls.

    Attributes
    ----------
    VERSION : the QOREgeo release this engine ships with
    """

    VERSION = "1.1.0"

    #: Datasets at least this large get a spatial index built on first query.
    AUTO_INDEX_THRESHOLD = AUTO_INDEX_THRESHOLD

    def __init__(self) -> None:
        self._data: Optional[Dict[str, Any]] = None      # GeoJSON FeatureCollection
        self._source_path: Optional[str] = None
        self._index: Optional[SpatialIndex] = None

    # ──────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────

    def _require_data(self, method_name: str) -> None:
        """Raise NoDataError with a helpful message if no data is loaded."""
        if self._data is None or not self._data.get("features"):
            raise NoDataError(method_name)

    def _features(self) -> List[Feature]:
        return self._data["features"] if self._data else []

    def _clone_with(self, features: List[Feature]) -> GeoEngine:
        """Return a new GeoEngine containing only the given features."""
        new = GeoEngine()
        new._data = {"type": "FeatureCollection", "features": features}
        new._source_path = self._source_path
        return new

    def _set_features(self, features: List[Feature]) -> None:
        self._data = {"type": "FeatureCollection", "features": features}
        self._index = None                    # positions changed; index is stale

    def _known_columns(self) -> List[str]:
        """Every property key present on at least one feature, in first-seen order."""
        keys: List[str] = []
        seen = set()
        for feat in self._features():
            for key in (feat.get("properties") or {}):
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        return keys

    def _require_column(self, column: str, method_name: str = "filter") -> None:
        columns = self._known_columns()
        if column not in columns:
            raise ColumnNotFoundError(
                column, columns, self._source_path or "dataset"
            )

    @staticmethod
    def _point_of(feature: Feature) -> Optional[Coord]:
        """``(lat, lng)`` of a feature, the centroid for non-point geometries."""
        geom = feature.get("geometry") or {}
        if geom.get("type") == "Point":
            coords = geom.get("coordinates") or []
            if len(coords) >= 2:
                return (float(coords[1]), float(coords[0]))
            return None
        try:
            return _geom.centroid_of(geom)
        except Exception:
            return None

    # ──────────────────────────────────────────────────────────────────────
    # I/O
    # ──────────────────────────────────────────────────────────────────────

    def load(
        self,
        path: str,
        lat_col: Optional[str] = None,
        lng_col: Optional[str] = None,
        encoding: str = "utf-8-sig",
    ) -> GeoEngine:
        """
        Load spatial data from a file.

        Supported: ``.csv``, ``.geojson``, ``.json``, ``.ndjson`` /
        ``.geojsonl``, ``.wkt``, ``.gpx``, ``.kml`` and Esri ``.shp``.
        Latitude/longitude columns in CSVs are auto-detected.

        Parameters
        ----------
        path     : path to the data file
        lat_col  : override the latitude column name (CSV only)
        lng_col  : override the longitude column name (CSV only)
        encoding : file encoding; the default handles a UTF-8 BOM

        Returns
        -------
        self, for method chaining
        """
        if not os.path.exists(path):
            raise QFileNotFoundError(path)

        ext = os.path.splitext(path)[1].lower()

        if ext == ".csv":
            self._data = self._load_csv(path, lat_col, lng_col, encoding)
        elif ext in (".geojson", ".json"):
            self._data = self._load_geojson(path, encoding)
        elif ext in _formats.READABLE:
            features = _formats.read_any(path, encoding=encoding)
            if not features:
                raise EmptyDatasetError(path)
            self._data = {"type": "FeatureCollection", "features": features}
        else:
            raise UnsupportedFormatError(ext)

        self._source_path = path
        self._index = None
        return self

    def _load_csv(
        self,
        path: str,
        lat_col: Optional[str],
        lng_col: Optional[str],
        encoding: str,
    ) -> Dict[str, Any]:
        features: List[Feature] = []

        with open(path, newline="", encoding=encoding) as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise EmptyDatasetError(path)

            # Auto-detect columns
            if lat_col and lng_col:
                detected_lat, detected_lng = lat_col, lng_col
            else:
                detected_lat, detected_lng = _detect_lat_lng_columns(
                    list(reader.fieldnames), path
                )
            lat_c = lat_col or detected_lat
            lng_c = lng_col or detected_lng

            for i, row in enumerate(reader, start=2):
                raw_lat = row.get(lat_c)
                raw_lng = row.get(lng_c)

                lat = _safe_float(raw_lat, lat_c, i, path)
                lng = _safe_float(raw_lng, lng_c, i, path)

                if lat is None or lng is None:
                    continue  # skip rows with missing coords

                _validate_coord(lat, lng)

                props = {k: v for k, v in row.items() if k not in (lat_c, lng_c)}

                features.append(
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [lng, lat],
                        },
                        "properties": props,
                    }
                )

        if not features:
            raise EmptyDatasetError(path)

        return {"type": "FeatureCollection", "features": features}

    def _load_geojson(self, path: str, encoding: str) -> Dict[str, Any]:
        with open(path, encoding=encoding) as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as e:
                raise UnsupportedFormatError(
                    f".geojson, invalid JSON: {e}"
                ) from e

        if data.get("type") == "FeatureCollection":
            return data
        if data.get("type") == "Feature":
            return {"type": "FeatureCollection", "features": [data]}

        raise UnsupportedFormatError(
            ".geojson, expected a FeatureCollection or Feature"
        )

    def load_data(self, features: Sequence[Any]) -> GeoEngine:
        """
        Load features already in memory, no file needed.

        Deliberately permissive about shape: GeoJSON Features, bare geometries,
        ``(lat, lng)`` tuples and plain dicts with lat/lng keys all work,
        because that is the range of things people actually have in a variable.

        Parameters
        ----------
        features : the records to load, see above for accepted shapes

        Returns
        -------
        self
        """
        if not features:
            raise EmptyDatasetError("<in-memory data>")
        self._data = {
            "type": "FeatureCollection",
            "features": _formats.iter_features(features),
        }
        self._index = None
        return self

    @classmethod
    def from_records(cls, records: Sequence[Dict[str, Any]]) -> GeoEngine:
        """
        Build an engine from plain dicts with latitude/longitude keys.

        >>> geo = GeoEngine.from_records([{"lat": 28.6, "lng": 77.2, "name": "Delhi"}])
        """
        return cls().load_data(list(records))

    @classmethod
    def from_points(cls, points: Sequence[Coord]) -> GeoEngine:
        """Build an engine from a list of ``(lat, lng)`` tuples."""
        return cls().load_data([(float(p[0]), float(p[1])) for p in points])

    def save(self, path: str, encoding: str = "utf-8") -> GeoEngine:
        """
        Save the current features to a file.

        The extension chooses the format: ``.geojson``, ``.json``, ``.csv``,
        ``.ndjson``, ``.wkt``, ``.gpx``, ``.kml``, ``.svg``, ``.png`` or
        ``.html``.

        Returns
        -------
        self
        """
        self._require_data("save")
        ext = os.path.splitext(path)[1].lower()

        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        if ext in (".geojson", ".json"):
            with open(path, "w", encoding=encoding) as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)

        elif ext == ".csv":
            self._save_csv(path, encoding)

        elif ext == ".html":
            build_map(self._features(), path, quiet=True)
        elif ext == ".svg":
            build_svg(self._features(), path)
        elif ext == ".png":
            build_png(self._features(), path)
        elif ext in _formats.WRITABLE:
            _formats.write_any(self._features(), path, encoding=encoding)
        else:
            raise UnsupportedFormatError(ext)

        return self

    def _save_csv(self, path: str, encoding: str) -> None:
        features = self._features()
        if not features:
            raise EmptyDatasetError(path)

        # Gather all property keys, preserving first-seen order.
        all_keys = self._known_columns()
        fieldnames = ["latitude", "longitude"] + all_keys

        with open(path, "w", newline="", encoding=encoding) as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for feat in features:
                point = self._point_of(feat)
                row: Dict[str, Any] = {
                    "latitude": point[0] if point else "",
                    "longitude": point[1] if point else "",
                }
                row.update(feat.get("properties", {}))
                writer.writerow(row)

    def to_geojson_string(self, indent: Optional[int] = 2) -> str:
        """The current FeatureCollection as a JSON string."""
        self._require_data("to_geojson_string")
        return json.dumps(self._data, indent=indent, ensure_ascii=False)

    def to_records(self) -> List[Dict[str, Any]]:
        """
        Flatten features into plain dicts with ``latitude``/``longitude`` keys.

        The shape every other data tool expects. Hand it straight to
        ``pandas.DataFrame`` or ``csv.DictWriter``.
        """
        self._require_data("to_records")
        rows = []
        for feat in self._features():
            point = self._point_of(feat)
            row: Dict[str, Any] = {
                "latitude": point[0] if point else None,
                "longitude": point[1] if point else None,
            }
            row.update(feat.get("properties", {}))
            rows.append(row)
        return rows

    def to_wkt(self) -> List[str]:
        """Every geometry as a WKT string."""
        self._require_data("to_wkt")
        return [_formats.to_wkt(f.get("geometry") or {}) for f in self._features()]

    # ──────────────────────────────────────────────────────────────────────
    # Geometry operations
    # ──────────────────────────────────────────────────────────────────────

    def distance(
        self,
        point_a: Coord,
        point_b: Coord,
        unit: str = "km",
        method: str = "haversine",
    ) -> float:
        """
        Distance between two coordinates.

        Parameters
        ----------
        point_a : (lat, lng) tuple
        point_b : (lat, lng) tuple
        unit    : ``'km'`` | ``'miles'`` | ``'m'`` | ``'ft'`` | ``'nm'``
        method  : ``'haversine'`` (spherical, fast) or ``'vincenty'``
                  (WGS84 ellipsoid, ~0.5 % more accurate over long distances)

        Returns
        -------
        Distance as a float in the requested unit.

        Examples
        --------
        >>> geo.distance((28.61, 77.20), (19.07, 72.87))
        1153.54
        >>> geo.distance((28.61, 77.20), (19.07, 72.87), unit='miles')
        716.84
        """
        _validate_coord(*point_a)
        _validate_coord(*point_b)

        unit = str(unit).lower()
        if unit not in DISTANCE_UNITS:
            raise InvalidUnitError(unit, list(DISTANCE_UNITS.keys()))

        if str(method).lower() in ("vincenty", "geodesic", "ellipsoid"):
            km = _geom.vincenty_km(point_a, point_b)
        else:
            km = _haversine_km(point_a, point_b)

        return round(km * DISTANCE_UNITS[unit], 4)

    def bearing(
        self,
        point_a: Coord,
        point_b: Coord,
        as_degrees: bool = False,
    ) -> Union[str, float]:
        """
        Compass bearing from ``point_a`` to ``point_b``.

        Parameters
        ----------
        point_a    : (lat, lng) origin
        point_b    : (lat, lng) destination
        as_degrees : return numeric degrees instead of a compass string

        Returns
        -------
        A 16-point compass direction such as ``'South-Southwest'``, or degrees.

        Examples
        --------
        >>> geo.bearing((28.61, 77.20), (19.07, 72.87))
        'South-Southwest'
        """
        _validate_coord(*point_a)
        _validate_coord(*point_b)
        deg = round(_geom.bearing_degrees(point_a, point_b), 2)
        return deg if as_degrees else _bearing_to_compass(deg)

    def destination(self, origin: Coord, bearing: float, distance: float,
                    unit: str = "km") -> Coord:
        """
        Where you end up travelling ``distance`` from ``origin`` on a bearing.

        The inverse of :meth:`bearing`, useful for projecting a search cone,
        an evacuation radius edge, or the next waypoint on a heading.
        """
        return _geom.destination(origin, bearing, self._to_km(distance, unit))

    def midpoint(self, point_a: Coord, point_b: Coord) -> Coord:
        """Great-circle midpoint between two coordinates."""
        return _geom.midpoint(point_a, point_b)

    def interpolate(self, point_a: Coord, point_b: Coord, fraction: float) -> Coord:
        """Point at ``fraction`` (0-1) along the great-circle path from A to B."""
        return _geom.interpolate(point_a, point_b, fraction)

    @staticmethod
    def _to_km(value: float, unit: str) -> float:
        key = str(unit).lower()
        if key not in RADIUS_UNITS:
            raise InvalidUnitError(unit, list(RADIUS_UNITS.keys()))
        return float(value) * RADIUS_UNITS[key]

    def buffer(
        self,
        center: Coord,
        radius: float,
        unit: str = "km",
        num_points: int = 64,
    ) -> Dict[str, Any]:
        """
        Circular geofence around a point, as a GeoJSON Polygon.

        Parameters
        ----------
        center     : (lat, lng) centre of the circle
        radius     : radius of the circle
        unit       : ``'km'`` | ``'miles'`` | ``'m'`` | ``'ft'`` | ``'nm'``
        num_points : polygon resolution. Higher is smoother

        Returns
        -------
        A GeoJSON Polygon, with ``_center`` and ``_radius_km`` attached for
        reference.

        Examples
        --------
        >>> zone = geo.buffer((28.61, 77.20), radius=10)
        >>> geo.point_in_polygon((28.65, 77.22), zone)
        True
        """
        _validate_coord(*center)
        if radius <= 0:
            raise InvalidRadiusError(radius)

        radius_km = self._to_km(radius, unit)
        coords = _geom.circle_ring(center, radius_km, num_points)

        return {
            "type": "Polygon",
            "coordinates": [coords],
            "_center": center,
            "_radius_km": radius_km,
        }

    def buffer_line(
        self,
        line: Sequence[Coord],
        radius: float,
        unit: str = "km",
    ) -> Dict[str, Any]:
        """
        Corridor of a given width around a polyline.

        This is the "everything within 500 m of this road / river / pipeline"
        zone, the line equivalent of :meth:`buffer`.

        Parameters
        ----------
        line   : ``(lat, lng)`` points describing the path
        radius : half-width of the corridor
        unit   : radius unit

        Returns
        -------
        A GeoJSON MultiPolygon covering the corridor.
        """
        path: List[Coord] = [(float(p[0]), float(p[1])) for p in line]
        return _geom.line_buffer(path, self._to_km(radius, unit))

    def buffer_geometry(
        self,
        geometry: Dict[str, Any],
        radius: float,
        unit: str = "km",
    ) -> Dict[str, Any]:
        """Buffer any geometry, point, line or polygon, by a distance."""
        return _geom.geometry_buffer(geometry, self._to_km(radius, unit))

    def point_in_polygon(
        self,
        point: Coord,
        polygon: Dict[str, Any],
    ) -> bool:
        """
        Test whether a point falls inside a polygon.

        Handles Polygons with holes and MultiPolygons: a point sitting in a
        hole reads as outside, which is what a hole means.

        Parameters
        ----------
        point   : (lat, lng)
        polygon : a GeoJSON Polygon/MultiPolygon, Feature, or ``buffer()`` result

        Returns
        -------
        True if inside, False otherwise.

        Examples
        --------
        >>> zone = geo.buffer((28.61, 77.20), 10)
        >>> geo.point_in_polygon((28.65, 77.22), zone)
        True
        """
        _validate_coord(*point)
        if not isinstance(polygon, dict):
            raise InvalidBufferError()
        return _geom.point_in_geometry(point, polygon)

    def intersects(self, geom_a: Dict[str, Any], geom_b: Dict[str, Any]) -> bool:
        """
        True if two geometries overlap or touch.

        Works for polygon/polygon, polygon/line and line/line, the test
        behind "does this delivery zone clash with that one?".
        """
        return _geom.intersects(geom_a, geom_b)

    def contains(self, outer: Dict[str, Any], inner: Dict[str, Any]) -> bool:
        """True if ``outer`` fully contains ``inner``."""
        return _geom.contains(outer, inner)

    def area(self, geometry: Optional[Dict[str, Any]] = None, unit: str = "km2") -> float:
        """
        Area of a polygon, using spherical geometry.

        With no argument, sums the area of every loaded feature, the "how
        much land do these districts cover?" answer.

        Parameters
        ----------
        unit : ``km2`` | ``m2`` | ``ha`` | ``acres`` | ``mi2``
        """
        if geometry is not None:
            if geometry.get("type") == "Feature":
                geometry = geometry.get("geometry") or {}
            return round(_geom.convert_area(_geom.geometry_area_km2(geometry), unit), 6)

        self._require_data("area")
        total = sum(
            _geom.geometry_area_km2(f.get("geometry") or {}) for f in self._features()
        )
        return round(_geom.convert_area(total, unit), 6)

    def length(self, geometry: Optional[Dict[str, Any]] = None, unit: str = "km") -> float:
        """
        Length of a line, or the perimeter of a polygon.

        With no argument, sums across every loaded feature.
        """
        if geometry is not None:
            if geometry.get("type") == "Feature":
                geometry = geometry.get("geometry") or {}
            return round(_geom.convert_length(_geom.geometry_length_km(geometry), unit), 6)

        self._require_data("length")
        total = sum(
            _geom.geometry_length_km(f.get("geometry") or {}) for f in self._features()
        )
        return round(_geom.convert_length(total, unit), 6)

    def centroid(self, geometry: Optional[Dict[str, Any]] = None) -> Coord:
        """
        Centroid as ``(lat, lng)``.

        With no argument, the mean centre of every loaded feature, computed
        on the sphere, so it stays correct across the antimeridian.
        """
        if geometry is not None:
            if geometry.get("type") == "Feature":
                geometry = geometry.get("geometry") or {}
            return _geom.centroid_of(geometry)

        self._require_data("centroid")
        return _analysis.spherical_mean(
            [p for p in (self._point_of(f) for f in self._features()) if p]
        )

    def centre_of_mass(self, weight_col: Optional[str] = None) -> Coord:
        """
        Weighted centre of the dataset. The "where should the hub go?" point.

        ``weight_col`` weights each feature by a numeric property (population,
        revenue, order volume).
        """
        self._require_data("centre_of_mass")
        if weight_col:
            self._require_column(weight_col, "centre_of_mass")
        return _analysis.centre_of_mass(self._features(), weight_col)

    #: US spelling alias.
    center_of_mass = centre_of_mass

    def convex_hull(self) -> Dict[str, Any]:
        """
        Smallest convex polygon containing every loaded feature.

        The standard way to draw a service area, a territory, or a search
        boundary around a scatter of points.
        """
        self._require_data("convex_hull")
        points = [
            c
            for f in self._features()
            for c in _geom.coords_of(f.get("geometry") or {})
        ]
        return {"type": "Polygon", "coordinates": [_geom.convex_hull(points)]}

    def simplify(self, tolerance: float = 0.001) -> GeoEngine:
        """
        Reduce vertex counts with Douglas-Peucker, keeping shape.

        Boundary files are routinely 100× larger than any map needs.
        ``tolerance`` is in degrees: 0.001 ≈ 100 m.

        Returns
        -------
        A new engine with simplified geometries.
        """
        self._require_data("simplify")
        out: List[Feature] = []

        for feat in self._features():
            geom = dict(feat.get("geometry") or {})
            gtype = geom.get("type")
            coords = geom.get("coordinates") or []

            if gtype == "LineString":
                geom["coordinates"] = _geom.simplify(coords, tolerance)
            elif gtype == "MultiLineString":
                geom["coordinates"] = [_geom.simplify(c, tolerance) for c in coords]
            elif gtype == "Polygon":
                geom["coordinates"] = [
                    _geom.simplify(r, tolerance, closed=True) for r in coords
                ]
            elif gtype == "MultiPolygon":
                geom["coordinates"] = [
                    [_geom.simplify(r, tolerance, closed=True) for r in poly]
                    for poly in coords
                ]

            out.append({**feat, "geometry": geom})

        return self._clone_with(out)

    def nearest(
        self,
        point: Coord,
        unit: str = "km",
    ) -> Dict[str, Any]:
        """
        Find the feature closest to a point.

        Parameters
        ----------
        point : (lat, lng)
        unit  : distance unit for the result

        Returns
        -------
        ``{"feature": ..., "distance": float, "index": int}``

        Examples
        --------
        >>> result = geo.nearest((28.61, 77.20))
        >>> result['feature']['properties']['name']
        'AIIMS New Delhi'
        """
        self._require_data("nearest")
        _validate_coord(*point)

        matches = self.knn(point, k=1, unit=unit)
        if not matches:
            return {"feature": None, "distance": float("inf"), "index": -1}
        return matches[0]

    def knn(
        self,
        point: Coord,
        k: int = 5,
        unit: str = "km",
    ) -> List[Dict[str, Any]]:
        """
        The ``k`` nearest features to a point, closest first.

        Backed by the spatial index on larger datasets, so this stays fast as
        the data grows rather than rescanning everything per query.

        Returns
        -------
        A list of ``{"feature", "distance", "index"}`` dicts.
        """
        self._require_data("knn")
        _validate_coord(*point)

        unit = str(unit).lower()
        if unit not in DISTANCE_UNITS:
            raise InvalidUnitError(unit, list(DISTANCE_UNITS.keys()))
        factor = DISTANCE_UNITS[unit]

        features = self._features()
        index = self._ensure_index()

        if index is not None:
            found = index.nearest(point, k)
        else:
            found = sorted(
                (
                    (_haversine_km(point, p), i)
                    for i, p in enumerate(self._point_of(f) for f in features)
                    if p is not None
                ),
                key=lambda r: (r[0], r[1]),
            )[: max(1, int(k))]

        return [
            {
                "feature": features[i],
                "distance": round(km * factor, 4),
                "index": i,
            }
            for km, i in found
        ]

    # ──────────────────────────────────────────────────────────────────────
    # Spatial index
    # ──────────────────────────────────────────────────────────────────────

    def build_index(self, cell_size_km: Optional[float] = None) -> GeoEngine:
        """
        Build a spatial index over the current features.

        Radius and nearest-neighbour queries go from scanning every feature to
        checking only the grid cells they touch. Worth it from a few thousand
        features up; built automatically above
        :data:`AUTO_INDEX_THRESHOLD`.

        Returns
        -------
        self
        """
        self._require_data("build_index")
        self._index = SpatialIndex(self._features(), cell_size_km=cell_size_km)
        return self

    def _ensure_index(self) -> Optional[SpatialIndex]:
        """Return the index, building it automatically for large datasets."""
        if self._index is not None and self._index.size == len(self._features()):
            return self._index
        if len(self._features()) >= self.AUTO_INDEX_THRESHOLD:
            self._index = SpatialIndex(self._features())
            return self._index
        return None

    def index_stats(self) -> Optional[Dict[str, Any]]:
        """Index health, or ``None`` when no index is built."""
        return self._index.stats() if self._index else None

    # ──────────────────────────────────────────────────────────────────────
    # Filtering and querying
    # ──────────────────────────────────────────────────────────────────────

    def filter(self, column: str, value: Any, op: str = "==") -> GeoEngine:
        """
        Filter features by a property value.

        String comparison is case-insensitive; numeric strings compare as
        numbers, so ``op=">"`` works on a CSV column that loaded as text.

        Parameters
        ----------
        column : property key
        value  : value to match
        op     : ``==`` ``!=`` ``>`` ``>=`` ``<`` ``<=`` ``contains``
                 ``startswith`` ``endswith``

        Returns
        -------
        A new GeoEngine with the matching features.

        Examples
        --------
        >>> geo.filter("state", "Maharashtra").count()
        134
        >>> geo.filter("population", 1000000, op=">").count()
        12
        """
        self._require_data("filter")
        self._require_column(column, "filter")

        from .query import _compare, _text_op

        operator = str(op).strip().lower()
        if operator in ("contains", "startswith", "endswith"):
            predicate = _text_op(column, operator, value)
        elif operator in ("==", "!=", ">", ">=", "<", "<="):
            predicate = _compare(column, operator, value)
        else:
            raise InvalidUnitError(
                op, ["==", "!=", ">", ">=", "<", "<=", "contains", "startswith", "endswith"]
            )

        return self._clone_with(
            [f for f in self._features() if predicate(f.get("properties") or {})]
        )

    def query(self, expression: str) -> GeoEngine:
        """
        Filter with an expression.

        The expression language is small and safe, parsed and never ``eval``'d,
        so it is fine to accept one from a config file or a web request.

        Parameters
        ----------
        expression : e.g. ``"population > 1e6 and state != 'Delhi'"``

        Returns
        -------
        A new GeoEngine with the matching features.

        Examples
        --------
        >>> geo.query("population > 1000000")
        >>> geo.query("name contains 'pur' or state in ('Delhi', 'Goa')")
        >>> geo.query("closed_date is null")
        """
        self._require_data("query")
        predicate = compile_query(expression)
        return self._clone_with(
            [f for f in self._features() if predicate(f.get("properties") or {})]
        )

    #: ``where`` reads better in some pipelines; same behaviour as ``query``.
    where = query

    def filter_by(self, predicate: Callable[[Dict[str, Any]], bool]) -> GeoEngine:
        """Filter with your own function over each feature's properties."""
        self._require_data("filter_by")
        return self._clone_with(
            [f for f in self._features() if predicate(f.get("properties") or {})]
        )

    def filter_by_radius(
        self,
        lat: float,
        lng: float,
        radius: float,
        unit: str = "km",
    ) -> GeoEngine:
        """
        Keep features within a radius of a point.

        Results come back sorted nearest-first with a ``_distance`` property
        injected, so you can rank them immediately.

        Parameters
        ----------
        lat, lng : centre of the search
        radius   : search radius
        unit     : ``'km'`` | ``'miles'`` | ``'m'`` | ``'ft'`` | ``'nm'``

        Returns
        -------
        A new GeoEngine, nearest first.

        Examples
        --------
        >>> nearby = geo.filter_by_radius(28.61, 77.20, radius=50)
        >>> nearby.count()
        8
        """
        self._require_data("filter_by_radius")
        _validate_coord(lat, lng)

        if radius <= 0:
            raise InvalidRadiusError(radius)

        unit = str(unit).lower()
        if unit not in DISTANCE_UNITS:
            raise InvalidUnitError(unit, list(DISTANCE_UNITS.keys()))

        factor = DISTANCE_UNITS[unit]
        radius_km = radius / factor
        centre = (lat, lng)
        features = self._features()
        index = self._ensure_index()

        if index is not None:
            found = index.within(centre, radius_km)
        else:
            found = sorted(
                (
                    (_haversine_km(centre, p), i)
                    for i, p in enumerate(self._point_of(f) for f in features)
                    if p is not None and _haversine_km(centre, p) <= radius_km
                ),
                key=lambda r: (r[0], r[1]),
            )

        results = []
        for km, i in found:
            feat = features[i]
            results.append(
                {
                    **feat,
                    "properties": {
                        **(feat.get("properties") or {}),
                        "_distance": round(km * factor, 4),
                    },
                }
            )
        return self._clone_with(results)

    def within(self, polygon: Dict[str, Any]) -> GeoEngine:
        """
        Keep the features that fall inside a polygon.

        The geofence query: which of my points are in this district, this
        delivery zone, this catchment?
        """
        self._require_data("within")
        if not isinstance(polygon, dict):
            raise InvalidBufferError()

        kept = []
        for feat in self._features():
            point = self._point_of(feat)
            if point and _geom.point_in_geometry(point, polygon):
                kept.append(feat)
        return self._clone_with(kept)

    def outside(self, polygon: Dict[str, Any]) -> GeoEngine:
        """Keep the features that fall *outside* a polygon."""
        self._require_data("outside")
        if not isinstance(polygon, dict):
            raise InvalidBufferError()

        kept = []
        for feat in self._features():
            point = self._point_of(feat)
            if point and not _geom.point_in_geometry(point, polygon):
                kept.append(feat)
        return self._clone_with(kept)

    def filter_by_bbox(
        self,
        min_lat: float,
        min_lng: float,
        max_lat: float,
        max_lng: float,
    ) -> GeoEngine:
        """Keep features inside a bounding box, the cheapest spatial filter."""
        self._require_data("filter_by_bbox")
        kept = []
        for feat in self._features():
            point = self._point_of(feat)
            if point and min_lat <= point[0] <= max_lat and min_lng <= point[1] <= max_lng:
                kept.append(feat)
        return self._clone_with(kept)

    # ──────────────────────────────────────────────────────────────────────
    # Table-style operations
    # ──────────────────────────────────────────────────────────────────────

    def sort_by(self, column: str, reverse: bool = False) -> GeoEngine:
        """
        Sort features by a property.

        Numeric-looking values sort numerically; everything else sorts as
        lower-cased text. Missing values sort last either way.
        """
        self._require_data("sort_by")
        self._require_column(column, "sort_by")

        from .query import _as_number

        def key(feat: Feature) -> Tuple[int, float, str]:
            value = (feat.get("properties") or {}).get(column)
            if value is None or (isinstance(value, str) and not value.strip()):
                return (1, 0.0, "")               # blanks last, both directions
            number = _as_number(value)
            if number is not None:
                return (0, number, "")
            return (0, 0.0, str(value).strip().lower())

        ordered = sorted(self._features(), key=key, reverse=reverse)
        if reverse:
            # Keep blanks at the end even when the sort is inverted.
            present = [f for f in ordered if key(f)[0] == 0]
            missing = [f for f in ordered if key(f)[0] == 1]
            ordered = present + missing
        return self._clone_with(ordered)

    def head(self, n: int = 5) -> GeoEngine:
        """First ``n`` features."""
        self._require_data("head")
        return self._clone_with(self._features()[: max(0, int(n))])

    def tail(self, n: int = 5) -> GeoEngine:
        """Last ``n`` features."""
        self._require_data("tail")
        n = max(0, int(n))
        return self._clone_with(self._features()[-n:] if n else [])

    def sample(self, n: int = 10, seed: Optional[int] = None) -> GeoEngine:
        """
        A random subset, for eyeballing a large dataset quickly.

        Pass ``seed`` for a reproducible sample.
        """
        self._require_data("sample")
        features = self._features()
        n = min(max(0, int(n)), len(features))
        rng = _random.Random(seed)
        return self._clone_with(rng.sample(features, n))

    def select(self, columns: Sequence[str]) -> GeoEngine:
        """Keep only these properties, dropping the rest."""
        self._require_data("select")
        keep = set(columns)
        return self._clone_with(
            [
                {
                    **f,
                    "properties": {
                        k: v for k, v in (f.get("properties") or {}).items() if k in keep
                    },
                }
                for f in self._features()
            ]
        )

    def drop(self, columns: Sequence[str]) -> GeoEngine:
        """Remove these properties."""
        self._require_data("drop")
        remove = set(columns)
        return self._clone_with(
            [
                {
                    **f,
                    "properties": {
                        k: v
                        for k, v in (f.get("properties") or {}).items()
                        if k not in remove
                    },
                }
                for f in self._features()
            ]
        )

    def rename(self, mapping: Dict[str, str]) -> GeoEngine:
        """Rename properties using an ``{old: new}`` mapping."""
        self._require_data("rename")
        return self._clone_with(
            [
                {
                    **f,
                    "properties": {
                        mapping.get(k, k): v
                        for k, v in (f.get("properties") or {}).items()
                    },
                }
                for f in self._features()
            ]
        )

    def add_column(
        self,
        name: str,
        value: Union[Any, Callable[[Dict[str, Any]], Any]],
    ) -> GeoEngine:
        """
        Add or overwrite a property.

        ``value`` may be a constant, or a function receiving each feature's
        properties dict, a computed column.

        Examples
        --------
        >>> geo.add_column("region", "West")
        >>> geo.add_column("density", lambda p: float(p["pop"]) / float(p["area"]))
        """
        self._require_data("add_column")
        out = []
        for feat in self._features():
            props = dict(feat.get("properties") or {})
            props[name] = value(props) if callable(value) else value
            out.append({**feat, "properties": props})
        return self._clone_with(out)

    def apply(self, function: Callable[[Feature], Feature]) -> GeoEngine:
        """Transform every feature with your own function."""
        self._require_data("apply")
        return self._clone_with([function(dict(f)) for f in self._features()])

    def concat(self, other: GeoEngine) -> GeoEngine:
        """Combine two engines into one. Also available as ``geo_a + geo_b``."""
        return self._clone_with(list(self._features()) + list(other._features()))

    def copy(self) -> GeoEngine:
        """A deep-enough copy that editing one engine won't disturb the other."""
        return self._clone_with([json.loads(json.dumps(f)) for f in self._features()])

    # ──────────────────────────────────────────────────────────────────────
    # Inspection and statistics
    # ──────────────────────────────────────────────────────────────────────

    def count(self) -> int:
        """Number of loaded features."""
        return len(self._features())

    def columns(self) -> List[str]:
        """Every property name present in the dataset."""
        self._require_data("columns")
        return self._known_columns()

    def get_features(self) -> List[Feature]:
        """The raw list of GeoJSON Feature dicts."""
        self._require_data("get_features")
        return self._features()

    def get_geojson(self) -> Dict[str, Any]:
        """The full GeoJSON FeatureCollection dict."""
        self._require_data("get_geojson")
        return self._data  # type: ignore[return-value]

    def bounds(self) -> Dict[str, float]:
        """
        Bounding box of every loaded feature.

        Returns
        -------
        ``{"min_lat", "max_lat", "min_lng", "max_lng"}``
        """
        self._require_data("bounds")
        lats: List[float] = []
        lngs: List[float] = []
        for feat in self._features():
            for lng, lat in _geom.coords_of(feat.get("geometry") or {}):
                lats.append(lat)
                lngs.append(lng)
        if not lats:
            raise EmptyDatasetError(self._source_path or "<dataset>")
        return {
            "min_lat": min(lats),
            "max_lat": max(lats),
            "min_lng": min(lngs),
            "max_lng": max(lngs),
        }

    def bounds_polygon(self) -> Dict[str, Any]:
        """The bounding box as a GeoJSON Polygon, ready to draw."""
        box = self.bounds()
        return _geom.bbox_polygon(
            [box["min_lng"], box["min_lat"], box["max_lng"], box["max_lat"]]
        )

    def unique(self, column: str) -> List[Any]:
        """Distinct values of a property, in first-seen order."""
        self._require_data("unique")
        self._require_column(column, "unique")
        seen: List[Any] = []
        marker = set()
        for feat in self._features():
            value = (feat.get("properties") or {}).get(column)
            key = str(value)
            if key not in marker:
                marker.add(key)
                seen.append(value)
        return seen

    def value_counts(self, column: str) -> Dict[str, int]:
        """How many features carry each value of a property, most common first."""
        self._require_data("value_counts")
        self._require_column(column, "value_counts")
        counts: Dict[str, int] = {}
        for feat in self._features():
            key = str((feat.get("properties") or {}).get(column))
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def stats(self, column: str) -> Dict[str, float]:
        """
        Numeric summary of a property: count, min, max, mean, median, sum.

        Non-numeric values are skipped rather than raising, because real CSVs
        contain "N/A".
        """
        self._require_data("stats")
        self._require_column(column, "stats")

        from .query import _as_number

        values = [
            v
            for v in (
                _as_number((f.get("properties") or {}).get(column))
                for f in self._features()
            )
            if v is not None
        ]
        if not values:
            return {"count": 0}

        ordered = sorted(values)
        n = len(ordered)
        median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
        return {
            "count": n,
            "min": ordered[0],
            "max": ordered[-1],
            "mean": round(sum(ordered) / n, 6),
            "median": median,
            "sum": round(sum(ordered), 6),
        }

    def describe(self) -> Dict[str, Any]:
        """
        A one-call overview: size, geometry mix, extent, spread and columns.

        The first thing to run on an unfamiliar dataset.
        """
        self._require_data("describe")
        features = self._features()

        kinds: Dict[str, int] = {}
        for feat in features:
            kind = (feat.get("geometry") or {}).get("type", "None")
            kinds[kind] = kinds.get(kind, 0) + 1

        summary: Dict[str, Any] = {
            "features": len(features),
            "geometry_types": kinds,
            "columns": self._known_columns(),
            "bounds": self.bounds(),
            "source": self._source_path,
        }

        points = [p for p in (self._point_of(f) for f in features) if p]
        if len(points) >= 2:
            summary["dispersion"] = _analysis.dispersion(features)
        elif points:
            summary["centre"] = points[0]
        return summary

    def validate(self) -> Dict[str, Any]:
        """
        Check every feature and report what's wrong, without raising.

        Catches the four failures that actually happen: missing geometry,
        out-of-range coordinates, empty coordinate arrays, and the classic
        latitude/longitude swap.

        Returns
        -------
        ``{"valid", "invalid", "issues", "likely_swapped"}``, ``issues`` lists
        ``{"index", "problem"}`` entries.
        """
        self._require_data("validate")

        issues: List[Dict[str, Any]] = []
        swapped = 0
        valid = 0

        for i, feat in enumerate(self._features()):
            geom = feat.get("geometry") or {}
            gtype = geom.get("type")

            if not gtype:
                issues.append({"index": i, "problem": "feature has no geometry"})
                continue

            coords = _geom.coords_of(geom)
            if not coords:
                issues.append({"index": i, "problem": f"{gtype} has no coordinates"})
                continue

            bad = False
            for lng, lat in coords:
                if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
                    issues.append(
                        {"index": i, "problem": f"coordinate out of range: lat={lat}, lng={lng}"}
                    )
                    bad = True
                    break
                # lat/lng swapped is legal-looking but detectable: a latitude
                # beyond ±90 in the longitude slot is the giveaway.
                if abs(lng) <= 90.0 and abs(lat) > 90.0:
                    swapped += 1

            if not bad:
                valid += 1

        return {
            "valid": valid,
            "invalid": len(issues),
            "issues": issues[:100],
            "likely_swapped": swapped,
        }

    def clean(self, drop_invalid: bool = True) -> GeoEngine:
        """
        Drop features that cannot be used: no geometry, or no coordinates.

        Returns
        -------
        A new engine holding only usable features.
        """
        self._require_data("clean")
        kept = []
        for feat in self._features():
            geom = feat.get("geometry") or {}
            if not geom.get("type"):
                continue
            if not _geom.coords_of(geom):
                continue
            kept.append(feat)

        if not kept and drop_invalid:
            raise EmptyDatasetError(self._source_path or "<dataset>")
        return self._clone_with(kept)

    def dropna(self, columns: Optional[Sequence[str]] = None) -> GeoEngine:
        """Drop features where any of these properties is missing or blank."""
        self._require_data("dropna")
        keys = list(columns) if columns else self._known_columns()

        def complete(props: Dict[str, Any]) -> bool:
            for key in keys:
                value = props.get(key)
                if value is None or (isinstance(value, str) and not value.strip()):
                    return False
            return True

        return self._clone_with(
            [f for f in self._features() if complete(f.get("properties") or {})]
        )

    def dedupe(
        self,
        tolerance_km: float = 0.0,
        subset: Optional[Sequence[str]] = None,
    ) -> GeoEngine:
        """
        Remove duplicate features, keeping the first of each group.

        With ``tolerance_km`` above zero, any point that close to an already
        kept feature counts as a duplicate, which is how you collapse the
        same shop geocoded twice a few metres apart.

        Parameters
        ----------
        tolerance_km : distance below which two points are "the same place"
        subset       : also require these properties to match before merging

        Returns
        -------
        A new engine with duplicates removed.
        """
        self._require_data("dedupe")

        def signature(feat: Feature) -> Optional[Tuple[str, ...]]:
            if not subset:
                return None
            props = feat.get("properties") or {}
            return tuple(str(props.get(key)) for key in subset)

        kept: List[Feature] = []

        if tolerance_km <= 0:
            seen = set()
            for feat in self._features():
                point = self._point_of(feat)
                key = (
                    round(point[0], 9) if point else None,
                    round(point[1], 9) if point else None,
                    signature(feat),
                )
                if key not in seen:
                    seen.add(key)
                    kept.append(feat)
            return self._clone_with(kept)

        kept_points: List[Optional[Coord]] = []
        for feat in self._features():
            point = self._point_of(feat)
            mine = signature(feat)

            duplicate = any(
                existing is not None
                and point is not None
                and _haversine_km(point, existing) <= tolerance_km
                and signature(kept[i]) == mine
                for i, existing in enumerate(kept_points)
            )
            if not duplicate:
                kept.append(feat)
                kept_points.append(point)

        return self._clone_with(kept)

    def fix_coordinates(self) -> GeoEngine:
        """
        Repair swapped latitude/longitude values.

        Only swaps a point when the stored latitude is outside ±90 but the
        longitude is not, an unambiguous signal, since a real latitude never
        exceeds 90. Ambiguous cases are left alone rather than guessed at.
        """
        self._require_data("fix_coordinates")
        out = []
        for feat in self._features():
            geom = feat.get("geometry") or {}
            if geom.get("type") == "Point":
                coords = geom.get("coordinates") or []
                if len(coords) >= 2:
                    lng, lat = float(coords[0]), float(coords[1])
                    if abs(lat) > 90.0 and abs(lng) <= 90.0:
                        feat = {
                            **feat,
                            "geometry": {**geom, "coordinates": [lat, lng]},
                        }
            out.append(feat)
        return self._clone_with(out)

    # ──────────────────────────────────────────────────────────────────────
    # Analysis
    # ──────────────────────────────────────────────────────────────────────

    def cluster(
        self,
        eps_km: float = 1.0,
        min_samples: int = 3,
        column: str = "_cluster",
    ) -> GeoEngine:
        """
        Density-based clustering (DBSCAN), written into a property.

        Finds groups without being told how many to expect, and marks
        genuinely isolated features as ``-1`` instead of forcing them into a
        cluster.

        Parameters
        ----------
        eps_km      : how close counts as neighbouring
        min_samples : neighbours needed to form a dense core

        Returns
        -------
        A new engine with a ``_cluster`` property on every feature.
        """
        self._require_data("cluster")
        labels = _analysis.dbscan(self._features(), eps_km, min_samples)
        return self._clone_with(
            [
                {**f, "properties": {**(f.get("properties") or {}), column: label}}
                for f, label in zip(self._features(), labels)
            ]
        )

    def kmeans(
        self,
        k: int = 3,
        column: str = "_cluster",
        seed: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        k-means clustering into exactly ``k`` groups.

        Returns
        -------
        ``{"engine", "centroids", "inertia", "iterations"}``, the engine
        carries the cluster label on each feature.
        """
        self._require_data("kmeans")
        result = _analysis.kmeans(self._features(), k, seed=seed)
        labelled = self._clone_with(
            [
                {**f, "properties": {**(f.get("properties") or {}), column: label}}
                for f, label in zip(self._features(), result["labels"])
            ]
        )
        return {
            "engine": labelled,
            "centroids": result["centroids"],
            "inertia": result["inertia"],
            "iterations": result["iterations"],
        }

    def spatial_join(
        self,
        polygons: Union[GeoEngine, Sequence[Feature]],
        prefix: str = "",
        keep: Optional[Sequence[str]] = None,
        how: str = "left",
    ) -> GeoEngine:
        """
        Tag each point with the attributes of the polygon containing it.

        The classic GIS join: which district, ward, sales territory or
        catchment does each record belong to?

        Parameters
        ----------
        polygons : another engine, or a list of polygon features
        prefix   : prepended to the copied property names
        keep     : only copy these polygon properties
        how      : ``"left"`` keeps unmatched points, ``"inner"`` drops them
        """
        self._require_data("spatial_join")
        shapes = (
            polygons.get_features() if isinstance(polygons, GeoEngine) else list(polygons)
        )
        return self._clone_with(
            _analysis.spatial_join(self._features(), shapes, prefix, keep, how)
        )

    def hotspots(self, cell_km: float = 5.0, min_count: int = 2) -> List[Dict[str, Any]]:
        """
        Grid the data and return the busiest cells, densest first.

        Each entry carries a polygon, so the result feeds straight into
        :meth:`choropleth`.
        """
        self._require_data("hotspots")
        return _analysis.hotspots(self._features(), cell_km, min_count)

    def dispersion(self) -> Dict[str, float]:
        """How spread out the features are around their centre."""
        self._require_data("dispersion")
        return _analysis.dispersion(self._features())

    def pattern(self) -> Dict[str, Any]:
        """
        Is this pattern clustered, dispersed, or random?

        The Clark & Evans nearest-neighbour ratio, which tells you whether the
        clumping you think you see is real.
        """
        self._require_data("pattern")
        return _analysis.nearest_neighbour_ratio(self._features())

    def optimise_route(
        self,
        start: Optional[Coord] = None,
        round_trip: bool = False,
        unit: str = "km",
    ) -> Dict[str, Any]:
        """
        Order the loaded features into a short visiting route.

        Greedy nearest-neighbour followed by 2-opt improvement, the standard
        practical TSP heuristic. Distances are straight-line, so this plans
        the *order* of stops, not the roads between them.

        Returns
        -------
        The route dict from :func:`qoregeo.routing.optimise_route`, plus an
        ``"engine"`` key holding the features in visiting order.
        """
        self._require_data("optimise_route")
        from .routing import optimise_route as _optimise

        features = self._features()
        points = [p for p in (self._point_of(f) for f in features) if p]
        route = _optimise(points, start=start, round_trip=round_trip, unit=unit)
        route["engine"] = self._clone_with([features[i] for i in route["order"]])
        return route

    #: US spelling alias.
    optimize_route = optimise_route

    def geohash_column(self, precision: int = 7, column: str = "_geohash") -> GeoEngine:
        """
        Add a geohash property to every feature.

        Geohashes share prefixes when they are close together, so this turns
        any plain database into one that can answer "near me" with a
        ``LIKE 'ttnfu%'``.
        """
        self._require_data("geohash_column")
        from .geohash import encode

        out = []
        for feat in self._features():
            point = self._point_of(feat)
            props = dict(feat.get("properties") or {})
            props[column] = encode(point[0], point[1], precision) if point else None
            out.append({**feat, "properties": props})
        return self._clone_with(out)

    def project(self, crs: str = "3857") -> List[Dict[str, Any]]:
        """
        Project every feature's position into another coordinate system.

        Parameters
        ----------
        crs : ``"3857"`` for Web Mercator metres, or ``"utm"`` for UTM

        Returns
        -------
        A list of dicts with the projected coordinates, the geometries
        themselves stay in WGS84, which is what GeoJSON requires.
        """
        self._require_data("project")
        from .crs import to_utm, to_web_mercator

        out = []
        for feat in self._features():
            point = self._point_of(feat)
            if point is None:
                continue
            if str(crs).lower() in ("utm", "32600"):
                out.append(to_utm(point[0], point[1]))
            else:
                x, y = to_web_mercator(point[0], point[1])
                out.append({"x": round(x, 3), "y": round(y, 3), "epsg": 3857})
        return out

    def geocode(
        self,
        column: str,
        user_agent: str,
        country: Optional[str] = None,
        skip_failures: bool = True,
    ) -> GeoEngine:
        """
        Fill in coordinates by looking up an address property.

        Needs network access. The only method in QOREgeo that does, and is
        rate limited to one request per second by the public geocoding
        service's rules.

        Parameters
        ----------
        column     : property holding the address text
        user_agent : identify your app, with contact details
        country    : ISO country code to narrow the search
        """
        self._require_data("geocode")
        self._require_column(column, "geocode")

        from .geocode import Geocoder

        coder = Geocoder(user_agent=user_agent)
        out = []
        for feat in self._features():
            props = dict(feat.get("properties") or {})
            address = props.get(column)
            if not address:
                out.append(feat)
                continue
            try:
                found = coder.geocode(str(address), country=country)
            except Exception:
                if not skip_failures:
                    raise
                found = None

            if found:
                out.append(
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [found["lng"], found["lat"]],
                        },
                        "properties": {**props, "_geocoded": found["display_name"]},
                    }
                )
            else:
                out.append(feat)
        return self._clone_with(out)

    # ──────────────────────────────────────────────────────────────────────
    # Dunder protocol
    # ──────────────────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return self.count()

    def __iter__(self) -> Iterator[Feature]:
        return iter(self._features())

    def __getitem__(self, key: Union[int, slice]) -> Union[Feature, GeoEngine]:
        features = self._features()
        if isinstance(key, slice):
            return self._clone_with(features[key])
        return features[key]

    def __add__(self, other: GeoEngine) -> GeoEngine:
        return self.concat(other)

    def __bool__(self) -> bool:
        return self.count() > 0

    def __repr__(self) -> str:
        status = f"{self.count()} features" if self._data else "no data loaded"
        return f"<GeoEngine [{status}]>"

    def _repr_html_(self) -> str:
        """
        Rich preview in Jupyter, the first few rows as a table.

        Notebooks are where most spatial exploration starts, so showing the
        data beats showing ``<GeoEngine object at 0x…>``.
        """
        if not self._data or not self._features():
            return "<b>GeoEngine</b>, <i>no data loaded</i>"

        features = self._features()
        columns = self._known_columns()[:8]
        header = "".join(f"<th style='text-align:left;padding:4px 10px'>{c}</th>" for c in columns)

        rows = []
        for feat in features[:10]:
            point = self._point_of(feat)
            cells = "".join(
                f"<td style='padding:4px 10px'>{(feat.get('properties') or {}).get(c, '')}</td>"
                for c in columns
            )
            location = f"{point[0]:.4f}, {point[1]:.4f}" if point else "-"
            rows.append(
                f"<tr><td style='padding:4px 10px;color:#888'>{location}</td>{cells}</tr>"
            )

        more = (
            f"<div style='color:#888;padding:4px 10px'>… {len(features) - 10} more</div>"
            if len(features) > 10
            else ""
        )
        return (
            "<div style='font-family:system-ui,sans-serif;font-size:13px'>"
            f"<b style='color:#00A183'>GeoEngine</b> · {len(features)} features"
            "<table style='border-collapse:collapse;margin-top:6px'>"
            "<tr style='border-bottom:1px solid #ccc'>"
            "<th style='text-align:left;padding:4px 10px'>lat, lng</th>"
            f"{header}</tr>"
            + "".join(rows)
            + "</table>"
            + more
            + "</div>"
        )

    # ──────────────────────────────────────────────────────────────────────
    # Visualisation
    # ──────────────────────────────────────────────────────────────────────

    def map(
        self,
        output_path: str = "map.html",
        title: str = "QOREgeo Map",
        zoom: int = 5,
        center: Optional[Coord] = None,
        basemap: str = "dark",
        cluster: Optional[bool] = None,
        colour_by: Optional[str] = None,
        color_by: Optional[str] = None,
        tooltip_field: Optional[str] = None,
        popup_fields: Optional[Sequence[str]] = None,
        search: bool = True,
        quiet: bool = False,
    ) -> GeoEngine:
        """
        Write an interactive Leaflet map to a standalone HTML file.

        Points, lines and polygons all render. Opens in any browser with no
        server behind it.

        Parameters
        ----------
        basemap       : ``dark`` | ``light`` | ``streets`` | ``terrain`` | ``satellite``
        cluster       : group nearby markers; automatic above ~750 features
        colour_by     : property to colour features by, with a legend
        tooltip_field : property shown on hover
        popup_fields  : restrict the popup to these properties
        search        : include a live filter box

        Returns
        -------
        self

        Examples
        --------
        >>> geo.load("cities.csv").map("cities.html", title="India Cities")
        >>> geo.map("by_state.html", colour_by="state", basemap="light")
        """
        self._require_data("map")
        build_map(
            features=self._features(),
            output_path=output_path,
            title=title,
            zoom=zoom,
            center=center,
            basemap=basemap,
            cluster=cluster,
            colour_by=colour_by or color_by,
            tooltip_field=tooltip_field,
            popup_fields=popup_fields,
            search=search,
            quiet=quiet,
        )
        return self

    def heatmap(
        self,
        output_path: str = "heatmap.html",
        title: str = "QOREgeo Heatmap",
        intensity_col: Optional[str] = None,
        zoom: int = 5,
        center: Optional[Coord] = None,
        basemap: str = "dark",
        radius: int = 25,
        blur: int = 18,
        quiet: bool = False,
    ) -> GeoEngine:
        """
        Write a density heatmap to an HTML file.

        Parameters
        ----------
        intensity_col : property weighting each point (sales, population, …)
        radius, blur  : heat spread in pixels

        Returns
        -------
        self

        Examples
        --------
        >>> geo.load("stores.csv").heatmap("heat.html", intensity_col="sales")
        """
        self._require_data("heatmap")
        build_heatmap(
            features=self._features(),
            output_path=output_path,
            title=title,
            intensity_col=intensity_col,
            zoom=zoom,
            center=center,
            basemap=basemap,
            radius=radius,
            blur=blur,
            quiet=quiet,
        )
        return self

    def choropleth(
        self,
        output_path: str,
        value_col: str,
        title: str = "QOREgeo Choropleth",
        label_col: Optional[str] = None,
        bins: int = 5,
        zoom: int = 5,
        center: Optional[Coord] = None,
        basemap: str = "dark",
        quiet: bool = False,
    ) -> GeoEngine:
        """
        Write a choropleth, features shaded by a numeric property.

        Classes are quantiles, so each colour holds roughly the same number of
        features even when the data is heavily skewed.

        Returns
        -------
        self
        """
        self._require_data("choropleth")
        self._require_column(value_col, "choropleth")
        build_choropleth(
            features=self._features(),
            output_path=output_path,
            value_col=value_col,
            title=title,
            label_col=label_col,
            bins=bins,
            zoom=zoom,
            center=center,
            basemap=basemap,
            quiet=quiet,
        )
        return self

    def svg(
        self,
        output_path: str,
        title: str = "QOREgeo Map",
        width: int = 1200,
        height: int = 800,
        theme: str = "dark",
        label_field: Optional[str] = None,
    ) -> GeoEngine:
        """
        Render a static SVG map, no browser, no network, no dependencies.

        For reports, README images and CI artefacts, where an interactive HTML
        map is the wrong shape entirely.

        Returns
        -------
        self
        """
        self._require_data("svg")
        build_svg(
            self._features(),
            output_path,
            title=title,
            width=width,
            height=height,
            theme=theme,
            label_field=label_field,
        )
        return self

    def png(
        self,
        output_path: str,
        title: str = "QOREgeo Map",
        width: int = 1200,
        height: int = 800,
        theme: str = "dark",
    ) -> GeoEngine:
        """
        Render a static PNG map, encoded in pure Python.

        Returns
        -------
        self
        """
        self._require_data("png")
        build_png(
            self._features(),
            output_path,
            title=title,
            width=width,
            height=height,
            theme=theme,
        )
        return self
