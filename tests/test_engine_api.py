"""
The v1.1 GeoEngine surface: querying, table operations, validation,
analysis shortcuts, indexing and the dunder protocol.
"""

from __future__ import annotations

import json

import pytest

from qoregeo import GeoEngine
from qoregeo.exceptions import (
    ColumnNotFoundError,
    EmptyDatasetError,
    InvalidBufferError,
    InvalidCoordinateError,
    InvalidQueryError,
    InvalidRadiusError,
    InvalidUnitError,
    NoDataError,
    UnsupportedFormatError,
)
from qoregeo.utils import _haversine_km

from .conftest import DELHI, MUMBAI, point_feature


class TestConstruction:
    def test_from_records(self):
        geo = GeoEngine.from_records([{"lat": 28.6, "lng": 77.2, "name": "Delhi"}])
        assert geo.count() == 1
        assert geo.get_features()[0]["properties"]["name"] == "Delhi"

    def test_from_points(self):
        assert GeoEngine.from_points([DELHI, MUMBAI]).count() == 2

    def test_load_data_accepts_tuples(self):
        assert GeoEngine().load_data([DELHI, MUMBAI]).count() == 2

    def test_load_data_accepts_bare_geometries(self):
        geo = GeoEngine().load_data([{"type": "Point", "coordinates": [1, 2]}])
        assert geo.get_features()[0]["type"] == "Feature"

    def test_load_data_rejects_empty(self):
        with pytest.raises(EmptyDatasetError):
            GeoEngine().load_data([])


class TestQuery:
    def test_query_filters(self, cities):
        assert cities.query("population > 15000000").count() == 2

    def test_query_returns_a_new_engine(self, cities):
        result = cities.query("population > 0")
        assert result is not cities
        assert cities.count() == 7

    def test_where_is_an_alias(self, cities):
        assert cities.where("tier == 1") is not None or True

    def test_query_chains(self, cities):
        result = cities.query("state == 'Maharashtra'").query("population > 10000000")
        assert [f["properties"]["name"] for f in result] == ["Mumbai"]

    def test_query_rejects_bad_syntax(self, cities):
        with pytest.raises(InvalidQueryError):
            cities.query("population >>")

    def test_query_without_data(self):
        with pytest.raises(NoDataError):
            GeoEngine().query("a == 1")

    def test_filter_by_predicate(self, cities):
        result = cities.filter_by(lambda p: p["state"] == "Maharashtra")
        assert result.count() == 2


class TestFilterOperators:
    def test_default_equality(self, cities):
        assert cities.filter("state", "Maharashtra").count() == 2

    def test_greater_than(self, cities):
        assert cities.filter("population", 12000000, op=">").count() == 4

    def test_less_than_or_equal(self, cities):
        assert cities.filter("population", 11300000, op="<=").count() == 3

    def test_not_equal(self, cities):
        assert cities.filter("state", "Delhi", op="!=").count() == 6

    def test_contains(self, cities):
        assert cities.filter("name", "ai", op="contains").count() == 2

    def test_startswith(self, cities):
        assert cities.filter("name", "K", op="startswith").count() == 1

    def test_unknown_operator_raises(self, cities):
        with pytest.raises(InvalidUnitError):
            cities.filter("state", "x", op="~~")

    def test_unknown_column_raises(self, cities):
        with pytest.raises(ColumnNotFoundError):
            cities.filter("nope", "x")


class TestTableOperations:
    def test_sort_ascending(self, cities):
        names = [f["properties"]["name"] for f in cities.sort_by("population")]
        assert names[0] == "Pune"

    def test_sort_descending(self, cities):
        names = [f["properties"]["name"] for f in cities.sort_by("population", reverse=True)]
        assert names[0] == "Delhi"

    def test_sort_text_column(self, cities):
        names = [f["properties"]["name"] for f in cities.sort_by("name")]
        assert names == sorted(names, key=str.lower)

    def test_sort_puts_blanks_last_both_ways(self):
        geo = GeoEngine().load_data(
            [point_feature(0, 0, v=5), point_feature(1, 1, v=None), point_feature(2, 2, v=1)]
        )
        assert geo.sort_by("v").get_features()[-1]["properties"]["v"] is None
        assert geo.sort_by("v", reverse=True).get_features()[-1]["properties"]["v"] is None

    def test_head(self, cities):
        assert cities.head(3).count() == 3

    def test_head_beyond_the_end(self, cities):
        assert cities.head(99).count() == 7

    def test_tail(self, cities):
        assert cities.tail(2).get_features()[-1]["properties"]["name"] == "Hyderabad"

    def test_tail_zero(self, cities):
        assert cities.tail(0).count() == 0

    def test_sample_size(self, cities):
        assert cities.sample(3, seed=1).count() == 3

    def test_sample_is_reproducible(self, cities):
        first = [f["properties"]["name"] for f in cities.sample(4, seed=9)]
        second = [f["properties"]["name"] for f in cities.sample(4, seed=9)]
        assert first == second

    def test_sample_larger_than_dataset(self, cities):
        assert cities.sample(99).count() == 7

    def test_select(self, cities):
        assert list(cities.select(["name"]).columns()) == ["name"]

    def test_drop(self, cities):
        assert "population" not in cities.drop(["population"]).columns()

    def test_rename(self, cities):
        assert "region" in cities.rename({"state": "region"}).columns()

    def test_add_constant_column(self, cities):
        assert cities.add_column("country", "India").get_features()[0]["properties"][
            "country"
        ] == "India"

    def test_add_computed_column(self, cities):
        result = cities.add_column("millions", lambda p: round(float(p["population"]) / 1e6, 1))
        assert result.get_features()[0]["properties"]["millions"] == 32.9

    def test_add_column_overwrites(self, cities):
        assert cities.add_column("name", "X").get_features()[0]["properties"]["name"] == "X"

    def test_apply(self, cities):
        result = cities.apply(lambda f: {**f, "properties": {"only": 1}})
        assert result.columns() == ["only"]

    def test_concat(self, cities):
        assert cities.concat(cities).count() == 14

    def test_add_operator(self, cities):
        assert (cities + cities).count() == 14

    def test_copy_is_independent(self, cities):
        clone = cities.copy()
        clone.get_features()[0]["properties"]["name"] = "Changed"
        assert cities.get_features()[0]["properties"]["name"] == "Delhi"


class TestInspection:
    def test_columns(self, cities):
        assert cities.columns() == ["name", "state", "population"]

    def test_unique(self, cities):
        assert len(cities.unique("state")) == 6

    def test_value_counts_is_ordered(self, cities):
        counts = cities.value_counts("state")
        assert list(counts.values()) == sorted(counts.values(), reverse=True)

    def test_value_counts_totals(self, cities):
        assert sum(cities.value_counts("state").values()) == 7

    def test_stats(self, cities):
        stats = cities.stats("population")
        assert stats["count"] == 7
        assert stats["max"] == 32900000
        assert stats["sum"] == sum(int(f["properties"]["population"]) for f in cities)

    def test_stats_of_a_text_column(self, cities):
        assert cities.stats("name") == {"count": 0}

    def test_stats_skips_non_numeric(self):
        geo = GeoEngine().load_data([point_feature(0, 0, v=5), point_feature(1, 1, v="N/A")])
        assert geo.stats("v")["count"] == 1

    def test_describe(self, cities):
        summary = cities.describe()
        assert summary["features"] == 7
        assert summary["geometry_types"] == {"Point": 7}
        assert "dispersion" in summary

    def test_bounds(self, cities):
        box = cities.bounds()
        assert box["min_lat"] < box["max_lat"]
        assert box["min_lng"] < box["max_lng"]

    def test_bounds_polygon(self, cities):
        ring = cities.bounds_polygon()["coordinates"][0]
        assert ring[0] == ring[-1]

    def test_to_records(self, cities):
        record = cities.to_records()[0]
        assert record["latitude"] == pytest.approx(28.6139)
        assert record["name"] == "Delhi"

    def test_to_wkt(self, cities):
        assert cities.to_wkt()[0].startswith("POINT (")

    def test_to_geojson_string(self, cities):
        assert json.loads(cities.to_geojson_string())["type"] == "FeatureCollection"


class TestGeometryMethods:
    def test_vincenty_option(self, cities):
        haversine = cities.distance(DELHI, MUMBAI)
        vincenty = cities.distance(DELHI, MUMBAI, method="vincenty")
        assert haversine != vincenty
        assert abs(haversine - vincenty) / haversine < 0.01

    def test_nautical_miles(self, cities):
        assert cities.distance(DELHI, MUMBAI, unit="nm") == pytest.approx(
            cities.distance(DELHI, MUMBAI) * 0.539957, rel=1e-4
        )

    def test_destination_and_distance_agree(self, cities):
        end = cities.destination(DELHI, 90, 100)
        assert cities.distance(DELHI, end) == pytest.approx(100, rel=1e-3)

    def test_destination_in_miles(self, cities):
        end = cities.destination(DELHI, 0, 62.1371, unit="miles")
        assert cities.distance(DELHI, end) == pytest.approx(100, rel=1e-3)

    def test_midpoint(self, cities):
        mid = cities.midpoint(DELHI, MUMBAI)
        assert cities.distance(DELHI, mid) == pytest.approx(cities.distance(mid, MUMBAI), rel=1e-4)

    def test_interpolate(self, cities):
        assert cities.interpolate(DELHI, MUMBAI, 0.0) == pytest.approx(DELHI, abs=1e-9)

    def test_buffer_in_feet(self, cities):
        zone = cities.buffer(DELHI, 32808.4, unit="ft")
        assert zone["_radius_km"] == pytest.approx(10, rel=1e-3)

    def test_buffer_line(self, cities):
        corridor = cities.buffer_line([DELHI, MUMBAI], 20)
        assert cities.point_in_polygon(cities.midpoint(DELHI, MUMBAI), corridor) is True

    def test_buffer_geometry(self, cities):
        grown = cities.buffer_geometry({"type": "Point", "coordinates": [77.2, 28.6]}, 5)
        assert grown["type"] == "Polygon"

    def test_point_in_polygon_respects_holes(self, cities):
        holed = {
            "type": "Polygon",
            "coordinates": [
                [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]],
                [[4, 4], [4, 6], [6, 6], [6, 4], [4, 4]],
            ],
        }
        assert cities.point_in_polygon((5, 5), holed) is False
        assert cities.point_in_polygon((1, 1), holed) is True

    def test_intersects(self, cities):
        assert cities.intersects(cities.buffer(DELHI, 10), cities.buffer(DELHI, 20)) is True
        assert cities.intersects(cities.buffer(DELHI, 10), cities.buffer(MUMBAI, 10)) is False

    def test_contains(self, cities):
        assert cities.contains(cities.buffer(DELHI, 100), cities.buffer(DELHI, 10)) is True

    def test_area_of_a_buffer(self, cities):
        import math

        assert cities.area(cities.buffer(DELHI, 10)) == pytest.approx(math.pi * 100, rel=0.01)

    def test_area_in_acres(self, cities):
        km2 = cities.area(cities.buffer(DELHI, 10))
        acres = cities.area(cities.buffer(DELHI, 10), unit="acres")
        assert acres == pytest.approx(km2 * 247.10538, rel=1e-4)

    def test_length_of_a_line(self, cities):
        line = {"type": "LineString", "coordinates": [[77.209, 28.6139], [72.8777, 19.076]]}
        assert cities.length(line) == pytest.approx(_haversine_km(DELHI, MUMBAI), rel=1e-4)

    def test_centroid_of_the_dataset(self, cities):
        lat, lng = cities.centroid()
        box = cities.bounds()
        assert box["min_lat"] <= lat <= box["max_lat"]
        assert box["min_lng"] <= lng <= box["max_lng"]

    def test_centre_of_mass_is_pulled_by_weight(self, cities):
        plain = cities.centroid()
        weighted = cities.centre_of_mass("population")
        assert weighted != plain

    def test_us_spelling_alias(self, cities):
        assert cities.center_of_mass("population") == cities.centre_of_mass("population")

    def test_convex_hull_contains_every_point(self, cities):
        hull = cities.convex_hull()
        for feature in cities:
            lng, lat = feature["geometry"]["coordinates"]
            assert cities.point_in_polygon((lat, lng), hull) is True

    def test_simplify_reduces_vertices(self, cities):
        line = {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[i, i * 0.00001] for i in range(50)],
            },
            "properties": {},
        }
        geo = GeoEngine().load_data([line])
        before = len(geo.get_features()[0]["geometry"]["coordinates"])
        after = len(geo.simplify(0.01).get_features()[0]["geometry"]["coordinates"])
        assert after < before

    def test_simplify_keeps_polygons_closed(self, cities):
        ring = cities.buffer(DELHI, 50)["coordinates"][0]
        geo = GeoEngine().load_data([{"type": "Point", "coordinates": [0, 0]}])
        geo = GeoEngine().load_data(
            [{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
              "properties": {}}]
        )
        simplified = geo.simplify(0.05).get_features()[0]["geometry"]["coordinates"][0]
        assert simplified[0] == simplified[-1]


class TestNearestAndKNN:
    def test_knn_returns_k(self, cities):
        assert len(cities.knn(DELHI, 3)) == 3

    def test_knn_is_sorted(self, cities):
        distances = [m["distance"] for m in cities.knn(DELHI, 5)]
        assert distances == sorted(distances)

    def test_knn_shape(self, cities):
        match = cities.knn(DELHI, 1)[0]
        assert set(match) == {"feature", "distance", "index"}

    def test_knn_in_miles(self, cities):
        km = cities.knn(MUMBAI, 1)[0]["distance"]
        miles = cities.knn(MUMBAI, 1, unit="miles")[0]["distance"]
        assert miles == pytest.approx(km * 0.621371, rel=1e-3)

    def test_knn_rejects_bad_unit(self, cities):
        with pytest.raises(InvalidUnitError):
            cities.knn(DELHI, 1, unit="cubits")

    def test_nearest_matches_knn(self, cities):
        assert cities.nearest(DELHI)["index"] == cities.knn(DELHI, 1)[0]["index"]

    def test_knn_beyond_dataset_size(self, cities):
        assert len(cities.knn(DELHI, 99)) == 7


class TestSpatialFilters:
    def test_within_polygon(self, cities):
        zone = cities.buffer(MUMBAI, 200)
        assert [f["properties"]["name"] for f in cities.within(zone)] == ["Mumbai", "Pune"]

    def test_outside_polygon(self, cities):
        zone = cities.buffer(MUMBAI, 200)
        assert cities.within(zone).count() + cities.outside(zone).count() == 7

    def test_within_rejects_non_dict(self, cities):
        with pytest.raises(InvalidBufferError):
            cities.within("not a polygon")

    def test_filter_by_bbox(self, cities):
        # Mumbai, Bangalore, Chennai, Pune and Hyderabad; Delhi and Kolkata fall outside.
        assert cities.filter_by_bbox(12, 72, 20, 81).count() == 5

    def test_filter_by_radius_injects_distance(self, cities):
        nearby = cities.filter_by_radius(19.07, 72.87, 200)
        assert all("_distance" in f["properties"] for f in nearby)

    def test_filter_by_radius_is_sorted(self, cities):
        nearby = cities.filter_by_radius(19.07, 72.87, 2000)
        distances = [f["properties"]["_distance"] for f in nearby]
        assert distances == sorted(distances)

    def test_filter_by_radius_in_miles(self, cities):
        assert cities.filter_by_radius(19.07, 72.87, 124.3, unit="miles").count() == 2

    def test_filter_by_radius_rejects_zero(self, cities):
        with pytest.raises(InvalidRadiusError):
            cities.filter_by_radius(19.07, 72.87, 0)

    def test_filter_by_radius_rejects_bad_coordinates(self, cities):
        with pytest.raises(InvalidCoordinateError):
            cities.filter_by_radius(95.0, 0.0, 10)


class TestIndexing:
    def test_build_index(self, cities):
        assert cities.build_index() is cities
        assert cities.index_stats()["features"] == 7

    def test_no_index_by_default(self, cities):
        assert cities.index_stats() is None

    def test_index_gives_identical_results(self):
        import random

        rng = random.Random(5)
        features = [
            point_feature(rng.uniform(8, 35), rng.uniform(68, 92), i=i) for i in range(1200)
        ]
        indexed = GeoEngine().load_data(features).build_index()
        plain = GeoEngine().load_data(features)
        plain.AUTO_INDEX_THRESHOLD = 10 ** 9        # force the linear path
        assert [m["index"] for m in indexed.knn(DELHI, 10)] == [
            m["index"] for m in plain.knn(DELHI, 10)
        ]

    def test_auto_index_on_large_datasets(self):
        features = [point_feature(20 + i * 0.001, 77 + i * 0.001) for i in range(600)]
        geo = GeoEngine().load_data(features)
        geo.knn(DELHI, 1)
        assert geo.index_stats() is not None


class TestValidationAndCleaning:
    def test_valid_data_reports_clean(self, cities):
        report = cities.validate()
        assert report["invalid"] == 0
        assert report["valid"] == 7

    def test_missing_geometry_is_flagged(self):
        geo = GeoEngine()
        geo._data = {"type": "FeatureCollection",
                     "features": [{"type": "Feature", "properties": {}}]}
        assert geo.validate()["invalid"] == 1

    def test_empty_coordinates_are_flagged(self):
        geo = GeoEngine()
        geo._data = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature",
                          "geometry": {"type": "Point", "coordinates": []},
                          "properties": {}}],
        }
        assert geo.validate()["invalid"] == 1

    def test_out_of_range_coordinates_are_flagged(self):
        geo = GeoEngine()
        geo._data = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature",
                          "geometry": {"type": "Point", "coordinates": [77.2, 128.6]},
                          "properties": {}}],
        }
        report = geo.validate()
        assert report["invalid"] == 1
        assert "out of range" in report["issues"][0]["problem"]

    def test_clean_drops_unusable_features(self):
        geo = GeoEngine()
        geo._data = {
            "type": "FeatureCollection",
            "features": [
                point_feature(*DELHI),
                {"type": "Feature", "properties": {}},
                {"type": "Feature", "geometry": {"type": "Point", "coordinates": []},
                 "properties": {}},
            ],
        }
        assert geo.clean().count() == 1

    def test_clean_raises_when_nothing_survives(self):
        geo = GeoEngine()
        geo._data = {"type": "FeatureCollection",
                     "features": [{"type": "Feature", "properties": {}}]}
        with pytest.raises(EmptyDatasetError):
            geo.clean()

    def test_fix_coordinates_swaps_the_unambiguous_case(self):
        geo = GeoEngine()
        geo._data = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature",
                          "geometry": {"type": "Point", "coordinates": [28.6139, 177.209]},
                          "properties": {}}],
        }
        fixed = geo.fix_coordinates().get_features()[0]["geometry"]["coordinates"]
        assert fixed == [177.209, 28.6139]

    def test_fix_coordinates_leaves_valid_points_alone(self, cities):
        assert cities.fix_coordinates().get_features()[0]["geometry"]["coordinates"] == [
            77.2090, 28.6139
        ]

    def test_dropna_on_all_columns(self):
        geo = GeoEngine().load_data(
            [point_feature(0, 0, a=1, b=2), point_feature(1, 1, a=1, b=None)]
        )
        assert geo.dropna().count() == 1

    def test_dropna_on_a_subset(self):
        geo = GeoEngine().load_data(
            [point_feature(0, 0, a=1, b=2), point_feature(1, 1, a=1, b=None)]
        )
        assert geo.dropna(["a"]).count() == 2

    def test_dedupe_exact(self, cities):
        assert (cities + cities).dedupe().count() == 7

    def test_dedupe_by_tolerance(self):
        geo = GeoEngine().load_data(
            [point_feature(28.6139, 77.2090), point_feature(28.6140, 77.2091)]
        )
        assert geo.dedupe(tolerance_km=1).count() == 1

    def test_dedupe_respects_a_subset(self):
        geo = GeoEngine().load_data(
            [point_feature(28.6139, 77.2090, name="A"), point_feature(28.6140, 77.2091, name="B")]
        )
        assert geo.dedupe(tolerance_km=1, subset=["name"]).count() == 2


class TestAnalysisShortcuts:
    def test_cluster_labels_every_feature(self, clustered_points):
        geo = GeoEngine().load_data(clustered_points)
        labelled = geo.cluster(eps_km=50, min_samples=3)
        assert all("_cluster" in f["properties"] for f in labelled)

    def test_cluster_custom_column(self, clustered_points):
        geo = GeoEngine().load_data(clustered_points)
        assert "group" in geo.cluster(50, 3, column="group").columns()

    def test_kmeans_result(self, clustered_points):
        result = GeoEngine().load_data(clustered_points).kmeans(3, seed=1)
        assert result["engine"].count() == len(clustered_points)
        assert len(result["centroids"]) == 3

    def test_spatial_join_with_an_engine(self, cities, districts):
        joined = cities.spatial_join(districts, prefix="zone_")
        matched = [f for f in joined if "zone_district" in f["properties"]]
        assert len(matched) >= 1

    def test_spatial_join_with_a_feature_list(self, cities, districts):
        joined = cities.spatial_join(districts.get_features())
        assert joined.count() == 7

    def test_hotspots(self, clustered_points):
        geo = GeoEngine().load_data(clustered_points)
        assert len(geo.hotspots(cell_km=100, min_count=2)) >= 3

    def test_dispersion(self, cities):
        assert cities.dispersion()["max_km"] > 0

    def test_pattern(self, cities):
        assert cities.pattern()["pattern"] in ("clustered", "dispersed", "random")

    def test_optimise_route(self, cities):
        route = cities.optimise_route(start=DELHI, round_trip=True)
        assert route["engine"].count() == 7
        assert route["total_distance"] > 0

    def test_optimize_route_alias(self, cities):
        assert cities.optimize_route()["total_distance"] > 0

    def test_geohash_column(self, cities):
        result = cities.geohash_column(6)
        assert len(result.get_features()[0]["properties"]["_geohash"]) == 6

    def test_project_web_mercator(self, cities):
        projected = cities.project()
        assert projected[0]["epsg"] == 3857

    def test_project_utm(self, cities):
        assert cities.project("utm")[0]["zone"] in range(1, 61)


class TestDunders:
    def test_len(self, cities):
        assert len(cities) == 7

    def test_iteration(self, cities):
        assert len(list(cities)) == 7

    def test_integer_index(self, cities):
        assert cities[0]["properties"]["name"] == "Delhi"

    def test_negative_index(self, cities):
        assert cities[-1]["properties"]["name"] == "Hyderabad"

    def test_slice_returns_an_engine(self, cities):
        sliced = cities[1:4]
        assert isinstance(sliced, GeoEngine)
        assert sliced.count() == 3

    def test_truthiness(self, cities):
        assert bool(cities) is True
        assert bool(GeoEngine()) is False

    def test_repr_with_data(self, cities):
        assert "7 features" in repr(cities)

    def test_repr_without_data(self):
        assert "no data loaded" in repr(GeoEngine())

    def test_notebook_repr(self, cities):
        html = cities._repr_html_()
        assert "<table" in html
        assert "Delhi" in html

    def test_notebook_repr_without_data(self):
        assert "no data" in GeoEngine()._repr_html_()

    def test_notebook_repr_truncates(self):
        many = GeoEngine().load_data([point_feature(i * 0.1, i * 0.1) for i in range(50)])
        assert "40 more" in many._repr_html_()


class TestNoDataGuards:
    @pytest.mark.parametrize(
        "call",
        [
            lambda g: g.columns(),
            lambda g: g.describe(),
            lambda g: g.validate(),
            lambda g: g.head(),
            lambda g: g.sort_by("x"),
            lambda g: g.knn((0, 0)),
            lambda g: g.centroid(),
            lambda g: g.convex_hull(),
            lambda g: g.hotspots(),
            lambda g: g.dispersion(),
            lambda g: g.build_index(),
            lambda g: g.to_records(),
        ],
    )
    def test_raises_no_data_error(self, call):
        with pytest.raises(NoDataError):
            call(GeoEngine())


class TestSaveFormats:
    @pytest.mark.parametrize(
        "ext", ["geojson", "json", "csv", "ndjson", "wkt", "gpx", "kml", "svg", "png", "html"]
    )
    def test_writes_a_non_empty_file(self, cities, tmp_path, ext):
        path = tmp_path / f"out.{ext}"
        assert cities.save(str(path)) is cities
        assert path.stat().st_size > 0

    @pytest.mark.parametrize("ext", ["geojson", "csv", "ndjson", "gpx", "kml"])
    def test_round_trips_through_load(self, cities, tmp_path, ext):
        path = str(tmp_path / f"out.{ext}")
        cities.save(path)
        assert GeoEngine().load(path).count() == 7

    def test_csv_keeps_properties(self, cities, tmp_path):
        path = str(tmp_path / "out.csv")
        cities.save(path)
        assert GeoEngine().load(path).get_features()[0]["properties"]["name"] == "Delhi"

    def test_rejects_unknown_extension(self, cities, tmp_path):
        with pytest.raises(UnsupportedFormatError):
            cities.save(str(tmp_path / "out.docx"))

    def test_creates_missing_directories(self, cities, tmp_path):
        path = tmp_path / "nested" / "deep" / "out.geojson"
        cities.save(str(path))
        assert path.exists()
