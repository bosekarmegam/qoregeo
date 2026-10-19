"""
tests/test_regressions.py
=========================
Regression tests for defects found while reviewing the 1.1.0 release — each
test pins one fixed behaviour (most also worked in 1.0.3 and must keep working).
"""

from __future__ import annotations

import concurrent.futures
import copy
import csv
import decimal
import errno
import json
import os
import pickle
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from qoregeo import (
    GeoEngine,
    GeoFileNotFoundError,
    InvalidArgumentError,
    InvalidCoordinateError,
    InvalidGeoJSONError,
)

DELHI = (28.6139, 77.2090)
JAIPUR = (26.9124, 75.7873)


@pytest.fixture
def two_cities() -> GeoEngine:
    return (GeoEngine(verbose=False)
            .add_point(*DELHI, name="Delhi")
            .add_point(*JAIPUR, name="Jaipur"))


def _names(geo: GeoEngine) -> list[str]:
    return [f["properties"]["name"] for f in geo]


def parse_config(page: str) -> dict[str, Any]:
    """The JSON assigned to ``var CONFIG`` in a generated map page."""
    found = re.findall(r"^[ \t]*var CONFIG = (.*);[ \t]*$", page, re.MULTILINE)
    assert len(found) == 1
    result: dict[str, Any] = json.loads(found[0])
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Columns seen by filter()
# ─────────────────────────────────────────────────────────────────────────────

class TestFilterColumns:
    def test_filter_on_injected_distance(self, two_cities):
        near = two_cities.filter_by_radius(28.61, 77.20, 1000)
        assert _names(near.filter("_distance", 100, op="<")) == ["Delhi"]
        assert _names(two_cities.k_nearest(DELHI, k=2).filter("_distance", 0)) == ["Delhi"]
        line = [DELHI, JAIPUR]
        assert two_cities.filter_by_line(line, 5).filter("_distance", 1, op="<").count() == 2

    def test_property_added_in_place_after_a_filter(self, two_cities):
        two_cities.filter("name", "Delhi")
        for feat in two_cities.get_features():
            feat["properties"]["tier"] = "1"
        assert two_cities.filter("tier", "1").count() == 2


# ─────────────────────────────────────────────────────────────────────────────
# Exact comparisons for large integers
# ─────────────────────────────────────────────────────────────────────────────

class TestBigIntegers:
    IDS = [9007199254740993, 9007199254740992]

    @pytest.fixture
    def orders(self) -> GeoEngine:
        geo = GeoEngine(verbose=False)
        for i, oid in enumerate(self.IDS):
            geo.add_point(10 + i, 20, order_id=oid, code=str(oid))
        return geo

    def test_equality_is_exact(self, orders):
        assert [f["properties"]["order_id"] for f in orders.filter("order_id", self.IDS[0])] == \
            [self.IDS[0]]
        assert orders.filter("code", self.IDS[1]).count() == 1

    def test_ordering_is_exact(self, orders):
        assert orders.filter("order_id", self.IDS[1], op=">").count() == 1
        assert [f["properties"]["order_id"] for f in orders.sort_by("code")] == sorted(self.IDS)


# ─────────────────────────────────────────────────────────────────────────────
# Loading edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadingEdgeCases:
    def test_polygon_with_empty_ring_does_not_crash(self, tmp_path):
        geo = GeoEngine(verbose=False).load_data([
            {"type": "Polygon", "coordinates": [[]]},
            {"type": "Point", "coordinates": [77.2, 28.6]},
        ])
        assert geo.nearest((28.6, 77.2))["index"] == 1
        assert geo.centroid() == pytest.approx((28.6, 77.2))
        geo.save(tmp_path / "x.csv")
        geo.map(tmp_path / "x.html", tiles="osm")

    @pytest.mark.parametrize("ftype", [None, "feature", "FEATURE"])
    def test_feature_with_missing_or_miscased_type(self, ftype):
        item = {"geometry": {"type": "Point", "coordinates": [77.2, 28.6]}, "properties": {"n": 1}}
        if ftype is not None:
            item["type"] = ftype
        geo = GeoEngine(verbose=False).load_data([item])
        assert geo.get_features()[0]["type"] == "Feature"
        assert geo.count() == 1

    def test_blank_line_after_sep_preamble(self, tmp_path):
        path = tmp_path / "excel.csv"
        path.write_text("sep=;\n\nname;lat;lng\nA;10;20\n", encoding="utf-8")
        assert GeoEngine().load(path).get_features()[0]["properties"] == {"name": "A"}

    def test_field_size_limit_restored_after_concurrent_loads(self, tmp_path):
        path = tmp_path / "big.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["lat", "lng", "blob"])
            writer.writerow(["10", "20", "x" * 200_000])
        before = csv.field_size_limit()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            counts = list(pool.map(lambda _: GeoEngine().load(path).count(), range(32)))
        assert counts == [1] * 32
        assert csv.field_size_limit() == before

    @pytest.mark.parametrize("kwargs", [{"encoding": "rot13"}, {"delimiter": "\n"},
                                        {"delimiter": '"'}])
    def test_bad_reader_options_are_invalid_arguments(self, tmp_path, kwargs):
        path = tmp_path / "x.csv"
        path.write_text("lat,lng\n1,2\n", encoding="utf-8")
        with pytest.raises(InvalidArgumentError):
            GeoEngine().load(path, **kwargs)

    def test_integer_with_too_many_digits_is_invalid_geojson(self, tmp_path):
        path = tmp_path / "huge.geojson"
        path.write_text('{"type": "Point", "coordinates": [1' + "0" * 5000 + ", 2]}",
                        encoding="utf-8")
        with pytest.raises(InvalidGeoJSONError):
            GeoEngine().load(path)

    def test_infer_types_keeps_digits_a_float_would_lose(self, tmp_path):
        path = tmp_path / "p.csv"
        path.write_text("lat,lng,pi,big\n1,2,3.14159265358979323846,1" + "0" * 5000 + "\n",
                        encoding="utf-8")
        props = GeoEngine().load(path, infer_types=True).get_features()[0]["properties"]
        assert props["pi"] == "3.14159265358979323846"
        assert props["big"] == "1" + "0" * 5000


class TestOverflowingNumbers:
    def test_huge_coordinates_raise_coordinate_errors(self):
        geo = GeoEngine(verbose=False)
        with pytest.raises(InvalidCoordinateError):
            geo.distance((10 ** 400, 0), (0, 0))
        with pytest.raises(InvalidCoordinateError):
            geo.add_point(10 ** 400, 0)

    def test_huge_geojson_positions_are_skipped(self):
        with pytest.warns(UserWarning, match="too large"):
            geo = GeoEngine(verbose=False).load_data([
                {"type": "Point", "coordinates": [10 ** 400, 0]},
                {"type": "Point", "coordinates": [1, 2]},
            ])
        assert geo.count() == 1

    def test_out_of_range_decimal_becomes_null(self, tmp_path):
        geo = GeoEngine(verbose=False).add_point(1, 2, big=decimal.Decimal("1e400"))
        out = tmp_path / "d.geojson"
        geo.save(out)
        assert json.loads(out.read_text(encoding="utf-8"))["features"][0]["properties"] == \
            {"big": None}
        geo.map(tmp_path / "d.html", tiles="osm")

    def test_huge_heat_weight_does_not_crash(self, tmp_path):
        geo = GeoEngine(verbose=False).add_point(1, 2, w=10 ** 400).add_point(3, 4, w=1)
        with pytest.warns(UserWarning, match="adds no heat"):
            geo.heatmap(tmp_path / "h.html", intensity_col="w", tiles="osm")


# ─────────────────────────────────────────────────────────────────────────────
# File writing
# ─────────────────────────────────────────────────────────────────────────────

needs_symlinks = pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
not_root = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission checks are bypassed for root / differ on Windows",
)


class TestFileWriting:
    @needs_symlinks
    @pytest.mark.parametrize("ext", [".geojson", ".html"])
    def test_writing_through_a_symlink_updates_its_target(self, two_cities, tmp_path, ext):
        real = tmp_path / f"real{ext}"
        real.write_text("OLD", encoding="utf-8")
        link = tmp_path / f"latest{ext}"
        link.symlink_to(real)
        if ext == ".html":
            two_cities.map(link, tiles="osm")
        else:
            two_cities.save(link)
        assert link.is_symlink()
        assert real.read_text(encoding="utf-8") != "OLD"

    @pytest.mark.skipif(not hasattr(os, "link"), reason="no hard links")
    def test_hard_linked_names_both_see_the_new_content(self, two_cities, tmp_path):
        a = tmp_path / "a.geojson"
        a.write_text("OLD", encoding="utf-8")
        b = tmp_path / "b.geojson"
        os.link(a, b)
        two_cities.save(a)
        assert b.read_text(encoding="utf-8") == a.read_text(encoding="utf-8") != "OLD"

    @not_root
    def test_read_only_target_is_not_replaced(self, two_cities, tmp_path):
        target = tmp_path / "locked.geojson"
        target.write_text("PROTECTED", encoding="utf-8")
        target.chmod(0o444)
        try:
            with pytest.raises(PermissionError):
                two_cities.save(target)
            assert target.read_text(encoding="utf-8") == "PROTECTED"
        finally:
            target.chmod(0o644)

    @not_root
    def test_writable_file_in_read_only_folder(self, two_cities, tmp_path):
        folder = tmp_path / "ro"
        folder.mkdir()
        target = folder / "out.geojson"
        target.write_text("OLD", encoding="utf-8")
        folder.chmod(0o555)
        try:
            two_cities.save(target)
            assert json.loads(target.read_text(encoding="utf-8"))["type"] == "FeatureCollection"
        finally:
            folder.chmod(0o755)

    def test_long_file_name(self, two_cities, tmp_path):
        target = tmp_path / ("x" * 240 + ".geojson")
        two_cities.save(target)
        assert target.exists()
        assert [p.name for p in tmp_path.iterdir()] == [target.name]   # no temp files left

    def test_bytes_output_path_for_maps(self, two_cities, tmp_path):
        out = os.fsencode(tmp_path / "m.html")
        two_cities.map(out, tiles="osm")
        two_cities.heatmap(os.fsencode(tmp_path / "h.html"), tiles="osm")
        assert (tmp_path / "m.html").exists() and (tmp_path / "h.html").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class TestExceptionRoundTrip:
    @pytest.mark.parametrize("clone", [lambda e: pickle.loads(pickle.dumps(e)), copy.copy,
                                       copy.deepcopy])
    def test_file_not_found_keeps_errno(self, clone):
        with pytest.raises(GeoFileNotFoundError) as info:
            GeoEngine().load("/no/such/file.csv")
        again = clone(info.value)
        assert type(again) is GeoFileNotFoundError
        assert again.errno == errno.ENOENT
        assert again.strerror == os.strerror(errno.ENOENT)
        assert again.filename == "/no/such/file.csv"
        assert str(again) == str(info.value)


# ─────────────────────────────────────────────────────────────────────────────
# Maps across the antimeridian
# ─────────────────────────────────────────────────────────────────────────────

def _lngs(cfg: dict) -> list[float]:
    out: list[float] = []

    def walk(c):
        if isinstance(c, list) and len(c) >= 2 and not isinstance(c[0], list):
            out.append(c[0])
        elif isinstance(c, list):
            for x in c:
                walk(x)

    for f in cfg["data"]["features"]:
        walk(f["geometry"]["coordinates"])
    return out


class TestAntimeridianMaps:
    def test_shifted_maps_disable_world_copy_jump(self, tmp_path):
        fiji = GeoEngine(verbose=False).add_point(-18.14, 178.44).add_point(-16.8, -179.95)
        out = tmp_path / "f.html"
        fiji.map(out, tiles="osm")
        cfg = parse_config(out.read_text(encoding="utf-8"))
        assert cfg["worldCopyJump"] is False
        assert sorted(_lngs(cfg)) == pytest.approx([178.44, 180.05])

        india = GeoEngine(verbose=False).add_point(*DELHI)
        india.map(out, tiles="osm")
        assert parse_config(out.read_text(encoding="utf-8"))["worldCopyJump"] is True

    def test_mixed_global_data_keeps_europe_together(self, tmp_path):
        geo = GeoEngine(verbose=False).load_data([
            {"type": "Point", "coordinates": [178.44, -18.14]},          # Fiji
            {"type": "Point", "coordinates": [-179.95, -16.8]},          # Fiji, east of 180
            {"type": "LineString", "coordinates": [[-0.1276, 51.5], [2.3522, 48.86]]},  # London–Paris
        ])
        out = tmp_path / "g.html"
        geo.map(out, tiles="osm")
        line = parse_config(out.read_text(encoding="utf-8"))["data"]["features"][2]
        assert line["geometry"]["coordinates"] == [[-0.1276, 51.5], [2.3522, 48.86]]

    def test_center_moves_with_shifted_data(self, tmp_path):
        fiji = GeoEngine(verbose=False).add_point(-18.14, 178.44).add_point(-16.8, -179.95)
        out = tmp_path / "c.html"
        fiji.map(out, center=(-17.5, -179.5), zoom=7, tiles="osm")
        assert parse_config(out.read_text(encoding="utf-8"))["view"]["center"] == \
            pytest.approx([-17.5, 180.5])

    @pytest.mark.parametrize("wrap", ["feature", "collection"])
    def test_feature_overlays_count_toward_the_shift(self, tmp_path, wrap):
        zone = GeoEngine().buffer((-17.5, 179.9), 300)
        overlay = {"type": "Feature", "geometry": zone, "properties": {}}
        if wrap == "collection":
            overlay = {"type": "FeatureCollection", "features": [overlay]}
        geo = GeoEngine(verbose=False).add_point(-17.0, -179.5)
        out = tmp_path / "o.html"
        geo.map(out, overlays=[overlay], tiles="osm")
        cfg = parse_config(out.read_text(encoding="utf-8"))
        assert cfg["worldCopyJump"] is False
        assert _lngs(cfg) == pytest.approx([180.5])

    @pytest.mark.parametrize("kwargs", [{"tile_api_key": 123}, {"attribution": 5}])
    def test_non_string_tile_options_rejected(self, two_cities, tmp_path, kwargs):
        with pytest.raises(InvalidArgumentError):
            two_cities.map(tmp_path / "t.html", **kwargs)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only path check")
def test_no_temp_files_left_after_failed_save(tmp_path, two_cities):
    target = tmp_path / "bad.geojson"
    target.write_text("OLD", encoding="utf-8")
    with pytest.raises(UnicodeEncodeError):
        two_cities.add_point(1, 2, name="Ω").save(target, encoding="ascii")
    assert target.read_text(encoding="utf-8") == "OLD"
    assert sorted(p.name for p in Path(tmp_path).iterdir()) == ["bad.geojson"]
