"""
tests/test_maps.py
==================
HTML map and heatmap generation — static analysis of the generated files.

No browser and no network: every test writes a page into ``tmp_path`` and
inspects its text (and the ``var CONFIG = JSON.parse("…");`` data it embeds).

Covers file output, title escaping, script-injection safety of the embedded
data, NaN / non-JSON property values, the pinned Subresource Integrity hashes
and CDN fallbacks, the view (center / zoom) config, basemap presets and
custom tile URLs, the file:// notice, overlays, heatmap intensity
normalisation and radius / blur validation, verbose output (including
consoles that cannot print emoji) and the map_builder JSON helpers.

Run with:
    pytest tests/test_maps.py -v
"""

from __future__ import annotations

import base64
import csv
import datetime
import decimal
import html
import io
import json
import math
import random
import re
import shlex
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest

import qoregeo
from qoregeo import GeoEngine
from qoregeo.exceptions import (
    ColumnNotFoundError,
    InvalidArgumentError,
    InvalidCoordinateError,
    NoDataError,
)
from qoregeo.map_builder import (
    _LEAFLET_CSS,
    _LEAFLET_HEAT_JS,
    _LEAFLET_JS,
    _TILES,
    _announce,
    _json_safe,
    _nesting_depth,
    _spans_world,
    _to_js,
    map_page,
    tile_presets,
)

# Heatmap tests feed deliberately unusable intensities; the warning they cause
# is asserted once in TestHeatmapIntensity.test_dropped_rows_warning.
pytestmark = pytest.mark.filterwarnings("ignore:QOREgeo. .* no heat:UserWarning")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# SHA-256 of dist/leaflet.css + dist/leaflet.js in the npm tarball leaflet@1.9.4
# and of dist/leaflet-heat.js in leaflet.heat@0.2.0 (base64, SRI format).
LEAFLET_CSS_SRI  = "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
LEAFLET_JS_SRI   = "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
LEAFLET_HEAT_SRI = "sha256-65UqrlgGoRAnKfKRuriH3eeDrOhZgZo1SCenduc+SGo="

LEAFLET_CSS_URLS = [
    "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
    "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css",
]
LEAFLET_JS_URLS = [
    "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
    "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js",
]
LEAFLET_HEAT_URLS = [
    "https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js",
    "https://cdn.jsdelivr.net/npm/leaflet.heat@0.2.0/dist/leaflet-heat.js",
]

OSM_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
PRESET_URLS = {
    "light":        OSM_URL,
    "osm":          OSM_URL,
    "dark":         OSM_URL,
    "humanitarian": "https://tile-{s}.openstreetmap.fr/hot/{z}/{x}/{y}.png",
    "topo":         "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
    "cyclosm":      "https://{s}.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png",
    "osm-de":       "https://tile.openstreetmap.de/{z}/{x}/{y}.png",
    "osm-fr":       "https://{s}.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png",
    "satellite":    "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/BlueMarble_NextGeneration/"
                    "default/GoogleMapsCompatible_Level8/{z}/{y}/{x}.jpeg",
    "none":         None,
}
OSM_PRESETS = ["light", "osm", "dark"]   # served by tile.openstreetmap.org
# Tile servers following the OpenStreetMap tile policy (a Referer is required).
REFERER_PRESETS = [*OSM_PRESETS, "humanitarian", "cyclosm", "osm-fr"]

OSM_COPYRIGHT_LINK = '<a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
OSM_ATTRIBUTION = (f"&copy; {OSM_COPYRIGHT_LINK} contributors | "
                   '<a href="https://www.openstreetmap.org/fixthemap">Report a map issue</a>')
CUSTOM_TILES = "https://tiles.example.com/{z}/{x}/{y}.png"

XSS = "</script><script>alert(1)</script>"

CITIES = [
    ("Delhi",     28.6139, 77.2090, 32900000),
    ("Mumbai",    19.0760, 72.8777, 20700000),
    ("Bangalore", 12.9716, 77.5946, 13200000),
    ("Kolkata",   22.5726, 88.3639, 14900000),
    ("Chennai",   13.0827, 80.2707, 11300000),
    ("Pune",      18.5204, 73.8567,  7400000),
    ("Hyderabad", 17.3850, 78.4867, 10500000),
]

BUILDERS = ["map", "heatmap"]

_MISSING = object()   # marker: feature has no value for the intensity column

_CONFIG_RE = re.compile(r"^[ \t]*var CONFIG = JSON\.parse\((.*)\);[ \t]*$", re.MULTILINE)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers + fixtures
# ─────────────────────────────────────────────────────────────────────────────

def read_page(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def config_text(page: str) -> str:
    """Return the raw string literal passed to ``JSON.parse`` for ``var CONFIG`` (exactly one)."""
    found = _CONFIG_RE.findall(page)
    assert len(found) == 1, "expected exactly one 'var CONFIG = JSON.parse(…);' line"
    return found[0]


def parse_config(page: str) -> dict[str, Any]:
    # The page holds a JS string literal (valid JSON) whose value is the JSON config.
    return json.loads(json.loads(config_text(page)))


def render(geo: GeoEngine, tmp_path: Path, builder: str = "map", **kwargs: Any) -> str:
    """Write a map / heatmap into tmp_path and return the page text."""
    out = tmp_path / f"{builder}.html"
    getattr(geo, builder)(out, **kwargs)
    return read_page(out)


def props_of(page: str, index: int = 0) -> dict[str, Any]:
    return parse_config(page)["data"]["features"][index]["properties"]


def weighted(*weights: Any, key: str = "w") -> GeoEngine:
    """Engine with one Point per weight at (lat=20+i, lng=70+i)."""
    feats = []
    for i, w in enumerate(weights):
        props = {"i": i} if w is _MISSING else {"i": i, key: w}
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [70.0 + i, 20.0 + i]},
            "properties": props,
        })
    return GeoEngine(verbose=False).load_data(feats)


def single(**properties: Any) -> GeoEngine:
    return GeoEngine(verbose=False).add_point(28.6139, 77.2090, **properties)


@pytest.fixture
def geo() -> GeoEngine:
    g = GeoEngine(verbose=False)
    for name, lat, lng, pop in CITIES:
        g.add_point(lat, lng, name=name, population=pop)
    return g


@pytest.fixture
def empty(geo: GeoEngine) -> GeoEngine:
    """Loaded dataset whose filter matched nothing."""
    result = geo.where(lambda p: False)
    assert result.count() == 0
    return result


# ─────────────────────────────────────────────────────────────────────────────
# File output — map()
# ─────────────────────────────────────────────────────────────────────────────

class TestMapFileOutput:
    def test_creates_file(self, geo, tmp_path):
        out = tmp_path / "cities.html"
        geo.map(str(out))
        assert out.is_file()
        assert out.stat().st_size > 0

    def test_creates_missing_parent_dirs(self, geo, tmp_path):
        out = tmp_path / "a" / "b" / "c" / "map.html"
        geo.map(str(out))
        assert out.is_file()

    def test_accepts_pathlib_path(self, geo, tmp_path):
        out = tmp_path / "nested" / "map.html"
        geo.map(out)
        assert out.is_file()

    def test_default_output_path(self, geo, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        geo.map()
        assert (tmp_path / "map.html").is_file()

    def test_returns_self(self, geo, tmp_path):
        assert geo.map(tmp_path / "m.html") is geo

    def test_overwrites_existing_file(self, geo, tmp_path):
        out = tmp_path / "m.html"
        out.write_text("stale", encoding="utf-8")
        geo.map(out)
        assert read_page(out).startswith("<!DOCTYPE html>")

    def test_is_standalone_html_document_with_version(self, geo, tmp_path):
        page = render(geo, tmp_path)
        assert page.startswith("<!DOCTYPE html>")
        assert page.rstrip().endswith("</html>")
        assert '<meta charset="UTF-8">' in page
        assert 'name="viewport"' in page
        assert '<div id="map"' in page
        assert f'<meta name="generator" content="QOREgeo {qoregeo.__version__}">' in page

    def test_written_as_utf8_with_literal_non_ascii(self, tmp_path):
        out = tmp_path / "m.html"
        single(name="Zürich", jp="東京").map(out)
        text = out.read_bytes().decode("utf-8")
        assert "Zürich" in text
        assert "東京" in text

    def test_embeds_all_features(self, geo, tmp_path):
        cfg = parse_config(render(geo, tmp_path))
        assert cfg["data"] == {"type": "FeatureCollection", "features": geo.get_features()}

    def test_embeds_only_filtered_features(self, geo, tmp_path):
        page = render(geo.filter("name", "Pune"), tmp_path)
        feats = parse_config(page)["data"]["features"]
        assert [f["properties"]["name"] for f in feats] == ["Pune"]

    def test_non_point_and_null_geometries_embedded(self, tmp_path):
        g = GeoEngine(verbose=False).load_data([
            {"type": "LineString", "coordinates": [[77.2, 28.6], [72.9, 19.1]]},
            {"type": "Polygon", "coordinates": [[[10, 40], [12, 40], [12, 42], [10, 40]]]},
            {"type": "Feature", "geometry": None, "properties": {"note": "no location"}},
        ])
        feats = parse_config(render(g, tmp_path))["data"]["features"]
        assert [f["geometry"] and f["geometry"]["type"] for f in feats] == \
            ["LineString", "Polygon", None]

    def test_empty_filtered_dataset(self, empty, tmp_path):
        out = tmp_path / "empty" / "map.html"
        assert empty.map(out) is empty
        cfg = parse_config(read_page(out))
        assert cfg["data"] == {"type": "FeatureCollection", "features": []}

    def test_no_data_raises_and_writes_nothing(self, tmp_path):
        out = tmp_path / "no.html"
        with pytest.raises(NoDataError):
            GeoEngine(verbose=False).map(out)
        assert not out.exists()

    def test_feature_count_panel_and_style(self, geo, tmp_path):
        page = render(geo, tmp_path)
        assert '<span id="n">0</span> features' in page
        assert set(parse_config(page)["style"]) == {"point", "shape", "overlay"}

    def test_does_not_mutate_features(self, tmp_path):
        g = single(score=float("nan"), pair=(1, 2))
        g.map(tmp_path / "m.html")
        props = g.get_features()[0]["properties"]
        assert math.isnan(props["score"])
        assert props["pair"] == (1, 2)


# ─────────────────────────────────────────────────────────────────────────────
# File output — heatmap()
# ─────────────────────────────────────────────────────────────────────────────

class TestHeatmapFileOutput:
    def test_creates_file_and_parent_dirs(self, geo, tmp_path):
        out = tmp_path / "x" / "y" / "heat.html"
        geo.heatmap(str(out))
        assert out.is_file()

    def test_default_output_path(self, geo, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        geo.heatmap()
        assert (tmp_path / "heatmap.html").is_file()

    def test_returns_self(self, geo, tmp_path):
        assert geo.heatmap(tmp_path / "h.html") is geo

    def test_empty_filtered_dataset(self, empty, tmp_path):
        out = tmp_path / "h.html"
        assert empty.heatmap(out) is empty
        cfg = parse_config(read_page(out))
        assert cfg["points"] == []
        assert cfg["bounds"] is None

    def test_no_data_raises_and_writes_nothing(self, tmp_path):
        out = tmp_path / "no.html"
        with pytest.raises(NoDataError):
            GeoEngine(verbose=False).heatmap(out)
        assert not out.exists()

    def test_uses_heat_layer_not_feature_layer(self, geo, tmp_path):
        page = render(geo, tmp_path, "heatmap")
        assert "L.heatLayer(CONFIG.points, CONFIG.heat)" in page
        assert "L.geoJSON" not in page
        assert 'id="count"' not in page

    def test_heat_options_embedded(self, geo, tmp_path):
        heat = parse_config(render(geo, tmp_path, "heatmap"))["heat"]
        assert heat["max"] == 1.0
        assert heat["maxZoom"] == 12
        assert set(heat["gradient"]) == {"0.1", "0.3", "0.6", "0.85", "1.0"}


# ─────────────────────────────────────────────────────────────────────────────
# Title escaping
# ─────────────────────────────────────────────────────────────────────────────

class TestTitleEscaping:
    @pytest.mark.parametrize("builder", BUILDERS)
    def test_script_title_never_raw(self, geo, tmp_path, builder):
        title = "</title><script>alert(1)</script>"
        page = render(geo, tmp_path, builder, title=title)
        assert title not in page
        assert "<script>alert" not in page
        assert page.count("<title>") == page.count("</title>") == 1

    @pytest.mark.parametrize("builder", BUILDERS)
    def test_escaped_title_in_tab_aria_label_and_brand(self, geo, tmp_path, builder):
        page = render(geo, tmp_path, builder, title="</title><script>alert(1)</script>")
        escaped = "&lt;/title&gt;&lt;script&gt;alert(1)&lt;/script&gt;"
        assert f"<title>{escaped}</title>" in page
        assert f'aria-label="{escaped}"' in page
        assert page.count(escaped) == 3

    def test_quotes_cannot_break_attribute(self, geo, tmp_path):
        page = render(geo, tmp_path, title='x" onmouseover="alert(1)')
        assert 'onmouseover="alert(1)' not in page
        assert 'aria-label="x&quot; onmouseover=&quot;alert(1)"' in page

    def test_ampersand_escaped(self, geo, tmp_path):
        page = render(geo, tmp_path, title="Fish & Chips")
        assert "<title>Fish &amp; Chips</title>" in page

    @pytest.mark.parametrize(("builder", "default"),
                             [("map", "QOREgeo Map"), ("heatmap", "QOREgeo Heatmap")])
    def test_default_titles(self, geo, tmp_path, builder, default):
        assert f"<title>{default}</title>" in render(geo, tmp_path, builder)

    def test_non_string_title(self, geo, tmp_path):
        assert "<title>2026</title>" in render(geo, tmp_path, title=2026)

    @pytest.mark.parametrize("placeholder", ["__CONFIG__", "__INIT__", "__TITLE__"])
    def test_template_placeholder_in_title_not_substituted(self, geo, tmp_path, placeholder):
        page = render(geo, tmp_path, title=placeholder)
        assert f"<title>{placeholder}</title>" in page
        parse_config(page)   # still exactly one CONFIG assignment


# ─────────────────────────────────────────────────────────────────────────────
# Script-injection safety of the embedded data
# ─────────────────────────────────────────────────────────────────────────────

def _script_blocks(page: str) -> tuple[int, int]:
    low = page.lower()
    return low.count("<script"), low.count("</script>")


class TestScriptInjection:
    @pytest.mark.parametrize("builder", BUILDERS)
    def test_single_inline_script_block(self, geo, tmp_path, builder):
        assert _script_blocks(render(geo, tmp_path, builder)) == (1, 1)

    def test_script_close_in_property_value(self, tmp_path):
        page = render(single(name=XSS), tmp_path)
        assert _script_blocks(page) == (1, 1)
        assert XSS not in page

    def test_script_close_in_property_key(self, tmp_path):
        page = render(single(**{XSS: "value"}), tmp_path)
        assert _script_blocks(page) == (1, 1)
        assert XSS in props_of(page)

    def test_uppercase_script_close(self, tmp_path):
        page = render(single(name="</SCRIPT ><img src=x onerror=alert(1)>"), tmp_path)
        assert _script_blocks(page) == (1, 1)
        assert "<img" not in page

    def test_html_chars_escaped_as_unicode(self, tmp_path):
        page = render(single(expr="a<b>&c"), tmp_path)
        assert "a\\u003cb\\u003e\\u0026c" in page
        assert "a<b>&c" not in page

    def test_config_line_has_no_raw_html_chars(self, tmp_path):
        g = single(name=XSS, amp="Tom & Jerry", cmp="1 < 2 > 0", comment="<!-- x -->")
        line = config_text(render(g, tmp_path))
        assert not set(line) & set("<>&")

    def test_values_round_trip_exactly(self, tmp_path):
        values = {"name": XSS, "amp": "&amp; &lt;", "quote": "\"'\\", "comment": "<!--"}
        assert props_of(render(single(**values), tmp_path)) == values

    def test_line_and_paragraph_separators_escaped(self, tmp_path):
        page = render(single(text="a\u2028b\u2029c"), tmp_path)
        assert "\u2028" not in page
        assert "\u2029" not in page
        assert "a\\u2028b\\u2029c" in page
        assert props_of(page)["text"] == "a\u2028b\u2029c"

    def test_custom_tile_url_cannot_break_out(self, geo, tmp_path):
        url = CUSTOM_TILES + "?q=" + XSS
        page = render(geo, tmp_path, tiles=url)
        assert _script_blocks(page) == (1, 1)
        assert parse_config(page)["tiles"]["url"] == url

    @pytest.mark.parametrize("builder", BUILDERS)
    def test_attribution_cannot_break_out(self, geo, tmp_path, builder):
        attribution = XSS + "<b>Tiles</b>"
        page = render(geo, tmp_path, builder, tiles=CUSTOM_TILES, attribution=attribution)
        assert _script_blocks(page) == (1, 1)
        assert parse_config(page)["tiles"]["options"]["attribution"] == attribution

    @pytest.mark.parametrize("builder", BUILDERS)
    def test_no_static_external_script_tags(self, geo, tmp_path, builder):
        # Every script is loaded by the SRI-checking loader, never by a bare tag.
        assert "<script src" not in render(geo, tmp_path, builder).lower()


# ─────────────────────────────────────────────────────────────────────────────
# NaN and non-JSON-serialisable property values
# ─────────────────────────────────────────────────────────────────────────────

class TestNonJsonValues:
    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_float_becomes_null(self, tmp_path, value):
        page = render(single(score=value), tmp_path)
        assert "NaN" not in config_text(page)
        assert "Infinity" not in config_text(page)
        assert props_of(page)["score"] is None

    def test_nested_nan_becomes_null(self, tmp_path):
        g = single(stats={"mean": float("nan"), "series": [1.0, float("nan"), (2.0, math.inf)]})
        assert props_of(render(g, tmp_path))["stats"] == {
            "mean": None, "series": [1.0, None, [2.0, None]],
        }

    def test_dates_and_times_become_iso_strings(self, tmp_path):
        g = single(
            at=datetime.datetime(2026, 10, 19, 8, 30, 15),
            day=datetime.date(2026, 10, 19),
            clock=datetime.time(23, 59),
        )
        assert props_of(render(g, tmp_path)) == {
            "at": "2026-10-19T08:30:15", "day": "2026-10-19", "clock": "23:59:00",
        }

    def test_decimal_becomes_number(self, tmp_path):
        assert props_of(render(single(price=decimal.Decimal("12.50")), tmp_path)) == \
            {"price": pytest.approx(12.5)}

    def test_set_becomes_sorted_list(self, tmp_path):
        g = single(tags={"b", "c", "a"}, frozen=frozenset({3, 1, 2}))
        assert props_of(render(g, tmp_path)) == {"tags": ["a", "b", "c"], "frozen": [1, 2, 3]}

    def test_arbitrary_object_becomes_string(self, tmp_path):
        class Thing:
            def __str__(self) -> str:
                return "<thing>"

        page = render(single(obj=Thing(), raw=b"ab"), tmp_path)
        assert props_of(page) == {"obj": "<thing>", "raw": "b'ab'"}
        assert "<thing>" not in page

    def test_heatmap_with_odd_values_does_not_crash(self, tmp_path):
        g = single(w=decimal.Decimal("3"), at=datetime.date(2026, 1, 1), tags={1, 2})
        cfg = parse_config(render(g, tmp_path, "heatmap", intensity_col="w"))
        assert cfg["points"] == [[28.6139, 77.2090, 1.0]]

    @pytest.mark.parametrize("value", ["NaN", "Infinity"])
    def test_non_finite_decimal_becomes_null(self, tmp_path, value):
        page = render(single(price=decimal.Decimal(value)), tmp_path)
        assert props_of(page)["price"] is None

    def test_set_containing_nan_does_not_crash(self, tmp_path):
        page = render(single(tags={1.0, float("nan")}), tmp_path)
        assert None in props_of(page)["tags"]


# ─────────────────────────────────────────────────────────────────────────────
# Assets — pinned versions, SRI hashes and CDN fallbacks
# ─────────────────────────────────────────────────────────────────────────────

class TestAssetsAndSRI:
    @pytest.mark.parametrize("builder", BUILDERS)
    def test_css_link_has_integrity_and_crossorigin(self, geo, tmp_path, builder):
        page = render(geo, tmp_path, builder)
        link = re.search(r"<link\b[^>]*>", page, re.DOTALL)
        assert link is not None
        tag = link.group(0)
        assert f'href="{LEAFLET_CSS_URLS[0]}"' in tag
        assert f'integrity="{LEAFLET_CSS_SRI}"' in tag
        assert 'crossorigin="anonymous"' in tag

    @pytest.mark.parametrize("builder", BUILDERS)
    def test_css_falls_back_to_second_cdn(self, geo, tmp_path, builder):
        page = render(geo, tmp_path, builder)
        assert f"this.onerror=null;this.href='{LEAFLET_CSS_URLS[1]}';" in page

    def test_map_scripts_leaflet_only(self, geo, tmp_path):
        page = render(geo, tmp_path)
        assert parse_config(page)["scripts"] == [
            {"urls": LEAFLET_JS_URLS, "integrity": LEAFLET_JS_SRI},
        ]
        assert "leaflet-heat" not in page

    def test_heatmap_scripts_load_leaflet_then_plugin(self, geo, tmp_path):
        cfg = parse_config(render(geo, tmp_path, "heatmap"))
        assert cfg["scripts"] == [
            {"urls": LEAFLET_JS_URLS, "integrity": LEAFLET_JS_SRI},
            {"urls": LEAFLET_HEAT_URLS, "integrity": LEAFLET_HEAT_SRI},
        ]

    @pytest.mark.parametrize("builder", BUILDERS)
    def test_loader_enforces_integrity_and_crossorigin(self, geo, tmp_path, builder):
        page = render(geo, tmp_path, builder)
        assert "s.integrity = asset.integrity;" in page
        assert 's.crossOrigin = "anonymous";' in page
        assert "s.onerror = function () { s.remove(); next(); };" in page

    def test_module_constants_are_pinned(self):
        assert _LEAFLET_CSS == {"urls": LEAFLET_CSS_URLS, "integrity": LEAFLET_CSS_SRI}
        assert _LEAFLET_JS == {"urls": LEAFLET_JS_URLS, "integrity": LEAFLET_JS_SRI}
        assert _LEAFLET_HEAT_JS == {"urls": LEAFLET_HEAT_URLS, "integrity": LEAFLET_HEAT_SRI}

    @pytest.mark.parametrize("sri", [LEAFLET_CSS_SRI, LEAFLET_JS_SRI, LEAFLET_HEAT_SRI])
    def test_sri_values_are_well_formed_sha256(self, sri):
        algo, _, digest = sri.partition("-")
        assert algo == "sha256"
        assert len(base64.b64decode(digest, validate=True)) == 32

    def test_asset_urls_are_https_and_version_pinned(self):
        for asset in (_LEAFLET_CSS, _LEAFLET_JS, _LEAFLET_HEAT_JS):
            assert len(asset["urls"]) == 2
            for url in asset["urls"]:
                assert url.startswith("https://")
                assert re.search(r"@\d+\.\d+\.\d+/", url), url


# ─────────────────────────────────────────────────────────────────────────────
# View config — center / zoom
# ─────────────────────────────────────────────────────────────────────────────

class TestViewConfig:
    @pytest.mark.parametrize("builder", BUILDERS)
    def test_default_view_fits_data(self, geo, tmp_path, builder):
        view = parse_config(render(geo, tmp_path, builder))["view"]
        assert view == {"center": None, "zoom": 10, "fitMaxZoom": 15}

    @pytest.mark.parametrize("builder", BUILDERS)
    def test_center_and_zoom(self, geo, tmp_path, builder):
        view = parse_config(render(geo, tmp_path, builder, center=(19.076, 72.8777), zoom=7))["view"]
        assert view == {"center": [19.076, 72.8777], "zoom": 7, "fitMaxZoom": 7}

    def test_center_without_zoom_uses_default_zoom(self, geo, tmp_path):
        view = parse_config(render(geo, tmp_path, center=[51.5074, -0.1278]))["view"]
        assert view["center"] == [51.5074, -0.1278]
        assert view["zoom"] == 10

    def test_zoom_without_center_caps_fit(self, geo, tmp_path):
        view = parse_config(render(geo, tmp_path, zoom=4))["view"]
        assert view == {"center": None, "zoom": 4, "fitMaxZoom": 4}

    @pytest.mark.parametrize("center", [
        {"type": "Point", "coordinates": [72.8777, 19.076]},
        {"type": "Feature", "properties": {},
         "geometry": {"type": "Point", "coordinates": [72.8777, 19.076]}},
        ("19.076", "72.8777"),
        (19.076, 72.8777, 14.0),
    ], ids=["geojson-point", "feature", "numeric-strings", "with-altitude"])
    def test_center_accepts_point_like_values(self, geo, tmp_path, center):
        view = parse_config(render(geo, tmp_path, center=center))["view"]
        assert view["center"] == pytest.approx([19.076, 72.8777])

    @pytest.mark.parametrize("builder", BUILDERS)
    @pytest.mark.parametrize("center", [(91, 0), (0, 181), "28.6,77.2", [28.6], (math.nan, 0)])
    def test_invalid_center_raises(self, geo, tmp_path, builder, center):
        out = tmp_path / "v.html"
        with pytest.raises(InvalidCoordinateError):
            getattr(geo, builder)(out, center=center)
        assert not out.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Zoom validation
# ─────────────────────────────────────────────────────────────────────────────

class TestZoomValidation:
    @pytest.mark.parametrize("builder", BUILDERS)
    @pytest.mark.parametrize("zoom", [-1, 23, 100, 5.0, "5", True, False, [5]])
    def test_invalid_zoom_raises(self, geo, tmp_path, builder, zoom):
        out = tmp_path / "z.html"
        with pytest.raises(InvalidArgumentError) as exc:
            getattr(geo, builder)(out, zoom=zoom)
        assert exc.value.name == "zoom"
        assert isinstance(exc.value, ValueError)
        assert not out.exists()

    @pytest.mark.parametrize("builder", BUILDERS)
    @pytest.mark.parametrize("zoom", [0, 22])
    def test_boundary_zoom_accepted(self, geo, tmp_path, builder, zoom):
        view = parse_config(render(geo, tmp_path, builder, zoom=zoom))["view"]
        assert view["zoom"] == zoom
        assert view["fitMaxZoom"] == zoom


# ─────────────────────────────────────────────────────────────────────────────
# Basemaps — presets (all open and keyless)
# ─────────────────────────────────────────────────────────────────────────────

class TestTilePresets:
    @pytest.mark.parametrize("name", list(PRESET_URLS))
    def test_preset_url(self, geo, tmp_path, name):
        tiles = parse_config(render(geo, tmp_path, tiles=name))["tiles"]
        assert tiles["url"] == PRESET_URLS[name]
        assert tiles["name"] == name

    def test_presets_table_matches(self):
        assert set(_TILES) == set(PRESET_URLS)

    @pytest.mark.parametrize("builder", BUILDERS)
    def test_default_is_light_openstreetmap(self, geo, tmp_path, builder):
        tiles = parse_config(render(geo, tmp_path, builder))["tiles"]
        assert tiles["name"] == "light"
        assert tiles["url"] == OSM_URL
        assert tiles["options"]["className"] == "qg-tiles-light"

    @pytest.mark.parametrize("name", list(PRESET_URLS))
    def test_no_preset_needs_a_key(self, geo, tmp_path, name):
        page = render(geo, tmp_path, tiles=name)
        assert "key=" not in (parse_config(page)["tiles"]["url"] or "")
        assert "cartocdn" not in page and "carto.com" not in page

    @pytest.mark.parametrize("builder", BUILDERS)
    @pytest.mark.parametrize("name", list(PRESET_URLS))
    def test_no_preset_ever_warns(self, geo, tmp_path, builder, name):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            getattr(geo, builder)(tmp_path / "w.html", tiles=name)

    @pytest.mark.parametrize(("alias", "name"), [
        ("hot", "humanitarian"), ("opentopomap", "topo"), ("blank", "none"), ("osm_light", "light"),
        ("osm-dark", "dark"), ("cycling", "cyclosm"), ("nasa", "satellite"), ("osm_de", "osm-de"),
    ])
    def test_aliases(self, geo, tmp_path, alias, name):
        assert parse_config(render(geo, tmp_path, tiles=alias))["tiles"]["name"] == name

    @pytest.mark.parametrize("spelling", ["OSM", "  osm  ", "Osm"])
    def test_preset_name_case_and_whitespace_insensitive(self, geo, tmp_path, spelling):
        tiles = parse_config(render(geo, tmp_path, tiles=spelling))["tiles"]
        assert tiles["url"] == PRESET_URLS["osm"]

    @pytest.mark.parametrize("builder", BUILDERS)
    @pytest.mark.parametrize("tiles", ["terrain", "", "dark2", None, 5])
    def test_unknown_preset_raises(self, geo, tmp_path, builder, tiles):
        out = tmp_path / "t.html"
        with pytest.raises(InvalidArgumentError) as exc:
            getattr(geo, builder)(out, tiles=tiles)
        assert exc.value.name == "tiles"
        assert not out.exists()

    @pytest.mark.parametrize("old", ["voyager", "carto", "positron", "cartodb"])
    def test_removed_carto_names_explain_why(self, geo, tmp_path, old):
        with pytest.raises(InvalidArgumentError, match="CARTO basemaps were replaced"):
            geo.map(tmp_path / "t.html", tiles=old)

    @pytest.mark.parametrize("tiles", [["dark"], {"url": CUSTOM_TILES}])
    def test_unhashable_tiles_raise_invalid_argument(self, geo, tmp_path, tiles):
        with pytest.raises(InvalidArgumentError):
            geo.map(tmp_path / "t.html", tiles=tiles)

    @pytest.mark.parametrize("builder", BUILDERS)
    def test_attribution_is_appended_to_preset_credit(self, geo, tmp_path, builder):
        # The OpenStreetMap credit its licence requires is never dropped.
        cfg = parse_config(render(geo, tmp_path, builder, tiles="osm", attribution="My tiles"))
        attribution = cfg["tiles"]["options"]["attribution"]
        assert attribution.endswith(", My tiles")
        assert "openstreetmap.org/copyright" in attribution

    def test_attribution_on_none_preset(self, geo, tmp_path):
        cfg = parse_config(render(geo, tmp_path, tiles="none", attribution="Data: me"))
        assert cfg["tiles"]["options"]["attribution"] == "Data: me"

    def test_none_preset_draws_no_tile_layer(self, geo, tmp_path):
        tiles = parse_config(render(geo, tmp_path, tiles="none"))["tiles"]
        assert tiles["url"] is None
        assert tiles["needsReferer"] is False
        assert tiles["background"]

    def test_dark_preset_inverts_osm_and_darkens_background(self, geo, tmp_path):
        tiles = parse_config(render(geo, tmp_path, tiles="dark"))["tiles"]
        assert tiles["options"]["className"] == "qg-tiles-dark"
        assert tiles["background"] == "#0f111a"

    @pytest.mark.parametrize("cls", ["qg-tiles-light", "qg-tiles-dark"])
    def test_tile_style_classes_defined(self, geo, tmp_path, cls):
        assert re.search(r"\." + cls + r"\s*\{\s*filter:", render(geo, tmp_path))

    def test_tile_presets_lists_every_name(self):
        presets = tile_presets()
        assert list(presets) == list(_TILES)
        assert all(isinstance(v, str) and v for v in presets.values())


# ─────────────────────────────────────────────────────────────────────────────
# Basemaps — OpenStreetMap and pages opened from disk
# ─────────────────────────────────────────────────────────────────────────────

class TestFileNotice:
    @pytest.mark.parametrize("name", REFERER_PRESETS)
    def test_osm_policy_presets_need_a_referer(self, geo, tmp_path, name):
        tiles = parse_config(render(geo, tmp_path, tiles=name))["tiles"]
        assert tiles["needsReferer"] is True
        assert tiles["options"]["referrerPolicy"] == "strict-origin-when-cross-origin"

    @pytest.mark.parametrize("name", sorted(set(PRESET_URLS) - set(REFERER_PRESETS)))
    def test_other_presets_do_not(self, geo, tmp_path, name):
        assert parse_config(render(geo, tmp_path, tiles=name))["tiles"]["needsReferer"] is False

    def test_custom_tiles_do_not(self, geo, tmp_path):
        assert parse_config(render(geo, tmp_path, tiles=CUSTOM_TILES))["tiles"]["needsReferer"] is False

    @pytest.mark.parametrize("builder", BUILDERS)
    def test_notice_names_the_file_and_the_fixes(self, geo, tmp_path, builder):
        out = tmp_path / "my map.html"
        getattr(geo, builder)(out)
        notice = parse_config(read_page(out))["fileNotice"]
        assert '`qoregeo serve "my map.html"`' in notice
        assert "`geo.show()`" in notice
        assert "'satellite'" in notice and "'topo'" in notice

    @pytest.mark.parametrize("builder", BUILDERS)
    def test_tiles_not_requested_without_a_referer(self, geo, tmp_path, builder):
        page = render(geo, tmp_path, builder)
        assert "var blocked = t.needsReferer && !sendsReferer();" in page
        assert "if (blocked) showNotice(CONFIG.fileNotice);" in page
        assert "else if (t.url) L.tileLayer(t.url, t.options).addTo(map);" in page
        assert "textContent = part" in page   # never innerHTML

    def test_referer_check_uses_origin_and_base_url(self, geo):
        # A notebook's srcdoc iframe has location.protocol "about:" but the base
        # URL (and so the Referer, if any) of its parent: a file:// parent sends
        # none. A sandboxed iframe has an opaque origin and sends none either.
        # (Verified in Chromium: file page, http page, srcdoc in file/http parents.)
        page = geo.map_html()
        assert ('return self.origin !== "null" && '
                '/^https?:$/.test(new URL(document.baseURI).protocol);') in page
        assert 'location.protocol === "file:"' not in page

    def test_notebook_display_uses_the_same_check(self, geo):
        frame = geo._repr_html_()
        assert frame is not None and frame.startswith("<iframe srcdoc=")
        assert "!sendsReferer()" in html.unescape(frame)

    def test_html_string_notice_defaults_to_map_html(self, geo):
        assert "`qoregeo serve map.html`" in parse_config(geo.map_html())["fileNotice"]


def notice_command(notice: str) -> str:
    """The code span holding the ``qoregeo serve …`` command in a file notice."""
    mark = "\0" if "\0" in notice else "`"
    spans = notice.split(mark)[1::2]
    assert spans[1] == "geo.show()", spans
    return spans[0]


class TestFileNoticeQuoting:
    @pytest.mark.parametrize(("name", "shown"), [
        ("map.html", "map.html"),
        ("café_2026-v1.2.html", "café_2026-v1.2.html"),
        ("50%+a=b,c@d.html", "50%+a=b,c@d.html"),
        ("my map.html", '"my map.html"'),
        ("sales(2026).html", '"sales(2026).html"'),
        ("bob's.html", '"bob\'s.html"'),
        ("a&b|c;d<e>.html", '"a&b|c;d<e>.html"'),
        ("~home*?.html", '"~home*?.html"'),
        ("=x.html", '"=x.html"'),                  # zsh expands a leading '='
        ("a$HOME.html", "'a$HOME.html'"),
        ("a!b.html", "'a!b.html'"),                # bash history expansion
        ('say "hi".html', "'say \"hi\".html'"),
        ("it's $5.html", "'it'\"'\"'s $5.html'"),
        ("-x.html", "./-x.html"),                  # never read as an option
    ])
    def test_command_can_be_pasted(self, geo, name, shown):
        # Only the name is used, so no file is written (several of these
        # names are not valid on Windows).
        page, _ = map_page(geo.get_features(), output_path=f"out/{name}")
        command = notice_command(parse_config(page)["fileNotice"])
        assert command == f"qoregeo serve {shown}"
        expected = "./" + name if name.startswith("-") else name
        assert shlex.split(command) == ["qoregeo", "serve", expected]   # POSIX shell words

    @pytest.mark.parametrize("builder", BUILDERS)
    @pytest.mark.parametrize(("name", "shown"), [
        ("sales(2026).html", '"sales(2026).html"'),
        ("bob's $5.html", "'bob'\"'\"'s $5.html'"),
        ("-x.html", "./-x.html"),
    ])
    def test_written_file_notice(self, geo, tmp_path, builder, name, shown):
        out = tmp_path / name
        getattr(geo, builder)(out)
        notice = parse_config(read_page(out))["fileNotice"]
        assert notice_command(notice) == f"qoregeo serve {shown}"

    @pytest.mark.parametrize("builder", BUILDERS)
    def test_backtick_in_name_switches_the_code_marker(self, geo, tmp_path, builder):
        out = tmp_path / "a`b.html"
        getattr(geo, builder)(out)
        page = read_page(out)
        notice = parse_config(page)["fileNotice"]
        assert "\0" in notice and notice.count("`") == 1
        assert notice_command(notice) == "qoregeo serve 'a`b.html'"
        assert 'text.split(text.indexOf("\\u0000") >= 0 ? "\\u0000" : "`")' in page

    def test_name_with_spaces_still_double_quoted(self, geo, tmp_path):
        out = tmp_path / "india cities.html"
        geo.map(out)
        assert '`qoregeo serve "india cities.html"`' in parse_config(read_page(out))["fileNotice"]


# ─────────────────────────────────────────────────────────────────────────────
# The title panel never covers the basemap credit
# ─────────────────────────────────────────────────────────────────────────────

class TestBrandPanel:
    @pytest.mark.parametrize("builder", BUILDERS)
    def test_brand_kept_above_the_attribution(self, geo, tmp_path, builder):
        # On phones the credit wraps onto 2-3 lines; the fixed panel would sit
        # on top of it. (Verified in Chromium at 320-1280 px for every preset.)
        page = render(geo, tmp_path, builder)
        assert "function keepBrandClear(map) {" in page
        assert "map.attributionControl.getContainer()" in page
        assert "brand.style.bottom = (document.documentElement.clientHeight - c.top + 6)" in page
        assert 'window.addEventListener("resize", place);' in page
        assert "new ResizeObserver(place).observe(credit)" in page
        init = page.index("function baseMap(L) {")
        assert page.index("keepBrandClear(map);", init) < page.index("return map;", init)

    def test_brand_is_smaller_on_narrow_screens(self, geo):
        page = geo.map_html()
        media = page[page.index("@media (max-width: 480px) {"):]
        assert media.index("#brand {") < media.index("}")
        assert "font-size:12px" in media[:media.index("}")]


# ─────────────────────────────────────────────────────────────────────────────
# Basemaps — custom URL templates
# ─────────────────────────────────────────────────────────────────────────────

class TestCustomTiles:
    @pytest.mark.parametrize("builder", BUILDERS)
    def test_https_template_with_attribution(self, geo, tmp_path, builder):
        page = render(geo, tmp_path, builder, tiles=CUSTOM_TILES, attribution="&copy; Example")
        tiles = parse_config(page)["tiles"]
        assert tiles["url"] == CUSTOM_TILES
        assert tiles["name"] == "custom"
        assert tiles["options"]["attribution"] == "&copy; Example"
        assert tiles["options"]["maxZoom"] == 19

    def test_attribution_defaults_to_empty(self, geo, tmp_path):
        tiles = parse_config(render(geo, tmp_path, tiles=CUSTOM_TILES))["tiles"]
        assert tiles["options"]["attribution"] == ""

    def test_subdomains_only_for_s_placeholder(self, geo, tmp_path):
        url = "https://{s}.tiles.example.com/{z}/{x}/{y}.png"
        with_s = parse_config(render(geo, tmp_path, tiles=url))["tiles"]["options"]
        without_s = parse_config(render(geo, tmp_path, tiles=CUSTOM_TILES))["tiles"]["options"]
        assert with_s["subdomains"] == "abc"
        assert "subdomains" not in without_s

    def test_key_in_custom_url_kept_verbatim(self, geo, tmp_path):
        url = "https://tiles.example.com/{z}/{x}/{y}.png?api_key=abc"
        assert parse_config(render(geo, tmp_path, tiles=url))["tiles"]["url"] == url

    @pytest.mark.parametrize("builder", BUILDERS)
    @pytest.mark.parametrize("url", [
        "https://tiles.example.com/{x}/{y}.png",
        "https://tiles.example.com/{z}/{y}.png",
        "https://tiles.example.com/{z}/{x}.png",
        "https://tiles.example.com/tile.png",
    ])
    def test_template_missing_placeholders_rejected(self, geo, tmp_path, builder, url):
        out = tmp_path / "t.html"
        with pytest.raises(InvalidArgumentError) as exc:
            getattr(geo, builder)(out, tiles=url)
        assert exc.value.name == "tiles"
        assert not out.exists()

    @pytest.mark.parametrize("url", [
        "ftp://tiles.example.com/{z}/{x}/{y}.png",
        "javascript:alert('{z}{x}{y}')",
        "//tiles.example.com/{z}/{x}/{y}.png",
    ])
    def test_non_http_schemes_rejected(self, geo, tmp_path, url):
        with pytest.raises(InvalidArgumentError):
            geo.map(tmp_path / "t.html", tiles=url)

    def test_custom_tiles_never_warn(self, geo, tmp_path):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            page = render(geo, tmp_path, tiles=CUSTOM_TILES)
        assert parse_config(page)["tiles"]["url"] == CUSTOM_TILES

    @pytest.mark.parametrize("builder", BUILDERS)
    def test_tile_api_key_parameter_removed(self, geo, tmp_path, builder):
        with pytest.raises(TypeError):
            getattr(geo, builder)(tmp_path / "t.html", tile_api_key="k")


class TestCustomUrlOnOsmServers:
    """OpenStreetMap's own servers given as a custom URL get the presets' safeguards."""

    @pytest.mark.parametrize("builder", BUILDERS)
    @pytest.mark.parametrize("url", [
        OSM_URL,
        "  HTTPS://Tile.OpenStreetMap.org/{z}/{x}/{y}.png  ",
        "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "https://tile.openstreetmap.org./{z}/{x}/{y}.png?x=1",
    ])
    def test_osm_url_needs_referer_and_credits_osm(self, geo, tmp_path, builder, url):
        tiles = parse_config(render(geo, tmp_path, builder, tiles=url))["tiles"]
        assert tiles["name"] == "custom"
        assert tiles["url"] == url.strip()
        assert tiles["needsReferer"] is True
        assert tiles["options"]["referrerPolicy"] == "strict-origin-when-cross-origin"
        assert tiles["options"]["attribution"] == OSM_ATTRIBUTION

    def test_users_attribution_appended_to_osm_credit(self, geo, tmp_path):
        tiles = parse_config(render(geo, tmp_path, tiles=OSM_URL, attribution="Data: Acme"))["tiles"]
        assert tiles["options"]["attribution"] == f"{OSM_ATTRIBUTION}, Data: Acme"

    def test_openstreetmap_fr_servers(self, geo, tmp_path):
        url = "https://tile-{s}.openstreetmap.fr/hot/{z}/{x}/{y}.png"
        tiles = parse_config(render(geo, tmp_path, tiles=url))["tiles"]
        assert tiles["needsReferer"] is True
        assert tiles["options"]["subdomains"] == "abc"
        attribution = tiles["options"]["attribution"]
        assert attribution.startswith(f"&copy; {OSM_COPYRIGHT_LINK} contributors")
        assert "https://openstreetmap.fr/" in attribution

    @pytest.mark.parametrize("url", [
        "https://tile.openstreetmap.org.example.com/{z}/{x}/{y}.png",
        "https://notopenstreetmap.org/{z}/{x}/{y}.png",
        "https://tile.openstreetmap.org@tiles.example.com/{z}/{x}/{y}.png",
        "https://tiles.example.com/tile.openstreetmap.org/{z}/{x}/{y}.png",
        "https://tile.openstreetmap.de/{z}/{x}/{y}.png",
        "https://[tile.openstreetmap.org/{z}/{x}/{y}.png",
    ])
    def test_other_hosts_untouched(self, geo, tmp_path, url):
        tiles = parse_config(render(geo, tmp_path, tiles=url))["tiles"]
        assert tiles["needsReferer"] is False
        assert tiles["options"] == {"maxZoom": 19, "attribution": ""}

    def test_saved_map_prints_how_to_view_it(self, tmp_path, capsys):
        out = tmp_path / "osm.html"
        GeoEngine(verbose=True).add_point(28.6, 77.2).map(out, tiles=OSM_URL)
        assert "qoregeo serve" in capsys.readouterr().out


# ─────────────────────────────────────────────────────────────────────────────
# Attribution
# ─────────────────────────────────────────────────────────────────────────────

class TestAttribution:
    @pytest.mark.parametrize("builder", BUILDERS)
    @pytest.mark.parametrize("name", OSM_PRESETS)
    def test_osm_attribution_links_copyright_page(self, geo, tmp_path, builder, name):
        options = parse_config(render(geo, tmp_path, builder, tiles=name))["tiles"]["options"]
        assert options["attribution"] == OSM_ATTRIBUTION

    @pytest.mark.parametrize("name", sorted(set(PRESET_URLS) - {"none", "satellite"}))
    def test_every_osm_basemap_credits_openstreetmap(self, geo, tmp_path, name):
        options = parse_config(render(geo, tmp_path, tiles=name))["tiles"]["options"]
        assert OSM_COPYRIGHT_LINK in options["attribution"]

    @pytest.mark.parametrize(("name", "credit"), [
        ("humanitarian", "hotosm.org"), ("topo", "opentopomap.org"), ("cyclosm", "CyclOSM"),
        ("osm-fr", "openstreetmap.fr"), ("osm-de", "openstreetmap.de"), ("satellite", "NASA GIBS"),
    ])
    def test_style_authors_credited(self, geo, tmp_path, name, credit):
        options = parse_config(render(geo, tmp_path, tiles=name))["tiles"]["options"]
        assert credit in options["attribution"]

    def test_osm_tiles_use_single_host_without_subdomains(self, geo, tmp_path):
        tiles = parse_config(render(geo, tmp_path, tiles="osm"))["tiles"]
        assert "{s}" not in tiles["url"]
        assert "subdomains" not in tiles["options"]
        assert tiles["options"]["maxZoom"] == 19

    @pytest.mark.parametrize("name", sorted(set(PRESET_URLS) - {"none"}))
    def test_max_zoom_set(self, geo, tmp_path, name):
        options = parse_config(render(geo, tmp_path, tiles=name))["tiles"]["options"]
        assert 10 <= options["maxZoom"] <= 20

    def test_satellite_overzooms_native_level_8(self, geo, tmp_path):
        options = parse_config(render(geo, tmp_path, tiles="satellite"))["tiles"]["options"]
        assert options["maxNativeZoom"] == 8

    @pytest.mark.parametrize("name", sorted(set(PRESET_URLS) - {"none"}))
    def test_tile_urls_are_https_and_keyless(self, geo, tmp_path, name):
        url = parse_config(render(geo, tmp_path, tiles=name))["tiles"]["url"]
        assert url.startswith("https://") and "key" not in url.lower()

    def test_osm_url_is_exactly_the_policy_url(self):
        # The OSMF policy requires this exact URL (no {s} subdomains, no http).
        assert {_TILES[n]["url"] for n in OSM_PRESETS} == {"https://tile.openstreetmap.org/{z}/{x}/{y}.png"}


# ─────────────────────────────────────────────────────────────────────────────
# Overlays
# ─────────────────────────────────────────────────────────────────────────────

class TestOverlays:
    def test_buffer_overlay_embedded(self, geo, tmp_path):
        zone = geo.buffer((28.6139, 77.2090), radius=50, num_points=16)
        overlays = parse_config(render(geo, tmp_path, overlays=[zone]))["overlays"]
        assert len(overlays) == 1
        assert overlays[0]["type"] == "Polygon"
        assert overlays[0]["coordinates"] == zone["coordinates"]
        assert overlays[0]["_center"] == [28.6139, 77.2090]   # tuple -> JSON array

    def test_several_overlays_kept_in_order(self, geo, tmp_path):
        feature = {"type": "Feature", "properties": {},
                   "geometry": {"type": "Point", "coordinates": [72.8777, 19.076]}}
        zone = geo.buffer((12.9716, 77.5946), radius=5)
        overlays = parse_config(render(geo, tmp_path, overlays=[zone, feature]))["overlays"]
        assert [o["type"] for o in overlays] == ["Polygon", "Feature"]

    @pytest.mark.parametrize("wrap", [tuple, iter], ids=["tuple", "iterator"])
    def test_any_iterable_of_overlays(self, geo, tmp_path, wrap):
        zone = geo.buffer((28.6139, 77.2090), radius=10)
        overlays = parse_config(render(geo, tmp_path, overlays=wrap([zone, zone])))["overlays"]
        assert len(overlays) == 2

    @pytest.mark.parametrize("value", [None, [], ()])
    def test_no_overlays(self, geo, tmp_path, value):
        assert parse_config(render(geo, tmp_path, overlays=value))["overlays"] == []

    @pytest.mark.parametrize("item", [42, "zone", None, {"coordinates": []}, [[77.2, 28.6]]])
    def test_invalid_overlay_rejected(self, geo, tmp_path, item):
        out = tmp_path / "o.html"
        with pytest.raises(InvalidArgumentError) as exc:
            geo.map(out, overlays=[item])
        assert exc.value.name == "overlays[0]"
        assert not out.exists()

    def test_error_names_offending_index(self, geo, tmp_path):
        zone = geo.buffer((28.6139, 77.2090), radius=10)
        with pytest.raises(InvalidArgumentError) as exc:
            geo.map(tmp_path / "o.html", overlays=[zone, zone, "oops"])
        assert exc.value.name == "overlays[2]"
        assert exc.value.value == "oops"

    def test_bare_shape_accepted_as_single_overlay(self, geo, tmp_path):
        zone = geo.buffer((28.6139, 77.2090), radius=10)
        out = tmp_path / "o.html"
        geo.map(out, overlays=zone)
        cfg = parse_config(out.read_text(encoding="utf-8"))
        assert len(cfg["overlays"]) == 1
        assert cfg["overlays"][0]["type"] == "Polygon"

    def test_non_iterable_overlays_rejected(self, geo, tmp_path):
        with pytest.raises(InvalidArgumentError):
            geo.map(tmp_path / "o.html", overlays=5)


# ─────────────────────────────────────────────────────────────────────────────
# Heatmap — intensity weights
# ─────────────────────────────────────────────────────────────────────────────

class TestHeatmapIntensity:
    def test_dropped_rows_warning(self, tmp_path):
        with pytest.warns(UserWarning, match=r"2 rows .* 'w' value \(-1, 'x'\) add no heat\.$"):
            render(weighted(10, -1, "x"), tmp_path, "heatmap", intensity_col="w")
        with pytest.warns(UserWarning, match=r"1 row .* adds no heat, so the heatmap is empty"):
            render(weighted(0), tmp_path, "heatmap", intensity_col="w")

    def test_unweighted_points_are_lat_lng_one(self, geo, tmp_path):
        points = parse_config(render(geo, tmp_path, "heatmap"))["points"]
        assert points == [[lat, lng, 1.0] for _, lat, lng, _ in CITIES]

    def test_weights_normalised_to_max(self, tmp_path):
        points = parse_config(render(weighted(10, 20, 40), tmp_path, "heatmap", intensity_col="w"))[
            "points"]
        assert [p[2] for p in points] == pytest.approx([0.25, 0.5, 1.0])

    def test_weights_lie_in_unit_interval(self, geo, tmp_path):
        points = parse_config(render(geo, tmp_path, "heatmap", intensity_col="population"))[
            "points"]
        weights = [p[2] for p in points]
        assert all(0 < w <= 1 for w in weights)
        assert max(weights) == 1.0
        assert weights[CITIES.index(("Pune", 18.5204, 73.8567, 7400000))] == \
            pytest.approx(7400000 / 32900000)

    @pytest.mark.parametrize("bad", [
        -5, 0, 0.0, None, "abc", "", float("nan"), float("inf"), float("-inf"), True, False,
        [1], {"v": 1}, _MISSING,
    ], ids=lambda v: "missing" if v is _MISSING else repr(v))
    def test_invalid_weights_dropped(self, tmp_path, bad):
        cfg = parse_config(render(weighted(10, bad, 40), tmp_path, "heatmap", intensity_col="w"))
        assert cfg["points"] == [[20.0, 70.0, 0.25], [22.0, 72.0, 1.0]]

    def test_numeric_strings_and_decimals_accepted(self, tmp_path):
        g = weighted(" 5 ", "1e1", decimal.Decimal("20"))
        points = parse_config(render(g, tmp_path, "heatmap", intensity_col="w"))["points"]
        assert [p[2] for p in points] == pytest.approx([0.25, 0.5, 1.0])

    def test_csv_string_values_used_as_weights(self, tmp_path):
        src = tmp_path / "stores.csv"
        with open(src, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["name", "latitude", "longitude", "sales"])
            writer.writerow(["A", "10.0", "20.0", "100"])
            writer.writerow(["B", "11.0", "21.0", ""])
            writer.writerow(["C", "12.0", "22.0", "n/a"])
            writer.writerow(["D", "13.0", "23.0", "400"])
        g = GeoEngine(verbose=False).load(src)
        points = parse_config(render(g, tmp_path, "heatmap", intensity_col="sales"))["points"]
        assert points == [[10.0, 20.0, 0.25], [13.0, 23.0, 1.0]]

    def test_all_weights_dropped_gives_no_points(self, tmp_path):
        cfg = parse_config(render(weighted(-1, 0, "x"), tmp_path, "heatmap", intensity_col="w"))
        assert cfg["points"] == []
        assert cfg["bounds"] is None

    def test_bounds_cover_only_kept_points(self, tmp_path):
        cfg = parse_config(render(weighted(5, -1, 5, 0, 5), tmp_path, "heatmap",
                                  intensity_col="w"))
        assert cfg["bounds"] == [[20.0, 70.0], [24.0, 74.0]]

    def test_missing_column_raises(self, geo, tmp_path):
        out = tmp_path / "h.html"
        with pytest.raises(ColumnNotFoundError) as exc:
            geo.heatmap(out, intensity_col="sales")
        assert isinstance(exc.value, LookupError)
        assert exc.value.column == "sales"
        assert exc.value.available == ["name", "population"]
        assert not out.exists()

    def test_missing_column_error_names_source_file(self, tmp_path):
        src = tmp_path / "pts.csv"
        with open(src, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([["lat", "lng", "name"], ["1", "2", "a"]])
        with pytest.raises(ColumnNotFoundError) as exc:
            GeoEngine(verbose=False).load(src).heatmap(tmp_path / "h.html", intensity_col="w")
        assert exc.value.filepath == str(src)

    def test_missing_column_on_empty_dataset_is_fine(self, empty, tmp_path):
        out = tmp_path / "h.html"
        empty.heatmap(out, intensity_col="sales")
        assert parse_config(read_page(out))["points"] == []

    def test_column_present_on_some_features_only(self, tmp_path):
        cfg = parse_config(render(weighted(_MISSING, 8, _MISSING), tmp_path, "heatmap",
                                  intensity_col="w"))
        assert cfg["points"] == [[21.0, 71.0, 1.0]]

    def test_polygon_contributes_its_centroid(self, tmp_path):
        g = GeoEngine(verbose=False).load_data([{
            "type": "Feature", "properties": {"w": 3},
            "geometry": {"type": "Polygon",
                         "coordinates": [[[10, 40], [12, 40], [12, 42], [10, 42], [10, 40]]]},
        }])
        points = parse_config(render(g, tmp_path, "heatmap", intensity_col="w"))["points"]
        assert points == [[pytest.approx(41.0, abs=1e-6), pytest.approx(11.0, abs=1e-6), 1.0]]

    def test_null_geometry_skipped(self, tmp_path):
        g = GeoEngine(verbose=False).load_data([
            {"type": "Feature", "geometry": None, "properties": {"w": 100}},
            {"type": "Feature", "properties": {"w": 4},
             "geometry": {"type": "Point", "coordinates": [77.2, 28.6]}},
        ])
        cfg = parse_config(render(g, tmp_path, "heatmap", intensity_col="w"))
        assert cfg["points"] == [[28.6, 77.2, 1.0]]


# ─────────────────────────────────────────────────────────────────────────────
# Heatmap — radius / blur
# ─────────────────────────────────────────────────────────────────────────────

class TestHeatmapRadiusBlur:
    @pytest.mark.parametrize(("kwargs", "expected"), [
        ({}, (25, 18)),
        ({"radius": 1, "blur": 0}, (1, 0)),
        ({"radius": 40, "blur": 30}, (40, 30)),
    ], ids=["defaults", "minimums", "custom"])
    def test_values_embedded(self, geo, tmp_path, kwargs, expected):
        heat = parse_config(render(geo, tmp_path, "heatmap", **kwargs))["heat"]
        assert (heat["radius"], heat["blur"]) == expected

    @pytest.mark.parametrize("radius", [0, -1, 2.5, "25", True, None])
    def test_invalid_radius(self, geo, tmp_path, radius):
        out = tmp_path / "h.html"
        with pytest.raises(InvalidArgumentError) as exc:
            geo.heatmap(out, radius=radius)
        assert exc.value.name == "radius"
        assert not out.exists()

    @pytest.mark.parametrize("blur", [-1, 1.5, "18", True, None])
    def test_invalid_blur(self, geo, tmp_path, blur):
        out = tmp_path / "h.html"
        with pytest.raises(InvalidArgumentError) as exc:
            geo.heatmap(out, blur=blur)
        assert exc.value.name == "blur"
        assert not out.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Verbose output
# ─────────────────────────────────────────────────────────────────────────────

class TestVerboseOutput:
    @pytest.mark.parametrize("builder", BUILDERS)
    def test_quiet_engine_prints_nothing(self, geo, tmp_path, capsys, builder):
        getattr(geo, builder)(tmp_path / "q.html", tiles="osm")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_map_prints_one_line(self, geo, tmp_path, capsys):
        geo.verbose = True
        out = tmp_path / "loud.html"
        geo.map(out, tiles="osm")
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 1
        assert "Map saved" in lines[0]
        assert str(out) in lines[0]
        assert f"({len(CITIES)} features)" in lines[0]

    def test_heatmap_prints_point_count(self, tmp_path, capsys):
        g = weighted(1, -1, 3)
        g.verbose = True
        g.heatmap(tmp_path / "loud.html", intensity_col="w", tiles="osm")
        out = capsys.readouterr().out
        assert "Heatmap saved" in out
        assert "(2 points)" in out

    def test_default_engine_is_verbose(self, tmp_path, capsys):
        GeoEngine().add_point(1.0, 2.0).map(tmp_path / "d.html", tiles="osm")
        assert "Map saved" in capsys.readouterr().out

    def test_filtered_engine_inherits_verbose(self, tmp_path, capsys):
        loud = GeoEngine(verbose=True).add_point(1.0, 2.0, k="a").add_point(3.0, 4.0, k="b")
        loud.filter("k", "a").map(tmp_path / "f.html", tiles="osm")
        assert "(1 feature)" in capsys.readouterr().out
        quiet = GeoEngine(verbose=False).add_point(1.0, 2.0, k="a")
        quiet.filter("k", "a").map(tmp_path / "f.html", tiles="osm")
        assert capsys.readouterr().out == ""


# ─────────────────────────────────────────────────────────────────────────────
# _announce — status line that survives non-UTF-8 consoles
# ─────────────────────────────────────────────────────────────────────────────

def _cp1252_stdout(monkeypatch: pytest.MonkeyPatch) -> tuple[io.TextIOWrapper, io.BytesIO]:
    """Replace sys.stdout with a strict cp1252 stream (like a legacy Windows console)."""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", newline="\n")
    monkeypatch.setattr(sys, "stdout", stream)
    return stream, raw


class TestAnnounce:
    def test_silent_when_not_verbose(self, capsys):
        _announce("\u2705  Map saved \u2192 x.html", False)
        assert capsys.readouterr().out == ""

    def test_prints_message_verbatim(self, capsys):
        _announce("\u2705  Map saved \u2192 x.html", True)
        assert capsys.readouterr().out == "\u2705  Map saved \u2192 x.html\n"

    def test_ascii_fallback_on_cp1252_console(self, monkeypatch):
        stream, raw = _cp1252_stdout(monkeypatch)
        _announce("\u2705  Map saved \u2192 caf\u00e9.html", True)
        stream.flush()
        # Characters the console can show (é in cp1252) are kept.
        assert raw.getvalue() == "[OK]  Map saved -> café.html\n".encode("cp1252")

    def test_encodable_message_unchanged_on_cp1252_console(self, monkeypatch):
        stream, raw = _cp1252_stdout(monkeypatch)
        _announce("Map saved -> caf\u00e9.html", True)
        stream.flush()
        assert raw.getvalue().decode("cp1252") == "Map saved -> caf\u00e9.html\n"

    @pytest.mark.parametrize(("builder", "label"), [("map", "Map saved"),
                                                     ("heatmap", "Heatmap saved")])
    def test_engine_does_not_crash_on_cp1252_console(self, tmp_path, monkeypatch, builder, label):
        stream, raw = _cp1252_stdout(monkeypatch)
        g = GeoEngine(verbose=True).add_point(28.6139, 77.2090, name="Delhi")
        out = tmp_path / "console.html"
        getattr(g, builder)(out, tiles="osm")
        stream.flush()
        text = raw.getvalue().decode("cp1252")
        assert text.startswith(f"[OK]  {label} ")
        assert "\u2705" not in text
        assert out.is_file()


# ─────────────────────────────────────────────────────────────────────────────
# White-box: map_builder._json_safe
# ─────────────────────────────────────────────────────────────────────────────

class TestJsonSafe:
    @pytest.mark.parametrize(("value", "expected"), [
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
        (1.5, 1.5),
        (-0.0, -0.0),
        (7, 7),
        (True, True),
        ("NaN", "NaN"),
        (None, None),
    ])
    def test_scalars(self, value, expected):
        result = _json_safe(value)
        assert result == expected
        assert type(result) is type(expected)

    def test_nested_containers(self):
        data = {"a": [1.0, float("nan"), {"b": float("inf")}], "c": {"d": (float("-inf"), (2,))}}
        assert _json_safe(data) == {"a": [1.0, None, {"b": None}], "c": {"d": [None, [2]]}}

    def test_key_order_preserved_and_keys_stringified(self):
        assert list(_json_safe({"z": 1, 3: float("nan"), "a": 2})) == ["z", "3", "a"]

    def test_input_not_mutated(self):
        inner = [float("nan")]
        data = {"x": inner}
        _json_safe(data)
        assert data["x"] is inner
        assert math.isnan(inner[0])

    def test_other_objects_converted(self):
        assert _json_safe({2.0, 1.0, float("nan")}) == [1.0, 2.0, None]
        assert _json_safe(datetime.date(2026, 1, 1)) == "2026-01-01"
        assert _json_safe(decimal.Decimal("NaN")) is None
        assert _json_safe(2 ** 60) == 2 ** 60
        assert _json_safe(2 ** 60, js=True) == str(2 ** 60)


# ─────────────────────────────────────────────────────────────────────────────
# White-box: map_builder._to_js
# ─────────────────────────────────────────────────────────────────────────────

def unwrap(text: str) -> str:
    """The JSON text inside ``JSON.parse("…")``."""
    assert text.startswith("JSON.parse(") and text.endswith(")"), text
    inner: str = json.loads(text[len("JSON.parse("):-1])
    return inner


class TestToJs:
    @pytest.mark.parametrize(("value", "expected"), [
        ("</script>", '"</script>"'),
        ("a&b", '"a&b"'),
        ("<!--", '"<!--"'),
        ("\u2028\u2029", '"\u2028\u2029"'),
        ("é東😀", '"é東😀"'),
        ({"<k>": 1}, '{"<k>": 1}'),
        (float("nan"), "null"),
        ({"x": float("inf")}, '{"x": null}'),
    ])
    def test_exact_json(self, value, expected):
        assert unwrap(_to_js(value)) == expected

    @pytest.mark.parametrize(("value", "expected"), [
        ("</script>", 'JSON.parse("\\"\\u003c/script\\u003e\\"")'),
        ("a&b", 'JSON.parse("\\"a\\u0026b\\"")'),
        ("\u2028", 'JSON.parse("\\"\\u2028\\"")'),
        ({"a": 1}, 'JSON.parse("{\\"a\\": 1}")'),
    ])
    def test_exact_literal(self, value, expected):
        assert _to_js(value) == expected

    def test_output_round_trips_as_json(self):
        obj = {"html": XSS, "amp": "&", "list": [1, 2.5, None, True], "nested": {"q": '"'}}
        assert json.loads(unwrap(_to_js(obj))) == obj

    def test_proto_key_is_plain_data(self):
        # An object literal would turn "__proto__" into the prototype; JSON.parse keeps it.
        obj = {"__proto__": {"polluted": 1}, "constructor": "x"}
        assert _to_js(obj).startswith("JSON.parse(")
        assert json.loads(unwrap(_to_js(obj))) == obj

    def test_lone_surrogates_escaped(self):
        text = _to_js({"s": "a\udc80b"})
        text.encode("utf-8")   # no unencodable code points in the page
        assert "\\\\udc80" in text

    def test_dates_times_decimals_sets(self):
        obj = {
            "dt": datetime.datetime(2026, 10, 19, 8, 30),
            "d": datetime.date(2026, 10, 19),
            "t": datetime.time(8, 30, 1),
            "dec": decimal.Decimal("0.1"),
            "set": {"b", "a"},
            "fs": frozenset({2, 1}),
        }
        assert json.loads(unwrap(_to_js(obj))) == {
            "dt": "2026-10-19T08:30:00", "d": "2026-10-19", "t": "08:30:01",
            "dec": 0.1, "set": ["a", "b"], "fs": [1, 2],
        }

    def test_set_order_is_deterministic(self):
        # Sorted by str() so mixed types never raise and output is stable.
        assert unwrap(_to_js({10, 9})) == "[10, 9]"
        assert unwrap(_to_js({1, "a"})) == '[1, "a"]'

    def test_unknown_objects_fall_back_to_str(self):
        assert unwrap(_to_js(complex(1, 2))) == '"(1+2j)"'
        assert unwrap(_to_js(Path("a"))) == json.dumps(str(Path("a")))

    def test_non_string_keys_become_strings(self):
        assert json.loads(unwrap(_to_js({1: "a", None: "b"}))) == {"1": "a", "null": "b"}

    def test_randomised_strings_never_leak_dangerous_chars(self):
        rng = random.Random(20261019)
        alphabet = "<>&/\\\"' \n\t\u2028\u2029aZ!-é😀"
        for _ in range(300):
            s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
            obj = {s: [s, {"k": s}]}
            text = _to_js(obj)
            assert not set(text) & set("<>&\n\u2028\u2029"), repr(s)
            assert json.loads(unwrap(text)) == obj


# ─────────────────────────────────────────────────────────────────────────────
# Deeply nested values
# ─────────────────────────────────────────────────────────────────────────────

def nested(depth: int, kind: str = "dict") -> Any:
    value: Any = 1
    for _ in range(depth):
        value = {"a": value} if kind == "dict" else [value]
    return value


def deep_error(call: Any) -> InvalidArgumentError:
    """
    Run ``call`` and return the InvalidArgumentError it raises. A RecursionError
    fails the test at once: pytest takes ~1 minute to format one whose frames
    hold a deeply nested value.
    """
    try:
        call()
    except InvalidArgumentError as e:
        return e
    except RecursionError:
        escaped = "a bare RecursionError"
    else:
        escaped = "no error"
    pytest.fail(f"expected InvalidArgumentError, got {escaped}")


class TestDeepNesting:
    """Values too deep to serialise raise a clean error naming them (not RecursionError)."""

    @pytest.mark.parametrize("kind", ["dict", "list"])
    def test_deep_property_named_in_error(self, kind):
        geo = single(ok=1, deep=nested(5000, kind))
        error = deep_error(geo.map_html)
        assert error.name == "features[0]['properties']['deep']"
        message = str(error)
        assert "more than 1000 levels deep" in message
        assert "del geo.get_features()[0]['properties']['deep']" in message

    def test_map_writes_nothing(self, tmp_path):
        out = tmp_path / "deep.html"
        deep_error(lambda: single(deep=nested(5000)).map(out))
        assert not out.exists()

    def test_self_referencing_property(self):
        loop: dict[str, Any] = {"x": 1}
        loop["self"] = loop
        error = deep_error(single(loop=loop).map_html)
        assert error.name == "features[0]['properties']['loop']"
        assert "containing itself" in str(error)

    def test_reports_the_deepest_of_several_features(self):
        geo = GeoEngine(verbose=False).add_point(1, 2, a=nested(200)).add_point(3, 4, b=nested(5000))
        assert deep_error(geo.map_html).name == "features[1]['properties']['b']"

    def test_deep_foreign_member(self):
        geo = GeoEngine(verbose=False).load_data([{
            "type": "Feature", "geometry": {"type": "Point", "coordinates": [77.2, 28.6]},
            "properties": {},
        }])
        geo.get_features()[0]["meta"] = nested(5000)
        assert deep_error(geo.map_html).name == "features[0]['meta']"

    def test_deep_overlay(self, geo):
        overlay = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [77.2, 28.6]},
                   "properties": {"deep": nested(5000)}}
        assert deep_error(lambda: geo.map_html(overlays=[overlay])).name == "overlays[0]"

    def test_fixing_the_property_as_suggested_works(self):
        geo = single(ok=1, deep=nested(5000))
        deep_error(geo.map_html)
        del geo.get_features()[0]["properties"]["deep"]
        assert props_of(geo.map_html()) == {"ok": 1}

    def test_moderately_deep_values_round_trip(self):
        value = nested(150, "list")
        assert props_of(single(v=value).map_html())["v"] == value

    def test_heatmap_does_not_embed_properties(self):
        assert "L.heatLayer" in single(deep=nested(5000)).heatmap_html()

    def test_notebook_display_falls_back_to_text(self):
        assert single(deep=nested(5000))._repr_html_() is None

    def test_unrelated_recursion_error_is_not_masked(self, geo, monkeypatch):
        def boom(obj: Any) -> str:
            raise RecursionError("elsewhere")
        monkeypatch.setattr("qoregeo.map_builder._to_js", boom)
        with pytest.raises(RecursionError, match="elsewhere"):
            geo.map_html()

    @pytest.mark.parametrize(("value", "depth"), [
        (1, 0), ("abc", 0), ([], 1), ({}, 1), ([1, [2, [3]]], 3),
        ({"a": {"b": [1]}, "c": 2}, 3), ({1, 2}, 1), ((1, (2,)), 2),
    ])
    def test_nesting_depth(self, value, depth):
        assert _nesting_depth(value) == depth

    def test_nesting_depth_is_capped_and_iterative(self):
        assert _nesting_depth(nested(50_000)) == 1001
        assert _nesting_depth(nested(50_000), cap=10) == 11
        loop: list[Any] = []
        loop.append(loop)
        assert _nesting_depth(loop) == 1001

    def test_nesting_depth_of_shared_containers_is_fast(self):
        shared: Any = 1
        for _ in range(400):              # 2**400 paths, 800 containers
            shared = [shared, {"k": shared}]
        assert _nesting_depth(shared) == 800


# ─────────────────────────────────────────────────────────────────────────────
# Near-world-wide shapes vs shapes crossing the antimeridian
# ─────────────────────────────────────────────────────────────────────────────

def box(west: float, east: float, south: float = -85.0, north: float = 85.0) -> dict[str, Any]:
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    return {"type": "Polygon", "coordinates": [ring]}


def lngs_of(geometry: dict[str, Any]) -> list[float]:
    out: list[float] = []

    def walk(c: Any) -> None:
        if isinstance(c, list) and c and not isinstance(c[0], list):
            out.append(c[0])
        elif isinstance(c, list):
            for x in c:
                walk(x)

    walk(geometry["coordinates"])
    return out


def feature(geometry: dict[str, Any]) -> dict[str, Any]:
    return {"type": "Feature", "geometry": geometry, "properties": {}}


# A track crossing 180° with a vertex every 0.1°, and an island straddling it:
# both have vertices within 0.1° of ±180°, so their raw span is ~359.9°.
DATELINE_TRACK = {"type": "LineString", "coordinates": [
    [round(179.0 + i / 10 - (360 if i > 10 else 0), 1), -17.0] for i in range(25)]}
TAVEUNI = {"type": "Polygon", "coordinates": [[
    [179.83, -16.7], [179.95, -16.65], [-179.97, -16.75], [-179.87, -16.9], [179.9, -17.05],
    [179.83, -16.7]]]}


class TestNearWorldShapes:
    @pytest.mark.parametrize("inset", [1e-4, 1e-2, 0.4])
    def test_extent_box_inset_from_180_stays_world_wide(self, inset):
        shape = box(-180 + inset, 180 - inset)
        cfg = parse_config(GeoEngine(verbose=False).load_data([feature(shape)]).map_html())
        assert cfg["worldCopyJump"] is True
        assert cfg["data"]["features"][0]["geometry"] == shape

    def test_extent_box_with_cities_stays_world_wide(self):
        shape = box(-179.9999, 179.9999)
        geo = GeoEngine(verbose=False).load_data([feature(shape)])
        geo.add_point(48.85, 2.35).add_point(35.7, 139.7)          # Paris, Tokyo
        cfg = parse_config(geo.map_html())
        assert cfg["worldCopyJump"] is True
        assert cfg["data"]["features"][0]["geometry"] == shape

    def test_extent_box_overlay_stays_world_wide(self):
        geo = GeoEngine(verbose=False).add_point(-17.0, 179.5).add_point(-17.5, -179.5)
        cfg = parse_config(geo.map_html(overlays=[box(-179.9999, 179.9999)]))
        assert cfg["worldCopyJump"] is True
        assert sorted(lngs_of(cfg["data"]["features"][0]["geometry"])
                      + lngs_of(cfg["data"]["features"][1]["geometry"])) == [-179.5, 179.5]

    @pytest.mark.parametrize("shape", [DATELINE_TRACK, TAVEUNI], ids=["track", "island"])
    def test_shapes_crossing_180_are_still_one_cluster(self, shape):
        cfg = parse_config(GeoEngine(verbose=False).load_data([feature(shape)]).map_html())
        assert cfg["worldCopyJump"] is False
        drawn = lngs_of(cfg["data"]["features"][0]["geometry"])
        assert 178.9 <= min(drawn) and max(drawn) <= 181.5

    def test_narrow_box_across_180_is_still_one_cluster(self):
        # Only two meridians, but 20° wide across ±180°, not 340° the other way.
        cfg = parse_config(GeoEngine(verbose=False).load_data([feature(box(170, -170))]).map_html())
        assert cfg["worldCopyJump"] is False
        assert sorted(set(lngs_of(cfg["data"]["features"][0]["geometry"]))) == [170, 190]

    @pytest.mark.parametrize(("geometry", "expected"), [
        (box(-180, 180), True),
        (box(-179.9999, 179.9999), True),
        (box(-179.5, 179.5), True),
        (box(-179.4, 179.4), False),                     # 358.8°: a 1.2° box across 180°
        (box(170, -170), False),
        ({"type": "LineString", "coordinates": [[-179.9, 10], [179.9, 10]]}, True),
        ({"type": "MultiLineString", "coordinates": [[[170, 0], [180, 0]], [[-180, 0], [-170, 0]]]},
         False),
        (DATELINE_TRACK, False),
        (TAVEUNI, False),
        ({"type": "Point", "coordinates": [179.9, 0]}, False),
        ({"type": "GeometryCollection", "geometries": [box(-179.9999, 179.9999)]}, True),
    ])
    def test_spans_world(self, geometry, expected):
        assert _spans_world([geometry]) is expected
