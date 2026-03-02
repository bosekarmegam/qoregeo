"""
qoregeo.engine
==============
Core GeoEngine class — the single entry point for all spatial operations.

Usage:
    from qoregeo import GeoEngine

    geo = GeoEngine()
    geo.load("cities.csv")
    geo.distance((28.61, 77.20), (19.07, 72.87), unit="km")
"""

from __future__ import annotations

import csv
import json
import math
import os
from typing import Any, Dict, List, Optional, Tuple, Union

from .exceptions import (
    NoDataError,
    InvalidCoordinateError,
    InvalidUnitError,
    ColumnNotFoundError,
    FileNotFoundError as QFileNotFoundError,
    UnsupportedFormatError,
    EmptyDatasetError,
    InvalidRadiusError,
    InvalidBufferError,
)
from .utils import (
    _validate_coord,
    _to_radians,
    _bearing_to_compass,
    _haversine_km,
    _generate_circle_polygon,
    _point_in_polygon_ray,
    _detect_lat_lng_columns,
    _safe_float,
)
from .map_builder import build_map, build_heatmap

# ─────────────────────────────────────────────────────────────────────────────
# Type aliases
# ─────────────────────────────────────────────────────────────────────────────
Coord  = Tuple[float, float]   # (lat, lng)
Feature = Dict[str, Any]       # GeoJSON Feature dict


class GeoEngine:
    """
    Quantum-ready spatial data engine for Python.

    Every method returns ``self`` to allow method chaining::

        GeoEngine().load("data.csv").filter("city", "Mumbai").map("out.html")

    Parameters
    ----------
    None — all configuration is done via method calls.
    """

    VERSION = "1.0.0"

    def __init__(self) -> None:
        self._data: Optional[Dict[str, Any]] = None   # GeoJSON FeatureCollection
        self._source_path: Optional[str] = None

    # ──────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────

    def _require_data(self, method_name: str) -> None:
        """Raise NoDataError with a helpful message if no data is loaded."""
        if self._data is None or not self._data.get("features"):
            raise NoDataError(method_name)

    def _features(self) -> List[Feature]:
        return self._data["features"] if self._data else []

    def _clone_with(self, features: List[Feature]) -> "GeoEngine":
        """Return a new GeoEngine containing only the given features."""
        new = GeoEngine()
        new._data = {"type": "FeatureCollection", "features": features}
        new._source_path = self._source_path
        return new

    # ──────────────────────────────────────────────────────────────────────
    # I/O
    # ──────────────────────────────────────────────────────────────────────

    def load(
        self,
        path: str,
        lat_col: Optional[str] = None,
        lng_col: Optional[str] = None,
        encoding: str = "utf-8-sig",
    ) -> "GeoEngine":
        """
        Load spatial data from a CSV or GeoJSON file.

        Auto-detects latitude/longitude column names (latitude, lat, y, LAT …).
        Raises helpful errors for common mistakes.

        Parameters
        ----------
        path       : path to .csv or .geojson file
        lat_col    : override latitude column name
        lng_col    : override longitude column name
        encoding   : file encoding (default utf-8-sig handles BOM)

        Returns
        -------
        self — for method chaining
        """
        if not os.path.exists(path):
            raise QFileNotFoundError(path)

        ext = os.path.splitext(path)[1].lower()

        if ext == ".csv":
            self._data = self._load_csv(path, lat_col, lng_col, encoding)
        elif ext in (".geojson", ".json"):
            self._data = self._load_geojson(path, encoding)
        else:
            raise UnsupportedFormatError(ext)

        self._source_path = path
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
                    f".geojson — invalid JSON: {e}"
                ) from e

        if data.get("type") == "FeatureCollection":
            return data
        if data.get("type") == "Feature":
            return {"type": "FeatureCollection", "features": [data]}

        raise UnsupportedFormatError(
            ".geojson — expected a FeatureCollection or Feature"
        )

    def load_data(self, features: List[Feature]) -> "GeoEngine":
        """
        Load raw GeoJSON Feature dicts directly (no file needed).

        Parameters
        ----------
        features : list of GeoJSON Feature dicts

        Returns
        -------
        self
        """
        if not features:
            raise EmptyDatasetError("<in-memory data>")
        self._data = {"type": "FeatureCollection", "features": list(features)}
        return self

    def save(self, path: str, encoding: str = "utf-8") -> "GeoEngine":
        """
        Save current features to a GeoJSON or CSV file.

        Parameters
        ----------
        path     : output path (.geojson, .json, or .csv)
        encoding : file encoding

        Returns
        -------
        self
        """
        self._require_data("save")
        ext = os.path.splitext(path)[1].lower()

        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)

        if ext in (".geojson", ".json"):
            with open(path, "w", encoding=encoding) as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)

        elif ext == ".csv":
            features = self._features()
            if not features:
                raise EmptyDatasetError(path)

            # Gather all property keys
            all_keys = []
            seen = set()
            for feat in features:
                for k in feat.get("properties", {}).keys():
                    if k not in seen:
                        all_keys.append(k)
                        seen.add(k)

            fieldnames = ["latitude", "longitude"] + all_keys

            with open(path, "w", newline="", encoding=encoding) as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for feat in features:
                    coords = feat["geometry"]["coordinates"]
                    row = {
                        "longitude": coords[0],
                        "latitude": coords[1],
                    }
                    row.update(feat.get("properties", {}))
                    writer.writerow(row)
        else:
            raise UnsupportedFormatError(ext)

        return self

    # ──────────────────────────────────────────────────────────────────────
    # Geometry operations
    # ──────────────────────────────────────────────────────────────────────

    def distance(
        self,
        point_a: Coord,
        point_b: Coord,
        unit: str = "km",
    ) -> float:
        """
        Calculate the great-circle (Haversine) distance between two coordinates.

        Parameters
        ----------
        point_a : (lat, lng) tuple
        point_b : (lat, lng) tuple
        unit    : 'km' | 'miles' | 'm' | 'ft'

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

        unit = unit.lower()
        _UNITS = {"km": 1.0, "miles": 0.621371, "mi": 0.621371, "m": 1000.0, "ft": 3280.84}

        if unit not in _UNITS:
            raise InvalidUnitError(unit, list(_UNITS.keys()))

        km = _haversine_km(point_a, point_b)
        return round(km * _UNITS[unit], 4)

    def bearing(
        self,
        point_a: Coord,
        point_b: Coord,
        as_degrees: bool = False,
    ) -> Union[str, float]:
        """
        Calculate the compass bearing from point_a to point_b.

        Parameters
        ----------
        point_a    : (lat, lng) origin
        point_b    : (lat, lng) destination
        as_degrees : if True, return numeric degrees instead of compass string

        Returns
        -------
        Compass direction string e.g. 'South-Southwest' or float degrees.

        Examples
        --------
        >>> geo.bearing((28.61, 77.20), (19.07, 72.87))
        'South-Southwest'
        >>> geo.bearing((28.61, 77.20), (19.07, 72.87), as_degrees=True)
        202.4
        """
        _validate_coord(*point_a)
        _validate_coord(*point_b)

        lat1 = _to_radians(point_a[0])
        lat2 = _to_radians(point_b[0])
        d_lng = _to_radians(point_b[1] - point_a[1])

        x = math.sin(d_lng) * math.cos(lat2)
        y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(d_lng)

        deg = (math.degrees(math.atan2(x, y)) + 360) % 360
        deg = round(deg, 2)

        return deg if as_degrees else _bearing_to_compass(deg)

    def buffer(
        self,
        center: Coord,
        radius: float,
        unit: str = "km",
        num_points: int = 64,
    ) -> Dict[str, Any]:
        """
        Create a circular buffer polygon around a center point.

        Parameters
        ----------
        center     : (lat, lng) center of the circle
        radius     : radius of the circle
        unit       : 'km' | 'miles' | 'm'
        num_points : polygon resolution (higher = smoother)

        Returns
        -------
        A GeoJSON Polygon dict representing the buffer zone.

        Examples
        --------
        >>> zone = geo.buffer((28.61, 77.20), radius=10, unit='km')
        >>> geo.point_in_polygon((28.65, 77.22), zone)
        True
        """
        _validate_coord(*center)

        if radius <= 0:
            raise InvalidRadiusError(radius)

        unit = unit.lower()
        to_km = {"km": 1.0, "miles": 1.60934, "mi": 1.60934, "m": 0.001}
        if unit not in to_km:
            raise InvalidUnitError(unit, list(to_km.keys()))

        radius_km = radius * to_km[unit]
        coords = _generate_circle_polygon(center, radius_km, num_points)

        return {
            "type": "Polygon",
            "coordinates": [coords],
            "_center": center,
            "_radius_km": radius_km,
        }

    def point_in_polygon(
        self,
        point: Coord,
        polygon: Dict[str, Any],
    ) -> bool:
        """
        Test whether a point falls inside a polygon (ray-casting algorithm).

        Parameters
        ----------
        point   : (lat, lng)
        polygon : GeoJSON Polygon dict or buffer() result

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
        _validate_coord(*point)

        if not isinstance(polygon, dict):
            raise InvalidBufferError()

        poly_type = polygon.get("type")

        if poly_type == "Polygon":
            ring = polygon["coordinates"][0]
        elif poly_type == "Feature":
            geom = polygon.get("geometry", {})
            if geom.get("type") != "Polygon":
                raise InvalidBufferError()
            ring = geom["coordinates"][0]
        else:
            raise InvalidBufferError()

        # ring is [[lng, lat], ...]
        lat, lng = point
        return _point_in_polygon_ray(lat, lng, ring)

    def nearest(
        self,
        point: Coord,
        unit: str = "km",
    ) -> Dict[str, Any]:
        """
        Find the nearest feature to a given point.

        Parameters
        ----------
        point : (lat, lng)
        unit  : distance unit for the result

        Returns
        -------
        Dict with keys ``'feature'``, ``'distance'``, ``'index'``.

        Examples
        --------
        >>> result = geo.nearest((28.61, 77.20))
        >>> result['distance']
        1.2
        >>> result['feature']['properties']['name']
        'AIIMS New Delhi'
        """
        self._require_data("nearest")
        _validate_coord(*point)

        best_dist = float("inf")
        best_feat = None
        best_idx  = -1

        for i, feat in enumerate(self._features()):
            coords = feat["geometry"]["coordinates"]
            feat_point = (coords[1], coords[0])
            d = self.distance(point, feat_point, unit=unit)
            if d < best_dist:
                best_dist = d
                best_feat = feat
                best_idx  = i

        return {"feature": best_feat, "distance": best_dist, "index": best_idx}

    # ──────────────────────────────────────────────────────────────────────
    # Filtering
    # ──────────────────────────────────────────────────────────────────────

    def filter(self, column: str, value: Any) -> "GeoEngine":
        """
        Filter features where properties[column] == value.

        Parameters
        ----------
        column : property key
        value  : value to match (case-insensitive for strings)

        Returns
        -------
        New GeoEngine with matching features.

        Examples
        --------
        >>> geo.filter("state", "Maharashtra").count()
        134
        """
        self._require_data("filter")

        # Check column exists in at least one feature
        sample_keys = set()
        for feat in self._features():
            sample_keys.update(feat.get("properties", {}).keys())

        if column not in sample_keys:
            raise ColumnNotFoundError(column, list(sample_keys), self._source_path or "dataset")

        val_str = str(value).strip().lower()

        matched = [
            feat for feat in self._features()
            if str(feat.get("properties", {}).get(column, "")).strip().lower() == val_str
        ]

        return self._clone_with(matched)

    def filter_by_radius(
        self,
        lat: float,
        lng: float,
        radius: float,
        unit: str = "km",
    ) -> "GeoEngine":
        """
        Filter features within a given radius of a point.

        Results are sorted by distance (nearest first) and a ``_distance``
        property is injected into each feature.

        Parameters
        ----------
        lat    : center latitude
        lng    : center longitude
        radius : search radius
        unit   : 'km' | 'miles' | 'm'

        Returns
        -------
        New GeoEngine with nearby features sorted nearest-first.

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

        center = (lat, lng)
        results = []

        for feat in self._features():
            coords = feat["geometry"]["coordinates"]
            feat_pt = (coords[1], coords[0])
            d = self.distance(center, feat_pt, unit=unit)
            if d <= radius:
                f2 = {**feat, "properties": {**feat.get("properties", {}), "_distance": d}}
                results.append((d, f2))

        results.sort(key=lambda x: x[0])
        return self._clone_with([r[1] for r in results])

    # ──────────────────────────────────────────────────────────────────────
    # Data access
    # ──────────────────────────────────────────────────────────────────────

    def count(self) -> int:
        """Return the number of loaded features."""
        return len(self._features())

    def get_features(self) -> List[Feature]:
        """Return the raw list of GeoJSON Feature dicts."""
        self._require_data("get_features")
        return self._features()

    def get_geojson(self) -> Dict[str, Any]:
        """Return the full GeoJSON FeatureCollection dict."""
        self._require_data("get_geojson")
        return self._data  # type: ignore[return-value]

    def bounds(self) -> Dict[str, float]:
        """
        Return the bounding box of all loaded features.

        Returns
        -------
        Dict with keys ``min_lat``, ``max_lat``, ``min_lng``, ``max_lng``.
        """
        self._require_data("bounds")
        lats, lngs = [], []
        for feat in self._features():
            coords = feat["geometry"]["coordinates"]
            lngs.append(coords[0])
            lats.append(coords[1])
        return {
            "min_lat": min(lats),
            "max_lat": max(lats),
            "min_lng": min(lngs),
            "max_lng": max(lngs),
        }

    def __len__(self) -> int:
        return self.count()

    def __repr__(self) -> str:
        status = f"{self.count()} features" if self._data else "no data loaded"
        return f"<GeoEngine [{status}]>"

    # ──────────────────────────────────────────────────────────────────────
    # Visualisation
    # ──────────────────────────────────────────────────────────────────────

    def map(
        self,
        output_path: str = "map.html",
        title: str = "QOREgeo Map",
        zoom: int = 5,
        center: Optional[Coord] = None,
    ) -> "GeoEngine":
        """
        Export an interactive Leaflet.js map to an HTML file.

        Opens in any browser — no server required.

        Parameters
        ----------
        output_path : where to write the HTML file
        title       : map title shown in the browser tab
        zoom        : initial zoom level (1–18)
        center      : (lat, lng) initial center; auto-detected if None

        Returns
        -------
        self

        Examples
        --------
        >>> geo.load("cities.csv").map("cities.html", title="India Cities")
        """
        self._require_data("map")
        build_map(
            features=self._features(),
            output_path=output_path,
            title=title,
            zoom=zoom,
            center=center,
        )
        return self

    def heatmap(
        self,
        output_path: str = "heatmap.html",
        title: str = "QOREgeo Heatmap",
        intensity_col: Optional[str] = None,
        zoom: int = 5,
        center: Optional[Coord] = None,
    ) -> "GeoEngine":
        """
        Export a density heatmap to an HTML file.

        Parameters
        ----------
        output_path   : where to write the HTML file
        title         : page title
        intensity_col : property column to use as heat intensity weight
        zoom          : initial zoom level
        center        : (lat, lng) initial center; auto-detected if None

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
        )
        return self
