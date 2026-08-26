"""
Spatial index correctness. The index is only useful if it returns *exactly*
what a brute-force scan would, so every test compares against one.
"""

from __future__ import annotations

import random

import pytest

from qoregeo.index import AUTO_INDEX_THRESHOLD, SpatialIndex, km_per_degree_lng
from qoregeo.utils import _haversine_km

from .conftest import DELHI, point_feature


def brute_force(point, features):
    """The answer the index must reproduce, computed the slow honest way."""
    return sorted(
        (
            (_haversine_km(point, (f["geometry"]["coordinates"][1],
                                   f["geometry"]["coordinates"][0])), i)
            for i, f in enumerate(features)
        ),
        key=lambda r: (r[0], r[1]),
    )


@pytest.fixture(scope="module")
def scattered():
    """20 000 points across the Indian subcontinent."""
    rng = random.Random(20240101)
    return [
        point_feature(rng.uniform(8, 35), rng.uniform(68, 92), i=i)
        for i in range(20_000)
    ]


class TestConstruction:
    def test_empty_index(self):
        index = SpatialIndex([])
        assert index.size == 0
        assert index.cells == 0
        assert index.nearest(DELHI, 1) == []
        assert index.within(DELHI, 100) == []

    def test_single_point(self):
        index = SpatialIndex([point_feature(*DELHI)])
        assert index.size == 1
        assert index.nearest(DELHI, 5) == [(pytest.approx(0.0), 0)]

    def test_auto_cell_size_is_bounded(self, scattered):
        index = SpatialIndex(scattered)
        assert 0.05 <= index.cell_size_km <= 500

    def test_explicit_cell_size_honoured(self, scattered):
        assert SpatialIndex(scattered, cell_size_km=25).cell_size_km == 25

    def test_stats_shape(self, scattered):
        stats = SpatialIndex(scattered).stats()
        assert stats["features"] == 20_000
        assert stats["cells"] > 0
        assert stats["max_per_cell"] >= stats["avg_per_cell"]

    def test_repr(self, scattered):
        assert "SpatialIndex" in repr(SpatialIndex(scattered))

    def test_non_point_geometry_uses_centroid(self):
        polygon = {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]],
            },
            "properties": {},
        }
        index = SpatialIndex([polygon])
        assert index.size == 1
        assert index.nearest((5.0, 5.0), 1)[0][0] < 1

    def test_feature_without_geometry_is_skipped(self):
        assert SpatialIndex([{"type": "Feature", "properties": {}}]).size == 0

    def test_auto_threshold_is_sane(self):
        assert AUTO_INDEX_THRESHOLD > 0


class TestRadiusQueries:
    @pytest.mark.parametrize("radius", [1, 10, 50, 200, 1000])
    def test_matches_brute_force(self, scattered, radius):
        index = SpatialIndex(scattered)
        expected = [r for r in brute_force(DELHI, scattered) if r[0] <= radius]
        assert index.within(DELHI, radius) == pytest.approx(expected)

    def test_results_are_sorted(self, scattered):
        found = SpatialIndex(scattered).within(DELHI, 100)
        assert found == sorted(found)

    def test_radius_beyond_the_data(self, scattered):
        assert len(SpatialIndex(scattered).within(DELHI, 40_000)) == len(scattered)

    def test_radius_with_no_hits(self):
        index = SpatialIndex([point_feature(*DELHI)])
        assert index.within((-40.0, -70.0), 10) == []

    def test_near_the_pole(self):
        polar = [point_feature(89.0, lng, i=lng) for lng in range(-180, 180, 10)]
        index = SpatialIndex(polar)
        expected = [r for r in brute_force((89.0, 0.0), polar) if r[0] <= 500]
        assert index.within((89.0, 0.0), 500) == pytest.approx(expected)

    def test_across_the_antimeridian(self):
        pair = [point_feature(0.0, 179.9), point_feature(0.0, -179.9)]
        index = SpatialIndex(pair)
        # Both points sit ~11 km from the query, on opposite sides of the line.
        assert len(index.within((0.0, 179.95), 100)) == 2


class TestNearest:
    @pytest.mark.parametrize("k", [1, 3, 10, 50])
    def test_knn_matches_brute_force(self, scattered, k):
        index = SpatialIndex(scattered)
        expected = brute_force(DELHI, scattered)[:k]
        assert index.nearest(DELHI, k) == pytest.approx(expected)

    def test_k_larger_than_dataset(self):
        index = SpatialIndex([point_feature(*DELHI)])
        assert len(index.nearest(DELHI, 99)) == 1

    def test_k_below_one_is_clamped(self, scattered):
        assert len(SpatialIndex(scattered).nearest(DELHI, 0)) == 1

    def test_query_far_from_all_data(self, scattered):
        # A point in the South Atlantic still resolves, via the widening search.
        found = SpatialIndex(scattered).nearest((-50.0, -30.0), 3)
        assert len(found) == 3
        assert found == pytest.approx(brute_force((-50.0, -30.0), scattered)[:3])

    def test_sparse_data_does_not_hang(self):
        # Two points on opposite sides of the planet: the doubling search must
        # terminate rather than expanding forever.
        pair = [point_feature(0.0, 0.0), point_feature(0.0, 180.0)]
        found = SpatialIndex(pair).nearest((0.0, 90.0), 2)
        assert len(found) == 2


class TestBBox:
    def test_in_bbox(self, scattered):
        found = set(SpatialIndex(scattered).in_bbox((28.0, 77.0, 29.0, 78.0)))
        expected = {
            i
            for i, f in enumerate(scattered)
            if 28.0 <= f["geometry"]["coordinates"][1] <= 29.0
            and 77.0 <= f["geometry"]["coordinates"][0] <= 78.0
        }
        assert found == expected

    def test_empty_bbox(self, scattered):
        assert SpatialIndex(scattered).in_bbox((-89.0, -179.0, -88.0, -178.0)) == []


class TestHelpers:
    def test_km_per_degree_shrinks_toward_the_poles(self):
        assert km_per_degree_lng(0) > km_per_degree_lng(45) > km_per_degree_lng(80)

    def test_km_per_degree_at_equator(self):
        assert km_per_degree_lng(0) == pytest.approx(111.32, rel=1e-6)

    def test_km_per_degree_never_zero(self):
        assert km_per_degree_lng(90) > 0


class TestPolarAndWrapAround:
    """
    The two places a grid index quietly loses results: over a pole, and across
    the antimeridian. Both are regressions, earlier versions dropped points in
    exactly these positions.
    """

    def test_search_crossing_the_north_pole(self):
        # Points on the far side of the pole are only a few hundred km away,
        # but ~180° of longitude apart.
        near_pole = [point_feature(86.0, lng) for lng in range(-180, 180, 15)]
        index = SpatialIndex(near_pole)
        query = (89.5, 0.0)
        expected = [r for r in brute_force(query, near_pole) if r[0] <= 600]
        assert index.within(query, 600) == pytest.approx(expected)
        assert len(expected) > 1

    def test_search_crossing_the_south_pole(self):
        near_pole = [point_feature(-86.0, lng) for lng in range(-180, 180, 15)]
        index = SpatialIndex(near_pole)
        query = (-89.5, 120.0)
        expected = [r for r in brute_force(query, near_pole) if r[0] <= 600]
        assert index.within(query, 600) == pytest.approx(expected)

    def test_wide_radius_at_high_latitude(self):
        rng = random.Random(4242)
        spread = [point_feature(rng.uniform(60, 89), rng.uniform(-180, 180)) for _ in range(400)]
        index = SpatialIndex(spread)
        query = (73.0, -29.0)
        expected = [r for r in brute_force(query, spread) if r[0] <= 3000]
        assert index.within(query, 3000) == pytest.approx(expected)

    def test_antimeridian_cluster(self):
        rng = random.Random(77)
        straddling = [
            point_feature(rng.uniform(-2, 2), rng.choice([179.9, -179.9, 180.0, -180.0]))
            for _ in range(60)
        ]
        index = SpatialIndex(straddling)
        expected = [r for r in brute_force((0.0, 180.0), straddling) if r[0] <= 300]
        assert index.within((0.0, 180.0), 300) == pytest.approx(expected)

    def test_global_radius_returns_everything(self):
        rng = random.Random(11)
        world = [point_feature(rng.uniform(-90, 90), rng.uniform(-180, 180)) for _ in range(300)]
        index = SpatialIndex(world)
        assert len(index.within((0.0, 0.0), 20_100)) == len(world)

    def test_coincident_points(self):
        stacked = [point_feature(*DELHI) for _ in range(20)]
        index = SpatialIndex(stacked)
        assert len(index.within(DELHI, 1)) == 20

    def test_zero_radius_returns_nothing(self):
        index = SpatialIndex([point_feature(*DELHI)])
        assert index.within(DELHI, 0) == []
