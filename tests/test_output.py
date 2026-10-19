"""
tests/test_output.py
====================
GeoEngine output features added in this release.

* ``map_html()`` / ``heatmap_html()`` — the page ``map()`` / ``heatmap()`` write
* the source-file guard: ``map()`` / ``heatmap()`` / ``image()`` never write
  over the CSV / GeoJSON the data was loaded from
* ``image()`` / ``to_png()`` / ``to_svg()`` static images
* ``_repr_html_()`` (Jupyter) and ``show()`` (local web server, faked here)
* ``save(csv_safe=…)`` formula escaping
* ``intersects()`` / ``filter_intersects()``

Offline and fast: files go to ``tmp_path``; ``show()`` never starts a server.

Run with:
    pytest tests/test_output.py -v
"""

from __future__ import annotations

import csv
import html
import json
import math
import os
import re
import shlex
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

import qoregeo.utils as qutils
from qoregeo import GeoEngine
from qoregeo.exceptions import InvalidArgumentError, InvalidBufferError, NoDataError
from qoregeo.map_builder import _tiles_config

pytestmark = pytest.mark.filterwarnings("ignore:QOREgeo. .* no heat:UserWarning")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SVG_NS = "{http://www.w3.org/2000/svg}"

_CONFIG_RE = re.compile(r"^[ \t]*var CONFIG = JSON\.parse\((.*)\);[ \t]*$", re.MULTILINE)

CITIES_CSV = (
    "name,lat,lng,population\n"
    "Delhi,28.6139,77.2090,32000000\n"
    "Mumbai,19.0760,72.8777,21000000\n"
    "Pune,18.5204,73.8567,7000000\n"
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers + fixtures
# ─────────────────────────────────────────────────────────────────────────────

def parse_config(page: str) -> dict[str, Any]:
    """The JSON config embedded as ``var CONFIG = JSON.parse("…");`` (exactly one)."""
    found = _CONFIG_RE.findall(page)
    assert len(found) == 1, "expected exactly one 'var CONFIG = JSON.parse(…);' line"
    return json.loads(json.loads(found[0]))


def without_config(page: str) -> str:
    return _CONFIG_RE.sub("CONFIG", page)


def square(x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]:
    """Axis-aligned Polygon with corners (x0, y0) / (x1, y1) in [lng, lat]."""
    ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
    return {"type": "Polygon", "coordinates": [ring]}


def point(lng: float, lat: float) -> dict[str, Any]:
    return {"type": "Point", "coordinates": [lng, lat]}


def line(*coords: tuple[float, float]) -> dict[str, Any]:
    return {"type": "LineString", "coordinates": [list(c) for c in coords]}


def feature(geometry: Any, **props: Any) -> dict[str, Any]:
    return {"type": "Feature", "geometry": geometry, "properties": props}


def can_symlink(tmp_path: Path) -> bool:
    if not hasattr(os, "symlink"):
        return False
    target = tmp_path / "_symlink_target"
    target.write_text("x", encoding="utf-8")
    try:
        os.symlink(target, tmp_path / "_symlink_probe")
    except (OSError, NotImplementedError):
        return False
    return True


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    path = tmp_path / "cities.csv"
    path.write_text(CITIES_CSV, encoding="utf-8")
    return path


@pytest.fixture
def geojson_file(tmp_path: Path) -> Path:
    path = tmp_path / "cities.geojson"
    data = {"type": "FeatureCollection", "features": [
        feature(point(77.2090, 28.6139), name="Delhi"),
        feature(point(72.8777, 19.0760), name="Mumbai"),
    ]}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def loaded(csv_file: Path) -> GeoEngine:
    return GeoEngine(verbose=False).load(csv_file, infer_types=True)


@pytest.fixture
def geo() -> GeoEngine:
    g = GeoEngine(verbose=False)
    g.add_point(28.6139, 77.2090, name="Delhi", population=32_000_000)
    g.add_point(19.0760, 72.8777, name="Mumbai", population=21_000_000)
    return g


# ─────────────────────────────────────────────────────────────────────────────
# map_html() / heatmap_html()
# ─────────────────────────────────────────────────────────────────────────────

class TestHtmlStrings:
    def test_map_html_is_the_page_map_writes(self, geo, tmp_path):
        out = tmp_path / "map.html"   # the name map_html() puts in its file notice
        geo.map(out, title="Cities", tiles="osm")
        assert out.read_text(encoding="utf-8") == geo.map_html(title="Cities", tiles="osm")

    def test_heatmap_html_is_the_page_heatmap_writes(self, geo, tmp_path):
        out = tmp_path / "map.html"
        geo.heatmap(out, title="Heat", intensity_col="population")
        assert out.read_text(encoding="utf-8") == geo.heatmap_html(title="Heat",
                                                                 intensity_col="population")

    @pytest.mark.parametrize("builder", ["map", "heatmap"])
    def test_only_the_file_notice_differs(self, geo, tmp_path, builder):
        out = tmp_path / "india cities.html"
        getattr(geo, builder)(out)
        written = out.read_text(encoding="utf-8")
        page = getattr(geo, builder + "_html")()
        assert without_config(written) == without_config(page)
        cfg_written, cfg_page = parse_config(written), parse_config(page)
        assert '"india cities.html"' in cfg_written.pop("fileNotice")
        assert "map.html" in cfg_page.pop("fileNotice")
        assert cfg_written == cfg_page

    def test_map_html_writes_nothing(self, geo, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        page = geo.map_html()
        assert page.startswith("<!DOCTYPE html>")
        assert list(tmp_path.iterdir()) == []

    def test_map_html_options(self, geo):
        page = geo.map_html(title="A <b> title", tiles="topo", zoom=5, center=(20, 70))
        assert "<title>A &lt;b&gt; title</title>" in page
        cfg = parse_config(page)
        assert cfg["tiles"]["name"] == "topo"
        assert cfg["view"] == {"center": [20.0, 70.0], "zoom": 5, "fitMaxZoom": 5}

    def test_map_html_overlays_single_dict(self, geo):
        zone = geo.buffer((28.6139, 77.2090), 10)
        cfg = parse_config(geo.map_html(overlays=zone))
        assert len(cfg["overlays"]) == 1

    def test_map_html_without_data(self):
        with pytest.raises(NoDataError):
            GeoEngine(verbose=False).map_html()
        with pytest.raises(NoDataError):
            GeoEngine(verbose=False).heatmap_html()

    def test_map_html_validates_like_map(self, geo):
        with pytest.raises(InvalidArgumentError):
            geo.map_html(tiles="no-such-tiles")
        with pytest.raises(InvalidArgumentError):
            geo.heatmap_html(radius=0)

    def test_proto_property_survives(self):
        g = GeoEngine(verbose=False).add_point(1, 2, __proto__="p", constructor="c")
        props = parse_config(g.map_html())["data"]["features"][0]["properties"]
        assert props == {"__proto__": "p", "constructor": "c"}

    def test_proto_column_from_csv_survives(self, tmp_path):
        path = tmp_path / "proto.csv"
        path.write_text("name,lat,lng,__proto__\nA,1,2,polluted\n", encoding="utf-8")
        g = GeoEngine(verbose=False).load(path)
        page = g.map_html()
        assert "JSON.parse(" in page
        props = parse_config(page)["data"]["features"][0]["properties"]
        assert props["__proto__"] == "polluted"


# ─────────────────────────────────────────────────────────────────────────────
# Source-file guard
# ─────────────────────────────────────────────────────────────────────────────

WRITERS = ["map", "heatmap", "image"]


def assert_refused(geo: GeoEngine, method: str, target: Any) -> None:
    with pytest.raises(InvalidArgumentError) as info:
        getattr(geo, method)(target)
    assert info.value.name == "output_path"
    assert "data file" in str(info.value)


class TestSourceFileGuard:
    @pytest.mark.parametrize("method", WRITERS)
    def test_same_path_refused(self, loaded, csv_file, method):
        assert_refused(loaded, method, csv_file)
        assert csv_file.read_text(encoding="utf-8") == CITIES_CSV

    @pytest.mark.parametrize("method", WRITERS)
    def test_same_path_as_str_refused(self, loaded, csv_file, method):
        assert_refused(loaded, method, str(csv_file))
        assert csv_file.read_text(encoding="utf-8") == CITIES_CSV

    @pytest.mark.parametrize("method", WRITERS)
    def test_relative_spelling_refused(self, loaded, csv_file, tmp_path, monkeypatch, method):
        monkeypatch.chdir(tmp_path)
        assert_refused(loaded, method, os.path.join(".", "cities.csv"))
        assert_refused(loaded, method, "cities.csv")
        assert csv_file.read_text(encoding="utf-8") == CITIES_CSV

    @pytest.mark.parametrize("method", WRITERS)
    def test_dot_dot_spelling_refused(self, loaded, csv_file, tmp_path, method):
        sub = tmp_path / "sub"
        sub.mkdir()
        assert_refused(loaded, method, os.path.join(str(sub), "..", "cities.csv"))
        assert csv_file.read_text(encoding="utf-8") == CITIES_CSV

    def test_loaded_with_relative_path_then_absolute_output(self, csv_file, tmp_path,
                                                            monkeypatch):
        monkeypatch.chdir(tmp_path)
        g = GeoEngine(verbose=False).load("cities.csv")
        assert_refused(g, "map", csv_file)
        assert csv_file.read_text(encoding="utf-8") == CITIES_CSV

    @pytest.mark.parametrize("method", WRITERS)
    def test_symlink_to_source_refused(self, loaded, csv_file, tmp_path, method):
        if not can_symlink(tmp_path):
            pytest.skip("symlinks are not available here")
        link = tmp_path / "alias.csv"
        os.symlink(csv_file, link)
        assert_refused(loaded, method, link)
        assert csv_file.read_text(encoding="utf-8") == CITIES_CSV
        assert link.is_symlink()

    @pytest.mark.parametrize("method", WRITERS)
    def test_geojson_source_refused(self, geojson_file, method):
        before = geojson_file.read_bytes()
        g = GeoEngine(verbose=False).load(geojson_file)
        assert_refused(g, method, geojson_file)
        assert geojson_file.read_bytes() == before

    @pytest.mark.parametrize("method, name", [("map", "out.html"), ("heatmap", "heat.html"),
                                              ("image", "out.png"), ("image", "out.svg")])
    def test_other_file_is_written(self, loaded, csv_file, tmp_path, method, name):
        out = tmp_path / name
        assert getattr(loaded, method)(out) is loaded
        assert out.is_file() and out.stat().st_size > 0
        assert csv_file.read_text(encoding="utf-8") == CITIES_CSV

    @pytest.mark.parametrize("make_clone", [
        lambda g: g.filter("name", "Delhi"),
        lambda g: g.filter_by_radius(28.6, 77.2, 50),
        lambda g: g.sort_by("population"),
        lambda g: g.head(1),
        lambda g: g.where(lambda p: True),
        lambda g: g.filter_intersects(square(70, 10, 80, 30)),
    ])
    def test_filtered_clones_inherit_guard(self, loaded, csv_file, make_clone):
        clone = make_clone(loaded)
        assert clone is not loaded
        for method in WRITERS:
            assert_refused(clone, method, csv_file)
        assert csv_file.read_text(encoding="utf-8") == CITIES_CSV

    def test_load_data_has_no_guard(self, loaded, csv_file):
        g = GeoEngine(verbose=False).load_data(loaded.get_geojson())
        assert g.filter("name", "Delhi").map(csv_file) is not None   # no file to protect
        assert csv_file.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
        with pytest.raises(InvalidArgumentError) as info:
            g.image(csv_file)
        assert "data file" not in str(info.value)   # rejected for its extension only

    def test_load_data_resets_guard_of_reused_engine(self, csv_file, tmp_path):
        g = GeoEngine(verbose=False).load(csv_file)
        g.load_data([feature(point(1, 2))])
        out = tmp_path / "cities.svg"
        g.image(out)
        assert out.is_file()

    @pytest.mark.parametrize("method", WRITERS)
    def test_guard_survives_chdir(self, tmp_path, monkeypatch, method):
        # The source path is resolved at load() time, so os.chdir() (or %cd in a
        # notebook) can't turn the guard off.
        (tmp_path / "data").mkdir()
        (tmp_path / "reports").mkdir()
        src = tmp_path / "data" / "cities.csv"
        src.write_text(CITIES_CSV, encoding="utf-8")
        monkeypatch.chdir(tmp_path / "data")
        g = GeoEngine(verbose=False).load("cities.csv")     # a relative path
        monkeypatch.chdir(tmp_path / "reports")
        assert_refused(g, method, src)
        assert_refused(g, method, os.path.join("..", "data", "cities.csv"))
        assert_refused(g.filter("name", "Delhi"), method, src)
        assert src.read_text(encoding="utf-8") == CITIES_CSV

    def test_same_name_in_another_directory_after_chdir_is_allowed(self, tmp_path, monkeypatch):
        (tmp_path / "data").mkdir()
        (tmp_path / "reports").mkdir()
        src = tmp_path / "data" / "cities.csv"
        src.write_text(CITIES_CSV, encoding="utf-8")
        monkeypatch.chdir(tmp_path / "data")
        g = GeoEngine(verbose=False).load("cities.csv")
        monkeypatch.chdir(tmp_path / "reports")
        g.map("cities.csv")      # reports/cities.csv is a different file
        assert (tmp_path / "reports" / "cities.csv").read_text(encoding="utf-8").startswith("<!DOCTYPE")
        assert src.read_text(encoding="utf-8") == CITIES_CSV

    def test_guard_message_names_the_path(self, loaded, csv_file):
        with pytest.raises(InvalidArgumentError) as info:
            loaded.map(csv_file)
        assert info.value.value == str(csv_file)


# ─────────────────────────────────────────────────────────────────────────────
# Static images
# ─────────────────────────────────────────────────────────────────────────────

class TestImage:
    def test_png_file(self, geo, tmp_path):
        out = tmp_path / "map.png"
        assert geo.image(out, width=120, height=90) is geo
        assert out.read_bytes()[:8] == PNG_SIGNATURE

    def test_png_uppercase_extension(self, geo, tmp_path):
        out = tmp_path / "MAP.PNG"
        geo.image(out, width=64, height=64)
        assert out.read_bytes()[:8] == PNG_SIGNATURE

    def test_svg_file_parses(self, geo, tmp_path):
        out = tmp_path / "map.svg"
        geo.image(out, title="Cities", width=300, height=200)
        root = ET.parse(out).getroot()
        assert root.tag == SVG_NS + "svg"
        assert root.get("width") == "300" and root.get("height") == "200"

    @pytest.mark.parametrize("name", ["map.jpg", "map.html", "map", "map.png.txt"])
    def test_other_extension_rejected(self, geo, tmp_path, name):
        out = tmp_path / name
        with pytest.raises(InvalidArgumentError) as info:
            geo.image(out)
        assert info.value.name == "output_path"
        assert not out.exists()
        assert list(tmp_path.iterdir()) == []

    def test_existing_file_untouched_on_error(self, geo, tmp_path):
        out = tmp_path / "keep.png"
        out.write_bytes(b"old")
        with pytest.raises(InvalidArgumentError):
            geo.image(out, overlays="not a shape")
        assert out.read_bytes() == b"old"

    def test_to_png_bytes(self, geo):
        png = geo.to_png(width=80, height=60)
        assert isinstance(png, bytes)
        assert png[:8] == PNG_SIGNATURE

    def test_to_svg_str(self, geo):
        svg = geo.to_svg(width=80, height=60, title="T")
        assert isinstance(svg, str)
        assert ET.fromstring(svg.encode("utf-8")).tag == SVG_NS + "svg"

    def test_overlay_single_dict(self, geo):
        zone = geo.buffer((28.6139, 77.2090), 50)
        plain = geo.to_svg(width=200, height=150)
        with_zone = geo.to_svg(width=200, height=150, overlays=zone)
        assert with_zone != plain
        assert geo.to_svg(width=200, height=150, overlays=[zone]) == with_zone

    def test_overlay_list(self, geo, tmp_path):
        zones = [geo.buffer((28.6139, 77.2090), 50), square(72, 18, 74, 20)]
        out = tmp_path / "zones.png"
        geo.image(out, overlays=zones, width=100, height=100)
        assert out.read_bytes()[:8] == PNG_SIGNATURE
        assert geo.to_png(width=100, height=100, overlays=zones)[:8] == PNG_SIGNATURE

    def test_overlay_invalid(self, geo):
        with pytest.raises(InvalidArgumentError):
            geo.to_png(overlays="nope")

    def test_no_data(self, tmp_path):
        g = GeoEngine(verbose=False)
        with pytest.raises(NoDataError):
            g.to_png()
        with pytest.raises(NoDataError):
            g.to_svg()
        with pytest.raises(NoDataError):
            g.image(tmp_path / "x.png")
        assert not (tmp_path / "x.png").exists()

    def test_verbose_announces(self, tmp_path, capsys):
        g = GeoEngine(verbose=True).add_point(1, 2)
        out = tmp_path / "v.svg"
        g.image(out)
        assert "v.svg" in capsys.readouterr().out


# ─────────────────────────────────────────────────────────────────────────────
# Jupyter display
# ─────────────────────────────────────────────────────────────────────────────

_SRCDOC_RE = re.compile(r'<iframe srcdoc="([^"]*)"')


class TestReprHtml:
    def test_iframe_with_escaped_map(self, geo):
        out = geo._repr_html_()
        assert out is not None
        assert out.startswith("<iframe srcdoc=")
        assert out.rstrip().endswith("</iframe>")
        m = _SRCDOC_RE.match(out)
        assert m, "srcdoc must be one attribute without raw double quotes"
        srcdoc = m.group(1)
        assert "<" not in srcdoc and ">" not in srcdoc
        page = html.unescape(srcdoc)
        assert page == geo.map_html(title=f"QOREgeo · {geo.count()} features")
        assert parse_config(page)["data"]["features"][0]["properties"]["name"] == "Delhi"

    def test_hostile_values_stay_escaped(self):
        g = GeoEngine(verbose=False).add_point(1, 2, name='"><script>alert(1)</script>')
        out = g._repr_html_()
        assert out is not None
        assert "<script>alert" not in out
        assert out.count("<iframe") == 1 and out.count("</iframe>") == 1

    def test_none_without_data(self):
        assert GeoEngine(verbose=False)._repr_html_() is None

    def test_none_above_limit(self, geo, monkeypatch):
        monkeypatch.setattr(GeoEngine, "_NOTEBOOK_MAP_LIMIT", 1)
        assert geo.count() == 2
        assert geo._repr_html_() is None
        one = geo.head(1)
        assert one._repr_html_() is not None

    def test_limit_is_inclusive(self, geo, monkeypatch):
        monkeypatch.setattr(GeoEngine, "_NOTEBOOK_MAP_LIMIT", 2)
        assert geo._repr_html_() is not None

    def test_empty_filtered_dataset(self, geo):
        empty = geo.where(lambda p: False)
        out = empty._repr_html_()
        assert out is not None and "<iframe" in out

    def test_never_raises(self, geo, monkeypatch):
        def boom(*args: Any, **kwargs: Any) -> str:
            raise RuntimeError("broken renderer")

        monkeypatch.setattr(geo, "map_html", boom)
        assert geo._repr_html_() is None

    def test_default_limit(self):
        assert GeoEngine._NOTEBOOK_MAP_LIMIT == 20_000


# ─────────────────────────────────────────────────────────────────────────────
# show()
# ─────────────────────────────────────────────────────────────────────────────

class FakeServe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, page: str, name: str = "map.html", **kwargs: Any) -> str:
        self.calls.append((page, name, kwargs))
        return f"http://127.0.0.1:1/{name}"


@pytest.fixture
def fake_serve(monkeypatch) -> FakeServe:
    import qoregeo.engine

    fake = FakeServe()
    monkeypatch.setattr(qoregeo.engine, "serve_html", fake)
    return fake


class TestShow:
    def test_map(self, geo, fake_serve):
        url = geo.show(open_browser=False, block=False)
        assert url == "http://127.0.0.1:1/map.html"
        (page, name, kwargs), = fake_serve.calls
        assert name == "map.html"
        assert page == geo.map_html()
        assert kwargs["open_browser"] is False
        assert kwargs["block"] is False
        assert kwargs["port"] == 0
        assert kwargs["verbose"] is False   # follows geo.verbose

    def test_heatmap(self, geo, fake_serve):
        url = geo.show(heatmap=True, intensity_col="population", open_browser=False)
        assert url.endswith("/heatmap.html")
        (page, name, _), = fake_serve.calls
        assert name == "heatmap.html"
        assert page == geo.heatmap_html(intensity_col="population")
        assert "leaflet-heat" in page

    def test_options_forwarded(self, geo, fake_serve):
        geo.show(title="My Cities", tiles="topo", port=8123, block=True, open_browser=True)
        (page, _, kwargs), = fake_serve.calls
        assert "<title>My Cities</title>" in page
        assert parse_config(page)["tiles"]["name"] == "topo"
        assert kwargs["port"] == 8123
        assert kwargs["block"] is True
        assert kwargs["open_browser"] is True

    def test_verbose_forwarded(self, fake_serve):
        g = GeoEngine(verbose=True).add_point(1, 2)
        g.show(open_browser=False)
        assert fake_serve.calls[0][2]["verbose"] is True

    def test_bad_option_raises_before_serving(self, geo, fake_serve):
        with pytest.raises(InvalidArgumentError):
            geo.show(tiles="bogus")
        with pytest.raises(TypeError):
            geo.show(no_such_option=1)
        assert fake_serve.calls == []

    def test_no_data(self, fake_serve):
        with pytest.raises(NoDataError):
            GeoEngine(verbose=False).show()
        assert fake_serve.calls == []


# ─────────────────────────────────────────────────────────────────────────────
# save(csv_safe=…)
# ─────────────────────────────────────────────────────────────────────────────

FORMULAS = ["=1+1", "+cmd", "-cmd", "@SUM(A1)", "\tTAB", "\rCR", "-", "=HYPERLINK(\"x\")"]
SAFE = ["-5", "+1.5", "-12.5", "+7", "plain", "a=b", " =space-first", ""]


def read_rows(path: Path, delimiter: str = ",") -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter=delimiter))


def text_engine(values: list[Any]) -> GeoEngine:
    g = GeoEngine(verbose=False)
    for v in values:
        g.add_point(1, 2, value=v)
    return g


class TestCsvSafe:
    def test_formulas_escaped(self, tmp_path):
        out = tmp_path / "safe.csv"
        text_engine(FORMULAS).save(out, csv_safe=True)
        assert [r["value"] for r in read_rows(out)] == ["'" + v for v in FORMULAS]

    def test_numbers_and_plain_text_untouched(self, tmp_path):
        out = tmp_path / "safe.csv"
        text_engine(SAFE).save(out, csv_safe=True)
        assert [r["value"] for r in read_rows(out)] == SAFE

    def test_non_strings_untouched(self, tmp_path):
        out = tmp_path / "safe.csv"
        text_engine([-5, -1.5, True, None, 0]).save(out, csv_safe=True)
        assert [r["value"] for r in read_rows(out)] == ["-5", "-1.5", "True", "", "0"]

    def test_nested_values_untouched(self, tmp_path):
        out = tmp_path / "safe.csv"
        text_engine([["=x"], {"a": "=y"}]).save(out, csv_safe=True)
        assert [json.loads(r["value"]) for r in read_rows(out)] == [["=x"], {"a": "=y"}]

    def test_default_is_raw(self, tmp_path):
        out = tmp_path / "raw.csv"
        text_engine(FORMULAS).save(out)
        assert [r["value"] for r in read_rows(out)] == FORMULAS

    def test_csv_safe_false_leaves_formulas(self, tmp_path):
        out = tmp_path / "raw.csv"
        text_engine(FORMULAS + SAFE).save(out, csv_safe=False)
        assert [r["value"] for r in read_rows(out)] == FORMULAS + SAFE

    def test_tsv_escaped(self, tmp_path):
        out = tmp_path / "safe.tsv"
        text_engine(["=1+1", "-5"]).save(out, csv_safe=True)
        assert [r["value"] for r in read_rows(out, "\t")] == ["'=1+1", "-5"]

    def test_coordinates_never_escaped(self, tmp_path):
        out = tmp_path / "neg.csv"
        GeoEngine(verbose=False).add_point(-33.86, -151.2, name="=x").save(out, csv_safe=True)
        row, = read_rows(out)
        assert row == {"latitude": "-33.86", "longitude": "-151.2", "name": "'=x"}

    def test_geojson_unaffected(self, tmp_path):
        out = tmp_path / "out.geojson"
        text_engine(["=1+1"]).save(out, csv_safe=True)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["features"][0]["properties"]["value"] == "=1+1"

    HEADER_FORMULAS = {
        '=HYPERLINK("http://evil.example","Click")': "x",
        "@SUM(A1)": 1,
        "+cmd": 2,
        "-cmd": 3,
    }

    @pytest.mark.parametrize("ext, delimiter", [(".csv", ","), (".tsv", "\t")])
    def test_header_cells_escaped(self, tmp_path, ext, delimiter):
        # Property names become header cells, which a spreadsheet evaluates too.
        g = GeoEngine(verbose=False).add_point(1, 2, name="=x", **self.HEADER_FORMULAS)
        out = tmp_path / ("safe" + ext)
        g.save(out, csv_safe=True)
        with open(out, newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh, delimiter=delimiter))
        assert header == ["latitude", "longitude", "name"] + ["'" + k for k in self.HEADER_FORMULAS]
        row, = read_rows(out, delimiter)
        assert row["name"] == "'=x"
        assert [row["'" + k] for k in self.HEADER_FORMULAS] == ["x", "1", "2", "3"]

    def test_numeric_and_plain_header_cells_untouched(self, tmp_path):
        g = GeoEngine(verbose=False).add_point(1, 2, **{"-5": "a", "a=b": "c", "geometry_id": 7})
        out = tmp_path / "safe.csv"
        g.save(out, csv_safe=True)
        with open(out, newline="", encoding="utf-8") as fh:
            assert next(csv.reader(fh)) == ["latitude", "longitude", "-5", "a=b", "geometry_id"]

    def test_header_raw_without_csv_safe(self, tmp_path):
        g = GeoEngine(verbose=False).add_point(1, 2, **self.HEADER_FORMULAS)
        out = tmp_path / "raw.csv"
        g.save(out)
        with open(out, newline="", encoding="utf-8") as fh:
            assert next(csv.reader(fh)) == ["latitude", "longitude", *self.HEADER_FORMULAS]

    def test_escaped_file_reloads(self, tmp_path):
        out = tmp_path / "safe.csv"
        text_engine(["=1+1", "-5"]).save(out, csv_safe=True)
        values = [f["properties"]["value"] for f in GeoEngine(verbose=False).load(out)]
        assert values == ["'=1+1", "-5"]


# ─────────────────────────────────────────────────────────────────────────────
# intersects()
# ─────────────────────────────────────────────────────────────────────────────

HOLED = {"type": "Polygon", "coordinates": [
    [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
    [[2, 2], [8, 2], [8, 8], [2, 8], [2, 2]],
]}
# A ring drawn continuously across the antimeridian (170°E … 170°W), as buffer() does.
DATELINE = {"type": "Polygon", "coordinates": [
    [[170, -10], [190, -10], [190, 10], [170, 10], [170, -10]],
]}

INTERSECTING = {
    "overlapping squares": (square(0, 0, 2, 2), square(1, 1, 3, 3)),
    "shared edge": (square(0, 0, 1, 1), square(1, 0, 2, 1)),
    "shared corner": (square(0, 0, 1, 1), square(1, 1, 2, 2)),
    "nested": (square(0, 0, 10, 10), square(2, 2, 3, 3)),
    "line crossing polygon": (line((-1, 0.5), (3, 0.5)), square(0, 0, 1, 1)),
    "diagonal line, no vertex inside": (line((-1, -1), (3, 3)), square(0, 0, 1, 1)),
    "point on edge": (point(1, 0.5), square(0, 0, 1, 1)),
    "point on vertex": (point(1, 1), square(0, 0, 1, 1)),
    "point inside": (point(0.5, 0.5), square(0, 0, 1, 1)),
    "same point": (point(3, 4), point(3, 4)),
    "crossing lines": (line((0, 0), (2, 2)), line((0, 2), (2, 0))),
    "polygon touching hole edge": (HOLED, square(2, 4, 3, 5)),
    "multipolygon": ({"type": "MultiPolygon", "coordinates": [
        square(20, 20, 21, 21)["coordinates"], square(0, 0, 1, 1)["coordinates"]]},
        point(0.5, 0.5)),
    "dateline polygon vs point east of 180": (DATELINE, point(-175, 0)),
    "dateline polygon vs point west of 180": (DATELINE, point(175, 0)),
    "dateline polygon vs polygon east of 180": (DATELINE, square(-178, -5, -172, 5)),
    "dateline polygon vs straddling polygon": (DATELINE, square(-175, -5, -160, 5)),
    "feature": (feature(square(0, 0, 1, 1)), feature(point(0.5, 0.5))),
    "feature collection": ({"type": "FeatureCollection", "features": [
        feature(square(5, 5, 6, 6)), feature(point(0.5, 0.5))]}, square(0, 0, 1, 1)),
    "geometry collection": ({"type": "GeometryCollection", "geometries": [
        point(50, 50), line((0, 0.5), (0.5, 0.5))]}, square(0, 0, 1, 1)),
}

DISJOINT = {
    "separate squares": (square(0, 0, 1, 1), square(2, 2, 3, 3)),
    "polygon inside hole": (HOLED, square(4, 4, 5, 5)),
    "point inside hole": (HOLED, point(5, 5)),
    "line passing by": (line((-1, 2), (3, 2)), square(0, 0, 1, 1)),
    "point just outside": (point(1.0001, 0.5), square(0, 0, 1, 1)),
    "parallel lines": (line((0, 0), (2, 0)), line((0, 1), (2, 1))),
    "dateline polygon vs far point": (DATELINE, point(-165, 0)),
    "dateline polygon vs greenwich": (DATELINE, square(0, 0, 10, 10)),
    "feature collection miss": ({"type": "FeatureCollection", "features": [
        feature(square(5, 5, 6, 6))]}, square(0, 0, 1, 1)),
    "empty feature collection": ({"type": "FeatureCollection", "features": []},
                                 square(0, 0, 1, 1)),
    "feature without geometry": (feature(None), square(0, 0, 1, 1)),
}


class TestIntersects:
    @pytest.mark.parametrize("a, b", INTERSECTING.values(), ids=INTERSECTING.keys())
    def test_intersecting(self, geo, a, b):
        assert geo.intersects(a, b) is True
        assert geo.intersects(b, a) is True

    @pytest.mark.parametrize("a, b", DISJOINT.values(), ids=DISJOINT.keys())
    def test_disjoint(self, geo, a, b):
        assert geo.intersects(a, b) is False
        assert geo.intersects(b, a) is False

    @pytest.mark.parametrize("other", [
        line((-175, -20), (-175, 20)),
        square(-178, -20, -175, 20),
    ], ids=["line", "polygon"])
    def test_dateline_polygon_crossed_without_vertex_inside(self, geo, other):
        # point(-175, 0) intersects DATELINE (see INTERSECTING), and it lies on `other`.
        assert geo.intersects(point(-175, 0), other)
        assert geo.intersects(DATELINE, other) is True

    def test_circle_circle_exact(self, geo):
        a = geo.buffer((0, 0), 100)
        near = geo.buffer((0, 1.79), 100)   # centres 199.04 km apart
        far = geo.buffer((0, 1.81), 100)    # centres 201.26 km apart
        assert geo.distance((0, 0), (0, 1.79)) < 200 < geo.distance((0, 0), (0, 1.81))
        assert geo.intersects(a, near) is True
        assert geo.intersects(a, far) is False

    def test_circle_point_exact(self, geo):
        zone = geo.buffer((0, 0), 100)   # 100 km ≈ 0.8993° of longitude at the equator
        assert geo.intersects(zone, point(0.89, 0)) is True
        assert geo.intersects(zone, point(0.91, 0)) is False

    def test_circle_polygon(self, geo):
        zone = geo.buffer((0, 0), 100)
        assert geo.intersects(zone, square(0.85, -0.1, 2, 0.1)) is True
        assert geo.intersects(square(0.95, -0.1, 2, 0.1), zone) is False
        assert geo.intersects(zone, square(-5, -5, 5, 5)) is True   # circle inside polygon

    def test_circle_across_dateline(self, geo):
        zone = geo.buffer((0, 179.9), 50)
        assert geo.intersects(zone, point(-179.9, 0)) is True
        assert geo.intersects(zone, geo.buffer((0, -179.9), 50)) is True

    def test_buffer_feature(self, geo):
        zone = geo.buffer((0, 0), 100)
        wrapped = {"type": "Feature", "geometry": zone, "properties": {}}
        assert geo.intersects(wrapped, point(0.5, 0)) is True

    @pytest.mark.parametrize("bad", [
        5, "Point", [1, 2], {"type": "Nope"}, {"type": "Polygon"},
        {"type": "Point", "coordinates": [0]}, {"type": "Point", "coordinates": [0, 100]},
        {"type": "LineString", "coordinates": "x"},
    ])
    def test_invalid_input(self, geo, bad):
        with pytest.raises(InvalidArgumentError) as info:
            geo.intersects(bad, square(0, 0, 1, 1))
        assert info.value.name == "geometry_a"
        with pytest.raises(InvalidArgumentError) as info:
            geo.intersects(square(0, 0, 1, 1), bad)
        assert info.value.name == "geometry_b"

    def test_none_is_invalid(self, geo):
        with pytest.raises(InvalidArgumentError):
            geo.intersects(None, square(0, 0, 1, 1))

    def test_works_without_data(self):
        assert GeoEngine(verbose=False).intersects(point(0, 0), point(0, 0)) is True


# ─────────────────────────────────────────────────────────────────────────────
# filter_intersects()
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def shapes() -> GeoEngine:
    return GeoEngine(verbose=False).load_data([
        feature(point(0.5, 0.5), name="inside point"),
        feature(point(5, 5), name="outside point"),
        feature(line((-1, 0.5), (3, 0.5)), name="crossing road"),
        feature(line((-1, 3), (3, 3)), name="far road"),
        feature(square(0.9, 0.9, 2, 2), name="overlapping area"),
        feature(square(1, -1, 2, 0), name="touching area"),
        feature(square(-10, -10, 10, 10), name="containing area"),
        feature(square(20, 20, 21, 21), name="distant area"),
        feature(None, name="no geometry"),
    ])


class TestFilterIntersects:
    def test_keeps_the_right_features(self, shapes):
        result = shapes.filter_intersects(square(0, 0, 1, 1))
        names = [f["properties"]["name"] for f in result]
        assert names == ["inside point", "crossing road", "overlapping area", "touching area",
                         "containing area"]

    def test_returns_a_new_engine(self, shapes):
        before = shapes.count()
        result = shapes.filter_intersects(square(0, 0, 1, 1))
        assert isinstance(result, GeoEngine)
        assert result is not shapes
        assert shapes.count() == before
        result.get_features()[0]["properties"]["name"] = "changed"
        assert shapes.get_features()[0]["properties"]["name"] == "inside point"

    def test_whole_geometry_not_representative_point(self, shapes):
        # The road's representative point is far from this small box, but the road crosses it.
        box = square(2.5, 0, 2.7, 1)
        names = [f["properties"]["name"] for f in shapes.filter_intersects(box)]
        assert "crossing road" in names
        assert shapes.filter_by_polygon(box).count() < shapes.filter_intersects(box).count()

    def test_with_buffer(self, shapes):
        zone = shapes.buffer((0.5, 0.5), 10)
        names = [f["properties"]["name"] for f in shapes.filter_intersects(zone)]
        assert names == ["inside point", "crossing road", "containing area"]

    def test_with_feature_collection(self, shapes):
        fc = {"type": "FeatureCollection", "features": [
            feature(point(5, 5)), feature(point(20.5, 20.5))]}
        names = [f["properties"]["name"] for f in shapes.filter_intersects(fc)]
        assert names == ["outside point", "containing area", "distant area"]

    def test_antimeridian(self):
        g = GeoEngine(verbose=False).load_data([
            feature(point(-175, 0), name="east of 180"),
            feature(point(175, 0), name="west of 180"),
            feature(point(-160, 0), name="too far"),
        ])
        names = [f["properties"]["name"] for f in g.filter_intersects(DATELINE)]
        assert names == ["east of 180", "west of 180"]

    def test_no_match_keeps_columns(self, shapes):
        result = shapes.filter_intersects(point(100, 80))
        assert result.count() == 0
        assert "name" in result._known_columns()

    @pytest.mark.parametrize("bad", [5, "x", {"type": "Nope"}, {"type": "Point"}])
    def test_invalid_geometry(self, shapes, bad):
        with pytest.raises(InvalidArgumentError):
            shapes.filter_intersects(bad)

    def test_invalid_geometry_on_empty_dataset(self, shapes):
        empty = shapes.where(lambda p: False)
        with pytest.raises(InvalidArgumentError):
            empty.filter_intersects({"type": "Nope"})

    def test_no_data(self):
        with pytest.raises(NoDataError):
            GeoEngine(verbose=False).filter_intersects(point(0, 0))

    def test_chains_with_output(self, shapes, tmp_path):
        out = tmp_path / "hits.svg"
        shapes.filter_intersects(square(0, 0, 1, 1)).image(out)
        assert ET.parse(out).getroot().tag == SVG_NS + "svg"


def test_written_page_is_utf8(tmp_path):
    g = GeoEngine(verbose=False).add_point(1, 2, name="São Paulo ✓")
    out = tmp_path / "utf8.html"
    g.map(out)
    page = out.read_bytes().decode("utf-8")
    assert parse_config(page)["data"]["features"][0]["properties"]["name"] == "São Paulo ✓"


# ─────────────────────────────────────────────────────────────────────────────
# Static images — labels, grid and scale bar options
# ─────────────────────────────────────────────────────────────────────────────

class TestImageOptions:
    def test_labels_drawn_in_svg(self, loaded):
        svg = loaded.to_svg(labels="name")
        assert ">Delhi<" in svg and ">Mumbai<" in svg

    def test_no_labels_by_default(self, loaded):
        assert ">Delhi<" not in loaded.to_svg()

    def test_grid_and_scale_bar_default_on(self, loaded):
        svg = loaded.to_svg()
        assert 'id="qg-grid"' in svg and 'id="qg-scale-bar"' in svg

    def test_grid_and_scale_bar_can_be_switched_off(self, loaded):
        svg = loaded.to_svg(grid=False, scale_bar=False)
        assert 'id="qg-grid"' not in svg and 'id="qg-scale-bar"' not in svg

    def test_image_passes_options(self, loaded, tmp_path):
        out = tmp_path / "labelled.svg"
        loaded.image(out, labels="name", grid=False)
        text = out.read_text(encoding="utf-8")
        assert ">Delhi<" in text and 'id="qg-grid"' not in text

    def test_png_options_accepted(self, loaded):
        assert loaded.to_png(labels="name", grid=False, scale_bar=False)[:8] == b"\x89PNG\r\n\x1a\n"

    @pytest.mark.parametrize("kwargs", [{"labels": 5}, {"grid": "yes"}, {"scale_bar": None}])
    def test_bad_options_rejected(self, loaded, kwargs):
        with pytest.raises(InvalidArgumentError):
            loaded.to_svg(**kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# "view it with: qoregeo serve …" hint and map() docs
# ─────────────────────────────────────────────────────────────────────────────

# Names that broke the printed command in bash (finding: unquoted ( ) ' & ; $ `).
SHELL_NAMES = ["sales(2026).html", "it's.html", "a b.html", "a&b;c|d.html", "$HOME.html",
               "x`id`.html", 'say "hi".html', "#tag.html", "~tilde.html", "bang!.html"]


def posix_words(cmd: str) -> list[str]:
    """Split like sh: ( ) ; & | < > are operators and # starts a comment."""
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def posix_expands(cmd: str) -> bool:
    """True if sh would expand a $, ` or ! in ``cmd`` (one outside single quotes)."""
    single = double = False
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if single:
            single = ch != "'"
        elif ch == "\\":
            i += 1
        elif ch == "'" and not double:
            single = True
        elif ch == '"':
            double = not double
        elif ch in "$`!":
            return True
        i += 1
    return False


class TestViewHint:
    @pytest.mark.parametrize("name", SHELL_NAMES)
    def test_printed_command_pastes_into_a_shell(self, tmp_path, monkeypatch, capsys, name):
        if os.name == "nt" and '"' in name:
            pytest.skip("not a valid Windows file name")
        monkeypatch.chdir(tmp_path)
        GeoEngine(verbose=True).add_point(1, 2).map(name)
        cmd = capsys.readouterr().out.split("view it with: ", 1)[1].strip()
        assert cmd.startswith("qoregeo serve ")
        if os.name != "nt":   # POSIX shell syntax
            assert posix_words(cmd) == ["qoregeo", "serve", name]
            assert not posix_expands(cmd)
        else:
            assert cmd == f'qoregeo serve "{name}"'

    @pytest.mark.parametrize("name", ["map.html", "out/m.html", "C:/maps/m-1_v2.html", "a+b=c@d,e%.html"])
    def test_plain_names_unquoted(self, name):
        assert qutils._shell_quote(name) == name

    def test_leading_dash_is_not_an_option(self):
        assert qutils._shell_quote("-v.html") == os.path.join(os.curdir, "-v.html")

    def test_heatmap_hint_quoted_too(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        GeoEngine(verbose=True).add_point(1, 2).heatmap("heat (1).html")
        assert 'qoregeo serve "heat (1).html"' in capsys.readouterr().out

    def test_no_hint_for_tiles_that_work_from_disk(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        GeoEngine(verbose=True).add_point(1, 2).map("t (1).html", tiles="topo")
        assert "qoregeo serve" not in capsys.readouterr().out


class TestMapDocstring:
    def test_presets_said_to_work_from_disk_really_do(self):
        doc = " ".join((GeoEngine.map.__doc__ or "").split())
        found = re.search(r"\(or pick (.*?), which also work when opened from disk\)", doc)
        assert found, "map() should say which basemaps work from disk"
        names = re.findall(r"'([a-z-]+)'", found.group(1))
        assert {"topo", "satellite", "none"} <= set(names)
        assert "humanitarian" not in names
        for name in names:
            assert _tiles_config(name)["needsReferer"] is False, name

    def test_tiles_list_includes_satellite(self):
        doc = " ".join((GeoEngine.map.__doc__ or "").split())
        tiles = doc.split("tiles :", 1)[1].split("overlays :", 1)[0]
        assert "'satellite'" in tiles and "'humanitarian'" in tiles


# ─────────────────────────────────────────────────────────────────────────────
# intersects(): poles, the antimeridian seam, invalid polygons, scaling
# ─────────────────────────────────────────────────────────────────────────────

HOLE = [[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]]


class TestIntersectsPoles:
    """Buffers are exact discs: shapes in the polar part of a disc intersect it."""

    def test_buffer_centred_on_north_pole(self, geo):
        zone = geo.buffer((90, 0), 500)
        assert geo.point_in_polygon((89.2, 0.5), zone)
        assert geo.intersects(zone, square(0, 89, 1, 89.5)) is True
        assert geo.intersects(zone, line((10, 89.5), (20, 89.5))) is True
        assert geo.intersects(zone, {"type": "MultiPoint", "coordinates": [[100, 86]]}) is True
        assert geo.intersects(zone, square(0, 80, 1, 85)) is False   # ≥ 556 km from the pole

    def test_disc_containing_the_pole(self, geo):
        helsinki = (60.17, 24.94)
        zone = geo.buffer(helsinki, 4000)      # reaches over the North Pole
        assert 3600 < geo.distance(helsinki, (85, -100)) < 4000
        assert geo.intersects(zone, square(-100, 85, -99, 86)) is True
        assert geo.intersects(zone, line((-150, 86), (-140, 86))) is True
        assert geo.intersects(square(-100, 85, -99, 86), zone) is True
        assert geo.intersects(zone, square(-100, 20, -99, 21)) is False   # Mexico

    def test_south_pole(self, geo):
        zone = geo.buffer((-90, 0), 300)
        assert geo.intersects(zone, square(-60, -89.6, -59.8, -89.4)) is True
        assert geo.intersects(zone, square(-60, -80, -59, -79)) is False

    def test_buffer_covering_both_poles(self, geo):
        zone = geo.buffer((0, 0), 12000)
        assert geo.intersects(zone, square(100, 80, 101, 81)) is True    # over the pole
        assert geo.intersects(zone, square(-120, -85, -119, -84)) is True
        assert geo.intersects(zone, square(170, -1, 171, 1)) is False    # near the antipode

    def test_long_edges_are_straight_in_lng_lat(self, geo):
        # An edge from (0, 89) to (180, 89) runs along the 89th parallel
        # (111 km from the pole), as point_in_polygon() reads it — not over the pole.
        assert geo.intersects(geo.buffer((90, 0), 100), line((0, 89), (180, 89))) is False
        assert geo.intersects(geo.buffer((90, 0), 112), line((0, 89), (180, 89))) is True

    def test_circle_edge_is_exact_between_vertices(self, geo):
        zone = geo.buffer((0, 0), 100)         # 100 km ≈ 0.8993° of latitude
        assert geo.intersects(zone, line((-10, 0.89), (10, 0.89))) is True
        assert geo.intersects(zone, line((-10, 0.91), (10, 0.91))) is False

    def test_agrees_with_point_tests_for_polar_buffers(self):
        g = GeoEngine(verbose=False)
        for lat in (80, 84, 86, 88, 89.5):
            for lng in range(-180, 180, 30):
                g.add_point(lat, lng)
        for center, radius in [((90, 0), 500), ((60.17, 24.94), 4000), ((89.5, 0), 200)]:
            zone = g.buffer(center, radius)
            assert g.filter_intersects(zone).count() == g.filter_by_polygon(zone).count() > 0


class TestIntersectsAntimeridianSeam:
    @pytest.mark.parametrize("a, b", [
        (point(180, 0), point(-180, 0)),
        (square(170, -5, 180, 5), square(-180, -5, -170, 5)),
        (square(170, -5, 180, 5), point(-180, 0)),
        (line((175, 0), (180, 0)), line((-180, 0), (-175, 0))),
        (line((175, 1), (180, 1)), square(-180, 0, -170, 2)),
    ], ids=["points", "polygons", "polygon-point", "lines", "line-polygon"])
    def test_shapes_meeting_on_the_antimeridian(self, geo, a, b):
        assert geo.intersects(a, b) is True
        assert geo.intersects(b, a) is True

    def test_seam_neighbours_that_do_not_touch(self, geo):
        assert geo.intersects(square(170, -5, 180, 5), square(-180, 6, -170, 10)) is False
        assert geo.intersects(point(180, 0), point(-180, 1)) is False

    def test_filter_intersects_on_the_seam(self):
        g = GeoEngine(verbose=False).load_data([
            feature(square(-180, -5, -170, 5), name="east half"),
            feature(point(-180, 0), name="seam point"),
            feature(point(-175, 0), name="inside east half"),
        ])
        names = [f["properties"]["name"] for f in g.filter_intersects(square(170, -5, 180, 5))]
        assert names == ["east half", "seam point"]


class TestIntersectsInvalidPolygons:
    @pytest.mark.parametrize("bad", [
        {"type": "Polygon", "coordinates": [[], HOLE]},
        {"type": "MultiPolygon", "coordinates": [[[], HOLE]]},
    ], ids=["polygon", "multipolygon"])
    def test_holes_without_exterior_rejected(self, geo, bad):
        # The hole used to be promoted to the exterior, so a point *in the hole* matched.
        with pytest.raises(InvalidArgumentError) as info:
            geo.intersects(bad, point(1.5, 1.5))
        assert "empty exterior ring" in str(info.value)
        with pytest.raises(InvalidBufferError):      # point_in_polygon agrees
            geo.point_in_polygon((1.5, 1.5), bad)

    def test_empty_polygon_is_still_empty(self, geo):
        assert geo.intersects({"type": "Polygon", "coordinates": [[]]}, point(0, 0)) is False
        assert geo.intersects({"type": "Polygon", "coordinates": []}, point(0, 0)) is False

    def test_load_skips_hole_without_exterior(self):
        with pytest.warns(UserWarning, match="empty exterior ring"):
            g = GeoEngine(verbose=False).load_data([
                feature({"type": "Polygon", "coordinates": [[], HOLE]}), feature(point(0, 0))])
        assert g.count() == 1


def zigzag(n: int, y: float, amplitude: float = 0.001) -> dict[str, Any]:
    return line(*[(-5 + 10 * i / n, y + (amplitude if i % 2 else -amplitude)) for i in range(n)])


def ring_polygon(n: int, cx: float, cy: float, r: float) -> dict[str, Any]:
    ring = [[cx + r * math.cos(2 * math.pi * k / n) * (1 + 0.2 * math.sin(14 * math.pi * k / n)),
             cy + r * math.sin(2 * math.pi * k / n)] for k in range(n)]
    return {"type": "Polygon", "coordinates": [ring + [ring[0]]]}


class CallCounter:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        self.calls = 0
        original = getattr(qutils, name)

        def counting(*args: Any) -> Any:
            self.calls += 1
            return original(*args)

        monkeypatch.setattr(qutils, name, counting)


class TestIntersectsScaling:
    """Edge pairs are pruned by bounding boxes (these used to be O(n·m))."""

    def test_parallel_lines_compare_few_edge_pairs(self, geo, monkeypatch):
        n = 3000
        counter = CallCounter(monkeypatch, "_segments_intersect")
        assert geo.intersects(zigzag(n, 0.0), zigzag(n, 0.01)) is False
        assert counter.calls < 10 * n            # was n² = 9,000,000
        assert geo.intersects(zigzag(n, 0.0), line((1e-4, -1), (1e-4, 1))) is True   # still found

    def test_far_apart_shapes_need_no_edge_tests(self, geo, monkeypatch):
        counter = CallCounter(monkeypatch, "_segments_intersect")
        a, b = ring_polygon(3000, 0, 0, 1), ring_polygon(3000, 5, 0, 1)
        assert geo.intersects(a, b) is False
        assert counter.calls == 0

    def test_circle_far_from_a_long_line_needs_no_distance_tests(self, geo, monkeypatch):
        counter = CallCounter(monkeypatch, "_arc_distance_km")
        river = zigzag(20000, 3.0, amplitude=0.5)
        assert geo.intersects(geo.buffer((0, 0), 100), river) is False
        assert counter.calls == 0
        assert geo.intersects(geo.buffer((3, 0), 100), river) is True

    def test_filter_intersects_prepares_the_query_once(self, monkeypatch):
        built: list[int] = []
        original = qutils._PolygonIndex

        class Counting(original):  # type: ignore[misc, valid-type]
            def __init__(self, rings: Any) -> None:
                built.append(len(rings[0]))
                super().__init__(rings)

        monkeypatch.setattr(qutils, "_PolygonIndex", Counting)
        g = GeoEngine(verbose=False)
        for i in range(400):
            g.add_point(-2 + (i % 20) * 0.2, -2 + (i // 20) * 0.2, i=i)
        zone = ring_polygon(1000, 0, 0, 1.5)
        hits = g.filter_intersects(zone)
        assert built == [1001]                    # one index for the query, not one per feature
        assert hits.count() == g.filter_by_polygon(zone).count() > 0
