"""Shared fixtures for the QOREgeo test suite."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from qoregeo import GeoEngine

# Reference coordinates used across the suite.
DELHI = (28.6139, 77.2090)
MUMBAI = (19.0760, 72.8777)
CHENNAI = (13.0827, 80.2707)
BANGALORE = (12.9716, 77.5946)
KOLKATA = (22.5726, 88.3639)
LONDON = (51.5074, -0.1278)
NYC = (40.7128, -74.0060)
SYDNEY = (-33.8688, 151.2093)

CITY_ROWS: List[Dict[str, Any]] = [
    {"name": "Delhi", "state": "Delhi", "population": 32900000,
     "latitude": 28.6139, "longitude": 77.2090},
    {"name": "Mumbai", "state": "Maharashtra", "population": 20700000,
     "latitude": 19.0760, "longitude": 72.8777},
    {"name": "Bangalore", "state": "Karnataka", "population": 13200000,
     "latitude": 12.9716, "longitude": 77.5946},
    {"name": "Kolkata", "state": "West Bengal", "population": 14900000,
     "latitude": 22.5726, "longitude": 88.3639},
    {"name": "Chennai", "state": "Tamil Nadu", "population": 11300000,
     "latitude": 13.0827, "longitude": 80.2707},
    {"name": "Pune", "state": "Maharashtra", "population": 7400000,
     "latitude": 18.5204, "longitude": 73.8567},
    {"name": "Hyderabad", "state": "Telangana", "population": 10500000,
     "latitude": 17.3850, "longitude": 78.4867},
]


def point_feature(lat: float, lng: float, **props: Any) -> Dict[str, Any]:
    """Build a GeoJSON Point Feature, the workhorse of these tests."""
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lng, lat]},
        "properties": dict(props),
    }


def polygon_feature(ring: List[List[float]], **props: Any) -> Dict[str, Any]:
    """Build a GeoJSON Polygon Feature from a single [lng, lat] ring."""
    closed = list(ring)
    if closed[0] != closed[-1]:
        closed.append(list(closed[0]))
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [closed]},
        "properties": dict(props),
    }


@pytest.fixture
def cities_csv(tmp_path: Path) -> str:
    """A seven-city CSV, written fresh for each test."""
    path = tmp_path / "cities.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CITY_ROWS[0].keys()))
        writer.writeheader()
        writer.writerows(CITY_ROWS)
    return str(path)


@pytest.fixture
def cities(cities_csv: str) -> GeoEngine:
    """A GeoEngine loaded with the seven-city CSV."""
    return GeoEngine().load(cities_csv)


@pytest.fixture
def districts() -> GeoEngine:
    """Two non-overlapping polygons covering parts of India."""
    north = polygon_feature(
        [[76.0, 27.5], [79.0, 27.5], [79.0, 30.0], [76.0, 30.0]],
        district="North", code="N1",
    )
    west = polygon_feature(
        [[72.0, 18.0], [75.0, 18.0], [75.0, 20.0], [72.0, 20.0]],
        district="West", code="W1",
    )
    return GeoEngine().load_data([north, west])


@pytest.fixture
def clustered_points() -> List[Dict[str, Any]]:
    """Three tight blobs plus two far-flung outliers, a DBSCAN test case."""
    features: List[Dict[str, Any]] = []
    for index, (lat, lng) in enumerate([DELHI, MUMBAI, CHENNAI]):
        for step in range(8):
            features.append(
                point_feature(
                    lat + step * 0.002,
                    lng + step * 0.002,
                    blob=index,
                    name=f"blob{index}-{step}",
                )
            )
    features.append(point_feature(0.0, 0.0, blob=-1, name="null-island"))
    features.append(point_feature(*SYDNEY, blob=-1, name="sydney"))
    return features


@pytest.fixture
def geojson_file(tmp_path: Path) -> str:
    """A small FeatureCollection on disk."""
    collection = {
        "type": "FeatureCollection",
        "features": [
            point_feature(*DELHI, name="Delhi", pop=32900000),
            point_feature(*MUMBAI, name="Mumbai", pop=20700000),
        ],
    }
    path = tmp_path / "cities.geojson"
    path.write_text(json.dumps(collection), encoding="utf-8")
    return str(path)
