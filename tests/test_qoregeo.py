"""
tests/test_qoregeo.py
=====================
Full test suite for QOREgeo v1.0.2.

Run with:
    pytest tests/ -v
    pytest tests/ -v --tb=short   # compact traceback
    pytest tests/ --cov=qoregeo   # with coverage
"""

from __future__ import annotations

import csv
import json
import math
import os
import tempfile
from pathlib import Path
from typing import List

import pytest

from qoregeo import GeoEngine
from qoregeo.exceptions import (
    NoDataError,
    InvalidCoordinateError,
    InvalidUnitError,
    ColumnNotFoundError,
    EmptyDatasetError,
    InvalidRadiusError,
    InvalidBufferError,
    UnsupportedFormatError,
)
from qoregeo.exceptions import FileNotFoundError as QFileNotFoundError
from qoregeo.utils import (
    _haversine_km,
    _bearing_to_compass,
    _generate_circle_polygon,
    _point_in_polygon_ray,
    _detect_lat_lng_columns,
    _validate_coord,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

DELHI  = (28.6139,  77.2090)
MUMBAI = (19.0760,  72.8777)
LONDON = (51.5074,  -0.1278)
NYC    = (40.7128,  -74.0060)
SYDNEY = (-33.8688, 151.2093)


@pytest.fixture
def tmp_csv(tmp_path: Path) -> str:
    """Create a temporary CSV with Indian cities."""
    rows = [
        {"name": "Delhi",     "state": "Delhi",       "population": 32900000, "latitude": 28.6139,  "longitude": 77.2090},
        {"name": "Mumbai",    "state": "Maharashtra",  "population": 20700000, "latitude": 19.0760,  "longitude": 72.8777},
        {"name": "Bangalore", "state": "Karnataka",    "population": 13200000, "latitude": 12.9716,  "longitude": 77.5946},
        {"name": "Kolkata",   "state": "West Bengal",  "population": 14900000, "latitude": 22.5726,  "longitude": 88.3639},
        {"name": "Chennai",   "state": "Tamil Nadu",   "population": 11300000, "latitude": 13.0827,  "longitude": 80.2707},
        {"name": "Pune",      "state": "Maharashtra",  "population":  7400000, "latitude": 18.5204,  "longitude": 73.8567},
        {"name": "Hyderabad", "state": "Telangana",    "population": 10500000, "latitude": 17.3850,  "longitude": 78.4867},
    ]

    p = tmp_path / "cities.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    return str(p)


@pytest.fixture
def tmp_geojson(tmp_path: Path) -> str:
    """Create a temporary GeoJSON file."""
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [77.2090, 28.6139]},
                "properties": {"name": "Delhi", "pop": 32900000},
            },
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [72.8777, 19.0760]},
                "properties": {"name": "Mumbai", "pop": 20700000},
            },
        ],
    }
    p = tmp_path / "cities.geojson"
    p.write_text(json.dumps(fc), encoding="utf-8")
    return str(p)


@pytest.fixture
def geo_loaded(tmp_csv: str) -> GeoEngine:
    return GeoEngine().load(tmp_csv)


# ─────────────────────────────────────────────────────────────────────────────
# GeoEngine initialisation
# ─────────────────────────────────────────────────────────────────────────────

class TestInit:
    def test_repr_no_data(self):
        assert "no data loaded" in repr(GeoEngine())

    def test_repr_with_data(self, geo_loaded):
        assert "7 features" in repr(geo_loaded)

    def test_version(self):
        assert GeoEngine.VERSION == "1.0.2"

    def test_len_no_data(self):
        assert len(GeoEngine()) == 0

    def test_len_with_data(self, geo_loaded):
        assert len(geo_loaded) == 7


# ─────────────────────────────────────────────────────────────────────────────
# Loading
# ─────────────────────────────────────────────────────────────────────────────

class TestLoad:
    def test_load_csv_returns_self(self, tmp_csv):
        geo = GeoEngine()
        result = geo.load(tmp_csv)
        assert result is geo

    def test_load_csv_count(self, geo_loaded):
        assert geo_loaded.count() == 7

    def test_load_geojson(self, tmp_geojson):
        geo = GeoEngine().load(tmp_geojson)
        assert geo.count() == 2

    def test_load_geojson_properties(self, tmp_geojson):
        geo = GeoEngine().load(tmp_geojson)
        names = {f["properties"]["name"] for f in geo.get_features()}
        assert "Delhi" in names
        assert "Mumbai" in names

    def test_load_file_not_found(self):
        with pytest.raises(QFileNotFoundError):
            GeoEngine().load("/nonexistent/path/file.csv")

    def test_load_unsupported_format(self, tmp_path):
        p = tmp_path / "data.xlsx"
        p.write_text("dummy")
        with pytest.raises(UnsupportedFormatError):
            GeoEngine().load(str(p))

    def test_load_csv_alt_column_names(self, tmp_path):
        """CSV with 'lat' and 'lon' instead of 'latitude'/'longitude'."""
        p = tmp_path / "alt.csv"
        with open(p, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["name", "lat", "lon"])
            writer.writeheader()
            writer.writerow({"name": "Delhi", "lat": 28.6139, "lon": 77.2090})
        geo = GeoEngine().load(str(p))
        assert geo.count() == 1

    def test_load_csv_override_columns(self, tmp_path):
        """Manual lat_col / lng_col override."""
        p = tmp_path / "custom.csv"
        with open(p, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["city", "ycoord", "xcoord"])
            writer.writeheader()
            writer.writerow({"city": "Delhi", "ycoord": 28.6139, "xcoord": 77.2090})
        geo = GeoEngine().load(str(p), lat_col="ycoord", lng_col="xcoord")
        assert geo.count() == 1

    def test_load_skips_blank_lat(self, tmp_path):
        """Rows with blank lat/lng are skipped, not crashed."""
        p = tmp_path / "blanks.csv"
        with open(p, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["name", "latitude", "longitude"])
            writer.writeheader()
            writer.writerow({"name": "Valid",   "latitude": 28.61, "longitude": 77.20})
            writer.writerow({"name": "Invalid", "latitude": "",    "longitude": 77.20})
        geo = GeoEngine().load(str(p))
        assert geo.count() == 1

    def test_load_data_in_memory(self):
        feats = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [77.20, 28.61]},
                "properties": {"name": "Delhi"},
            }
        ]
        geo = GeoEngine().load_data(feats)
        assert geo.count() == 1

    def test_load_data_empty_raises(self):
        with pytest.raises(EmptyDatasetError):
            GeoEngine().load_data([])


# ─────────────────────────────────────────────────────────────────────────────
# Save
# ─────────────────────────────────────────────────────────────────────────────

class TestSave:
    def test_save_geojson(self, geo_loaded, tmp_path):
        out = str(tmp_path / "out.geojson")
        geo_loaded.save(out)
        assert os.path.exists(out)
        with open(out) as f:
            data = json.load(f)
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 7

    def test_save_csv(self, geo_loaded, tmp_path):
        out = str(tmp_path / "out.csv")
        geo_loaded.save(out)
        assert os.path.exists(out)
        with open(out, newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 7
        assert "latitude" in rows[0]
        assert "longitude" in rows[0]

    def test_save_returns_self(self, geo_loaded, tmp_path):
        out = str(tmp_path / "out.geojson")
        result = geo_loaded.save(out)
        assert result is geo_loaded

    def test_save_no_data_raises(self, tmp_path):
        with pytest.raises(NoDataError):
            GeoEngine().save(str(tmp_path / "out.geojson"))

    def test_save_unsupported_format(self, geo_loaded, tmp_path):
        with pytest.raises(UnsupportedFormatError):
            geo_loaded.save(str(tmp_path / "out.xlsx"))


# ─────────────────────────────────────────────────────────────────────────────
# Distance
# ─────────────────────────────────────────────────────────────────────────────

class TestDistance:
    def test_delhi_mumbai_km(self):
        d = GeoEngine().distance(DELHI, MUMBAI)
        assert 1140 < d < 1170   # ~1153 km

    def test_delhi_mumbai_miles(self):
        d = GeoEngine().distance(DELHI, MUMBAI, unit="miles")
        assert 700 < d < 730

    def test_delhi_mumbai_meters(self):
        d = GeoEngine().distance(DELHI, MUMBAI, unit="m")
        assert 1_140_000 < d < 1_170_000

    def test_delhi_mumbai_feet(self):
        d = GeoEngine().distance(DELHI, MUMBAI, unit="ft")
        assert d > 3_000_000

    def test_same_point_is_zero(self):
        assert GeoEngine().distance(DELHI, DELHI) == 0.0

    def test_london_nyc(self):
        d = GeoEngine().distance(LONDON, NYC)
        assert 5500 < d < 5600

    def test_invalid_unit(self):
        with pytest.raises(InvalidUnitError):
            GeoEngine().distance(DELHI, MUMBAI, unit="lightyears")

    def test_invalid_coord_lat_too_high(self):
        with pytest.raises(InvalidCoordinateError):
            GeoEngine().distance((95.0, 0.0), MUMBAI)

    def test_invalid_coord_lng_too_low(self):
        with pytest.raises(InvalidCoordinateError):
            GeoEngine().distance(DELHI, (0.0, -200.0))

    def test_symmetric(self):
        d1 = GeoEngine().distance(DELHI, MUMBAI)
        d2 = GeoEngine().distance(MUMBAI, DELHI)
        assert abs(d1 - d2) < 0.001

    def test_southern_hemisphere(self):
        cape_town = (-33.9249, 18.4241)
        joburg = (-26.2041, 28.0473)
        d = GeoEngine().distance(cape_town, joburg)
        assert 1200 < d < 1350

    def test_result_is_float(self):
        d = GeoEngine().distance(DELHI, MUMBAI)
        assert isinstance(d, float)


# ─────────────────────────────────────────────────────────────────────────────
# Bearing
# ─────────────────────────────────────────────────────────────────────────────

class TestBearing:
    def test_delhi_to_mumbai_string(self):
        b = GeoEngine().bearing(DELHI, MUMBAI)
        assert isinstance(b, str)
        assert "south" in b.lower()

    def test_delhi_to_mumbai_degrees(self):
        deg = GeoEngine().bearing(DELHI, MUMBAI, as_degrees=True)
        assert 190 < deg < 220

    def test_delhi_to_london_degrees(self):
        deg = GeoEngine().bearing(DELHI, LONDON, as_degrees=True)
        assert 280 < deg < 320   # roughly northwest

    def test_north_bearing(self):
        south = (0.0, 0.0)
        north = (10.0, 0.0)
        b = GeoEngine().bearing(south, north)
        assert "north" in b.lower()

    def test_east_bearing(self):
        west = (0.0, 0.0)
        east = (0.0, 10.0)
        deg = GeoEngine().bearing(west, east, as_degrees=True)
        assert 80 < deg < 100

    def test_invalid_coord(self):
        with pytest.raises(InvalidCoordinateError):
            GeoEngine().bearing((95.0, 0.0), MUMBAI)

    def test_returns_string_by_default(self):
        b = GeoEngine().bearing(DELHI, MUMBAI)
        assert isinstance(b, str)


# ─────────────────────────────────────────────────────────────────────────────
# Buffer
# ─────────────────────────────────────────────────────────────────────────────

class TestBuffer:
    def test_buffer_returns_polygon(self):
        zone = GeoEngine().buffer(DELHI, radius=10)
        assert zone["type"] == "Polygon"

    def test_buffer_has_coordinates(self):
        zone = GeoEngine().buffer(DELHI, radius=10)
        assert "coordinates" in zone
        ring = zone["coordinates"][0]
        assert len(ring) > 4

    def test_buffer_ring_is_closed(self):
        zone = GeoEngine().buffer(DELHI, radius=10)
        ring = zone["coordinates"][0]
        assert ring[0] == ring[-1]

    def test_buffer_miles(self):
        zone = GeoEngine().buffer(DELHI, radius=10, unit="miles")
        assert zone["_radius_km"] > 15   # 10 miles > 16 km

    def test_buffer_resolution(self):
        zone = GeoEngine().buffer(DELHI, radius=10, num_points=32)
        assert len(zone["coordinates"][0]) == 33   # 32 + closing point

    def test_buffer_zero_radius_raises(self):
        with pytest.raises(InvalidRadiusError):
            GeoEngine().buffer(DELHI, radius=0)

    def test_buffer_negative_radius_raises(self):
        with pytest.raises(InvalidRadiusError):
            GeoEngine().buffer(DELHI, radius=-5)

    def test_buffer_invalid_unit(self):
        with pytest.raises(InvalidUnitError):
            GeoEngine().buffer(DELHI, radius=10, unit="parsecs")


# ─────────────────────────────────────────────────────────────────────────────
# Point in polygon
# ─────────────────────────────────────────────────────────────────────────────

class TestPointInPolygon:
    def test_center_is_inside(self):
        zone = GeoEngine().buffer(DELHI, radius=50)
        assert GeoEngine().point_in_polygon(DELHI, zone) is True

    def test_near_center_inside(self):
        zone = GeoEngine().buffer(DELHI, radius=50)
        close = (28.62, 77.21)
        assert GeoEngine().point_in_polygon(close, zone) is True

    def test_far_away_is_outside(self):
        zone = GeoEngine().buffer(DELHI, radius=50)
        assert GeoEngine().point_in_polygon(MUMBAI, zone) is False

    def test_edge_case_at_boundary(self):
        """Point at ~50 km from Delhi is uncertain — just check it doesn't crash."""
        zone = GeoEngine().buffer(DELHI, radius=50)
        edge = (28.6139, 78.0)   # roughly east of Delhi
        result = GeoEngine().point_in_polygon(edge, zone)
        assert isinstance(result, bool)

    def test_invalid_polygon_raises(self):
        with pytest.raises(InvalidBufferError):
            GeoEngine().point_in_polygon(DELHI, {"type": "INVALID"})

    def test_non_dict_polygon_raises(self):
        with pytest.raises(InvalidBufferError):
            GeoEngine().point_in_polygon(DELHI, "not a polygon")   # type: ignore

    def test_small_buffer_excludes_far_point(self):
        zone = GeoEngine().buffer(DELHI, radius=1)
        assert GeoEngine().point_in_polygon(MUMBAI, zone) is False


# ─────────────────────────────────────────────────────────────────────────────
# Nearest
# ─────────────────────────────────────────────────────────────────────────────

class TestNearest:
    def test_nearest_returns_dict(self, geo_loaded):
        result = geo_loaded.nearest(DELHI)
        assert isinstance(result, dict)
        assert "feature" in result
        assert "distance" in result
        assert "index" in result

    def test_nearest_to_delhi_is_delhi(self, geo_loaded):
        result = geo_loaded.nearest(DELHI)
        assert result["feature"]["properties"]["name"] == "Delhi"
        assert result["distance"] < 1.0

    def test_nearest_to_mumbai_is_mumbai(self, geo_loaded):
        result = geo_loaded.nearest(MUMBAI)
        assert result["feature"]["properties"]["name"] == "Mumbai"

    def test_nearest_no_data_raises(self):
        with pytest.raises(NoDataError):
            GeoEngine().nearest(DELHI)

    def test_nearest_distance_is_float(self, geo_loaded):
        result = geo_loaded.nearest(DELHI)
        assert isinstance(result["distance"], float)

    def test_nearest_index_valid(self, geo_loaded):
        result = geo_loaded.nearest(DELHI)
        assert 0 <= result["index"] < geo_loaded.count()


# ─────────────────────────────────────────────────────────────────────────────
# Filter
# ─────────────────────────────────────────────────────────────────────────────

class TestFilter:
    def test_filter_maharashtra(self, geo_loaded):
        result = geo_loaded.filter("state", "Maharashtra")
        assert result.count() == 2   # Mumbai + Pune

    def test_filter_single(self, geo_loaded):
        result = geo_loaded.filter("state", "Karnataka")
        assert result.count() == 1
        assert result.get_features()[0]["properties"]["name"] == "Bangalore"

    def test_filter_case_insensitive(self, geo_loaded):
        r1 = geo_loaded.filter("state", "maharashtra")
        r2 = geo_loaded.filter("state", "MAHARASHTRA")
        assert r1.count() == r2.count()

    def test_filter_returns_new_engine(self, geo_loaded):
        result = geo_loaded.filter("state", "Delhi")
        assert result is not geo_loaded

    def test_filter_empty_result(self, geo_loaded):
        result = geo_loaded.filter("state", "Atlantis")
        assert result.count() == 0

    def test_filter_bad_column_raises(self, geo_loaded):
        with pytest.raises(ColumnNotFoundError):
            geo_loaded.filter("nonexistent_column", "value")

    def test_filter_no_data_raises(self):
        with pytest.raises(NoDataError):
            GeoEngine().filter("state", "Delhi")

    def test_filter_chaining(self, geo_loaded):
        result = geo_loaded.filter("state", "Maharashtra")
        names = {f["properties"]["name"] for f in result.get_features()}
        assert "Mumbai" in names
        assert "Pune" in names


# ─────────────────────────────────────────────────────────────────────────────
# Filter by radius
# ─────────────────────────────────────────────────────────────────────────────

class TestFilterByRadius:
    def test_near_delhi_finds_delhi(self, geo_loaded):
        result = geo_loaded.filter_by_radius(28.61, 77.20, radius=10)
        assert result.count() >= 1
        names = {f["properties"]["name"] for f in result.get_features()}
        assert "Delhi" in names

    def test_large_radius_finds_all(self, geo_loaded):
        result = geo_loaded.filter_by_radius(20.0, 78.0, radius=5000)
        assert result.count() == 7

    def test_tiny_radius_finds_nothing(self, geo_loaded):
        result = geo_loaded.filter_by_radius(0.0, 0.0, radius=1)
        assert result.count() == 0

    def test_sorted_nearest_first(self, geo_loaded):
        result = geo_loaded.filter_by_radius(28.61, 77.20, radius=5000)
        dists = [f["properties"]["_distance"] for f in result.get_features()]
        assert dists == sorted(dists)

    def test_distance_injected(self, geo_loaded):
        result = geo_loaded.filter_by_radius(28.61, 77.20, radius=5000)
        for feat in result.get_features():
            assert "_distance" in feat["properties"]

    def test_returns_new_engine(self, geo_loaded):
        result = geo_loaded.filter_by_radius(28.61, 77.20, radius=500)
        assert result is not geo_loaded

    def test_no_data_raises(self):
        with pytest.raises(NoDataError):
            GeoEngine().filter_by_radius(28.61, 77.20, radius=100)

    def test_zero_radius_raises(self, geo_loaded):
        with pytest.raises(InvalidRadiusError):
            geo_loaded.filter_by_radius(28.61, 77.20, radius=0)

    def test_negative_radius_raises(self, geo_loaded):
        with pytest.raises(InvalidRadiusError):
            geo_loaded.filter_by_radius(28.61, 77.20, radius=-10)


# ─────────────────────────────────────────────────────────────────────────────
# Count, bounds, get_features, get_geojson
# ─────────────────────────────────────────────────────────────────────────────

class TestDataAccess:
    def test_count(self, geo_loaded):
        assert geo_loaded.count() == 7

    def test_get_features_list(self, geo_loaded):
        feats = geo_loaded.get_features()
        assert isinstance(feats, list)
        assert len(feats) == 7

    def test_get_geojson_type(self, geo_loaded):
        gj = geo_loaded.get_geojson()
        assert gj["type"] == "FeatureCollection"

    def test_bounds_keys(self, geo_loaded):
        b = geo_loaded.bounds()
        for key in ("min_lat", "max_lat", "min_lng", "max_lng"):
            assert key in b

    def test_bounds_delhi_is_in_range(self, geo_loaded):
        b = geo_loaded.bounds()
        lat_ok = b["min_lat"] <= DELHI[0] <= b["max_lat"]
        lng_ok = b["min_lng"] <= DELHI[1] <= b["max_lng"]
        assert lat_ok and lng_ok

    def test_get_features_no_data_raises(self):
        with pytest.raises(NoDataError):
            GeoEngine().get_features()

    def test_get_geojson_no_data_raises(self):
        with pytest.raises(NoDataError):
            GeoEngine().get_geojson()

    def test_bounds_no_data_raises(self):
        with pytest.raises(NoDataError):
            GeoEngine().bounds()


# ─────────────────────────────────────────────────────────────────────────────
# Method chaining
# ─────────────────────────────────────────────────────────────────────────────

class TestMethodChaining:
    def test_chain_load_filter(self, tmp_csv):
        result = GeoEngine().load(tmp_csv).filter("state", "Maharashtra")
        assert result.count() == 2

    def test_chain_load_filter_radius(self, tmp_csv):
        result = (
            GeoEngine()
            .load(tmp_csv)
            .filter("state", "Maharashtra")
            .filter_by_radius(18.52, 73.85, radius=200)
        )
        assert result.count() >= 1

    def test_chain_save_returns_self(self, tmp_csv, tmp_path):
        out = str(tmp_path / "chain.geojson")
        result = GeoEngine().load(tmp_csv).filter("state", "Delhi").save(out)
        assert result.count() == 1


# ─────────────────────────────────────────────────────────────────────────────
# Map + Heatmap (filesystem only — no browser)
# ─────────────────────────────────────────────────────────────────────────────

class TestMapOutput:
    def test_map_creates_file(self, geo_loaded, tmp_path):
        out = str(tmp_path / "test_map.html")
        geo_loaded.map(out)
        assert os.path.exists(out)

    def test_map_is_html(self, geo_loaded, tmp_path):
        out = str(tmp_path / "test_map.html")
        geo_loaded.map(out)
        content = Path(out).read_text()
        assert "<html" in content
        assert "leaflet" in content.lower()

    def test_map_returns_self(self, geo_loaded, tmp_path):
        out = str(tmp_path / "test_map.html")
        result = geo_loaded.map(out)
        assert result is geo_loaded

    def test_map_no_data_raises(self, tmp_path):
        with pytest.raises(NoDataError):
            GeoEngine().map(str(tmp_path / "no.html"))

    def test_heatmap_creates_file(self, geo_loaded, tmp_path):
        out = str(tmp_path / "test_heat.html")
        geo_loaded.heatmap(out)
        assert os.path.exists(out)

    def test_heatmap_is_html(self, geo_loaded, tmp_path):
        out = str(tmp_path / "test_heat.html")
        geo_loaded.heatmap(out)
        content = Path(out).read_text()
        assert "<html" in content

    def test_heatmap_with_intensity(self, geo_loaded, tmp_path):
        out = str(tmp_path / "test_heat_int.html")
        geo_loaded.heatmap(out, intensity_col="population")
        assert os.path.exists(out)

    def test_heatmap_returns_self(self, geo_loaded, tmp_path):
        out = str(tmp_path / "test_heat.html")
        result = geo_loaded.heatmap(out)
        assert result is geo_loaded

    def test_heatmap_no_data_raises(self, tmp_path):
        with pytest.raises(NoDataError):
            GeoEngine().heatmap(str(tmp_path / "no.html"))


# ─────────────────────────────────────────────────────────────────────────────
# Utilities (white-box)
# ─────────────────────────────────────────────────────────────────────────────

class TestUtils:
    def test_haversine_known(self):
        km = _haversine_km(DELHI, MUMBAI)
        assert 1140 < km < 1170

    def test_haversine_zero(self):
        assert _haversine_km(DELHI, DELHI) == 0.0

    def test_haversine_symmetric(self):
        d1 = _haversine_km(DELHI, MUMBAI)
        d2 = _haversine_km(MUMBAI, DELHI)
        assert abs(d1 - d2) < 0.001

    def test_bearing_north(self):
        result = _bearing_to_compass(0.0)
        assert result == "North"

    def test_bearing_south(self):
        result = _bearing_to_compass(180.0)
        assert result == "South"

    def test_bearing_east(self):
        result = _bearing_to_compass(90.0)
        assert result == "East"

    def test_bearing_west(self):
        result = _bearing_to_compass(270.0)
        assert result == "West"

    def test_circle_polygon_count(self):
        ring = _generate_circle_polygon(DELHI, 10.0, num_points=32)
        assert len(ring) == 33   # 32 + closing

    def test_circle_polygon_is_closed(self):
        ring = _generate_circle_polygon(DELHI, 10.0)
        assert ring[0] == ring[-1]

    def test_ray_cast_inside(self):
        ring = _generate_circle_polygon(DELHI, 50.0)
        assert _point_in_polygon_ray(DELHI[0], DELHI[1], ring) is True

    def test_ray_cast_outside(self):
        ring = _generate_circle_polygon(DELHI, 50.0)
        assert _point_in_polygon_ray(MUMBAI[0], MUMBAI[1], ring) is False

    def test_detect_columns_standard(self):
        lat, lng = _detect_lat_lng_columns(
            ["name", "latitude", "longitude", "population"], "test.csv"
        )
        assert lat == "latitude"
        assert lng == "longitude"

    def test_detect_columns_short(self):
        lat, lng = _detect_lat_lng_columns(["id", "lat", "lng"], "test.csv")
        assert lat == "lat"
        assert lng == "lng"

    def test_detect_columns_missing_raises(self):
        with pytest.raises(ColumnNotFoundError):
            _detect_lat_lng_columns(["id", "name", "city"], "test.csv")

    def test_validate_coord_valid(self):
        _validate_coord(28.61, 77.20)   # should not raise

    def test_validate_coord_invalid_lat(self):
        with pytest.raises(InvalidCoordinateError):
            _validate_coord(95.0, 0.0)

    def test_validate_coord_invalid_lng(self):
        with pytest.raises(InvalidCoordinateError):
            _validate_coord(0.0, 200.0)


# ─────────────────────────────────────────────────────────────────────────────
# Error message quality
# ─────────────────────────────────────────────────────────────────────────────

class TestErrorMessages:
    """Ensure every error message contains actionable guidance."""

    def test_no_data_error_has_fix(self):
        with pytest.raises(NoDataError) as exc:
            GeoEngine().map("x.html")
        assert "Fix it" in str(exc.value)
        assert "geo.load" in str(exc.value)

    def test_invalid_coord_has_range(self):
        with pytest.raises(InvalidCoordinateError) as exc:
            GeoEngine().distance((95.0, 0.0), MUMBAI)
        assert "90" in str(exc.value)

    def test_invalid_unit_lists_valid(self):
        with pytest.raises(InvalidUnitError) as exc:
            GeoEngine().distance(DELHI, MUMBAI, unit="cubits")
        assert "km" in str(exc.value)

    def test_file_not_found_has_tip(self):
        with pytest.raises(QFileNotFoundError) as exc:
            GeoEngine().load("/no/such/file.csv")
        assert "spelling" in str(exc.value).lower() or "path" in str(exc.value).lower()

    def test_column_not_found_shows_available(self, geo_loaded):
        with pytest.raises(ColumnNotFoundError) as exc:
            geo_loaded.filter("nonexistent", "value")
        assert "state" in str(exc.value) or "name" in str(exc.value)

    def test_empty_dataset_error(self, tmp_path):
        p = tmp_path / "header_only.csv"
        p.write_text("latitude,longitude,name\n")
        with pytest.raises(EmptyDatasetError):
            GeoEngine().load(str(p))
