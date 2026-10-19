"""
Tests for qoregeo.geocode — fully offline.

Every HTTP request goes through ``Geocoder._fetch`` (or, for the one test of
``_fetch`` itself, ``urllib.request.urlopen``), both of which are replaced
here; an autouse fixture makes any real network access fail loudly. Time is
faked, so the 1 request/second throttle costs no wall-clock time. The on-disk
state shared by processes (request slot + answers) lives in a per-test temporary
directory; one test starts two real subprocesses with a short interval.
"""

from __future__ import annotations

import email.message
import http.client
import io
import json
import os
import pickle
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit

import pytest

from qoregeo import geocode as gmod
from qoregeo._version import __version__
from qoregeo.exceptions import (
    GeocodingError,
    InvalidArgumentError,
    InvalidCoordinateError,
    QOREgeoError,
)
from qoregeo.geocode import Geocoder

# ─────────────────────────────────────────────────────────────────────────────
# Recorded responses (trimmed real-world samples)
# ─────────────────────────────────────────────────────────────────────────────
LICENCE = "Data © OpenStreetMap contributors, ODbL 1.0. http://osm.org/copyright"

NOMINATIM_SEARCH_BERLIN = [
    {
        "place_id": 133863914,
        "licence": LICENCE,
        "osm_type": "relation",
        "osm_id": 62422,
        "lat": "52.5173885",
        "lon": "13.3951309",
        "category": "boundary",
        "type": "administrative",
        "place_rank": 8,
        "importance": 0.8875390282491362,
        "addresstype": "city",
        "name": "Berlin",
        "display_name": "Berlin, Deutschland",
        "address": {
            "city": "Berlin",
            "ISO3166-2-lvl4": "DE-BE",
            "country": "Deutschland",
            "country_code": "de",
        },
        "boundingbox": ["52.3382448", "52.6755087", "13.0883450", "13.7611609"],
    },
    {
        "place_id": 297262393,
        "licence": LICENCE,
        "osm_type": "relation",
        "osm_id": 61315,
        "lat": "44.4686711",
        "lon": "-71.1850869",
        "category": "boundary",
        "type": "administrative",
        "place_rank": 16,
        "importance": 0.4785198283396634,
        "addresstype": "city",
        "name": "Berlin",
        "display_name": "Berlin, Coös County, New Hampshire, 03570, United States",
        "address": {
            "city": "Berlin",
            "county": "Coös County",
            "state": "New Hampshire",
            "ISO3166-2-lvl4": "US-NH",
            "postcode": "03570",
            "country": "United States",
            "country_code": "us",
        },
        "boundingbox": ["44.4204550", "44.5394840", "-71.2741730", "-71.1253510"],
    },
]

NOMINATIM_SEARCH_BAKERY = [
    {
        "address": {
            "ISO3166-2-lvl4": "DE-BE",
            "borough": "Mitte",
            "city": "Berlin",
            "country": "Deutschland",
            "country_code": "de",
            "neighbourhood": "Sprengelkiez",
            "postcode": "13347",
            "road": "Lindower Straße",
            "shop": "Ditsch",
            "suburb": "Wedding",
        },
        "addresstype": "shop",
        "boundingbox": ["52.5427201", "52.5427654", "13.3668619", "13.3669442"],
        "category": "shop",
        "display_name": "Ditsch, Lindower Straße, Sprengelkiez, Wedding, Mitte, Berlin, "
                        "13347, Deutschland",
        "importance": 9.99999999995449e-06,
        "lat": "52.54274275",
        "licence": LICENCE,
        "lon": "13.36690305710228",
        "name": "Ditsch",
        "osm_id": 437595031,
        "osm_type": "way",
        "place_id": 204751033,
        "place_rank": 30,
        "type": "bakery",
    }
]

NOMINATIM_REVERSE = {
    "place_id": "134140761",
    "licence": "Data © OpenStreetMap contributors, ODbL 1.0. "
               "https://www.openstreetmap.org/copyright",
    "osm_type": "way",
    "osm_id": "280940520",
    "lat": "-34.4391708",
    "lon": "-58.7064573",
    "place_rank": "26",
    "category": "highway",
    "type": "motorway",
    "importance": "0.1",
    "addresstype": "road",
    "display_name": "Autopista Pedro Eugenio Aramburu, El Triángulo, Partido de Malvinas "
                    "Argentinas, Buenos Aires, 1.619, Argentina",
    "name": "Autopista Pedro Eugenio Aramburu",
    "address": {
        "road": "Autopista Pedro Eugenio Aramburu",
        "village": "El Triángulo",
        "state_district": "Partido de Malvinas Argentinas",
        "state": "Buenos Aires",
        "postcode": "1.619",
        "country": "Argentina",
        "country_code": "ar",
    },
    "boundingbox": ["-34.44159", "-34.4370994", "-58.7086067", "-58.7044712"],
}

NOMINATIM_REVERSE_NOTHING = {"error": "Unable to geocode"}

PHOTON_SEARCH_BERLIN = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [13.3888599, 52.5170365]},
            "properties": {
                "osm_type": "N",
                "osm_id": 240109189,
                "osm_key": "place",
                "osm_value": "city",
                "type": "city",
                "name": "Berlin",
                "state": "Berlin",
                "country": "Germany",
                "countrycode": "DE",
                "extent": [13.088345, 52.6755087, 13.7611609, 52.3382448],
            },
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [13.239514674078611, 52.51467945]},
            "properties": {
                "osm_type": "W",
                "osm_id": 38862723,
                "osm_key": "leisure",
                "osm_value": "stadium",
                "type": "house",
                "name": "Berlin Olympic Stadium",
                "street": "Olympischer Platz",
                "housenumber": "3",
                "postcode": "14053",
                "district": "Westend",
                "city": "Berlin",
                "state": "Berlin",
                "country": "Germany",
                "countrycode": "DE",
                "extent": [13.23727, 52.5157151, 13.241757, 52.5135972],
            },
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-71.1850869, 44.4686711]},
            "properties": {
                "osm_type": "R",
                "osm_id": 61315,
                "osm_key": "place",
                "osm_value": "city",
                "type": "city",
                "name": "Berlin",
                "county": "Coös County",
                "state": "New Hampshire",
                "country": "United States",
                "countrycode": "US",
            },
        },
    ],
}

PHOTON_REVERSE_EIFFEL = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [2.2944990543196795, 48.8582602]},
            "properties": {
                "osm_type": "W",
                "osm_id": 5013364,
                "osm_key": "man_made",
                "osm_value": "tower",
                "type": "house",
                "name": "Tour Eiffel",
                "housenumber": "5",
                "street": "Avenue Gustave Eiffel",
                "locality": "Quartier du Gros-Caillou",
                "district": "Gros-Caillou",
                "city": "Paris",
                "county": "Paris",
                "state": "Île-de-France",
                "postcode": "75007",
                "country": "France",
                "countrycode": "FR",
                "extent": [2.2933084, 48.8590465, 2.2956897, 48.8574753],
            },
        }
    ],
}

PHOTON_EMPTY = {"type": "FeatureCollection", "features": []}


def _bytes(payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _http_error(url: str, code: int, body: bytes = b"",
                headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    hdrs = email.message.Message()
    for key, value in (headers or {}).items():
        hdrs[key] = value
    return urllib.error.HTTPError(url, code, "error", hdrs, io.BytesIO(body))


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

# The real clock, for the test that runs subprocesses (the fixtures fake it).
_REAL_TIME = (time.time, time.monotonic, time.sleep)


class FakeClock:
    """Deterministic replacement for time.time / time.monotonic / time.sleep."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.advance_on_sleep = True
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            if self.advance_on_sleep:
                self.now += seconds


class FakeServer:
    """Stands in for Geocoder._fetch: records URLs and replays scripted answers."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.responses: list[Any] = []   # bytes, or an exception to raise
        self.default: Any = b"[]"
        self._lock = threading.Lock()

    def queue(self, *items: Any) -> None:
        self.responses.extend(items)

    def __call__(self, geocoder: Geocoder, url: str) -> bytes:
        with self._lock:
            self.urls.append(url)
            item = self.responses.pop(0) if self.responses else self.default
        if isinstance(item, BaseException):
            raise item
        return item if isinstance(item, bytes) else _bytes(item)

    def params(self, index: int = -1) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.urls[index]).query)

    def path(self, index: int = -1) -> str:
        parts = urlsplit(self.urls[index])
        return f"{parts.scheme}://{parts.netloc}{parts.path}"


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("test tried to access the network")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    gmod._reset_rate_limits()


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(time, "time", fake.monotonic)    # one clock for both
    monkeypatch.setattr(time, "monotonic", fake.monotonic)
    monkeypatch.setattr(time, "sleep", fake.sleep)
    return fake


@pytest.fixture
def cache_mode() -> str:
    """"disk": the default on-disk state, in a temporary directory; "memory": off.
    Classes parametrize it to run their tests both ways."""
    return "disk"


@pytest.fixture(autouse=True)
def cache_home(cache_mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never touch the real user cache: point QOREGEO_CACHE_DIR at tmp_path."""
    home = tmp_path / "qoregeo-cache"
    monkeypatch.setenv("QOREGEO_CACHE_DIR", str(home) if cache_mode == "disk" else "off")
    return home


def _rows(home: Path, table: str = "results") -> int:
    con = sqlite3.connect(home / "geocode-v1.sqlite3")
    try:
        return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        con.close()


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    fake = FakeServer()

    def fetch(self: Geocoder, url: str) -> bytes:
        return fake(self, url)

    monkeypatch.setattr(Geocoder, "_fetch", fetch)
    return fake


# ─────────────────────────────────────────────────────────────────────────────
# Construction
# ─────────────────────────────────────────────────────────────────────────────

class TestConstruction:
    def test_defaults(self) -> None:
        gc = Geocoder()
        assert gc.provider == "nominatim"
        assert gc.url == "https://nominatim.openstreetmap.org"
        assert gc.user_agent == f"qoregeo/{__version__} (+https://github.com/bosekarmegam/qoregeo)"
        assert gc.user_agent == gmod.DEFAULT_USER_AGENT
        assert gc.is_public and gc.min_interval == 1.0
        assert gc.timeout == 10.0 and gc.cache_size == 1024
        assert gc.email is None and gc.language is None
        assert repr(gc) == "Geocoder(provider='nominatim', url='https://nominatim.openstreetmap.org')"
        assert gc.cache_dir == os.environ["QOREGEO_CACHE_DIR"]   # see the cache_home fixture

    def test_photon_defaults(self) -> None:
        gc = Geocoder(provider=" Photon ")
        assert gc.provider == "photon"
        assert gc.url == "https://photon.komoot.io"
        assert gc.is_public and gc.min_interval == 1.0

    def test_providers_listed(self) -> None:
        assert gmod.PROVIDERS == ("nominatim", "photon")

    def test_custom_url_is_not_throttled_by_default(self) -> None:
        gc = Geocoder(url="http://localhost:8080/nominatim/")
        assert gc.url == "http://localhost:8080/nominatim"
        assert not gc.is_public and gc.min_interval == 0.0

    def test_custom_url_min_interval(self) -> None:
        assert Geocoder(url="http://geo.internal", min_interval=0.25).min_interval == 0.25

    @pytest.mark.parametrize("value, expected", [(0, 1.0), (0.2, 1.0), (1, 1.0), (3.5, 3.5)])
    def test_public_endpoint_never_below_one_second(self, value: float, expected: float) -> None:
        assert Geocoder(min_interval=value).min_interval == expected
        assert Geocoder("photon", min_interval=value).min_interval == expected

    def test_public_host_detected_even_with_explicit_url(self) -> None:
        gc = Geocoder(url="https://NOMINATIM.openstreetmap.org/", min_interval=0)
        assert gc.is_public and gc.min_interval == 1.0

    def test_custom_identity(self) -> None:
        gc = Geocoder(user_agent=" my-app/2.0 (ops@example.org) ", email="ops@example.org",
                      language="de,en;q=0.8", timeout=3, cache_size=0)
        assert gc.user_agent == "my-app/2.0 (ops@example.org)"
        assert gc.email == "ops@example.org"
        assert gc.language == "de,en;q=0.8"
        assert gc.timeout == 3.0 and gc.cache_size == 0

    @pytest.mark.parametrize("kwargs", [
        {"provider": "google"},
        {"provider": ""},
        {"provider": None},
        {"url": "ftp://example.com"},
        {"url": "localhost:8080"},
        {"url": "http://"},
        {"url": "https://example.com/search?q=x"},
        {"url": "https://example.com/#top"},
        {"url": "http://exa mple.com"},
        {"url": "http://example.com:99999"},
        {"url": ""},
        {"url": 42},
        {"user_agent": ""},
        {"user_agent": "   "},
        {"user_agent": "app\r\nX-Evil: 1"},
        {"user_agent": 7},
        {"email": "not-an-email"},
        {"email": "a@"},
        {"email": "a b@example.com"},
        {"email": "a@example.com\n"},
        {"min_interval": -1},
        {"min_interval": float("nan")},
        {"min_interval": float("inf")},
        {"min_interval": "1"},
        {"min_interval": True},
        {"timeout": 0},
        {"timeout": -5},
        {"timeout": float("inf")},
        {"timeout": None},
        {"timeout": True},
        {"cache_size": -1},
        {"cache_size": 1.5},
        {"cache_size": True},
        {"language": ""},
        {"language": "en\n"},
        {"language": "en,"},
        {"language": "en;q=x"},
        {"language": ["en"]},
        # found in review: these used to pass validation and then fail inside urllib,
        # build a broken request URL, leak credentials or overflow time.sleep()
        {"url": "http://example.com/a?"},
        {"url": "http://example.com/a#"},
        {"url": "http://user:secret@example.com"},
        {"url": "http://example.com/a\x7f"},
        {"url": "http://example.com\\geo"},
        {"url": "http://exa\u200bmple.com"},
        {"url": "http://example.com/a\u00a0b"},
        {"url": "http://[::1"},
        {"user_agent": "app \u2603 1.0"},
        {"user_agent": "app\x85x"},
        {"user_agent": "app\x7f"},
        {"email": "a\udcff@example.com"},
        {"timeout": 1e300},
        {"timeout": 86_401},
        {"min_interval": 1e12},
        {"language": "en;q=1.5"},
        {"language": "en;q=0.1234"},
        {"language": "toolongsubtag"},
        {"language": "-"},
        {"language": "en--us"},
        {"cache_dir": 5},
        {"cache_dir": ""},
        {"cache_dir": "  "},
        {"cache_dir": b"/tmp/geo"},
        {"cache_dir": "geo\x00cache"},
        {"cache_dir": ["/tmp"]},
    ])
    def test_invalid_arguments(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(InvalidArgumentError) as info:
            Geocoder(**kwargs)
        assert isinstance(info.value, ValueError)
        assert info.value.name == next(iter(kwargs))
        assert "Fix it" in str(info.value)


# ─────────────────────────────────────────────────────────────────────────────
# Nominatim
# ─────────────────────────────────────────────────────────────────────────────

class TestNominatimSearch:
    def test_request(self, server: FakeServer) -> None:
        server.queue(NOMINATIM_SEARCH_BERLIN)
        Geocoder(email="me@example.com", language="de").search(
            "  Berlin  ", limit=2, country_codes=["DE", "us", "de"])
        assert server.path() == "https://nominatim.openstreetmap.org/search"
        assert server.params() == {
            "q": ["Berlin"], "format": ["jsonv2"], "addressdetails": ["1"], "limit": ["2"],
            "countrycodes": ["de,us"], "accept-language": ["de"], "email": ["me@example.com"],
        }

    def test_minimal_request_has_no_optional_params(self, server: FakeServer) -> None:
        Geocoder().search("Berlin")
        params = server.params()
        assert params["limit"] == ["5"]
        for key in ("countrycodes", "accept-language", "email"):
            assert key not in params

    def test_country_codes_string_forms(self, server: FakeServer) -> None:
        gc = Geocoder(url="http://localhost:8080")
        gc.search("x", country_codes="FR")
        assert server.params()["countrycodes"] == ["fr"]
        gc.search("x", country_codes=" de , at ")
        assert server.params()["countrycodes"] == ["de,at"]
        gc.search("x", country_codes=("ch",))  # type: ignore[arg-type]
        assert server.params()["countrycodes"] == ["ch"]

    def test_parse(self, server: FakeServer) -> None:
        server.queue(NOMINATIM_SEARCH_BERLIN)
        results = Geocoder().search("Berlin")
        assert len(results) == 2
        first = results[0]
        assert first == {
            "lat": 52.5173885,
            "lng": 13.3951309,
            "name": "Berlin, Deutschland",
            "category": "boundary",
            "type": "administrative",
            "importance": pytest.approx(0.8875390282491362),
            "bbox": [13.088345, 52.3382448, 13.7611609, 52.6755087],
            "address": {"city": "Berlin", "ISO3166-2-lvl4": "DE-BE",
                        "country": "Deutschland", "country_code": "de"},
            "provider": "nominatim",
            "attribution": LICENCE,
        }
        west, south, east, north = results[1]["bbox"]
        assert west < east and south < north
        assert results[1]["lng"] == -71.1850869

    def test_parse_poi(self, server: FakeServer) -> None:
        server.queue(NOMINATIM_SEARCH_BAKERY)
        place = Geocoder().geocode("bakery in berlin wedding")
        assert place is not None
        assert place["category"] == "shop" and place["type"] == "bakery"
        assert place["name"].startswith("Ditsch, Lindower Straße")
        assert place["address"]["road"] == "Lindower Straße"
        assert place["importance"] == pytest.approx(1e-05)

    def test_parse_tolerates_missing_and_bad_fields(self, server: FakeServer) -> None:
        server.queue([
            {"lat": "10.5", "lon": "20.25"},                               # bare minimum
            {"lat": "abc", "lon": "1"},                                    # skipped
            {"lat": "95", "lon": "1"},                                     # skipped
            {"lon": "1"},                                                  # skipped
            "garbage",                                                     # skipped
            {"lat": 1, "lon": 2, "name": "Spot", "boundingbox": ["x", 1, 2, 3],
             "address": [], "importance": "high", "class": "amenity"},
        ])
        results = Geocoder().search("x", limit=10)
        assert len(results) == 2
        bare, spot = results
        assert bare["name"] == "10.500000, 20.250000"
        assert bare["category"] is None and bare["type"] is None
        assert bare["bbox"] is None and bare["address"] is None and bare["importance"] is None
        assert bare["attribution"] == gmod.OSM_ATTRIBUTION
        assert spot["name"] == "Spot" and spot["category"] == "amenity"
        assert spot["bbox"] is None and spot["address"] is None and spot["importance"] is None

    def test_limit_capped_at_40_for_nominatim(self, server: FakeServer) -> None:
        Geocoder().search("bakery", limit=50)
        assert server.params()["limit"] == ["40"]
        server.queue(PHOTON_EMPTY)
        Geocoder("photon").search("bakery", limit=50)   # Photon has no such cap
        assert server.params()["limit"] == ["50"]

    def test_never_more_results_than_limit(self, server: FakeServer) -> None:
        server.queue([NOMINATIM_SEARCH_BAKERY[0]] * 12)   # a server ignoring `limit`
        assert len(Geocoder().search("bakery", limit=3)) == 3

    def test_results_truncated_to_limit(self, server: FakeServer) -> None:
        server.queue(NOMINATIM_SEARCH_BERLIN)
        assert len(Geocoder().search("Berlin", limit=1)) == 1

    def test_empty(self, server: FakeServer) -> None:
        server.queue([])
        assert Geocoder().search("qwertzuiopasdf") == []

    def test_geocode_best_match(self, server: FakeServer) -> None:
        server.queue(NOMINATIM_SEARCH_BERLIN)
        place = Geocoder().geocode("Berlin", country_codes="de")
        assert place is not None and place["name"] == "Berlin, Deutschland"
        assert server.params()["limit"] == ["1"]
        assert server.params()["countrycodes"] == ["de"]

    def test_geocode_none(self, server: FakeServer) -> None:
        server.queue([])
        assert Geocoder().geocode("nowhere at all") is None

    def test_error_document_with_200(self, server: FakeServer) -> None:
        server.queue({"error": {"code": 400, "message": "Nominatim has encountered an error"}})
        with pytest.raises(GeocodingError, match="Nominatim has encountered an error"):
            Geocoder().search("x")

    def test_unexpected_shape(self, server: FakeServer) -> None:
        server.queue({"type": "FeatureCollection", "features": []})
        with pytest.raises(GeocodingError, match="not with a Nominatim response"):
            Geocoder().search("x")


class TestNominatimReverse:
    def test_request(self, server: FakeServer) -> None:
        server.queue(NOMINATIM_REVERSE)
        Geocoder(email="me@example.com", language="es").reverse(-34.44076, -58.70521, zoom=17)
        assert server.path() == "https://nominatim.openstreetmap.org/reverse"
        assert server.params() == {
            "lat": ["-34.44076"], "lon": ["-58.70521"], "format": ["jsonv2"],
            "addressdetails": ["1"], "zoom": ["17"], "accept-language": ["es"],
            "email": ["me@example.com"],
        }

    def test_default_zoom_and_rounding(self, server: FakeServer) -> None:
        server.queue(NOMINATIM_REVERSE)
        Geocoder().reverse("28.613912345678", 77.2090)  # type: ignore[arg-type]
        params = server.params()
        assert params["zoom"] == ["18"]
        assert params["lat"] == ["28.6139123"] and params["lon"] == ["77.209"]

    def test_parse(self, server: FakeServer) -> None:
        server.queue(NOMINATIM_REVERSE)
        place = Geocoder().reverse(-34.44076, -58.70521)
        assert place is not None
        assert place["lat"] == -34.4391708 and place["lng"] == -58.7064573
        assert place["name"].startswith("Autopista Pedro Eugenio Aramburu, El Triángulo")
        assert place["category"] == "highway" and place["type"] == "motorway"
        assert place["importance"] == 0.1
        assert place["bbox"] == [-58.7086067, -34.44159, -58.7044712, -34.4370994]
        assert place["address"]["country_code"] == "ar"
        assert place["attribution"] == NOMINATIM_REVERSE["licence"]
        assert place["provider"] == "nominatim"

    def test_unable_to_geocode_is_none(self, server: FakeServer) -> None:
        server.queue(NOMINATIM_REVERSE_NOTHING)
        assert Geocoder().reverse(0.0, -140.0) is None

    def test_unexpected_shape(self, server: FakeServer) -> None:
        server.queue([NOMINATIM_REVERSE])
        with pytest.raises(GeocodingError):
            Geocoder().reverse(1, 1)

    @pytest.mark.parametrize("lat, lng", [(91, 0), (0, 181), ("abc", 0), (None, 0),
                                          (float("nan"), 0), (True, 0)])
    def test_invalid_coordinates(self, server: FakeServer, lat: Any, lng: Any) -> None:
        with pytest.raises(InvalidCoordinateError):
            Geocoder().reverse(lat, lng)
        assert server.urls == []

    @pytest.mark.parametrize("zoom", [-1, 19, 10.0, True, "18", None])
    def test_invalid_zoom(self, server: FakeServer, zoom: Any) -> None:
        with pytest.raises(InvalidArgumentError) as info:
            Geocoder().reverse(1, 1, zoom=zoom)
        assert info.value.name == "zoom"
        assert server.urls == []


# ─────────────────────────────────────────────────────────────────────────────
# Photon
# ─────────────────────────────────────────────────────────────────────────────

class TestPhoton:
    def test_search_request(self, server: FakeServer) -> None:
        server.queue(PHOTON_SEARCH_BERLIN)
        Geocoder("photon", email="me@example.com", language="de-AT,en;q=0.5").search(
            "berlin", limit=7, country_codes=["de", "at"])
        assert server.path() == "https://photon.komoot.io/api"
        assert parse_qsl(urlsplit(server.urls[-1]).query) == [
            ("q", "berlin"), ("limit", "7"), ("lang", "de"),
            ("countrycode", "DE"), ("countrycode", "AT"),
        ]

    def test_search_minimal_request(self, server: FakeServer) -> None:
        server.queue(PHOTON_EMPTY)
        assert Geocoder("photon", language="*").search("berlin") == []
        assert server.params() == {"q": ["berlin"], "limit": ["5"]}

    def test_search_parse(self, server: FakeServer) -> None:
        server.queue(PHOTON_SEARCH_BERLIN)
        city, stadium, nh = Geocoder("photon").search("berlin")
        assert city == {
            "lat": 52.5170365,
            "lng": 13.3888599,
            "name": "Berlin, Germany",
            "category": "place",
            "type": "city",
            "importance": None,
            "bbox": [13.088345, 52.3382448, 13.7611609, 52.6755087],
            "address": {"state": "Berlin", "country": "Germany", "country_code": "de"},
            "provider": "photon",
            "attribution": gmod.OSM_ATTRIBUTION + " · Geocoding by Photon",
        }
        assert stadium["name"] == ("Berlin Olympic Stadium, 3, Olympischer Platz, Westend, "
                                   "Berlin, 14053, Germany")
        assert stadium["address"] == {
            "house_number": "3", "road": "Olympischer Platz", "suburb": "Westend",
            "city": "Berlin", "state": "Berlin", "postcode": "14053",
            "country": "Germany", "country_code": "de",
        }
        assert stadium["bbox"] == [13.23727, 52.5135972, 13.241757, 52.5157151]
        assert nh["bbox"] is None and nh["address"]["country_code"] == "us"

    def test_country_codes_also_filtered_client_side(self, server: FakeServer) -> None:
        server.queue(PHOTON_SEARCH_BERLIN)   # a server that ignored `countrycode`
        results = Geocoder("photon").search("berlin", country_codes="us")
        assert [r["address"]["state"] for r in results] == ["New Hampshire"]

    def test_geocode(self, server: FakeServer) -> None:
        server.queue(PHOTON_SEARCH_BERLIN)
        place = Geocoder("photon").geocode("berlin")
        assert place is not None and place["name"] == "Berlin, Germany"
        assert server.params()["limit"] == ["1"]

    def test_reverse(self, server: FakeServer) -> None:
        server.queue(PHOTON_REVERSE_EIFFEL)
        place = Geocoder("photon", language="fr").reverse(48.8584, 2.2945, zoom=5)
        assert server.path() == "https://photon.komoot.io/reverse"
        assert server.params() == {"lat": ["48.8584"], "lon": ["2.2945"], "limit": ["1"],
                                   "lang": ["fr"]}
        assert place is not None
        assert place["lat"] == 48.8582602 and place["lng"] == 2.2944990543196795
        assert place["name"] == ("Tour Eiffel, 5, Avenue Gustave Eiffel, Quartier du "
                                 "Gros-Caillou, Gros-Caillou, Paris, Île-de-France, 75007, France")
        assert place["category"] == "man_made" and place["type"] == "tower"
        assert place["address"]["neighbourhood"] == "Quartier du Gros-Caillou"
        assert place["address"]["county"] == "Paris"
        assert place["bbox"] == [2.2933084, 48.8574753, 2.2956897, 48.8590465]

    def test_reverse_nothing(self, server: FakeServer) -> None:
        server.queue(PHOTON_EMPTY)
        assert Geocoder("photon").reverse(0, 0) is None

    def test_skips_malformed_features(self, server: FakeServer) -> None:
        server.queue({"features": [
            None,
            {"geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
             "properties": {}},
            {"geometry": {"type": "Point", "coordinates": [500, 0]}, "properties": {}},
            {"geometry": {"type": "Point", "coordinates": [1]}, "properties": {}},
            {"geometry": {"type": "Point", "coordinates": [5, 6]}},
            {"geometry": {"type": "Point", "coordinates": [5, 6]},
             "properties": {"extent": [1, "x", 2, 3], "name": "  "}},
        ]})
        (only,) = Geocoder("photon").search("x", limit=10)
        assert only["lat"] == 6 and only["lng"] == 5
        assert only["name"] == "6.000000, 5.000000"
        assert only["bbox"] is None and only["address"] is None

    def test_unexpected_shape(self, server: FakeServer) -> None:
        server.queue([{"lat": "1", "lon": "2"}])   # a Nominatim answer
        with pytest.raises(GeocodingError, match="not with a Photon response"):
            Geocoder("photon").search("x")

    def test_self_hosted(self, server: FakeServer) -> None:
        server.queue(PHOTON_EMPTY)
        Geocoder("photon", url="http://127.0.0.1:2322").search("x")
        assert server.path() == "http://127.0.0.1:2322/api"


# ─────────────────────────────────────────────────────────────────────────────
# Input validation
# ─────────────────────────────────────────────────────────────────────────────

class TestValidation:
    @pytest.mark.parametrize("query", ["", "   ", None, 123, b"Berlin", ["Berlin"]])
    def test_bad_query(self, server: FakeServer, query: Any) -> None:
        with pytest.raises(InvalidArgumentError) as info:
            Geocoder().search(query)
        assert info.value.name == "query"
        with pytest.raises(InvalidArgumentError):
            Geocoder().geocode(query)
        assert server.urls == []

    @pytest.mark.parametrize("limit", [0, -1, 51, True, 2.0, "5", None])
    def test_bad_limit(self, server: FakeServer, limit: Any) -> None:
        with pytest.raises(InvalidArgumentError) as info:
            Geocoder().search("Berlin", limit=limit)
        assert info.value.name == "limit"
        assert server.urls == []

    @pytest.mark.parametrize("codes", ["", "deu", "d1", ["de", 1], [], (), ",", "de,,fr",
                                       42, {"de"}, ["d"], ["ÄÖ"]])
    def test_bad_country_codes(self, server: FakeServer, codes: Any) -> None:
        with pytest.raises(InvalidArgumentError) as info:
            Geocoder().search("Berlin", country_codes=codes)
        assert info.value.name == "country_codes"
        assert server.urls == []

    def test_limit_bounds_accepted(self, server: FakeServer) -> None:
        Geocoder(url="http://localhost").search("a", limit=1)
        Geocoder(url="http://localhost").search("a", limit=50)
        assert len(server.urls) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiting
# ─────────────────────────────────────────────────────────────────────────────

BOTH_MODES = pytest.mark.parametrize("cache_mode", ["disk", "memory"])


@BOTH_MODES
class TestRateLimit:
    def test_public_endpoint_one_per_second(self, server: FakeServer, clock: FakeClock) -> None:
        gc = Geocoder()
        gc.search("a")
        assert clock.sleeps == []          # first request goes straight out
        gc.search("b")
        assert clock.sleeps == [1.0]
        clock.now += 0.4
        server.queue(NOMINATIM_REVERSE_NOTHING)
        gc.reverse(1, 2)                   # /reverse shares the server's budget
        assert clock.sleeps == [1.0, pytest.approx(0.6)]
        clock.now += 5
        gc.search("c")
        assert len(clock.sleeps) == 2      # enough time passed: no wait

    def test_shared_across_instances(self, server: FakeServer, clock: FakeClock) -> None:
        Geocoder().search("a")
        Geocoder(user_agent="other-app/1.0").search("b")
        assert clock.sleeps == [1.0]

    def test_endpoints_are_independent(self, server: FakeServer, clock: FakeClock) -> None:
        server.default = _bytes(PHOTON_EMPTY)
        Geocoder("photon").search("a")
        server.default = b"[]"
        Geocoder().search("a")
        assert clock.sleeps == []

    def test_custom_endpoint_not_throttled(self, server: FakeServer, clock: FakeClock) -> None:
        gc = Geocoder(url="http://localhost:8080")
        for q in "abcde":
            gc.search(q)
        assert clock.sleeps == []

    def test_custom_min_interval(self, server: FakeServer, clock: FakeClock) -> None:
        gc = Geocoder(url="http://localhost:8080", min_interval=0.5)
        gc.search("a")
        gc.search("b")
        gc.search("c")
        assert clock.sleeps == [0.5, 0.5]

    def test_larger_public_interval(self, server: FakeServer, clock: FakeClock) -> None:
        gc = Geocoder(min_interval=2.5)
        gc.search("a")
        gc.search("b")
        assert clock.sleeps == [2.5]

    def test_cache_hits_are_not_throttled(self, server: FakeServer, clock: FakeClock) -> None:
        gc = Geocoder()
        gc.search("a")
        gc.search("a")
        gc.search("a")
        assert clock.sleeps == [] and len(server.urls) == 1


@pytest.mark.parametrize("cache_mode", ["memory"])   # the frozen clock confuses the disk slot
def test_thread_safe_slots(server: FakeServer, clock: FakeClock) -> None:
    clock.advance_on_sleep = False     # every thread sees the same "now"
    gc = Geocoder()
    threads = [threading.Thread(target=gc.search, args=(f"q{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert len(server.urls) == 5
    assert sorted(clock.sleeps) == [1.0, 2.0, 3.0, 4.0]


# ─────────────────────────────────────────────────────────────────────────────
# Caching
# ─────────────────────────────────────────────────────────────────────────────

@BOTH_MODES
class TestCache:
    def test_same_request_fetched_once(self, server: FakeServer) -> None:
        server.queue(NOMINATIM_SEARCH_BERLIN)
        gc = Geocoder()
        first = gc.search("Berlin")
        second = gc.search("Berlin")
        assert first == second and len(server.urls) == 1

    def test_results_are_copies(self, server: FakeServer) -> None:
        server.queue(NOMINATIM_SEARCH_BERLIN)
        gc = Geocoder()
        first = gc.search("Berlin")
        first[0]["name"] = "changed"
        first[0]["address"]["city"] = "changed"
        first.clear()
        again = gc.search("Berlin")
        assert again[0]["name"] == "Berlin, Deutschland"
        assert again[0]["address"]["city"] == "Berlin"

    def test_key_includes_parameters(self, server: FakeServer) -> None:
        gc = Geocoder()
        gc.search("Berlin")
        gc.search("Berlin", limit=2)
        gc.search("Berlin", country_codes="de")
        gc.geocode("Berlin")
        assert len(server.urls) == 4

    def test_lru_eviction(self, server: FakeServer) -> None:
        gc = Geocoder(url="http://localhost", cache_size=2)
        gc.search("a")
        gc.search("b")
        gc.search("a")          # refresh "a" -> "b" is now least recently used
        gc.search("c")          # evicts "b"
        assert len(server.urls) == 3
        gc.search("a")
        assert len(server.urls) == 3
        gc.search("b")
        assert len(server.urls) == 4

    def test_cache_disabled(self, server: FakeServer) -> None:
        gc = Geocoder(url="http://localhost", cache_size=0)
        gc.search("a")
        gc.search("a")
        assert len(server.urls) == 2

    def test_clear_cache(self, server: FakeServer) -> None:
        gc = Geocoder(url="http://localhost")
        gc.search("a")
        gc.clear_cache()
        gc.search("a")
        assert len(server.urls) == 2

    def test_reverse_cached_including_no_result(self, server: FakeServer) -> None:
        server.queue(NOMINATIM_REVERSE_NOTHING)
        gc = Geocoder()
        assert gc.reverse(0, -140) is None
        assert gc.reverse(0, -140) is None
        assert len(server.urls) == 1

    def test_errors_are_not_cached(self, server: FakeServer) -> None:
        url = "https://nominatim.openstreetmap.org/search"
        server.queue(_http_error(url, 429), NOMINATIM_SEARCH_BERLIN)
        gc = Geocoder()
        with pytest.raises(GeocodingError):
            gc.search("Berlin")
        assert len(gc.search("Berlin")) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Error mapping and retries
# ─────────────────────────────────────────────────────────────────────────────

URL = "https://nominatim.openstreetmap.org/search"


class TestErrors:
    def test_403_identify_yourself(self, server: FakeServer) -> None:
        server.queue(_http_error(URL, 403, b"<html>Access blocked</html>"))
        with pytest.raises(GeocodingError) as info:
            Geocoder().search("x")
        err = info.value
        assert err.status == 403 and len(server.urls) == 1
        assert "user_agent=" in (err.hint or "") and "own server" in (err.hint or "")
        assert "HTTP 403" in str(err) and "<html>" not in str(err)

    def test_429_slow_down_without_retry(self, server: FakeServer) -> None:
        server.queue(_http_error(URL, 429, headers={"Retry-After": "30"}))
        with pytest.raises(GeocodingError) as info:
            Geocoder().search("x")
        assert info.value.status == 429
        assert len(server.urls) == 1                  # never hammer a rate-limited server
        assert "Retry after 30 s" in str(info.value)
        assert "min_interval" in (info.value.hint or "")

    def test_400_includes_server_message(self, server: FakeServer) -> None:
        body = _bytes({"message": "language not supported"})
        server.queue(_http_error("https://photon.komoot.io/api", 400, body))
        with pytest.raises(GeocodingError, match="language not supported") as info:
            Geocoder("photon", language="xx").search("x")
        assert info.value.status == 400 and len(server.urls) == 1

    def test_400_nominatim_error_document(self, server: FakeServer) -> None:
        body = _bytes({"error": {"code": 400, "message": "Parameter 'lat' expected"}})
        server.queue(_http_error(URL, 400, body))
        with pytest.raises(GeocodingError, match="Parameter 'lat' expected"):
            Geocoder().search("x")

    def test_404_points_at_url(self, server: FakeServer) -> None:
        server.queue(_http_error("http://localhost:8080/search", 404))
        with pytest.raises(GeocodingError) as info:
            Geocoder(url="http://localhost:8080").search("x")
        assert info.value.status == 404 and "url=" in (info.value.hint or "")

    def test_other_status(self, server: FakeServer) -> None:
        server.queue(_http_error(URL, 418))
        with pytest.raises(GeocodingError) as info:
            Geocoder().search("x")
        assert info.value.status == 418

    def test_5xx_retried_once_then_fails(self, server: FakeServer, clock: FakeClock) -> None:
        server.queue(_http_error(URL, 503), _http_error(URL, 502), NOMINATIM_SEARCH_BERLIN)
        with pytest.raises(GeocodingError) as info:
            Geocoder().search("x")
        assert info.value.status == 502
        assert len(server.urls) == 2                  # one retry, not a storm
        assert clock.sleeps == [1.0]                  # the retry respects the rate limit
        assert "provider='photon'" in (info.value.hint or "")

    def test_5xx_then_success(self, server: FakeServer) -> None:
        server.queue(_http_error(URL, 500), NOMINATIM_SEARCH_BERLIN)
        assert len(Geocoder().search("Berlin")) == 2
        assert len(server.urls) == 2

    def test_retry_waits_on_custom_endpoint(self, server: FakeServer, clock: FakeClock) -> None:
        server.queue(_http_error("http://localhost/search", 500), [])
        assert Geocoder(url="http://localhost").search("x") == []
        assert clock.sleeps == [1.0]

    def test_timeout_retried_once(self, server: FakeServer) -> None:
        server.queue(TimeoutError("timed out"), TimeoutError("timed out"))
        with pytest.raises(GeocodingError) as info:
            Geocoder(timeout=2.5).search("x")
        assert len(server.urls) == 2
        assert "2.5 s" in str(info.value) and "timeout=" in (info.value.hint or "")
        assert info.value.status is None

    def test_timeout_then_success(self, server: FakeServer) -> None:
        server.queue(TimeoutError(), NOMINATIM_SEARCH_BERLIN)
        assert len(Geocoder().search("x")) == 2

    def test_urlerror_wrapping_timeout(self, server: FakeServer) -> None:
        server.queue(urllib.error.URLError(TimeoutError("timed out")),
                     urllib.error.URLError(TimeoutError("timed out")))
        with pytest.raises(GeocodingError, match="did not answer"):
            Geocoder().search("x")
        assert len(server.urls) == 2

    def test_dns_failure_not_retried(self, server: FakeServer) -> None:
        server.queue(urllib.error.URLError(OSError("Name or service not known")))
        with pytest.raises(GeocodingError) as info:
            Geocoder().search("x")
        assert "Could not reach nominatim.openstreetmap.org" in str(info.value)
        assert "internet connection" in (info.value.hint or "")
        assert len(server.urls) == 1

    def test_certificate_problem(self, server: FakeServer) -> None:
        server.queue(urllib.error.URLError(
            OSError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")))
        with pytest.raises(GeocodingError) as info:
            Geocoder().search("x")
        assert "certificate" in (info.value.hint or "").lower()

    def test_dropped_connection_retried_once(self, server: FakeServer) -> None:
        server.queue(http.client.RemoteDisconnected("closed"), ConnectionResetError("reset"))
        with pytest.raises(GeocodingError, match="Could not reach"):
            Geocoder().search("x")
        assert len(server.urls) == 2

    def test_incomplete_read_then_success(self, server: FakeServer) -> None:
        server.queue(http.client.IncompleteRead(b"[{"), NOMINATIM_SEARCH_BERLIN)
        assert len(Geocoder().search("x")) == 2

    def test_other_oserror(self, server: FakeServer) -> None:
        server.queue(OSError("network unreachable"))
        with pytest.raises(GeocodingError, match="network unreachable"):
            Geocoder().search("x")

    @pytest.mark.parametrize("body", [b"<html><body>Welcome to nginx!</body></html>",
                                      b"", b"\xff\xfe\x00", b"[1, 2"])
    def test_bad_json(self, server: FakeServer, body: bytes) -> None:
        server.queue(body)
        with pytest.raises(GeocodingError, match="did not return JSON") as info:
            Geocoder().search("x")
        assert "url=" in (info.value.hint or "")

    def test_utf8_bom_accepted(self, server: FakeServer) -> None:
        server.queue(b"\xef\xbb\xbf" + _bytes(NOMINATIM_SEARCH_BERLIN))
        assert len(Geocoder().search("x")) == 2


class TestGeocodingError:
    def test_house_style(self) -> None:
        err = GeocodingError("Something broke.", "Do this.\nThen that.", 503)
        text = str(err)
        assert "❌  QOREgeo — Geocoding Failed" in text
        assert "Something broke." in text and "HTTP status: 503" in text
        assert "Fix it:\n    Do this.\n    Then that." in text
        assert err.status == 503 and err.hint == "Do this.\nThen that."
        assert err.reason == "Something broke."

    def test_default_hint(self) -> None:
        err = GeocodingError("Offline.")
        assert err.status is None and err.hint is None
        assert "Fix it:" in str(err) and "HTTP status" not in str(err)

    def test_hierarchy(self) -> None:
        err = GeocodingError("x")
        assert isinstance(err, QOREgeoError) and isinstance(err, RuntimeError)

    def test_exported(self) -> None:
        from qoregeo import exceptions
        assert "GeocodingError" in exceptions.__all__

    def test_pickle_roundtrip(self) -> None:
        err = GeocodingError("Too many requests.", "Slow down.", 429)
        clone = pickle.loads(pickle.dumps(err))
        assert type(clone) is GeocodingError
        assert clone.status == 429 and clone.hint == "Slow down."
        assert str(clone) == str(err)


# ─────────────────────────────────────────────────────────────────────────────
# The real _fetch, with urlopen replaced
# ─────────────────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class TestFetch:
    def _install(self, monkeypatch: pytest.MonkeyPatch, body: bytes) -> list[Any]:
        calls: list[Any] = []

        def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeResponse:
            calls.append((request, timeout))
            return _FakeResponse(body)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        return calls

    def test_headers_and_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._install(monkeypatch, _bytes(NOMINATIM_SEARCH_BERLIN))
        results = Geocoder(timeout=4).search("Berlin")
        assert len(results) == 2
        ((request, timeout),) = calls
        assert timeout == 4.0
        assert request.get_method() == "GET"
        assert request.full_url.startswith("https://nominatim.openstreetmap.org/search?q=Berlin")
        assert request.get_header("User-agent") == gmod.DEFAULT_USER_AGENT
        assert request.get_header("Accept") == "application/json"

    def test_custom_user_agent_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._install(monkeypatch, _bytes(PHOTON_EMPTY))
        Geocoder("photon", user_agent="field-survey/3.1 (gis@example.org)").reverse(1, 2)
        request = calls[0][0]
        assert request.get_header("User-agent") == "field-survey/3.1 (gis@example.org)"
        assert "email" not in request.full_url     # e-mail is a Nominatim-only parameter

    def test_oversized_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gmod, "_MAX_RESPONSE_BYTES", 10)
        self._install(monkeypatch, b"[" + b" " * 20 + b"]")
        with pytest.raises(GeocodingError, match="larger than"):
            Geocoder().search("x")

    def test_http_error_from_urlopen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_urlopen(request: urllib.request.Request, timeout: float) -> Any:
            raise _http_error(request.full_url, 403)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(GeocodingError) as info:
            Geocoder().search("x")
        assert info.value.status == 403


# ─────────────────────────────────────────────────────────────────────────────
# Hardening (found in an adversarial review)
# ─────────────────────────────────────────────────────────────────────────────

class TestHardeningInputs:
    def test_url_is_normalised_to_ascii(self, server: FakeServer) -> None:
        gc = Geocoder(url="HTTP://Bücher.Example:08080/géo/")
        assert gc.url == "http://xn--bcher-kva.example:8080/g%C3%A9o"
        gc.search("x")
        assert server.urls[-1].isascii()
        assert server.path() == "http://xn--bcher-kva.example:8080/g%C3%A9o/search"

    def test_ipv6_url(self, server: FakeServer) -> None:
        gc = Geocoder(url="http://[::1]:8080/")
        assert gc.url == "http://[::1]:8080"
        gc.search("x")
        assert server.path() == "http://[::1]:8080/search"

    def test_latin1_user_agent_and_limits_accepted(self) -> None:
        gc = Geocoder(user_agent="José's app/1.0", timeout=86_400, min_interval=86_400)
        assert gc.user_agent == "José's app/1.0"
        assert gc.timeout == 86_400.0 and gc.min_interval == 86_400.0

    @pytest.mark.parametrize("query", ["Berlin\udcff", "a\x00b", "\x1b[2J"])
    def test_unsendable_query_rejected(self, server: FakeServer, query: str) -> None:
        with pytest.raises(InvalidArgumentError) as info:
            Geocoder().search(query)
        assert info.value.name == "query" and server.urls == []

    def test_query_whitespace_collapsed(self, server: FakeServer) -> None:
        gc = Geocoder()
        gc.search("10 Downing St\n\tLondon ")
        gc.search("10  Downing St London")          # same request -> cache hit
        assert server.params()["q"] == ["10 Downing St London"]
        assert len(server.urls) == 1

    def test_zero_width_joiners_kept(self, server: FakeServer) -> None:
        Geocoder().search("می\u200cخواهم")          # ZWNJ is part of Persian text
        assert server.params()["q"] == ["می\u200cخواهم"]

    @pytest.mark.parametrize("language, lang", [
        ("en;q=0.5,de", "de"),               # the highest q-value wins
        ("*;q=0.5,fr", "fr"),                # "*" is skipped
        ("en;Q=0.8,fr;q=0.8", "en"),         # first one on ties; Q is case-insensitive
        ("pt-BR", "pt"),
        ("de;q=0", None),                    # q=0 means "not acceptable"
    ])
    def test_photon_language_preference(self, server: FakeServer, language: str,
                                        lang: str | None) -> None:
        server.default = _bytes(PHOTON_EMPTY)
        Geocoder("photon", language=language).search("x")
        assert server.params().get("lang") == ([lang] if lang else None)


@BOTH_MODES
class TestHardeningRateLimit:
    @pytest.mark.parametrize("url", [
        "https://nominatim.openstreetmap.org./",       # trailing-dot FQDN
        "https://nominatim.openstreetmap.org:443",
        "http://nominatim.openstreetmap.org",
    ])
    def test_spellings_of_the_public_host_share_one_budget(
        self, server: FakeServer, clock: FakeClock, url: str
    ) -> None:
        other = Geocoder(url=url, min_interval=0)
        assert other.is_public and other.min_interval == 1.0
        Geocoder().search("a")
        other.search("b")
        assert clock.sleeps == [1.0]

    def test_old_photon_host_is_public(self) -> None:
        gc = Geocoder("photon", url="https://photon.komoot.de")
        assert gc.is_public and gc.min_interval == 1.0

    def test_custom_ports_are_separate_endpoints(self, server: FakeServer,
                                                 clock: FakeClock) -> None:
        Geocoder(url="http://localhost:8080", min_interval=5).search("a")
        Geocoder(url="http://localhost:2322", min_interval=5).search("a")
        assert clock.sleeps == []
        Geocoder(url="http://LOCALHOST:8080/", min_interval=5).search("b")
        assert clock.sleeps == [5.0]


class TestHardeningResponses:
    def test_deeply_nested_json_is_a_geocoding_error(self, server: FakeServer) -> None:
        server.queue(b"[" * 200_000 + b"]" * 200_000)
        try:
            result = Geocoder().search("x")
        except GeocodingError as exc:
            assert "nested too deeply" in str(exc)
        else:                                 # a decoder without a depth limit
            assert result == []

    def test_recursion_error_while_decoding_is_mapped(self, server: FakeServer,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
        def too_deep(text: str) -> Any:
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(json, "loads", too_deep)
        with pytest.raises(GeocodingError, match="nested too deeply"):
            Geocoder().search("x")

    def test_address_is_flat_text(self, server: FakeServer) -> None:
        nested = "[" * 50 + "]" * 50
        body = ('[{"lat": "1", "lon": "2", "address": {"road": "Main St", "postcode": 10115,'
                ' "x": ' + nested + ', "y": {"z": "1"}, "empty": "", "flag": true}}]')
        server.queue(body.encode())
        gc = Geocoder()
        (place,) = gc.search("x")
        assert place["address"] == {"road": "Main St", "postcode": "10115"}
        assert gc.search("x") == [place]                         # cached copy

    def test_licence_is_sanitised_and_capped(self, server: FakeServer) -> None:
        server.queue([{"lat": "1", "lon": "2", "licence": "\x1b[2J" + "x" * 2000}])
        (place,) = Geocoder().search("x")
        assert "\x1b" not in place["attribution"]
        assert len(place["attribution"]) == 300

    def test_server_error_text_is_sanitised(self, server: FakeServer) -> None:
        body = _bytes({"message": "bad \x1b[31mRED\x1b[0m\x07 " + "y" * 1000})
        server.queue(_http_error(URL, 400, body))
        with pytest.raises(GeocodingError) as info:
            Geocoder().search("x")
        text = str(info.value)
        assert "\x1b" not in text and "\x07" not in text
        assert "Server said: bad [31mRED [0m" in text
        assert len(text) < 1200

    @pytest.mark.parametrize("value, shown", [
        ("30", "Retry after 30 s."),
        ("Wed, 21 Oct 2015 07:28:00 GMT", "Retry after Wed, 21 Oct 2015 07:28:00 GMT."),
    ])
    def test_retry_after_forms(self, server: FakeServer, value: str, shown: str) -> None:
        server.queue(_http_error(URL, 429, headers={"Retry-After": value}))
        with pytest.raises(GeocodingError, match=shown):
            Geocoder().search("x")

    def test_retry_after_is_capped(self, server: FakeServer) -> None:
        server.queue(_http_error(URL, 429, headers={"Retry-After": "z" * 5000}))
        with pytest.raises(GeocodingError) as info:
            Geocoder().search("x")
        assert "z" * 41 not in str(info.value)

    def test_network_error_text_is_sanitised(self, server: FakeServer) -> None:
        server.queue(http.client.BadStatusLine("\x1b[2Jevil"),
                     http.client.BadStatusLine("\x1b[2Jevil"))
        with pytest.raises(GeocodingError, match="Could not reach") as info:
            Geocoder().search("x")
        assert "\x1b" not in str(info.value) and "[2Jevil" in str(info.value)

    def test_value_error_from_http_stack_is_mapped(self, server: FakeServer) -> None:
        server.queue(ValueError("Invalid header value"), [])
        with pytest.raises(GeocodingError, match="Could not send the request"):
            Geocoder().search("x")
        assert len(server.urls) == 1                     # not retried

    def test_invalid_url_from_http_stack_not_retried(self, server: FakeServer) -> None:
        server.queue(http.client.InvalidURL("nonnumeric port"), [])
        with pytest.raises(GeocodingError, match="nonnumeric port"):
            Geocoder().search("x")
        assert len(server.urls) == 1

    def test_parsing_stops_at_limit(self, server: FakeServer) -> None:
        server.queue([NOMINATIM_SEARCH_BAKERY[0]] * 1000)   # a server ignoring `limit`
        gc = Geocoder()
        assert len(gc.search("bakery", limit=3)) == 3
        assert [len(v) for v in gc._cache.values()] == [3]

    def test_photon_country_filter_keeps_limit(self, server: FakeServer) -> None:
        us = PHOTON_SEARCH_BERLIN["features"][2]
        de = PHOTON_SEARCH_BERLIN["features"][0]
        server.queue({"features": [us, us, de, us, de]})
        (only,) = Geocoder("photon").search("berlin", limit=1, country_codes="de")
        assert only["address"]["country_code"] == "de"


class TestHardeningReverse:
    @pytest.mark.parametrize("lat, lng, lat_s, lng_s", [
        (5e-07, -0.0, "0.0000005", "0"),     # no exponent, no "-0"
        (10, 20, "10", "20"),
        (-1e-09, 179.99999999, "0", "180"),
        (-89.123456789, -0.5, "-89.1234568", "-0.5"),
    ])
    def test_coordinates_in_fixed_point(self, server: FakeServer, lat: float, lng: float,
                                        lat_s: str, lng_s: str) -> None:
        server.queue(NOMINATIM_REVERSE_NOTHING)
        Geocoder().reverse(lat, lng)
        assert server.params()["lat"] == [lat_s] and server.params()["lon"] == [lng_s]

    def test_structured_error_is_not_a_missing_address(self, server: FakeServer) -> None:
        error = {"error": {"code": 500, "message": "Internal Server Error"}}
        server.queue(error, error)
        gc = Geocoder()
        with pytest.raises(GeocodingError, match="Internal Server Error"):
            gc.reverse(1, 2)
        with pytest.raises(GeocodingError):
            gc.reverse(1, 2)                                 # an error is never cached
        assert len(server.urls) == 2


def test_real_fetch_sends_ascii_url(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        seen.append(request)
        return _FakeResponse(b"[]")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert Geocoder(url="http://bücher.example/géo").search("Straße") == []
    assert seen[0].full_url == ("http://xn--bcher-kva.example/g%C3%A9o/search?q=Stra%C3%9Fe"
                                "&format=jsonv2&addressdetails=1&limit=5")


# ─────────────────────────────────────────────────────────────────────────────
# On-disk state shared by processes (review finding 31: a shell loop of
# `qoregeo geocode` sent ~10 requests/s, because every process started with an
# empty throttle and an empty cache)
# ─────────────────────────────────────────────────────────────────────────────

def _new_process() -> None:
    """What a fresh process sees: no in-process slots (the disk is shared)."""
    gmod._reset_rate_limits()


class TestSharedAcrossProcesses:
    def test_slot_survives_a_new_process(self, server: FakeServer, clock: FakeClock) -> None:
        Geocoder().search("Berlin")
        _new_process()
        Geocoder().search("Paris")
        assert clock.sleeps == [1.0] and len(server.urls) == 2

    def test_shell_loop_is_one_request_per_second(self, server: FakeServer,
                                                  clock: FakeClock) -> None:
        for city in ("Berlin", "Paris", "Rome", "Madrid"):
            _new_process()
            Geocoder().search(city)
            clock.now += 0.1               # interpreter start-up between runs
        assert clock.sleeps == pytest.approx([0.9, 0.9, 0.9])

    @pytest.mark.parametrize("cache_mode", ["memory"])
    def test_without_disk_only_the_process_is_throttled(self, server: FakeServer,
                                                         clock: FakeClock) -> None:
        Geocoder().search("Berlin")
        _new_process()
        Geocoder().search("Paris")
        assert clock.sleeps == []

    def test_public_hosts_have_separate_slots(self, server: FakeServer,
                                              clock: FakeClock) -> None:
        Geocoder().search("a")
        _new_process()
        server.default = _bytes(PHOTON_EMPTY)
        Geocoder("photon").search("a")
        assert clock.sleeps == []

    def test_custom_endpoint_has_no_disk_state(self, server: FakeServer, clock: FakeClock,
                                               cache_home: Path) -> None:
        gc = Geocoder(url="http://localhost:8080", cache_dir=cache_home)
        assert gc.cache_dir is None
        gc.search("a")
        _new_process()
        Geocoder(url="http://localhost:8080").search("a")
        assert clock.sleeps == [] and len(server.urls) == 2
        assert not cache_home.exists()

    def test_answer_reused_by_the_next_process(self, server: FakeServer, clock: FakeClock,
                                               cache_home: Path) -> None:
        server.queue(NOMINATIM_SEARCH_BERLIN)
        first = Geocoder().search("Berlin")
        _new_process()
        again = Geocoder(user_agent="other-app/1.0").search("Berlin")
        assert again == first and len(server.urls) == 1
        assert clock.sleeps == []          # a disk hit is not throttled
        assert _rows(cache_home) == 1

    def test_answer_expires_after_ttl(self, server: FakeServer, clock: FakeClock) -> None:
        Geocoder().search("Berlin")
        clock.now += gmod.CACHE_TTL - 1
        Geocoder().search("Berlin")
        assert len(server.urls) == 1
        clock.now += 2
        Geocoder().search("Berlin")
        assert len(server.urls) == 2

    def test_reverse_and_no_result_are_stored(self, server: FakeServer) -> None:
        server.queue(NOMINATIM_REVERSE_NOTHING)
        assert Geocoder().reverse(0, -140) is None
        assert Geocoder().reverse(0, -140) is None
        assert len(server.urls) == 1

    def test_errors_are_not_stored(self, server: FakeServer, cache_home: Path) -> None:
        server.queue(_http_error(URL, 429), NOMINATIM_SEARCH_BERLIN)
        with pytest.raises(GeocodingError):
            Geocoder().search("Berlin")
        assert len(Geocoder().search("Berlin")) == 2
        assert len(server.urls) == 2

    def test_key_includes_the_limit(self, server: FakeServer) -> None:
        server.default = _bytes([NOMINATIM_SEARCH_BAKERY[0]] * 50)   # ignores `limit`
        assert len(Geocoder().search("bakery", limit=41)) == 41
        assert len(Geocoder().search("bakery", limit=45)) == 45   # same URL (limit=40)
        assert len(server.urls) == 2

    def test_cache_size_zero_stores_nothing_but_keeps_the_slot(
        self, server: FakeServer, clock: FakeClock, cache_home: Path
    ) -> None:
        Geocoder(cache_size=0).search("a")
        _new_process()
        Geocoder(cache_size=0).search("a")
        assert len(server.urls) == 2 and clock.sleeps == [1.0]
        assert _rows(cache_home) == 0

    @pytest.mark.parametrize("off", [None, False])
    def test_cache_dir_off(self, server: FakeServer, clock: FakeClock, cache_home: Path,
                           off: Any) -> None:
        gc = Geocoder(cache_dir=off)
        assert gc.cache_dir is None
        gc.search("a")
        _new_process()
        Geocoder(cache_dir=off).search("a")
        assert len(server.urls) == 2 and clock.sleeps == []
        assert not cache_home.exists()

    def test_explicit_cache_dir(self, server: FakeServer, tmp_path: Path,
                                monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        gc = Geocoder(cache_dir=Path("mine"))           # relative: fixed at construction
        assert gc.cache_dir is not None
        assert Path(gc.cache_dir).resolve() == (tmp_path / "mine").resolve()
        gc.search("a")
        db = tmp_path / "mine" / "geocode-v1.sqlite3"
        assert db.is_file()
        if os.name == "posix":            # it holds the queries: owner-only
            assert db.stat().st_mode & 0o077 == 0

    @pytest.mark.parametrize("value", ["off", "OFF", "0", "none", "false"])
    def test_env_turns_it_off(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("QOREGEO_CACHE_DIR", value)
        assert Geocoder().cache_dir is None

    def test_env_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("QOREGEO_CACHE_DIR", str(tmp_path / "env-cache"))
        assert Geocoder().cache_dir == str(tmp_path / "env-cache")
        assert Geocoder(cache_dir=tmp_path / "arg").cache_dir == str(tmp_path / "arg")

    def test_clear_cache_forgets_its_servers_answers(self, server: FakeServer) -> None:
        Geocoder().search("a")
        server.default = _bytes(PHOTON_EMPTY)
        Geocoder("photon").search("a")
        server.default = b"[]"
        Geocoder().clear_cache()
        Geocoder().search("a")
        assert len(server.urls) == 3
        server.default = _bytes(PHOTON_EMPTY)
        Geocoder("photon").search("a")    # another server: still stored
        assert len(server.urls) == 3

    def test_old_answers_are_pruned(self, server: FakeServer, cache_home: Path,
                                    monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> None:
        monkeypatch.setattr(gmod, "_DB_MAX_ROWS", 3)
        gc = Geocoder()
        for q in "abcde":
            gc.search(q)
        assert _rows(cache_home) == 3
        clock.now += gmod.CACHE_TTL + 1
        gc.search("f")                    # expired answers are dropped on the next write
        assert _rows(cache_home) == 1

    def test_unusual_text_is_stored(self, server: FakeServer) -> None:
        server.queue(b'[{"lat": "1", "lon": "2", "display_name": "\\ud800 x \\u00e9"}]')
        gc = Geocoder()
        (place,) = gc.search("x")
        assert gc._store is not None and not gc._store.broken
        assert Geocoder().search("x") == [place] and len(server.urls) == 1

    def test_tampered_answer_is_ignored(self, server: FakeServer, cache_home: Path) -> None:
        Geocoder().search("a")
        con = sqlite3.connect(cache_home / "geocode-v1.sqlite3")
        con.execute("UPDATE results SET value = '{\"not\": \"a list\"}'")
        con.commit()
        con.close()
        assert Geocoder().search("a") == [] and len(server.urls) == 2

    def test_clock_set_back_does_not_block(self, server: FakeServer, clock: FakeClock) -> None:
        Geocoder().search("a")
        clock.now -= 500                   # e.g. NTP corrected a fast clock
        _new_process()
        Geocoder().search("b")
        assert clock.sleeps == [1.0]       # not 501 s

    def test_stale_far_future_slot_is_ignored(self, server: FakeServer, clock: FakeClock,
                                              cache_home: Path) -> None:
        Geocoder().search("a")
        con = sqlite3.connect(cache_home / "geocode-v1.sqlite3")
        con.execute("UPDATE slots SET start = start + 1e9")
        con.commit()
        con.close()
        _new_process()
        Geocoder().search("b")
        assert clock.sleeps == []

    def test_bookings_from_many_connections_are_serialised(self, cache_home: Path,
                                                           clock: FakeClock) -> None:
        clock.advance_on_sleep = False     # everyone books at the same "now"
        waits: list[float | None] = []
        lock = threading.Lock()

        def book() -> None:
            wait = gmod._DiskStore(str(cache_home)).book_slot("example.org", 1.0)
            with lock:
                waits.append(wait)

        threads = [threading.Thread(target=book) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert sorted(w for w in waits if w is not None) == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]

    def test_large_min_interval_does_not_hold_up_others(
        self, server: FakeServer, clock: FakeClock
    ) -> None:
        slow = Geocoder(min_interval=3600)
        slow.search("a")
        clock.now += 5
        _new_process()
        Geocoder().search("b")             # another process: the 1 s policy only
        assert clock.sleeps == []


class TestSharedStateDegrades:
    """An unusable cache directory or a Python without sqlite3 falls back to the
    in-process limit and cache — never an error."""

    def _check_in_process(self, server: FakeServer, clock: FakeClock, gc: Geocoder) -> None:
        assert gc.search("a") == [] and gc.search("a") == []   # memory cache
        gc.search("b")
        assert len(server.urls) == 2 and clock.sleeps == [1.0]
        assert gc._store is not None and gc._store.broken

    def test_cache_dir_is_a_file(self, server: FakeServer, clock: FakeClock,
                                 tmp_path: Path) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("x")
        self._check_in_process(server, clock, Geocoder(cache_dir=blocker / "sub"))

    def test_unwritable_directory(self, server: FakeServer, clock: FakeClock,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
        def denied(*args: Any, **kwargs: Any) -> int:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(os, "open", denied)    # root ignores chmod, so simulate it
        self._check_in_process(server, clock, Geocoder())

    def test_no_sqlite3_module(self, server: FakeServer, clock: FakeClock,
                               monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "sqlite3", None)
        self._check_in_process(server, clock, Geocoder())

    @pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "symlink"),
                        reason="needs O_NOFOLLOW and symlinks")
    def test_symlinked_database_is_not_followed(self, server: FakeServer, clock: FakeClock,
                                                cache_home: Path, tmp_path: Path) -> None:
        victim = tmp_path / "victim.txt"
        victim.write_bytes(b"")
        cache_home.mkdir()
        try:
            os.symlink(victim, cache_home / "geocode-v1.sqlite3")
        except OSError:                    # e.g. no symlink privilege
            pytest.skip("cannot create symlinks here")
        self._check_in_process(server, clock, Geocoder())
        assert victim.read_bytes() == b""

    def test_corrupt_database(self, server: FakeServer, clock: FakeClock,
                              cache_home: Path) -> None:
        cache_home.mkdir()
        (cache_home / "geocode-v1.sqlite3").write_bytes(b"not a database " * 100)
        self._check_in_process(server, clock, Geocoder())

    def test_locked_database(self, server: FakeServer, clock: FakeClock,
                             monkeypatch: pytest.MonkeyPatch) -> None:
        def locked(*args: Any, **kwargs: Any) -> Any:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(sqlite3, "connect", locked)
        self._check_in_process(server, clock, Geocoder())

    def test_failure_after_a_disk_booking_keeps_the_local_slot(
        self, server: FakeServer, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failing(*args: Any, **kwargs: Any) -> Any:
            raise sqlite3.OperationalError("disk I/O error")

        gc = Geocoder()
        gc.search("a")
        monkeypatch.setattr(sqlite3, "connect", failing)
        gc.search("b")
        assert clock.sleeps == [1.0] and len(server.urls) == 2


@pytest.mark.parametrize("platform, env, expected", [
    ("linux", {"HOME": "/home/ann"}, "/home/ann/.cache/qoregeo"),
    ("linux", {"HOME": "/home/ann", "XDG_CACHE_HOME": "/var/cache/ann"},
     "/var/cache/ann/qoregeo"),
    ("linux", {"HOME": "/home/ann", "XDG_CACHE_HOME": "relative"},   # invalid per XDG
     "/home/ann/.cache/qoregeo"),
    ("darwin", {"HOME": "/Users/ann"}, "/Users/ann/Library/Caches/qoregeo"),
    ("win32", {"LOCALAPPDATA": "/c/Users/ann/AppData/Local"},
     "/c/Users/ann/AppData/Local/qoregeo"),
    ("win32", {"USERPROFILE": "/c/Users/ann"}, "/c/Users/ann/AppData/Local/qoregeo"),
    ("linux", {"HOME": "/home/ann", "QOREGEO_CACHE_DIR": "/srv/geo"}, "/srv/geo"),
    ("linux", {"HOME": "/home/ann", "QOREGEO_CACHE_DIR": " Off "}, None),
    ("linux", {"HOME": "/home/ann", "QOREGEO_CACHE_DIR": ""}, "/home/ann/.cache/qoregeo"),
])
def test_default_cache_dir(platform: str, env: dict[str, str], expected: str | None) -> None:
    def native(path: str) -> str:   # "/a/b" -> an absolute path on the running OS
        if not path.startswith("/"):
            return path
        return os.path.join(os.path.abspath(os.sep), *path.strip("/").split("/"))

    result = gmod._default_cache_dir({k: native(v) for k, v in env.items()}, platform)
    assert result == (None if expected is None else native(expected))


def test_default_cache_dir_without_a_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "expanduser", lambda path: path)   # "~" stays "~"
    assert gmod._default_cache_dir({}, "linux") is None


# A child process: geocodes twice against a "public" test host with a short
# interval, after waiting for its sibling to be ready. It prints the start time
# each request booked in the shared file and when each request really began
# (wall clock, comparable across processes).
_CHILD = r"""
import json, os, sys, time
from qoregeo import geocode as gmod

host, interval, cache_dir, me, *others = sys.argv[1:]
gmod.PUBLIC_HOSTS = frozenset({host})
gmod.PUBLIC_MIN_INTERVAL = float(interval)
starts, booked = [], []

def fetch(self, url):
    starts.append(time.time())
    return b"[]"

real_book, real_clock = gmod._DiskStore.book_slot, time.time

def book_slot(self, endpoint, interval):
    seen = []                        # the one time.time() the booking reads: its "now"
    def clock():
        seen.append(real_clock())
        return seen[-1]
    time.time = clock
    try:
        wait = real_book(self, endpoint, interval)
    finally:
        time.time = real_clock
    if wait is not None and seen:
        booked.append(seen[-1] + wait)
    return wait

gmod.Geocoder._fetch = fetch
gmod._DiskStore.book_slot = book_slot
open(me, "w").close()
deadline = time.monotonic() + 60
while not all(os.path.exists(p) for p in others):
    if time.monotonic() > deadline:
        sys.exit("the other process never started")
    time.sleep(0.005)
gc = gmod.Geocoder(url="https://" + host, cache_dir=None if cache_dir == "-" else cache_dir)
for query in ("first", "second"):
    gc.search(query + " from " + os.path.basename(me))
print(json.dumps({"starts": starts, "booked": booked}))
"""


def _run_two_processes(tmp_path: Path, cache_dir: str,
                       interval: float) -> list[dict[str, list[float]]]:
    root = str(Path(gmod.__file__).resolve().parents[1])     # this checkout of qoregeo
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        p for p in (root, os.environ.get("PYTHONPATH")) if p))
    ready = [str(tmp_path / f"ready-{i}") for i in range(2)]
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD, "geo.invalid", str(interval), cache_dir, me,
             *[r for r in ready if r != me]],
            cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for me in ready
    ]
    outputs = [proc.communicate(timeout=120) for proc in procs]
    for proc, (_, err) in zip(procs, outputs, strict=True):
        assert proc.returncode == 0, err
    return [json.loads(out) for out, _ in outputs]


def test_two_real_processes_share_one_slot(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    for name, real in zip(("time", "monotonic", "sleep"), _REAL_TIME, strict=True):
        monkeypatch.setattr(time, name, real)
    interval = 0.2
    runs = _run_two_processes(tmp_path, str(tmp_path / "cache"), interval)
    # The shared file hands out slots at least `interval` apart across both
    # processes. (The moments the requests then begin also carry the OS's
    # scheduling delay, which is why the slots, not those, are compared.)
    slots = sorted(t for run in runs for t in run["booked"])
    assert len(slots) == 4
    gaps = [b - a for a, b in zip(slots[:-1], slots[1:], strict=True)]
    assert min(gaps) >= interval - 1e-6, gaps
    # Each process waits for its slot before it sends the request.
    for run in runs:
        assert len(run["starts"]) == len(run["booked"]) == 2
        for slot, start in zip(run["booked"], run["starts"], strict=True):
            assert start >= slot - 0.02, (slot, start)   # timer resolution
