"""
tests/test_maps.py
==================
HTML map and heatmap generation — static analysis of the generated files.

No browser and no network: every test writes a page into ``tmp_path`` and
inspects its text (and the ``var CONFIG = {...};`` JSON it embeds).

Covers file output, title escaping, script-injection safety of the embedded
data, NaN / non-JSON property values, the pinned Subresource Integrity hashes
and CDN fallbacks, the view (center / zoom) config, basemap presets and
custom tile URLs, CARTO API-key handling, overlays, heatmap intensity
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
import io
import json
import math
import random
import re
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
    CARTO_KEY_ENV,
    _announce,
    _json_safe,
    _to_js,
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

PRESET_URLS = {
    "dark":    "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
    "light":   "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
    "voyager": "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
    "osm":     "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
}
CARTO_PRESETS = ["dark", "light", "voyager"]

OSM_COPYRIGHT_LINK = '<a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
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

_CONFIG_RE = re.compile(r"^[ \t]*var CONFIG = (.*);[ \t]*$", re.MULTILINE)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers + fixtures
# ─────────────────────────────────────────────────────────────────────────────

def read_page(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def config_text(page: str) -> str:
    """Return the raw JSON literal assigned to ``var CONFIG`` (exactly one)."""
    found = _CONFIG_RE.findall(page)
    assert len(found) == 1, "expected exactly one 'var CONFIG = …;' line"
    return found[0]


def parse_config(page: str) -> dict[str, Any]:
    return json.loads(config_text(page))


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


@pytest.fixture(autouse=True)
def _no_carto_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a developer's / CI's real CARTO key leak into these tests."""
    monkeypatch.delenv(CARTO_KEY_ENV, raising=False)


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
# Basemaps — presets
# ─────────────────────────────────────────────────────────────────────────────

class TestTilePresets:
    @pytest.mark.parametrize("name", list(PRESET_URLS))
    def test_preset_url(self, geo, tmp_path, name):
        tiles = parse_config(render(geo, tmp_path, tiles=name))["tiles"]
        assert tiles["url"] == PRESET_URLS[name]

    def test_default_is_dark(self, geo, tmp_path):
        assert parse_config(render(geo, tmp_path))["tiles"]["url"] == PRESET_URLS["dark"]

    @pytest.mark.parametrize("spelling", ["OSM", "  osm  ", "Osm"])
    def test_preset_name_case_and_whitespace_insensitive(self, geo, tmp_path, spelling):
        tiles = parse_config(render(geo, tmp_path, tiles=spelling))["tiles"]
        assert tiles["url"] == PRESET_URLS["osm"]

    @pytest.mark.parametrize("builder", BUILDERS)
    @pytest.mark.parametrize("tiles", ["satellite", "", "dark2", None, 5])
    def test_unknown_preset_raises(self, geo, tmp_path, builder, tiles):
        out = tmp_path / "t.html"
        with pytest.raises(InvalidArgumentError) as exc:
            getattr(geo, builder)(out, tiles=tiles)
        assert exc.value.name == "tiles"
        assert not out.exists()

    @pytest.mark.parametrize("tiles", [["dark"], {"url": CUSTOM_TILES}])
    def test_unhashable_tiles_raise_invalid_argument(self, geo, tmp_path, tiles):
        with pytest.raises(InvalidArgumentError):
            geo.map(tmp_path / "t.html", tiles=tiles)

    @pytest.mark.parametrize("builder", BUILDERS)
    def test_attribution_is_appended_to_preset_credit(self, geo, tmp_path, builder):
        # The OpenStreetMap / CARTO credit their terms require is never dropped.
        cfg = parse_config(render(geo, tmp_path, builder, tiles="osm", attribution="My tiles"))
        attribution = cfg["tiles"]["options"]["attribution"]
        assert attribution.endswith(", My tiles")
        assert "openstreetmap.org/copyright" in attribution


# ─────────────────────────────────────────────────────────────────────────────
# Basemaps — custom URL templates
# ─────────────────────────────────────────────────────────────────────────────

class TestCustomTiles:
    @pytest.mark.parametrize("builder", BUILDERS)
    def test_https_template_with_attribution(self, geo, tmp_path, builder):
        page = render(geo, tmp_path, builder, tiles=CUSTOM_TILES, attribution="&copy; Example")
        tiles = parse_config(page)["tiles"]
        assert tiles["url"] == CUSTOM_TILES
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

    def test_custom_tiles_ignore_api_key_and_never_warn(self, geo, tmp_path):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            page = render(geo, tmp_path, tiles=CUSTOM_TILES, tile_api_key="secret")
        assert parse_config(page)["tiles"]["url"] == CUSTOM_TILES
        assert "secret" not in page


# ─────────────────────────────────────────────────────────────────────────────
# CARTO API key
# ─────────────────────────────────────────────────────────────────────────────

class TestCartoApiKey:
    @pytest.mark.parametrize("builder", BUILDERS)
    @pytest.mark.parametrize("preset", CARTO_PRESETS)
    def test_key_appended_to_tile_url(self, geo, tmp_path, builder, preset):
        cfg = parse_config(render(geo, tmp_path, builder, tiles=preset, tile_api_key="abc123"))
        assert cfg["tiles"]["url"] == PRESET_URLS[preset] + "?key=abc123"

    def test_key_is_url_encoded(self, geo, tmp_path):
        page = render(geo, tmp_path, tile_api_key="a b/c+d&e?=é")
        assert parse_config(page)["tiles"]["url"] == \
            PRESET_URLS["dark"] + "?key=a%20b%2Fc%2Bd%26e%3F%3D%C3%A9"
        assert "a b/c" not in page

    def test_env_var_used_when_no_argument(self, geo, tmp_path, monkeypatch):
        monkeypatch.setenv(CARTO_KEY_ENV, "from-env")
        cfg = parse_config(render(geo, tmp_path, tiles="light"))
        assert cfg["tiles"]["url"] == PRESET_URLS["light"] + "?key=from-env"

    def test_env_var_whitespace_stripped(self, geo, tmp_path, monkeypatch):
        monkeypatch.setenv(CARTO_KEY_ENV, "  padded  ")
        assert parse_config(render(geo, tmp_path))["tiles"]["url"].endswith("?key=padded")

    def test_argument_overrides_env_var(self, geo, tmp_path, monkeypatch):
        monkeypatch.setenv(CARTO_KEY_ENV, "from-env")
        page = render(geo, tmp_path, tile_api_key="from-arg")
        assert parse_config(page)["tiles"]["url"].endswith("?key=from-arg")
        assert "from-env" not in page

    def test_empty_argument_falls_back_to_env_var(self, geo, tmp_path, monkeypatch):
        monkeypatch.setenv(CARTO_KEY_ENV, "from-env")
        cfg = parse_config(render(geo, tmp_path, tile_api_key=""))
        assert cfg["tiles"]["url"].endswith("?key=from-env")

    @pytest.mark.parametrize("builder", BUILDERS)
    def test_osm_never_gets_key(self, geo, tmp_path, monkeypatch, builder):
        monkeypatch.setenv(CARTO_KEY_ENV, "from-env")
        page = render(geo, tmp_path, builder, tiles="osm", tile_api_key="from-arg")
        assert parse_config(page)["tiles"]["url"] == PRESET_URLS["osm"]
        assert "from-arg" not in page
        assert "from-env" not in page

    @pytest.mark.parametrize("builder", BUILDERS)
    @pytest.mark.parametrize("preset", CARTO_PRESETS)
    def test_warns_without_key(self, geo, tmp_path, builder, preset):
        with pytest.warns(UserWarning, match="API key"):
            getattr(geo, builder)(tmp_path / "w.html", tiles=preset)

    def test_warning_names_preset_and_remedies(self, geo, tmp_path):
        with pytest.warns(UserWarning, match="API key") as record:
            geo.map(tmp_path / "w.html", tiles="voyager")
        message = str(record[0].message)
        assert "'voyager'" in message
        assert CARTO_KEY_ENV in message
        assert "tile_api_key" in message
        assert "tiles='osm'" in message
        assert "https://carto.com/basemaps/apikey/" in message

    @pytest.mark.parametrize("builder", BUILDERS)
    def test_warning_points_at_caller(self, geo, tmp_path, builder):
        with pytest.warns(UserWarning, match="API key") as record:
            getattr(geo, builder)(tmp_path / "w.html")
        assert len(record) == 1
        assert Path(record[0].filename).name == Path(__file__).name

    @pytest.mark.parametrize(("env", "key"), [("   ", None), (None, "")],
                             ids=["blank-env-var", "empty-argument"])
    def test_blank_key_still_warns(self, geo, tmp_path, monkeypatch, env, key):
        if env is not None:
            monkeypatch.setenv(CARTO_KEY_ENV, env)
        with pytest.warns(UserWarning, match="API key"):
            geo.map(tmp_path / "w.html", tile_api_key=key)

    @pytest.mark.parametrize("builder", BUILDERS)
    @pytest.mark.parametrize("kwargs", [
        {"tiles": "osm"},
        {"tiles": "dark", "tile_api_key": "k"},
        {"tiles": CUSTOM_TILES},
    ], ids=["osm", "dark-with-key", "custom-url"])
    def test_no_warning_when_not_needed(self, geo, tmp_path, builder, kwargs):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            getattr(geo, builder)(tmp_path / "w.html", **kwargs)

    def test_no_warning_with_env_var(self, geo, tmp_path, monkeypatch):
        monkeypatch.setenv(CARTO_KEY_ENV, "from-env")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            geo.map(tmp_path / "w.html", tiles="dark")


# ─────────────────────────────────────────────────────────────────────────────
# Attribution
# ─────────────────────────────────────────────────────────────────────────────

class TestAttribution:
    @pytest.mark.parametrize("builder", BUILDERS)
    def test_osm_attribution_links_copyright_page(self, geo, tmp_path, builder):
        options = parse_config(render(geo, tmp_path, builder, tiles="osm"))["tiles"]["options"]
        assert options["attribution"] == f"&copy; {OSM_COPYRIGHT_LINK} contributors"

    @pytest.mark.parametrize("preset", CARTO_PRESETS)
    def test_carto_attribution_credits_osm_and_carto(self, geo, tmp_path, preset):
        options = parse_config(render(geo, tmp_path, tiles=preset))["tiles"]["options"]
        assert OSM_COPYRIGHT_LINK in options["attribution"]
        assert '<a href="https://carto.com/attribution/">CARTO</a>' in options["attribution"]
        assert options["subdomains"] == "abcd"
        assert options["maxZoom"] == 20

    def test_osm_tiles_use_single_host_without_subdomains(self, geo, tmp_path):
        tiles = parse_config(render(geo, tmp_path, tiles="osm"))["tiles"]
        assert "{s}" not in tiles["url"]
        assert "subdomains" not in tiles["options"]
        assert tiles["options"]["maxZoom"] == 19


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
        with pytest.warns(UserWarning, match=r"1 row .* adds no heat — the heatmap is empty"):
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

class TestToJs:
    @pytest.mark.parametrize(("value", "expected"), [
        ("</script>", '"\\u003c/script\\u003e"'),
        ("a&b", '"a\\u0026b"'),
        ("<!--", '"\\u003c!--"'),
        ("\u2028\u2029", '"\\u2028\\u2029"'),
        ("é東😀", '"é東😀"'),
        ({"<k>": 1}, '{"\\u003ck\\u003e": 1}'),
        (float("nan"), "null"),
        ({"x": float("inf")}, '{"x": null}'),
    ])
    def test_exact_output(self, value, expected):
        assert _to_js(value) == expected

    def test_output_round_trips_as_json(self):
        obj = {"html": XSS, "amp": "&", "list": [1, 2.5, None, True], "nested": {"q": '"'}}
        assert json.loads(_to_js(obj)) == obj

    def test_dates_times_decimals_sets(self):
        obj = {
            "dt": datetime.datetime(2026, 10, 19, 8, 30),
            "d": datetime.date(2026, 10, 19),
            "t": datetime.time(8, 30, 1),
            "dec": decimal.Decimal("0.1"),
            "set": {"b", "a"},
            "fs": frozenset({2, 1}),
        }
        assert json.loads(_to_js(obj)) == {
            "dt": "2026-10-19T08:30:00", "d": "2026-10-19", "t": "08:30:01",
            "dec": 0.1, "set": ["a", "b"], "fs": [1, 2],
        }

    def test_set_order_is_deterministic(self):
        # Sorted by str() so mixed types never raise and output is stable.
        assert _to_js({10, 9}) == "[10, 9]"
        assert _to_js({1, "a"}) == '[1, "a"]'

    def test_unknown_objects_fall_back_to_str(self):
        assert _to_js(complex(1, 2)) == '"(1+2j)"'
        assert _to_js(Path("a")) == json.dumps(str(Path("a")))

    def test_non_string_keys_become_strings(self):
        assert json.loads(_to_js({1: "a", None: "b"})) == {"1": "a", "null": "b"}

    def test_randomised_strings_never_leak_dangerous_chars(self):
        rng = random.Random(20261019)
        alphabet = "<>&/\\\"' \n\t\u2028\u2029aZ!-é😀"
        for _ in range(300):
            s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
            obj = {s: [s, {"k": s}]}
            text = _to_js(obj)
            assert not set(text) & set("<>&\n\u2028\u2029"), repr(s)
            assert json.loads(text) == obj
