"""Geohashes, slippy tiles, quadkeys and coordinate reference systems."""

from __future__ import annotations

import pytest

from qoregeo import crs
from qoregeo import geohash as gh
from qoregeo.exceptions import InvalidCoordinateError
from qoregeo.utils import _haversine_km

from .conftest import DELHI, LONDON, MUMBAI, NYC, SYDNEY


class TestGeohashEncode:
    def test_known_london_hash(self):
        # 'gcpvj0d' is the published geohash for central London.
        assert gh.encode(51.5074, -0.1278, 7) == "gcpvj0d"

    def test_precision_controls_length(self):
        for precision in range(1, 13):
            assert len(gh.encode(*DELHI, precision)) == precision

    def test_precision_is_clamped(self):
        assert len(gh.encode(*DELHI, 0)) == 1
        assert len(gh.encode(*DELHI, 99)) == 12

    def test_prefixes_are_nested(self):
        long_hash = gh.encode(*DELHI, 9)
        for precision in range(1, 9):
            assert long_hash.startswith(gh.encode(*DELHI, precision))

    def test_nearby_points_share_a_prefix(self):
        a = gh.encode(28.6139, 77.2090, 6)
        b = gh.encode(28.6140, 77.2091, 6)
        assert a[:5] == b[:5]

    def test_distant_points_do_not(self):
        assert gh.encode(*DELHI, 3)[0] != gh.encode(*NYC, 3)[0]

    def test_rejects_bad_coordinates(self):
        with pytest.raises(InvalidCoordinateError):
            gh.encode(95.0, 0.0, 7)


class TestGeohashDecode:
    @pytest.mark.parametrize("point", [DELHI, MUMBAI, LONDON, NYC, SYDNEY, (0.0, 0.0)])
    def test_round_trip_is_accurate(self, point):
        decoded = gh.decode(gh.encode(*point, 12))
        assert _haversine_km(point, decoded) < 0.001

    def test_decode_exactly_reports_error(self):
        lat, lng, lat_err, lng_err = gh.decode_exactly(gh.encode(*DELHI, 6))
        assert abs(lat - DELHI[0]) <= lat_err
        assert abs(lng - DELHI[1]) <= lng_err

    def test_precision_narrows_the_box(self):
        coarse = gh.bbox(gh.encode(*DELHI, 3))
        fine = gh.bbox(gh.encode(*DELHI, 8))
        assert (fine["max_lat"] - fine["min_lat"]) < (coarse["max_lat"] - coarse["min_lat"])

    def test_bbox_contains_the_point(self):
        box = gh.bbox(gh.encode(*DELHI, 7))
        assert box["min_lat"] <= DELHI[0] <= box["max_lat"]
        assert box["min_lng"] <= DELHI[1] <= box["max_lng"]

    def test_is_case_insensitive(self):
        assert gh.decode("TTNFUCJ") == gh.decode("ttnfucj")

    def test_rejects_empty(self):
        with pytest.raises(InvalidCoordinateError):
            gh.bbox("")

    def test_rejects_invalid_characters(self):
        with pytest.raises(InvalidCoordinateError):
            gh.bbox("abcia")          # 'i' and 'a' are not in the base32 alphabet


class TestGeohashNeighbours:
    def test_eight_neighbours(self):
        assert len(gh.neighbours(gh.encode(*DELHI, 7))) == 8

    def test_neighbours_are_distinct(self):
        found = gh.neighbours(gh.encode(*DELHI, 7))
        assert len(set(found.values())) == 8

    def test_neighbours_exclude_self(self):
        cell = gh.encode(*DELHI, 7)
        assert cell not in gh.neighbours(cell).values()

    def test_neighbours_are_same_precision(self):
        cell = gh.encode(*DELHI, 6)
        assert all(len(n) == 6 for n in gh.neighbours(cell).values())

    def test_north_neighbour_is_north(self):
        cell = gh.encode(*DELHI, 6)
        assert gh.decode(gh.neighbours(cell)["n"])[0] > gh.decode(cell)[0]

    def test_us_spelling_alias(self):
        assert gh.neighbors is gh.neighbours

    def test_polar_cell_drops_impossible_neighbours(self):
        # There is nothing north of the north pole.
        assert "n" not in gh.neighbours(gh.encode(89.999, 0.0, 4))


class TestGeohashHelpers:
    def test_polygon_matches_bbox(self):
        cell = gh.encode(*DELHI, 6)
        ring = gh.geohash_polygon(cell)["coordinates"][0]
        box = gh.bbox(cell)
        assert min(p[0] for p in ring) == pytest.approx(box["min_lng"])
        assert max(p[1] for p in ring) == pytest.approx(box["max_lat"])

    def test_polygon_is_closed(self):
        ring = gh.geohash_polygon(gh.encode(*DELHI, 5))["coordinates"][0]
        assert ring[0] == ring[-1]

    def test_common_prefix(self):
        assert gh.common_prefix(["ttnfucj", "ttnfucm", "ttnfuby"]) == "ttnfu"

    def test_common_prefix_of_one(self):
        assert gh.common_prefix(["abc"]) == "abc"

    def test_common_prefix_of_none(self):
        assert gh.common_prefix([]) == ""

    def test_common_prefix_disjoint(self):
        assert gh.common_prefix(["abc", "xyz"]) == ""

    def test_precision_table_is_ordered(self):
        widths = [gh.PRECISION_METRES[p][0] for p in sorted(gh.PRECISION_METRES)]
        assert widths == sorted(widths, reverse=True)


class TestTilesAndQuadkeys:
    def test_tile_at_zoom_zero(self):
        assert gh.tile_of(*DELHI, 0) == (0, 0, 0)

    def test_tile_bounds_contain_the_point(self):
        x, y, z = gh.tile_of(*DELHI, 12)
        box = gh.tile_bounds(x, y, z)
        assert box["min_lat"] <= DELHI[0] <= box["max_lat"]
        assert box["min_lng"] <= DELHI[1] <= box["max_lng"]

    def test_tile_indices_stay_in_range(self):
        for zoom in (1, 5, 12, 18):
            x, y, z = gh.tile_of(85.0, 179.9, zoom)
            assert 0 <= x < 2 ** zoom
            assert 0 <= y < 2 ** zoom

    def test_extreme_latitude_is_clamped(self):
        x, y, z = gh.tile_of(89.9, 0.0, 5)
        assert y == 0

    @pytest.mark.parametrize("zoom", [1, 5, 10, 18])
    def test_quadkey_round_trip(self, zoom):
        tile = gh.tile_of(*DELHI, zoom)
        assert gh.quadkey_to_tile(gh.quadkey(*tile)) == tile

    def test_quadkey_length_equals_zoom(self):
        assert len(gh.quadkey(*gh.tile_of(*DELHI, 14))) == 14

    def test_quadkey_digits_are_valid(self):
        assert set(gh.quadkey(*gh.tile_of(*DELHI, 14))) <= set("0123")

    def test_quadkey_rejects_bad_digits(self):
        with pytest.raises(InvalidCoordinateError):
            gh.quadkey_to_tile("0129")


class TestWebMercator:
    def test_origin(self):
        assert crs.to_web_mercator(0, 0) == pytest.approx((0.0, 0.0))

    @pytest.mark.parametrize("point", [DELHI, MUMBAI, LONDON, NYC, SYDNEY])
    def test_round_trip(self, point):
        x, y = crs.to_web_mercator(*point)
        assert crs.from_web_mercator(x, y) == pytest.approx(point, abs=1e-7)

    def test_known_antipode_x(self):
        x, _ = crs.to_web_mercator(0, 180)
        assert x == pytest.approx(20_037_508.34, rel=1e-6)

    def test_latitude_is_clamped(self):
        _, y_pole = crs.to_web_mercator(89.999, 0)
        _, y_limit = crs.to_web_mercator(crs.MERCATOR_MAX_LAT, 0)
        assert y_pole == pytest.approx(y_limit)

    def test_northern_y_is_positive(self):
        assert crs.to_web_mercator(45, 0)[1] > 0
        assert crs.to_web_mercator(-45, 0)[1] < 0


class TestUTM:
    def test_delhi_zone(self):
        result = crs.to_utm(*DELHI)
        assert result["zone"] == 43
        assert result["hemisphere"] == "N"
        assert result["epsg"] == 32643

    def test_sydney_zone(self):
        result = crs.to_utm(*SYDNEY)
        assert result["zone"] == 56
        assert result["hemisphere"] == "S"
        assert result["epsg"] == 32756

    @pytest.mark.parametrize("point", [DELHI, MUMBAI, LONDON, NYC, SYDNEY, (0.0, 0.0)])
    def test_round_trip(self, point):
        utm = crs.to_utm(*point)
        back = crs.from_utm(utm["easting"], utm["northing"], utm["zone"], utm["hemisphere"])
        assert back == pytest.approx(point, abs=1e-6)

    def test_easting_is_near_the_false_origin(self):
        # Every UTM easting sits within ~500 km of the 500 000 m false origin.
        assert 100_000 < crs.to_utm(*DELHI)["easting"] < 900_000

    def test_southern_northing_uses_false_northing(self):
        assert crs.to_utm(*SYDNEY)["northing"] > 5_000_000

    def test_norway_exception(self):
        # Zone 32 is widened over south-west Norway.
        assert crs.utm_zone(60.0, 5.0)["zone"] == 32

    def test_svalbard_exceptions(self):
        assert crs.utm_zone(78.0, 5.0)["zone"] == 31
        assert crs.utm_zone(78.0, 15.0)["zone"] == 33
        assert crs.utm_zone(78.0, 25.0)["zone"] == 35
        assert crs.utm_zone(78.0, 35.0)["zone"] == 37

    def test_band_letter(self):
        assert crs.utm_zone(*DELHI)["band"] == "R"

    def test_rejects_polar_latitudes(self):
        # UTM is undefined beyond 84°N / 80°S; UPS covers those.
        with pytest.raises(InvalidCoordinateError):
            crs.to_utm(88.0, 0.0)

    def test_rejects_invalid_coordinates(self):
        with pytest.raises(InvalidCoordinateError):
            crs.to_utm(0.0, 200.0)
