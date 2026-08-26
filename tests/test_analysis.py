"""Clustering, spatial joins, density and point-pattern statistics."""

from __future__ import annotations

import pytest

from qoregeo import analysis
from qoregeo.exceptions import EmptyDatasetError, InvalidRadiusError
from qoregeo.utils import _haversine_km

from .conftest import CHENNAI, DELHI, MUMBAI, point_feature, polygon_feature


class TestDBSCAN:
    def test_finds_the_three_blobs(self, clustered_points):
        labels = analysis.dbscan(clustered_points, eps_km=50, min_samples=3)
        assert len({label for label in labels if label >= 0}) == 3

    def test_each_blob_gets_one_label(self, clustered_points):
        labels = analysis.dbscan(clustered_points, eps_km=50, min_samples=3)
        assert len(set(labels[0:8])) == 1
        assert len(set(labels[8:16])) == 1
        assert len(set(labels[16:24])) == 1

    def test_blobs_get_different_labels(self, clustered_points):
        labels = analysis.dbscan(clustered_points, eps_km=50, min_samples=3)
        assert labels[0] != labels[8] != labels[16]

    def test_outliers_are_noise(self, clustered_points):
        labels = analysis.dbscan(clustered_points, eps_km=50, min_samples=3)
        assert labels[-1] == analysis.NOISE
        assert labels[-2] == analysis.NOISE

    def test_one_label_per_feature(self, clustered_points):
        assert len(analysis.dbscan(clustered_points, 50, 3)) == len(clustered_points)

    def test_large_eps_merges_everything(self, clustered_points):
        labels = analysis.dbscan(clustered_points, eps_km=20_000, min_samples=2)
        assert len(set(labels)) == 1

    def test_high_min_samples_makes_everything_noise(self, clustered_points):
        labels = analysis.dbscan(clustered_points, eps_km=1, min_samples=50)
        assert set(labels) == {analysis.NOISE}

    def test_empty_input(self):
        assert analysis.dbscan([], 1, 3) == []

    def test_rejects_zero_eps(self, clustered_points):
        with pytest.raises(InvalidRadiusError):
            analysis.dbscan(clustered_points, 0, 3)

    def test_handles_polygon_features(self):
        shapes = [
            polygon_feature([[0, 0], [0, 1], [1, 1], [1, 0]]),
            polygon_feature([[0.01, 0.01], [0.01, 1], [1, 1], [1, 0.01]]),
            polygon_feature([[50, 50], [50, 51], [51, 51], [51, 50]]),
        ]
        labels = analysis.dbscan(shapes, eps_km=200, min_samples=2)
        assert labels[0] == labels[1]
        assert labels[2] == analysis.NOISE


class TestKMeans:
    def test_recovers_known_centres(self, clustered_points):
        result = analysis.kmeans(clustered_points[:24], k=3, seed=7)
        found = sorted(result["centroids"])
        for expected in sorted([DELHI, MUMBAI, CHENNAI]):
            assert any(_haversine_km(expected, c) < 50 for c in found)

    def test_balanced_partition(self, clustered_points):
        result = analysis.kmeans(clustered_points[:24], k=3, seed=7)
        assert sorted(result["labels"].count(i) for i in range(3)) == [8, 8, 8]

    def test_result_shape(self, clustered_points):
        result = analysis.kmeans(clustered_points, k=2, seed=1)
        assert set(result) == {"labels", "centroids", "iterations", "inertia"}

    def test_k_is_clamped_to_dataset_size(self):
        result = analysis.kmeans([point_feature(*DELHI)], k=10)
        assert len(result["centroids"]) == 1

    def test_deterministic_with_a_seed(self, clustered_points):
        first = analysis.kmeans(clustered_points, k=3, seed=42)
        second = analysis.kmeans(clustered_points, k=3, seed=42)
        assert first["labels"] == second["labels"]

    def test_inertia_falls_as_k_rises(self, clustered_points):
        low = analysis.kmeans(clustered_points[:24], k=1, seed=3)["inertia"]
        high = analysis.kmeans(clustered_points[:24], k=3, seed=3)["inertia"]
        assert high < low

    def test_empty_input_raises(self):
        with pytest.raises(EmptyDatasetError):
            analysis.kmeans([], k=2)


class TestSphericalMean:
    def test_simple_average(self):
        assert analysis.spherical_mean([(0.0, 0.0), (0.0, 10.0)]) == pytest.approx((0.0, 5.0))

    def test_crosses_the_antimeridian(self):
        # A naive average of longitudes would give 0 — the wrong side of the planet.
        lat, lng = analysis.spherical_mean([(0.0, 179.0), (0.0, -179.0)])
        assert abs(lng) == pytest.approx(180.0, abs=1e-6)
        assert lat == pytest.approx(0.0, abs=1e-9)

    def test_single_point(self):
        assert analysis.spherical_mean([DELHI]) == pytest.approx(DELHI, abs=1e-9)

    def test_opposite_points_do_not_crash(self):
        assert analysis.spherical_mean([(90.0, 0.0), (-90.0, 0.0)]) == (0.0, 0.0)

    def test_empty_raises(self):
        with pytest.raises(EmptyDatasetError):
            analysis.spherical_mean([])


class TestSpatialJoin:
    @pytest.fixture
    def zone(self):
        return polygon_feature(
            [[77.0, 28.4], [77.4, 28.4], [77.4, 28.8], [77.0, 28.8]],
            district="New Delhi", code="DL01",
        )

    def test_inside_points_gain_attributes(self, zone):
        joined = analysis.spatial_join([point_feature(*DELHI)], [zone])
        assert joined[0]["properties"]["district"] == "New Delhi"

    def test_outside_points_keep_their_own(self, zone):
        joined = analysis.spatial_join([point_feature(*MUMBAI, name="Mumbai")], [zone])
        assert "district" not in joined[0]["properties"]
        assert joined[0]["properties"]["name"] == "Mumbai"

    def test_inner_join_drops_unmatched(self, zone):
        joined = analysis.spatial_join(
            [point_feature(*DELHI), point_feature(*MUMBAI)], [zone], how="inner"
        )
        assert len(joined) == 1

    def test_prefix_avoids_collisions(self, zone):
        joined = analysis.spatial_join([point_feature(*DELHI, code="mine")], [zone], prefix="zone_")
        assert joined[0]["properties"]["code"] == "mine"
        assert joined[0]["properties"]["zone_code"] == "DL01"

    def test_keep_restricts_columns(self, zone):
        joined = analysis.spatial_join([point_feature(*DELHI)], [zone], keep=["code"])
        assert "code" in joined[0]["properties"]
        assert "district" not in joined[0]["properties"]

    def test_no_polygons_is_a_no_op(self):
        points = [point_feature(*DELHI)]
        assert analysis.spatial_join(points, []) == points

    def test_non_point_features_pass_through_on_left(self, zone):
        line = {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            "properties": {"n": 1},
        }
        assert len(analysis.spatial_join([line], [zone])) == 1

    def test_first_matching_polygon_wins(self):
        overlapping = [
            polygon_feature([[0, 0], [10, 0], [10, 10], [0, 10]], name="first"),
            polygon_feature([[0, 0], [10, 0], [10, 10], [0, 10]], name="second"),
        ]
        joined = analysis.spatial_join([point_feature(5, 5)], overlapping)
        assert joined[0]["properties"]["name"] == "first"


class TestHotspots:
    def test_dense_cells_come_first(self, clustered_points):
        cells = analysis.hotspots(clustered_points, cell_km=100, min_count=2)
        counts = [cell["count"] for cell in cells]
        assert counts == sorted(counts, reverse=True)

    def test_min_count_filters(self, clustered_points):
        assert analysis.hotspots(clustered_points, 100, 100) == []

    def test_cell_carries_a_polygon(self, clustered_points):
        cell = analysis.hotspots(clustered_points, 100, 2)[0]
        assert cell["polygon"]["type"] == "Polygon"
        assert cell["polygon"]["coordinates"][0][0] == cell["polygon"]["coordinates"][0][-1]

    def test_centre_lies_within_the_cell(self, clustered_points):
        cell = analysis.hotspots(clustered_points, 100, 2)[0]
        assert cell["bounds"]["min_lat"] <= cell["centre"][0] <= cell["bounds"]["max_lat"]

    def test_empty_input(self):
        assert analysis.hotspots([], 10) == []

    def test_rejects_zero_cell(self, clustered_points):
        with pytest.raises(InvalidRadiusError):
            analysis.hotspots(clustered_points, 0)


class TestPointPattern:
    def test_clustered_data_is_reported_as_clustered(self, clustered_points):
        assert analysis.nearest_neighbour_ratio(clustered_points[:24])["pattern"] == "clustered"

    def test_regular_grid_is_dispersed(self):
        grid = [point_feature(lat, lng) for lat in range(10) for lng in range(10)]
        assert analysis.nearest_neighbour_ratio(grid)["ratio"] > 1.0

    def test_result_shape(self, clustered_points):
        result = analysis.nearest_neighbour_ratio(clustered_points)
        assert set(result) == {
            "observed_mean_km", "expected_mean_km", "ratio", "pattern", "area_km2", "n",
        }

    def test_needs_two_points(self):
        with pytest.raises(EmptyDatasetError):
            analysis.nearest_neighbour_ratio([point_feature(*DELHI)])


class TestCentreOfMass:
    def test_unweighted_is_the_mean(self):
        points = [point_feature(0, 0), point_feature(0, 10)]
        assert analysis.centre_of_mass(points) == pytest.approx((0.0, 5.0))

    def test_weight_pulls_the_centre(self):
        points = [point_feature(0, 0, pop=1), point_feature(10, 0, pop=9)]
        lat, _ = analysis.centre_of_mass(points, "pop")
        assert lat == pytest.approx(9.0, abs=0.05)

    def test_zero_weights_fall_back_to_the_mean(self):
        points = [point_feature(0, 0, pop=0), point_feature(10, 0, pop=0)]
        assert analysis.centre_of_mass(points, "pop") == pytest.approx(
            analysis.centre_of_mass(points)
        )

    def test_non_numeric_weights_are_skipped(self):
        points = [point_feature(0, 0, pop="n/a"), point_feature(10, 0, pop=5)]
        assert analysis.centre_of_mass(points, "pop")[0] == pytest.approx(10.0, abs=0.01)

    def test_empty_raises(self):
        with pytest.raises(EmptyDatasetError):
            analysis.centre_of_mass([])


class TestDispersion:
    def test_reports_spread(self, clustered_points):
        result = analysis.dispersion(clustered_points)
        assert result["max_km"] >= result["mean_km"] > 0

    def test_identical_points_have_no_spread(self):
        stacked = [point_feature(*DELHI) for _ in range(5)]
        assert analysis.dispersion(stacked)["max_km"] == pytest.approx(0, abs=1e-6)

    def test_result_shape(self, clustered_points):
        assert set(analysis.dispersion(clustered_points)) == {
            "centre_lat", "centre_lng", "mean_km", "median_km",
            "max_km", "standard_distance_km",
        }

    def test_empty_raises(self):
        with pytest.raises(EmptyDatasetError):
            analysis.dispersion([])

    def test_cap_area_grows_with_radius(self):
        assert analysis.great_circle_area_of(10) < analysis.great_circle_area_of(100)

    def test_cap_area_matches_flat_circle_when_small(self):
        import math
        assert analysis.great_circle_area_of(10) == pytest.approx(math.pi * 100, rel=1e-4)

    def test_cap_area_rejects_zero(self):
        with pytest.raises(InvalidRadiusError):
            analysis.great_circle_area_of(0)
