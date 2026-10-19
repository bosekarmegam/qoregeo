"""
tests/test_dataset.py
=====================
Dataset operations and engine semantics for QOREgeo: ``filter()`` with every
operator, ``where()``, the spatial filters (radius, bbox, polygon, line),
``nearest()`` / ``k_nearest()``, ``sort_by()``, ``head()``, the container
protocol (iteration, ``len``, ``repr``), ``bounds()`` / ``bbox()`` /
``centroid()``, chaining without mutating the source, the "never loaded" vs
"dataset is empty" distinction, and the exception hierarchy.

Known-answer vectors (RFC 7946 §5.2 Fiji bbox, Movable Type cross-track
distance, spherical-mean centroid across the antimeridian) are checked with
explicit tolerances.

Run with:
    pytest tests/test_dataset.py -v
"""

from __future__ import annotations

import builtins
import csv
import math
import pickle
from pathlib import Path
from typing import Any

import pytest

import qoregeo
from qoregeo import (
    ColumnNotFoundError,
    EmptyDatasetError,
    FileReadError,
    GeoEngine,
    GeoFileNotFoundError,
    InvalidArgumentError,
    InvalidBufferError,
    InvalidCoordinateError,
    InvalidGeoJSONError,
    InvalidRadiusError,
    InvalidUnitError,
    NoDataError,
    QOREgeoError,
    UnsupportedFormatError,
)
from qoregeo.utils import EARTH_RADIUS_KM, _haversine_km

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures & helpers
# ─────────────────────────────────────────────────────────────────────────────

ONE_DEGREE_KM = EARTH_RADIUS_KM * math.pi / 180.0          # ≈ 111.19508 km (sphere)
WGS84_ONE_DEGREE_EQUATOR_KM = 6378.137 * math.pi / 180.0    # ≈ 111.31949 km (ellipsoid)

# Movable Type "crossTrackDistanceTo" example: a path and two test points.
XT_PATH = [(53.3206, -1.7297), (53.1887, 0.1334)]
XT_NEAR = (53.2611, -0.7972)    # 307.5 m off the path (cross-track)
XT_FAR = (53.0, 1.0)            # 61.551 km — nearest to endpoint b
LONDON = (51.5074, -0.1278)

CITY_ROWS: list[tuple[str, float, float, dict[str, Any]]] = [
    ("Delhi",     28.6139, 77.2090, {"state": "Delhi",       "population": 32900000}),
    ("Mumbai",    19.0760, 72.8777, {"state": "Maharashtra", "population": "20700000"}),
    ("Bangalore", 12.9716, 77.5946, {"state": "Karnataka",   "population": 13200000.0}),
    ("Kolkata",   22.5726, 88.3639, {"state": "West Bengal", "population": "14900000"}),
    ("Pune",      18.5204, 73.8567, {"state": "maharashtra", "population": 7400000}),
    ("Jaipur",    26.9124, 75.7873, {"state": "Rajasthan"}),   # no population key
]
CITY_NAMES = [row[0] for row in CITY_ROWS]

HOLED_SQUARE = {
    "type": "Polygon",
    "coordinates": [
        [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
        [[4, 4], [4, 6], [6, 6], [6, 4], [4, 4]],
    ],
}
TWO_SQUARES = {
    "type": "MultiPolygon",
    "coordinates": [
        [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
        [[[5, 5], [6, 5], [6, 6], [5, 6], [5, 5]]],
    ],
}
FIJI = [(-20.0, 177.0), (-16.0, 179.0), (-17.0, -179.0), (-18.0, -178.0)]


def _pt(lat: float, lng: float, **props: Any) -> dict[str, Any]:
    """A GeoJSON Point Feature from (lat, lng)."""
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lng, lat]},
        "properties": props,
    }


def _square(lat: float, lng: float, half: float = 1.0, **props: Any) -> dict[str, Any]:
    """A square Polygon Feature centred on (lat, lng) with half-size ``half`` degrees."""
    ring = [
        [lng - half, lat - half], [lng + half, lat - half],
        [lng + half, lat + half], [lng - half, lat + half],
        [lng - half, lat - half],
    ]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": props}


def _null(**props: Any) -> dict[str, Any]:
    """A Feature with a null geometry (allowed by RFC 7946)."""
    return {"type": "Feature", "geometry": None, "properties": props}


def _engine(*features: dict[str, Any]) -> GeoEngine:
    return GeoEngine(verbose=False).load_data(list(features))


def _names(geo: GeoEngine, key: str = "name") -> list[Any]:
    return [f["properties"].get(key) for f in geo]


def _dists(geo: GeoEngine) -> list[float]:
    return [f["properties"]["_distance"] for f in geo]


def _write_csv(path: Path, rows: list[list[Any]]) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    return path


@pytest.fixture
def cities() -> GeoEngine:
    return _engine(*(_pt(lat, lng, name=name, **props) for name, lat, lng, props in CITY_ROWS))


@pytest.fixture
def empty_result(cities: GeoEngine) -> GeoEngine:
    """Data was loaded, but a filter matched nothing."""
    result = cities.filter("name", "Atlantis")
    assert result.count() == 0
    return result


@pytest.fixture
def values() -> GeoEngine:
    """Features with assorted ``v`` property types (``id`` identifies each)."""
    raw: list[Any] = [5, "5", 5.0, "5.0", " 5 ", "five", True, None, float("nan"), "7", 3]
    feats = [_pt(0.0, float(i), id=i, v=val) for i, val in enumerate(raw)]
    feats.append(_pt(0.0, 20.0, id=99))   # no ``v`` at all
    return _engine(*feats)


@pytest.fixture
def along_path() -> GeoEngine:
    return _engine(_pt(*XT_FAR, name="far"), _pt(*LONDON, name="London"), _pt(*XT_NEAR, name="near"))


# ─────────────────────────────────────────────────────────────────────────────
# filter() — equality
# ─────────────────────────────────────────────────────────────────────────────

class TestFilterEquality:
    def test_string_equality_is_case_insensitive(self, cities):
        assert _names(cities.filter("state", "MAHARASHTRA")) == ["Mumbai", "Pune"]

    def test_string_equality_ignores_surrounding_whitespace(self):
        geo = _engine(_pt(0, 0, name=" Mumbai "), _pt(0, 1, name="Pune"))
        assert _names(geo.filter("name", "mumbai")) == [" Mumbai "]

    def test_string_equality_uses_casefold(self):
        geo = _engine(_pt(0, 0, name="Straße"), _pt(0, 1, name="Strasse"), _pt(0, 2, name="Gasse"))
        assert _names(geo.filter("name", "STRASSE")) == ["Straße", "Strasse"]

    def test_numeric_value_matches_numeric_strings_and_floats(self, values):
        # 5, "5", 5.0, "5.0", " 5 " — but not "five", True, None or NaN
        assert _names(values.filter("v", 5), "id") == [0, 1, 2, 3, 4]

    def test_string_value_compares_as_text(self, values):
        # "5" equals 5 and " 5 " textually, but not "5.0"
        assert _names(values.filter("v", "5"), "id") == [0, 1, 4]

    def test_numeric_string_property_matches_number(self, cities):
        assert _names(cities.filter("population", 20_700_000)) == ["Mumbai"]
        assert _names(cities.filter("population", 13_200_000)) == ["Bangalore"]   # 13200000.0

    def test_bool_value_is_compared_as_text_not_number(self):
        geo = _engine(_pt(0, 0, id=0, flag=True), _pt(0, 1, id=1, flag="TRUE"),
                      _pt(0, 2, id=2, flag=1), _pt(0, 3, id=3, flag="yes"))
        assert _names(geo.filter("flag", True), "id") == [0, 1]

    def test_not_equal_is_complement_of_equal(self, cities):
        eq = _names(cities.filter("population", 7_400_000))
        ne = _names(cities.filter("population", 7_400_000, op="!="))
        assert eq == ["Pune"]
        assert sorted(eq + ne) == sorted(CITY_NAMES)
        assert "Jaipur" in ne   # a missing property is "not equal"

    def test_filter_preserves_original_order(self, cities):
        assert _names(cities.filter("state", "Maharashtra", op="!=")) == \
            ["Delhi", "Bangalore", "Kolkata", "Jaipur"]


# ─────────────────────────────────────────────────────────────────────────────
# filter() — ordering operators
# ─────────────────────────────────────────────────────────────────────────────

class TestFilterOrdering:
    @pytest.mark.parametrize(
        ("op", "value", "expected"),
        [
            (">",  15_000_000, ["Delhi", "Mumbai"]),
            (">=", 14_900_000, ["Delhi", "Mumbai", "Kolkata"]),
            ("<",  13_200_000, ["Pune"]),
            ("<=", 13_200_000, ["Bangalore", "Pune"]),
        ],
    )
    def test_numeric_comparisons(self, cities, op, value, expected):
        # Jaipur (no population) is never included.
        assert _names(cities.filter("population", value, op=op)) == expected

    def test_numeric_comparison_excludes_non_numeric_values(self, values):
        # 5, "5", 5.0, "5.0", " 5 ", "7" and 3 are numeric; "five", True, None, NaN are not
        assert _names(values.filter("v", 1, op=">"), "id") == [0, 1, 2, 3, 4, 9, 10]

    def test_numeric_boundaries(self, values):
        assert _names(values.filter("v", 5, op="<"), "id") == [10]
        assert _names(values.filter("v", 5, op=">"), "id") == [9]
        assert _names(values.filter("v", 5, op=">="), "id") == [0, 1, 2, 3, 4, 9]

    def test_string_value_compares_lexicographically(self, cities):
        # Textual comparison: "7400000" > "20700000" because "7" > "2".
        assert _names(cities.filter("population", "20700000", op=">")) == ["Delhi", "Pune"]
        assert _names(cities.filter("population", 20_700_000, op=">")) == ["Delhi"]

    def test_string_comparison_is_case_insensitive(self, cities):
        assert _names(cities.filter("state", "M", op="<")) == ["Delhi", "Bangalore"]
        assert _names(cities.filter("state", "m", op="<")) == ["Delhi", "Bangalore"]

    def test_csv_numeric_strings_compare_numerically(self, tmp_path):
        path = _write_csv(tmp_path / "pop.csv", [
            ["name", "lat", "lng", "pop"],
            ["a", "10", "10", "900"],
            ["b", "11", "11", "1000"],
            ["c", "12", "12", "10000"],
        ])
        geo = GeoEngine(verbose=False).load(path)
        assert _names(geo.filter("pop", 950, op=">")) == ["b", "c"]
        assert _names(geo.filter("pop", 1000, op="<=")) == ["a", "b"]


# ─────────────────────────────────────────────────────────────────────────────
# filter() — membership and substring operators
# ─────────────────────────────────────────────────────────────────────────────

class TestFilterMembership:
    def test_in_with_strings_is_case_insensitive(self, cities):
        assert _names(cities.filter("state", ["delhi", "KARNATAKA"], op="in")) == \
            ["Delhi", "Bangalore"]

    def test_in_mixes_numeric_and_text_equality(self, cities):
        assert _names(cities.filter("population", [7_400_000, "32900000"], op="in")) == \
            ["Delhi", "Pune"]

    @pytest.mark.parametrize("make", [tuple, set, iter], ids=["tuple", "set", "iterator"])
    def test_in_accepts_any_non_string_iterable(self, cities, make):
        assert _names(cities.filter("name", make(["pune", "delhi"]), op="in")) == ["Delhi", "Pune"]

    def test_not_in_is_complement_of_in(self, cities):
        assert _names(cities.filter("state", ["Maharashtra"], op="in")) == ["Mumbai", "Pune"]
        assert _names(cities.filter("state", ["Maharashtra"], op="not in")) == \
            ["Delhi", "Bangalore", "Kolkata", "Jaipur"]

    def test_in_empty_list_matches_nothing_and_not_in_matches_all(self, cities):
        assert cities.filter("name", [], op="in").count() == 0
        assert cities.filter("name", [], op="not in").count() == len(CITY_ROWS)

    @pytest.mark.parametrize(
        ("op", "value"),
        [("in", "Delhi"), ("in", b"Delhi"), ("in", 5), ("not in", "Delhi"), ("not in", None)],
    )
    def test_in_requires_a_list(self, cities, op, value):
        with pytest.raises(InvalidArgumentError) as exc:
            cities.filter("name", value, op=op)
        assert exc.value.name == "value"

    def test_contains_is_case_insensitive_substring(self, cities):
        assert _names(cities.filter("state", "BENGAL", op="contains")) == ["Kolkata"]
        assert _names(cities.filter("state", "rash", op="contains")) == ["Mumbai", "Pune"]

    def test_contains_on_numbers_uses_text(self, cities):
        assert _names(cities.filter("population", 290, op="contains")) == ["Delhi"]

    def test_contains_excludes_missing_and_null(self):
        geo = _engine(_pt(0, 0, id=0, tag="none here"), _pt(0, 1, id=1, tag=None), _pt(0, 2, id=2))
        assert _names(geo.filter("tag", "none", op="contains"), "id") == [0]


# ─────────────────────────────────────────────────────────────────────────────
# filter() — validation and empty datasets
# ─────────────────────────────────────────────────────────────────────────────

class TestFilterValidation:
    @pytest.mark.parametrize("op", ["=", "IN", "", None])
    def test_invalid_op_raises(self, cities, op):
        with pytest.raises(InvalidArgumentError) as exc:
            cities.filter("name", "Delhi", op=op)
        assert exc.value.name == "op"
        assert exc.value.value == op

    def test_invalid_op_raises_even_on_empty_dataset(self, empty_result):
        with pytest.raises(InvalidArgumentError):
            empty_result.filter("name", "Delhi", op="~")

    def test_unknown_column_raises(self, cities):
        with pytest.raises(ColumnNotFoundError) as exc:
            cities.filter("country", "India")
        assert exc.value.column == "country"
        assert exc.value.available == ["name", "state", "population"]
        assert exc.value.filepath == "dataset"

    def test_column_present_in_only_one_feature_is_accepted(self):
        geo = _engine(_pt(0, 0, name="a"), _pt(0, 1, name="b"), _pt(0, 2, name="c", rare="x"))
        assert _names(geo.filter("rare", "X")) == ["c"]

    def test_unknown_column_reports_file_path(self, tmp_path):
        path = _write_csv(tmp_path / "c.csv", [["name", "lat", "lng"], ["a", "1", "2"]])
        geo = GeoEngine(verbose=False).load(path)
        with pytest.raises(ColumnNotFoundError) as exc:
            geo.filter("nope", 1)
        assert exc.value.filepath == str(path)
        assert exc.value.available == ["name"]

    def test_filter_on_empty_dataset_returns_empty_engine(self, empty_result):
        out = empty_result.filter("population", 1, op=">")
        assert isinstance(out, GeoEngine)
        assert out is not empty_result
        assert out.count() == 0
        assert out.get_features() == []

    def test_unknown_column_still_caught_after_empty_filter(self, empty_result):
        # Columns are inherited from the dataset the result was filtered from,
        # so a typo in a chained filter is not hidden by an empty result.
        with pytest.raises(ColumnNotFoundError):
            empty_result.filter("no_such_column", 1, op=">")


# ─────────────────────────────────────────────────────────────────────────────
# where()
# ─────────────────────────────────────────────────────────────────────────────

class TestWhere:
    def test_custom_predicate(self, cities):
        out = cities.where(lambda p: p.get("state") == "Maharashtra")   # exact, user-defined
        assert _names(out) == ["Mumbai"]

    def test_truthy_return_values_keep_features(self, cities):
        assert _names(cities.where(lambda p: p.get("population"))) == CITY_NAMES[:-1]

    def test_predicate_receives_properties_dict(self, cities):
        seen: list[dict[str, Any]] = []
        cities.where(lambda p: seen.append(p))
        assert [p["name"] for p in seen] == CITY_NAMES

    @pytest.mark.parametrize("predicate", ["state", None, {"state": "Delhi"}])
    def test_non_callable_raises(self, cities, predicate):
        with pytest.raises(InvalidArgumentError) as exc:
            cities.where(predicate)
        assert exc.value.name == "predicate"


# ─────────────────────────────────────────────────────────────────────────────
# filter_by_radius()
# ─────────────────────────────────────────────────────────────────────────────

class TestFilterByRadius:
    def test_sorted_nearest_first(self, cities):
        out = cities.filter_by_radius(28.61, 77.20, radius=1500)
        assert _names(out) == ["Delhi", "Jaipur", "Mumbai", "Pune", "Kolkata"]
        assert _dists(out) == sorted(_dists(out))

    @pytest.mark.parametrize(
        ("unit", "radius", "expected"),
        [
            ("km", 200, ONE_DEGREE_KM),
            ("m", 200_000, ONE_DEGREE_KM * 1000),
            ("miles", 100, ONE_DEGREE_KM / 1.609344),
        ],
    )
    def test_distance_in_requested_unit(self, unit, radius, expected):
        out = _engine(_pt(0, 1, name="E")).filter_by_radius(0, 0, radius, unit=unit)
        assert _dists(out) == [pytest.approx(expected, abs=1e-3)]

    def test_radius_is_interpreted_in_unit(self):
        geo = _engine(_pt(0, 1, name="E"))
        assert geo.filter_by_radius(0, 0, 200, unit="m").count() == 0
        assert geo.filter_by_radius(0, 0, 200, unit="km").count() == 1

    def test_vincenty_method_uses_ellipsoid(self):
        # One degree of longitude on the equator: 111.195 km (sphere) vs 111.319 km (WGS-84).
        geo = _engine(_pt(0, 1, name="E"))
        assert geo.filter_by_radius(0, 0, 111.25).count() == 1
        assert geo.filter_by_radius(0, 0, 111.25, method="vincenty").count() == 0
        out = geo.filter_by_radius(0, 0, 112, method="vincenty")
        assert _dists(out) == [pytest.approx(WGS84_ONE_DEGREE_EQUATOR_KM, abs=1e-3)]

    def test_ties_keep_original_order(self):
        west_first = _engine(_pt(0, -1, name="W"), _pt(0, 1, name="E"), _pt(0, -1, name="W2"))
        assert _names(west_first.filter_by_radius(0, 0, 500)) == ["W", "E", "W2"]
        east_first = _engine(_pt(0, 1, name="E"), _pt(0, -1, name="W"))
        assert _names(east_first.filter_by_radius(0, 0, 500)) == ["E", "W"]

    def test_boundary_is_inclusive(self):
        geo = _engine(_pt(0, 1, name="E"))
        exact = _haversine_km((0.0, 0.0), (0.0, 1.0))
        assert geo.filter_by_radius(0, 0, exact).count() == 1
        assert geo.filter_by_radius(0, 0, math.nextafter(exact, 0.0)).count() == 0

    def test_null_geometry_is_skipped(self):
        geo = _engine(_null(name="nowhere"), _pt(0, 0.1, name="here"))
        assert _names(geo.filter_by_radius(0, 0, 50)) == ["here"]

    def test_polygon_uses_representative_point(self):
        geo = _engine(_square(10, 10, 1.0, name="sq"), _pt(30, 30, name="far"))
        out = geo.filter_by_radius(10, 10, 1)
        assert _names(out) == ["sq"]
        assert _dists(out) == [pytest.approx(0.0, abs=1e-6)]
        assert out.get_features()[0]["geometry"]["type"] == "Polygon"

    @pytest.mark.parametrize(("lat", "lng"), [(91, 0), (0, 181), ("north", 0)])
    def test_invalid_center_raises(self, cities, lat, lng):
        with pytest.raises(InvalidCoordinateError):
            cities.filter_by_radius(lat, lng, 10)

    @pytest.mark.parametrize("radius", [0, -5, float("nan"), True, "ten"])
    def test_invalid_radius_raises(self, cities, radius):
        with pytest.raises(InvalidRadiusError):
            cities.filter_by_radius(28.61, 77.20, radius)

    def test_invalid_unit_or_method_raises(self, cities):
        with pytest.raises(InvalidUnitError):
            cities.filter_by_radius(28.61, 77.20, 10, unit="furlong")
        with pytest.raises(InvalidArgumentError):
            cities.filter_by_radius(28.61, 77.20, 10, method="manhattan")

    @pytest.mark.parametrize("call", ["filter_by_radius", "k_nearest"])
    def test_invalid_method_rejected_on_empty_dataset(self, empty_result, call):
        with pytest.raises(InvalidArgumentError):
            if call == "filter_by_radius":
                empty_result.filter_by_radius(0, 0, 10, method="manhattan")
            else:
                empty_result.k_nearest((0, 0), k=3, method="manhattan")


# ─────────────────────────────────────────────────────────────────────────────
# filter_by_bbox()
# ─────────────────────────────────────────────────────────────────────────────

class TestFilterByBBox:
    def test_normal_box(self, cities):
        assert _names(cities.filter_by_bbox(18.0, 72.0, 20.0, 74.0)) == ["Mumbai", "Pune"]

    def test_antimeridian_crossing_box(self):
        # RFC 7946 §5.2 Fiji box [177, -20, -178, -16] (west > east), passed by keyword.
        geo = _engine(_pt(-18, 179.5, name="in-east"), _pt(-18, -179, name="in-west"),
                      _pt(-18, 0, name="out-greenwich"), _pt(-21, 179, name="out-south"),
                      _pt(-18, 180, name="in-180"), _pt(-18, -180, name="in-minus-180"))
        out = geo.filter_by_bbox(min_lat=-20, min_lng=177, max_lat=-16, max_lng=-178)
        assert _names(out) == ["in-east", "in-west", "in-180", "in-minus-180"]

    def test_edges_are_inclusive(self):
        geo = _engine(_pt(10, 20, name="sw"), _pt(11, 21, name="ne"), _pt(10.5, 21.0001, name="out"))
        assert _names(geo.filter_by_bbox(10, 20, 11, 21)) == ["sw", "ne"]

    def test_accepts_bounds_of_another_engine(self, cities):
        maharashtra = cities.filter("state", "Maharashtra")
        assert _names(cities.filter_by_bbox(**maharashtra.bounds())) == ["Mumbai", "Pune"]

    def test_own_bounds_keep_every_located_feature(self, cities):
        assert cities.filter_by_bbox(**cities.bounds()).count() == cities.count()

    def test_polygon_uses_representative_point(self):
        geo = _engine(_square(10, 10, 1.0, name="sq"))
        assert geo.filter_by_bbox(9.5, 9.5, 10.5, 10.5).count() == 1
        # Box over the NE corner only: the polygon's centroid is outside it.
        assert geo.filter_by_bbox(10.5, 10.5, 11.0, 11.0).count() == 0

    def test_null_geometry_excluded(self):
        geo = _engine(_null(name="nowhere"), _pt(0, 0, name="origin"))
        assert _names(geo.filter_by_bbox(-90, -180, 90, 180)) == ["origin"]

    def test_min_lat_greater_than_max_lat_raises(self, cities, empty_result):
        with pytest.raises(InvalidArgumentError) as exc:
            cities.filter_by_bbox(20.0, 72.0, 18.0, 74.0)
        assert exc.value.name == "min_lat"
        with pytest.raises(InvalidArgumentError):   # validated even with nothing to filter
            empty_result.filter_by_bbox(20.0, 72.0, 18.0, 74.0)

    @pytest.mark.parametrize("box", [(-91, 0, 0, 10), (0, 0, 10, 200)])
    def test_out_of_range_box_raises(self, cities, box):
        with pytest.raises(InvalidCoordinateError):
            cities.filter_by_bbox(*box)


# ─────────────────────────────────────────────────────────────────────────────
# filter_by_polygon()
# ─────────────────────────────────────────────────────────────────────────────

class TestFilterByPolygon:
    def test_buffer_zone(self, cities):
        zone = GeoEngine().buffer((19.0760, 72.8777), radius=200)
        assert _names(cities.filter_by_polygon(zone)) == ["Mumbai", "Pune"]

    def test_feature_wrapping_buffer(self, cities):
        zone = GeoEngine().buffer((19.0760, 72.8777), radius=200)
        feature = {"type": "Feature", "geometry": zone, "properties": {}}
        assert _names(cities.filter_by_polygon(feature)) == ["Mumbai", "Pune"]

    def test_polygon_with_hole(self):
        geo = _engine(_pt(2, 2, name="inside"), _pt(5, 5, name="in-hole"), _pt(5, 12, name="outside"))
        assert _names(geo.filter_by_polygon(HOLED_SQUARE)) == ["inside"]

    def test_multipolygon(self):
        geo = _engine(_pt(0.5, 0.5, name="a"), _pt(3, 3, name="between"), _pt(5.5, 5.5, name="b"))
        assert _names(geo.filter_by_polygon(TWO_SQUARES)) == ["a", "b"]

    def test_feature_collection_is_a_union(self):
        fc = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": HOLED_SQUARE, "properties": {}},
            {"type": "Feature", "geometry": TWO_SQUARES, "properties": {}},
        ]}
        geo = _engine(_pt(2, 2, name="sq"), _pt(5.5, 5.5, name="hole-but-multi"), _pt(20, 20, name="x"))
        assert _names(geo.filter_by_polygon(fc)) == ["sq", "hole-but-multi"]

    def test_null_geometry_excluded(self):
        geo = _engine(_null(name="nowhere"), _pt(2, 2, name="inside"))
        assert _names(geo.filter_by_polygon(HOLED_SQUARE)) == ["inside"]

    @pytest.mark.parametrize(
        "bad",
        [
            "zone",
            {"type": "Point", "coordinates": [0, 0]},
            {"type": "Polygon", "coordinates": [[[0, 0], [1, 1]]]},
            {"type": "MultiPolygon", "coordinates": None},
            {"type": "Feature", "geometry": None, "properties": {}},
        ],
    )
    def test_invalid_polygon_raises(self, cities, bad):
        with pytest.raises(InvalidBufferError):
            cities.filter_by_polygon(bad)

    def test_invalid_polygon_raises_even_on_empty_dataset(self, empty_result):
        with pytest.raises(InvalidBufferError):
            empty_result.filter_by_polygon({"type": "LineString", "coordinates": [[0, 0], [1, 1]]})

    def test_no_data_checked_before_polygon(self):
        with pytest.raises(NoDataError):
            GeoEngine().filter_by_polygon("not a polygon")


# ─────────────────────────────────────────────────────────────────────────────
# filter_by_line()
# ─────────────────────────────────────────────────────────────────────────────

class TestFilterByLine:
    def test_cross_track_vector(self, along_path):
        out = along_path.filter_by_line(XT_PATH, distance=1)
        assert _names(out) == ["near"]
        assert _dists(out) == [pytest.approx(0.3075, abs=5e-4)]
        assert along_path.filter_by_line(XT_PATH, distance=0.3).count() == 0

    def test_sorted_nearest_first_with_distance(self, along_path):
        out = along_path.filter_by_line(XT_PATH, distance=100)
        assert _names(out) == ["near", "far"]
        # "far" lies beyond the end of the segment: distance is to endpoint b.
        assert _dists(out) == [pytest.approx(0.3075, abs=5e-4), pytest.approx(61.551, abs=0.01)]

    def test_distance_unit(self, along_path):
        out = along_path.filter_by_line(XT_PATH, distance=400, unit="m")
        assert _names(out) == ["near"]
        assert _dists(out) == [pytest.approx(307.5, abs=0.5)]

    def test_geojson_linestring_and_feature(self, along_path):
        line = {"type": "LineString", "coordinates": [[lng, lat] for lat, lng in XT_PATH]}
        feature = {"type": "Feature", "geometry": line, "properties": {}}
        expected = _names(along_path.filter_by_line(XT_PATH, distance=100))
        assert _names(along_path.filter_by_line(line, distance=100)) == expected
        assert _names(along_path.filter_by_line(feature, distance=100)) == expected

    def test_multilinestring(self):
        geo = _engine(_pt(10.05, 10.5, name="by-second"), _pt(0.01, 0.5, name="by-first"),
                      _pt(5, 5, name="between"))
        line = {"type": "MultiLineString", "coordinates": [
            [[0, 0], [1, 0]],
            [[10, 10], [11, 10]],
        ]}
        assert _names(geo.filter_by_line(line, distance=10)) == ["by-first", "by-second"]

    @pytest.mark.parametrize("distance", [0, -1, float("nan"), "far"])
    def test_invalid_distance_raises(self, along_path, distance):
        with pytest.raises(InvalidRadiusError):
            along_path.filter_by_line(XT_PATH, distance=distance)

    @pytest.mark.parametrize("line", [[], "53.3,-1.7", {"type": "Point", "coordinates": [0, 0]}])
    def test_invalid_line_raises(self, along_path, line):
        with pytest.raises(InvalidArgumentError):
            along_path.filter_by_line(line, distance=10)

    def test_invalid_point_in_line_raises(self, along_path):
        with pytest.raises(InvalidCoordinateError):
            along_path.filter_by_line([(53.3, -1.7), (95.0, 0.1)], distance=10)


# ─────────────────────────────────────────────────────────────────────────────
# nearest()
# ─────────────────────────────────────────────────────────────────────────────

class TestNearest:
    def test_result_shape(self, cities):
        result = cities.nearest((19.0, 72.9))
        assert set(result) == {"feature", "distance", "index"}
        assert result["feature"]["properties"]["name"] == "Mumbai"
        assert result["index"] == 1
        assert isinstance(result["distance"], float)

    def test_polygon_feature_measured_from_representative_point(self):
        geo = _engine(_pt(0, 5, name="point"), _square(0, 0, 1.0, name="square"))
        result = geo.nearest((0, 0.2))
        assert result["feature"]["properties"]["name"] == "square"
        assert result["distance"] == pytest.approx(0.2 * ONE_DEGREE_KM, abs=1e-3)

    def test_representative_point_not_nearest_vertex(self):
        # The square's corner (1, 1) is ~28 km away, but its centroid (0, 0) is ~189 km away,
        # while the point at (2.2, 2.2) is ~157 km away.
        geo = _engine(_square(0, 0, 1.0, name="square"), _pt(2.2, 2.2, name="point"))
        assert geo.nearest((1.2, 1.2))["feature"]["properties"]["name"] == "point"

    def test_null_geometry_ignored_and_index_is_original_position(self):
        geo = _engine(_null(name="nowhere"), _pt(10, 10, name="far"), _pt(0, 0.1, name="near"))
        result = geo.nearest((0, 0))
        assert result["feature"]["properties"]["name"] == "near"
        assert result["index"] == 2

    @pytest.mark.parametrize(
        ("unit", "factor"), [("km", 1.0), ("m", 1000.0), ("miles", 1 / 1.609344)],
    )
    def test_unit(self, unit, factor):
        result = _engine(_pt(0, 1)).nearest((0, 0), unit=unit)
        assert result["distance"] == pytest.approx(ONE_DEGREE_KM * factor, rel=1e-6)

    def test_vincenty_method(self):
        result = _engine(_pt(0, 1)).nearest((0, 0), method="vincenty")
        assert result["distance"] == pytest.approx(WGS84_ONE_DEGREE_EQUATOR_KM, abs=1e-3)

    def test_tie_returns_first_feature(self):
        geo = _engine(_pt(0, 1, name="E"), _pt(0, -1, name="W"))
        assert geo.nearest((0, 0))["index"] == 0

    def test_accepts_point_feature_as_query(self, cities):
        assert cities.nearest(_pt(28.61, 77.20))["feature"]["properties"]["name"] == "Delhi"

    def test_all_null_geometry_raises_empty(self):
        with pytest.raises(NoDataError) as exc:
            _engine(_null(name="a"), _null(name="b")).nearest((0, 0))
        assert exc.value.empty is True

    def test_invalid_arguments_raise(self, cities):
        with pytest.raises(InvalidCoordinateError):
            cities.nearest((95, 0))
        with pytest.raises(InvalidUnitError):
            cities.nearest((0, 0), unit="parsec")


# ─────────────────────────────────────────────────────────────────────────────
# k_nearest()
# ─────────────────────────────────────────────────────────────────────────────

class TestKNearest:
    def test_order(self, cities):
        assert _names(cities.k_nearest((28.61, 77.20), k=3)) == ["Delhi", "Jaipur", "Mumbai"]

    def test_default_k_is_five(self, cities):
        assert cities.k_nearest((28.61, 77.20)).count() == 5

    def test_k_larger_than_dataset_returns_all_sorted(self, cities):
        out = cities.k_nearest((28.61, 77.20), k=100)
        assert _names(out) == ["Delhi", "Jaipur", "Mumbai", "Pune", "Kolkata", "Bangalore"]

    def test_distance_injected_and_consistent_with_nearest(self, cities):
        out = cities.k_nearest((19.0, 73.0), k=2, unit="m")
        assert _dists(out) == sorted(_dists(out))
        assert _dists(out)[0] == cities.nearest((19.0, 73.0), unit="m")["distance"]

    def test_null_geometry_excluded(self):
        geo = _engine(_null(name="nowhere"), _pt(0, 1, name="a"), _pt(0, 2, name="b"))
        assert _names(geo.k_nearest((0, 0), k=5)) == ["a", "b"]

    def test_ties_keep_original_order(self):
        geo = _engine(_pt(0, 1, name="E"), _pt(0, -1, name="W"), _pt(1, 0, name="N"))
        assert _names(geo.k_nearest((0, 0), k=2)) == ["E", "W"]

    @pytest.mark.parametrize("k", [0, -1, 2.5, True, "3"])
    def test_invalid_k_raises(self, cities, k):
        with pytest.raises(InvalidArgumentError) as exc:
            cities.k_nearest((0, 0), k=k)
        assert exc.value.name == "k"


# ─────────────────────────────────────────────────────────────────────────────
# sort_by()
# ─────────────────────────────────────────────────────────────────────────────

class TestSortBy:
    def test_numeric_strings_sort_numerically(self):
        geo = _engine(_pt(0, 0, v="10"), _pt(0, 1, v="9"), _pt(0, 2, v="100"), _pt(0, 3, v=2.5))
        assert _names(geo.sort_by("v"), "v") == [2.5, "9", "10", "100"]

    def test_ascending(self, cities):
        assert _names(cities.sort_by("population")) == \
            ["Pune", "Bangalore", "Kolkata", "Mumbai", "Delhi", "Jaipur"]

    def test_descending(self, cities):
        assert _names(cities.sort_by("population", descending=True)) == \
            ["Delhi", "Mumbai", "Kolkata", "Bangalore", "Pune", "Jaipur"]

    @pytest.mark.parametrize("descending", [False, True])
    def test_missing_values_go_last(self, descending):
        geo = _engine(_pt(0, 0, id="none", v=None), _pt(0, 1, id="one", v=1),
                      _pt(0, 2, id="blank", v=""), _pt(0, 3, id="absent"), _pt(0, 4, id="two", v=2))
        present = ["two", "one"] if descending else ["one", "two"]
        assert _names(geo.sort_by("v", descending=descending), "id") == \
            present + ["none", "blank", "absent"]

    def test_text_sorts_case_insensitively(self):
        geo = _engine(_pt(0, 0, v="banana"), _pt(0, 1, v="Apple"), _pt(0, 2, v="cherry"))
        assert _names(geo.sort_by("v"), "v") == ["Apple", "banana", "cherry"]

    def test_numbers_before_text_when_ascending(self):
        geo = _engine(_pt(0, 0, v="abc"), _pt(0, 1, v="10"), _pt(0, 2, v=5))
        assert _names(geo.sort_by("v"), "v") == [5, "10", "abc"]

    @pytest.mark.parametrize("descending", [False, True])
    def test_stable_for_equal_keys(self, descending):
        geo = _engine(_pt(0, 0, id="a", v=1), _pt(0, 1, id="b", v="1"), _pt(0, 2, id="c", v=1.0))
        assert _names(geo.sort_by("v", descending=descending), "id") == ["a", "b", "c"]


# ─────────────────────────────────────────────────────────────────────────────
# head(), iteration, len(), repr()
# ─────────────────────────────────────────────────────────────────────────────

class TestHead:
    def test_default_and_n(self, cities):
        assert _names(cities.head()) == CITY_NAMES[:5]
        assert _names(cities.head(2)) == ["Delhi", "Mumbai"]

    def test_zero_returns_empty_engine(self, cities):
        out = cities.head(0)
        assert out.count() == 0
        assert out.get_features() == []

    def test_n_larger_than_dataset(self, cities):
        assert cities.head(1000).count() == len(CITY_ROWS)

    @pytest.mark.parametrize("n", [-1, 1.5, True, "2"])
    def test_invalid_n_raises(self, cities, n):
        with pytest.raises(InvalidArgumentError) as exc:
            cities.head(n)
        assert exc.value.name == "n"


class TestContainerProtocol:
    def test_iteration_yields_features(self, cities):
        feats = list(cities)
        assert feats == cities.get_features()
        assert [f["properties"]["name"] for f in feats] == CITY_NAMES

    def test_iterating_without_data_is_empty(self):
        assert list(GeoEngine()) == []

    def test_iteration_is_over_a_snapshot(self):
        geo = _engine(_pt(0, 0), _pt(0, 1))
        for _ in geo:
            geo.add_point(1, 1)
        assert geo.count() == 4

    def test_len(self, cities, empty_result):
        assert len(cities) == len(CITY_ROWS) == cities.count()
        assert len(empty_result) == 0
        assert len(GeoEngine()) == 0 == GeoEngine().count()

    def test_truthiness_follows_len(self, cities, empty_result):
        assert cities
        assert not empty_result

    def test_repr(self, cities, empty_result):
        assert repr(cities) == f"<GeoEngine [{len(CITY_ROWS)} features]>"
        assert repr(empty_result) == "<GeoEngine [0 features]>"
        assert repr(GeoEngine()) == "<GeoEngine [no data loaded]>"


# ─────────────────────────────────────────────────────────────────────────────
# bounds(), bbox(), centroid()
# ─────────────────────────────────────────────────────────────────────────────

class TestBounds:
    def test_points(self, cities):
        assert cities.bounds() == {
            "min_lat": 12.9716, "max_lat": 28.6139, "min_lng": 72.8777, "max_lng": 88.3639,
        }

    def test_polygon_vertices_count(self):
        geo = _engine(_square(10, 10, 1.0), _pt(0, 0))
        assert geo.bounds() == {"min_lat": 0.0, "max_lat": 11.0, "min_lng": 0.0, "max_lng": 11.0}

    def test_polygon_with_hole(self):
        geo = _engine({"type": "Feature", "geometry": HOLED_SQUARE, "properties": {}})
        assert geo.bounds() == {"min_lat": 0.0, "max_lat": 10.0, "min_lng": 0.0, "max_lng": 10.0}

    def test_null_geometry_ignored(self):
        geo = _engine(_null(), _pt(5, 6), _pt(-5, -6))
        assert geo.bounds() == {"min_lat": -5.0, "max_lat": 5.0, "min_lng": -6.0, "max_lng": 6.0}

    def test_naive_across_antimeridian(self):
        geo = _engine(*(_pt(lat, lng) for lat, lng in FIJI))
        assert geo.bounds() == {"min_lat": -20.0, "max_lat": -16.0, "min_lng": -179.0, "max_lng": 179.0}

    def test_all_null_geometry_raises_empty(self):
        with pytest.raises(NoDataError) as exc:
            _engine(_null(), _null()).bounds()
        assert exc.value.empty is True


class TestBBox:
    def test_fiji_antimeridian_vector(self):
        geo = _engine(*(_pt(lat, lng) for lat, lng in FIJI))
        assert geo.bbox() == [177.0, -20.0, -178.0, -16.0]

    def test_regular_order_is_west_south_east_north(self, cities):
        assert cities.bbox() == [72.8777, 12.9716, 88.3639, 28.6139]

    def test_single_point(self):
        assert _engine(_pt(28.6139, 77.2090)).bbox() == [77.209, 28.6139, 77.209, 28.6139]

    def test_polygon_vertices(self):
        assert _engine(_square(10, 10, 1.0)).bbox() == [9.0, 9.0, 11.0, 11.0]

    def test_all_null_geometry_raises_empty(self):
        with pytest.raises(NoDataError) as exc:
            _engine(_null()).bbox()
        assert exc.value.empty is True


class TestCentroid:
    def test_antimeridian_safe(self):
        lat, lng = _engine(_pt(0, 179), _pt(0, -179)).centroid()
        assert lat == pytest.approx(0.0, abs=1e-9)
        assert abs(lng) == pytest.approx(180.0, abs=1e-9)   # not the naive mean of 0

    def test_antimeridian_off_equator(self):
        lat, lng = _engine(_pt(-18, 179.5), _pt(-18, -179)).centroid()
        assert lat == pytest.approx(-18.0, abs=0.01)
        assert lng == pytest.approx(-179.75, abs=1e-6)

    def test_equator_midpoint_and_single_point(self):
        assert _engine(_pt(0, 0), _pt(0, 10)).centroid() == pytest.approx((0.0, 5.0), abs=1e-9)
        assert _engine(_pt(28.6139, 77.209)).centroid() == pytest.approx((28.6139, 77.209), abs=1e-6)

    def test_polygon_uses_representative_point(self):
        assert _engine(_square(10, 10, 1.0)).centroid() == pytest.approx((10.0, 10.0), abs=1e-6)

    def test_null_geometry_ignored(self):
        assert _engine(_null(), _pt(0, 0), _pt(0, 10)).centroid() == pytest.approx((0.0, 5.0), abs=1e-9)

    def test_rounded_to_six_decimals(self, cities):
        lat, lng = cities.centroid()
        assert round(lat, 6) == lat and round(lng, 6) == lng

    def test_all_null_geometry_raises_empty(self):
        with pytest.raises(NoDataError) as exc:
            _engine(_null()).centroid()
        assert exc.value.empty is True


# ─────────────────────────────────────────────────────────────────────────────
# Chaining and immutability
# ─────────────────────────────────────────────────────────────────────────────

CHAIN_OPS = {
    "filter": lambda g: g.filter("state", "Maharashtra"),
    "where": lambda g: g.where(lambda p: True),
    "filter_by_radius": lambda g: g.filter_by_radius(28.61, 77.20, 5000),
    "filter_by_bbox": lambda g: g.filter_by_bbox(-90, -180, 90, 180),
    "filter_by_polygon": lambda g: g.filter_by_polygon(GeoEngine().buffer((20, 78), 3000)),
    "filter_by_line": lambda g: g.filter_by_line([(28.61, 77.20), (19.07, 72.87)], 5000),
    "k_nearest": lambda g: g.k_nearest((28.61, 77.20), k=10),
    "sort_by": lambda g: g.sort_by("population", descending=True),
    "head": lambda g: g.head(3),
}


class TestChaining:
    @pytest.mark.parametrize("op", list(CHAIN_OPS))
    def test_returns_new_engine_without_mutating_source(self, cities, op):
        before = [dict(f["properties"]) for f in cities]
        out = CHAIN_OPS[op](cities)
        assert isinstance(out, GeoEngine)
        assert out is not cities
        assert out.get_features() is not cities.get_features()
        assert [f["properties"] for f in cities] == before   # e.g. no "_distance" leaks back

    def test_result_carries_verbose_flag(self, cities):
        loud = GeoEngine(verbose=True).load_data(cities.get_features())
        for name, op in CHAIN_OPS.items():
            assert op(cities).verbose is False, name
            assert op(loud).verbose is True, name

    def test_adding_to_result_does_not_touch_source(self, cities):
        out = cities.filter("state", "Maharashtra")
        out.add_point(0, 0, name="extra")
        assert out.count() == 3
        assert cities.count() == len(CITY_ROWS)

    def test_long_chain(self, cities):
        out = (
            cities.filter("population", 1, op=">=")
            .filter_by_radius(20.0, 75.0, 1200)
            .sort_by("population", descending=True)
            .head(2)
        )
        assert _names(out) == ["Delhi", "Mumbai"]
        assert all("_distance" in f["properties"] for f in out)
        assert all("_distance" not in f["properties"] for f in cities)

    def test_chained_filters_remember_source_path(self, tmp_path):
        path = _write_csv(tmp_path / "pts.csv", [["name", "lat", "lng"], ["a", "1", "2"]])
        out = GeoEngine(verbose=False).load(path).filter("name", "a").head(1)
        with pytest.raises(ColumnNotFoundError) as exc:
            out.filter("missing", 1)
        assert exc.value.filepath == str(path)


# ─────────────────────────────────────────────────────────────────────────────
# "No data loaded" vs "dataset is empty"
# ─────────────────────────────────────────────────────────────────────────────

NEEDS_DATA = {
    "filter": lambda g: g.filter("name", "x"),
    "where": lambda g: g.where(lambda p: True),
    "filter_by_radius": lambda g: g.filter_by_radius(0, 0, 10),
    "filter_by_bbox": lambda g: g.filter_by_bbox(0, 0, 1, 1),
    "filter_by_polygon": lambda g: g.filter_by_polygon(HOLED_SQUARE),
    "filter_by_line": lambda g: g.filter_by_line([(0, 0), (1, 1)], 10),
    "nearest": lambda g: g.nearest((0, 0)),
    "k_nearest": lambda g: g.k_nearest((0, 0)),
    "sort_by": lambda g: g.sort_by("name"),
    "head": lambda g: g.head(),
    "get_features": lambda g: g.get_features(),
    "get_geojson": lambda g: g.get_geojson(),
    "bounds": lambda g: g.bounds(),
    "bbox": lambda g: g.bbox(),
    "centroid": lambda g: g.centroid(),
}
NEEDS_FEATURES = ("nearest", "bounds", "bbox", "centroid")


class TestNoDataSemantics:
    @pytest.mark.parametrize("method", list(NEEDS_DATA))
    def test_never_loaded(self, method):
        with pytest.raises(NoDataError) as exc:
            NEEDS_DATA[method](GeoEngine())
        assert exc.value.empty is False
        assert exc.value.method_name == method
        assert "No Data Loaded" in str(exc.value)
        assert f"geo.{method}()" in str(exc.value)

    @pytest.mark.parametrize("method", NEEDS_FEATURES)
    def test_empty_after_filter(self, empty_result, method):
        with pytest.raises(NoDataError) as exc:
            NEEDS_DATA[method](empty_result)
        assert exc.value.empty is True
        assert exc.value.method_name == method
        assert "Dataset Is Empty" in str(exc.value)
        assert "No Data Loaded" not in str(exc.value)
        assert f"geo.{method}()" in str(exc.value)

    @pytest.mark.parametrize(
        "method", [m for m in NEEDS_DATA if m not in NEEDS_FEATURES and m != "get_geojson"]
    )
    def test_empty_dataset_operations_return_empty(self, empty_result, method):
        out = NEEDS_DATA[method](empty_result)
        if isinstance(out, GeoEngine):
            assert out.count() == 0
        else:
            assert out == []

    def test_get_geojson_of_empty_result(self, empty_result):
        assert empty_result.get_geojson() == {"type": "FeatureCollection", "features": []}


# ─────────────────────────────────────────────────────────────────────────────
# Exception hierarchy
# ─────────────────────────────────────────────────────────────────────────────

HIERARCHY = [
    (NoDataError, RuntimeError),
    (InvalidCoordinateError, ValueError),
    (InvalidUnitError, ValueError),
    (InvalidArgumentError, ValueError),
    (ColumnNotFoundError, LookupError),
    (GeoFileNotFoundError, builtins.FileNotFoundError),
    (UnsupportedFormatError, ValueError),
    (InvalidGeoJSONError, UnsupportedFormatError),
    (EmptyDatasetError, ValueError),
    (InvalidRadiusError, ValueError),
    (InvalidBufferError, ValueError),
    (FileReadError, ValueError),
]


class TestExceptionHierarchy:
    @pytest.mark.parametrize(("cls", "builtin"), HIERARCHY, ids=lambda x: x.__name__)
    def test_is_qoregeo_error_and_builtin(self, cls, builtin):
        assert issubclass(cls, QOREgeoError)
        assert issubclass(cls, builtin)

    def test_every_exported_error_is_covered(self):
        exported = {
            name for name in qoregeo.__all__
            if isinstance(getattr(qoregeo, name), type)
            and issubclass(getattr(qoregeo, name), BaseException)
        }
        assert exported == {"QOREgeoError"} | {cls.__name__ for cls, _ in HIERARCHY}

    def test_base_class_is_plain_exception(self):
        assert issubclass(QOREgeoError, Exception)
        assert not issubclass(QOREgeoError, ValueError)
        assert issubclass(InvalidGeoJSONError, ValueError)

    def test_raised_errors_caught_by_builtins(self, cities):
        with pytest.raises(ValueError):
            cities.filter("name", "x", op="~")
        with pytest.raises(LookupError):
            cities.filter("nope", "x")
        with pytest.raises(RuntimeError):
            GeoEngine().nearest((0, 0))
        with pytest.raises(QOREgeoError):
            cities.filter_by_radius(0, 0, -1)

    def test_missing_file_caught_by_builtin_file_not_found(self, tmp_path):
        missing = tmp_path / "missing.csv"
        try:
            GeoEngine().load(missing)
        except builtins.FileNotFoundError as e:
            caught: BaseException = e
        else:  # pragma: no cover - the load must fail
            pytest.fail("load() of a missing file did not raise")
        assert isinstance(caught, GeoFileNotFoundError)
        assert isinstance(caught, OSError)
        assert caught.path == str(missing)
        assert caught.filename == str(missing)
        assert "File Not Found" in str(caught)
        assert "[Errno" not in str(caught)

    @pytest.mark.parametrize("module", ["qoregeo", "qoregeo.exceptions"])
    def test_star_import_does_not_shadow_builtin(self, module):
        namespace: dict[str, Any] = {}
        exec(f"from {module} import *\nresult = FileNotFoundError", namespace)
        assert "FileNotFoundError" not in namespace
        assert namespace["result"] is builtins.FileNotFoundError
        assert namespace["GeoFileNotFoundError"] is GeoFileNotFoundError

    def test_star_import_exports_all_public_names(self):
        namespace: dict[str, Any] = {}
        exec("from qoregeo import *", namespace)
        assert set(qoregeo.__all__) <= set(namespace)
        assert namespace["GeoEngine"] is GeoEngine

    def test_file_not_found_alias_still_importable(self):
        # The 1.0 name still works but is deprecated: importing it by name
        # would shadow the built-in in the caller's module.
        with pytest.deprecated_call(match="GeoFileNotFoundError"):
            from qoregeo import FileNotFoundError as QFileNotFoundError
        with pytest.deprecated_call(match="GeoFileNotFoundError"):
            from qoregeo.exceptions import FileNotFoundError as ExcFileNotFoundError
        with pytest.deprecated_call():
            assert qoregeo.FileNotFoundError is GeoFileNotFoundError

        assert QFileNotFoundError is GeoFileNotFoundError
        assert ExcFileNotFoundError is GeoFileNotFoundError
        assert "FileNotFoundError" not in qoregeo.__all__
        assert "FileNotFoundError" not in qoregeo.exceptions.__all__

    @pytest.mark.parametrize(
        "exc",
        [
            InvalidCoordinateError(95.0, 10.0, "center"),
            ColumnNotFoundError("pop", ["name", "state"]),
            InvalidArgumentError("k", 0, "an integer >= 1"),
            NoDataError("bounds", empty=True),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_pickle_round_trip(self, exc):
        clone = pickle.loads(pickle.dumps(exc))
        assert type(clone) is type(exc)
        assert str(clone) == str(exc)
        assert vars(clone) == vars(exc)


# ─────────────────────────────────────────────────────────────────────────────
# Exception attributes
# ─────────────────────────────────────────────────────────────────────────────

class TestExceptionAttributes:
    def test_invalid_coordinate_lat_lng(self, cities):
        with pytest.raises(InvalidCoordinateError) as exc:
            cities.filter_by_radius(95, 10, 5)
        assert (exc.value.lat, exc.value.lng) == (95.0, 10.0)
        with pytest.raises(InvalidCoordinateError) as exc:
            cities.filter_by_radius("north", 10, 5)
        assert (exc.value.lat, exc.value.lng) == ("north", 10)   # raw value kept

    def test_column_not_found_attributes(self, cities):
        with pytest.raises(ColumnNotFoundError) as exc:
            cities.filter("country", "India")
        assert exc.value.column == "country"
        assert exc.value.available == ["name", "state", "population"]
        assert "'name', 'state', 'population'" in str(exc.value)

    def test_column_not_found_truncates_long_column_list(self):
        geo = _engine(_pt(0, 0, **{f"c{i:02d}": i for i in range(13)}))
        with pytest.raises(ColumnNotFoundError) as exc:
            geo.filter("zzz", 1)
        assert len(exc.value.available) == 13
        assert "(+3 more)" in str(exc.value)

    def test_invalid_unit_valid_list(self, cities):
        with pytest.raises(InvalidUnitError) as exc:
            cities.filter_by_radius(0, 0, 10, unit="furlong")
        assert exc.value.unit == "furlong"
        assert set(exc.value.valid) >= {"km", "m", "miles", "mi", "ft", "yd", "nmi"}
        assert "furlong" not in exc.value.valid

    def test_invalid_argument_attributes(self, cities):
        with pytest.raises(InvalidArgumentError) as exc:
            cities.filter("name", "x", op="~")
        assert exc.value.name == "op"
        assert exc.value.value == "~"
        assert "'=='" in exc.value.expected and "'contains'" in exc.value.expected

    def test_invalid_radius_attribute(self, cities):
        with pytest.raises(InvalidRadiusError) as exc:
            cities.filter_by_radius(0, 0, -5)
        assert exc.value.radius == -5

    def test_invalid_buffer_reason(self, cities):
        with pytest.raises(InvalidBufferError) as exc:
            cities.filter_by_polygon({"type": "Point", "coordinates": [0, 0]})
        assert exc.value.reason and "Point" in exc.value.reason


# ─────────────────────────────────────────────────────────────────────────────
# Version
# ─────────────────────────────────────────────────────────────────────────────

class TestVersion:
    def test_engine_version_matches_package(self):
        from qoregeo._version import __version__ as raw_version

        assert GeoEngine.VERSION == qoregeo.__version__ == raw_version
        assert GeoEngine().VERSION == qoregeo.__version__

    def test_version_is_semver_like(self):
        parts = qoregeo.__version__.split(".")
        assert len(parts) >= 3
        assert all(p.isdigit() for p in parts[:3])
