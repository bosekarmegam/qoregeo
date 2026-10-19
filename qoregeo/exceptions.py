"""
qoregeo.exceptions
==================
All custom exceptions raised by QOREgeo.

Every exception includes:
- What went wrong (clear English)
- Exactly how to fix it (copy-paste ready)

Every exception also inherits from the closest built-in exception, so
``except ValueError`` / ``except FileNotFoundError`` keep working, and
exposes the offending values as attributes for programmatic handling.
"""

from __future__ import annotations

import builtins
from typing import Any

_LINE = "─" * 44


class QOREgeoError(Exception):
    """Base class for all QOREgeo exceptions."""


class NoDataError(QOREgeoError, RuntimeError):
    """
    Raised when a method needs features but none are available.

    ``empty`` is False when nothing was ever loaded, and True when data was
    loaded but the dataset is now empty (e.g. a filter matched nothing).
    """

    def __init__(self, method_name: str = "this method", empty: bool = False) -> None:
        self.method_name = method_name
        self.empty = empty
        if empty:
            msg = (
                f"\n\n❌  QOREgeo — Dataset Is Empty\n{_LINE}\n"
                f"You called geo.{method_name}() but the dataset has no features to use.\n"
                f"This usually means a previous filter matched nothing\n"
                f"(or every feature has a null geometry).\n\n"
                f"Fix it — check your filter before calling geo.{method_name}():\n\n"
                f"    result = geo.filter('state', 'Maharashtra')\n"
                f"    if result.count():\n"
                f"        result.{method_name}(...)"
            )
        else:
            msg = (
                f"\n\n❌  QOREgeo — No Data Loaded\n{_LINE}\n"
                f"You called geo.{method_name}() but haven't loaded any data yet.\n\n"
                f"Fix it — load your data first:\n\n"
                f"    geo.load('your_file.csv')\n"
                f"    geo.load('your_file.geojson')\n"
                f"    geo.load_data([...])\n\n"
                f"Then call geo.{method_name}() again."
            )
        super().__init__(msg)


class InvalidCoordinateError(QOREgeoError, ValueError):
    """Raised when lat/lng values are missing, non-numeric or out of range."""

    def __init__(self, lat: Any, lng: Any, context: str | None = None) -> None:
        self.lat = lat
        self.lng = lng
        self.context = context
        where = f"Where   : {context}\n" if context else ""
        msg = (
            f"\n\n❌  QOREgeo — Invalid Coordinate\n{_LINE}\n"
            f"Received: lat={lat!r}, lng={lng!r}\n"
            f"{where}\n"
            f"Valid ranges are:\n"
            f"    latitude  : -90.0  to  90.0\n"
            f"    longitude : -180.0 to 180.0\n\n"
            f"Fix it:\n"
            f"    Make sure you're passing (latitude, longitude) — not (longitude, latitude).\n"
            f"    Example: (28.6139, 77.2090)  ← Delhi"
        )
        super().__init__(msg)


class InvalidUnitError(QOREgeoError, ValueError):
    """Raised when an unrecognised distance or area unit is used."""

    def __init__(self, unit: Any, valid: list[str]) -> None:
        self.unit = unit
        self.valid = list(valid)
        valid_str = ", ".join(f"'{u}'" for u in valid)
        msg = (
            f"\n\n❌  QOREgeo — Invalid Unit '{unit}'\n{_LINE}\n"
            f"'{unit}' is not a recognised unit here.\n\n"
            f"Supported units: {valid_str}\n\n"
            f"Fix it:\n"
            f"    geo.distance(a, b, unit='{valid[0] if valid else 'km'}')"
        )
        super().__init__(msg)


class ColumnNotFoundError(QOREgeoError, LookupError):
    """Raised when a requested column doesn't exist in the dataset."""

    def __init__(
        self,
        column: str,
        available: list[str],
        filepath: str = "dataset",
        hint: str | None = None,
    ) -> None:
        self.column = column
        self.available = list(available)
        self.filepath = filepath

        available_str = ", ".join(f"'{c}'" for c in available[:10])
        if len(available) > 10:
            available_str += f", … (+{len(available) - 10} more)"
        if not available_str:
            available_str = "(none)"

        if hint is None:
            first = available[0] if available else "column_name"
            hint = f"geo.filter('{first}', value)"

        msg = (
            f"\n\n❌  QOREgeo — Column Not Found\n{_LINE}\n"
            f"File: '{filepath}'\n"
            f"Could not find a '{column}' column.\n\n"
            f"Columns in your file:\n"
            f"    {available_str}\n\n"
            f"Fix it:\n"
            f"    {hint}"
        )
        super().__init__(msg)


class GeoFileNotFoundError(QOREgeoError, builtins.FileNotFoundError):
    """
    Raised when the data file doesn't exist.

    Subclasses the built-in ``FileNotFoundError``, so ``except FileNotFoundError``
    catches it too.
    """

    def __init__(self, path: Any) -> None:
        self.path = str(path)
        msg = (
            f"\n\n❌  QOREgeo — File Not Found\n{_LINE}\n"
            f"Could not find the file: '{path}'\n\n"
            f"Fix it:\n"
            f"    • Check the spelling of the filename\n"
            f"    • Check that the file is in the right folder\n"
            f"    • Use an absolute path if needed:\n"
            f"      geo.load('/full/path/to/your_file.csv')"
        )
        super().__init__(msg)
        self.filename = self.path

    def __str__(self) -> str:   # OSError would otherwise format "[Errno …]" style
        return str(self.args[0]) if self.args else ""


# Backward-compatible name. Not exported via ``from qoregeo import *`` so it
# never shadows the built-in in user code.
FileNotFoundError = GeoFileNotFoundError


class UnsupportedFormatError(QOREgeoError, ValueError):
    """Raised for unsupported file formats."""

    def __init__(self, ext: str) -> None:
        self.ext = ext
        msg = (
            f"\n\n❌  QOREgeo — Unsupported File Format '{ext}'\n{_LINE}\n"
            f"QOREgeo currently supports:\n"
            f"    • .csv / .tsv / .txt (delimited text)\n"
            f"    • .geojson\n"
            f"    • .json (GeoJSON format)\n\n"
            f"Fix it — convert your file first, or use geo.load_data([...]) "
            f"with raw Feature dicts."
        )
        super().__init__(msg)


class InvalidGeoJSONError(UnsupportedFormatError):
    """Raised when a .geojson/.json file or dict is not valid GeoJSON."""

    def __init__(self, source: str, reason: str) -> None:
        self.source = source
        self.reason = reason
        msg = (
            f"\n\n❌  QOREgeo — Invalid GeoJSON\n{_LINE}\n"
            f"Source: '{source}'\n"
            f"Problem: {reason}\n\n"
            f"Fix it — QOREgeo accepts a FeatureCollection, a Feature, a bare\n"
            f"Geometry, or a list of Features, e.g.:\n\n"
            f'    {{"type": "FeatureCollection", "features": [...]}}'
        )
        # Skip UnsupportedFormatError.__init__ — it builds a different message.
        QOREgeoError.__init__(self, msg)
        self.ext = source


class EmptyDatasetError(QOREgeoError, ValueError):
    """Raised when a file or dataset contains no usable features."""

    def __init__(self, path: str) -> None:
        self.path = str(path)
        msg = (
            f"\n\n❌  QOREgeo — Empty Dataset\n{_LINE}\n"
            f"No valid spatial features were found in: '{path}'\n\n"
            f"Fix it:\n"
            f"    • Make sure the file has data rows (not just a header)\n"
            f"    • Make sure latitude and longitude values are not all blank\n"
            f"    • If using a CSV, check that lat/lng columns contain numbers"
        )
        super().__init__(msg)


class InvalidRadiusError(QOREgeoError, ValueError):
    """Raised when a zero, negative or non-numeric radius is given."""

    def __init__(self, radius: Any) -> None:
        self.radius = radius
        msg = (
            f"\n\n❌  QOREgeo — Invalid Radius ({radius!r})\n{_LINE}\n"
            f"Radius must be a finite number greater than zero.\n\n"
            f"Fix it:\n"
            f"    geo.buffer(point, radius=10)          ← 10 km\n"
            f"    geo.filter_by_radius(lat, lng, 50)    ← 50 km"
        )
        super().__init__(msg)


class InvalidBufferError(QOREgeoError, ValueError):
    """Raised when a polygon argument is missing, malformed or not a polygon."""

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason
        detail = f"Problem: {reason}\n\n" if reason else ""
        msg = (
            f"\n\n❌  QOREgeo — Invalid Polygon\n{_LINE}\n"
            f"The polygon you passed is not valid.\n"
            f"{detail}"
            f"Accepted: a geo.buffer() result, a GeoJSON Polygon or MultiPolygon,\n"
            f"a Feature whose geometry is one of those, or a FeatureCollection.\n\n"
            f"Fix it — e.g. use a polygon created with geo.buffer():\n\n"
            f"    zone = geo.buffer((28.61, 77.20), radius=10)\n"
            f"    geo.point_in_polygon((28.65, 77.22), zone)"
        )
        super().__init__(msg)


class InvalidArgumentError(QOREgeoError, ValueError):
    """Raised when an argument has the wrong type or an out-of-range value."""

    def __init__(self, name: str, value: Any, expected: str, example: str | None = None) -> None:
        self.name = name
        self.value = value
        self.expected = expected
        fix = f"\n\nFix it:\n    {example}" if example else ""
        msg = (
            f"\n\n❌  QOREgeo — Invalid Argument '{name}'\n{_LINE}\n"
            f"Received: {value!r}\n"
            f"Expected: {expected}"
            f"{fix}"
        )
        super().__init__(msg)


__all__ = [
    "QOREgeoError",
    "InvalidArgumentError",
    "NoDataError",
    "InvalidCoordinateError",
    "InvalidUnitError",
    "ColumnNotFoundError",
    "GeoFileNotFoundError",
    "UnsupportedFormatError",
    "InvalidGeoJSONError",
    "EmptyDatasetError",
    "InvalidRadiusError",
    "InvalidBufferError",
]
