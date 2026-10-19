"""
qoregeo
=======
Zero-dependency geospatial toolkit for Python and for AI assistants.

Pure standard library: installs in seconds anywhere Python 3.10+ runs.

Quick start
-----------
    from qoregeo import GeoEngine

    geo = GeoEngine()
    geo.load("cities.csv")

    # Distance between two points
    km = geo.distance((28.6139, 77.2090), (19.0760, 72.8777))
    print(km)  # 1148.0965

    # Compass bearing
    direction = geo.bearing((28.6139, 77.2090), (19.0760, 72.8777))
    print(direction)  # South-Southwest

    # Buffer + geofencing
    zone = geo.buffer((28.6139, 77.2090), radius=10)
    inside = geo.point_in_polygon((28.65, 77.22), zone)

    # Filter by radius, then an OpenStreetMap map and a static image
    nearby = geo.filter_by_radius(28.61, 77.20, radius=400)
    nearby.map("nearby.html")       # view with: qoregeo serve nearby.html
    nearby.image("nearby.png")

AI assistants
-------------
    qoregeo mcp                     # MCP server (Claude, Codex, Cursor, VS Code, Gemini …)
    qoregeo skill install           # Agent Skill for Claude Code / Codex / Gemini / Copilot
    from qoregeo.tools import tool_definitions, call_tool   # Claude / OpenAI function calling

Links
-----
- GitHub   : https://github.com/bosekarmegam/qoregeo
- PyPI     : https://pypi.org/project/qoregeo
- Issues   : https://github.com/bosekarmegam/qoregeo/issues
"""

from typing import TYPE_CHECKING, Any

from . import exceptions as _exceptions
from ._version import __version__
from .engine import GeoEngine, PointLike
from .exceptions import (
    ColumnNotFoundError,
    EmptyDatasetError,
    FileReadError,
    GeocodingError,
    GeoFileNotFoundError,
    InvalidArgumentError,
    InvalidBufferError,
    InvalidCoordinateError,
    InvalidGeoJSONError,
    InvalidRadiusError,
    InvalidUnitError,
    NoDataError,
    QOREgeoError,
    UnsupportedFormatError,
)
from .map_builder import tile_presets

if TYPE_CHECKING:
    from .geocode import Geocoder
    from .render import render_png, render_svg
    from .serve import serve_file, serve_html

# Loaded on first use so `import qoregeo` doesn't pull in urllib / http.server.
_LAZY = {
    "Geocoder": "geocode",
    "render_png": "render",
    "render_svg": "render",
    "serve_file": "serve",
    "serve_html": "serve",
}

__author__   = "Suneel Bose"
__email__    = "suneelbosekarmegam@gmail.com"
__license__  = "MIT"


def __getattr__(name: str) -> Any:
    # ``qoregeo.FileNotFoundError`` (the 1.0 name of GeoFileNotFoundError) keeps
    # working with a DeprecationWarning; it is deliberately not in ``__all__`` so
    # ``from qoregeo import *`` never shadows the built-in.
    if name == "FileNotFoundError":
        return _exceptions._deprecated_file_not_found()
    if name in _LAZY:
        import importlib

        value = getattr(importlib.import_module(f".{_LAZY[name]}", __name__), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Main class
    "GeoEngine",
    "PointLike",
    # Geocoding, images, maps
    "Geocoder",
    "render_png",
    "render_svg",
    "serve_file",
    "serve_html",
    "tile_presets",
    # Exceptions
    "QOREgeoError",
    "NoDataError",
    "InvalidCoordinateError",
    "InvalidUnitError",
    "InvalidArgumentError",
    "FileReadError",
    "ColumnNotFoundError",
    "GeoFileNotFoundError",
    "UnsupportedFormatError",
    "InvalidGeoJSONError",
    "EmptyDatasetError",
    "InvalidRadiusError",
    "InvalidBufferError",
    "GeocodingError",
    "__version__",
]
