"""
Geometry primitives: navigation, area, hulls, simplification, buffers,
intersection and point-in-geometry with holes.
"""

from __future__ import annotations

import math

import pytest

from qoregeo import geometry as geom
from qoregeo.exceptions import (
    InvalidBufferError,
    InvalidGeometryError,
    InvalidRadiusError,
    InvalidUnitError,
)
from qoregeo.utils import _haversine_km

from .conftest import DELHI, LONDON, MUMBAI, NYC, SYDNEY

SQUARE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]],
}
SQUARE_WITH_HOLE = {
    "type": "Polygon",
    "coordinates": [
        [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]],
        [[4, 4], [4, 6], [6, 6], [6, 4], [4, 4]],
    ],
}


class TestUnitConversion:
    def test_km_is_identity(self):
        assert geom.convert_length(100, "km") == 100

    @pytest.mark.parametrize(
        "unit,expected",
        [("miles", 62.1371), ("m", 100000), ("ft", 328084), ("nm", 53.9957)],
    )
    def test_length_units(self, unit, expected):
        assert geom.convert_length(100, unit) == pytest.approx(expected, rel=1e-4)

    def test_to_km_round_trips(self):
        assert geom.to_km(geom.convert_length(42, "miles"), "miles") == pytest.approx(42)

    def test_area_units(self):
        assert geom.convert_area(1, "m2") == pytest.approx(1_000_000)
        assert geom.convert_area(1, "ha") == pytest.approx(100)
        assert geom.convert_area(1, "acres") == pytest.approx(247.10538)

    def test_area_unit_accepts_superscript(self):
        assert geom.convert_area(1, "km²") == 1

    def test_bad_length_unit(self):
        with pytest.raises(InvalidUnitError):
            geom.convert_length(1, "parsecs")

    def test_bad_area_unit(self):
        with pytest.raises(InvalidUnitError):
            geom.convert_area(1, "parsecs")


class TestNavigation:
    def test_bearing_east(self):
        assert geom.bearing_degrees((0, 0), (0, 10)) == pytest.approx(90, abs=0.01)

    def test_bearing_north(self):
        assert geom.bearing_degrees((0, 0), (10, 0)) == pytest.approx(0, abs=0.01)

    def test_bearing_wraps_positive(self):
        assert 0 <= geom.bearing_degrees(DELHI, MUMBAI) < 360

    def test_destination_matches_distance(self):
        end = geom.destination(DELHI, 45, 250)
        assert _haversine_km(DELHI, end) == pytest.approx(250, rel=1e-4)

    def test_destination_matches_bearing(self):
        end = geom.destination(DELHI, 137, 100)
        assert geom.bearing_degrees(DELHI, end) == pytest.approx(137, abs=0.5)

    def test_destination_normalises_longitude(self):
        _, lng = geom.destination((0.0, 179.0), 90, 500)
        assert -180 <= lng <= 180

    def test_midpoint_is_equidistant(self):
        mid = geom.midpoint(DELHI, MUMBAI)
        assert _haversine_km(DELHI, mid) == pytest.approx(_haversine_km(mid, MUMBAI), rel=1e-6)

    def test_interpolate_endpoints(self):
        assert geom.interpolate(DELHI, MUMBAI, 0.0) == pytest.approx(DELHI, abs=1e-9)
        assert geom.interpolate(DELHI, MUMBAI, 1.0) == pytest.approx(MUMBAI, abs=1e-9)

    def test_interpolate_clamps_out_of_range(self):
        assert geom.interpolate(DELHI, MUMBAI, 5.0) == pytest.approx(MUMBAI, abs=1e-9)

    def test_interpolate_identical_points(self):
        assert geom.interpolate(DELHI, DELHI, 0.5) == pytest.approx(DELHI)

    def test_interpolate_quarter_way(self):
        quarter = geom.interpolate(DELHI, MUMBAI, 0.25)
        total = _haversine_km(DELHI, MUMBAI)
        assert _haversine_km(DELHI, quarter) == pytest.approx(total * 0.25, rel=1e-4)


class TestVincenty:
    def test_zero_for_same_point(self):
        assert geom.vincenty_km(DELHI, DELHI) == 0.0

    def test_close_to_haversine(self):
        vincenty = geom.vincenty_km(DELHI, MUMBAI)
        haversine = _haversine_km(DELHI, MUMBAI)
        assert abs(vincenty - haversine) / haversine < 0.01

    def test_known_london_nyc(self):
        # The accepted WGS84 geodesic is ~5570 km.
        assert geom.vincenty_km(LONDON, NYC) == pytest.approx(5570, rel=0.005)

    def test_symmetric(self):
        assert geom.vincenty_km(DELHI, SYDNEY) == pytest.approx(
            geom.vincenty_km(SYDNEY, DELHI), rel=1e-9
        )

    def test_antipodal_falls_back(self):
        # Vincenty does not converge for antipodes; the result must still be sane.
        result = geom.vincenty_km((0.0, 0.0), (0.0, 180.0))
        assert 19000 < result < 21000


class TestCrossTrack:
    def test_point_on_line_has_zero_offset(self):
        mid = geom.midpoint(DELHI, MUMBAI)
        assert geom.cross_track_km(mid, DELHI, MUMBAI) == pytest.approx(0, abs=1e-6)

    def test_sign_flips_across_the_line(self):
        left = geom.destination(geom.midpoint((0, 0), (0, 10)), 0, 50)
        right = geom.destination(geom.midpoint((0, 0), (0, 10)), 180, 50)
        assert geom.cross_track_km(left, (0, 0), (0, 10)) * geom.cross_track_km(
            right, (0, 0), (0, 10)
        ) < 0

    def test_magnitude_matches_offset(self):
        off = geom.destination(geom.midpoint((0, 0), (0, 10)), 0, 50)
        assert abs(geom.cross_track_km(off, (0, 0), (0, 10))) == pytest.approx(50, rel=0.02)

    def test_nearest_point_on_line(self):
        result = geom.nearest_point_on_line((0.5, 5.0), [(0, 0), (0, 10)])
        assert result["distance"] == pytest.approx(55.6, rel=0.02)
        assert 0 <= result["fraction"] <= 1
        assert result["segment"] == 0

    def test_nearest_point_snaps_to_endpoint(self):
        result = geom.nearest_point_on_line((0.0, -5.0), [(0, 0), (0, 10)])
        assert result["fraction"] == 0.0

    def test_nearest_point_multi_segment(self):
        result = geom.nearest_point_on_line((5.1, 10.0), [(0, 0), (5, 10), (10, 20)])
        assert result["distance"] < 20

    def test_nearest_point_needs_two_points(self):
        with pytest.raises(InvalidGeometryError):
            geom.nearest_point_on_line((0, 0), [(1, 1)])


class TestAreaAndLength:
    def test_ring_area_of_ten_degree_square(self):
        # A 10°×10° box on the equator is roughly 1.23 million km².
        area = geom.ring_area_km2(SQUARE["coordinates"][0])
        assert area == pytest.approx(1_232_000, rel=0.02)

    def test_hole_is_subtracted(self):
        whole = geom.geometry_area_km2(SQUARE)
        holed = geom.geometry_area_km2(SQUARE_WITH_HOLE)
        assert holed < whole
        assert whole - holed == pytest.approx(
            geom.ring_area_km2(SQUARE_WITH_HOLE["coordinates"][1]), rel=1e-6
        )

    def test_circle_area_matches_formula(self):
        ring = geom.circle_ring(DELHI, 10, 256)
        expected = math.pi * 10 ** 2
        assert geom.ring_area_km2(ring) == pytest.approx(expected, rel=0.01)

    def test_point_has_no_area(self):
        assert geom.geometry_area_km2({"type": "Point", "coordinates": [0, 0]}) == 0

    def test_multipolygon_area_sums(self):
        multi = {"type": "MultiPolygon", "coordinates": [SQUARE["coordinates"]] * 2}
        assert geom.geometry_area_km2(multi) == pytest.approx(
            2 * geom.geometry_area_km2(SQUARE), rel=1e-9
        )

    def test_degenerate_ring_has_no_area(self):
        assert geom.ring_area_km2([[0, 0], [1, 1]]) == 0.0

    def test_line_length(self):
        line = {"type": "LineString", "coordinates": [[77.2090, 28.6139], [72.8777, 19.0760]]}
        assert geom.geometry_length_km(line) == pytest.approx(_haversine_km(DELHI, MUMBAI))

    def test_polygon_length_is_perimeter(self):
        ring = geom.circle_ring(DELHI, 10, 256)
        perimeter = geom.geometry_length_km({"type": "Polygon", "coordinates": [ring]})
        assert perimeter == pytest.approx(2 * math.pi * 10, rel=0.01)

    def test_polygon_perimeter_includes_holes(self):
        assert geom.geometry_length_km(SQUARE_WITH_HOLE) > geom.geometry_length_km(SQUARE)

    def test_point_has_no_length(self):
        assert geom.geometry_length_km({"type": "Point", "coordinates": [0, 0]}) == 0

    def test_geometry_collection_area(self):
        collection = {"type": "GeometryCollection", "geometries": [SQUARE, SQUARE]}
        assert geom.geometry_area_km2(collection) == pytest.approx(
            2 * geom.geometry_area_km2(SQUARE)
        )


class TestCoordsAndBounds:
    def test_coords_of_point(self):
        assert geom.coords_of({"type": "Point", "coordinates": [1, 2]}) == [[1, 2]]

    def test_coords_of_polygon_flattens(self):
        assert len(geom.coords_of(SQUARE_WITH_HOLE)) == 10

    def test_coords_of_multipolygon(self):
        multi = {"type": "MultiPolygon", "coordinates": [SQUARE["coordinates"]] * 3}
        assert len(geom.coords_of(multi)) == 15

    def test_coords_of_empty(self):
        assert geom.coords_of(None) == []

    def test_bbox(self):
        assert geom.bbox_of(SQUARE) == [0, 0, 10, 10]

    def test_bbox_of_empty_raises(self):
        with pytest.raises(InvalidGeometryError):
            geom.bbox_of({"type": "Point", "coordinates": []})

    def test_bbox_polygon_round_trip(self):
        polygon = geom.bbox_polygon([0, 0, 10, 10])
        assert geom.bbox_of(polygon) == [0, 0, 10, 10]

    def test_centroid_of_square(self):
        assert geom.centroid_of(SQUARE) == pytest.approx((5.0, 5.0))

    def test_centroid_of_point(self):
        assert geom.centroid_of({"type": "Point", "coordinates": [3, 4]}) == (4.0, 3.0)

    def test_centroid_of_line(self):
        line = {"type": "LineString", "coordinates": [[0, 0], [10, 10]]}
        assert geom.centroid_of(line) == pytest.approx((5.0, 5.0))


class TestPointInGeometry:
    def test_inside_polygon(self):
        assert geom.point_in_geometry((5, 5), SQUARE) is True

    def test_outside_polygon(self):
        assert geom.point_in_geometry((50, 50), SQUARE) is False

    def test_hole_reads_as_outside(self):
        assert geom.point_in_geometry((5, 5), SQUARE_WITH_HOLE) is False

    def test_body_outside_hole_is_inside(self):
        assert geom.point_in_geometry((1, 1), SQUARE_WITH_HOLE) is True

    def test_multipolygon_any_part(self):
        multi = {
            "type": "MultiPolygon",
            "coordinates": [
                SQUARE["coordinates"],
                [[[20, 20], [20, 22], [22, 22], [22, 20], [20, 20]]],
            ],
        }
        assert geom.point_in_geometry((21, 21), multi) is True
        assert geom.point_in_geometry((15, 15), multi) is False

    def test_feature_wrapper(self):
        feature = {"type": "Feature", "geometry": SQUARE, "properties": {}}
        assert geom.point_in_geometry((5, 5), feature) is True

    def test_feature_collection(self):
        collection = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "geometry": SQUARE, "properties": {}}],
        }
        assert geom.point_in_geometry((5, 5), collection) is True

    def test_line_has_no_interior(self):
        line = {"type": "LineString", "coordinates": [[0, 0], [10, 10]]}
        assert geom.point_in_geometry((5, 5), line) is False

    def test_unknown_type_raises(self):
        with pytest.raises(InvalidBufferError):
            geom.point_in_geometry((0, 0), {"type": "Sphere"})

    def test_non_dict_raises(self):
        with pytest.raises(InvalidBufferError):
            geom.point_in_geometry((0, 0), "nope")


class TestIntersects:
    def test_overlapping_squares(self):
        other = {
            "type": "Polygon",
            "coordinates": [[[5, 5], [5, 15], [15, 15], [15, 5], [5, 5]]],
        }
        assert geom.intersects(SQUARE, other) is True

    def test_disjoint_squares(self):
        other = {
            "type": "Polygon",
            "coordinates": [[[50, 50], [50, 60], [60, 60], [60, 50], [50, 50]]],
        }
        assert geom.intersects(SQUARE, other) is False

    def test_contained_square_intersects(self):
        inner = {
            "type": "Polygon",
            "coordinates": [[[2, 2], [2, 4], [4, 4], [4, 2], [2, 2]]],
        }
        assert geom.intersects(SQUARE, inner) is True

    def test_line_crossing_polygon(self):
        line = {"type": "LineString", "coordinates": [[-5, 5], [15, 5]]}
        assert geom.intersects(SQUARE, line) is True

    def test_line_missing_polygon(self):
        line = {"type": "LineString", "coordinates": [[-5, 50], [15, 50]]}
        assert geom.intersects(SQUARE, line) is False

    def test_crossing_lines(self):
        a = {"type": "LineString", "coordinates": [[0, 0], [10, 10]]}
        b = {"type": "LineString", "coordinates": [[0, 10], [10, 0]]}
        assert geom.intersects(a, b) is True

    def test_parallel_lines(self):
        a = {"type": "LineString", "coordinates": [[0, 0], [10, 0]]}
        b = {"type": "LineString", "coordinates": [[0, 5], [10, 5]]}
        assert geom.intersects(a, b) is False

    def test_contains_true(self):
        inner = {
            "type": "Polygon",
            "coordinates": [[[2, 2], [2, 4], [4, 4], [4, 2], [2, 2]]],
        }
        assert geom.contains(SQUARE, inner) is True

    def test_contains_false_when_straddling(self):
        straddle = {
            "type": "Polygon",
            "coordinates": [[[8, 8], [8, 20], [20, 20], [20, 8], [8, 8]]],
        }
        assert geom.contains(SQUARE, straddle) is False

    def test_line_cannot_contain(self):
        line = {"type": "LineString", "coordinates": [[0, 0], [10, 10]]}
        assert geom.contains(line, SQUARE) is False

    def test_segments_intersect_touching(self):
        assert geom.segments_intersect([0, 0], [10, 0], [5, 0], [5, 10]) is True

    def test_segments_do_not_intersect(self):
        assert geom.segments_intersect([0, 0], [1, 0], [5, 5], [6, 5]) is False

    def test_bbox_overlaps(self):
        assert geom.bbox_overlaps([0, 0, 5, 5], [4, 4, 10, 10]) is True
        assert geom.bbox_overlaps([0, 0, 5, 5], [6, 6, 10, 10]) is False


class TestConvexHull:
    def test_square_from_scattered_points(self):
        points = [[0, 0], [1, 0], [1, 1], [0, 1], [0.5, 0.5], [0.2, 0.9]]
        hull = geom.convex_hull(points)
        assert hull[0] == hull[-1]
        assert len(hull) == 5

    def test_interior_points_excluded(self):
        hull = geom.convex_hull([[0, 0], [10, 0], [10, 10], [0, 10], [5, 5]])
        assert [5, 5] not in hull

    def test_needs_three_points(self):
        with pytest.raises(InvalidGeometryError):
            geom.convex_hull([[0, 0], [1, 1]])

    def test_collinear_points_raise(self):
        with pytest.raises(InvalidGeometryError):
            geom.convex_hull([[0, 0], [1, 1], [2, 2], [3, 3]])

    def test_duplicates_are_ignored(self):
        hull = geom.convex_hull([[0, 0], [0, 0], [1, 0], [1, 1], [0, 1]])
        assert len(hull) == 5


class TestSimplify:
    def test_removes_collinear_noise(self):
        line = [[0, 0], [1, 0.00001], [2, 0], [3, 0.5], [4, 0]]
        assert len(geom.simplify(line, 0.01)) < len(line)

    def test_keeps_endpoints(self):
        line = [[0, 0], [1, 0.0001], [2, 0], [3, 0.5], [4, 0]]
        result = geom.simplify(line, 0.01)
        assert result[0] == [0, 0]
        assert result[-1] == [4, 0]

    def test_short_lines_untouched(self):
        assert geom.simplify([[0, 0], [1, 1]], 10) == [[0, 0], [1, 1]]

    def test_zero_tolerance_keeps_shape(self):
        line = [[0, 0], [1, 1], [2, 0]]
        assert len(geom.simplify(line, 0)) == 3

    def test_closed_ring_stays_closed(self):
        ring = geom.circle_ring(DELHI, 50, 64)
        simplified = geom.simplify(ring, 0.05, closed=True)
        assert simplified[0] == simplified[-1]

    def test_huge_tolerance_keeps_valid_ring(self):
        ring = geom.circle_ring(DELHI, 50, 64)
        simplified = geom.simplify(ring, 1000, closed=True)
        assert len(simplified) >= 4


class TestBuffers:
    def test_circle_ring_is_closed(self):
        ring = geom.circle_ring(DELHI, 10)
        assert ring[0] == ring[-1]

    def test_circle_ring_radius(self):
        for point in geom.circle_ring(DELHI, 25, 32)[:-1]:
            assert _haversine_km(DELHI, (point[1], point[0])) == pytest.approx(25, rel=1e-4)

    def test_circle_ring_rejects_zero(self):
        with pytest.raises(InvalidRadiusError):
            geom.circle_ring(DELHI, 0)

    def test_line_buffer_covers_the_line(self):
        corridor = geom.line_buffer([DELHI, MUMBAI], 20)
        assert geom.point_in_geometry(geom.midpoint(DELHI, MUMBAI), corridor) is True

    def test_line_buffer_excludes_far_points(self):
        corridor = geom.line_buffer([DELHI, MUMBAI], 5)
        assert geom.point_in_geometry(SYDNEY, corridor) is False

    def test_line_buffer_covers_endpoints(self):
        corridor = geom.line_buffer([DELHI, MUMBAI], 10)
        assert geom.point_in_geometry(DELHI, corridor) is True
        assert geom.point_in_geometry(MUMBAI, corridor) is True

    def test_line_buffer_multi_segment(self):
        corridor = geom.line_buffer([DELHI, MUMBAI, SYDNEY], 50)
        assert len(corridor["coordinates"]) == 2

    def test_line_buffer_needs_two_points(self):
        with pytest.raises(InvalidGeometryError):
            geom.line_buffer([DELHI], 10)

    def test_line_buffer_rejects_zero_radius(self):
        with pytest.raises(InvalidRadiusError):
            geom.line_buffer([DELHI, MUMBAI], 0)

    def test_line_buffer_handles_repeated_point(self):
        corridor = geom.line_buffer([DELHI, DELHI], 5)
        assert geom.point_in_geometry(DELHI, corridor) is True

    def test_geometry_buffer_point(self):
        buffered = geom.geometry_buffer({"type": "Point", "coordinates": [77.209, 28.6139]}, 10)
        assert buffered["type"] == "Polygon"
        assert geom.point_in_geometry(DELHI, buffered) is True

    def test_geometry_buffer_line(self):
        line = {"type": "LineString", "coordinates": [[77.209, 28.6139], [72.877, 19.076]]}
        buffered = geom.geometry_buffer(line, 15)
        assert buffered["type"] == "MultiPolygon"

    def test_geometry_buffer_polygon_grows(self):
        buffered = geom.geometry_buffer(SQUARE, 100)
        assert geom.geometry_area_km2(buffered) >= geom.geometry_area_km2(SQUARE)

    def test_geometry_buffer_multipoint(self):
        multi = {"type": "MultiPoint", "coordinates": [[0, 0], [10, 10]]}
        assert geom.geometry_buffer(multi, 5)["type"] == "MultiPolygon"

    def test_geometry_buffer_rejects_unknown(self):
        with pytest.raises(InvalidGeometryError):
            geom.geometry_buffer({"type": "Sphere"}, 5)


class TestCorridorAccuracy:
    """
    A corridor must be a true fixed distance from its path on *both* sides.

    Joining the end offsets with straight lng/lat chords silently fails this:
    a great circle is a curve in lng/lat space, so the corridor bulges one way
    and pinches the other. These tests pin the densified behaviour.
    """

    CORRIDOR = geom.line_buffer([DELHI, MUMBAI], 20)

    @pytest.mark.parametrize("side", [-90, 90])
    @pytest.mark.parametrize("offset_km,expected", [(5, True), (15, True), (19, True),
                                                    (25, False), (40, False)])
    def test_symmetric_about_the_path(self, side, offset_km, expected):
        mid = geom.midpoint(DELHI, MUMBAI)
        heading = geom.bearing_degrees(mid, MUMBAI)
        probe = geom.destination(mid, heading + side, offset_km)
        assert geom.point_in_geometry(probe, self.CORRIDOR) is expected

    def test_area_matches_the_analytic_stadium(self):
        # Two rectangles plus a full circle of caps.
        length = _haversine_km(DELHI, MUMBAI)
        analytic = 2 * 20 * length + math.pi * 20 ** 2
        assert geom.geometry_area_km2(self.CORRIDOR) == pytest.approx(analytic, rel=0.01)

    def test_holds_over_an_intercontinental_leg(self):
        corridor = geom.line_buffer([DELHI, SYDNEY], 100)
        mid = geom.midpoint(DELHI, SYDNEY)
        heading = geom.bearing_degrees(mid, SYDNEY)
        assert geom.point_in_geometry(geom.destination(mid, heading + 90, 50), corridor)
        assert geom.point_in_geometry(geom.destination(mid, heading - 90, 50), corridor)
        assert not geom.point_in_geometry(geom.destination(mid, heading + 90, 150), corridor)

    def test_holds_for_a_very_short_segment(self):
        corridor = geom.line_buffer([(28.6139, 77.2090), (28.6150, 77.2100)], 0.5)
        assert geom.point_in_geometry((28.6145, 77.2095), corridor) is True
        assert geom.point_in_geometry((28.70, 77.21), corridor) is False

    def test_vertex_count_stays_bounded(self):
        # A 10 m radius over a 1000 km line must not generate a million points.
        corridor = geom.line_buffer([DELHI, MUMBAI], 0.01)
        assert len(corridor["coordinates"][0][0]) < 1200


class TestBoundaryInclusion:
    """
    Points exactly on a polygon edge count as inside.

    Plain ray casting leaves these undefined, which produces two results
    nobody expects: a convex hull that excludes the points it was built from,
    and a geofence that rejects an address on its own boundary.
    """

    def test_vertex_is_inside(self):
        assert geom.point_in_geometry((0, 0), SQUARE) is True

    def test_edge_midpoint_is_inside(self):
        assert geom.point_in_geometry((0, 5), SQUARE) is True

    def test_just_outside_the_edge_is_outside(self):
        assert geom.point_in_geometry((-0.001, 5), SQUARE) is False

    def test_interior_is_unaffected(self):
        assert geom.point_in_geometry((5, 5), SQUARE) is True

    def test_hole_interior_is_still_excluded(self):
        assert geom.point_in_geometry((5, 5), SQUARE_WITH_HOLE) is False

    def test_circle_edge_is_inside(self):
        ring = geom.circle_ring(DELHI, 10, 64)
        vertex = (ring[0][1], ring[0][0])
        assert geom.point_in_geometry(vertex, {"type": "Polygon", "coordinates": [ring]}) is True

    def test_a_hull_contains_its_own_points(self):
        points = [[0, 0], [10, 0], [10, 10], [0, 10], [3, 4]]
        hull = {"type": "Polygon", "coordinates": [geom.convex_hull(points)]}
        assert all(geom.point_in_geometry((p[1], p[0]), hull) for p in points)
