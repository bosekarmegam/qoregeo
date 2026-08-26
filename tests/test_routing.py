"""Route optimisation — greedy tour plus 2-opt improvement."""

from __future__ import annotations

import random
from itertools import permutations

import pytest

from qoregeo.exceptions import EmptyDatasetError, InvalidCoordinateError
from qoregeo.routing import (
    _matrix,
    _tour_length,
    nearest_neighbour_tour,
    optimise_route,
    route_line,
    travel_time,
    two_opt,
)
from qoregeo.utils import _haversine_km

from .conftest import BANGALORE, CHENNAI, DELHI, KOLKATA, MUMBAI

SIX_CITIES = [DELHI, MUMBAI, CHENNAI, KOLKATA, BANGALORE, (17.3850, 78.4867)]


def tour_length(points, round_trip=True):
    total = sum(_haversine_km(points[i], points[i + 1]) for i in range(len(points) - 1))
    return total + (_haversine_km(points[-1], points[0]) if round_trip else 0)


class TestMatrix:
    def test_is_symmetric(self):
        matrix = _matrix(SIX_CITIES)
        assert all(matrix[i][j] == matrix[j][i] for i in range(6) for j in range(6))

    def test_diagonal_is_zero(self):
        assert all(row[i] == 0 for i, row in enumerate(_matrix(SIX_CITIES)))

    def test_matches_haversine(self):
        assert _matrix(SIX_CITIES)[0][1] == pytest.approx(_haversine_km(DELHI, MUMBAI))


class TestGreedyTour:
    def test_visits_every_stop_once(self):
        order = nearest_neighbour_tour(_matrix(SIX_CITIES))
        assert sorted(order) == list(range(6))

    def test_starts_where_told(self):
        assert nearest_neighbour_tour(_matrix(SIX_CITIES), start=3)[0] == 3

    def test_first_hop_is_the_closest(self):
        matrix = _matrix(SIX_CITIES)
        order = nearest_neighbour_tour(matrix, start=0)
        assert order[1] == min(range(1, 6), key=lambda j: matrix[0][j])


class TestTwoOpt:
    def test_never_lengthens_the_tour(self):
        matrix = _matrix(SIX_CITIES)
        greedy = nearest_neighbour_tour(matrix)
        improved = two_opt(list(greedy), matrix, round_trip=True)
        assert _tour_length(improved, matrix, True) <= _tour_length(greedy, matrix, True) + 1e-9

    def test_keeps_every_stop(self):
        matrix = _matrix(SIX_CITIES)
        improved = two_opt(nearest_neighbour_tour(matrix), matrix, round_trip=True)
        assert sorted(improved) == list(range(6))

    def test_holds_the_start_fixed(self):
        matrix = _matrix(SIX_CITIES)
        improved = two_opt(nearest_neighbour_tour(matrix, 2), matrix, fixed_start=True)
        assert improved[0] == 2

    def test_short_tours_pass_through(self):
        matrix = _matrix(SIX_CITIES[:3])
        assert two_opt([0, 1, 2], matrix) == [0, 1, 2]

    def test_untangles_a_scrambled_ring(self):
        # Eight points on a circle: the optimal tour walks round in order, so a
        # scrambled visiting order is full of crossings for 2-opt to remove.
        import math

        ring = [
            (5 * math.cos(math.radians(a)), 5 * math.sin(math.radians(a)))
            for a in range(0, 360, 45)
        ]
        matrix = _matrix(ring)
        scrambled = [0, 3, 6, 1, 4, 7, 2, 5]
        improved = two_opt(list(scrambled), matrix, round_trip=True)
        assert _tour_length(improved, matrix, True) < _tour_length(scrambled, matrix, True)
        # The perimeter is the optimum; 2-opt should land on it.
        assert _tour_length(improved, matrix, True) == pytest.approx(
            _tour_length(list(range(8)), matrix, True), rel=1e-6
        )

    def test_untangles_a_crossed_square(self):
        # The smallest possible crossing. Fixing it needs the reversal of a
        # two-stop segment — the move an off-by-one `j - i == 1` guard would
        # skip, leaving the tour crossed forever.
        square = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)]
        matrix = _matrix(square)
        improved = two_opt([0, 2, 1, 3], matrix, round_trip=True)
        assert _tour_length(improved, matrix, True) == pytest.approx(
            _tour_length([0, 1, 2, 3], matrix, True), rel=1e-9
        )

    def test_adjacent_swaps_are_considered(self):
        # Two stops out of order, everything else already optimal.
        stops = [(0.0, float(i)) for i in range(6)]
        matrix = _matrix(stops)
        swapped = [0, 1, 3, 2, 4, 5]
        improved = two_opt(list(swapped), matrix, round_trip=False)
        assert _tour_length(improved, matrix, False) < _tour_length(swapped, matrix, False)


class TestOptimiseRoute:
    def test_matches_brute_force_on_six_cities(self):
        route = optimise_route(SIX_CITIES, start=DELHI, round_trip=True)
        optimal = min(
            tour_length([SIX_CITIES[0]] + list(perm))
            for perm in permutations(SIX_CITIES[1:])
        )
        assert route["total_distance"] == pytest.approx(optimal, rel=1e-6)

    def test_beats_greedy_on_a_larger_problem(self):
        rng = random.Random(3)
        stops = [(rng.uniform(10, 30), rng.uniform(70, 88)) for _ in range(30)]
        greedy = optimise_route(stops, improve=False)["total_distance"]
        improved = optimise_route(stops, improve=True)["total_distance"]
        assert improved < greedy

    def test_improvement_percentage_is_reported(self):
        rng = random.Random(5)
        stops = [(rng.uniform(10, 30), rng.uniform(70, 88)) for _ in range(20)]
        assert optimise_route(stops)["improvement_pct"] >= 0

    def test_order_indexes_the_input(self):
        route = optimise_route(SIX_CITIES)
        assert sorted(route["order"]) == list(range(6))

    def test_depot_outside_the_stops_is_prepended(self):
        route = optimise_route(SIX_CITIES[1:], start=DELHI)
        assert route["points"][0] == DELHI
        assert sorted(route["order"]) == list(range(5))

    def test_depot_inside_the_stops_starts_the_tour(self):
        route = optimise_route(SIX_CITIES, start=CHENNAI)
        assert route["points"][0] == CHENNAI

    def test_round_trip_returns_home(self):
        route = optimise_route(SIX_CITIES, start=DELHI, round_trip=True)
        assert route["legs"][-1]["to"] == DELHI

    def test_open_route_does_not(self):
        route = optimise_route(SIX_CITIES, start=DELHI, round_trip=False)
        assert route["legs"][-1]["to"] != DELHI

    def test_round_trip_is_longer(self):
        closed = optimise_route(SIX_CITIES, start=DELHI, round_trip=True)["total_distance"]
        open_route = optimise_route(SIX_CITIES, start=DELHI, round_trip=False)["total_distance"]
        assert closed > open_route

    def test_leg_count(self):
        assert len(optimise_route(SIX_CITIES, round_trip=False)["legs"]) == 5
        assert len(optimise_route(SIX_CITIES, round_trip=True)["legs"]) == 6

    def test_legs_carry_bearings(self):
        leg = optimise_route(SIX_CITIES)["legs"][0]
        assert 0 <= leg["bearing"] < 360
        assert isinstance(leg["direction"], str)

    def test_leg_distances_sum_to_the_total(self):
        route = optimise_route(SIX_CITIES, round_trip=True)
        assert sum(leg["distance"] for leg in route["legs"]) == pytest.approx(
            route["total_distance"], rel=1e-4
        )

    def test_unit_conversion(self):
        km = optimise_route(SIX_CITIES, start=DELHI)["total_distance"]
        miles = optimise_route(SIX_CITIES, start=DELHI, unit="miles")["total_distance"]
        assert miles == pytest.approx(km * 0.621371, rel=1e-4)

    def test_single_stop(self):
        route = optimise_route([DELHI])
        assert route["total_distance"] == 0.0
        assert route["legs"] == []

    def test_two_stops(self):
        route = optimise_route([DELHI, MUMBAI])
        assert route["total_distance"] == pytest.approx(_haversine_km(DELHI, MUMBAI), rel=1e-4)

    def test_no_stops_raises(self):
        with pytest.raises(EmptyDatasetError):
            optimise_route([])

    def test_invalid_coordinate_raises(self):
        with pytest.raises(InvalidCoordinateError):
            optimise_route([(95.0, 0.0), MUMBAI])


class TestRouteLine:
    def test_line_has_a_point_per_stop(self):
        route = optimise_route(SIX_CITIES, round_trip=False)
        assert len(route_line(route)["coordinates"]) == 6

    def test_round_trip_line_closes(self):
        line = route_line(optimise_route(SIX_CITIES, round_trip=True))
        assert line["coordinates"][0] == line["coordinates"][-1]

    def test_coordinates_are_geojson_order(self):
        line = route_line(optimise_route([DELHI]))
        assert line["coordinates"][0] == [DELHI[1], DELHI[0]]


class TestTravelTime:
    def test_driving_time(self):
        assert travel_time(120, speed_kmh=60)["driving_minutes"] == pytest.approx(120)

    def test_stop_time_is_added(self):
        result = travel_time(60, speed_kmh=60, stop_minutes=10, stops=5)
        assert result["stop_minutes"] == 50
        assert result["total_minutes"] == pytest.approx(110)

    def test_hours_match_minutes(self):
        result = travel_time(120, speed_kmh=60)
        assert result["total_hours"] == pytest.approx(result["total_minutes"] / 60)

    def test_negative_stops_are_ignored(self):
        assert travel_time(60, 60, 10, -5)["stop_minutes"] == 0

    def test_zero_speed_raises(self):
        with pytest.raises(ValueError):
            travel_time(10, speed_kmh=0)
