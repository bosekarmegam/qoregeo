"""
Format readers and writers: WKT, NDJSON, GPX, KML and Esri shapefiles.

The shapefile tests build real ``.shp``/``.dbf`` byte streams with ``struct``
rather than shipping binary fixtures, so what is under test is the actual
documented layout — and the test data stays readable.
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest

from qoregeo import formats as fm
from qoregeo.exceptions import (
    EmptyDatasetError,
    InvalidGeometryError,
    UnsupportedFormatError,
)
from qoregeo.exceptions import FileNotFoundError as QFileNotFoundError
from qoregeo.geometry import point_in_geometry

from .conftest import DELHI, MUMBAI, point_feature

# ─────────────────────────────────────────────────────────────────────────────
# Shapefile writers, used only by the tests
# ─────────────────────────────────────────────────────────────────────────────

def write_shp(path, shape_type, records, box=(0, 0, 1, 1)):
    """Assemble a minimal but valid .shp file from pre-built record bodies."""
    body = b"".join(
        struct.pack(">ii", i, len(content) // 2) + content
        for i, content in enumerate(records, start=1)
    )
    header = struct.pack(">iiiiii", 9994, 0, 0, 0, 0, 0)
    header += struct.pack(">ii", (100 + len(body)) // 2, 1000)
    header += struct.pack("<i", shape_type)
    header += struct.pack("<4d", *box) + struct.pack("<4d", 0, 0, 0, 0)
    Path(path).write_bytes(header + body)


def point_record(x, y):
    return struct.pack("<i2d", 1, x, y)


def pointz_record(x, y, z):
    return struct.pack("<i4d", 11, x, y, z, 0.0)


def null_record():
    return struct.pack("<i", 0)


def shape_record(shape_type, rings):
    points = [p for ring in rings for p in ring]
    parts, running = [], 0
    for ring in rings:
        parts.append(running)
        running += len(ring)
    box = (
        min(p[0] for p in points), min(p[1] for p in points),
        max(p[0] for p in points), max(p[1] for p in points),
    )
    out = struct.pack("<i", shape_type) + struct.pack("<4d", *box)
    out += struct.pack("<2i", len(rings), len(points))
    out += struct.pack(f"<{len(parts)}i", *parts)
    for point in points:
        out += struct.pack("<2d", point[0], point[1])
    return out


def write_dbf(path, fields, rows):
    """Assemble a dBase III attribute table."""
    header_len = 32 + 32 * len(fields) + 1
    record_len = 1 + sum(f[2] for f in fields)
    header = struct.pack("<4B", 0x03, 125, 1, 1)
    header += struct.pack("<i2h", len(rows), header_len, record_len) + b"\x00" * 20
    for name, kind, length, decimals in fields:
        header += name.encode("latin-1")[:11].ljust(11, b"\x00") + kind.encode("ascii")
        header += b"\x00" * 4 + bytes([length, decimals]) + b"\x00" * 14
    header += b"\x0D"

    body = b""
    for row in rows:
        body += b" "
        for (_name, kind, length, _dec), value in zip(fields, row):
            text = "" if value is None else str(value)
            padded = text.rjust(length) if kind in "NF" else text.ljust(length)
            body += padded[:length].encode("latin-1")
    Path(path).write_bytes(header + body + b"\x1A")


# ─────────────────────────────────────────────────────────────────────────────
# WKT
# ─────────────────────────────────────────────────────────────────────────────

class TestWKTParsing:
    @pytest.mark.parametrize(
        "wkt,expected_type",
        [
            ("POINT (77.209 28.6139)", "Point"),
            ("LINESTRING (0 0, 1 1, 2 2)", "LineString"),
            ("POLYGON ((0 0, 4 0, 4 4, 0 0))", "Polygon"),
            ("MULTIPOINT ((0 0), (1 1))", "MultiPoint"),
            ("MULTIPOINT (0 0, 1 1)", "MultiPoint"),
            ("MULTILINESTRING ((0 0, 1 1), (2 2, 3 3))", "MultiLineString"),
            ("MULTIPOLYGON (((0 0, 1 0, 1 1, 0 0)))", "MultiPolygon"),
            ("GEOMETRYCOLLECTION (POINT (1 2), LINESTRING (0 0, 1 1))", "GeometryCollection"),
        ],
    )
    def test_types(self, wkt, expected_type):
        assert fm.parse_wkt(wkt)["type"] == expected_type

    def test_point_coordinates(self):
        assert fm.parse_wkt("POINT (77.209 28.6139)")["coordinates"] == [77.209, 28.6139]

    def test_lowercase_keyword(self):
        assert fm.parse_wkt("point (1 2)")["type"] == "Point"

    def test_z_ordinates_are_dropped(self):
        assert fm.parse_wkt("POINT Z (1 2 3)")["coordinates"] == [1, 2]

    def test_zm_ordinates_are_dropped(self):
        assert fm.parse_wkt("POINT ZM (1 2 3 4)")["coordinates"] == [1, 2]

    def test_ewkt_srid_is_accepted(self):
        assert fm.parse_wkt("SRID=4326;POINT (1 2)")["coordinates"] == [1, 2]

    def test_polygon_with_hole(self):
        wkt = "POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0), (4 4, 6 4, 6 6, 4 6, 4 4))"
        assert len(fm.parse_wkt(wkt)["coordinates"]) == 2

    def test_empty_geometries(self):
        assert fm.parse_wkt("POINT EMPTY")["coordinates"] == []
        assert fm.parse_wkt("LINESTRING EMPTY")["coordinates"] == []

    def test_geometry_collection_members(self):
        parsed = fm.parse_wkt("GEOMETRYCOLLECTION (POINT (1 2), POINT (3 4))")
        assert len(parsed["geometries"]) == 2

    def test_rejects_garbage(self):
        with pytest.raises(InvalidGeometryError):
            fm.parse_wkt("NOT WKT AT ALL !!")

    def test_rejects_unknown_type(self):
        with pytest.raises(InvalidGeometryError):
            fm.parse_wkt("TRIANGLE ((0 0, 1 0, 1 1, 0 0))")

    def test_rejects_one_dimensional_point(self):
        with pytest.raises(InvalidGeometryError):
            fm.parse_wkt("POINT (5)")


class TestWKTWriting:
    @pytest.mark.parametrize(
        "wkt",
        [
            "POINT (77.209 28.6139)",
            "LINESTRING (0 0, 1 1, 2 2)",
            "POLYGON ((0 0, 4 0, 4 4, 0 0))",
            "MULTIPOINT ((0 0), (1 1))",
            "MULTILINESTRING ((0 0, 1 1), (2 2, 3 3))",
            "MULTIPOLYGON (((0 0, 1 0, 1 1, 0 0)))",
        ],
    )
    def test_round_trips(self, wkt):
        assert fm.to_wkt(fm.parse_wkt(wkt)) == wkt

    def test_trailing_zeros_are_trimmed(self):
        assert fm.to_wkt({"type": "Point", "coordinates": [1.0, 2.0]}) == "POINT (1 2)"

    def test_empty_point(self):
        assert fm.to_wkt({"type": "Point", "coordinates": []}) == "POINT EMPTY"

    def test_accepts_a_feature(self):
        feature = point_feature(*DELHI, name="Delhi")
        assert fm.to_wkt(feature).startswith("POINT (")

    def test_rejects_unknown_type(self):
        with pytest.raises(InvalidGeometryError):
            fm.to_wkt({"type": "Sphere", "coordinates": []})


class TestWKTFiles:
    def test_reads_one_geometry_per_line(self, tmp_path):
        path = tmp_path / "shapes.wkt"
        path.write_text("POINT (1 2)\nLINESTRING (0 0, 1 1)\n", encoding="utf-8")
        assert len(fm.read_wkt_file(str(path))) == 2

    def test_skips_blanks_and_comments(self, tmp_path):
        path = tmp_path / "shapes.wkt"
        path.write_text("# a comment\n\nPOINT (1 2)\n", encoding="utf-8")
        assert len(fm.read_wkt_file(str(path))) == 1

    def test_tab_separated_label(self, tmp_path):
        path = tmp_path / "shapes.wkt"
        path.write_text("POINT (77.2 28.6)\tDelhi\n", encoding="utf-8")
        assert fm.read_wkt_file(str(path))[0]["properties"]["name"] == "Delhi"

    def test_semicolon_separated_label(self, tmp_path):
        path = tmp_path / "shapes.wkt"
        path.write_text("POINT (77.2 28.6);Delhi\n", encoding="utf-8")
        assert fm.read_wkt_file(str(path))[0]["properties"]["name"] == "Delhi"

    def test_writes_and_reads_back(self, tmp_path):
        path = str(tmp_path / "out.wkt")
        fm.write_wkt_file([point_feature(*DELHI), point_feature(*MUMBAI)], path)
        assert len(fm.read_wkt_file(path)) == 2


# ─────────────────────────────────────────────────────────────────────────────
# NDJSON
# ─────────────────────────────────────────────────────────────────────────────

class TestNDJSON:
    def test_round_trip(self, tmp_path):
        path = str(tmp_path / "out.ndjson")
        features = [point_feature(*DELHI, name="Delhi"), point_feature(*MUMBAI, name="Mumbai")]
        fm.write_ndjson(features, path)
        assert fm.read_ndjson(path) == features

    def test_one_line_per_feature(self, tmp_path):
        path = tmp_path / "out.ndjson"
        fm.write_ndjson([point_feature(*DELHI)] * 3, str(path))
        assert len(path.read_text().strip().splitlines()) == 3

    def test_blank_lines_are_skipped(self, tmp_path):
        path = tmp_path / "in.ndjson"
        path.write_text(json.dumps(point_feature(*DELHI)) + "\n\n\n", encoding="utf-8")
        assert len(fm.read_ndjson(str(path))) == 1

    def test_accepts_bare_geometries(self, tmp_path):
        path = tmp_path / "in.ndjson"
        path.write_text(json.dumps({"type": "Point", "coordinates": [1, 2]}), encoding="utf-8")
        assert fm.read_ndjson(str(path))[0]["type"] == "Feature"

    def test_accepts_a_feature_collection_line(self, tmp_path):
        path = tmp_path / "in.ndjson"
        collection = {"type": "FeatureCollection", "features": [point_feature(*DELHI)] * 2}
        path.write_text(json.dumps(collection), encoding="utf-8")
        assert len(fm.read_ndjson(str(path))) == 2

    def test_bad_json_names_the_line(self, tmp_path):
        path = tmp_path / "in.ndjson"
        path.write_text('{"type":"Feature"}\nnot json\n', encoding="utf-8")
        with pytest.raises(UnsupportedFormatError) as exc:
            fm.read_ndjson(str(path))
        assert "line 2" in str(exc.value)


# ─────────────────────────────────────────────────────────────────────────────
# GPX
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_GPX = """<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <wpt lat="28.6139" lon="77.2090">
    <name>Start</name><ele>216.5</ele><time>2025-01-01T10:00:00Z</time>
  </wpt>
  <rte><name>Route A</name>
    <rtept lat="1" lon="1"/><rtept lat="2" lon="2"/>
  </rte>
  <trk><name>Morning Ride</name>
    <trkseg><trkpt lat="28.6" lon="77.2"/><trkpt lat="28.7" lon="77.3"/></trkseg>
    <trkseg><trkpt lat="29.6" lon="78.2"/><trkpt lat="29.7" lon="78.3"/></trkseg>
  </trk>
</gpx>
"""


class TestGPX:
    @pytest.fixture
    def sample(self, tmp_path):
        path = tmp_path / "track.gpx"
        path.write_text(SAMPLE_GPX, encoding="utf-8")
        return str(path)

    def test_reads_all_parts(self, sample):
        assert len(fm.read_gpx(sample)) == 4       # 1 waypoint, 1 route, 2 track segments

    def test_waypoint_becomes_a_point(self, sample):
        waypoint = fm.read_gpx(sample)[0]
        assert waypoint["geometry"]["type"] == "Point"
        assert waypoint["properties"]["_kind"] == "waypoint"

    def test_elevation_is_numeric(self, sample):
        assert fm.read_gpx(sample)[0]["properties"]["ele"] == 216.5

    def test_timestamp_is_kept(self, sample):
        assert fm.read_gpx(sample)[0]["properties"]["time"].startswith("2025")

    def test_route_becomes_a_line(self, sample):
        route = next(f for f in fm.read_gpx(sample) if f["properties"]["_kind"] == "route")
        assert route["geometry"]["type"] == "LineString"
        assert route["properties"]["name"] == "Route A"

    def test_track_name_reaches_every_segment(self, sample):
        tracks = [f for f in fm.read_gpx(sample) if f["properties"]["_kind"] == "track"]
        assert len(tracks) == 2
        assert all(t["properties"]["name"] == "Morning Ride" for t in tracks)

    def test_coordinates_are_geojson_order(self, sample):
        assert fm.read_gpx(sample)[0]["geometry"]["coordinates"] == [77.2090, 28.6139]

    def test_write_round_trip(self, tmp_path):
        path = str(tmp_path / "out.gpx")
        fm.write_gpx([point_feature(*DELHI, name="Delhi")], path)
        assert fm.read_gpx(path)[0]["properties"]["name"] == "Delhi"

    def test_writes_lines_as_tracks(self, tmp_path):
        path = str(tmp_path / "out.gpx")
        line = {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            "properties": {"name": "Leg"},
        }
        fm.write_gpx([line], path)
        assert fm.read_gpx(path)[0]["geometry"]["type"] == "LineString"

    def test_empty_gpx_raises(self, tmp_path):
        path = tmp_path / "empty.gpx"
        path.write_text('<?xml version="1.0"?><gpx version="1.1"></gpx>', encoding="utf-8")
        with pytest.raises(EmptyDatasetError):
            fm.read_gpx(str(path))

    def test_malformed_xml_raises(self, tmp_path):
        path = tmp_path / "bad.gpx"
        path.write_text("<gpx><unclosed>", encoding="utf-8")
        with pytest.raises(UnsupportedFormatError):
            fm.read_gpx(str(path))

    def test_missing_file_raises(self):
        with pytest.raises(QFileNotFoundError):
            fm.read_gpx("/no/such/track.gpx")


# ─────────────────────────────────────────────────────────────────────────────
# KML
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
  <Placemark>
    <name>Delhi</name><description>Capital</description>
    <ExtendedData><Data name="pop"><value>32900000</value></Data></ExtendedData>
    <Point><coordinates>77.2090,28.6139,0</coordinates></Point>
  </Placemark>
  <Placemark>
    <name>Corridor</name>
    <LineString><coordinates>77.2,28.6 72.9,19.1</coordinates></LineString>
  </Placemark>
  <Placemark>
    <name>Zone</name>
    <Polygon>
      <outerBoundaryIs><LinearRing>
        <coordinates>0,0 10,0 10,10 0,10 0,0</coordinates>
      </LinearRing></outerBoundaryIs>
      <innerBoundaryIs><LinearRing>
        <coordinates>4,4 6,4 6,6 4,6 4,4</coordinates>
      </LinearRing></innerBoundaryIs>
    </Polygon>
  </Placemark>
</Document></kml>
"""


class TestKML:
    @pytest.fixture
    def sample(self, tmp_path):
        path = tmp_path / "places.kml"
        path.write_text(SAMPLE_KML, encoding="utf-8")
        return str(path)

    def test_reads_every_placemark(self, sample):
        assert len(fm.read_kml(sample)) == 3

    def test_geometry_types(self, sample):
        kinds = [f["geometry"]["type"] for f in fm.read_kml(sample)]
        assert kinds == ["Point", "LineString", "Polygon"]

    def test_name_and_description(self, sample):
        first = fm.read_kml(sample)[0]
        assert first["properties"]["name"] == "Delhi"
        assert first["properties"]["description"] == "Capital"

    def test_extended_data(self, sample):
        assert fm.read_kml(sample)[0]["properties"]["pop"] == "32900000"

    def test_altitude_is_dropped(self, sample):
        assert fm.read_kml(sample)[0]["geometry"]["coordinates"] == [77.2090, 28.6139]

    def test_polygon_hole_is_read(self, sample):
        polygon = fm.read_kml(sample)[2]
        assert len(polygon["geometry"]["coordinates"]) == 2
        assert point_in_geometry((5, 5), polygon["geometry"]) is False
        assert point_in_geometry((1, 1), polygon["geometry"]) is True

    def test_write_round_trip(self, tmp_path):
        path = str(tmp_path / "out.kml")
        fm.write_kml([point_feature(*DELHI, name="Delhi", pop=100)], path)
        result = fm.read_kml(path)[0]
        assert result["properties"]["name"] == "Delhi"
        assert result["properties"]["pop"] == "100"

    def test_special_characters_are_escaped(self, tmp_path):
        path = str(tmp_path / "out.kml")
        fm.write_kml([point_feature(*DELHI, name="A & B <tag>")], path)
        assert fm.read_kml(path)[0]["properties"]["name"] == "A & B <tag>"

    def test_private_properties_are_not_exported(self, tmp_path):
        path = str(tmp_path / "out.kml")
        fm.write_kml([point_feature(*DELHI, name="X", _distance=5)], path)
        assert "_distance" not in fm.read_kml(path)[0]["properties"]

    def test_empty_kml_raises(self, tmp_path):
        path = tmp_path / "empty.kml"
        path.write_text('<?xml version="1.0"?><kml><Document/></kml>', encoding="utf-8")
        with pytest.raises(EmptyDatasetError):
            fm.read_kml(str(path))


# ─────────────────────────────────────────────────────────────────────────────
# Shapefile
# ─────────────────────────────────────────────────────────────────────────────

class TestShapefile:
    def test_points_with_attributes(self, tmp_path):
        shp = str(tmp_path / "cities.shp")
        write_shp(shp, 1, [point_record(77.2090, 28.6139), point_record(72.8777, 19.0760)])
        write_dbf(
            str(tmp_path / "cities.dbf"),
            [("NAME", "C", 20, 0), ("POP", "N", 10, 0), ("AREA", "N", 12, 2),
             ("CAPITAL", "L", 1, 0)],
            [("Delhi", 32900000, 1484.00, "T"), ("Mumbai", 20700000, 603.40, "F")],
        )
        features = fm.read_shapefile(shp)
        assert len(features) == 2
        assert features[0]["properties"]["NAME"] == "Delhi"
        assert features[0]["properties"]["POP"] == 32900000
        assert features[0]["properties"]["AREA"] == 1484.0
        assert features[0]["properties"]["CAPITAL"] is True
        assert features[1]["properties"]["CAPITAL"] is False

    def test_coordinates_are_geojson_order(self, tmp_path):
        shp = str(tmp_path / "one.shp")
        write_shp(shp, 1, [point_record(77.2090, 28.6139)])
        assert fm.read_shapefile(shp)[0]["geometry"]["coordinates"] == [77.2090, 28.6139]

    def test_null_shapes_are_skipped(self, tmp_path):
        shp = str(tmp_path / "gaps.shp")
        write_shp(shp, 1, [point_record(1, 2), null_record(), point_record(3, 4)])
        assert len(fm.read_shapefile(shp)) == 2

    def test_pointz_drops_the_z(self, tmp_path):
        shp = str(tmp_path / "z.shp")
        write_shp(shp, 11, [pointz_record(10.0, 20.0, 300.0)])
        assert fm.read_shapefile(shp)[0]["geometry"]["coordinates"] == [10.0, 20.0]

    def test_single_part_polyline(self, tmp_path):
        shp = str(tmp_path / "road.shp")
        write_shp(shp, 3, [shape_record(3, [[(0, 0), (1, 1), (2, 0)]])])
        assert fm.read_shapefile(shp)[0]["geometry"]["type"] == "LineString"

    def test_multi_part_polyline(self, tmp_path):
        shp = str(tmp_path / "roads.shp")
        write_shp(shp, 3, [shape_record(3, [[(5, 5), (6, 6)], [(7, 7), (8, 8)]])])
        assert fm.read_shapefile(shp)[0]["geometry"]["type"] == "MultiLineString"

    def test_simple_polygon(self, tmp_path):
        shp = str(tmp_path / "zone.shp")
        ring = [(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)]
        write_shp(shp, 5, [shape_record(5, [ring])])
        assert fm.read_shapefile(shp)[0]["geometry"]["type"] == "Polygon"

    def test_polygon_with_hole_uses_winding(self, tmp_path):
        # Esri marks holes by winding: clockwise outer, counter-clockwise inner.
        shp = str(tmp_path / "holed.shp")
        outer = [(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)]
        hole = [(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)]
        write_shp(shp, 5, [shape_record(5, [outer, hole])])
        geometry = fm.read_shapefile(shp)[0]["geometry"]
        assert geometry["type"] == "Polygon"
        assert len(geometry["coordinates"]) == 2
        assert point_in_geometry((5, 5), geometry) is False
        assert point_in_geometry((1, 1), geometry) is True

    def test_two_outer_rings_become_a_multipolygon(self, tmp_path):
        shp = str(tmp_path / "islands.shp")
        first = [(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)]
        second = [(20, 20), (20, 22), (22, 22), (22, 20), (20, 20)]
        write_shp(shp, 5, [shape_record(5, [first, second])])
        geometry = fm.read_shapefile(shp)[0]["geometry"]
        assert geometry["type"] == "MultiPolygon"
        assert point_in_geometry((21, 21), geometry) is True

    def test_multipoint(self, tmp_path):
        shp = str(tmp_path / "mp.shp")
        record = struct.pack("<i", 8) + struct.pack("<4d", 0, 0, 1, 1)
        record += struct.pack("<i", 2) + struct.pack("<4d", 0, 0, 1, 1)
        write_shp(shp, 8, [record])
        assert fm.read_shapefile(shp)[0]["geometry"]["type"] == "MultiPoint"

    def test_missing_dbf_is_not_fatal(self, tmp_path):
        shp = str(tmp_path / "bare.shp")
        write_shp(shp, 1, [point_record(1, 2)])
        assert fm.read_shapefile(shp)[0]["properties"] == {}

    def test_blank_dbf_values_become_none(self, tmp_path):
        shp = str(tmp_path / "blank.shp")
        write_shp(shp, 1, [point_record(1, 2)])
        write_dbf(str(tmp_path / "blank.dbf"), [("NAME", "C", 10, 0)], [("",)])
        assert fm.read_shapefile(shp)[0]["properties"]["NAME"] is None

    def test_bad_magic_number(self, tmp_path):
        path = tmp_path / "fake.shp"
        path.write_bytes(b"\x00" * 200)
        with pytest.raises(UnsupportedFormatError):
            fm.read_shapefile(str(path))

    def test_truncated_file(self, tmp_path):
        path = tmp_path / "short.shp"
        path.write_bytes(b"\x00" * 20)
        with pytest.raises(UnsupportedFormatError):
            fm.read_shapefile(str(path))

    def test_missing_file(self):
        with pytest.raises(QFileNotFoundError):
            fm.read_shapefile("/no/such/file.shp")

    def test_all_null_shapes_raise(self, tmp_path):
        shp = str(tmp_path / "empty.shp")
        write_shp(shp, 1, [null_record()])
        with pytest.raises(EmptyDatasetError):
            fm.read_shapefile(shp)


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch and coercion
# ─────────────────────────────────────────────────────────────────────────────

class TestDispatch:
    def test_read_any_dispatches_on_extension(self, tmp_path):
        path = str(tmp_path / "x.ndjson")
        fm.write_ndjson([point_feature(*DELHI)], path)
        assert len(fm.read_any(path)) == 1

    def test_read_any_rejects_unknown(self, tmp_path):
        path = tmp_path / "x.docx"
        path.write_text("nope", encoding="utf-8")
        with pytest.raises(UnsupportedFormatError):
            fm.read_any(str(path))

    @pytest.mark.parametrize("ext", [".ndjson", ".wkt", ".gpx", ".kml"])
    def test_write_any_dispatches(self, tmp_path, ext):
        path = str(tmp_path / f"out{ext}")
        fm.write_any([point_feature(*DELHI, name="Delhi")], path)
        assert os.path.getsize(path) > 0

    def test_write_any_rejects_unknown(self, tmp_path):
        with pytest.raises(UnsupportedFormatError):
            fm.write_any([point_feature(*DELHI)], str(tmp_path / "out.docx"))

    def test_readable_and_writable_lists(self):
        assert ".shp" in fm.READABLE
        assert ".png" in fm.WRITABLE


class TestIterFeatures:
    def test_passes_features_through(self):
        feature = point_feature(*DELHI)
        assert fm.iter_features([feature]) == [feature]

    def test_wraps_bare_geometries(self):
        result = fm.iter_features([{"type": "Point", "coordinates": [1, 2]}])
        assert result[0]["type"] == "Feature"

    def test_accepts_lat_lng_tuples(self):
        result = fm.iter_features([(28.6139, 77.2090)])
        assert result[0]["geometry"]["coordinates"] == [77.2090, 28.6139]

    def test_accepts_dicts_with_lat_lng(self):
        result = fm.iter_features([{"lat": 28.6, "lng": 77.2, "name": "Delhi"}])
        assert result[0]["geometry"]["coordinates"] == [77.2, 28.6]
        assert result[0]["properties"] == {"name": "Delhi"}

    def test_accepts_latitude_longitude_spelling(self):
        result = fm.iter_features([{"latitude": 1.0, "longitude": 2.0}])
        assert result[0]["geometry"]["coordinates"] == [2.0, 1.0]

    def test_rejects_dicts_without_coordinates(self):
        with pytest.raises(InvalidGeometryError):
            fm.iter_features([{"name": "nowhere"}])

    def test_rejects_unusable_records(self):
        with pytest.raises(InvalidGeometryError):
            fm.iter_features(["just a string"])
