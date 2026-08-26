"""
Geocoding.

Every test here runs offline. The HTTP layer is stubbed, because a test suite
that reaches out to a public API is a test suite that fails on a train, and
hammering Nominatim from CI is exactly what its usage policy forbids.
"""

from __future__ import annotations

import time

import pytest

from qoregeo import GeoEngine
from qoregeo.exceptions import GeocodingError
from qoregeo.geocode import (
    DEFAULT_BASE_URL,
    DEFAULT_MIN_INTERVAL,
    Geocoder,
    _normalise,
    to_feature,
)

RAW_RESULT = {
    "lat": "28.6129",
    "lon": "77.2295",
    "display_name": "India Gate, New Delhi, Delhi, 110001, India",
    "type": "monument",
    "importance": 0.7,
    "osm_id": 12345,
    "address": {
        "city": "New Delhi",
        "state": "Delhi",
        "postcode": "110001",
        "country": "India",
        "country_code": "in",
    },
}


@pytest.fixture
def stubbed(monkeypatch):
    """A Geocoder whose HTTP layer returns canned data and records its calls."""
    calls = []

    def fake_get(self, endpoint, params, label):
        calls.append((endpoint, params))
        if endpoint == "reverse":
            return RAW_RESULT
        if params.get("q", "").strip().lower() == "nowhere":
            return []
        return [RAW_RESULT]

    monkeypatch.setattr(Geocoder, "_get", fake_get)
    coder = Geocoder(user_agent="qoregeo-tests/1.0 (tests@example.com)", min_interval=0)
    coder.calls = calls
    return coder


class TestConstruction:
    def test_requires_a_user_agent(self):
        with pytest.raises(GeocodingError):
            Geocoder("")

    def test_rejects_a_blank_user_agent(self):
        with pytest.raises(GeocodingError):
            Geocoder("   ")

    def test_error_explains_why(self):
        with pytest.raises(GeocodingError) as exc:
            Geocoder("")
        assert "user_agent" in str(exc.value)

    def test_defaults(self):
        coder = Geocoder("test/1.0 (a@b.c)")
        assert coder.base_url == DEFAULT_BASE_URL
        assert coder.min_interval == DEFAULT_MIN_INTERVAL

    def test_base_url_trailing_slash_is_trimmed(self):
        assert Geocoder("t/1 (a@b.c)", base_url="http://x/").base_url == "http://x"

    def test_repr(self):
        assert "Geocoder" in repr(Geocoder("t/1 (a@b.c)"))


class TestForwardGeocoding:
    def test_returns_a_normalised_result(self, stubbed):
        result = stubbed.geocode("India Gate")
        assert result["lat"] == 28.6129
        assert result["lng"] == 77.2295
        assert result["city"] == "New Delhi"
        assert result["country_code"] == "in"

    def test_no_match_returns_none(self, stubbed):
        assert stubbed.geocode("nowhere") is None

    def test_rejects_an_empty_address(self, stubbed):
        with pytest.raises(GeocodingError):
            stubbed.geocode("")

    def test_rejects_whitespace(self, stubbed):
        with pytest.raises(GeocodingError):
            stubbed.geocode("   ")

    def test_country_filter_is_passed_through(self, stubbed):
        stubbed.geocode("India Gate", country="IN")
        assert stubbed.calls[-1][1]["countrycodes"] == "in"

    def test_limit_is_passed_through(self, stubbed):
        stubbed.geocode("India Gate", limit=5)
        assert stubbed.calls[-1][1]["limit"] == 5

    def test_geocode_many(self, stubbed):
        results = stubbed.geocode_many(["India Gate", "nowhere", "Red Fort"])
        assert [r is not None for r in results] == [True, False, True]

    def test_geocode_many_can_propagate_errors(self, monkeypatch):
        coder = Geocoder("t/1 (a@b.c)", min_interval=0)

        def explode(self, endpoint, params, label):
            raise GeocodingError(label, "boom")

        monkeypatch.setattr(Geocoder, "_get", explode)
        with pytest.raises(GeocodingError):
            coder.geocode_many(["a"], skip_failures=False)

    def test_geocode_many_skips_failures_by_default(self, monkeypatch):
        coder = Geocoder("t/1 (a@b.c)", min_interval=0)

        def explode(self, endpoint, params, label):
            raise GeocodingError(label, "boom")

        monkeypatch.setattr(Geocoder, "_get", explode)
        assert coder.geocode_many(["a", "b"]) == [None, None]


class TestReverseGeocoding:
    def test_returns_an_address(self, stubbed):
        assert stubbed.reverse(28.6129, 77.2295)["city"] == "New Delhi"

    def test_zoom_is_passed_through(self, stubbed):
        stubbed.reverse(28.6129, 77.2295, zoom=10)
        assert stubbed.calls[-1][1]["zoom"] == 10

    def test_error_payload_returns_none(self, monkeypatch):
        coder = Geocoder("t/1 (a@b.c)", min_interval=0)
        monkeypatch.setattr(Geocoder, "_get", lambda self, endpoint, params, label: {"error": "Unable to geocode"})
        assert coder.reverse(0, 0) is None


class _FakeResponse:
    """Minimal stand-in for the object urlopen returns."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestCaching:
    """
    The cache lives inside ``_get``, so these stub the transport *below* it.
    Stubbing ``_get`` itself would bypass the thing under test.
    """

    @pytest.fixture
    def counting_transport(self, monkeypatch):
        import json as json_module

        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(request.full_url)
            return _FakeResponse(json_module.dumps([RAW_RESULT]).encode("utf-8"))

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        return calls

    def test_repeat_lookups_hit_the_cache(self, counting_transport):
        coder = Geocoder("t/1 (a@b.c)", min_interval=0)
        coder.geocode("India Gate")
        coder.geocode("India Gate")
        assert len(counting_transport) == 1
        assert coder.cache_size == 1

    def test_different_queries_miss(self, counting_transport):
        coder = Geocoder("t/1 (a@b.c)", min_interval=0)
        coder.geocode("India Gate")
        coder.geocode("Red Fort")
        assert len(counting_transport) == 2

    def test_clearing_forces_a_refetch(self, counting_transport):
        coder = Geocoder("t/1 (a@b.c)", min_interval=0)
        coder.geocode("India Gate")
        coder.clear_cache()
        coder.geocode("India Gate")
        assert len(counting_transport) == 2
        assert coder.cache_size == 1

    def test_caching_off_always_refetches(self, counting_transport):
        coder = Geocoder("t/1 (a@b.c)", min_interval=0, cache=False)
        coder.geocode("India Gate")
        coder.geocode("India Gate")
        assert len(counting_transport) == 2

    def test_user_agent_is_sent(self, monkeypatch):
        import json as json_module

        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["agent"] = request.get_header("User-agent")
            return _FakeResponse(json_module.dumps([RAW_RESULT]).encode("utf-8"))

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        Geocoder("my-app/2.0 (me@example.com)", min_interval=0).geocode("India Gate")
        assert seen["agent"] == "my-app/2.0 (me@example.com)"

    def test_malformed_json_is_reported(self, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda request, timeout=None: _FakeResponse(b"<html>nope</html>"))
        coder = Geocoder("t/1 (a@b.c)", min_interval=0)
        with pytest.raises(GeocodingError) as exc:
            coder.geocode("India Gate")
        assert "wasn't JSON" in str(exc.value)

    def test_http_error_is_reported(self, monkeypatch):
        import urllib.error

        def fail(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", fail)
        coder = Geocoder("t/1 (a@b.c)", min_interval=0)
        with pytest.raises(GeocodingError) as exc:
            coder.geocode("India Gate")
        assert "429" in str(exc.value)

    def test_cache_can_be_disabled(self):
        assert Geocoder("t/1 (a@b.c)", cache=False).cache_size == 0

    def test_clear_cache_is_safe_without_one(self):
        Geocoder("t/1 (a@b.c)", cache=False).clear_cache()

    def test_cache_size_starts_empty(self):
        assert Geocoder("t/1 (a@b.c)").cache_size == 0


class TestRateLimiting:
    def test_throttle_spaces_calls(self):
        coder = Geocoder("t/1 (a@b.c)", min_interval=0.25)
        start = time.monotonic()
        coder._throttle()
        coder._throttle()
        assert time.monotonic() - start >= 0.24

    def test_zero_interval_does_not_sleep(self):
        coder = Geocoder("t/1 (a@b.c)", min_interval=0)
        start = time.monotonic()
        for _ in range(5):
            coder._throttle()
        assert time.monotonic() - start < 0.1

    def test_default_respects_the_public_policy(self):
        assert DEFAULT_MIN_INTERVAL >= 1.0


class TestNetworkErrors:
    def test_unreachable_host(self):
        coder = Geocoder("t/1 (a@b.c)", base_url="http://127.0.0.1:9", min_interval=0, timeout=1)
        with pytest.raises(GeocodingError) as exc:
            coder.geocode("anything")
        assert "Could not reach" in str(exc.value) or "Network error" in str(exc.value)

    def test_error_message_is_actionable(self):
        coder = Geocoder("t/1 (a@b.c)", base_url="http://127.0.0.1:9", min_interval=0, timeout=1)
        with pytest.raises(GeocodingError) as exc:
            coder.geocode("anything")
        assert "opt-in" in str(exc.value) or "network connection" in str(exc.value)


class TestHelpers:
    def test_normalise_flattens_the_address(self):
        result = _normalise(RAW_RESULT)
        assert result["state"] == "Delhi"
        assert result["postcode"] == "110001"

    def test_normalise_handles_a_missing_address(self):
        result = _normalise({"lat": "1", "lon": "2"})
        assert result["city"] == ""
        assert result["lat"] == 1.0

    def test_normalise_prefers_town_then_village(self):
        assert _normalise({"lat": "1", "lon": "2", "address": {"town": "Small"}})["city"] == "Small"
        assert _normalise({"lat": "1", "lon": "2", "address": {"village": "Tiny"}})["city"] == "Tiny"

    def test_to_feature(self):
        feature = to_feature(_normalise(RAW_RESULT))
        assert feature["type"] == "Feature"
        assert feature["geometry"]["coordinates"] == [77.2295, 28.6129]
        assert "lat" not in feature["properties"]

    def test_to_feature_merges_extra_properties(self):
        feature = to_feature(_normalise(RAW_RESULT), {"customer_id": 42})
        assert feature["properties"]["customer_id"] == 42


class TestEngineGeocoding:
    def test_fills_in_coordinates(self, monkeypatch):
        from qoregeo import geocode as geocode_module

        monkeypatch.setattr(geocode_module.Geocoder, "_get",
                            lambda self, endpoint, params, label: [RAW_RESULT])
        geo = GeoEngine().load_data(
            [{"type": "Feature",
              "geometry": {"type": "Point", "coordinates": [0, 0]},
              "properties": {"address": "India Gate"}}]
        )
        result = geo.geocode("address", user_agent="t/1 (a@b.c)")
        assert result.get_features()[0]["geometry"]["coordinates"] == [77.2295, 28.6129]
        assert "_geocoded" in result.get_features()[0]["properties"]

    def test_blank_addresses_are_left_alone(self, monkeypatch):
        from qoregeo import geocode as geocode_module

        monkeypatch.setattr(geocode_module.Geocoder, "_get", lambda self, endpoint, params, label: [RAW_RESULT])
        geo = GeoEngine().load_data(
            [{"type": "Feature",
              "geometry": {"type": "Point", "coordinates": [1, 2]},
              "properties": {"address": ""}}]
        )
        assert geo.geocode("address", user_agent="t/1 (a@b.c)").get_features()[0][
            "geometry"
        ]["coordinates"] == [1, 2]

    def test_missing_column_raises(self):
        from qoregeo.exceptions import ColumnNotFoundError

        geo = GeoEngine().load_data([(28.6, 77.2)])
        with pytest.raises(ColumnNotFoundError):
            geo.geocode("address", user_agent="t/1 (a@b.c)")
