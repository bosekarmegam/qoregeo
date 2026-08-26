"""
Map output — interactive HTML and static SVG/PNG.

The HTML maps cannot be executed here, so the tests assert on the generated
document: that every placeholder is filled, the data is embedded, the options
take effect, and the markup is well formed. The PNG tests go further and
decode the file back, since a corrupt PNG looks fine as a byte count.
"""

from __future__ import annotations

import json
import re
import struct
import zlib

import pytest

from qoregeo.exceptions import EmptyDatasetError
from qoregeo.map_builder import (
    AUTO_CLUSTER_THRESHOLD,
    BASEMAPS,
    _basemap_config,
    _quantile_breaks,
    _sample_ramp,
    build_choropleth,
    build_heatmap,
    build_map,
)
from qoregeo.static_map import (
    THEMES,
    Canvas,
    Projector,
    bounds_of,
    build_png,
    build_svg,
)

from .conftest import CHENNAI, DELHI, MUMBAI, point_feature, polygon_feature

MIXED = [
    point_feature(*DELHI, name="Delhi", region="North", pop=32.9),
    point_feature(*MUMBAI, name="Mumbai", region="West", pop=20.7),
    point_feature(*CHENNAI, name="Chennai", region="South", pop=11.3),
    polygon_feature([[74, 20], [80, 20], [80, 26], [74, 26]], name="Zone", pop=5.0),
    {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[77.2, 28.6], [72.9, 19.1]]},
        "properties": {"name": "Corridor"},
    },
]


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


class TestInteractiveMap:
    def test_writes_a_document(self, tmp_path):
        path = str(tmp_path / "m.html")
        build_map(MIXED, path, quiet=True)
        html = read(path)
        assert html.startswith("<!DOCTYPE html>")
        assert html.rstrip().endswith("</html>")

    def test_no_placeholder_survives(self, tmp_path):
        path = str(tmp_path / "m.html")
        build_map(MIXED, path, quiet=True)
        assert "/*__" not in read(path)

    def test_embeds_the_data(self, tmp_path):
        path = str(tmp_path / "m.html")
        build_map(MIXED, path, quiet=True)
        payload = re.search(r"var DATA = (\{.*?\});\n", read(path), re.S)
        assert len(json.loads(payload.group(1))["features"]) == 5

    def test_uses_geojson_so_polygons_render(self, tmp_path):
        path = str(tmp_path / "m.html")
        build_map(MIXED, path, quiet=True)
        assert "L.geoJSON" in read(path)

    def test_title_is_escaped(self, tmp_path):
        path = str(tmp_path / "m.html")
        build_map(MIXED, path, title="<script>alert(1)</script>", quiet=True)
        assert "<script>alert(1)</script>" not in read(path)
        assert "&lt;script&gt;" in read(path)

    def test_colour_by_builds_a_legend(self, tmp_path):
        path = str(tmp_path / "m.html")
        build_map(MIXED, path, colour_by="region", quiet=True)
        html = read(path)
        assert 'id="legend"' in html
        assert "North" in html

    def test_no_legend_without_colour_by(self, tmp_path):
        path = str(tmp_path / "m.html")
        build_map(MIXED, path, quiet=True)
        assert 'id="legend"' not in read(path)

    def test_search_box_can_be_disabled(self, tmp_path):
        path = str(tmp_path / "m.html")
        build_map(MIXED, path, search=False, quiet=True)
        assert 'id="q-search"' not in read(path)

    def test_clustering_is_off_for_small_data(self, tmp_path):
        path = str(tmp_path / "m.html")
        build_map(MIXED, path, quiet=True)
        assert "var CLUSTER = false;" in read(path)

    def test_clustering_turns_on_automatically(self, tmp_path):
        many = [point_feature(20 + i * 0.001, 77 + i * 0.001) for i in range(AUTO_CLUSTER_THRESHOLD + 1)]
        path = str(tmp_path / "big.html")
        build_map(many, path, quiet=True)
        html = read(path)
        assert "var CLUSTER = true;" in html
        assert "markercluster" in html

    def test_clustering_can_be_forced(self, tmp_path):
        path = str(tmp_path / "m.html")
        build_map(MIXED, path, cluster=True, quiet=True)
        assert "var CLUSTER = true;" in read(path)

    def test_requested_basemap_comes_first(self, tmp_path):
        path = str(tmp_path / "m.html")
        build_map(MIXED, path, basemap="satellite", quiet=True)
        specs = json.loads(re.search(r"qoreBasemaps\(map, (\[.*?\])\);", read(path), re.S).group(1))
        assert specs[0]["name"] == "satellite"

    def test_all_basemaps_are_offered(self, tmp_path):
        path = str(tmp_path / "m.html")
        build_map(MIXED, path, quiet=True)
        specs = json.loads(re.search(r"qoreBasemaps\(map, (\[.*?\])\);", read(path), re.S).group(1))
        assert len(specs) == len(BASEMAPS)

    def test_unknown_basemap_falls_back(self):
        assert _basemap_config("neon")[0]["name"] == "dark"

    def test_tooltip_and_popup_options(self, tmp_path):
        path = str(tmp_path / "m.html")
        build_map(MIXED, path, tooltip_field="name", popup_fields=["name"], quiet=True)
        html = read(path)
        assert 'var TOOLTIP_FIELD = "name";' in html
        assert 'var POPUP_FIELDS = ["name"];' in html

    def test_has_a_cdn_fallback(self, tmp_path):
        path = str(tmp_path / "m.html")
        build_map(MIXED, path, quiet=True)
        html = read(path)
        assert "unpkg.com" in html
        assert "jsdelivr.net" in html

    def test_reports_failure_rather_than_a_blank_page(self, tmp_path):
        path = str(tmp_path / "m.html")
        build_map(MIXED, path, quiet=True)
        assert "qoreFail" in read(path)

    def test_returns_the_path(self, tmp_path):
        path = str(tmp_path / "m.html")
        assert build_map(MIXED, path, quiet=True) == path


class TestHeatmap:
    def test_writes_a_document(self, tmp_path):
        path = str(tmp_path / "h.html")
        build_heatmap(MIXED, path, quiet=True)
        assert "L.heatLayer" in read(path)

    def test_every_feature_becomes_a_point(self, tmp_path):
        path = str(tmp_path / "h.html")
        build_heatmap(MIXED, path, quiet=True)
        points = json.loads(re.search(r"var POINTS = (\[.*?\]);", read(path), re.S).group(1))
        assert len(points) == 5          # polygons and lines contribute their centroid

    def test_intensity_is_normalised(self, tmp_path):
        path = str(tmp_path / "h.html")
        build_heatmap(MIXED, path, intensity_col="pop", quiet=True)
        points = json.loads(re.search(r"var POINTS = (\[.*?\]);", read(path), re.S).group(1))
        assert max(p[2] for p in points) == 1.0
        assert min(p[2] for p in points) > 0

    def test_missing_intensity_defaults_to_one(self, tmp_path):
        path = str(tmp_path / "h.html")
        build_heatmap([point_feature(*DELHI)], path, intensity_col="absent", quiet=True)
        points = json.loads(re.search(r"var POINTS = (\[.*?\]);", read(path), re.S).group(1))
        assert points[0][2] == 1.0

    def test_radius_and_blur_are_applied(self, tmp_path):
        path = str(tmp_path / "h.html")
        build_heatmap(MIXED, path, radius=40, blur=30, quiet=True)
        assert "radius: 40, blur: 30" in read(path)

    def test_explains_a_missing_plugin(self, tmp_path):
        path = str(tmp_path / "h.html")
        build_heatmap(MIXED, path, quiet=True)
        assert "leaflet.heat plugin failed to load" in read(path)


class TestChoropleth:
    def test_writes_a_document(self, tmp_path):
        path = str(tmp_path / "c.html")
        build_choropleth(MIXED, path, value_col="pop", quiet=True)
        assert "var VALUE_COL" in read(path)

    def test_legend_rows_match_the_classes(self, tmp_path):
        path = str(tmp_path / "c.html")
        build_choropleth(MIXED, path, value_col="pop", bins=3, quiet=True)
        html = read(path)
        breaks = json.loads(re.search(r"var BREAKS = (\[.*?\]);", html).group(1))
        assert html.count('class="row"') == len(breaks)

    def test_quantile_breaks_ascend(self):
        breaks = _quantile_breaks([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 5)
        assert breaks == sorted(breaks)
        assert len(set(breaks)) == len(breaks)

    def test_tied_values_collapse_classes(self):
        # Half the rows sharing one value must not create an empty "5 – 5" class.
        assert _quantile_breaks([5, 5, 5, 5, 7.4, 11.3, 20.7], 5) == [5, 7.4]

    def test_uniform_data_yields_one_class(self):
        assert _quantile_breaks([5, 5, 5], 5) == [5]

    def test_no_values_still_yields_a_break(self):
        assert _quantile_breaks([], 5) == [0.0]

    def test_ramp_sampling_keeps_both_ends(self):
        from qoregeo.map_builder import SEQUENTIAL_RAMP

        sampled = _sample_ramp(SEQUENTIAL_RAMP, 4)
        assert sampled[0] == SEQUENTIAL_RAMP[0]
        assert sampled[-1] == SEQUENTIAL_RAMP[-1]

    def test_ramp_sampling_is_bounded(self):
        from qoregeo.map_builder import SEQUENTIAL_RAMP

        assert len(_sample_ramp(SEQUENTIAL_RAMP, 99)) == len(SEQUENTIAL_RAMP)
        assert len(_sample_ramp(SEQUENTIAL_RAMP, 0)) == 1

    def test_bins_are_clamped(self, tmp_path):
        path = str(tmp_path / "c.html")
        build_choropleth(MIXED, path, value_col="pop", bins=99, quiet=True)
        breaks = json.loads(re.search(r"var BREAKS = (\[.*?\]);", read(path)).group(1))
        assert len(breaks) <= 7


class TestProjector:
    def test_projects_into_the_canvas(self):
        proj = Projector({"min_lat": 0, "max_lat": 10, "min_lng": 0, "max_lng": 10}, 800, 600)
        x, y = proj.project(5, 5)
        assert 0 <= x <= 800
        assert 0 <= y <= 600

    def test_north_is_up(self):
        proj = Projector({"min_lat": 0, "max_lat": 10, "min_lng": 0, "max_lng": 10}, 800, 600)
        assert proj.project(9, 5)[1] < proj.project(1, 5)[1]

    def test_east_is_right(self):
        proj = Projector({"min_lat": 0, "max_lat": 10, "min_lng": 0, "max_lng": 10}, 800, 600)
        assert proj.project(5, 9)[0] > proj.project(5, 1)[0]

    def test_degenerate_bounds_do_not_divide_by_zero(self):
        proj = Projector({"min_lat": 5, "max_lat": 5, "min_lng": 5, "max_lng": 5}, 400, 300)
        x, y = proj.project(5, 5)
        assert 0 <= x <= 400 and 0 <= y <= 300

    def test_extreme_latitude_is_clamped(self):
        proj = Projector({"min_lat": -89, "max_lat": 89, "min_lng": -180, "max_lng": 180},
                         800, 600)
        assert abs(proj.project(89.9, 0)[1]) < 10_000

    def test_bounds_of_mixed_geometry(self):
        box = bounds_of(MIXED)
        assert box["min_lat"] < box["max_lat"]

    def test_bounds_of_nothing_raises(self):
        with pytest.raises(EmptyDatasetError):
            bounds_of([])


class TestSVG:
    def test_is_valid_svg(self, tmp_path):
        svg = build_svg(MIXED, str(tmp_path / "m.svg"))
        assert svg.startswith("<svg")
        assert svg.rstrip().endswith("</svg>")

    def test_draws_every_geometry_kind(self, tmp_path):
        svg = build_svg(MIXED, str(tmp_path / "m.svg"))
        assert svg.count("<circle") == 3
        assert svg.count("<path") == 2

    def test_labels_are_optional(self, tmp_path):
        with_labels = build_svg(MIXED, str(tmp_path / "a.svg"), label_field="name")
        without = build_svg(MIXED, str(tmp_path / "b.svg"))
        assert with_labels.count("<text") > without.count("<text")

    def test_title_is_escaped(self, tmp_path):
        svg = build_svg(MIXED, str(tmp_path / "m.svg"), title="A & B <x>")
        assert "&amp;" in svg
        assert "<x>" not in svg

    def test_themes_differ(self, tmp_path):
        dark = build_svg(MIXED, str(tmp_path / "d.svg"), theme="dark")
        light = build_svg(MIXED, str(tmp_path / "l.svg"), theme="light")
        assert dark != light

    def test_unknown_theme_falls_back(self, tmp_path):
        assert build_svg(MIXED, str(tmp_path / "x.svg"), theme="neon")

    def test_writes_the_file(self, tmp_path):
        path = tmp_path / "m.svg"
        build_svg(MIXED, str(path))
        assert path.read_text(encoding="utf-8").startswith("<svg")

    def test_can_skip_writing(self):
        assert build_svg(MIXED).startswith("<svg")

    def test_viewbox_matches_the_size(self, tmp_path):
        svg = build_svg(MIXED, str(tmp_path / "m.svg"), width=640, height=480)
        assert 'viewBox="0 0 640 480"' in svg


class TestPNG:
    def decode(self, path):
        """Pull the raw scanlines back out — a size check proves nothing."""
        data = read_bytes(path)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"

        chunks = {}
        idat = b""
        offset = 8
        while offset < len(data):
            length = struct.unpack(">I", data[offset : offset + 4])[0]
            kind = data[offset + 4 : offset + 8]
            body = data[offset + 4 : offset + 8 + length]
            crc = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])[0]
            assert zlib.crc32(body) & 0xFFFFFFFF == crc, f"bad CRC on {kind!r}"
            if kind == b"IDAT":
                idat += data[offset + 8 : offset + 8 + length]
            chunks[kind] = body
            offset += 12 + length

        width, height, depth, colour = struct.unpack(">2I2B", data[16:26])
        return width, height, depth, colour, zlib.decompress(idat), chunks

    def test_produces_a_valid_png(self, tmp_path):
        path = build_png(MIXED, str(tmp_path / "m.png"), width=200, height=150)
        width, height, depth, colour, raw, chunks = self.decode(path)
        assert (width, height, depth, colour) == (200, 150, 8, 2)
        assert set(chunks) == {b"IHDR", b"IDAT", b"IEND"}

    def test_scanlines_are_complete(self, tmp_path):
        path = build_png(MIXED, str(tmp_path / "m.png"), width=120, height=90)
        width, height, _, _, raw, _ = self.decode(path)
        assert len(raw) == height * (1 + width * 3)

    def test_every_scanline_uses_filter_zero(self, tmp_path):
        path = build_png(MIXED, str(tmp_path / "m.png"), width=60, height=40)
        width, height, _, _, raw, _ = self.decode(path)
        stride = 1 + width * 3
        assert all(raw[row * stride] == 0 for row in range(height))

    def test_something_is_actually_drawn(self, tmp_path):
        path = build_png(MIXED, str(tmp_path / "m.png"), width=200, height=150)
        width, height, _, _, raw, _ = self.decode(path)
        background = bytes(THEMES["dark"]["background"])
        stride = 1 + width * 3
        drawn = sum(
            1
            for row in range(height)
            for col in range(width)
            if raw[row * stride + 1 + col * 3 : row * stride + 4 + col * 3] != background
        )
        assert drawn > 50

    def test_a_single_point_still_renders(self, tmp_path):
        path = build_png([point_feature(*DELHI)], str(tmp_path / "one.png"), width=80, height=60)
        assert self.decode(path)[0] == 80

    def test_theme_changes_the_background(self, tmp_path):
        dark = build_png(MIXED, str(tmp_path / "d.png"), width=40, height=30, theme="dark")
        light = build_png(MIXED, str(tmp_path / "l.png"), width=40, height=30, theme="light")
        assert read_bytes(dark) != read_bytes(light)

    def test_returns_the_path(self, tmp_path):
        path = str(tmp_path / "m.png")
        assert build_png(MIXED, path) == path


class TestCanvas:
    def test_starts_filled_with_the_background(self):
        canvas = Canvas(4, 3, (10, 20, 30))
        assert bytes(canvas.pixels[0:3]) == bytes((10, 20, 30))
        assert len(canvas.pixels) == 4 * 3 * 3

    def test_set_pixel(self):
        canvas = Canvas(4, 3, (0, 0, 0))
        canvas.set(1, 1, (255, 0, 0))
        assert bytes(canvas.pixels[(1 * 4 + 1) * 3 : (1 * 4 + 1) * 3 + 3]) == b"\xff\x00\x00"

    def test_out_of_bounds_is_ignored(self):
        canvas = Canvas(4, 3, (0, 0, 0))
        canvas.set(99, 99, (255, 255, 255))
        canvas.set(-1, -1, (255, 255, 255))
        assert set(canvas.pixels) == {0}

    def test_alpha_blends(self):
        canvas = Canvas(2, 2, (0, 0, 0))
        canvas.set(0, 0, (200, 200, 200), alpha=0.5)
        assert 90 <= canvas.pixels[0] <= 110

    def test_zero_alpha_does_nothing(self):
        canvas = Canvas(2, 2, (0, 0, 0))
        canvas.set(0, 0, (255, 255, 255), alpha=0.0)
        assert canvas.pixels[0] == 0

    def test_circle_fills(self):
        canvas = Canvas(21, 21, (0, 0, 0))
        canvas.circle(10, 10, 5, (255, 255, 255))
        assert canvas.pixels[(10 * 21 + 10) * 3] == 255
        assert canvas.pixels[0] == 0

    def test_line_draws_between_endpoints(self):
        canvas = Canvas(11, 11, (0, 0, 0))
        canvas.line(0, 5, 10, 5, (255, 255, 255))
        assert all(canvas.pixels[(5 * 11 + x) * 3] == 255 for x in range(11))

    def test_line_far_outside_terminates(self):
        # A projected point can land far off-canvas; the walk must be bounded.
        canvas = Canvas(10, 10, (0, 0, 0))
        canvas.line(-10_000_000, -10_000_000, 10_000_000, 10_000_000, (255, 255, 255))
        assert len(canvas.pixels) == 300

    def test_polygon_fills_its_interior(self):
        canvas = Canvas(21, 21, (0, 0, 0))
        canvas.polygon([(2, 2), (18, 2), (18, 18), (2, 18)], (255, 255, 255), alpha=1.0)
        assert canvas.pixels[(10 * 21 + 10) * 3] == 255
        assert canvas.pixels[0] == 0

    def test_polygon_needs_three_points(self):
        canvas = Canvas(5, 5, (0, 0, 0))
        canvas.polygon([(0, 0), (1, 1)], (255, 255, 255))
        assert set(canvas.pixels) == {0}


class TestEngineRendering:
    def test_map_returns_self(self, cities, tmp_path):
        assert cities.map(str(tmp_path / "m.html"), quiet=True) is cities

    def test_colour_by_us_spelling(self, cities, tmp_path):
        path = str(tmp_path / "m.html")
        cities.map(path, color_by="state", quiet=True)
        assert 'var COLOUR_BY = "state";' in read(path)

    def test_choropleth(self, cities, tmp_path):
        path = str(tmp_path / "c.html")
        assert cities.choropleth(path, value_col="population", quiet=True) is cities

    def test_choropleth_checks_the_column(self, cities, tmp_path):
        from qoregeo.exceptions import ColumnNotFoundError

        with pytest.raises(ColumnNotFoundError):
            cities.choropleth(str(tmp_path / "c.html"), value_col="nope", quiet=True)

    def test_svg_method(self, cities, tmp_path):
        path = tmp_path / "m.svg"
        assert cities.svg(str(path)) is cities
        assert path.read_text(encoding="utf-8").startswith("<svg")

    def test_png_method(self, cities, tmp_path):
        path = tmp_path / "m.png"
        assert cities.png(str(path)) is cities
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
