"""
tests/test_render.py
====================
Static map images: qoregeo.render.render_svg / render_png.

Offline and dependency-free: SVGs are parsed with xml.etree.ElementTree and
PNGs are read back with the small stdlib decoder below (chunk CRCs, IHDR,
zlib, scanline filters), so the tests check real pixels: point markers,
polygon fills and holes, line strokes, dashed overlays, the title text, the
antimeridian rule, argument validation and the rendering speed.

Run with:
    pytest tests/test_render.py -v
"""

from __future__ import annotations

import ast
import math
import re
import struct
import sys
import time
import tracemalloc
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any

import pytest

from qoregeo.exceptions import InvalidArgumentError, QOREgeoError
from qoregeo.render import _FONT, _ascii, _Canvas, _glyph, render_png, render_svg

SVG_NS = "{http://www.w3.org/2000/svg}"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

WHITE = (255, 255, 255, 255)
TEAL = (0x00, 0xD4, 0xAA, 255)
GOLD = (0xFF, 0xD7, 0x00, 255)
DARK_TEXT = (0x1F, 0x29, 0x33, 255)
LIGHT_TEXT = (0xF5, 0xF7, 0xFA, 255)
CREDIT = (0x8A, 0x94, 0xA6, 255)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def feature(geometry: dict[str, Any] | None, **props: Any) -> dict[str, Any]:
    return {"type": "Feature", "properties": props, "geometry": geometry}


def point(lng: float, lat: float) -> dict[str, Any]:
    return feature({"type": "Point", "coordinates": [lng, lat]})


def square(half: float, cx: float = 0.0, cy: float = 0.0) -> list[list[float]]:
    """Closed counter-clockwise square ring centred on (cx, cy)."""
    return [[cx - half, cy - half], [cx + half, cy - half], [cx + half, cy + half],
            [cx - half, cy + half], [cx - half, cy - half]]


def circle_ring(cx: float, cy: float, r: float, n: int = 64) -> list[list[float]]:
    ring = [[cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)]
            for i in range(n)]
    return ring + [ring[0]]


def donut() -> dict[str, Any]:
    """A 20°x20° square around (0, 0) with a 4°x4° hole in the middle."""
    return feature({"type": "Polygon", "coordinates": [square(10), square(2)[::-1]]})


def buffer_like(lat: float, lng: float, radius_deg: float) -> dict[str, Any]:
    """Shaped like GeoEngine.buffer(): a Polygon plus _center / _radius_km keys."""
    return {"type": "Polygon", "coordinates": [circle_ring(lng, lat, radius_deg)],
            "_center": (lat, lng), "_radius_km": radius_deg * 111.0}


def parse_svg(svg: str) -> ET.Element:
    return ET.fromstring(svg)


def svg_group(root: ET.Element, gid: str) -> ET.Element | None:
    for g in root.iter(f"{SVG_NS}g"):
        if g.get("id") == gid:
            return g
    return None


def circles(root: ET.Element) -> list[tuple[float, float]]:
    return [(float(c.get("cx", "nan")), float(c.get("cy", "nan")))
            for c in root.iter(f"{SVG_NS}circle")]


def path_points(d: str) -> list[tuple[float, float]]:
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", d)]
    return list(zip(nums[0::2], nums[1::2], strict=True))


# ── A minimal PNG decoder (stdlib only) ─────────────────────────────────────

def png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    assert data[:8] == PNG_SIGNATURE
    pos, chunks = 8, []
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF, f"bad CRC in {kind!r}"
        chunks.append((kind, body))
        pos += 12 + length
    assert pos == len(data), "trailing bytes after the last chunk"
    return chunks


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


class PNG:
    """Decoded 8-bit RGBA PNG: ``img.px(x, y) -> (r, g, b, a)``."""

    def __init__(self, data: bytes) -> None:
        self.chunks = png_chunks(data)
        kind, ihdr = self.chunks[0]
        assert kind == b"IHDR" and len(ihdr) == 13
        (self.width, self.height, self.depth, self.color_type, self.compression,
         self.filter, self.interlace) = struct.unpack(">IIBBBBB", ihdr)
        assert (self.depth, self.color_type) == (8, 6), "expected 8-bit RGBA"
        raw = zlib.decompress(b"".join(body for k, body in self.chunks if k == b"IDAT"))
        stride = self.width * 4
        assert len(raw) == self.height * (stride + 1)
        self.filters: set[int] = set()
        rows: list[bytes] = []
        prev = bytearray(stride)
        for y in range(self.height):
            start = y * (stride + 1)
            ftype = raw[start]
            self.filters.add(ftype)
            line = bytearray(raw[start + 1:start + 1 + stride])
            for i in range(stride if ftype else 0):     # filter 0 (None): bytes as-is
                a = line[i - 4] if i >= 4 else 0
                b = prev[i]
                c = prev[i - 4] if i >= 4 else 0
                if ftype == 1:
                    line[i] = (line[i] + a) & 0xFF
                elif ftype == 2:
                    line[i] = (line[i] + b) & 0xFF
                elif ftype == 3:
                    line[i] = (line[i] + (a + b) // 2) & 0xFF
                elif ftype == 4:
                    line[i] = (line[i] + _paeth(a, b, c)) & 0xFF
            rows.append(bytes(line))
            prev = line
        self.rows = rows

    def px(self, x: int, y: int) -> tuple[int, int, int, int]:
        o = x * 4
        r = self.rows[y]
        return r[o], r[o + 1], r[o + 2], r[o + 3]

    def all_pixels(self) -> list[tuple[int, int, tuple[int, int, int, int]]]:
        out = []
        for y, row in enumerate(self.rows):
            pixels = zip(row[0::4], row[1::4], row[2::4], row[3::4], strict=True)
            out += [(x, y, c) for x, c in enumerate(pixels)]
        return out

    def where(self, color: tuple[int, int, int, int], tol: int = 0) -> list[tuple[int, int]]:
        r0, g0, b0, a0 = color
        found = []
        for y, row in enumerate(self.rows):
            pixels = zip(row[0::4], row[1::4], row[2::4], row[3::4], strict=True)
            found += [(x, y) for x, (r, g, b, a) in enumerate(pixels)
                      if abs(r - r0) <= tol and abs(g - g0) <= tol and abs(b - b0) <= tol
                      and abs(a - a0) <= tol]
        return found

    def chunk(self, kind: bytes) -> bytes | None:
        return next((body for k, body in self.chunks if k == kind), None)


def close(a: tuple[int, ...], b: tuple[int, ...], tol: int = 0) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def png(*args: Any, **kwargs: Any) -> PNG:
    return PNG(render_png(*args, **kwargs))


# ─────────────────────────────────────────────────────────────────────────────
# SVG output
# ─────────────────────────────────────────────────────────────────────────────

class TestSvgDocument:
    def test_parses_as_standalone_svg(self) -> None:
        root = parse_svg(render_svg([point(77.2, 28.6)], width=640, height=480))
        assert root.tag == f"{SVG_NS}svg"
        assert root.get("version") == "1.1"
        assert root.get("width") == "640" and root.get("height") == "480"
        assert root.get("viewBox") == "0 0 640 480"

    def test_xml_declaration_and_namespace(self) -> None:
        svg = render_svg([point(0, 0)])
        assert svg.startswith('<?xml version="1.0" encoding="UTF-8"?>')
        assert 'xmlns="http://www.w3.org/2000/svg"' in svg

    def test_title_element_is_escaped(self) -> None:
        title = "Stores <script>alert('x')</script> & \"more\""
        svg = render_svg([point(0, 0)], title=title)
        assert "<script>" not in svg
        assert "&lt;script&gt;" in svg and "&amp;" in svg
        root = parse_svg(svg)
        assert root.findtext(f"{SVG_NS}title") == title
        visible = [t for t in root.iter(f"{SVG_NS}text") if t.get("id") == "qg-title"]
        assert len(visible) == 1 and visible[0].text == title

    def test_default_title_element_without_visible_title(self) -> None:
        root = parse_svg(render_svg([point(0, 0)]))
        assert root.findtext(f"{SVG_NS}title") == "QOREgeo map"
        assert all(t.get("id") != "qg-title" for t in root.iter(f"{SVG_NS}text"))

    def test_xml_invalid_characters_in_title_are_replaced(self) -> None:
        title = "a" + chr(1) + "b" + chr(0xD800) + "c" + chr(0xFFFE)
        svg = render_svg([], title=title)
        svg.encode("utf-8")                          # no lone surrogates left
        root = parse_svg(svg)
        assert root.findtext(f"{SVG_NS}title") == "a" + chr(0xFFFD) + "b" + chr(0xFFFD) + "c" \
            + chr(0xFFFD)

    def test_non_ascii_title_kept_verbatim(self) -> None:
        title = "東京 · Zürich · मुंबई"
        root = parse_svg(render_svg([point(0, 0)], title=title))
        assert root.findtext(f"{SVG_NS}title") == title

    def test_credit_bottom_right(self) -> None:
        root = parse_svg(render_svg([point(0, 0)], width=800, height=600))
        credit = [t for t in root.iter(f"{SVG_NS}text") if t.get("id") == "qg-credit"]
        assert len(credit) == 1
        assert credit[0].text == "QOREgeo"
        assert credit[0].get("text-anchor") == "end"
        assert float(credit[0].get("x", 0)) > 700 and float(credit[0].get("y", 0)) > 550

    def test_no_credit_on_tiny_canvas(self) -> None:
        root = parse_svg(render_svg([point(0, 0)], width=64, height=32, padding=4))
        assert all(t.get("id") != "qg-credit" for t in root.iter(f"{SVG_NS}text"))

    def test_background_rect(self) -> None:
        root = parse_svg(render_svg([point(0, 0)], background="#ABC"))
        rect = root.find(f"{SVG_NS}rect")
        assert rect is not None and rect.get("fill") == "#aabbcc"

    def test_deterministic(self) -> None:
        feats = [point(77.2, 28.6), donut()]
        assert render_svg(feats, title="x") == render_svg(feats, title="x")

    @pytest.mark.parametrize("encoding", ["cp1252", "latin-1", "ascii", "utf-8"])
    def test_ascii_output_survives_any_save_encoding(self, tmp_path: Path,
                                                     encoding: str) -> None:
        # open(path, "w") on Windows is not UTF-8: non-ASCII text is written as
        # numeric character references, so the declared UTF-8 always holds.
        title = "Délhi — 東京 €"
        svg = render_svg([point(77.2, 28.6)], title=title)
        assert svg.isascii()
        out = tmp_path / "map.svg"
        out.write_text(svg, encoding=encoding)
        root = ET.parse(out).getroot()
        assert root.findtext(f"{SVG_NS}title") == title
        visible = [t for t in root.iter(f"{SVG_NS}text") if t.get("id") == "qg-title"]
        assert visible[0].text == title

    def test_root_uses_only_svg11_attributes(self) -> None:
        root = parse_svg(render_svg([point(0, 0)]))
        assert set(root.attrib) == {"version", "width", "height", "viewBox"}


class TestSvgShapes:
    def test_points_style(self) -> None:
        root = parse_svg(render_svg([point(0, 0), point(1, 1), point(2, 0)]))
        group = svg_group(root, "qg-points")
        assert group is not None
        assert group.get("fill") == "#00d4aa"
        assert group.get("stroke") == "#ffffff"
        assert group.get("stroke-width") == "1.5"
        assert len(circles(root)) == 3
        assert all(c.get("r") == "4" for c in root.iter(f"{SVG_NS}circle"))

    def test_polygon_with_hole_uses_evenodd(self) -> None:
        root = parse_svg(render_svg([donut()]))
        group = svg_group(root, "qg-shapes")
        assert group is not None
        assert group.get("fill") == "#00d4aa" and group.get("stroke") == "#00d4aa"
        assert group.get("stroke-width") == "2"
        assert float(group.get("fill-opacity", "0")) == pytest.approx(0.18)
        paths = list(group.iter(f"{SVG_NS}path"))
        assert len(paths) == 1
        assert paths[0].get("fill-rule") == "evenodd"
        d = paths[0].get("d", "")
        assert d.count("M") == 2 and d.count("Z") == 2      # outer ring + hole

    def test_line_is_unfilled(self) -> None:
        line = feature({"type": "LineString", "coordinates": [[0, 0], [1, 1], [2, 0]]})
        root = parse_svg(render_svg([line]))
        group = svg_group(root, "qg-shapes")
        assert group is not None
        path = next(group.iter(f"{SVG_NS}path"))
        assert path.get("fill") == "none"
        assert "Z" not in path.get("d", "")
        assert len(path_points(path.get("d", ""))) == 3

    def test_multi_geometries(self) -> None:
        feats = [
            feature({"type": "MultiPoint", "coordinates": [[0, 0], [1, 1], [2, 2]]}),
            feature({"type": "MultiLineString", "coordinates": [[[0, 0], [1, 0]], [[0, 1], [1, 1]]]}),
            feature({"type": "MultiPolygon", "coordinates": [[square(0.2)], [square(0.2, 2, 2)]]}),
        ]
        root = parse_svg(render_svg(feats))
        assert len(circles(root)) == 3
        group = svg_group(root, "qg-shapes")
        assert group is not None
        paths = list(group.iter(f"{SVG_NS}path"))
        assert sum(p.get("fill-rule") == "evenodd" for p in paths) == 2
        assert sum(p.get("fill") == "none" for p in paths) == 2

    def test_geometry_collection(self) -> None:
        gc = feature({"type": "GeometryCollection", "geometries": [
            {"type": "Point", "coordinates": [0, 0]},
            {"type": "LineString", "coordinates": [[1, 1], [2, 0]]},
            {"type": "GeometryCollection", "geometries": [
                {"type": "Polygon", "coordinates": [square(0.5, 3, 3)]},
            ]},
        ]})
        root = parse_svg(render_svg([gc]))
        assert len(circles(root)) == 1
        group = svg_group(root, "qg-shapes")
        assert group is not None and len(list(group.iter(f"{SVG_NS}path"))) == 2

    def test_coordinates_rounded_to_two_decimals(self) -> None:
        feats = [point(77.123456, 28.654321), donut(),
                 feature({"type": "LineString", "coordinates": [[0.1234, 0.5678], [3.3333, 1.1111]]})]
        root = parse_svg(render_svg(feats, width=777, height=555))
        numbers = [c.get(a, "") for c in root.iter(f"{SVG_NS}circle") for a in ("cx", "cy")]
        for p in root.iter(f"{SVG_NS}path"):
            numbers += re.findall(r"-?\d+(?:\.\d+)?", p.get("d", ""))
        assert numbers
        assert all(re.fullmatch(r"-?\d+(\.\d{1,2})?", n) for n in numbers), numbers

    def test_shapes_stay_inside_canvas(self) -> None:
        feats = [point(-70, -50), point(150, 60), donut()]
        root = parse_svg(render_svg(feats, width=400, height=300, padding=0))
        pts = circles(root)
        for p in root.iter(f"{SVG_NS}path"):
            pts += path_points(p.get("d", ""))
        assert all(0 <= x <= 400 and 0 <= y <= 300 for x, y in pts)

    def test_padding_respected(self) -> None:
        feats = [point(-1, -1), point(1, 1), point(-1, 1), point(1, -1)]
        root = parse_svg(render_svg(feats, width=800, height=600, padding=100))
        xs, ys = zip(*circles(root), strict=True)
        inset = 100 + 4 + 1.5 / 2                      # padding + marker radius + half outline
        assert min(xs) >= inset - 0.01 and max(xs) <= 800 - inset + 0.01
        assert min(ys) >= inset - 0.01 and max(ys) <= 600 - inset + 0.01

    def test_aspect_ratio_preserved(self) -> None:
        feats = [point(-1, -1), point(1, 1), point(-1, 1), point(1, -1)]
        root = parse_svg(render_svg(feats, width=900, height=400))
        xs, ys = zip(*circles(root), strict=True)
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        assert w == pytest.approx(h, rel=0.01)          # square near the equator stays square
        assert (min(xs) + max(xs)) / 2 == pytest.approx(450, abs=0.02)

    def test_single_point_centred(self) -> None:
        root = parse_svg(render_svg([point(77.2, 28.6)], width=800, height=600))
        assert circles(root) == [(400.0, 300.0)]

    def test_single_point_centred_below_title(self) -> None:
        root = parse_svg(render_svg([point(77.2, 28.6)], width=800, height=600, title="T"))
        assert circles(root) == [(400.0, 28 + (600 - 28) / 2)]

    def test_coincident_points_get_small_extent(self) -> None:
        # Two points ~1 m apart are not blown up to the full canvas.
        root = parse_svg(render_svg([point(77.2, 28.6), point(77.20001, 28.6)]))
        for x, y in circles(root):
            assert abs(x - 400) < 5 and abs(y - 300) < 5

    def test_polar_latitudes_are_clamped(self) -> None:
        svg = render_svg([point(0, 90), point(10, -90), point(5, 0)])
        assert "nan" not in svg and "inf" not in svg
        assert len(circles(parse_svg(svg))) == 3

    def test_dark_background_gets_light_title(self) -> None:
        root = parse_svg(render_svg([point(0, 0)], title="Night", background="#0f111a"))
        title = next(t for t in root.iter(f"{SVG_NS}text") if t.get("id") == "qg-title")
        assert title.get("fill") == "#f5f7fa"

    def test_small_marker_outline_scales_down(self) -> None:
        root = parse_svg(render_svg([point(0, 0)], point_radius=0.5))
        group = svg_group(root, "qg-points")
        assert group is not None and float(group.get("stroke-width", "0")) <= 0.25

    def test_line_caps_inside_padding_with_tiny_markers(self) -> None:
        # The inset is the larger of the marker extent and the line half-width.
        line = feature({"type": "LineString", "coordinates": [[0, 0], [10, 5]]})
        root = parse_svg(render_svg([line, point(5, 2)], width=400, height=300,
                                    padding=10, point_radius=0.5))
        group = svg_group(root, "qg-shapes")
        assert group is not None
        pts = path_points(next(group.iter(f"{SVG_NS}path")).get("d", ""))
        assert min(x for x, _ in pts) >= 11 - 0.01 and max(x for x, _ in pts) <= 389 + 0.01

    @pytest.mark.parametrize("height", [16, 24, 40, 55])
    def test_title_on_short_canvas_keeps_data_inside(self, height: int) -> None:
        # A 28 px title band does not fit: the map stays on the canvas and the
        # title is set small over its top.
        feats = [point(0, 0), point(1, 1)]
        root = parse_svg(render_svg(feats, width=200, height=height, padding=0, title="T"))
        for x, y in circles(root):
            assert 0 <= x <= 200 and 0 <= y <= height
        title = next(t for t in root.iter(f"{SVG_NS}text") if t.get("id") == "qg-title")
        assert title.get("font-size") == "9" and float(title.get("y", "99")) < height

    def test_title_band_from_56_px(self) -> None:
        root = parse_svg(render_svg([point(0, 0)], width=200, height=56, padding=0, title="T"))
        title = next(t for t in root.iter(f"{SVG_NS}text") if t.get("id") == "qg-title")
        assert title.get("font-size") == "16"
        assert circles(root) == [(100.0, 28 + (56 - 28) / 2)]


class TestSvgOverlays:
    def test_overlay_dashed_gold(self) -> None:
        root = parse_svg(render_svg([point(77.2, 28.6)], overlays=[buffer_like(28.6, 77.2, 0.1)]))
        group = svg_group(root, "qg-overlays")
        assert group is not None
        assert group.get("stroke") == "#ffd700"
        assert group.get("stroke-dasharray") == "6 4"
        assert group.get("stroke-width") == "2"
        assert 0 < float(group.get("fill-opacity", "0")) < 0.2
        path = next(group.iter(f"{SVG_NS}path"))
        assert path.get("fill-rule") == "evenodd"

    def test_overlay_extends_bounds(self) -> None:
        # The buffer is much larger than the single point, so it sets the extent.
        root = parse_svg(render_svg([point(77.2, 28.6)], overlays=[buffer_like(28.6, 77.2, 1.0)]))
        group = svg_group(root, "qg-overlays")
        assert group is not None
        xs = [x for x, _ in path_points(next(group.iter(f"{SVG_NS}path")).get("d", ""))]
        assert max(xs) - min(xs) > 400

    def test_overlay_feature_collection_and_points(self) -> None:
        fc = {"type": "FeatureCollection", "features": [
            feature({"type": "Point", "coordinates": [1, 1]}),
            feature({"type": "LineString", "coordinates": [[0, 0], [2, 2]]}),
            feature(None),
        ]}
        root = parse_svg(render_svg([point(0, 0)], overlays=[fc]))
        assert svg_group(root, "qg-overlay-points") is not None
        group = svg_group(root, "qg-overlays")
        assert group is not None
        assert next(group.iter(f"{SVG_NS}path")).get("fill") == "none"


class TestInputs:
    def test_empty_input_is_blank_canvas(self) -> None:
        root = parse_svg(render_svg([], title="Nothing here"))
        assert not circles(root)
        assert not list(root.iter(f"{SVG_NS}path"))
        assert root.findtext(f"{SVG_NS}title") == "Nothing here"

    def test_null_geometries_skipped(self) -> None:
        root = parse_svg(render_svg([feature(None), point(1, 2), feature(None)]))
        assert len(circles(root)) == 1

    def test_feature_collection_and_bare_geometries_accepted(self) -> None:
        fc = {"type": "FeatureCollection", "features": [point(0, 0), point(1, 1)]}
        assert len(circles(parse_svg(render_svg(fc)))) == 2          # type: ignore[arg-type]
        bare: list[dict[str, Any]] = [{"type": "Point", "coordinates": [0, 0]},
                                      {"geometry": None}]
        assert len(circles(parse_svg(render_svg(bare)))) == 1
        assert len(circles(parse_svg(render_svg((point(0, 0),))))) == 1  # type: ignore[arg-type]

    def test_input_not_mutated(self) -> None:
        feats = [point(179.5, 0), point(-179.5, 0), donut()]
        before = repr(feats)
        render_svg(feats)
        render_png(feats, width=64, height=64, padding=4)
        assert repr(feats) == before


# ─────────────────────────────────────────────────────────────────────────────
# Antimeridian
# ─────────────────────────────────────────────────────────────────────────────

ANTIMERIDIAN = [point(179.5, 0), point(-179.5, 0), point(179.8, 10)]


class TestAntimeridian:
    def test_svg_points_stay_clustered(self) -> None:
        xs = [x for x, _ in circles(parse_svg(render_svg(ANTIMERIDIAN, width=800, height=600)))]
        assert max(xs) - min(xs) < 200                 # not at opposite edges of the canvas

    def test_svg_line_across_antimeridian_is_short(self) -> None:
        feats = [feature({"type": "LineString", "coordinates": [[179, 0], [-179, 0.5]]}),
                 point(180, 20)]
        root = parse_svg(render_svg(feats))
        group = svg_group(root, "qg-shapes")
        assert group is not None
        xs = [x for x, _ in path_points(next(group.iter(f"{SVG_NS}path")).get("d", ""))]
        assert max(xs) - min(xs) < 100

    def test_png_pixels_stay_clustered(self) -> None:
        img = png(ANTIMERIDIAN, width=400, height=300)
        teal = img.where(TEAL, tol=8)
        assert teal
        xs = [x for x, _ in teal]
        assert max(xs) - min(xs) < 100
        assert abs((max(xs) + min(xs)) / 2 - 200) < 20

    def test_buffer_ring_beyond_180_is_one_shape(self) -> None:
        zone = buffer_like(0, 179.9, 0.5)              # ring longitudes run past 180
        root = parse_svg(render_svg([point(179.9, 0)], overlays=[zone]))
        group = svg_group(root, "qg-overlays")
        assert group is not None
        xs = [x for x, _ in path_points(next(group.iter(f"{SVG_NS}path")).get("d", ""))]
        cx = circles(root)[0][0]
        assert min(xs) < cx < max(xs)                  # the point sits inside its zone

    def test_world_spanning_polygon_not_folded(self) -> None:
        band = feature({"type": "Polygon", "coordinates": [
            [[-180, -80], [180, -80], [180, -60], [-180, -60], [-180, -80]]]})
        root = parse_svg(render_svg([band, point(77.2, 28.6)], width=800, height=600))
        group = svg_group(root, "qg-shapes")
        assert group is not None
        xs = [x for x, _ in path_points(next(group.iter(f"{SVG_NS}path")).get("d", ""))]
        assert max(xs) - min(xs) > 600

    def test_world_view_keeps_ring_past_180_in_one_piece(self) -> None:
        # World-wide data is not unwrapped, but a buffer ring drawn past 180°
        # must not be torn into a stripe across the whole map.
        zone = buffer_like(0, 179.9, 0.5)
        root = parse_svg(render_svg([point(0, 0), point(179.9, 0)], overlays=[zone]))
        group = svg_group(root, "qg-overlays")
        assert group is not None
        xs = [x for x, _ in path_points(next(group.iter(f"{SVG_NS}path")).get("d", ""))]
        assert max(xs) - min(xs) < 20
        cx = max(x for x, _ in circles(root))          # the point at 179.9°
        assert min(xs) < cx < max(xs)

    def test_ring_from_0_to_360_is_a_world_box(self) -> None:
        box = feature({"type": "Polygon", "coordinates": [
            [[0, -60], [360, -60], [360, 60], [0, 60], [0, -60]]]})
        root = parse_svg(render_svg([box], width=800, height=600))
        group = svg_group(root, "qg-shapes")
        assert group is not None
        xs = [x for x, _ in path_points(next(group.iter(f"{SVG_NS}path")).get("d", ""))]
        assert max(xs) - min(xs) > 700                 # not folded into a vertical line

    def test_world_view_polygon_and_hole_move_together(self) -> None:
        # Outer ring 170°..200°, hole 185°..190° (wholly past 180°): in a world
        # view the polygon stays in one piece with its hole inside it.
        poly = feature({"type": "Polygon", "coordinates": [
            [[170, -20], [200, -20], [200, 20], [170, 20], [170, -20]],
            [[185, -5], [190, -5], [190, 5], [185, 5], [185, -5]]]})
        feats = [poly, point(-100, 0), point(0, 0), point(100, 0)]
        root = parse_svg(render_svg(feats, width=800, height=600))
        group = svg_group(root, "qg-shapes")
        assert group is not None
        d = next(group.iter(f"{SVG_NS}path")).get("d", "")
        outer, hole = (path_points("M" + part) for part in d.split("M")[1:])
        ox, hx = [x for x, _ in outer], [x for x, _ in hole]
        assert max(ox) - min(ox) < 200                 # not torn across the map
        assert min(ox) < min(hx) < max(hx) < max(ox)

    def test_shape_wholly_past_180_joins_its_neighbours(self) -> None:
        # A line given as 200°..210° is the same place as -160°..-150°.
        line = feature({"type": "LineString", "coordinates": [[200, 0], [210, 0]]})
        root = parse_svg(render_svg([line, point(-155, 0), point(0, 0), point(100, 0)]))
        group = svg_group(root, "qg-shapes")
        assert group is not None
        xs = [x for x, _ in path_points(next(group.iter(f"{SVG_NS}path")).get("d", ""))]
        cx = min(x for x, _ in circles(root))          # the point at -155°
        assert min(xs) < cx < max(xs)


# ─────────────────────────────────────────────────────────────────────────────
# PNG output
# ─────────────────────────────────────────────────────────────────────────────

class TestPngFormat:
    def test_signature_ihdr_and_crcs(self) -> None:
        data = render_png([point(0, 0)], width=320, height=200)
        assert data.startswith(PNG_SIGNATURE)
        img = PNG(data)                                # verifies every chunk CRC
        assert (img.width, img.height) == (320, 200)
        assert (img.depth, img.color_type) == (8, 6)
        assert (img.compression, img.filter, img.interlace) == (0, 0, 0)
        assert img.filters == {0}

    def test_chunk_order(self) -> None:
        kinds = [k for k, _ in png_chunks(render_png([point(0, 0)], title="T"))]
        assert kinds[0] == b"IHDR" and kinds[-1] == b"IEND"
        assert b"IDAT" in kinds
        assert kinds.index(b"IDAT") > kinds.index(b"IHDR")

    def test_metadata(self) -> None:
        img = png([point(0, 0)], title="Tokyo 東京")
        software = img.chunk(b"tEXt")
        assert software is not None and software.startswith(b"Software\x00QOREgeo ")
        itxt = img.chunk(b"iTXt")
        assert itxt is not None
        keyword, rest = itxt.split(b"\x00", 1)
        assert keyword == b"Title"
        assert rest[4:].decode("utf-8") == "Tokyo 東京"

    def test_opaque_and_background(self) -> None:
        img = png([point(0, 0)], width=100, height=80, background="#123456",
                  grid=False, scale_bar=False)
        assert img.px(0, 0) == (0x12, 0x34, 0x56, 255)
        assert img.px(99, 40) == (0x12, 0x34, 0x56, 255)
        assert all(c[3] == 255 for _, _, c in img.all_pixels())

    def test_short_hex_background(self) -> None:
        img = png([], width=32, height=32, padding=0, background="#f00")
        assert img.px(5, 5) == (255, 0, 0, 255)

    @pytest.mark.parametrize("size", [(16, 16), (8192, 16), (16, 300)])
    def test_extreme_sizes(self, size: tuple[int, int]) -> None:
        w, h = size
        img = png([point(0, 0), point(1, 1)], width=w, height=h, padding=0, title="t")
        assert (img.width, img.height) == (w, h)

    def test_deterministic(self) -> None:
        feats = [point(77.2, 28.6), donut()]
        assert render_png(feats, title="x") == render_png(feats, title="x")


class TestPngPixels:
    def test_point_is_teal_with_white_outline(self) -> None:
        img = png([point(77.2, 28.6)], width=200, height=100, background="#000000",
                  point_radius=6, grid=False, scale_bar=False)
        assert img.px(100, 50) == TEAL                 # marker centre
        assert img.px(97, 50) == TEAL
        assert close(img.px(106, 50), WHITE, tol=40)   # the outline straddles r = 6
        assert img.px(110, 50) == (0, 0, 0, 255)       # outside the marker
        assert img.px(5, 5) == (0, 0, 0, 255)

    def test_point_radius_changes_marker_size(self) -> None:
        small = len(png([point(0, 0)], width=120, height=120, point_radius=2).where(TEAL, 30))
        large = len(png([point(0, 0)], width=120, height=120, point_radius=10).where(TEAL, 30))
        assert large > small * 10

    def test_polygon_fill_hole_and_outline(self) -> None:
        img = png([donut()], width=800, height=600, grid=False, scale_bar=False)
        assert img.px(400, 300) == WHITE               # inside the hole: untouched
        interior = img.px(550, 300)                    # between the hole and the outer ring
        assert close(interior, (207, 247, 239, 255), tol=2)   # 18 % teal over white
        assert img.px(20, 300) == WHITE                # outside the polygon
        # The outer ring's left edge is stroked in solid teal somewhere along row 300.
        row = [img.px(x, 300) for x in range(0, 300)]
        assert TEAL in row

    def test_polygon_rings_filled_together(self) -> None:
        # Hole given with the same winding as the outer ring: even-odd still cuts it out.
        same_winding = feature({"type": "Polygon", "coordinates": [square(10), square(2)]})
        img = png([same_winding], width=800, height=600, grid=False, scale_bar=False)
        assert img.px(400, 300) == WHITE
        assert img.px(550, 300) != WHITE

    def test_line_pixels(self) -> None:
        line = feature({"type": "LineString", "coordinates": [[-10, 0], [10, 0]]})
        img = png([line], width=800, height=600, grid=False, scale_bar=False)
        assert img.px(400, 300) == TEAL and img.px(400, 299) == TEAL   # 2 px wide
        assert img.px(400, 303) == WHITE and img.px(400, 296) == WHITE
        assert img.px(400, 100) == WHITE

    def test_overlay_dashed_outline(self) -> None:
        dashed = {"type": "LineString", "coordinates": [[-10, 0], [10, 0]]}
        img = png([], width=800, height=600, overlays=[dashed])
        row = [close(img.px(x, 300), GOLD, tol=10) for x in range(800)]
        runs: list[int] = []
        gaps: list[int] = []
        x = 0
        while x < 800:
            j = x
            while j < 800 and row[j] == row[x]:
                j += 1
            (runs if row[x] else gaps).append(j - x)
            x = j
        inner_gaps = gaps[1:-1]
        assert len(runs) > 50                          # dashed, not one solid line
        assert all(4 <= r <= 8 for r in runs[1:-1]), runs
        assert all(2 <= g <= 6 for g in inner_gaps), inner_gaps

    def test_overlay_polygon_gold_outline_and_faint_fill(self) -> None:
        zone = {"type": "Polygon", "coordinates": [square(10)]}
        img = png([point(30, 0)], width=800, height=600, overlays=[zone])
        assert img.where(GOLD, tol=10)                 # gold outline present
        inside = img.px(300, 300)
        assert inside != WHITE and inside[2] < 250     # faintly tinted, still light
        assert min(inside[:3]) > 200

    def test_geometry_collection_drawn(self) -> None:
        gc = feature({"type": "GeometryCollection", "geometries": [
            {"type": "Point", "coordinates": [-5, 0]},
            {"type": "Polygon", "coordinates": [square(1, 5, 0)]},
        ]})
        img = png([gc], width=400, height=300)
        teal_xs = [x for x, _ in img.where(TEAL, tol=8)]
        assert min(teal_xs) < 100 and max(teal_xs) > 300   # both members drawn

    def test_marker_clipped_at_border(self) -> None:
        feats = [point(0, 0), point(0.001, 0.001)]
        img = png(feats, width=16, height=16, padding=0, point_radius=50)
        assert img.px(8, 8) == TEAL                     # huge marker covers the canvas

    def test_clipped_marker_matches_unclipped(self) -> None:
        # The border path (clipped slices) draws exactly the pixels of the fast path.
        full = _Canvas(300, 300, (255, 255, 255))
        full.markers([(150.2, 150.7)], 50.0, 1.5)
        for w, h in [(170, 300), (300, 160), (120, 120)]:
            cut = _Canvas(w, h, (255, 255, 255))
            cut.markers([(150.2, 150.7)], 50.0, 1.5)
            for y in range(h):
                assert cut.buf[y * w * 4:(y + 1) * w * 4] == full.buf[y * 1200:y * 1200 + w * 4]

    def test_many_markers_on_the_border_are_fast(self) -> None:
        # Large markers whose centres are all within a radius of the edge.
        # (Clipping them pixel by pixel took over 3 s; slices take ~0.3 s.)
        edge = [point(12 * i / 600, lat) for i in range(600) for lat in (0, 8)]
        edge += [point(lng, 8 * i / 400) for i in range(400) for lng in (0, 12)]
        start = time.perf_counter()
        data = render_png(edge, width=1200, height=800, padding=0, point_radius=50,
                          grid=False, scale_bar=False)
        assert time.perf_counter() - start < 2.0
        img = PNG(data)
        assert img.px(600, 400) == WHITE
        assert any(img.px(x, y) == TEAL for y in range(50) for x in range(0, 1200, 4))

    def test_title_on_short_canvas(self) -> None:
        img = png([point(0, 0), point(1, 1)], width=200, height=40, padding=0, title="Hi")
        text_rows = {y for x, y in img.where(DARK_TEXT)}
        assert text_rows and min(text_rows) >= 2 and max(text_rows) <= 8   # 5x7 at 1x
        assert img.where(TEAL, tol=8)                   # the markers are on the canvas

    def test_large_canvas_has_no_full_size_copies(self) -> None:
        # The canvas is one bytearray and the PNG rows are compressed in
        # batches, so peak memory stays close to a single canvas.
        size = 2048
        render_png([point(0, 0)], width=64, height=64)        # warm caches
        already_tracing = tracemalloc.is_tracing()
        if not already_tracing:
            tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            before = tracemalloc.get_traced_memory()[0]
            data = render_png([point(0, 0), point(1, 1)], width=size, height=size)
            peak = tracemalloc.get_traced_memory()[1] - before
        finally:
            if not already_tracing:
                tracemalloc.stop()
        assert peak < 1.5 * size * size * 4
        img = PNG(data)
        assert (img.width, img.height) == (size, size) and img.px(5, 5) == WHITE


class TestPngText:
    def test_title_drawn_in_band(self) -> None:
        with_title = png([point(0, 0)], width=400, height=300, title="Hello World")
        without = png([point(0, 0)], width=400, height=300, grid=False, scale_bar=False)
        band = [(x, y) for y in range(28) for x in range(400)]
        assert any(with_title.px(x, y) == DARK_TEXT for x, y in band)
        assert all(without.px(x, y) == WHITE for x, y in band if y < 20)

    def test_light_title_on_dark_background(self) -> None:
        img = png([point(0, 0)], title="Night", background="#0f111a")
        assert any(img.px(x, y) == LIGHT_TEXT for y in range(28) for x in range(200))

    def test_non_ascii_title(self) -> None:
        img = png([point(0, 0)], width=400, height=300, title="東京 Zürich")
        assert any(img.px(x, y) == DARK_TEXT for y in range(28) for x in range(400))

    def test_long_title_truncated_inside_canvas(self) -> None:
        img = png([point(0, 0)], width=200, height=120, title="W" * 500)
        text_xs = [x for y in range(28) for x in range(200) if img.px(x, y) == DARK_TEXT]
        assert text_xs and max(text_xs) < 200 - 8

    def test_credit_drawn(self) -> None:
        img = png([], width=400, height=300)
        credit = img.where(CREDIT)
        assert credit and min(x for x, _ in credit) > 300 and min(y for _, y in credit) > 270

    def test_empty_input_blank_canvas(self) -> None:
        img = png([], width=64, height=32, padding=4)    # too small for the credit
        assert all(c == WHITE for _, _, c in img.all_pixels())


class TestFont:
    def test_covers_printable_ascii(self) -> None:
        assert len(_FONT) == 95 * 5
        glyphs = [_glyph(chr(c)) for c in range(0x20, 0x7F)]
        assert glyphs[0] == bytes(5)                   # space is blank
        assert all(any(g) for g in glyphs[1:])         # every other glyph has pixels
        assert len(set(glyphs)) == 95                  # and they are all different
        assert all(b < 0x80 for g in glyphs for b in g)   # 7 rows

    def test_fallback_glyph(self) -> None:
        assert _glyph("é") == _glyph("?") == _glyph(chr(0x1F600))

    def test_ascii_folding(self) -> None:
        assert _ascii("Zürich") == "Zurich"
        assert _ascii("São Paulo") == "Sao Paulo"
        assert _ascii("東京") == "??"
        assert _ascii("a\tb\nc") == "a b c"
        assert _ascii("Delhi — “north”") == 'Delhi - "north"'
        assert _ascii("Łódź · Tromsø · Gießen · Æsir") == "Lodz . Tromso . Giessen . AEsir"
        assert _ascii("40°26′ «x»") == "40?26' \"x\""


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────

RENDERERS = [render_svg, render_png]


@pytest.mark.parametrize("fn", RENDERERS)
class TestValidation:
    @pytest.mark.parametrize("bad", [15, 8193, 0, -1, 800.0, "800", True, None])
    def test_width_and_height(self, fn: Any, bad: Any) -> None:
        with pytest.raises(InvalidArgumentError) as exc:
            fn([point(0, 0)], width=bad)
        assert exc.value.name == "width"
        with pytest.raises(InvalidArgumentError) as exc:
            fn([point(0, 0)], height=bad)
        assert exc.value.name == "height"

    @pytest.mark.parametrize("bad", [-1, 1.5, True, "24", 300, 1000])
    def test_padding(self, fn: Any, bad: Any) -> None:
        with pytest.raises(InvalidArgumentError) as exc:
            fn([point(0, 0)], width=800, height=600, padding=bad)
        assert exc.value.name == "padding"
        assert "half" in str(exc.value)

    @pytest.mark.parametrize("size", [16, 40, 48])
    def test_default_padding_on_small_image_suggests_a_fitting_one(self, fn: Any,
                                                                   size: int) -> None:
        with pytest.raises(InvalidArgumentError) as exc:
            fn([point(0, 0)], width=size, height=size)       # default padding=24
        assert exc.value.name == "padding"
        suggested = re.search(r"padding=(\d+)\)", str(exc.value))
        assert suggested is not None
        fn([point(0, 0)], width=size, height=size, padding=int(suggested.group(1)))

    @pytest.mark.parametrize("ok", [0, 299])
    def test_padding_limits_accepted(self, fn: Any, ok: int) -> None:
        fn([point(0, 0)], width=800, height=600, padding=ok)

    @pytest.mark.parametrize("bad", [0.4, 50.1, 0, -4, math.nan, math.inf, "4", True, None])
    def test_point_radius(self, fn: Any, bad: Any) -> None:
        with pytest.raises(InvalidArgumentError) as exc:
            fn([point(0, 0)], point_radius=bad)
        assert exc.value.name == "point_radius"

    @pytest.mark.parametrize("ok", [0.5, 50, 4])
    def test_point_radius_limits_accepted(self, fn: Any, ok: float) -> None:
        fn([point(0, 0)], width=120, height=120, point_radius=ok)

    @pytest.mark.parametrize("bad", ["white", "#ffff", "#gggggg", "ffffff", "#fffffff",
                                     0xFFFFFF, None, "#ffffff "])
    def test_background(self, fn: Any, bad: Any) -> None:
        with pytest.raises(InvalidArgumentError) as exc:
            fn([point(0, 0)], background=bad)
        assert exc.value.name == "background"
        assert "#rrggbb" in str(exc.value)

    @pytest.mark.parametrize(("bad", "name"), [
        (42, "features"),
        ("points", "features"),
        ([42], "features[0]"),
        ([{"type": "Blob"}], "features[0]"),
        ([feature({"type": "Point", "coordinates": [0, 100]})], "features[0]['geometry']"),
        ([feature({"type": "Polygon", "coordinates": [[[0, 0], [1, 1]]]})],
         "features[0]['geometry']"),
        ([{"type": "FeatureCollection", "features": 3}], "features[0]['features']"),
    ])
    def test_bad_features(self, fn: Any, bad: Any, name: str) -> None:
        with pytest.raises(InvalidArgumentError) as exc:
            fn(bad)
        assert exc.value.name == name
        assert "Fix it" in str(exc.value) and fn.__name__ in str(exc.value)

    @pytest.mark.parametrize("bad", [[42], [{"type": "Polygon", "coordinates": "x"}], "zone"])
    def test_bad_overlays(self, fn: Any, bad: Any) -> None:
        with pytest.raises(InvalidArgumentError) as exc:
            fn([point(0, 0)], overlays=bad)
        assert exc.value.name.startswith("overlays")
        assert "buffer" in str(exc.value)

    def test_errors_are_value_errors(self, fn: Any) -> None:
        with pytest.raises(ValueError):
            fn([point(0, 0)], width=1)
        with pytest.raises(QOREgeoError):
            fn([point(0, 0)], background="red")

    def test_huge_bad_geometry_is_summarised(self, fn: Any) -> None:
        ring = [[i * 0.001, 0.0] for i in range(2000)] + [[0.0, 95.0]]
        with pytest.raises(InvalidArgumentError) as exc:
            fn([feature({"type": "Polygon", "coordinates": [ring]})])
        assert len(str(exc.value)) < 1000

    @pytest.mark.parametrize(("name", "value"), [
        ("width", 10 ** 5000),          # beyond Python's int-to-str digit limit
        ("height", -(10 ** 5000)),
        ("padding", 10 ** 5000),
        ("point_radius", 10 ** 400),    # too large for a float
        ("point_radius", -(10 ** 400)),
        ("background", "#" + "f" * 100_000),
        ("title", 10 ** 5000),
    ], ids=["width", "height", "padding", "radius-big", "radius-neg", "background", "title"])
    def test_huge_option_values_are_summarised(self, fn: Any, name: str, value: Any) -> None:
        with pytest.raises(InvalidArgumentError) as exc:
            fn([point(0, 0)], **{name: value})
        assert exc.value.name == name
        assert len(str(exc.value)) < 1000

    def test_huge_number_in_coordinates_is_summarised(self, fn: Any) -> None:
        with pytest.raises(InvalidArgumentError) as exc:
            fn([feature({"type": "LineString", "coordinates": [[0, 0], [10 ** 5000, 0]]})])
        assert exc.value.name == "features[0]['geometry']"
        assert len(str(exc.value)) < 1000


# ─────────────────────────────────────────────────────────────────────────────
# Performance & packaging
# ─────────────────────────────────────────────────────────────────────────────

class TestPerformance:
    def test_20k_points_png_under_3_seconds(self, tmp_path: Path) -> None:
        # Deterministic pseudo-random scatter (no `random` state shared with other tests).
        feats = [point(77.0 + ((i * 7919) % 10007) / 10007, 28.0 + ((i * 104729) % 10009) / 10009)
                 for i in range(20_000)]
        feats += [
            feature({"type": "Polygon", "coordinates": [circle_ring(77.5, 28.5, 0.45, 400),
                                                         circle_ring(77.5, 28.5, 0.2, 200)]}),
            feature({"type": "Polygon", "coordinates": [circle_ring(77.2, 28.3, 0.3, 500)]}),
            feature({"type": "MultiPolygon", "coordinates": [[square(0.1, 77.8, 28.8)],
                                                              [square(0.1, 77.1, 28.9)]]}),
        ]
        start = time.perf_counter()
        data = render_png(feats, width=1200, height=800, title="20k points",
                          overlays=[buffer_like(28.5, 77.5, 0.3)])
        elapsed = time.perf_counter() - start
        assert elapsed < 3.0, f"render_png took {elapsed:.2f}s"
        out = tmp_path / "big.png"
        out.write_bytes(data)
        assert PNG(out.read_bytes()).width == 1200

    def test_svg_written_to_file(self, tmp_path: Path) -> None:
        out = tmp_path / "map.svg"
        out.write_text(render_svg([point(77.2, 28.6)], title="Délhi"), encoding="utf-8")
        root = ET.parse(out).getroot()
        assert root.findtext(f"{SVG_NS}title") == "Délhi"


def test_render_module_is_stdlib_only() -> None:
    import qoregeo.render as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    assert names
    assert names <= set(sys.stdlib_module_names) | {"__future__"}, names
