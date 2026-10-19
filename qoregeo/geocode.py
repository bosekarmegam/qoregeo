"""
qoregeo.geocode
===============
Forward and reverse geocoding with the standard library only (``urllib``).

Turn an address or place name into coordinates, and coordinates back into an
address, using open-source OpenStreetMap geocoders:

* ``"nominatim"`` — https://nominatim.openstreetmap.org  (default)
* ``"photon"``    — https://photon.komoot.io

Usage:
    from qoregeo.geocode import Geocoder

    gc = Geocoder(user_agent="my-app/1.0 (you@example.com)")
    place = gc.geocode("Brandenburger Tor, Berlin")
    place["lat"], place["lng"]            # (52.5162746, 13.3777041)
    gc.reverse(48.8584, 2.2945)["name"]   # 'Tour Eiffel, 5, Avenue Gustave Eiffel, …'

Every result is a plain dict::

    {"lat": float, "lng": float, "name": str, "category": str | None,
     "type": str | None, "importance": float | None,
     "bbox": [west, south, east, north] | None, "address": dict | None,
     "provider": "nominatim" | "photon", "attribution": str}

``address`` uses Nominatim's keys for both providers (``house_number``,
``road``, ``suburb``, ``city``, ``state``, ``postcode``, ``country``,
``country_code`` …).

Fair use of the free public servers
-----------------------------------
The public Nominatim and Photon servers are community resources. The Nominatim
usage policy (https://operations.osmfoundation.org/policies/nominatim/) asks for:

* an absolute maximum of 1 request per second — enforced here for the public
  endpoints across threads *and processes* (``min_interval`` can only raise it
  within a process);
* an identifying User-Agent — qoregeo sends one; pass your own application
  name and a contact (``user_agent=...``, ``email=...``) for real projects;
* caching of results — every ``Geocoder`` keeps an LRU cache of answers, and
  answers from the public servers are also kept on disk for 30 days;
* no bulk geocoding of large lists and no autocomplete-as-you-type;
* visible attribution — every result carries an ``attribution`` string
  ("Data © OpenStreetMap contributors, ODbL 1.0").

The shared request slot and the on-disk answers live in one small SQLite file,
``geocode-v1.sqlite3``, in the per-user cache directory: ``$XDG_CACHE_HOME`` or
``~/.cache`` (``~/Library/Caches`` on macOS) or ``%LOCALAPPDATA%`` on Windows,
in a ``qoregeo`` folder. So a shell loop of ``qoregeo geocode`` calls, or several
MCP servers, still send at most 1 request per second and do not repeat
questions already answered. Choose another directory with
``Geocoder(cache_dir=...)`` or the ``QOREGEO_CACHE_DIR`` environment variable;
turn it off with ``Geocoder(cache_dir=None)`` or ``QOREGEO_CACHE_DIR=off``. If
the directory is not writable (or Python lacks ``sqlite3``), the limit falls
back to applying within the process only.

There is deliberately no batch method. To geocode thousands of addresses, run
your own server (Nominatim and Photon are open source and ship Docker images)
and point ``url=`` at it — custom endpoints are not throttled by default and
their answers are not stored on disk.
"""

from __future__ import annotations

import contextlib
import copy
import http.client
import json
import math
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import quote, urlencode, urlsplit

from ._version import __version__
from .exceptions import GeocodingError, InvalidArgumentError, InvalidCoordinateError
from .utils import _validate_coord

if TYPE_CHECKING:
    import sqlite3

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_USER_AGENT = f"qoregeo/{__version__} (+https://github.com/bosekarmegam/qoregeo)"

OSM_ATTRIBUTION = "Data © OpenStreetMap contributors, ODbL 1.0. https://osm.org/copyright"

# Hosts run as free public services: requests to them are always spaced by at
# least PUBLIC_MIN_INTERVAL seconds (their usage policies' 1 request/second).
# photon.komoot.de is Photon's former address (it redirects to photon.komoot.io).
PUBLIC_HOSTS = frozenset({"nominatim.openstreetmap.org", "photon.komoot.io",
                          "photon.komoot.de"})
PUBLIC_MIN_INTERVAL = 1.0

_MAX_LIMIT = 50                       # largest `limit` accepted by search()
_MAX_ZOOM = 18                        # Nominatim's reverse zoom range is 0..18
_MAX_SECONDS = 86_400.0               # upper bound for timeout / min_interval (1 day)
_RETRY_DELAY = 1.0                    # pause before the single retry
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
# One Accept-Language entry: a tag (or "*") with an optional RFC 9110 q-value.
_LANGUAGE_RE = re.compile(
    r"(?:\*|[A-Za-z0-9_]{1,8}(?:-[A-Za-z0-9_]{1,8})*)"
    r"(?:\s*;\s*[qQ]\s*=\s*(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?))?"
)


@dataclass(frozen=True)
class _ProviderSpec:
    label: str
    default_url: str
    search_path: str
    reverse_path: str
    attribution: str
    max_limit: int


_PROVIDERS: dict[str, _ProviderSpec] = {
    "nominatim": _ProviderSpec(
        label="Nominatim",
        default_url="https://nominatim.openstreetmap.org",
        search_path="/search",
        reverse_path="/reverse",
        attribution=OSM_ATTRIBUTION,
        max_limit=40,                 # Nominatim never returns more than 40
    ),
    "photon": _ProviderSpec(
        label="Photon",
        default_url="https://photon.komoot.io",
        search_path="/api",
        reverse_path="/reverse",
        attribution=f"{OSM_ATTRIBUTION} · Geocoding by Photon",
        max_limit=_MAX_LIMIT,
    ),
}

PROVIDERS: tuple[str, ...] = tuple(_PROVIDERS)


# ─────────────────────────────────────────────────────────────────────────────
# On-disk state shared by processes (public servers only)
# ─────────────────────────────────────────────────────────────────────────────
CACHE_DIR_ENV = "QOREGEO_CACHE_DIR"   # overrides the cache directory; "off" disables it
CACHE_TTL = 30 * 86_400.0             # seconds an answer from a public server is reused
_CACHE_OFF = frozenset({"0", "off", "no", "none", "false", "disable", "disabled"})
_DB_NAME = "geocode-v1.sqlite3"
_DB_SCHEMA = 1                        # PRAGMA user_version of a ready database
_DB_TIMEOUT = 10.0                    # seconds to wait while another process holds the lock
_DB_MAX_ROWS = 10_000                 # answers kept on disk (the oldest go first)
# A booked slot further ahead than this is stale or corrupt (a legitimate queue
# is one interval per waiting request) and is ignored rather than waited for.
_MAX_QUEUE_SECONDS = 300.0

_T = TypeVar("_T")


def _default_cache_dir(environ: Mapping[str, str] | None = None,
                       platform: str | None = None) -> str | None:
    """The per-user cache directory for qoregeo, or ``None`` when it is turned off
    (``QOREGEO_CACHE_DIR=off``) or no home directory can be found."""
    env = os.environ if environ is None else environ
    plat = sys.platform if platform is None else platform
    override = env.get(CACHE_DIR_ENV, "").strip()
    if override:
        if override.lower() in _CACHE_OFF:
            return None
        return os.path.abspath(os.path.expanduser(override))
    home = env.get("USERPROFILE" if plat == "win32" else "HOME") or os.path.expanduser("~")
    if plat == "win32":
        base = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    elif os.path.isabs(env.get("XDG_CACHE_HOME", "")):   # relative values are invalid (XDG)
        base = env["XDG_CACHE_HOME"]
    elif plat == "darwin":
        base = os.path.join(home, "Library", "Caches")
    else:
        base = os.path.join(home, ".cache")
    if not os.path.isabs(base):            # no usable home directory
        return None
    return os.path.join(base, "qoregeo")


def _check_cache_dir(value: Any) -> str | None:
    """``True`` → the default directory, ``None`` / ``False`` → none, or a path."""
    if value is True:
        return _default_cache_dir()
    if value is None or value is False:
        return None
    path: Any = None
    if isinstance(value, (str, os.PathLike)):
        with contextlib.suppress(TypeError):
            path = os.fspath(value)
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise InvalidArgumentError(
            "cache_dir", value,
            "a directory path, True (the per-user cache, the default) or None (memory only)",
            "Geocoder(cache_dir='./geocode-cache')   or   Geocoder(cache_dir=None)",
        )
    return os.path.abspath(os.path.expanduser(path))


class _DiskStore:
    """The SQLite file through which processes share the public servers' request
    slot and answers. A short-lived connection per operation keeps it safe across
    threads; ``BEGIN IMMEDIATE`` serialises bookings across processes on every OS.

    Nothing here raises: if ``sqlite3`` is missing or the directory or file is
    unusable, the store marks itself broken and every call returns ``None``, so
    the caller falls back to the in-process behaviour.
    """

    def __init__(self, directory: str) -> None:
        self.directory = directory
        self.path = os.path.join(directory, _DB_NAME)
        self.broken = False

    def _open(self) -> sqlite3.Connection:
        import sqlite3

        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        # Create the file owner-only (on POSIX): it holds the queries. A symlink in
        # its place is refused (O_NOFOLLOW) and so falls back to memory only.
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        os.close(os.open(self.path, flags, 0o600))
        con = sqlite3.connect(self.path, timeout=_DB_TIMEOUT, isolation_level=None)
        try:
            if con.execute("PRAGMA user_version").fetchone()[0] != _DB_SCHEMA:
                con.execute("BEGIN IMMEDIATE")
                con.execute("CREATE TABLE IF NOT EXISTS slots (endpoint TEXT PRIMARY KEY,"
                            " start REAL NOT NULL, booked_at REAL NOT NULL)")
                con.execute("CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY,"
                            " endpoint TEXT NOT NULL, value TEXT NOT NULL,"
                            " expires REAL NOT NULL)")
                con.execute("CREATE INDEX IF NOT EXISTS results_expires ON results (expires)")
                con.execute(f"PRAGMA user_version = {_DB_SCHEMA}")
                con.execute("COMMIT")
        except BaseException:
            con.close()                    # also rolls back an open transaction
            raise
        return con

    def _run(self, action: Callable[[sqlite3.Connection], _T]) -> _T | None:
        if self.broken:
            return None
        try:
            import sqlite3
        except ImportError:                # Python built without SQLite
            self.broken = True
            return None
        try:
            con = self._open()
        except (sqlite3.Error, OSError, ValueError):
            self.broken = True
            return None
        try:
            return action(con)
        except (sqlite3.Error, OSError, ValueError):
            self.broken = True
            return None
        finally:
            con.close()                    # rolls back anything left uncommitted

    def book_slot(self, endpoint: str, interval: float) -> float | None:
        """Book the next request to ``endpoint`` for every process sharing this file:
        at least ``interval`` s after the previous booking. Returns the seconds to
        wait first, or ``None`` if the file cannot be used."""
        def book(con: sqlite3.Connection) -> float:
            con.execute("BEGIN IMMEDIATE")   # waits while another process books
            row = con.execute("SELECT start, booked_at FROM slots WHERE endpoint = ?",
                              (endpoint,)).fetchone()
            now = time.time()
            start = now
            last = _float_or_none(row[0]) if row else None
            booked_at = _float_or_none(row[1]) if row else None
            if last is not None and booked_at is not None:
                if booked_at > now:          # the clock was set back since then
                    last -= booked_at - now
                if last - now <= _MAX_QUEUE_SECONDS:
                    start = max(now, last + max(interval, 0.0))
            con.execute("INSERT OR REPLACE INTO slots (endpoint, start, booked_at)"
                        " VALUES (?, ?, ?)", (endpoint, start, now))
            con.execute("COMMIT")
            return start - now

        return self._run(book)

    def get_results(self, key: str) -> list[dict[str, Any]] | None:
        """A stored, unexpired answer for ``key`` — or ``None``."""
        def get(con: sqlite3.Connection) -> list[dict[str, Any]] | None:
            row = con.execute("SELECT value, expires FROM results WHERE key = ?",
                              (key,)).fetchone()
            expires = _float_or_none(row[1]) if row else None
            now = time.time()
            if (row is None or expires is None or not now < expires <= now + CACHE_TTL
                    or not isinstance(row[0], str)):
                return None
            try:
                value = json.loads(row[0])
            except (ValueError, RecursionError):
                return None
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                return value
            return None

        return self._run(get)

    def put_results(self, key: str, endpoint: str, results: list[dict[str, Any]]) -> None:
        """Store an answer for ``CACHE_TTL`` seconds; drop expired and excess ones."""
        try:   # ASCII-only JSON: lone surrogates from a server cannot break the binding
            value = json.dumps(results, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return

        def put(con: sqlite3.Connection) -> None:
            now = time.time()
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT OR REPLACE INTO results (key, endpoint, value, expires)"
                        " VALUES (?, ?, ?, ?)", (key, endpoint, value, now + CACHE_TTL))
            con.execute("DELETE FROM results WHERE expires <= ?", (now,))
            con.execute("DELETE FROM results WHERE key IN (SELECT key FROM results"
                        " ORDER BY expires DESC LIMIT -1 OFFSET ?)", (_DB_MAX_ROWS,))
            con.execute("COMMIT")

        self._run(put)

    def clear_results(self, endpoint: str) -> None:
        """Forget every stored answer from ``endpoint``."""
        def clear(con: sqlite3.Connection) -> None:
            con.execute("DELETE FROM results WHERE endpoint = ?", (endpoint,))

        self._run(clear)


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiting
# ─────────────────────────────────────────────────────────────────────────────
# Slot reservation: each request books the earliest start time that is at
# least `interval` after the previously booked one for the same endpoint, then
# sleeps (outside the lock) until its slot. Concurrent threads and separate
# Geocoder instances therefore never exceed the rate together. For the public
# servers a second booking in the shared _DiskStore then spaces the requests of
# all processes on the machine by PUBLIC_MIN_INTERVAL.
_RATE_LOCK = threading.Lock()
_NEXT_SLOT: dict[str, float] = {}


def _throttle(key: str, interval: float, store: _DiskStore | None = None) -> None:
    """Block until a request to endpoint ``key`` may start: ``interval`` s after
    this process's previous one and, with a ``store``, ``PUBLIC_MIN_INTERVAL`` s
    after the previous one of any process using that store."""
    with _RATE_LOCK:
        now = time.monotonic()
        last = _NEXT_SLOT.get(key)
        start = now if last is None else max(now, last + max(interval, 0.0))
        _NEXT_SLOT[key] = start
    if start > now:
        time.sleep(start - now)
    # The shared booking comes after the local wait, so the file only ever holds
    # requests that are ready to go (a large min_interval never blocks others).
    wait = store.book_slot(key, PUBLIC_MIN_INTERVAL) if store is not None else None
    if wait is not None and wait > 0:
        with _RATE_LOCK:                   # later local bookings follow this start
            begin = time.monotonic() + wait
            _NEXT_SLOT[key] = max(begin, _NEXT_SLOT.get(key, begin))
        time.sleep(wait)


def _reset_rate_limits() -> None:
    """Forget all booked request slots in this process (used by the test-suite)."""
    with _RATE_LOCK:
        _NEXT_SLOT.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Validation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_provider(provider: Any) -> str:
    name = provider.strip().lower() if isinstance(provider, str) else None
    if name not in _PROVIDERS:
        raise InvalidArgumentError(
            "provider", provider, f"one of {', '.join(repr(p) for p in PROVIDERS)}",
            "Geocoder(provider='photon')",
        )
    return name


def _check_url(url: Any, provider: str) -> str:
    """Validate a base URL and return it normalised (lower-case scheme and host,
    IDNA host, percent-encoded path, no trailing slash)."""
    example = f"Geocoder(provider='{provider}', url='http://localhost:8080')"
    if not isinstance(url, str) or not url.strip():
        raise InvalidArgumentError("url", url, "an http:// or https:// base URL", example)
    text = url.strip()
    if any(ch.isspace() or not ch.isprintable() for ch in text):
        raise InvalidArgumentError("url", url, "a URL without spaces or control characters",
                                   example)
    if "?" in text or "#" in text:
        raise InvalidArgumentError(
            "url", url, "a base URL without '?query' or '#fragment'", example
        )
    if "\\" in text:
        raise InvalidArgumentError("url", url, "a URL with forward slashes only", example)
    try:
        parts = urlsplit(text)
        port = parts.port            # raises ValueError for a malformed port
    except ValueError:
        raise InvalidArgumentError("url", url, "a valid http(s) URL", example) from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        raise InvalidArgumentError(
            "url", url, "an http:// or https:// base URL with a host name", example
        )
    if "@" in parts.netloc:
        raise InvalidArgumentError(
            "url", url, "a URL without 'user:password@' (credentials are not supported "
            "and would leak into error messages)", example,
        )
    host = parts.hostname
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise InvalidArgumentError("url", url, "a valid host name", example) from None
    if ":" in host:                  # IPv6 literal
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    path = quote(parts.path, safe="/%!$&'()*+,;=:@~")
    return f"{scheme}://{netloc}{path}".rstrip("/")


def _endpoint_key(base_url: str) -> tuple[str, bool]:
    """Return ``(rate-limit key, is_public)`` for a normalised base URL.

    Public servers are keyed by host name alone, so ``https://host``,
    ``https://host:443`` and ``http://host`` share one 1 request/second budget.
    """
    parts = urlsplit(base_url)
    host = (parts.hostname or "").rstrip(".")
    if host in PUBLIC_HOSTS:
        return host, True
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{host}:{port}", False


def _check_text(name: str, value: Any, expected: str, example: str) -> str:
    """A non-empty, printable one-line string (no control or format characters)."""
    if (not isinstance(value, str) or not value.strip()
            or any(not ch.isprintable() for ch in value)):
        raise InvalidArgumentError(name, value, expected, example)
    return value.strip()


def _check_user_agent(value: Any) -> str:
    expected = "a non-empty one-line string naming your app (Latin-1 characters only)"
    example = "Geocoder(user_agent='my-app/1.0 (you@example.com)')"
    text = _check_text("user_agent", value, expected, example)
    try:
        text.encode("latin-1")       # what http.client can put in a header
    except UnicodeEncodeError:
        raise InvalidArgumentError("user_agent", value, expected, example) from None
    return text


def _check_email(email: Any) -> str:
    example = "Geocoder(email='you@example.com')"
    expected = "an e-mail address such as 'you@example.com'"
    text = _check_text("email", email, expected, example)
    local, at, domain = text.partition("@")
    if not at or not local or not domain or any(ch.isspace() for ch in text):
        raise InvalidArgumentError("email", email, expected, example)
    return text


def _check_language(language: Any) -> str:
    example = "Geocoder(language='en')  or  Geocoder(language='de,en;q=0.8')"
    expected = "a language code like 'en', or an Accept-Language list like 'de,en;q=0.8'"
    if (not isinstance(language, str) or not language.strip()
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in language)):
        raise InvalidArgumentError("language", language, expected, example)
    tags = [t.strip() for t in language.split(",")]
    if not all(_LANGUAGE_RE.fullmatch(t) for t in tags):
        raise InvalidArgumentError("language", language, expected, example)
    return ",".join(tags)


def _check_interval(value: Any) -> float:
    if (not _is_number(value) or not math.isfinite(value)
            or not 0 <= value <= _MAX_SECONDS):
        raise InvalidArgumentError("min_interval", value,
                                   f"a number of seconds from 0 to {_MAX_SECONDS:g}",
                                   "Geocoder(min_interval=2.0)")
    return float(value)


def _check_timeout(value: Any) -> float:
    if (not _is_number(value) or not math.isfinite(value)
            or not 0 < value <= _MAX_SECONDS):
        raise InvalidArgumentError("timeout", value,
                                   f"a number of seconds > 0 and <= {_MAX_SECONDS:g}",
                                   "Geocoder(timeout=30)")
    return float(value)


def _check_cache_size(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidArgumentError("cache_size", value, "a whole number >= 0 (0 disables it)",
                                   "Geocoder(cache_size=1024)")
    return value


def _check_query(query: Any) -> str:
    """A non-empty query; runs of whitespace (incl. newlines) collapse to one space."""
    text = " ".join(query.split()) if isinstance(query, str) else ""
    # Control characters and lone surrogates (e.g. from a mis-decoded file) cannot
    # be sent; format characters such as ZWJ / ZWNJ are legitimate in many scripts.
    if not text or any(unicodedata.category(ch) in ("Cc", "Cs") for ch in text):
        raise InvalidArgumentError("query", query, "a non-empty address or place name",
                                   "gc.geocode('Brandenburger Tor, Berlin')")
    return text


def _check_limit(limit: Any) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_LIMIT:
        raise InvalidArgumentError("limit", limit, f"a whole number from 1 to {_MAX_LIMIT}",
                                   "gc.search('Springfield', limit=10)")
    return limit


def _check_country_codes(codes: Any) -> list[str]:
    """Normalise ``'de'`` / ``'de,fr'`` / ``['de', 'FR']`` to ``['de', 'fr']``."""
    if codes is None:
        return []
    expected = "a 2-letter ISO 3166-1 country code or a list of them (e.g. 'de' or ['de', 'fr'])"
    example = "gc.search('Springfield', country_codes=['us', 'ca'])"
    if isinstance(codes, str):
        items: list[Any] = codes.split(",")
    elif isinstance(codes, (list, tuple)):
        items = list(codes)
    else:
        raise InvalidArgumentError("country_codes", codes, expected, example)
    result: list[str] = []
    for item in items:
        code = item.strip() if isinstance(item, str) else None
        if code is None or len(code) != 2 or not (code.isascii() and code.isalpha()):
            raise InvalidArgumentError("country_codes", codes, expected, example)
        if code.lower() not in result:
            result.append(code.lower())
    if not result:
        raise InvalidArgumentError("country_codes", codes, expected, example)
    return result


def _check_zoom(zoom: Any) -> int:
    if not isinstance(zoom, int) or isinstance(zoom, bool) or not 0 <= zoom <= _MAX_ZOOM:
        raise InvalidArgumentError(
            "zoom", zoom, f"a whole number from 0 (country) to {_MAX_ZOOM} (building)",
            "gc.reverse(48.8584, 2.2945, zoom=10)   ← city level",
        )
    return zoom


# ─────────────────────────────────────────────────────────────────────────────
# Response parsing
# ─────────────────────────────────────────────────────────────────────────────

def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _point_or_none(lat: Any, lng: Any) -> tuple[float, float] | None:
    try:
        return _validate_coord(lat, lng)
    except InvalidCoordinateError:
        return None


def _text_or_none(value: Any) -> str | None:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def _coord_label(lat: float, lng: float) -> str:
    return f"{lat:.6f}, {lng:.6f}"


def _coord_param(value: float) -> str:
    """Fixed-point text for a URL parameter: 7 decimals (~1 cm), no exponent, no '-0'."""
    text = f"{round(value, 7):.7f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _clean_text(value: Any, limit: int = 300) -> str:
    """Server-supplied text made safe to print: control characters (e.g. ANSI
    escapes) become spaces, whitespace is collapsed and the length is capped."""
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Cs") else ch
                   for ch in str(value)[: limit * 4])
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _clean_address(raw: Any) -> dict[str, str] | None:
    """A flat ``{key: text}`` address; nested or non-text values are dropped."""
    if not isinstance(raw, dict):
        return None
    address: dict[str, str] = {}
    for key, value in raw.items():
        text = _text_or_none(value)
        if isinstance(key, str) and text is not None:
            address[key] = text
    return address or None


def _parse_nominatim_place(item: Any, attribution: str) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    point = _point_or_none(item.get("lat"), item.get("lon"))
    if point is None:
        return None
    lat, lng = point

    bbox: list[float] | None = None
    raw_box = item.get("boundingbox")
    if isinstance(raw_box, (list, tuple)) and len(raw_box) == 4:
        south, north, west, east = (_float_or_none(v) for v in raw_box)   # Nominatim order
        if south is not None and north is not None and west is not None and east is not None:
            bbox = [west, south, east, north]

    licence = item.get("licence")
    name = (_text_or_none(item.get("display_name")) or _text_or_none(item.get("name"))
            or _coord_label(lat, lng))
    return {
        "lat": lat,
        "lng": lng,
        "name": name,
        "category": _text_or_none(item.get("category") or item.get("class")),
        "type": _text_or_none(item.get("type")),
        "importance": _float_or_none(item.get("importance")),
        "bbox": bbox,
        "address": _clean_address(item.get("address")),
        "provider": "nominatim",
        "attribution": (_clean_text(licence) if isinstance(licence, str) and licence.strip()
                        else attribution),
    }


# Photon property -> Nominatim-style address key (same keys for both providers).
_PHOTON_ADDRESS_KEYS: tuple[tuple[str, str], ...] = (
    ("housenumber", "house_number"),
    ("street", "road"),
    ("locality", "neighbourhood"),
    ("district", "suburb"),
    ("city", "city"),
    ("county", "county"),
    ("state", "state"),
    ("postcode", "postcode"),
    ("country", "country"),
)

# Order of the parts in a Photon label (mirrors Nominatim's display_name).
_PHOTON_LABEL_KEYS = ("name", "housenumber", "street", "locality", "district", "city",
                      "county", "state", "postcode", "country")


def _parse_photon_feature(feature: Any, attribution: str) -> dict[str, Any] | None:
    if not isinstance(feature, dict):
        return None
    geometry = feature.get("geometry")
    props = feature.get("properties")
    if not isinstance(geometry, dict) or not isinstance(props, dict):
        return None
    coords = geometry.get("coordinates")
    if (geometry.get("type") != "Point" or not isinstance(coords, (list, tuple))
            or len(coords) < 2):
        return None
    point = _point_or_none(coords[1], coords[0])   # GeoJSON is [lng, lat]
    if point is None:
        return None
    lat, lng = point

    bbox: list[float] | None = None
    extent = props.get("extent")   # Photon: [minLon, maxLat, maxLon, minLat]
    if isinstance(extent, (list, tuple)) and len(extent) == 4:
        west, lat_a, east, lat_b = (_float_or_none(v) for v in extent)
        if west is not None and east is not None and lat_a is not None and lat_b is not None:
            bbox = [west, min(lat_a, lat_b), east, max(lat_a, lat_b)]

    address: dict[str, str] = {}
    for src, dst in _PHOTON_ADDRESS_KEYS:
        text = _text_or_none(props.get(src))
        if text is not None:
            address[dst] = text
    country_code = _text_or_none(props.get("countrycode"))
    if country_code is not None:
        address["country_code"] = country_code.lower()

    parts: list[str] = []
    for key in _PHOTON_LABEL_KEYS:
        text = _text_or_none(props.get(key))
        if text is not None and text.casefold() not in (p.casefold() for p in parts):
            parts.append(text)

    return {
        "lat": lat,
        "lng": lng,
        "name": ", ".join(parts) or _coord_label(lat, lng),
        "category": _text_or_none(props.get("osm_key")),
        "type": _text_or_none(props.get("osm_value")),
        "importance": None,
        "bbox": bbox,
        "address": address or None,
        "provider": "photon",
        "attribution": attribution,
    }


def _error_text(data: Any) -> str | None:
    """The message of a JSON error document: ``{"error": "…"}``,
    ``{"error": {"message": "…"}}`` (Nominatim) or ``{"message": "…"}`` (Photon)."""
    message: Any = None
    if isinstance(data, dict):
        message = data.get("error", data.get("message"))
        if isinstance(message, dict):
            message = message.get("message")
    if isinstance(message, str) and message.strip():
        return _clean_text(message)
    return None


def _server_message(body: bytes) -> str | None:
    """Extract a short error message from an HTTP error body, if it is JSON."""
    try:
        return _error_text(json.loads(body.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Geocoder
# ─────────────────────────────────────────────────────────────────────────────

class Geocoder:
    """
    Forward / reverse geocoder for Nominatim and Photon (stdlib only).

    Args:
        provider    : ``"nominatim"`` (default) or ``"photon"``.
        url         : Base URL of a self-hosted server of that provider type,
                      e.g. ``"http://localhost:8080"``. Default: the public server.
                      No ``?query``, ``#fragment`` or ``user:password@``; the
                      stored ``url`` is normalised (lower-case host, IDNA,
                      percent-encoded path, no trailing slash).
        user_agent  : Identifies your application, e.g.
                      ``"my-app/1.0 (you@example.com)"`` (one line, Latin-1). Default:
                      ``"qoregeo/<version> (+https://github.com/bosekarmegam/qoregeo)"``.
        email       : Contact address sent to Nominatim with each request
                      (recommended by its usage policy for regular use).
        min_interval: Seconds between requests to the same server (0 … 86400).
                      Public servers are always throttled to at least 1.0 s,
                      however their URL is spelled; custom ``url`` servers
                      default to 0 (no throttling).
        timeout     : Seconds to wait for a response (default 10, at most 86400).
        cache_size  : Number of answers kept in the LRU cache (0 disables result
                      caching, on disk too).
        language    : Preferred result language — ``"en"`` or an Accept-Language
                      list such as ``"de,en;q=0.8"`` (Photon uses the first one).
        cache_dir   : Where the public servers' shared request slot and answers
                      (kept 30 days) are stored, so the 1 request/second limit and
                      the cache also hold across processes. ``True`` (default): the
                      per-user cache directory, or ``$QOREGEO_CACHE_DIR``. ``None``
                      or ``False``: memory only. Not used for custom ``url`` servers;
                      an unwritable directory falls back to memory only.

    Example:
        >>> gc = Geocoder(user_agent="my-app/1.0 (you@example.com)")
        >>> gc.geocode("Eiffel Tower")["lat"]                  # doctest: +SKIP
        48.8582602
        >>> gc.search("Springfield", limit=3, country_codes="us")   # doctest: +SKIP

    Bulk geocoding (thousands of rows) must use your own server — the public
    ones forbid it. There is intentionally no batch method.
    """

    def __init__(
        self,
        provider: str = "nominatim",
        url: str | None = None,
        user_agent: str | None = None,
        email: str | None = None,
        min_interval: float | None = None,
        timeout: float = 10.0,
        cache_size: int = 1024,
        language: str | None = None,
        cache_dir: str | os.PathLike[str] | bool | None = True,
    ) -> None:
        self.provider: str = _check_provider(provider)
        self._spec = _PROVIDERS[self.provider]
        self.url: str = _check_url(self._spec.default_url if url is None else url,
                                   self.provider)
        self.user_agent: str = (DEFAULT_USER_AGENT if user_agent is None
                                else _check_user_agent(user_agent))
        self.email: str | None = None if email is None else _check_email(email)
        self.timeout: float = _check_timeout(timeout)
        self.cache_size: int = _check_cache_size(cache_size)
        self.language: str | None = None if language is None else _check_language(language)

        self._rate_key, self.is_public = _endpoint_key(self.url)
        interval = None if min_interval is None else _check_interval(min_interval)
        if self.is_public:
            interval = max(PUBLIC_MIN_INTERVAL, interval or 0.0)
        self.min_interval: float = interval or 0.0

        directory = _check_cache_dir(cache_dir)
        #: Directory of the on-disk state this geocoder uses (``None``: memory only).
        self.cache_dir: str | None = directory if self.is_public else None
        self._store = _DiskStore(self.cache_dir) if self.cache_dir else None

        self._cache: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        self._cache_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"Geocoder(provider={self.provider!r}, url={self.url!r})"

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        limit: int = 5,
        country_codes: str | list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Find places matching an address or place name, best match first.

        Args:
            query        : Free-form text, e.g. ``"10 Downing St, London"``.
            limit        : Maximum number of results, 1–50 (Nominatim caps at 40).
            country_codes: Restrict to ISO 3166-1 alpha-2 countries —
                           ``"de"``, ``"de,fr"`` or ``["de", "fr"]``.

        Returns:
            A list of result dicts (possibly empty).

        Raises:
            InvalidArgumentError: for a bad query, limit or country code.
            GeocodingError      : when the server cannot be reached or fails.
        """
        text = _check_query(query)
        count = _check_limit(limit)
        codes = _check_country_codes(country_codes)
        if self.provider == "nominatim":
            params: list[tuple[str, str]] = [
                ("q", text), ("format", "jsonv2"), ("addressdetails", "1"),
                ("limit", str(min(count, self._spec.max_limit))),
            ]
            if codes:
                params.append(("countrycodes", ",".join(codes)))
            params += self._nominatim_extras()
        else:
            params = [("q", text), ("limit", str(count))]
            params += self._photon_extras()
            params += [("countrycode", c.upper()) for c in codes]
        url = f"{self.url}{self._spec.search_path}?{urlencode(params)}"
        # The key includes `count`: Nominatim's URL caps limit at 40, the parse does not.
        return self._cached(url, lambda payload: self._parse_search(payload, codes, count),
                            key=f"{url}#{count}")

    def geocode(
        self,
        query: str,
        country_codes: str | list[str] | None = None,
    ) -> dict[str, Any] | None:
        """
        Return the best match for ``query`` — or ``None`` if nothing matched.

        Example:
            >>> gc.geocode("Taj Mahal, Agra")   # doctest: +SKIP
            {'lat': 27.1750123, 'lng': 78.0420968, 'name': 'Taj Mahal, …', …}
        """
        results = self.search(query, limit=1, country_codes=country_codes)
        return results[0] if results else None

    def reverse(self, lat: float, lng: float, zoom: int = 18) -> dict[str, Any] | None:
        """
        Return the address at ``(lat, lng)`` — or ``None`` if there is none.

        Args:
            lat, lng: The coordinate (WGS-84 degrees).
            zoom    : Address detail for Nominatim, 0 (country) … 10 (city) …
                      18 (building, default). Photon always returns the nearest place.

        Raises:
            InvalidCoordinateError: for an out-of-range coordinate.
            GeocodingError        : when the server cannot be reached or fails.
        """
        lat_f, lng_f = _validate_coord(lat, lng, "Geocoder.reverse()")
        level = _check_zoom(zoom)
        lat_s, lng_s = _coord_param(lat_f), _coord_param(lng_f)
        if self.provider == "nominatim":
            params = [("lat", lat_s), ("lon", lng_s), ("format", "jsonv2"),
                      ("addressdetails", "1"), ("zoom", str(level))]
            params += self._nominatim_extras()
        else:
            params = [("lat", lat_s), ("lon", lng_s), ("limit", "1")]
            params += self._photon_extras()
        url = f"{self.url}{self._spec.reverse_path}?{urlencode(params)}"
        results = self._cached(url, self._parse_reverse)
        return results[0] if results else None

    def clear_cache(self) -> None:
        """Empty this geocoder's result cache, including the answers from its
        server stored on disk (see ``cache_dir``)."""
        with self._cache_lock:
            self._cache.clear()
        if self._store is not None:
            self._store.clear_results(self._rate_key)

    # ──────────────────────────────────────────────────────────────────────
    # Request parameters
    # ──────────────────────────────────────────────────────────────────────

    def _nominatim_extras(self) -> list[tuple[str, str]]:
        extras: list[tuple[str, str]] = []
        if self.language:
            extras.append(("accept-language", self.language))
        if self.email:
            extras.append(("email", self.email))
        return extras

    def _photon_extras(self) -> list[tuple[str, str]]:
        if not self.language:
            return []
        # Photon takes a single language: the primary subtag of the most preferred
        # entry (highest q-value, first on ties), ignoring "*" and q=0.
        best_q, best_tag = 0.0, ""
        for entry in self.language.split(","):
            tag, _, param = entry.partition(";")
            q = float(param.split("=", 1)[1]) if param else 1.0
            if tag.strip() != "*" and q > best_q:
                best_q, best_tag = q, tag.strip()
        primary = re.split(r"[-_]", best_tag)[0].lower()
        return [("lang", primary)] if primary else []

    # ──────────────────────────────────────────────────────────────────────
    # Parsing
    # ──────────────────────────────────────────────────────────────────────

    def _parse_search(
        self, payload: Any, codes: list[str], limit: int
    ) -> list[dict[str, Any]]:
        """Parse a search answer into at most ``limit`` valid results (parsing stops
        there, so a server that ignores ``limit`` cannot bloat the cache)."""
        attribution = self._spec.attribution
        parse: Callable[[Any, str], dict[str, Any] | None]
        if self.provider == "nominatim":
            if isinstance(payload, dict) and "error" in payload:
                raise GeocodingError(
                    f"{self._spec.label} returned an error: "
                    f"{_error_text(payload) or 'no details given'}",
                    "Check the query and arguments, then try again.",
                )
            if not isinstance(payload, list):
                raise self._unexpected(payload)
            items: list[Any] = payload
            parse = _parse_nominatim_place
        else:
            items = self._photon_features(payload)
            parse = _parse_photon_feature
        results: list[dict[str, Any]] = []
        for item in items:
            place = parse(item, attribution)
            if place is None:
                continue
            # Older Photon servers ignore `countrycode`; filter client-side too.
            if codes and (place["address"] or {}).get("country_code", "") not in codes:
                continue
            results.append(place)
            if len(results) >= limit:
                break
        return results

    def _parse_reverse(self, payload: Any) -> list[dict[str, Any]]:
        attribution = self._spec.attribution
        if self.provider == "nominatim":
            if not isinstance(payload, dict):
                raise self._unexpected(payload)
            if "error" in payload:
                if isinstance(payload["error"], str):
                    return []        # {"error": "Unable to geocode"} — nothing there
                raise GeocodingError(  # a structured error is a failure, not "no address"
                    f"{self._spec.label} returned an error: "
                    f"{_error_text(payload) or 'no details given'}",
                    "Check the coordinate and arguments, then try again.",
                )
            place = _parse_nominatim_place(payload, attribution)
            return [place] if place is not None else []
        features = self._photon_features(payload)
        for feature in features:
            place = _parse_photon_feature(feature, attribution)
            if place is not None:
                return [place]
        return []

    def _photon_features(self, payload: Any) -> list[Any]:
        features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list):
            raise self._unexpected(payload)
        return features

    def _unexpected(self, payload: Any) -> GeocodingError:
        kind = type(payload).__name__
        return GeocodingError(
            f"{self.url} answered, but not with a {self._spec.label} response "
            f"(got a JSON {kind}).",
            f"Check that url= points to a {self._spec.label} server, e.g.\n"
            f"Geocoder(provider='{self.provider}', url='{self._spec.default_url}')",
        )

    # ──────────────────────────────────────────────────────────────────────
    # Cache + HTTP
    # ──────────────────────────────────────────────────────────────────────

    def _cached(
        self, url: str, parse: Callable[[Any], list[dict[str, Any]]], key: str | None = None
    ) -> list[dict[str, Any]]:
        """Answer from the memory cache, then the disk cache (public servers), and
        only then from the server; a fresh answer is stored in both."""
        key = url if key is None else key
        disk_key = f"{__version__} {key}"   # a new release may parse differently
        if self.cache_size:
            with self._cache_lock:
                hit = self._cache.get(key)
                if hit is not None:
                    self._cache.move_to_end(key)
                    return copy.deepcopy(hit)
            stored = self._store.get_results(disk_key) if self._store is not None else None
            if stored is not None:
                self._remember(key, stored)
                return stored
        results = parse(self._get(url))
        if self.cache_size:
            self._remember(key, results)
            if self._store is not None:
                self._store.put_results(disk_key, self._rate_key, results)
        return results

    def _remember(self, key: str, results: list[dict[str, Any]]) -> None:
        with self._cache_lock:
            self._cache[key] = copy.deepcopy(results)
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)

    def _fetch(self, url: str) -> bytes:
        """Perform one HTTP GET and return the body. All network I/O goes through here."""
        request = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body: bytes = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise GeocodingError(
                f"The response from {self.url} is larger than {_MAX_RESPONSE_BYTES:,} bytes.",
                f"Check that url= points to a {self._spec.label} server.",
            )
        return body

    def _get(self, url: str) -> Any:
        """Throttle, fetch (one retry on 5xx / timeout / dropped connection), decode JSON."""
        host = urlsplit(self.url).netloc
        for attempt in (1, 2):
            _throttle(self._rate_key,
                      self.min_interval if attempt == 1 else max(self.min_interval, _RETRY_DELAY),
                      self._store)
            last = attempt == 2
            try:
                body = self._fetch(url)
            except urllib.error.HTTPError as exc:
                detail = None
                with contextlib.suppress(Exception):
                    detail = _server_message(exc.read(64 * 1024))
                with contextlib.suppress(Exception):
                    exc.close()
                if exc.code >= 500 and not last:
                    continue
                raise self._http_error(exc.code, detail, exc.headers) from None
            except TimeoutError:
                if not last:
                    continue
                raise self._timeout_error() from None
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, TimeoutError):
                    if not last:
                        continue
                    raise self._timeout_error() from None
                raise self._network_error(host, exc.reason) from None
            except http.client.InvalidURL as exc:
                raise self._network_error(host, exc) from None
            except (ConnectionError, http.client.HTTPException) as exc:
                if not last:
                    continue
                raise self._network_error(host, exc) from None
            except OSError as exc:
                raise self._network_error(host, exc) from None
            except ValueError as exc:   # e.g. http.client refusing a header value
                raise GeocodingError(
                    f"Could not send the request to {host}: {_clean_text(exc)}",
                    "Check url=, user_agent= and email= for unusual characters.",
                ) from None
            return self._decode(body)
        raise AssertionError("unreachable")   # pragma: no cover

    def _decode(self, body: bytes) -> Any:
        try:
            return json.loads(body.decode("utf-8-sig"))
        except RecursionError:
            raise GeocodingError(
                f"{self.url} returned JSON nested too deeply to be a "
                f"{self._spec.label} response.",
                f"Check that url= points to a {self._spec.label} server.",
            ) from None
        except (UnicodeDecodeError, ValueError):
            snippet = body[:60].decode("utf-8", "replace").strip()
            raise GeocodingError(
                f"{self.url} did not return JSON (starts with {snippet!r}).",
                f"Check that url= points to a {self._spec.label} server, e.g.\n"
                f"Geocoder(provider='{self.provider}', url='{self._spec.default_url}')",
            ) from None

    # ──────────────────────────────────────────────────────────────────────
    # Error mapping
    # ──────────────────────────────────────────────────────────────────────

    def _http_error(self, status: int, detail: str | None, headers: Any) -> GeocodingError:
        host = urlsplit(self.url).netloc
        said = f"\nServer said: {detail}" if detail else ""
        other = "photon" if self.provider == "nominatim" else "nominatim"
        if status in (401, 403):
            return GeocodingError(
                f"{host} refused the request (HTTP {status} — access denied).{said}",
                "Identify your app with a real name and contact:\n"
                "Geocoder(user_agent='my-app/1.0 (you@example.com)', email='you@example.com')\n"
                "If you sent many requests you may be blocked — for bulk work run your\n"
                "own server (Nominatim / Photon are open source): Geocoder(url='http://localhost:8080')",
                status,
            )
        if status == 429:
            retry_after = None
            with contextlib.suppress(Exception):
                retry_after = headers.get("Retry-After") if headers is not None else None
            when = _clean_text(retry_after, 40) if retry_after else ""
            wait = (f" Retry after {when} s." if when.isdigit()
                    else f" Retry after {when}." if when else "")
            return GeocodingError(
                f"{host} is rate-limiting you (HTTP 429 — too many requests).{wait}{said}",
                "Slow down: reuse one Geocoder (its cache avoids repeat requests), keep at\n"
                "most 1 request per second (e.g. Geocoder(min_interval=2.0)), and never\n"
                "geocode in bulk against a public server — run your own instead.",
                status,
            )
        if status == 404:
            return GeocodingError(
                f"{self.url} has no {self._spec.label} API (HTTP 404 — not found).{said}",
                f"Check that url= is the base address of a {self._spec.label} server, e.g.\n"
                f"Geocoder(provider='{self.provider}', url='{self._spec.default_url}')",
                status,
            )
        if status == 400:
            return GeocodingError(
                f"{host} rejected the request (HTTP 400 — bad request).{said}",
                "Check the arguments: language='en' (Photon supports only a few languages),\n"
                "country codes like country_codes=['de', 'fr'], and the query text.",
                status,
            )
        if status >= 500:
            return GeocodingError(
                f"{host} had a server error (HTTP {status}), also after one retry.{said}",
                "The service may be overloaded or down — try again later, or switch provider:\n"
                f"Geocoder(provider='{other}')",
                status,
            )
        return GeocodingError(
            f"{host} answered with HTTP {status}.{said}",
            f"Check that url= points to a {self._spec.label} server and try again later.",
            status,
        )

    def _timeout_error(self) -> GeocodingError:
        return GeocodingError(
            f"{urlsplit(self.url).netloc} did not answer within {self.timeout:g} s "
            f"(also after one retry).",
            "Check your connection, or allow more time: Geocoder(timeout=30)",
        )

    def _network_error(self, host: str, reason: Any) -> GeocodingError:
        text = _clean_text(reason) or type(reason).__name__
        if "CERTIFICATE" in text.upper() or "SSL" in text.upper():
            hint = ("A TLS/SSL certificate problem — check your system clock and CA\n"
                    "certificates (or your proxy's), then try again.")
        else:
            hint = ("Check your internet connection, proxy settings (HTTPS_PROXY) and the\n"
                    f"url ({self.url}), then try again.")
        return GeocodingError(f"Could not reach {host}: {text}", hint)


__all__ = ["Geocoder", "PROVIDERS", "DEFAULT_USER_AGENT", "OSM_ATTRIBUTION"]
