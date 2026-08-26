"""
qoregeo.formats
===============
Readers and writers for the spatial formats people actually receive.

CSV and GeoJSON cover most modern work, but real datasets arrive as GPS
tracks, Google Earth exports, database WKT columns and — still, constantly —
Esri shapefiles. Each of those normally means another dependency; here they
are parsed with ``struct``, ``re`` and the standard library XML parser.

The shapefile reader is the notable one: ``.shp`` is a documented binary
format, so reading it needs no GDAL, no C compiler, and no 40-minute
Windows install.
"""

from __future__ import annotations

import json
import os
import re
import struct
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .exceptions import (
    EmptyDatasetError,
    InvalidGeometryError,
    UnsupportedFormatError,
)
from .exceptions import FileNotFoundError as QFileNotFoundError

Feature = Dict[str, Any]
Geometry = Dict[str, Any]

#: Extensions that :func:`read_any` and ``GeoEngine.load()`` understand.
READABLE = (
    ".csv", ".geojson", ".json", ".ndjson", ".geojsonl", ".jsonl",
    ".wkt", ".gpx", ".kml", ".shp",
)

#: Extensions that :func:`write_any` and ``GeoEngine.save()`` understand.
WRITABLE = (
    ".csv", ".geojson", ".json", ".ndjson", ".geojsonl", ".jsonl",
    ".wkt", ".gpx", ".kml", ".svg", ".png", ".html",
)


# ─────────────────────────────────────────────────────────────────────────────
# WKT / EWKT
# ─────────────────────────────────────────────────────────────────────────────

_WKT_HEAD = re.compile(
    r"^\s*(?:SRID=(\d+)\s*;\s*)?([A-Za-z]+)\s*(Z|M|ZM)?\s*(\(.*\)|EMPTY)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def parse_wkt(wkt: str) -> Geometry:
    """
    Parse a WKT (or PostGIS EWKT) string into a GeoJSON geometry.

    Handles POINT, LINESTRING, POLYGON, their MULTI variants and
    GEOMETRYCOLLECTION. Z/M ordinates are accepted and dropped, since GeoJSON
    positions here are 2-D.

    >>> parse_wkt("POINT (77.209 28.6139)")
    {'type': 'Point', 'coordinates': [77.209, 28.6139]}
    """
    match = _WKT_HEAD.match(str(wkt))
    if not match:
        raise InvalidGeometryError("WKT", f"Could not parse WKT: {str(wkt)[:80]!r}")

    _srid, kind, _dims, body = match.groups()
    kind = kind.upper()

    if body.upper() == "EMPTY":
        empty: Dict[str, Any] = {
            "POINT": {"type": "Point", "coordinates": []},
            "LINESTRING": {"type": "LineString", "coordinates": []},
            "POLYGON": {"type": "Polygon", "coordinates": []},
        }
        if kind in empty:
            return empty[kind]
        raise InvalidGeometryError("WKT", f"Empty {kind} is not supported")

    inner = body.strip()[1:-1]

    if kind == "POINT":
        return {"type": "Point", "coordinates": _wkt_point(inner)}
    if kind == "LINESTRING":
        return {"type": "LineString", "coordinates": _wkt_points(inner)}
    if kind == "MULTIPOINT":
        return {"type": "MultiPoint", "coordinates": _wkt_multipoint(inner)}
    if kind == "POLYGON":
        return {"type": "Polygon", "coordinates": [_wkt_points(g) for g in _split_groups(inner)]}
    if kind == "MULTILINESTRING":
        return {
            "type": "MultiLineString",
            "coordinates": [_wkt_points(g) for g in _split_groups(inner)],
        }
    if kind == "MULTIPOLYGON":
        return {
            "type": "MultiPolygon",
            "coordinates": [
                [_wkt_points(r) for r in _split_groups(poly)]
                for poly in _split_groups(inner)
            ],
        }
    if kind == "GEOMETRYCOLLECTION":
        return {
            "type": "GeometryCollection",
            "geometries": [parse_wkt(part) for part in _split_collection(inner)],
        }

    raise InvalidGeometryError("WKT", f"Unsupported WKT type: {kind}")


def _wkt_point(text: str) -> List[float]:
    nums = [float(n) for n in text.replace(",", " ").split()]
    if len(nums) < 2:
        raise InvalidGeometryError("WKT", "A POINT needs at least two ordinates")
    return nums[:2]


def _wkt_points(text: str) -> List[List[float]]:
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    out = []
    for chunk in text.split(","):
        nums = [float(n) for n in chunk.split()]
        if len(nums) >= 2:
            out.append(nums[:2])
    return out


def _wkt_multipoint(text: str) -> List[List[float]]:
    # MULTIPOINT allows both "(1 2, 3 4)" and "((1 2), (3 4))".
    if "(" in text:
        return [_wkt_point(g) for g in _split_groups(text)]
    return _wkt_points(text)


def _split_groups(text: str) -> List[str]:
    """Split on commas that sit at parenthesis depth zero, keeping groups intact."""
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    for char in text:
        if char == "(":
            depth += 1
            if depth == 1:
                current = []
                continue
        elif char == ")":
            depth -= 1
            if depth == 0:
                parts.append("".join(current))
                continue
        if depth >= 1:
            current.append(char)
    return parts if parts else [text]


def _split_collection(text: str) -> List[str]:
    """Split a GEOMETRYCOLLECTION body into its member WKT strings."""
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    for char in text:
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        current.append(char)
    if current:
        parts.append("".join(current).strip())
    return [p for p in parts if p]


def to_wkt(geometry: Geometry) -> str:
    """Serialise a GeoJSON geometry back to WKT."""
    if geometry.get("type") == "Feature":
        geometry = geometry.get("geometry") or {}

    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []

    def pair(c: Sequence[float]) -> str:
        return f"{_fmt(c[0])} {_fmt(c[1])}"

    def ring(r: Sequence[Sequence[float]]) -> str:
        return "(" + ", ".join(pair(c) for c in r) + ")"

    if gtype == "Point":
        return "POINT EMPTY" if not coords else f"POINT ({pair(coords)})"
    if gtype == "MultiPoint":
        return "MULTIPOINT (" + ", ".join(f"({pair(c)})" for c in coords) + ")"
    if gtype == "LineString":
        return "LINESTRING EMPTY" if not coords else f"LINESTRING {ring(coords)}"
    if gtype == "MultiLineString":
        return "MULTILINESTRING (" + ", ".join(ring(line) for line in coords) + ")"
    if gtype == "Polygon":
        return "POLYGON EMPTY" if not coords else (
            "POLYGON (" + ", ".join(ring(r) for r in coords) + ")"
        )
    if gtype == "MultiPolygon":
        return "MULTIPOLYGON (" + ", ".join(
            "(" + ", ".join(ring(r) for r in poly) + ")" for poly in coords
        ) + ")"
    if gtype == "GeometryCollection":
        return "GEOMETRYCOLLECTION (" + ", ".join(
            to_wkt(g) for g in geometry.get("geometries", [])
        ) + ")"

    raise InvalidGeometryError(str(gtype), "Cannot serialise this geometry to WKT")


def _fmt(value: float) -> str:
    """Trim trailing zeros so WKT output stays readable."""
    text = f"{float(value):.10f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-") else "0"


def read_wkt_file(path: str, encoding: str = "utf-8") -> List[Feature]:
    """
    Read a file of WKT geometries, one per line.

    A line may be ``<wkt>`` alone, or ``<wkt>\\t<name>`` / ``<wkt>;<name>``
    to carry a label alongside it.
    """
    features: List[Feature] = []
    with open(path, encoding=encoding) as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # An optional label may follow the geometry, separated by a tab
            # or semicolon. Split only after the geometry's closing bracket so
            # separators inside the WKT itself are never mistaken for one.
            label = None
            close = line.rfind(")")
            tail = line[close + 1 :] if close != -1 else ""
            if tail[:1] in ("\t", ";"):
                label = tail[1:].strip()
                line = line[: close + 1].strip()

            geometry = parse_wkt(line)
            props: Dict[str, Any] = {"_line": line_no}
            if label:
                props["name"] = label
            features.append({"type": "Feature", "geometry": geometry, "properties": props})
    return features


def write_wkt_file(features: Sequence[Feature], path: str, encoding: str = "utf-8") -> None:
    """Write features as one WKT geometry per line."""
    with open(path, "w", encoding=encoding) as handle:
        for feat in features:
            handle.write(to_wkt(feat.get("geometry") or {}) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# NDJSON / GeoJSON Lines
# ─────────────────────────────────────────────────────────────────────────────

def read_ndjson(path: str, encoding: str = "utf-8") -> List[Feature]:
    """
    Read newline-delimited GeoJSON — one Feature per line.

    This is the format big spatial exports use, because it streams: you never
    have to hold the whole collection in memory to append to it.
    """
    features: List[Feature] = []
    with open(path, encoding=encoding) as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise UnsupportedFormatError(
                    f".ndjson — line {line_no} is not valid JSON: {exc}"
                ) from exc

            if record.get("type") == "Feature":
                features.append(record)
            elif record.get("type") == "FeatureCollection":
                features.extend(record.get("features", []))
            elif "type" in record and "coordinates" in record:
                features.append(
                    {"type": "Feature", "geometry": record, "properties": {}}
                )
    return features


def write_ndjson(features: Sequence[Feature], path: str, encoding: str = "utf-8") -> None:
    """Write one GeoJSON Feature per line."""
    with open(path, "w", encoding=encoding) as handle:
        for feat in features:
            handle.write(json.dumps(feat, ensure_ascii=False) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# GPX
# ─────────────────────────────────────────────────────────────────────────────

_GPX_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}


def read_gpx(path: str) -> List[Feature]:
    """
    Read a GPX file — waypoints, routes and tracks.

    Waypoints become Points; routes and track segments become LineStrings,
    with elevation and timestamps preserved as properties where present.
    """
    root = _parse_xml(path)
    features: List[Feature] = []

    for wpt in _findall(root, "wpt"):
        point = _gpx_point(wpt)
        if point is None:
            continue
        props = {"_kind": "waypoint"}
        props.update(_gpx_meta(wpt))
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": point},
                "properties": props,
            }
        )

    for route in _findall(root, "rte"):
        coords = [p for p in (_gpx_point(pt) for pt in _findall(route, "rtept")) if p]
        if len(coords) >= 2:
            features.append(_gpx_line(coords, "route", _gpx_meta(route)))

    # Track metadata (name, description) lives on <trk>, while the points live
    # in one or more child <trkseg>s — so read the name once and reuse it.
    for track in _findall(root, "trk"):
        meta = _gpx_meta(track)
        for segment in _findall(track, "trkseg"):
            coords = [p for p in (_gpx_point(pt) for pt in _findall(segment, "trkpt")) if p]
            if len(coords) >= 2:
                features.append(_gpx_line(coords, "track", meta))

    if not features:
        raise EmptyDatasetError(path)
    return features


def _gpx_line(
    coords: List[List[float]],
    kind: str,
    meta: Dict[str, Any],
) -> Feature:
    """Wrap a run of GPX points into a LineString feature."""
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": {"_kind": kind, "_points": len(coords), **meta},
    }


def _gpx_point(element: ET.Element) -> Optional[List[float]]:
    lat = element.get("lat")
    lon = element.get("lon")
    if lat is None or lon is None:
        return None
    try:
        return [float(lon), float(lat)]
    except ValueError:
        return None


def _gpx_meta(element: ET.Element) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    for tag in ("name", "desc", "time", "ele", "sym", "type", "cmt"):
        found = _find(element, tag)
        if found is not None and found.text:
            value: Any = found.text.strip()
            if tag == "ele":
                try:
                    value = float(value)
                except ValueError:
                    pass
            meta[tag] = value
    return meta


def write_gpx(features: Sequence[Feature], path: str, name: str = "QOREgeo export") -> None:
    """Write features as GPX: Points become waypoints, lines become tracks."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="QOREgeo" '
        'xmlns="http://www.topografix.com/GPX/1/1">',
        f"  <metadata><name>{_xml_escape(name)}</name></metadata>",
    ]

    for feat in features:
        geom = feat.get("geometry") or {}
        props = feat.get("properties") or {}
        label = _xml_escape(str(props.get("name", props.get("Name", ""))))

        if geom.get("type") == "Point" and geom.get("coordinates"):
            lng, lat = geom["coordinates"][:2]
            lines.append(f'  <wpt lat="{lat}" lon="{lng}">')
            if label:
                lines.append(f"    <name>{label}</name>")
            lines.append("  </wpt>")

        elif geom.get("type") == "LineString":
            lines.append("  <trk>")
            if label:
                lines.append(f"    <name>{label}</name>")
            lines.append("    <trkseg>")
            for coord in geom.get("coordinates", []):
                lines.append(f'      <trkpt lat="{coord[1]}" lon="{coord[0]}"></trkpt>')
            lines.append("    </trkseg>")
            lines.append("  </trk>")

    lines.append("</gpx>")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# KML
# ─────────────────────────────────────────────────────────────────────────────

def read_kml(path: str) -> List[Feature]:
    """
    Read a KML file (Google Earth / My Maps export).

    Placemarks become Features; ``<ExtendedData>`` fields and the name and
    description become properties.
    """
    root = _parse_xml(path)
    features: List[Feature] = []

    for placemark in _findall(root, "Placemark", recursive=True):
        geometry = _kml_geometry(placemark)
        if geometry is None:
            continue

        props: Dict[str, Any] = {}
        for tag in ("name", "description"):
            found = _find(placemark, tag)
            if found is not None and found.text:
                props[tag] = found.text.strip()

        for data in _findall(placemark, "Data", recursive=True):
            key = data.get("name")
            value = _find(data, "value")
            if key and value is not None and value.text:
                props[key] = value.text.strip()

        for simple in _findall(placemark, "SimpleData", recursive=True):
            key = simple.get("name")
            if key and simple.text:
                props[key] = simple.text.strip()

        features.append({"type": "Feature", "geometry": geometry, "properties": props})

    if not features:
        raise EmptyDatasetError(path)
    return features


def _kml_geometry(placemark: ET.Element) -> Optional[Geometry]:
    point = _find(placemark, "Point", recursive=True)
    if point is not None:
        coords = _kml_coords(point)
        if coords:
            return {"type": "Point", "coordinates": coords[0]}

    line = _find(placemark, "LineString", recursive=True)
    if line is not None:
        coords = _kml_coords(line)
        if len(coords) >= 2:
            return {"type": "LineString", "coordinates": coords}

    polygon = _find(placemark, "Polygon", recursive=True)
    if polygon is not None:
        rings: List[List[List[float]]] = []
        outer = _find(polygon, "outerBoundaryIs", recursive=True)
        if outer is not None:
            ring = _kml_coords(outer)
            if len(ring) >= 3:
                rings.append(_close(ring))
        for inner in _findall(polygon, "innerBoundaryIs", recursive=True):
            ring = _kml_coords(inner)
            if len(ring) >= 3:
                rings.append(_close(ring))
        if rings:
            return {"type": "Polygon", "coordinates": rings}

    return None


def _kml_coords(element: ET.Element) -> List[List[float]]:
    node = _find(element, "coordinates", recursive=True)
    if node is None or not node.text:
        return []
    out = []
    for token in node.text.replace("\n", " ").replace("\t", " ").split():
        parts = token.split(",")
        if len(parts) >= 2:
            try:
                out.append([float(parts[0]), float(parts[1])])
            except ValueError:
                continue
    return out


def _close(ring: List[List[float]]) -> List[List[float]]:
    if ring and ring[0] != ring[-1]:
        ring = ring + [list(ring[0])]
    return ring


def write_kml(features: Sequence[Feature], path: str, name: str = "QOREgeo export") -> None:
    """Write features as KML, ready to open in Google Earth."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        "  <Document>",
        f"    <name>{_xml_escape(name)}</name>",
    ]

    for feat in features:
        geom = feat.get("geometry") or {}
        props = feat.get("properties") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if not gtype or coords is None:
            continue

        lines.append("    <Placemark>")
        label = props.get("name") or props.get("Name")
        if label:
            lines.append(f"      <name>{_xml_escape(str(label))}</name>")

        visible = {k: v for k, v in props.items() if not str(k).startswith("_")}
        if visible:
            lines.append("      <ExtendedData>")
            for key, value in visible.items():
                lines.append(
                    f'        <Data name="{_xml_escape(str(key))}">'
                    f"<value>{_xml_escape(str(value))}</value></Data>"
                )
            lines.append("      </ExtendedData>")

        if gtype == "Point":
            lines.append(
                f"      <Point><coordinates>{coords[0]},{coords[1]}"
                f"</coordinates></Point>"
            )
        elif gtype == "LineString":
            joined = " ".join(f"{c[0]},{c[1]}" for c in coords)
            lines.append(
                f"      <LineString><coordinates>{joined}</coordinates></LineString>"
            )
        elif gtype == "Polygon" and coords:
            joined = " ".join(f"{c[0]},{c[1]}" for c in coords[0])
            lines.append("      <Polygon><outerBoundaryIs><LinearRing>")
            lines.append(f"        <coordinates>{joined}</coordinates>")
            lines.append("      </LinearRing></outerBoundaryIs></Polygon>")

        lines.append("    </Placemark>")

    lines.extend(["  </Document>", "</kml>"])
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# XML helpers — KML and GPX both use namespaces inconsistently in the wild
# ─────────────────────────────────────────────────────────────────────────────

def _parse_xml(path: str) -> ET.Element:
    if not os.path.exists(path):
        raise QFileNotFoundError(path)
    try:
        return ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise UnsupportedFormatError(
            f"{os.path.splitext(path)[1]} — malformed XML: {exc}"
        ) from exc


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _findall(root: ET.Element, tag: str, recursive: bool = True) -> List[ET.Element]:
    """Find elements by local name, ignoring whichever namespace the file used."""
    source = root.iter() if recursive else list(root)
    return [el for el in source if _localname(el.tag) == tag]


def _find(root: ET.Element, tag: str, recursive: bool = False) -> Optional[ET.Element]:
    found = _findall(root, tag, recursive=recursive)
    return found[0] if found else None


def _xml_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ─────────────────────────────────────────────────────────────────────────────
# Esri Shapefile (.shp + .dbf)
# ─────────────────────────────────────────────────────────────────────────────

#: Shapefile shape type codes, per the Esri white paper.
SHP_NULL, SHP_POINT, SHP_POLYLINE, SHP_POLYGON = 0, 1, 3, 5
SHP_MULTIPOINT = 8
SHP_POINTZ, SHP_POLYLINEZ, SHP_POLYGONZ, SHP_MULTIPOINTZ = 11, 13, 15, 18
SHP_POINTM, SHP_POLYLINEM, SHP_POLYGONM, SHP_MULTIPOINTM = 21, 23, 25, 28

_POINT_TYPES = (SHP_POINT, SHP_POINTZ, SHP_POINTM)
_LINE_TYPES = (SHP_POLYLINE, SHP_POLYLINEZ, SHP_POLYLINEM)
_POLY_TYPES = (SHP_POLYGON, SHP_POLYGONZ, SHP_POLYGONM)
_MULTI_TYPES = (SHP_MULTIPOINT, SHP_MULTIPOINTZ, SHP_MULTIPOINTM)


def read_shapefile(path: str, encoding: str = "latin-1") -> List[Feature]:
    """
    Read an Esri shapefile without GDAL.

    ``.shp`` supplies geometry and the sibling ``.dbf`` supplies attributes;
    both are parsed here with :mod:`struct`. Point, Polyline, Polygon and
    MultiPoint shapes are supported, including their Z and M variants (the
    extra ordinates are read past and dropped).

    Coordinates are returned exactly as stored. Shapefiles carry their CRS in
    a separate ``.prj`` file, so if yours is projected rather than lat/lng,
    convert it before treating the values as degrees.
    """
    if not os.path.exists(path):
        raise QFileNotFoundError(path)

    with open(path, "rb") as handle:
        data = handle.read()

    if len(data) < 100:
        raise UnsupportedFormatError(".shp — file is too short to be a shapefile")

    magic = struct.unpack(">i", data[0:4])[0]
    if magic != 9994:
        raise UnsupportedFormatError(
            ".shp — bad magic number; this is not an Esri shapefile"
        )

    geometries: List[Optional[Geometry]] = []
    offset = 100
    while offset + 8 <= len(data):
        _record_number, content_len = struct.unpack(">ii", data[offset : offset + 8])
        offset += 8
        end = offset + content_len * 2
        if end > len(data):
            break
        geometries.append(_read_shp_record(data[offset:end]))
        offset = end

    records = _read_dbf(os.path.splitext(path)[0] + ".dbf", encoding=encoding)

    features: List[Feature] = []
    for i, geometry in enumerate(geometries):
        if geometry is None:
            continue
        props = records[i] if i < len(records) else {}
        features.append({"type": "Feature", "geometry": geometry, "properties": props})

    if not features:
        raise EmptyDatasetError(path)
    return features


def _read_shp_record(chunk: bytes) -> Optional[Geometry]:
    """Decode one shapefile record body into a GeoJSON geometry."""
    if len(chunk) < 4:
        return None
    shape_type = struct.unpack("<i", chunk[0:4])[0]

    if shape_type == SHP_NULL:
        return None

    if shape_type in _POINT_TYPES:
        if len(chunk) < 20:
            return None
        x, y = struct.unpack("<2d", chunk[4:20])
        return {"type": "Point", "coordinates": [x, y]}

    if shape_type in _MULTI_TYPES:
        count = struct.unpack("<i", chunk[36:40])[0]
        coords = _read_points(chunk, 40, count)
        return {"type": "MultiPoint", "coordinates": coords}

    if shape_type in _LINE_TYPES + _POLY_TYPES:
        num_parts, num_points = struct.unpack("<2i", chunk[36:44])
        parts = list(struct.unpack(f"<{num_parts}i", chunk[44 : 44 + 4 * num_parts]))
        points = _read_points(chunk, 44 + 4 * num_parts, num_points)

        rings: List[List[List[float]]] = []
        for i, begin in enumerate(parts):
            stop = parts[i + 1] if i + 1 < len(parts) else num_points
            ring = points[begin:stop]
            if ring:
                rings.append(ring)

        if not rings:
            return None

        if shape_type in _LINE_TYPES:
            if len(rings) == 1:
                return {"type": "LineString", "coordinates": rings[0]}
            return {"type": "MultiLineString", "coordinates": rings}

        polygons = _group_polygon_rings(rings)
        if len(polygons) == 1:
            return {"type": "Polygon", "coordinates": polygons[0]}
        return {"type": "MultiPolygon", "coordinates": polygons}

    return None


def _read_points(chunk: bytes, offset: int, count: int) -> List[List[float]]:
    size = 16 * count
    if offset + size > len(chunk):
        count = max(0, (len(chunk) - offset) // 16)
        size = 16 * count
    flat = struct.unpack(f"<{2 * count}d", chunk[offset : offset + size])
    return [[flat[i], flat[i + 1]] for i in range(0, len(flat), 2)]


def _group_polygon_rings(rings: List[List[List[float]]]) -> List[List[List[List[float]]]]:
    """
    Split shapefile rings into polygons.

    Esri encodes holes by winding: clockwise rings are outer boundaries,
    counter-clockwise rings are holes belonging to the preceding outer ring.
    """
    from .geometry import ring_is_clockwise

    polygons: List[List[List[List[float]]]] = []
    for ring in rings:
        if ring_is_clockwise(ring) or not polygons:
            polygons.append([ring])
        else:
            polygons[-1].append(ring)
    return polygons


_DBF_TYPES = {"N", "F", "I", "O", "L", "D", "C", "M"}


def _read_dbf(path: str, encoding: str = "latin-1") -> List[Dict[str, Any]]:
    """Parse the attribute table beside a shapefile. Missing .dbf is not fatal."""
    if not os.path.exists(path):
        return []

    with open(path, "rb") as handle:
        data = handle.read()

    if len(data) < 32:
        return []

    num_records, header_len, record_len = struct.unpack("<i2h", data[4:12])

    fields: List[Tuple[str, str, int, int]] = []
    offset = 32
    while offset + 32 <= header_len and data[offset] not in (0x0D, 0x00):
        raw = data[offset : offset + 32]
        name = raw[0:11].split(b"\x00")[0].decode(encoding, "replace").strip()
        kind = raw[11:12].decode("ascii", "replace")
        length = raw[16]
        decimals = raw[17]
        fields.append((name, kind, length, decimals))
        offset += 32

    records: List[Dict[str, Any]] = []
    cursor = header_len
    for _ in range(num_records):
        if cursor + record_len > len(data):
            break
        row = data[cursor : cursor + record_len]
        cursor += record_len
        if row[:1] == b"*":
            continue                       # tombstoned record

        values: Dict[str, Any] = {}
        pos = 1
        for name, kind, length, decimals in fields:
            raw_value = row[pos : pos + length].decode(encoding, "replace").strip()
            pos += length
            values[name] = _cast_dbf(raw_value, kind, decimals)
        records.append(values)

    return records


def _cast_dbf(value: str, kind: str, decimals: int) -> Any:
    if value == "":
        return None
    if kind in ("N", "F", "I", "O"):
        try:
            return float(value) if decimals else int(float(value))
        except ValueError:
            return value
    if kind == "L":
        return value.upper() in ("Y", "T")
    return value


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch
# ─────────────────────────────────────────────────────────────────────────────

def read_any(path: str, encoding: str = "utf-8") -> List[Feature]:
    """
    Read any supported spatial file, dispatching on extension.

    CSV is handled by ``GeoEngine.load()`` itself, since it needs column
    detection options this function has no way to receive.
    """
    ext = os.path.splitext(path)[1].lower()

    if ext in (".ndjson", ".geojsonl", ".jsonl"):
        return read_ndjson(path, encoding=encoding)
    if ext == ".wkt":
        return read_wkt_file(path, encoding=encoding)
    if ext == ".gpx":
        return read_gpx(path)
    if ext == ".kml":
        return read_kml(path)
    if ext == ".shp":
        return read_shapefile(path)

    raise UnsupportedFormatError(ext)


def write_any(features: Sequence[Feature], path: str, encoding: str = "utf-8") -> None:
    """Write features to any supported vector format, dispatching on extension."""
    ext = os.path.splitext(path)[1].lower()

    if ext in (".ndjson", ".geojsonl", ".jsonl"):
        write_ndjson(features, path, encoding=encoding)
    elif ext == ".wkt":
        write_wkt_file(features, path, encoding=encoding)
    elif ext == ".gpx":
        write_gpx(features, path)
    elif ext == ".kml":
        write_kml(features, path)
    else:
        raise UnsupportedFormatError(ext)


def iter_features(source: Iterable[Any]) -> List[Feature]:
    """
    Coerce loosely-shaped input into GeoJSON Features.

    Accepts Features, bare geometries, ``(lat, lng)`` pairs and dicts with
    lat/lng keys — the four shapes people actually have lying around.
    """
    out: List[Feature] = []
    for item in source:
        if isinstance(item, dict):
            if item.get("type") == "Feature":
                out.append(item)
            elif "type" in item and "coordinates" in item:
                out.append({"type": "Feature", "geometry": item, "properties": {}})
            else:
                lat = item.get("lat", item.get("latitude"))
                lng = item.get("lng", item.get("lon", item.get("longitude")))
                if lat is None or lng is None:
                    raise InvalidGeometryError(
                        "record", f"No coordinates found in {list(item)[:6]}"
                    )
                props = {
                    k: v
                    for k, v in item.items()
                    if k not in ("lat", "latitude", "lng", "lon", "longitude")
                }
                out.append(
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [float(lng), float(lat)],
                        },
                        "properties": props,
                    }
                )
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [float(item[1]), float(item[0])],
                    },
                    "properties": {},
                }
            )
        else:
            raise InvalidGeometryError("record", f"Cannot interpret {item!r} as a feature")
    return out
