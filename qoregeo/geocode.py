"""
qoregeo.geocode
===============
Address → coordinate lookup, and back again.

Every other part of QOREgeo works offline. Geocoding cannot: turning
"India Gate, New Delhi" into a latitude needs somebody's address database.
So this module is deliberately **opt-in**. Nothing here runs unless you
construct a :class:`Geocoder` yourself.

It talks to OpenStreetMap's Nominatim service over ``urllib`` from the
standard library, so there is still nothing to install. Nominatim's usage
policy is respected by construction:

* a real user agent is required, the service blocks generic ones
* requests are rate limited to one per second, in-process
* results are cached, so re-running a script doesn't re-hit the API

For bulk work (thousands of addresses), run your own Nominatim or a
commercial geocoder and point ``base_url`` at it.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .exceptions import GeocodingError

Coord = Tuple[float, float]

DEFAULT_BASE_URL = "https://nominatim.openstreetmap.org"
DEFAULT_MIN_INTERVAL = 1.0          # seconds between requests. Nominatim's rule
DEFAULT_TIMEOUT = 10.0


class Geocoder:
    """
    Forward and reverse geocoding against a Nominatim-compatible service.

    Parameters
    ----------
    user_agent   : identifies your app. Nominatim rejects requests without a
                   meaningful one, include contact details, e.g.
                   ``"store-locator/1.0 (ops@example.com)"``
    base_url     : point this at your own Nominatim instance for bulk work
    min_interval : seconds between requests (never set below 1.0 for the
                   public service)
    timeout      : per-request socket timeout in seconds
    cache        : remember results in memory for the life of the object

    Examples
    --------
    >>> coder = Geocoder(user_agent="my-app/1.0 (me@example.com)")
    >>> coder.geocode("India Gate, New Delhi")        # doctest: +SKIP
    {'lat': 28.6129, 'lng': 77.2295, 'display_name': 'India Gate, ...'}
    """

    def __init__(
        self,
        user_agent: str,
        base_url: str = DEFAULT_BASE_URL,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        cache: bool = True,
    ) -> None:
        if not user_agent or not str(user_agent).strip():
            raise GeocodingError(
                "<no query>",
                "A user_agent is required. Public Nominatim blocks anonymous "
                "clients outright.",
            )

        self.user_agent = str(user_agent).strip()
        self.base_url = str(base_url).rstrip("/")
        self.min_interval = max(0.0, float(min_interval))
        self.timeout = float(timeout)
        self._cache: Optional[Dict[str, Any]] = {} if cache else None
        self._last_call = 0.0
        self._lock = threading.Lock()

    # ── HTTP plumbing ───────────────────────────────────────────────────────

    def _throttle(self) -> None:
        """Sleep just long enough to stay inside the rate limit."""
        with self._lock:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_call = time.monotonic()

    def _get(self, endpoint: str, params: Dict[str, Any], label: str) -> Any:
        key = f"{endpoint}?{urllib.parse.urlencode(sorted(params.items()))}"
        if self._cache is not None and key in self._cache:
            return self._cache[key]

        url = f"{self.base_url}/{endpoint}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
        )

        self._throttle()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise GeocodingError(
                label, f"The geocoding service returned HTTP {exc.code} ({exc.reason})."
            ) from exc
        except urllib.error.URLError as exc:
            raise GeocodingError(
                label, f"Could not reach {self.base_url}, {exc.reason}."
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise GeocodingError(label, f"Network error: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise GeocodingError(
                label, "The geocoding service returned something that wasn't JSON."
            ) from exc

        if self._cache is not None:
            self._cache[key] = payload
        return payload

    # ── public API ──────────────────────────────────────────────────────────

    def geocode(
        self,
        address: str,
        country: Optional[str] = None,
        limit: int = 1,
    ) -> Optional[Dict[str, Any]]:
        """
        Look up one address. Returns the best match, or ``None`` if nothing matched.

        Parameters
        ----------
        country : ISO 3166-1 alpha-2 code (``"in"``, ``"us"``) to narrow the search
        """
        if not address or not str(address).strip():
            raise GeocodingError(str(address), "The address is empty.")

        params: Dict[str, Any] = {
            "q": str(address).strip(),
            "format": "jsonv2",
            "limit": max(1, int(limit)),
            "addressdetails": 1,
        }
        if country:
            params["countrycodes"] = str(country).lower()

        results = self._get("search", params, str(address))
        if not results:
            return None
        return _normalise(results[0])

    def geocode_many(
        self,
        addresses: List[str],
        country: Optional[str] = None,
        skip_failures: bool = True,
    ) -> List[Optional[Dict[str, Any]]]:
        """
        Geocode a list of addresses, in order.

        Rate limiting makes this roughly one address per second against the
        public service, budget accordingly, or run your own instance.
        """
        out: List[Optional[Dict[str, Any]]] = []
        for address in addresses:
            try:
                out.append(self.geocode(address, country=country))
            except GeocodingError:
                if not skip_failures:
                    raise
                out.append(None)
        return out

    def reverse(self, lat: float, lng: float, zoom: int = 18) -> Optional[Dict[str, Any]]:
        """
        Coordinate → nearest address.

        ``zoom`` controls granularity: 18 is building level, 10 is city,
        3 is country.
        """
        params = {
            "lat": float(lat),
            "lon": float(lng),
            "format": "jsonv2",
            "zoom": int(zoom),
            "addressdetails": 1,
        }
        result = self._get("reverse", params, f"({lat}, {lng})")
        if not result or "error" in result:
            return None
        return _normalise(result)

    def clear_cache(self) -> None:
        """Drop remembered results."""
        if self._cache is not None:
            self._cache.clear()

    @property
    def cache_size(self) -> int:
        """How many responses are currently cached."""
        return len(self._cache) if self._cache is not None else 0

    def __repr__(self) -> str:
        return f"<Geocoder {self.base_url} · {self.cache_size} cached>"


def _normalise(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a Nominatim record into the shape the rest of QOREgeo speaks."""
    address = raw.get("address") or {}
    return {
        "lat": float(raw.get("lat", 0.0)),
        "lng": float(raw.get("lon", 0.0)),
        "display_name": raw.get("display_name", ""),
        "type": raw.get("type") or raw.get("category", ""),
        "importance": raw.get("importance"),
        "osm_id": raw.get("osm_id"),
        "city": address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("suburb", ""),
        "state": address.get("state", ""),
        "postcode": address.get("postcode", ""),
        "country": address.get("country", ""),
        "country_code": address.get("country_code", ""),
        "address": address,
    }


def to_feature(result: Dict[str, Any], extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Turn a geocoding result into a GeoJSON Feature ready for ``load_data()``."""
    props = {
        k: v for k, v in result.items() if k not in ("lat", "lng", "address")
    }
    if extra:
        props.update(extra)
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [result["lng"], result["lat"]]},
        "properties": props,
    }
