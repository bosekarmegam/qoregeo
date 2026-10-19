"""
tests/test_geometry.py
======================
Geometry & math test suite for QOREgeo: distance (haversine / Vincenty),
units, coordinate coercion, bearing, destination, midpoint, buffer,
point-in-polygon, area, length, distance-to-line and geohash — plus
white-box tests of the pure helpers in ``qoregeo.utils``.

Known-answer vectors come from published sources (Movable Type Scripts,
Vincenty 1975 / Geoscience Australia, geopy, Wikipedia, geographiclib)
and are checked with explicit tolerances.

Run with:
    pytest tests/test_geometry.py -v
"""

from __future__ import annotations

import json
import math
from decimal import Decimal

import pytest

import qoregeo.engine as engine_mod
from qoregeo import (
    GeoEngine,
    InvalidArgumentError,
    InvalidBufferError,
    InvalidCoordinateError,
    InvalidRadiusError,
    InvalidUnitError,
    QOREgeoError,
)
from qoregeo.utils import (
    EARTH_RADIUS_KM,
    EARTH_RADIUS_M,
    _bearing_to_compass,
    _geohash_bounds,
    _haversine_km,
    _lng_span,
    _normalize_lng,
    _ring_area_m2,
    _spherical_mean,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures & helpers
# ─────────────────────────────────────────────────────────────────────────────

DELHI  = (28.6139, 77.2090)
MUMBAI = (19.0760, 72.8777)
AGRA   = (27.1767, 78.0081)
JAIPUR = (26.9124, 75.7873)
SYDNEY = (-33.8688, 151.2093)

ONE_DEGREE_KM = EARTH_RADIUS_KM * math.pi / 180.0   # ≈ 111.19508 km on the sphere
HALF_CIRCUMFERENCE_KM = math.pi * EARTH_RADIUS_KM   # ≈ 20015.1144 km

COMPASS_16 = [
    "North", "North-Northeast", "Northeast", "East-Northeast",
    "East", "East-Southeast", "Southeast", "South-Southeast",
    "South", "South-Southwest", "Southwest", "West-Southwest",
    "West", "West-Northwest", "Northwest", "North-Northwest",
]

UNIT_SQUARE = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}

SQUARE_WITH_HOLE = {
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

# A box straddling the antimeridian, written with continuous longitudes
# (170 → 190), the form buffer() emits and Leaflet draws as one shape.
ANTIMERIDIAN_BOX = {
    "type": "Polygon",
    "coordinates": [[[170, -10], [190, -10], [190, 10], [170, 10], [170, -10]]],
}

# The same corners written with a jump from +170 to -170. GeoJSON edges are
# straight lines in lng/lat (RFC 7946 §3.1.1; Turf, Shapely and Leaflet agree),
# so this is a 340°-wide band, not the 20° box — RFC 7946 §3.1.9 says to cut
# such shapes at ±180 (see FIJI_SPLIT) or keep longitudes continuous.
JUMP_BOX = {
    "type": "Polygon",
    "coordinates": [[[170, -10], [-170, -10], [-170, 10], [170, 10], [170, -10]]],
}

# Fiji-style MultiPolygon split at ±180 (RFC 7946 §3.1.9).
FIJI_SPLIT = {
    "type": "MultiPolygon",
    "coordinates": [
        [[[177, -20], [180, -20], [180, -16], [177, -16], [177, -20]]],
        [[[-180, -20], [-178, -20], [-178, -16], [-180, -16], [-180, -20]]],
    ],
}


@pytest.fixture
def geo() -> GeoEngine:
    return GeoEngine(verbose=False)


def _feature(geometry: dict) -> dict:
    return {"type": "Feature", "geometry": geometry, "properties": {}}


def _signed_planar_area(ring: list[list[float]]) -> float:
    """Shoelace area in the (lng, lat) plane: > 0 means counter-clockwise."""
    return sum(
        ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
        for i in range(len(ring) - 1)
    ) / 2.0


def _strip_private(buffer: dict) -> dict:
    """A buffer without its _center/_radius_km metadata: a plain GeoJSON Polygon."""
    return {k: v for k, v in buffer.items() if not k.startswith("_")}


# ─────────────────────────────────────────────────────────────────────────────
# Coordinate coercion (shared by every point-taking method)
# ─────────────────────────────────────────────────────────────────────────────

class TestCoordinateCoercion:
    @pytest.mark.parametrize(
        "point",
        [
            DELHI,
            [28.6139, 77.2090],
            ("28.6139", "77.2090"),
            (" 28.6139 ", "77.2090 "),
            (Decimal("28.6139"), Decimal("77.2090")),
            (28.6139, 77.2090, 216.0),                         # (lat, lng, alt)
            {"type": "Point", "coordinates": [77.2090, 28.6139]},
            {"type": "Point", "coordinates": [77.2090, 28.6139, 216.0]},
            _feature({"type": "Point", "coordinates": [77.2090, 28.6139]}),
            {"lat": 28.6139, "lng": 77.2090},                  # the MCP tools' shape
            {"lat": 28.6139, "lon": 77.2090},
            {"latitude": 28.6139, "longitude": 77.2090, "name": "Delhi"},
            {"lat": "28.6139", "lng": "77.2090"},
        ],
        ids=[
            "tuple", "list", "strings", "padded-strings", "decimals", "3-tuple",
            "geojson-point", "geojson-point-3d", "point-feature",
            "lat-lng-dict", "lat-lon-dict", "latitude-longitude-dict", "lat-lng-strings",
        ],
    )
    def test_accepted_point_forms(self, geo, point):
        assert geo.distance(point, MUMBAI) == pytest.approx(1148.0965, abs=1e-4)

    @pytest.mark.parametrize(
        "call",
        [
            lambda g, p: g.bearing(p, MUMBAI, as_degrees=True),
            lambda g, p: g.destination(p, 10, 90),
            lambda g, p: g.midpoint(p, MUMBAI),
            lambda g, p: g.geohash(p, precision=7),
            lambda g, p: g.point_in_polygon(p, g.buffer(DELHI, 1)),
            lambda g, p: g.distance_to_line(p, [MUMBAI, AGRA]),
            lambda g, p: g.buffer(p, 5)["_center"],
        ],
        ids=["bearing", "destination", "midpoint", "geohash", "point_in_polygon",
             "distance_to_line", "buffer"],
    )
    def test_lat_lng_dict_same_as_tuple(self, geo, call):
        # The docs and the MCP tools use {"lat": …, "lng": …}; every method takes it.
        assert call(geo, {"lat": DELHI[0], "lng": DELHI[1]}) == call(geo, DELHI)

    def test_lat_lng_dict_errors_name_both_forms(self, geo):
        with pytest.raises(InvalidCoordinateError) as info:
            geo.distance({"lat": 28.6}, MUMBAI)
        assert "{'lat': …, 'lng': …}" in str(info.value)

    def test_geojson_point_uses_lng_lat_order(self, geo):
        point = {"type": "Point", "coordinates": [10.0, 50.0]}   # lng=10, lat=50
        assert geo.distance(point, (50.0, 10.0)) == 0.0

    @pytest.mark.parametrize(
        "bad",
        [
            (True, 0.0),
            (0.0, False),
            (math.nan, 0.0),
            (0.0, math.nan),
            (math.inf, 0.0),
            (0.0, -math.inf),
            (90.0001, 0.0),
            (-90.0001, 0.0),
            (0.0, 180.0001),
            (0.0, -180.0001),
            None,
            (1.0,),
            (),
            "28.6,77.2",
            5,
            ("north", "east"),
            (None, 1.0),
            {"lat": 1.0},
            {"lat": 95.0, "lng": 2.0},
            {"type": "Point", "lat": 1.0, "lng": 2.0},
            {"type": "Point", "coordinates": [1.0]},
            {"type": "Point", "coordinates": [200.0, 0.0]},   # lng out of range
            {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            {"type": "Feature", "geometry": None, "properties": {}},
        ],
        ids=[
            "bool-lat", "bool-lng", "nan-lat", "nan-lng", "inf-lat", "neg-inf-lng",
            "lat>90", "lat<-90", "lng>180", "lng<-180", "none", "1-tuple", "empty-tuple",
            "string", "int", "words", "none-lat", "lat-only-dict", "lat-lng-dict-out-of-range",
            "point-with-lat-lng-keys", "short-point",
            "point-lng-out-of-range", "linestring", "null-feature",
        ],
    )
    @pytest.mark.parametrize("position", ["a", "b"])
    def test_rejected_point_forms(self, geo, bad, position):
        args = (bad, MUMBAI) if position == "a" else (MUMBAI, bad)
        with pytest.raises(InvalidCoordinateError):
            geo.distance(*args)

    @pytest.mark.parametrize("point", [(90.0, 180.0), (-90.0, -180.0), (0.0, 180.0), (-90, 0)])
    def test_range_boundaries_are_inclusive(self, geo, point):
        assert geo.distance(point, point) == 0.0

    def test_error_is_value_error_and_qoregeo_error(self, geo):
        with pytest.raises(ValueError) as exc_info:
            geo.distance((95.0, 0.0), MUMBAI)
        assert isinstance(exc_info.value, QOREgeoError)
        assert isinstance(exc_info.value, InvalidCoordinateError)

    @pytest.mark.parametrize(
        "call",
        [
            lambda g, p: g.bearing(p, MUMBAI),
            lambda g, p: g.bearing(MUMBAI, p, as_degrees=True),
            lambda g, p: g.destination(p, 10, 90),
            lambda g, p: g.midpoint(p, MUMBAI),
            lambda g, p: g.buffer(p, 10),
            lambda g, p: g.point_in_polygon(p, UNIT_SQUARE),
            lambda g, p: g.distance_to_line(p, [DELHI, MUMBAI]),
            lambda g, p: g.length([DELHI, p]),
            lambda g, p: g.geohash(p),
        ],
        ids=[
            "bearing-a", "bearing-b", "destination", "midpoint", "buffer",
            "point_in_polygon", "distance_to_line", "length", "geohash",
        ],
    )
    def test_every_point_method_validates(self, geo, call):
        with pytest.raises(InvalidCoordinateError):
            call(geo, (95.0, 0.0))


# ─────────────────────────────────────────────────────────────────────────────
# Distance — haversine (default)
# ─────────────────────────────────────────────────────────────────────────────

class TestDistanceHaversine:
    def test_delhi_mumbai_documented_value(self, geo):
        assert geo.distance(DELHI, MUMBAI) == pytest.approx(1148.0965, abs=1e-4)

    def test_default_method_is_haversine(self, geo):
        assert geo.distance(DELHI, MUMBAI) == geo.distance(DELHI, MUMBAI, method="haversine")

    def test_identical_points_zero(self, geo):
        assert geo.distance(SYDNEY, SYDNEY) == 0.0

    def test_symmetric(self, geo):
        assert geo.distance(DELHI, SYDNEY) == geo.distance(SYDNEY, DELHI)

    def test_one_degree_of_longitude_on_equator(self, geo):
        assert geo.distance((0, 0), (0, 1)) == pytest.approx(ONE_DEGREE_KM, abs=1e-4)

    def test_across_antimeridian_is_short_way_round(self, geo):
        assert geo.distance((0, 179.5), (0, -179.5)) == pytest.approx(ONE_DEGREE_KM, abs=1e-4)

    def test_antipodal_points_half_circumference(self, geo):
        # Floating error can push the haversine term above 1; must not raise.
        assert geo.distance((0, 0), (0, 180)) == pytest.approx(HALF_CIRCUMFERENCE_KM, abs=1e-4)
        assert geo.distance((90, 0), (-90, 0)) == pytest.approx(HALF_CIRCUMFERENCE_KM, abs=1e-4)

    def test_longitude_irrelevant_at_pole(self, geo):
        assert geo.distance((90, 0), (90, 100)) == 0.0
        assert geo.distance((-90, -45), (-90, 135)) == 0.0

    def test_geopy_newport_cleveland_great_circle(self, geo):
        # geopy docstring pair; haversine with R = 6371.0088 km.
        d = geo.distance((41.49008, -71.312796), (41.499498, -81.695391), unit="miles")
        assert d == pytest.approx(536.997973839, abs=1e-3)

    def test_land_s_end_to_john_o_groats(self, geo):
        # Movable Type Scripts example: 968.9 km.
        lands_end = (50 + 3 / 60 + 59 / 3600, -(5 + 42 / 60 + 53 / 3600))
        john_o_groats = (58 + 38 / 60 + 38 / 3600, -(3 + 4 / 60 + 12 / 3600))
        assert geo.distance(lands_end, john_o_groats) == pytest.approx(968.9, abs=0.1)

    def test_result_is_float_rounded_to_4_decimals(self, geo):
        d = geo.distance(DELHI, SYDNEY, unit="m")
        assert isinstance(d, float)
        assert d == round(d, 4)

    @pytest.mark.parametrize("method", ["euclidean", "", None, "great-circle"])
    def test_invalid_method(self, geo, method):
        with pytest.raises(InvalidArgumentError) as exc_info:
            geo.distance(DELHI, MUMBAI, method=method)
        assert exc_info.value.name == "method"


# ─────────────────────────────────────────────────────────────────────────────
# Distance — units and aliases
# ─────────────────────────────────────────────────────────────────────────────

class TestDistanceUnits:
    KM = _haversine_km(DELHI, MUMBAI)

    @pytest.mark.parametrize(
        "unit, km_per_unit",
        [
            ("km", 1.0),
            ("m", 0.001),
            ("miles", 1.609344),
            ("mi", 1.609344),
            ("ft", 0.0003048),
            ("yd", 0.0009144),
            ("nmi", 1.852),
        ],
    )
    def test_canonical_units(self, geo, unit, km_per_unit):
        assert geo.distance(DELHI, MUMBAI, unit=unit) == pytest.approx(self.KM / km_per_unit, abs=1e-4)

    @pytest.mark.parametrize(
        "alias, canonical",
        [
            ("kilometer", "km"), ("kilometers", "km"), ("kilometre", "km"), ("kilometres", "km"),
            ("meter", "m"), ("meters", "m"), ("metre", "m"), ("metres", "m"),
            ("mile", "miles"),
            ("foot", "ft"), ("feet", "ft"),
            ("yard", "yd"), ("yards", "yd"),
            ("nautical_mile", "nmi"), ("nautical_miles", "nmi"),
            ("KM", "km"), (" Miles ", "miles"), ("NMI", "nmi"), ("Ft", "ft"),
        ],
    )
    def test_aliases_and_case_insensitivity(self, geo, alias, canonical):
        assert geo.distance(DELHI, MUMBAI, unit=alias) == geo.distance(DELHI, MUMBAI, unit=canonical)

    def test_nautical_mile_is_exactly_1852_m(self, geo):
        nmi = geo.distance(DELHI, MUMBAI, unit="nmi")
        m = geo.distance(DELHI, MUMBAI, unit="m")
        assert nmi * 1852 == pytest.approx(m, abs=0.2)

    def test_yard_is_three_feet(self, geo):
        yd = geo.distance(DELHI, MUMBAI, unit="yd")
        ft = geo.distance(DELHI, MUMBAI, unit="ft")
        assert yd * 3 == pytest.approx(ft, abs=1e-3)

    @pytest.mark.parametrize("unit", ["lightyears", "", "km2", "kms", None, 5, ["km"]])
    def test_invalid_unit(self, geo, unit):
        with pytest.raises(InvalidUnitError) as exc_info:
            geo.distance(DELHI, MUMBAI, unit=unit)
        assert exc_info.value.unit == unit
        assert {"km", "m", "miles", "mi", "ft", "yd", "nmi"} <= set(exc_info.value.valid)

    def test_invalid_unit_is_value_error(self, geo):
        with pytest.raises(ValueError):
            geo.distance(DELHI, MUMBAI, unit="furlongs")


# ─────────────────────────────────────────────────────────────────────────────
# Distance — Vincenty (WGS-84 ellipsoid)
# ─────────────────────────────────────────────────────────────────────────────

class TestDistanceVincenty:
    def test_flinders_peak_to_buninyong(self, geo):
        # Vincenty (1975) / Geoscience Australia worked example: 54 972.271 m.
        flinders = (-37.95103341666667, 144.42486788888888)
        buninyong = (-37.65282113888889, 143.92649552777777)
        d = geo.distance(flinders, buninyong, unit="m", method="vincenty")
        assert d == pytest.approx(54972.271, abs=1e-3)

    def test_geopy_newport_cleveland(self, geo):
        d = geo.distance((41.49008, -71.312796), (41.499498, -81.695391),
                         unit="miles", method="vincenty")
        assert d == pytest.approx(538.390445368, abs=1e-3)

    def test_geopy_wellington_salamanca(self, geo):
        d = geo.distance((-41.32, 174.81), (40.96, -5.50), method="vincenty")
        assert d == pytest.approx(19959.6792674, abs=1e-3)

    def test_delhi_mumbai(self, geo):
        assert geo.distance(DELHI, MUMBAI, method="vincenty") == pytest.approx(1144.5264, abs=1e-3)

    def test_one_degree_along_equator(self, geo):
        # a · π/180 with a = 6 378 137 m.
        assert geo.distance((0, 0), (0, 1), method="vincenty") == pytest.approx(111.3195, abs=1e-3)

    def test_one_degree_along_meridian_at_equator(self, geo):
        assert geo.distance((0, 0), (1, 0), method="vincenty") == pytest.approx(110.5744, abs=1e-3)

    def test_pole_to_pole_is_half_meridian(self, geo):
        assert geo.distance((90, 0), (-90, 0), method="vincenty") == pytest.approx(20003.9315, abs=1e-3)

    def test_identical_and_polar_points_zero(self, geo):
        assert geo.distance(DELHI, DELHI, method="vincenty") == 0.0
        assert geo.distance((90, 0), (90, 100), method="vincenty") == 0.0

    def test_symmetric(self, geo):
        d1 = geo.distance(DELHI, SYDNEY, method="vincenty")
        d2 = geo.distance(SYDNEY, DELHI, method="vincenty")
        assert d1 == pytest.approx(d2, abs=1e-4)

    def test_close_to_haversine(self, geo):
        for a, b in [(DELHI, MUMBAI), (DELHI, SYDNEY), ((51.5, -0.12), (40.71, -74.0))]:
            h = geo.distance(a, b)
            v = geo.distance(a, b, method="vincenty")
            assert abs(h - v) / h < 0.006

    def test_units_apply(self, geo):
        km = geo.distance(DELHI, MUMBAI, method="vincenty")
        nmi = geo.distance(DELHI, MUMBAI, unit="nmi", method="vincenty")
        assert nmi == pytest.approx(km / 1.852, abs=1e-3)

    def test_nearly_antipodal_falls_back_to_haversine_with_warning(self, geo):
        a, b = (0.0, 0.0), (0.5, 179.7)   # Vincenty's iteration does not converge here
        with pytest.warns(UserWarning, match="did not converge"):
            d = geo.distance(a, b, method="vincenty")
        assert d == geo.distance(a, b)
        # Still within 0.05% of the exact geodesic (Karney: 19 944.127 km).
        assert d == pytest.approx(19944.127421, rel=5e-4)


# ─────────────────────────────────────────────────────────────────────────────
# Bearing
# ─────────────────────────────────────────────────────────────────────────────

class TestBearing:
    def test_delhi_mumbai_compass(self, geo):
        assert geo.bearing(DELHI, MUMBAI) == "South-Southwest"

    def test_delhi_mumbai_degrees(self, geo):
        assert geo.bearing(DELHI, MUMBAI, as_degrees=True) == pytest.approx(203.47, abs=0.01)

    def test_return_types(self, geo):
        assert isinstance(geo.bearing(DELHI, MUMBAI), str)
        assert isinstance(geo.bearing(DELHI, MUMBAI, as_degrees=True), float)

    @pytest.mark.parametrize(
        "target, degrees, name",
        [
            ((1, 0), 0.0, "North"),
            ((0, 1), 90.0, "East"),
            ((-1, 0), 180.0, "South"),
            ((0, -1), 270.0, "West"),
        ],
    )
    def test_cardinal_directions(self, geo, target, degrees, name):
        assert geo.bearing((0, 0), target, as_degrees=True) == pytest.approx(degrees, abs=1e-9)
        assert geo.bearing((0, 0), target) == name

    def test_identical_points(self, geo):
        assert geo.bearing(DELHI, DELHI) == "North"
        assert geo.bearing(DELHI, DELHI, as_degrees=True) == 0.0

    def test_land_s_end_to_john_o_groats(self, geo):
        # Movable Type Scripts: initial bearing 009° 07′ 11″.
        lands_end = (50 + 3 / 60 + 59 / 3600, -(5 + 42 / 60 + 53 / 3600))
        john_o_groats = (58 + 38 / 60 + 38 / 3600, -(3 + 4 / 60 + 12 / 3600))
        expected = 9 + 7 / 60 + 11 / 3600
        assert geo.bearing(lands_end, john_o_groats, as_degrees=True) == pytest.approx(expected, abs=0.01)

    def test_across_antimeridian_heads_east(self, geo):
        assert geo.bearing((0, 179.5), (0, -179.5), as_degrees=True) == pytest.approx(90.0, abs=1e-9)
        assert geo.bearing((0, -179.5), (0, 179.5)) == "West"

    def test_from_and_to_the_pole(self, geo):
        assert geo.bearing((90, 0), (0, 0)) == "South"
        assert geo.bearing((10, 10), (90, 0), as_degrees=True) == 0.0

    def test_degrees_always_in_0_360(self, geo):
        targets = [(lat, lng) for lat in (-60, -1, 0, 1, 60) for lng in (-179, -90, -1, 0, 1, 90, 179)]
        for target in targets:
            b = geo.bearing((0.5, 0.5), target, as_degrees=True)
            assert 0.0 <= b < 360.0

    def test_degrees_just_west_of_north_round_to_zero_not_360(self, geo):
        # The raw bearing is 359.999…°; rounding must wrap to 0.0, not give 360.0.
        assert geo.bearing((0, 0), (1, -0.00001), as_degrees=True) == 0.0

    @pytest.mark.parametrize(
        "raw, expected",
        [
            (11.25, "North-Northeast"),
            (33.75, "Northeast"),
            (56.25, "East-Northeast"),    # round-half-even would give Northeast
            (101.25, "East-Southeast"),   # round-half-even would give East
            (348.75, "North"),
            (348.7499, "North-Northwest"),
        ],
    )
    def test_sector_boundaries_round_up(self, geo, monkeypatch, raw, expected):
        monkeypatch.setattr(engine_mod, "_initial_bearing_deg", lambda a, b: raw)
        assert geo.bearing(DELHI, MUMBAI) == expected

    @pytest.mark.parametrize("raw, expected", [(359.996, 0.0), (359.994, 359.99), (0.004, 0.0)])
    def test_degrees_rounding_wraps(self, geo, monkeypatch, raw, expected):
        monkeypatch.setattr(engine_mod, "_initial_bearing_deg", lambda a, b: raw)
        assert geo.bearing(DELHI, MUMBAI, as_degrees=True) == expected


class TestBearingToCompass:
    @pytest.mark.parametrize("index", range(16))
    def test_sector_centres(self, index):
        assert _bearing_to_compass(index * 22.5) == COMPASS_16[index]

    @pytest.mark.parametrize("index", range(16))
    def test_boundaries_round_up_to_next_point(self, index):
        boundary = index * 22.5 + 11.25
        assert _bearing_to_compass(boundary) == COMPASS_16[(index + 1) % 16]

    @pytest.mark.parametrize("index", range(16))
    def test_just_below_boundary_stays(self, index):
        assert _bearing_to_compass(index * 22.5 + 11.2499) == COMPASS_16[index]

    @pytest.mark.parametrize(
        "degrees, expected",
        [(360.0, "North"), (720.0, "North"), (-90.0, "West"), (-11.25, "North"),
         (382.5, "North-Northeast"), (359.99, "North")],
    )
    def test_out_of_range_input_is_normalised(self, degrees, expected):
        assert _bearing_to_compass(degrees) == expected


# ─────────────────────────────────────────────────────────────────────────────
# Destination
# ─────────────────────────────────────────────────────────────────────────────

class TestDestination:
    @pytest.mark.parametrize(
        "origin, km, bearing, expected, tol",
        [
            (DELHI, 100, 90, (28.610049829645867, 78.2334132335709), 1e-6),
            (DELHI, 100, 0, (29.51322036372454, 77.2090), 1e-6),
            # haversine (PyPI) README: Paris, 32 km due west.
            ((48.8567, 2.3508), 32, 270, (48.85587279023947, 1.9134085092836945), 1e-6),
            # Movable Type destinationPoint example (computed there with R = 6371 km).
            ((51.47788, -0.00147), 7.794, 300.7, (51.5136, -0.0983), 1e-4),
        ],
        ids=["delhi-east", "delhi-north", "paris-west", "movable-type"],
    )
    def test_known_vectors(self, geo, origin, km, bearing, expected, tol):
        lat, lng = geo.destination(origin, km, bearing)
        assert lat == pytest.approx(expected[0], abs=tol)
        assert lng == pytest.approx(expected[1], abs=tol)

    def test_documented_example(self, geo):
        assert geo.destination(DELHI, 10, 90) == pytest.approx((28.613861, 77.311444), abs=1e-6)

    def test_returns_tuple_rounded_to_6_decimals(self, geo):
        result = geo.destination(DELHI, 123.456, 37.5)
        assert isinstance(result, tuple) and len(result) == 2
        assert all(round(v, 6) == v for v in result)

    def test_zero_distance_returns_origin(self, geo):
        assert geo.destination(DELHI, 0, 123) == pytest.approx(DELHI, abs=1e-9)

    @pytest.mark.parametrize("bearing", [0, 45, 90, 135, 200, 271.5, 359])
    def test_round_trip_with_distance_and_bearing(self, geo, bearing):
        dest = geo.destination(DELHI, 250, bearing)
        assert geo.distance(DELHI, dest) == pytest.approx(250, abs=1e-3)
        initial = geo.bearing(DELHI, dest, as_degrees=True)
        diff = (initial - bearing + 180) % 360 - 180
        assert abs(diff) <= 0.01

    def test_bearing_is_taken_modulo_360(self, geo):
        east = geo.destination((10, 10), 50, 90)
        assert geo.destination((10, 10), 50, 450) == pytest.approx(east, abs=2e-6)
        assert geo.destination((10, 10), 50, -270) == pytest.approx(east, abs=2e-6)

    def test_wraps_across_antimeridian(self, geo):
        lat, lng = geo.destination((0, 179.5), 111.19508, 90)
        assert lat == pytest.approx(0.0, abs=1e-6)
        assert lng == pytest.approx(-179.5, abs=1e-6)

    def test_long_trip_longitude_is_normalised(self, geo):
        lat, lng = geo.destination((0, 170), 5000, 90)
        assert -180.0 <= lng <= 180.0
        assert lng == pytest.approx(170 + 5000 / ONE_DEGREE_KM - 360, abs=1e-5)

    def test_over_the_north_pole(self, geo):
        lat, lng = geo.destination((89.9, 0), 50, 0)
        # 0.1° to reach the pole, the remaining arc is travelled south on the far side.
        assert lat == pytest.approx(90.0 - (50 / ONE_DEGREE_KM - 0.1), abs=1e-5)
        assert abs(lng) == pytest.approx(180.0, abs=1e-6)

    @pytest.mark.parametrize(
        "unit, distance",
        [("km", 1.609344), ("m", 1609.344), ("miles", 1), ("mi", 1), ("ft", 5280),
         ("yd", 1760), ("nmi", 1.609344 / 1.852)],
    )
    def test_units(self, geo, unit, distance):
        expected = geo.destination((0, 0), 1, 90, unit="miles")
        assert geo.destination((0, 0), distance, 90, unit=unit) == pytest.approx(expected, abs=2e-6)

    @pytest.mark.parametrize(
        "distance, bearing, name",
        [
            (True, 90, "distance"),
            (math.nan, 90, "distance"),
            (math.inf, 90, "distance"),
            (None, 90, "distance"),
            ("far", 90, "distance"),
            (10, False, "bearing"),
            (10, math.nan, "bearing"),
            (10, -math.inf, "bearing"),
            (10, None, "bearing"),
            (10, "east", "bearing"),
        ],
    )
    def test_invalid_distance_or_bearing(self, geo, distance, bearing, name):
        with pytest.raises(InvalidArgumentError) as exc_info:
            geo.destination(DELHI, distance, bearing)
        assert exc_info.value.name == name

    def test_invalid_unit(self, geo):
        with pytest.raises(InvalidUnitError):
            geo.destination(DELHI, 10, 90, unit="parsec")


# ─────────────────────────────────────────────────────────────────────────────
# Midpoint
# ─────────────────────────────────────────────────────────────────────────────

class TestMidpoint:
    def test_movable_type_example(self, geo):
        lat, lng = geo.midpoint((52.205, 0.119), (48.857, 2.351))
        assert lat == pytest.approx(50.5363, abs=1e-4)
        assert lng == pytest.approx(1.2746, abs=1e-4)

    def test_delhi_mumbai(self, geo):
        assert geo.midpoint(DELHI, MUMBAI) == pytest.approx(
            (23.860070139587833, 74.96345639661524), abs=1e-6
        )

    def test_returns_rounded_tuple(self, geo):
        result = geo.midpoint(DELHI, SYDNEY)
        assert isinstance(result, tuple) and len(result) == 2
        assert all(round(v, 6) == v for v in result)

    def test_identical_points(self, geo):
        assert geo.midpoint(DELHI, DELHI) == pytest.approx(DELHI, abs=1e-9)

    def test_along_equator_and_meridian(self, geo):
        assert geo.midpoint((0, 0), (0, 90)) == pytest.approx((0.0, 45.0), abs=1e-9)
        assert geo.midpoint((0, 0), (90, 0)) == pytest.approx((45.0, 0.0), abs=1e-9)

    @pytest.mark.parametrize("a, b", [((0, 179), (0, -179)), ((0, -179), (0, 179)), ((10, 175), (-10, -175))])
    def test_across_antimeridian(self, geo, a, b):
        lat, lng = geo.midpoint(a, b)
        assert lat == pytest.approx(0.0, abs=1e-6)
        assert abs(lng) == pytest.approx(180.0, abs=1e-6)

    @pytest.mark.parametrize("a, b", [(DELHI, MUMBAI), (DELHI, SYDNEY), ((60, -150), (-20, 100))])
    def test_equidistant_from_both_ends(self, geo, a, b):
        m = geo.midpoint(a, b)
        half = geo.distance(a, b) / 2
        assert geo.distance(a, m) == pytest.approx(half, abs=1e-3)
        assert geo.distance(m, b) == pytest.approx(half, abs=1e-3)

    def test_symmetric(self, geo):
        assert geo.midpoint(DELHI, SYDNEY) == pytest.approx(geo.midpoint(SYDNEY, DELHI), abs=2e-6)


# ─────────────────────────────────────────────────────────────────────────────
# Buffer
# ─────────────────────────────────────────────────────────────────────────────

class TestBuffer:
    def test_structure(self, geo):
        zone = geo.buffer(DELHI, 10)
        assert zone["type"] == "Polygon"
        assert len(zone["coordinates"]) == 1
        ring = zone["coordinates"][0]
        assert len(ring) == 65            # default num_points=64, plus closing vertex
        assert ring[0] == ring[-1]
        assert all(len(pos) == 2 for pos in ring)

    @pytest.mark.parametrize("num_points", [3, 4, 8, 100])
    def test_num_points_controls_resolution(self, geo, num_points):
        ring = geo.buffer(DELHI, 10, num_points=num_points)["coordinates"][0]
        assert len(ring) == num_points + 1
        assert ring[0] == ring[-1]
        assert len({tuple(p) for p in ring}) == num_points

    def test_vertices_lie_on_the_circle(self, geo):
        zone = geo.buffer(DELHI, 25)
        for lng, lat in zone["coordinates"][0]:
            assert _haversine_km((lat, lng), DELHI) == pytest.approx(25, rel=1e-9)

    @pytest.mark.parametrize(
        "center", [DELHI, SYDNEY, (0.0, 179.9), (60.0, -100.0), (-45.0, -179.95), (0.0, 0.0)]
    )
    @pytest.mark.parametrize("num_points", [3, 64])
    def test_ring_is_counter_clockwise(self, geo, center, num_points):
        # RFC 7946 §3.1.6: exterior rings follow the right-hand rule (CCW).
        ring = geo.buffer(center, 30, num_points=num_points)["coordinates"][0]
        assert _signed_planar_area(ring) > 0

    def test_ring_starts_north_then_heads_west(self, geo):
        ring = geo.buffer(DELHI, 10)["coordinates"][0]
        assert ring[0][0] == pytest.approx(DELHI[1], abs=1e-9)
        assert ring[0][1] > DELHI[0]
        assert ring[1][0] < DELHI[1]

    def test_metadata(self, geo):
        zone = geo.buffer(DELHI, 10)
        assert zone["_center"] == DELHI
        assert isinstance(zone["_center"], tuple)
        assert zone["_radius_km"] == 10.0

    def test_center_coerced_from_strings_and_geojson(self, geo):
        assert geo.buffer(("28.6139", "77.2090"), 5)["_center"] == DELHI
        point = {"type": "Point", "coordinates": [77.2090, 28.6139]}
        assert geo.buffer(point, 5)["_center"] == DELHI
        assert geo.buffer(_feature(point), 5)["_center"] == DELHI

    @pytest.mark.parametrize(
        "unit, radius, expected_km",
        [
            ("km", 10, 10.0),
            ("m", 1500, 1.5),
            ("miles", 2, 3.218688),
            ("mi", 2, 3.218688),
            ("ft", 5280, 1.609344),
            ("feet", 1000, 0.3048),
            ("yd", 1760, 1.609344),
            ("nmi", 1, 1.852),
            ("kilometres", 3, 3.0),
        ],
    )
    def test_units(self, geo, unit, radius, expected_km):
        zone = geo.buffer(DELHI, radius, unit=unit)
        assert zone["_radius_km"] == pytest.approx(expected_km, rel=1e-12)
        lng, lat = zone["coordinates"][0][0]
        assert _haversine_km((lat, lng), DELHI) == pytest.approx(expected_km, rel=1e-9)

    def test_invalid_unit(self, geo):
        with pytest.raises(InvalidUnitError):
            geo.buffer(DELHI, 10, unit="leagues")

    def test_antimeridian_ring_is_continuous(self, geo):
        ring = geo.buffer((0.0, 179.9), 50)["coordinates"][0]
        assert max(p[0] for p in ring) > 180.0   # kept continuous rather than wrapped
        assert all(abs(ring[i][0] - ring[i - 1][0]) < 180.0 for i in range(1, len(ring)))

    def test_is_json_serialisable(self, geo):
        zone = geo.buffer(DELHI, 10)
        restored = json.loads(json.dumps(zone))
        assert restored["coordinates"] == zone["coordinates"]
        assert restored["_center"] == list(DELHI)

    def test_half_circumference_radius_is_allowed(self, geo):
        zone = geo.buffer((0, 0), HALF_CIRCUMFERENCE_KM)
        assert zone["_radius_km"] == pytest.approx(HALF_CIRCUMFERENCE_KM)

    @pytest.mark.parametrize(
        "radius, unit",
        [(20016, "km"), (HALF_CIRCUMFERENCE_KM * 1000 + 1, "m"), (12440, "miles"), (1e9, "km")],
    )
    def test_radius_capped_at_half_circumference(self, geo, radius, unit):
        with pytest.raises(InvalidArgumentError) as exc_info:
            geo.buffer((0, 0), radius, unit=unit)
        assert exc_info.value.name == "radius"

    @pytest.mark.parametrize("pole", [(90.0, 0.0), (-90.0, 0.0)])
    def test_pole_centred_ring_is_not_degenerate(self, geo, pole):
        ring = geo.buffer(pole, 100, num_points=8)["coordinates"][0]
        assert len({tuple(p) for p in ring}) == 8


class TestBufferValidation:
    @pytest.mark.parametrize(
        "radius",
        [0, 0.0, -1, -0.5, math.nan, math.inf, -math.inf, True, False, None, "ten", [], {}],
        ids=lambda r: repr(r),
    )
    def test_invalid_radius(self, geo, radius):
        with pytest.raises(InvalidRadiusError) as exc_info:
            geo.buffer(DELHI, radius)
        assert exc_info.value.radius is radius

    def test_invalid_radius_is_value_error(self, geo):
        with pytest.raises(ValueError):
            geo.buffer(DELHI, -5)

    @pytest.mark.parametrize("num_points", [2, 1, 0, -5, True, 3.0, 64.5, "64", None])
    def test_invalid_num_points(self, geo, num_points):
        with pytest.raises(InvalidArgumentError) as exc_info:
            geo.buffer(DELHI, 10, num_points=num_points)
        assert exc_info.value.name == "num_points"


# ─────────────────────────────────────────────────────────────────────────────
# Point-in-polygon
# ─────────────────────────────────────────────────────────────────────────────

class TestPointInBuffer:
    def test_documented_example(self, geo):
        zone = geo.buffer((28.61, 77.20), 10)
        assert geo.point_in_polygon((28.65, 77.22), zone) is True
        assert geo.point_in_polygon((19.07, 72.87), zone) is False

    def test_centre_is_inside(self, geo):
        assert geo.point_in_polygon(SYDNEY, geo.buffer(SYDNEY, 0.001)) is True

    @pytest.mark.parametrize("bearing", [0, 37, 90, 181, 300])
    def test_exact_circle_edge(self, geo, bearing):
        zone = geo.buffer(DELHI, 10)
        assert geo.point_in_polygon(geo.destination(DELHI, 9.99, bearing), zone) is True
        assert geo.point_in_polygon(geo.destination(DELHI, 10.01, bearing), zone) is False

    def test_buffer_wrapped_in_feature(self, geo):
        zone = _feature(geo.buffer(DELHI, 10))
        assert geo.point_in_polygon((28.65, 77.22), zone) is True
        assert geo.point_in_polygon(MUMBAI, zone) is False

    def test_buffer_after_json_round_trip(self, geo):
        zone = json.loads(json.dumps(geo.buffer((89.5, 0), 200)))
        assert geo.point_in_polygon((90, 0), zone) is True
        assert geo.point_in_polygon((87, 0), zone) is False

    def test_plain_polygon_copy_of_buffer(self, geo):
        zone = _strip_private(geo.buffer(DELHI, 10))
        assert geo.point_in_polygon(DELHI, zone) is True
        assert geo.point_in_polygon((28.65, 77.22), zone) is True
        assert geo.point_in_polygon(MUMBAI, zone) is False

    @pytest.mark.parametrize(
        "point, inside",
        [((0, -179.9), True), ((0, 179.5), True), ((0.3, 179.9), True),
         ((0, -179.0), False), ((0, 0), False), ((0, 179.0), False)],
    )
    @pytest.mark.parametrize("strip", [False, True], ids=["buffer", "plain-polygon"])
    def test_antimeridian_buffer(self, geo, point, inside, strip):
        zone = geo.buffer((0.0, 179.9), 50)
        if strip:
            zone = _strip_private(zone)
        assert geo.point_in_polygon(point, zone) is inside

    @pytest.mark.parametrize(
        "center, radius, point, inside",
        [
            ((89.5, 0), 200, (90, 0), True),
            ((89.5, 0), 200, (89, 180), True),
            ((89.5, 0), 200, (89.9, -120), True),
            ((89.5, 0), 200, (87, 0), False),
            ((90, 0), 100, (89.5, 123), True),
            ((90, 0), 100, (89, 0), False),
            ((-90, 0), 100, (-89.5, -45), True),
            ((-90, 0), 100, (-88, 170), False),
        ],
    )
    def test_polar_buffers(self, geo, center, radius, point, inside):
        assert geo.point_in_polygon(point, geo.buffer(center, radius)) is inside


class TestPointInPolygon:
    @pytest.mark.parametrize(
        "point, inside",
        [((2, 2), True), ((8, 9), True), ((5, 5), False), ((4.5, 5.5), False),
         ((5, 12), False), ((-1, 5), False)],
    )
    def test_polygon_with_hole(self, geo, point, inside):
        assert geo.point_in_polygon(point, SQUARE_WITH_HOLE) is inside

    @pytest.mark.parametrize("point, inside", [((0.5, 0.5), True), ((5.5, 5.5), True), ((3, 3), False)])
    def test_multipolygon(self, geo, point, inside):
        assert geo.point_in_polygon(point, TWO_SQUARES) is inside

    def test_multipolygon_with_hole(self, geo):
        far_square = [[[20, 20], [21, 20], [21, 21], [20, 21], [20, 20]]]
        mp = {"type": "MultiPolygon", "coordinates": [SQUARE_WITH_HOLE["coordinates"], far_square]}
        assert geo.point_in_polygon((2, 2), mp) is True
        assert geo.point_in_polygon((20.5, 20.5), mp) is True
        assert geo.point_in_polygon((5, 5), mp) is False   # hole of the first part

    def test_feature_wrapping_polygon_and_multipolygon(self, geo):
        assert geo.point_in_polygon((2, 2), _feature(SQUARE_WITH_HOLE)) is True
        assert geo.point_in_polygon((5, 5), _feature(SQUARE_WITH_HOLE)) is False
        assert geo.point_in_polygon((5.5, 5.5), _feature(TWO_SQUARES)) is True

    def test_unclosed_ring_is_accepted(self, geo):
        triangle = {"type": "Polygon", "coordinates": [[[0, 0], [4, 0], [0, 4]]]}
        assert geo.point_in_polygon((1, 1), triangle) is True
        assert geo.point_in_polygon((3, 3), triangle) is False

    def test_accepts_geojson_point_argument(self, geo):
        point = {"type": "Point", "coordinates": [0.5, 0.5]}
        assert geo.point_in_polygon(point, UNIT_SQUARE) is True

    @pytest.mark.parametrize(
        "point, inside",
        [((0, 180), True), ((0, -180), True), ((0, 175), True), ((0, -175), True),
         ((5, 179.9), True), ((0, 0), False), ((0, 160), False), ((0, -160), False),
         ((20, 180), False)],
    )
    def test_antimeridian_polygon(self, geo, point, inside):
        assert geo.point_in_polygon(point, ANTIMERIDIAN_BOX) is inside

    @pytest.mark.parametrize(
        "point, inside",
        [((0, 0), True), ((0, 160), True), ((0, -160), True),
         ((0, 175), False), ((0, -175), False), ((0, 180), False)],
    )
    def test_jump_ring_uses_planar_reading(self, geo, point, inside):
        assert geo.point_in_polygon(point, JUMP_BOX) is inside

    def test_world_box_contains_everything(self, geo):
        world = {"type": "Polygon",
                 "coordinates": [[[-180, -90], [180, -90], [180, 90], [-180, 90], [-180, -90]]]}
        for point in [(0, 0), (45, 100), (-30, -60), (10, 179.9), (10, -179.9)]:
            assert geo.point_in_polygon(point, world) is True

    @pytest.mark.parametrize(
        "point, inside",
        [((-18, 179.5), True), ((-18, -179), True), ((-18, 180), True), ((-18, -180), True),
         ((-18, 0), False), ((-21, 179), False), ((-18, 176), False), ((-18, -177), False)],
    )
    def test_fiji_split_at_antimeridian(self, geo, point, inside):
        assert geo.point_in_polygon(point, FIJI_SPLIT) is inside

    def test_docstring_describes_the_antimeridian_rule(self):
        # A ring that jumps from 177 to -178 is read planarly (see JUMP_BOX), so
        # the docstring must not promise that crossing polygons "are handled".
        doc = " ".join((GeoEngine.point_in_polygon.__doc__ or "").split())
        assert "are handled" not in doc
        assert "continuous longitudes" in doc
        assert "MultiPolygon at ±180°" in doc

    def test_many_points_against_a_large_polygon(self, geo):
        # filter_by_polygon prepares the polygon once; answers match the
        # one-point-at-a-time test, holes and all.
        n = 360
        outer = [[10 * math.cos(2 * math.pi * k / n) * (1 + 0.3 * math.sin(9 * 2 * math.pi * k / n)),
                  10 * math.sin(2 * math.pi * k / n)] for k in range(n)]
        hole = [[-2, -2], [2, -2], [2, 2], [-2, 2], [-2, -2]]
        star = {"type": "Polygon", "coordinates": [outer + [outer[0]], hole]}
        g = GeoEngine(verbose=False)
        points = [(lat / 2, lng / 2) for lat in range(-30, 31, 2) for lng in range(-30, 31, 2)]
        for lat, lng in points:
            g.add_point(lat, lng)
        kept = {(f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0])
                for f in g.filter_by_polygon(star)}
        expected = {p for p in points if geo.point_in_polygon(p, star)}
        assert kept == expected
        assert 0 < len(expected) < len(points)
        assert (0.0, 0.0) not in expected and (3.0, 0.0) in expected


class TestPointInPolygonErrors:
    @pytest.mark.parametrize(
        "polygon",
        [
            {},
            {"type": "Point", "coordinates": [0, 0]},
            {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            {"type": "Polygon"},
            {"type": "Polygon", "coordinates": None},
            {"type": "Polygon", "coordinates": []},
            {"type": "Polygon", "coordinates": [[[0, 0], [1, 1]]]},
            {"type": "Polygon", "coordinates": [[[0, 0], [1, "x"], [2, 2]]]},
            {"type": "Polygon", "coordinates": [[[0], [1, 1], [2, 2]]]},
            {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [None, 1], [0, 0]]]},
            {"type": "Polygon", "coordinates": ["abc"]},
            {"type": "Polygon", "coordinates": [UNIT_SQUARE["coordinates"][0], "hole"]},
            {"type": "MultiPolygon", "coordinates": "x"},
            {"type": "MultiPolygon", "coordinates": [[]]},
            {"type": "Feature", "geometry": None, "properties": {}},
            _feature({"type": "LineString", "coordinates": [[0, 0], [1, 1]]}),
        ],
        ids=[
            "empty-dict", "point", "linestring", "no-coordinates", "null-coordinates",
            "no-rings", "short-ring", "non-numeric", "short-position", "null-position",
            "string-ring", "string-hole", "multipolygon-string", "multipolygon-empty-part",
            "null-feature", "linestring-feature",
        ],
    )
    def test_malformed_polygon(self, geo, polygon):
        with pytest.raises(InvalidBufferError) as exc_info:
            geo.point_in_polygon((0, 0), polygon)
        assert exc_info.value.reason

    @pytest.mark.parametrize("polygon", [None, [[0, 0], [1, 0], [1, 1]], "POLYGON((0 0))", (1, 2), 5])
    def test_non_dict_polygon(self, geo, polygon):
        with pytest.raises(InvalidBufferError, match="expected a dict"):
            geo.point_in_polygon((0, 0), polygon)

    def test_error_is_value_error(self, geo):
        with pytest.raises(ValueError):
            geo.point_in_polygon((0, 0), {"type": "Point", "coordinates": [0, 0]})

    def test_invalid_metadata_falls_back_to_ring_test(self, geo):
        zone = geo.buffer((0, 0), 10)
        zone["_radius_km"] = 0           # not a usable circle any more
        assert geo.point_in_polygon((0, 0), zone) is True
        assert geo.point_in_polygon((0, 0.2), zone) is False


# ─────────────────────────────────────────────────────────────────────────────
# Area
# ─────────────────────────────────────────────────────────────────────────────

class TestArea:
    def test_one_degree_cell_at_equator(self, geo):
        # Closed form R²·Δλ·(sin φ2 − sin φ1) with R = 6371.0088 km.
        assert geo.area(UNIT_SQUARE) == pytest.approx(12363.718145, abs=1e-3)

    def test_one_degree_cell_near_delhi(self, geo):
        cell = {"type": "Polygon", "coordinates": [[[77, 28], [78, 28], [78, 29], [77, 29], [77, 28]]]}
        assert geo.area(cell) == pytest.approx(10865.860821, abs=1e-3)

    def test_holes_are_subtracted(self, geo):
        assert geo.area(SQUARE_WITH_HOLE) == pytest.approx(1180900.133710, abs=1e-3)
        outer_only = {"type": "Polygon", "coordinates": [SQUARE_WITH_HOLE["coordinates"][0]]}
        assert geo.area(outer_only) > geo.area(SQUARE_WITH_HOLE)

    def test_hole_larger_than_outer_never_negative(self, geo):
        poly = {"type": "Polygon", "coordinates": [
            UNIT_SQUARE["coordinates"][0],
            [[-1, -1], [2, -1], [2, 2], [-1, 2], [-1, -1]],
        ]}
        assert geo.area(poly) == 0.0

    def test_multipolygon_is_sum_of_parts(self, geo):
        parts = [{"type": "Polygon", "coordinates": c} for c in TWO_SQUARES["coordinates"]]
        assert geo.area(TWO_SQUARES) == pytest.approx(sum(geo.area(p) for p in parts), abs=2e-4)

    def test_feature(self, geo):
        assert geo.area(_feature(SQUARE_WITH_HOLE)) == geo.area(SQUARE_WITH_HOLE)

    def test_one_km_buffer_is_about_pi(self, geo):
        assert geo.area(geo.buffer((28.61, 77.20), radius=1)) == pytest.approx(math.pi, rel=2e-3)
        smooth = geo.buffer((28.61, 77.20), radius=1, num_points=1024)
        assert geo.area(smooth) == pytest.approx(math.pi, abs=1e-3)

    def test_ten_km_buffer_is_exact_spherical_cap(self, geo):
        # Buffers are measured as the circle they represent, 2πR²(1 − cos(r/R)),
        # consistent with point_in_polygon's exact circle test.
        r = 10 / EARTH_RADIUS_KM
        cap = 2 * math.pi * EARTH_RADIUS_KM ** 2 * (1 - math.cos(r))
        assert geo.area(geo.buffer(DELHI, 10)) == pytest.approx(cap, abs=1e-3)
        assert geo.area(geo.buffer(DELHI, 10)) == pytest.approx(314.1592, abs=1e-3)

    def test_plain_polygon_copy_of_buffer_uses_ring_area(self, geo):
        # Without the buffer metadata only the 64-gon ring is known.
        ring = {"type": "Polygon", "coordinates": geo.buffer(DELHI, 10)["coordinates"]}
        assert geo.area(ring) == pytest.approx(313.655, abs=1e-3)

    def test_area_independent_of_winding(self, geo):
        clockwise = {"type": "Polygon", "coordinates": [list(reversed(UNIT_SQUARE["coordinates"][0]))]}
        assert geo.area(clockwise) == pytest.approx(geo.area(UNIT_SQUARE), abs=1e-4)

    def test_area_independent_of_ring_closure(self, geo):
        unclosed = {"type": "Polygon", "coordinates": [UNIT_SQUARE["coordinates"][0][:-1]]}
        assert geo.area(unclosed) == geo.area(UNIT_SQUARE)

    def test_antimeridian_polygon_matches_prime_meridian_copy(self, geo):
        prime = {"type": "Polygon",
                 "coordinates": [[[-10, -10], [10, -10], [10, 10], [-10, 10], [-10, -10]]]}
        assert geo.area(ANTIMERIDIAN_BOX) == pytest.approx(geo.area(prime), abs=1e-3)
        assert geo.area(ANTIMERIDIAN_BOX) > 4_000_000

    def test_antimeridian_buffer_matches_equator_buffer(self, geo):
        assert geo.area(geo.buffer((0, 179.9), 50)) == pytest.approx(
            geo.area(geo.buffer((0, 0), 50)), abs=1e-3
        )

    def test_movable_type_triangle(self, geo):
        # Movable Type areaOf example (lat/lng (0,0), (1,0), (0,1)): 6.18e9 m².
        tri = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 0], [0, 0]]]}
        assert geo.area(tri, unit="m2") == pytest.approx(6.18e9, rel=1e-3)

    @pytest.mark.parametrize(
        "unit, m2_per_unit",
        [
            ("km2", 1e6), ("m2", 1.0), ("ha", 1e4), ("acres", 4046.8564224),
            ("mi2", 2589988.110336), ("ft2", 0.09290304),
            ("sqkm", 1e6), ("sq_km", 1e6), ("sqm", 1.0), ("sq_m", 1.0),
            ("hectare", 1e4), ("hectares", 1e4), ("acre", 4046.8564224),
            ("sqmi", 2589988.110336), ("sq_mi", 2589988.110336),
            ("sqft", 0.09290304), ("sq_ft", 0.09290304), (" KM2 ", 1e6),
        ],
    )
    def test_units(self, geo, unit, m2_per_unit):
        m2 = _ring_area_m2(UNIT_SQUARE["coordinates"][0])
        assert geo.area(UNIT_SQUARE, unit=unit) == pytest.approx(m2 / m2_per_unit, rel=1e-9, abs=1e-4)

    @pytest.mark.parametrize("unit", ["km", "m", "square-furlongs", "", None, 2])
    def test_invalid_unit(self, geo, unit):
        with pytest.raises(InvalidUnitError):
            geo.area(UNIT_SQUARE, unit=unit)

    @pytest.mark.parametrize(
        "polygon",
        [None, {}, {"type": "Point", "coordinates": [0, 0]}, {"type": "Polygon", "coordinates": []},
         {"type": "Polygon", "coordinates": [[[0, 0], [1, 1]]]}],
    )
    def test_malformed_polygon(self, geo, polygon):
        with pytest.raises(InvalidBufferError):
            geo.area(polygon)

    @pytest.mark.parametrize(
        "ring",
        [
            [[1e308, 0], [0, 1e308], [-1e308, 0], [1e308, 0]],      # was area=inf
            [[500, 0], [0, 700], [-900, 0], [500, 0]],              # was 169,606,894 km²
            [[0, 0], [1, 0], [1, 91], [0, 0]],                      # latitude beyond the pole
            [[0, 0], [361, 0], [361, 1], [0, 0]],                   # longitude beyond ±360
            [[0, 0], [1, 0], [1, math.nan], [0, 0]],
            [[0, 0], [1, 0], [1, math.inf], [0, 0]],
            [[0, 0], [10 ** 400, 0], [1, 1], [0, 0]],               # too large for a float
        ],
        ids=["huge", "lat-700", "lat-91", "lng-361", "nan", "inf", "int-overflow"],
    )
    def test_out_of_range_positions_rejected(self, geo, ring):
        polygon = {"type": "Polygon", "coordinates": [ring]}
        with pytest.raises(InvalidBufferError) as info:
            geo.area(polygon)
        assert info.value.reason
        with pytest.raises(InvalidBufferError):
            geo.area({"type": "MultiPolygon", "coordinates": [[UNIT_SQUARE["coordinates"][0]], [ring]]})
        with pytest.raises(InvalidBufferError):
            geo.point_in_polygon((0.5, 0.5), polygon)   # same validation

    def test_out_of_range_error_names_the_position(self, geo):
        with pytest.raises(InvalidBufferError) as info:
            geo.area({"type": "Polygon", "coordinates": [[[500, 0], [0, 700], [-900, 0], [500, 0]]]})
        assert "[500.0, 0.0]" in info.value.reason
        assert "swapped" in info.value.reason

    def test_continuous_longitudes_still_accepted(self, geo):
        assert geo.area(ANTIMERIDIAN_BOX) > 0   # longitudes up to ±360 are fine

    def test_docstring_says_spherical_not_wgs84(self):
        doc = " ".join((GeoEngine.area.__doc__ or "").split())
        assert "sphere of mean Earth radius" in doc
        assert "not the WGS-84 ellipsoid" in doc

    def test_returns_float_rounded_to_4_decimals(self, geo):
        a = geo.area(geo.buffer(SYDNEY, 3.3))
        assert isinstance(a, float)
        assert a == round(a, 4)

    @pytest.mark.parametrize("center, radius_km", [((90, 0), 100), ((89.5, 0), 200), ((-89.9, 45), 300)])
    def test_polar_buffer_area(self, geo, center, radius_km):
        r = radius_km / EARTH_RADIUS_KM
        cap = 2 * math.pi * EARTH_RADIUS_KM ** 2 * (1 - math.cos(r))
        assert geo.area(geo.buffer(center, radius_km)) == pytest.approx(cap, rel=1e-6)


# ─────────────────────────────────────────────────────────────────────────────
# Length
# ─────────────────────────────────────────────────────────────────────────────

class TestLength:
    def test_two_degrees_along_equator(self, geo):
        path = [(0, 0), (0, 1), (0, 2)]
        assert geo.length(path) == pytest.approx(222.390160, abs=1e-3)
        assert geo.length(path, method="vincenty") == pytest.approx(222.638982, abs=1e-3)

    def test_delhi_agra_jaipur(self, geo):
        path = [DELHI, AGRA, JAIPUR]
        assert geo.length(path) == pytest.approx(399.952163, abs=1e-3)
        assert geo.length(path, method="vincenty") == pytest.approx(399.911532, abs=1e-3)

    def test_documented_example(self, geo):
        assert geo.length([DELHI, JAIPUR, MUMBAI]) == pytest.approx(1156.0342, abs=1e-4)

    def test_two_points_equals_distance(self, geo):
        assert geo.length([DELHI, MUMBAI]) == geo.distance(DELHI, MUMBAI)

    def test_linestring_uses_lng_lat_order(self, geo):
        line = {"type": "LineString", "coordinates": [[77.2090, 28.6139], [78.0081, 27.1767],
                                                      [75.7873, 26.9124]]}
        assert geo.length(line) == geo.length([DELHI, AGRA, JAIPUR])

    def test_feature_wrapping_linestring(self, geo):
        line = {"type": "LineString", "coordinates": [[0, 0], [1, 0], [2, 0]]}
        assert geo.length(_feature(line)) == geo.length(line) == pytest.approx(222.3902, abs=1e-4)

    def test_multilinestring_sums_parts_without_joining_them(self, geo):
        mls = {"type": "MultiLineString", "coordinates": [[[0, 0], [1, 0]], [[10, 10], [10, 11]]]}
        expected = geo.distance((0, 0), (0, 1)) + geo.distance((10, 10), (11, 10))
        assert geo.length(mls) == pytest.approx(expected, abs=2e-4)
        assert geo.length(_feature(mls)) == geo.length(mls)

    def test_units(self, geo):
        km = geo.length([DELHI, AGRA, JAIPUR])
        assert geo.length([DELHI, AGRA, JAIPUR], unit="m") == pytest.approx(km * 1000, abs=0.1)
        assert geo.length([DELHI, AGRA, JAIPUR], unit="nmi") == pytest.approx(km / 1.852, abs=1e-3)

    @pytest.mark.parametrize("path", [[], [DELHI], {"type": "LineString", "coordinates": []}])
    def test_degenerate_paths_have_zero_length(self, geo, path):
        assert geo.length(path) == 0.0

    def test_accepts_tuples_and_generators(self, geo):
        expected = geo.length([(0, 0), (0, 1)])
        assert geo.length(((0, 0), (0, 1))) == expected
        assert geo.length(p for p in [(0, 0), (0, 1)]) == expected

    @pytest.mark.parametrize(
        "line",
        [
            None, "abc", 5,
            {"type": "Point", "coordinates": [0, 0]},
            {"type": "Polygon", "coordinates": [UNIT_SQUARE["coordinates"][0]]},
            {"type": "LineString", "coordinates": [[0]]},
            {"type": "LineString", "coordinates": "xx"},
            {"type": "MultiLineString", "coordinates": [[[0, 0]], 5]},
            {"type": "Feature", "geometry": None, "properties": {}},
        ],
        ids=[
            "none", "string", "int", "point", "polygon", "short-position",
            "string-coordinates", "non-list-part", "null-feature",
        ],
    )
    def test_invalid_line(self, geo, line):
        with pytest.raises(InvalidArgumentError):
            geo.length(line)

    def test_out_of_range_positions(self, geo):
        with pytest.raises(InvalidCoordinateError):
            geo.length([(0, 0), (95, 0)])
        with pytest.raises(InvalidCoordinateError):
            geo.length({"type": "LineString", "coordinates": [[0, 95], [0, 0]]})

    def test_invalid_method_and_unit(self, geo):
        with pytest.raises(InvalidArgumentError):
            geo.length([DELHI, MUMBAI], method="manhattan")
        with pytest.raises(InvalidUnitError):
            geo.length([DELHI, MUMBAI], unit="km2")


# ─────────────────────────────────────────────────────────────────────────────
# Distance to line (cross-track)
# ─────────────────────────────────────────────────────────────────────────────

class TestDistanceToLine:
    SEGMENT = [(53.3206, -1.7297), (53.1887, 0.1334)]   # Movable Type example path

    def test_movable_type_cross_track(self, geo):
        # Movable Type crossTrackDistanceTo: -307.5 m (sign dropped here).
        assert geo.distance_to_line((53.2611, -0.7972), self.SEGMENT) == pytest.approx(0.3075, abs=1e-3)
        d_m = geo.distance_to_line((53.2611, -0.7972), self.SEGMENT, unit="m")
        assert d_m == pytest.approx(307.5, abs=0.5)

    def test_beyond_segment_end_uses_endpoint(self, geo):
        p = (53.0, 1.0)   # along-track 184.99 km > segment length 124.8 km
        d = geo.distance_to_line(p, self.SEGMENT)
        assert d == pytest.approx(61.551, abs=1e-3)
        assert d == geo.distance(p, self.SEGMENT[1])

    def test_before_segment_start_uses_endpoint(self, geo):
        p = (53.5, -2.5)
        assert geo.distance_to_line(p, self.SEGMENT) == geo.distance(p, self.SEGMENT[0])

    def test_point_on_line_is_zero(self, geo):
        assert geo.distance_to_line((0, 0.5), [(0, 0), (0, 1)]) == 0.0
        assert geo.distance_to_line((0, 0), [(0, 0), (0, 1)]) == 0.0

    def test_perpendicular_from_equator(self, geo):
        assert geo.distance_to_line((1, 0.5), [(0, 0), (0, 1)]) == pytest.approx(ONE_DEGREE_KM, abs=1e-4)

    def test_never_exceeds_nearest_vertex_distance(self, geo):
        line = [DELHI, AGRA, JAIPUR]
        for p in [(27.5, 77.0), (30.0, 76.0), (25.0, 80.0), MUMBAI]:
            d = geo.distance_to_line(p, line)
            assert d <= min(geo.distance(p, v) for v in line) + 1e-4

    def test_geojson_inputs(self, geo):
        p = (1, 0.5)
        line = {"type": "LineString", "coordinates": [[0, 0], [1, 0]]}
        mls = {"type": "MultiLineString", "coordinates": [[[0, 0], [1, 0]], [[0, 2], [1, 2]]]}
        assert geo.distance_to_line(p, line) == pytest.approx(ONE_DEGREE_KM, abs=1e-4)
        assert geo.distance_to_line(p, _feature(line)) == geo.distance_to_line(p, line)
        assert geo.distance_to_line(p, mls) == pytest.approx(ONE_DEGREE_KM, abs=1e-4)

    def test_multiline_picks_nearest_part(self, geo):
        near = [[0, 5], [1, 5]]
        mls = {"type": "MultiLineString", "coordinates": [[[0, 0], [1, 0]], near]}
        p = (4.5, 0.5)
        d = geo.distance_to_line(p, mls)
        assert d == geo.distance_to_line(p, {"type": "LineString", "coordinates": near})
        assert d == pytest.approx(ONE_DEGREE_KM / 2, abs=0.05)   # arc bulges slightly poleward

    def test_single_point_line(self, geo):
        assert geo.distance_to_line((1, 1), [(0, 0)]) == geo.distance((1, 1), (0, 0))

    def test_degenerate_segment(self, geo):
        assert geo.distance_to_line((1, 0.5), [(0, 0), (0, 0)]) == geo.distance((1, 0.5), (0, 0))

    @pytest.mark.parametrize(
        "line",
        [[], {"type": "LineString", "coordinates": []}, {"type": "MultiLineString", "coordinates": []}],
    )
    def test_empty_line(self, geo, line):
        with pytest.raises(InvalidArgumentError) as exc_info:
            geo.distance_to_line(DELHI, line)
        assert exc_info.value.name == "line"

    def test_invalid_unit(self, geo):
        with pytest.raises(InvalidUnitError):
            geo.distance_to_line(DELHI, [DELHI, MUMBAI], unit="cubits")


# ─────────────────────────────────────────────────────────────────────────────
# Geohash
# ─────────────────────────────────────────────────────────────────────────────

class TestGeohash:
    def test_wikipedia_vector(self, geo):
        assert geo.geohash((57.64911, 10.40744), precision=11) == "u4pruydqqvj"

    @pytest.mark.parametrize("point, expected", [(DELHI, "ttnfucjbh"), (MUMBAI, "te7ud2evv")])
    def test_default_precision_is_9(self, geo, point, expected):
        assert geo.geohash(point) == expected

    @pytest.mark.parametrize(
        "point, expected", [((90, 180), "zzzzz"), ((-90, -180), "00000"), ((0, 0), "s0000")]
    )
    def test_world_edges(self, geo, point, expected):
        assert geo.geohash(point, precision=5) == expected

    @pytest.mark.parametrize("precision", range(1, 13))
    def test_length_and_prefix_property(self, geo, precision):
        full = geo.geohash(SYDNEY, precision=12)
        h = geo.geohash(SYDNEY, precision=precision)
        assert len(h) == precision
        assert full.startswith(h)
        assert set(h) <= set("0123456789bcdefghjkmnpqrstuvwxyz")

    def test_accepts_geojson_point(self, geo):
        point = {"type": "Point", "coordinates": [10.40744, 57.64911]}
        assert geo.geohash(point, precision=11) == "u4pruydqqvj"

    @pytest.mark.parametrize("precision", [0, 13, -1, True, 1.5, "5", None])
    def test_invalid_precision(self, geo, precision):
        with pytest.raises(InvalidArgumentError) as exc_info:
            geo.geohash(DELHI, precision=precision)
        assert exc_info.value.name == "precision"


class TestGeohashDecode:
    def test_wikipedia_ezs42(self, geo):
        assert geo.geohash_decode("ezs42") == pytest.approx((42.60498046875, -5.60302734375), abs=1e-6)

    def test_ezs42_bounds_exact(self):
        assert _geohash_bounds("ezs42") == (42.5830078125, -5.625, 42.626953125, -5.5810546875)

    def test_documented_example(self, geo):
        assert geo.geohash_decode("u4pruydqqvj") == pytest.approx((57.649111, 10.40744), abs=1e-6)

    def test_returns_tuple_of_floats(self, geo):
        result = geo.geohash_decode("ttnfucjbh")
        assert isinstance(result, tuple) and len(result) == 2
        assert all(isinstance(v, float) for v in result)

    def test_case_and_whitespace_insensitive(self, geo):
        expected = geo.geohash_decode("u4pruydqqvj")
        assert geo.geohash_decode("U4PRUYDQQVJ") == expected
        assert geo.geohash_decode("  u4pruydqqvj\n") == expected

    @pytest.mark.parametrize("point", [DELHI, SYDNEY, (40.7128, -74.006), (0, 0), (-89.99, 179.99)])
    def test_round_trip(self, geo, point):
        assert geo.geohash_decode(geo.geohash(point, precision=12)) == pytest.approx(point, abs=1e-6)

    @pytest.mark.parametrize("precision", [1, 3, 5, 7, 9])
    def test_decoded_centre_is_in_the_same_cell(self, geo, precision):
        h = geo.geohash(MUMBAI, precision=precision)
        min_lat, min_lng, max_lat, max_lng = _geohash_bounds(h)
        assert min_lat <= MUMBAI[0] <= max_lat and min_lng <= MUMBAI[1] <= max_lng
        assert geo.geohash(geo.geohash_decode(h), precision=precision) == h

    @pytest.mark.parametrize("bad", ["abc!", "ai", "u4pl", "u4po", "u4pi", "u4p-r", "ü"])
    def test_invalid_characters(self, geo, bad):
        with pytest.raises(InvalidArgumentError, match="geohash characters") as exc_info:
            geo.geohash_decode(bad)
        assert exc_info.value.name == "geohash"

    @pytest.mark.parametrize("bad", ["", "   ", None, 5, b"u4pru", ["u4pru"]])
    def test_empty_or_non_string(self, geo, bad):
        with pytest.raises(InvalidArgumentError) as exc_info:
            geo.geohash_decode(bad)
        assert exc_info.value.name == "geohash"


# ─────────────────────────────────────────────────────────────────────────────
# utils helpers (white-box)
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeLng:
    @pytest.mark.parametrize(
        "lng, expected",
        [
            (0.0, 0.0), (179.5, 179.5), (180.0, 180.0), (-180.0, -180.0),
            (181.0, -179.0), (-181.0, 179.0), (190.0, -170.0), (360.0, 0.0),
            (540.0, 180.0), (-540.0, -180.0), (720.0, 0.0), (-359.0, 1.0),
        ],
    )
    def test_wraps_into_range(self, lng, expected):
        assert _normalize_lng(lng) == pytest.approx(expected, abs=1e-9)

    def test_in_range_values_returned_unchanged(self):
        for lng in (-180.0, -0.000001, 12.345678, 180.0):
            assert _normalize_lng(lng) == lng


class TestLngSpan:
    def test_rfc7946_fiji_example(self):
        assert _lng_span([177, 179, -179, -178]) == (177, -178)

    def test_simple_interval(self):
        assert _lng_span([10, 30, 20]) == (10, 30)

    def test_single_longitude(self):
        assert _lng_span([42.0]) == (42.0, 42.0)

    def test_wide_spread_without_crossing(self):
        assert _lng_span([-100, 0, 100]) == (-100, 100)

    def test_crossing_pair(self):
        assert _lng_span([-170, 170]) == (170, -170)

    def test_unnormalised_input(self):
        assert _lng_span([190.0, -200.0]) == (160.0, -170.0)


class TestSphericalMean:
    def test_antimeridian_safe(self):
        lat, lng = _spherical_mean([(0, 179), (0, -179)])
        assert lat == pytest.approx(0.0, abs=1e-9)
        assert abs(lng) == pytest.approx(180.0, abs=1e-9)

    def test_single_point_is_identity(self):
        assert _spherical_mean([(10.0, 20.0)]) == pytest.approx((10.0, 20.0), abs=1e-9)

    def test_quarter_circle(self):
        assert _spherical_mean([(0, 0), (0, 90)]) == pytest.approx((0.0, 45.0), abs=1e-9)

    def test_symmetric_about_equator(self):
        assert _spherical_mean([(30, 40), (-30, 40)]) == pytest.approx((0.0, 40.0), abs=1e-9)

    @pytest.mark.parametrize("points", [[(0, 0), (0, 180)], [(90, 0), (-90, 0)]])
    def test_cancelling_points_fall_back_without_error(self, points):
        lat, lng = _spherical_mean(points)
        assert math.isfinite(lat) and math.isfinite(lng)
        assert -90 <= lat <= 90 and -180 <= lng <= 180


class TestRingArea:
    def test_fewer_than_three_positions(self):
        assert _ring_area_m2([]) == 0.0
        assert _ring_area_m2([[0, 0], [1, 0]]) == 0.0

    def test_known_value(self):
        assert _ring_area_m2(UNIT_SQUARE["coordinates"][0]) == pytest.approx(12363718145.18, rel=1e-9)

    def test_closure_and_orientation_do_not_matter(self):
        closed = UNIT_SQUARE["coordinates"][0]
        expected = _ring_area_m2(closed)
        assert _ring_area_m2(closed[:-1]) == pytest.approx(expected, rel=1e-12)
        assert _ring_area_m2(list(reversed(closed))) == pytest.approx(expected, rel=1e-12)

    def test_scales_with_earth_radius_squared(self):
        # 1° × 1° cell at the equator: R² · Δλ · (sin 1° − sin 0°)
        expected = EARTH_RADIUS_M ** 2 * math.radians(1) * math.sin(math.radians(1))
        assert _ring_area_m2(UNIT_SQUARE["coordinates"][0]) == pytest.approx(expected, rel=1e-12)
