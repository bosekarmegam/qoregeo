"""
tests/test_tools.py
===================
qoregeo.tools — QOREgeo's operations as AI tools (MCP, Claude API, OpenAI).

* ``tool_definitions()`` in all four formats and the JSON-Schema rules the
  platforms enforce (object schemas, ``additionalProperties: false``,
  ``required`` ⊆ ``properties``, tool-name pattern)
* ``call_tool()`` for every tool with valid input, checking the result data
* error paths — every one returns ``is_error=True`` with a useful message and
  never raises: unknown tools, bad arguments, sandbox escapes ('..', absolute
  paths, symlinks), missing / oversized files, read-only mode, the input file
  as output, existing outputs (``overwrite``), wrong extensions, geocoding
  without network access
* result paging (``max_features``, ``offset``, the character cap)
* ``ToolResult.to_mcp()`` / ``to_anthropic()``

Offline: files live in ``tmp_path`` and geocoding uses a fake geocoder.

Run with:
    pytest tests/test_tools.py -v
"""

from __future__ import annotations

import base64
import csv
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from qoregeo import GeoEngine
from qoregeo.exceptions import InvalidArgumentError
from qoregeo.map_builder import tile_presets
from qoregeo.tools import (
    SERVER_INSTRUCTIONS,
    TOOLS,
    ToolContext,
    ToolResult,
    call_tool,
    tool_definitions,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
FORMATS = ["mcp", "anthropic", "openai", "openai-responses"]
NETWORK_TOOLS = {"geocode", "reverse_geocode"}
WRITING_TOOLS = {"make_map", "export_features"}
# Claude and OpenAI both require tool names matching this pattern.
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_CONFIG_RE = re.compile(r"^[ \t]*var CONFIG = JSON\.parse\((.*)\);[ \t]*$", re.MULTILINE)

CITIES_CSV = (
    "name,lat,lng,population,state,note\n"
    "Delhi,28.6139,77.2090,32000000,Delhi,=1+1\n"
    "Mumbai,19.0760,72.8777,21000000,Maharashtra,ok\n"
    "Pune,18.5204,73.8567,7000000,Maharashtra,ok\n"
    "Jaipur,26.9124,75.7873,4000000,Rajasthan,ok\n"
    "Chennai,13.0827,80.2707,11000000,Tamil Nadu,ok\n"
    "Kolkata,22.5726,88.3639,15000000,West Bengal,ok\n"
    "Ahmedabad,23.0225,72.5714,8000000,Gujarat,ok\n"
)
CITY_NAMES = ["Delhi", "Mumbai", "Pune", "Jaipur", "Chennai", "Kolkata", "Ahmedabad"]

DELHI = {"lat": 28.6139, "lng": 77.2090}
MUMBAI = {"lat": 19.0760, "lng": 72.8777}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers + fixtures
# ─────────────────────────────────────────────────────────────────────────────

def square(x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]:
    ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
    return {"type": "Polygon", "coordinates": [ring]}


def points_fc(n: int, pad: int = 0) -> dict[str, Any]:
    """FeatureCollection of n points along the equator, property i = index."""
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [i * 0.01, 0.0]},
         "properties": {"i": i, **({"pad": "x" * pad} if pad else {})}}
        for i in range(n)
    ]}


def ok(name: str, args: dict[str, Any] | None, ctx: ToolContext) -> dict[str, Any]:
    result = call_tool(name, args, ctx)
    assert not result.is_error, result.data
    assert json.loads(result.text) == result.data
    return result.data


def err(name: str, args: Any, ctx: ToolContext | None = None) -> str:
    result = call_tool(name, args, ctx)
    assert result.is_error, result.data
    assert set(result.data) == {"error"}
    message = result.data["error"]
    assert isinstance(message, str) and len(message) > 10
    assert "Traceback" not in message
    return message


def names_of(data: dict[str, Any]) -> list[str]:
    return [f["properties"]["name"] for f in data["features"]]


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


def walk_schemas(schema: Any, where: str = "$") -> list[tuple[str, dict[str, Any]]]:
    """Every (path, sub-schema) dict inside a JSON schema."""
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(schema, dict):
        found.append((where, schema))
        for key, sub in (schema.get("properties") or {}).items():
            found += walk_schemas(sub, f"{where}.{key}")
        if isinstance(schema.get("items"), dict):
            found += walk_schemas(schema["items"], f"{where}[]")
    return found


class FakeGeocoder:
    """Stands in for qoregeo.geocode.Geocoder — never touches the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def search(self, query: str, limit: int = 5, country_codes: Any = None) -> list[dict[str, Any]]:
        self.calls.append(("search", (query,), {"limit": limit, "country_codes": country_codes}))
        return [{"lat": 18.921984, "lng": 72.834654, "name": "Gateway of India, Mumbai",
                 "attribution": "Data © OpenStreetMap contributors"}][:limit]

    def reverse(self, lat: float, lng: float) -> dict[str, Any] | None:
        self.calls.append(("reverse", (lat, lng), {}))
        if lat == 0 and lng == 0:
            return None
        return {"lat": lat, "lng": lng, "name": "Somewhere, Mumbai",
                "attribution": "Data © OpenStreetMap contributors"}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "root"
    base.mkdir()
    (base / "cities.csv").write_text(CITIES_CSV, encoding="utf-8")
    zones = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": square(72, 15, 76, 27), "properties": {"name": "west"}},
    ]}
    (base / "zones.geojson").write_text(json.dumps(zones), encoding="utf-8")
    sub = base / "data" / "deep"
    sub.mkdir(parents=True)
    (sub / "more.tsv").write_text("name\tlat\tlng\nX\t1\t2\n", encoding="utf-8")
    (base / "data" / "notes.md").write_text("not data", encoding="utf-8")
    hidden = base / ".git"
    hidden.mkdir()
    (hidden / "secret.json").write_text("{}", encoding="utf-8")
    return base


@pytest.fixture
def ctx(root: Path) -> ToolContext:
    return ToolContext(root=str(root))


# A valid call of every tool (relative to the ``root`` fixture).
VALID_ARGS: dict[str, dict[str, Any]] = {
    "distance": {"from": DELHI, "to": MUMBAI},
    "bearing": {"from": DELHI, "to": MUMBAI},
    "destination": {"start": DELHI, "bearing": 90, "distance": 10},
    "midpoint": {"a": DELHI, "b": MUMBAI},
    "buffer": {"center": DELHI, "radius": 5, "num_points": 8},
    "contains": {"point": DELHI, "area": square(77, 28, 78, 29)},
    "intersects": {"a": square(0, 0, 1, 1), "b": square(0.5, 0.5, 2, 2)},
    "measure": {"geometry": square(0, 0, 1, 1)},
    "geohash": {"point": DELHI},
    "list_files": {},
    "describe_dataset": {"path": "cities.csv"},
    "query_features": {"path": "cities.csv"},
    "nearest": {"path": "cities.csv", "point": DELHI},
    "preview_map": {"path": "cities.csv", "width": 64, "height": 64},
    "make_map": {"path": "cities.csv", "output": "every.svg", "width": 64, "height": 64,
                 "overwrite": True},
    "export_features": {"path": "cities.csv", "output": "every.csv", "overwrite": True},
    "geocode": {"query": "Pune"},
    "reverse_geocode": {"point": DELHI},
}


@pytest.fixture
def full_ctx(root: Path) -> ToolContext:
    """Everything allowed, with a fake geocoder."""
    return ToolContext(root=str(root), allow_network=True, geocoder=FakeGeocoder())


# ─────────────────────────────────────────────────────────────────────────────
# tool_definitions()
# ─────────────────────────────────────────────────────────────────────────────

def schema_of(definition: dict[str, Any], fmt: str) -> dict[str, Any]:
    if fmt == "mcp":
        return definition["inputSchema"]
    if fmt == "anthropic":
        return definition["input_schema"]
    if fmt == "openai":
        return definition["function"]["parameters"]
    return definition["parameters"]


def name_of(definition: dict[str, Any], fmt: str) -> str:
    return definition["function"]["name"] if fmt == "openai" else definition["name"]


class TestToolDefinitions:
    def test_mcp_shape(self):
        defs = tool_definitions("mcp")
        for d in defs:
            assert set(d) == {"name", "title", "description", "inputSchema", "annotations"}
            tool = TOOLS[d["name"]]
            assert d["title"] == tool.title
            assert d["annotations"] == {
                "title": tool.title,
                "readOnlyHint": tool.read_only,
                "destructiveHint": tool.destructive,
                "idempotentHint": True,
                "openWorldHint": tool.open_world,
            }

    def test_default_format_is_mcp(self):
        assert tool_definitions() == tool_definitions("mcp")

    def test_anthropic_shape(self):
        for d in tool_definitions("anthropic"):
            assert set(d) == {"name", "description", "input_schema"}

    def test_openai_shape(self):
        for d in tool_definitions("openai"):
            assert set(d) == {"type", "function"}
            assert d["type"] == "function"
            assert set(d["function"]) == {"name", "description", "parameters"}

    def test_openai_responses_shape(self):
        for d in tool_definitions("openai-responses"):
            assert set(d) == {"type", "name", "description", "parameters"}
            assert d["type"] == "function"

    @pytest.mark.parametrize("fmt", FORMATS)
    def test_same_tools_and_schemas_in_every_format(self, fmt):
        defs = tool_definitions(fmt)
        assert [name_of(d, fmt) for d in defs] == list(TOOLS)
        for d in defs:
            tool = TOOLS[name_of(d, fmt)]
            assert schema_of(d, fmt) == tool.input_schema
            desc = d["function"]["description"] if fmt == "openai" else d["description"]
            assert desc == tool.description and len(desc) > 20
        json.dumps(defs, allow_nan=False)   # plain JSON

    @pytest.mark.parametrize("fmt", FORMATS)
    def test_names_unique_and_valid(self, fmt):
        names = [name_of(d, fmt) for d in tool_definitions(fmt)]
        assert len(names) == len(set(names))
        for name in names:
            assert NAME_RE.match(name), name

    @pytest.mark.parametrize("fmt", FORMATS)
    def test_input_schemas_are_strict_objects(self, fmt):
        for d in tool_definitions(fmt):
            schema = schema_of(d, fmt)
            assert schema["type"] == "object"
            assert schema["additionalProperties"] is False
            assert isinstance(schema["properties"], dict)
            assert isinstance(schema["required"], list)
            assert set(schema["required"]) <= set(schema["properties"])
            assert len(set(schema["required"])) == len(schema["required"])

    def test_nested_object_schemas_are_strict(self):
        for tool in TOOLS.values():
            for where, sub in walk_schemas(tool.input_schema):
                if "properties" in sub:
                    assert sub.get("additionalProperties") is False, f"{tool.name} {where}"
                    assert set(sub.get("required", [])) <= set(sub["properties"]), where

    def test_registry_names_match_keys(self):
        for key, tool in TOOLS.items():
            assert tool.name == key

    def test_writers_and_network_flags(self):
        assert {n for n, t in TOOLS.items() if not t.read_only} == WRITING_TOOLS
        assert {n for n, t in TOOLS.items() if t.destructive} == WRITING_TOOLS
        assert {n for n, t in TOOLS.items() if t.open_world} == NETWORK_TOOLS

    def test_paging_and_overwrite_properties(self):
        for name in ("query_features", "nearest"):
            offset = TOOLS[name].input_schema["properties"]["offset"]
            assert offset["type"] == "integer" and offset["minimum"] == 0
        for name in WRITING_TOOLS:
            props = TOOLS[name].input_schema["properties"]
            assert props["overwrite"]["type"] == "boolean"
            assert props["overwrite"].get("default") is False
            assert TOOLS[name].input_schema["required"] == ["output"]

    @pytest.mark.parametrize("fmt", FORMATS)
    def test_include_network(self, fmt):
        all_names = {name_of(d, fmt) for d in tool_definitions(fmt)}
        offline = {name_of(d, fmt) for d in tool_definitions(fmt, include_network=False)}
        assert NETWORK_TOOLS <= all_names
        assert offline == all_names - NETWORK_TOOLS

    def test_names_filter(self):
        defs = tool_definitions("anthropic", names=["nearest", "distance"])
        assert [d["name"] for d in defs] == ["nearest", "distance"]
        assert tool_definitions("mcp", names=[]) == []
        assert tool_definitions("mcp", names=["geocode", "buffer"], include_network=False) == \
            tool_definitions("mcp", names=["buffer"])

    @pytest.mark.parametrize("fmt", ["claude", "MCP", "", "openai_responses", None, 1])
    def test_invalid_format(self, fmt):
        with pytest.raises(InvalidArgumentError) as info:
            tool_definitions(fmt)   # type: ignore[arg-type]
        assert info.value.name == "format"

    @pytest.mark.parametrize("names", [["no_such_tool"], ["distance", "Distance"]])
    def test_unknown_name_in_filter(self, names):
        with pytest.raises(InvalidArgumentError):
            tool_definitions("mcp", names=names)

    def test_tiles_enum_lists_every_preset(self):
        enum = TOOLS["make_map"].input_schema["properties"]["tiles"]["enum"]
        assert set(enum) == set(tile_presets())
        assert "satellite" in enum

    def test_server_instructions(self):
        assert "lat" in SERVER_INSTRUCTIONS and "list_files" in SERVER_INSTRUCTIONS


# ─────────────────────────────────────────────────────────────────────────────
# call_tool — every tool with valid input
# ─────────────────────────────────────────────────────────────────────────────

class TestGeometryTools:
    def test_distance(self, ctx):
        data = ok("distance", {"from": DELHI, "to": MUMBAI}, ctx)
        assert data == {"distance": pytest.approx(1148.1, abs=0.1), "unit": "km",
                        "method": "haversine"}

    def test_distance_vincenty_miles(self, ctx):
        data = ok("distance", {"from": DELHI, "to": MUMBAI, "unit": "miles",
                               "method": "vincenty"}, ctx)
        assert data["unit"] == "miles" and data["method"] == "vincenty"
        assert data["distance"] == pytest.approx(1148.1 / 1.609344, rel=0.01)

    def test_distance_rejects_lat_lng_pairs(self, ctx):
        message = err("distance", {"from": [0, 0], "to": [0, 1]}, ctx)
        assert "from" in message and "object" in message

    def test_bearing(self, ctx):
        assert ok("bearing", {"from": {"lat": 0, "lng": 0}, "to": {"lat": 0, "lng": 1}}, ctx) == \
            {"degrees": 90.0, "compass": "East"}
        data = ok("bearing", {"from": DELHI, "to": MUMBAI}, ctx)
        assert 180 < data["degrees"] < 225 and data["compass"] == "South-Southwest"

    def test_destination(self, ctx):
        data = ok("destination", {"start": {"lat": 0, "lng": 0}, "bearing": 90,
                                  "distance": 111.195}, ctx)
        assert data["lat"] == pytest.approx(0.0, abs=1e-6)
        assert data["lng"] == pytest.approx(1.0, abs=1e-3)

    def test_destination_north_in_other_unit(self, ctx):
        data = ok("destination", {"start": {"lat": 0, "lng": 0}, "bearing": 0,
                                  "distance": 111195, "unit": "m"}, ctx)
        assert data["lat"] == pytest.approx(1.0, abs=1e-3)
        assert data["lng"] == pytest.approx(0.0, abs=1e-6)

    def test_midpoint(self, ctx):
        assert ok("midpoint", {"a": {"lat": 0, "lng": 0}, "b": {"lat": 0, "lng": 2}}, ctx) == \
            {"lat": 0.0, "lng": 1.0}

    def test_buffer(self, ctx):
        data = ok("buffer", {"center": {"lat": 10, "lng": 20}, "radius": 5, "num_points": 8}, ctx)
        assert data["radius"] == 5 and data["unit"] == "km"
        geometry = data["geometry"]
        assert set(geometry) == {"type", "coordinates"}   # no private _center keys
        assert geometry["type"] == "Polygon"
        ring = geometry["coordinates"][0]
        assert len(ring) == 9 and ring[0] == ring[-1]
        geo = GeoEngine(verbose=False)
        for lng, lat in ring[:-1]:
            assert geo.distance((10, 20), (lat, lng)) == pytest.approx(5, rel=0.01)

    def test_buffer_feeds_contains(self, ctx):
        zone = ok("buffer", {"center": DELHI, "radius": 10}, ctx)["geometry"]
        assert ok("contains", {"point": {"lat": 28.65, "lng": 77.22}, "area": zone}, ctx) == \
            {"inside": True}
        assert ok("contains", {"point": MUMBAI, "area": zone}, ctx) == {"inside": False}

    def test_contains_respects_holes(self, ctx):
        holed = {"type": "Polygon", "coordinates": [
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
            [[2, 2], [8, 2], [8, 8], [2, 8], [2, 2]],
        ]}
        assert ok("contains", {"point": {"lat": 1, "lng": 1}, "area": holed}, ctx)["inside"]
        assert not ok("contains", {"point": {"lat": 5, "lng": 5}, "area": holed}, ctx)["inside"]
        feature = {"type": "Feature", "geometry": holed, "properties": {}}
        assert ok("contains", {"point": {"lat": 1, "lng": 1}, "area": feature}, ctx)["inside"]

    def test_intersects(self, ctx):
        a = square(0, 0, 2, 2)
        assert ok("intersects", {"a": a, "b": square(1, 1, 3, 3)}, ctx) == {"intersects": True}
        assert ok("intersects", {"a": a, "b": square(5, 5, 6, 6)}, ctx) == {"intersects": False}
        line = {"type": "LineString", "coordinates": [[-1, 1], [3, 1]]}
        assert ok("intersects", {"a": line, "b": a}, ctx)["intersects"] is True

    def test_measure_line(self, ctx):
        line = {"type": "LineString", "coordinates": [[0, 0], [1, 0]]}
        assert ok("measure", {"geometry": line}, ctx) == {
            "type": "LineString", "length": pytest.approx(111.195, abs=0.01), "length_unit": "km"}
        data = ok("measure", {"geometry": line, "length_unit": "m"}, ctx)
        assert data["length"] == pytest.approx(111195, rel=1e-3) and data["length_unit"] == "m"

    def test_measure_polygon_and_feature(self, ctx):
        poly = square(0, 0, 1, 1)
        data = ok("measure", {"geometry": poly}, ctx)
        assert data["type"] == "Polygon" and data["area_unit"] == "km2"
        assert data["area"] == pytest.approx(12364, rel=0.01)
        ha = ok("measure", {"geometry": {"type": "Feature", "geometry": poly, "properties": {}},
                            "area_unit": "ha"}, ctx)
        assert ha["area"] == pytest.approx(data["area"] * 100, rel=1e-6)

    def test_geohash(self, ctx):
        data = ok("geohash", {"point": DELHI, "precision": 6}, ctx)
        assert data["geohash"] == "ttnfuc"
        assert data["center"]["lat"] == pytest.approx(28.6139, abs=0.01)
        assert data["center"]["lng"] == pytest.approx(77.2090, abs=0.01)
        assert len(ok("geohash", {"point": DELHI}, ctx)["geohash"]) == 9


class TestDataTools:
    def test_list_files(self, ctx, root):
        data = ok("list_files", {}, ctx)
        assert data["root"] == ctx.root
        assert data["truncated"] is False
        paths = [f["path"] for f in data["files"]]
        assert paths == ["cities.csv", "zones.geojson", "data/deep/more.tsv"]
        sizes = {f["path"]: f["bytes"] for f in data["files"]}
        assert sizes["cities.csv"] == (root / "cities.csv").stat().st_size

    def test_list_files_subdirectory(self, ctx):
        data = ok("list_files", {"directory": "data"}, ctx)
        assert [f["path"] for f in data["files"]] == ["data/deep/more.tsv"]

    def test_describe_dataset(self, ctx):
        data = ok("describe_dataset", {"path": "cities.csv"}, ctx)
        assert data["count"] == 7
        assert data["columns"] == {"name": "str", "population": "int", "state": "str",
                                   "note": "str"}
        assert data["geometry_types"] == {"Point": 7}
        west, south, east, north = data["bbox"]
        assert (west, south, east, north) == (72.5714, 13.0827, 88.3639, 28.6139)
        assert south < data["centroid"]["lat"] < north
        assert west < data["centroid"]["lng"] < east
        assert [s["name"] for s in data["sample"]] == CITY_NAMES[:5]

    def test_describe_inline_geojson(self, ctx):
        data = ok("describe_dataset", {"geojson": {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": square(0, 0, 1, 1), "properties": {"a": 1}},
            {"type": "Feature", "geometry": None, "properties": {"a": "x"}},
        ]}}, ctx)
        assert data["count"] == 2
        assert data["columns"] == {"a": "mixed"}
        assert data["geometry_types"] == {"Polygon": 1, "none": 1}

    def test_describe_geojson_file_and_explicit_columns(self, ctx, root):
        assert ok("describe_dataset", {"path": "zones.geojson"}, ctx)["count"] == 1
        (root / "odd.csv").write_text("place,y_deg,x_deg\nA,1,2\n", encoding="utf-8")
        data = ok("describe_dataset", {"path": "odd.csv", "lat_column": "y_deg",
                                       "lng_column": "x_deg"}, ctx)
        assert data["bbox"] == [2.0, 1.0, 2.0, 1.0]

    def test_query_features_combined(self, ctx):
        data = ok("query_features", {
            "path": "cities.csv",
            "near": {"lat": MUMBAI["lat"], "lng": MUMBAI["lng"], "radius": 1200, "unit": "km"},
            "bbox": [68, 15, 80, 30],                 # drops Chennai
            "within": square(72, 15, 76, 27),          # drops Delhi
            "where": [{"column": "population", "op": ">=", "value": 5_000_000}],   # drops Jaipur
            "sort_by": "population", "descending": True, "limit": 2,
        }, ctx)
        assert names_of(data) == ["Mumbai", "Ahmedabad"]
        assert data["count"] == 2 and data["returned"] == 2 and data["truncated"] is False
        assert data["offset"] == 0 and "next_offset" not in data

    def test_query_features_each_filter(self, ctx):
        def q(**args: Any) -> list[str]:
            return names_of(ok("query_features", {"path": "cities.csv", **args}, ctx))

        assert q() == CITY_NAMES
        assert q(near={"lat": 19.0760, "lng": 72.8777, "radius": 200}) == ["Mumbai", "Pune"]
        assert q(near={"lat": 19.0760, "lng": 72.8777, "radius": 125, "unit": "miles"}) == \
            ["Mumbai", "Pune"]
        assert q(bbox=[85, 20, 90, 25]) == ["Kolkata"]
        assert q(within={"type": "Feature", "geometry": square(72, 15, 76, 27),
                         "properties": {}}) == ["Mumbai", "Pune", "Jaipur", "Ahmedabad"]
        assert q(where=[{"column": "state", "value": "maharashtra"}]) == ["Mumbai", "Pune"]
        assert q(where=[{"column": "state", "op": "in", "value": ["Gujarat", "Delhi"]}]) == \
            ["Delhi", "Ahmedabad"]
        assert q(where=[{"column": "name", "op": "contains", "value": "ai"}]) == \
            ["Mumbai", "Jaipur", "Chennai"]
        assert q(where=[{"column": "population", "op": "<", "value": 8_000_000},
                        {"column": "state", "op": "!=", "value": "Rajasthan"}]) == ["Pune"]
        assert q(sort_by="population") == ["Jaipur", "Pune", "Ahmedabad", "Chennai", "Kolkata",
                                           "Mumbai", "Delhi"]
        assert q(sort_by="name", descending=True, limit=3) == ["Pune", "Mumbai", "Kolkata"]

    def test_query_features_inline(self, ctx):
        data = ok("query_features", {"geojson": points_fc(5), "bbox": [0.015, -1, 0.035, 1]}, ctx)
        assert [f["properties"]["i"] for f in data["features"]] == [2, 3]

    def test_nearest(self, ctx):
        data = ok("nearest", {"path": "cities.csv", "point": {"lat": 28.6, "lng": 77.2},
                              "k": 3}, ctx)
        assert names_of(data) == ["Delhi", "Jaipur", "Ahmedabad"]
        distances = [f["properties"]["_distance"] for f in data["features"]]
        assert distances == sorted(distances) and distances[0] < 2
        assert data["unit"] == "km" and data["count"] == 3

    def test_nearest_with_filter_and_unit(self, ctx):
        data = ok("nearest", {"path": "cities.csv", "point": DELHI, "k": 2, "unit": "miles",
                              "where": [{"column": "state", "value": "Maharashtra"}]}, ctx)
        assert names_of(data) == ["Mumbai", "Pune"]
        assert data["unit"] == "miles"
        assert data["features"][0]["properties"]["_distance"] == pytest.approx(713.4, rel=0.01)

    def test_preview_map(self, ctx):
        result = call_tool("preview_map", {"path": "cities.csv", "width": 200, "height": 150,
                                           "title": "Cities",
                                           "overlays": [square(72, 15, 76, 27)]}, ctx)
        assert not result.is_error, result.data
        (mime, png), = result.images
        assert mime == "image/png"
        assert png[:8] == PNG_SIGNATURE
        assert result.data == {"count": 7, "format": "png", "bytes": len(png)}

    def test_preview_map_filtered(self, ctx):
        result = call_tool("preview_map", {"path": "cities.csv",
                                           "where": [{"column": "state", "value": "Gujarat"}]},
                           ctx)
        assert result.data["count"] == 1
        assert result.images[0][1][:8] == PNG_SIGNATURE


class TestWritingTools:
    def test_make_map_html_in_new_subdirectory(self, ctx, root):
        data = ok("make_map", {"path": "cities.csv", "output": "out/maps/india.html",
                               "title": "India", "tiles": "topo"}, ctx)
        out = root / "out" / "maps" / "india.html"
        assert data["path"] == "out/maps/india.html"
        assert data["features"] == 7
        assert "qoregeo serve" in data["view"]
        page = out.read_text(encoding="utf-8")
        assert "JSON.parse(" in page
        assert "<title>India</title>" in page
        config = json.loads(json.loads(_CONFIG_RE.search(page).group(1)))
        assert config["tiles"]["name"] == "topo"
        assert len(config["data"]["features"]) == 7

    def test_make_map_filtered_with_overlays(self, ctx, root):
        data = ok("make_map", {"path": "cities.csv", "output": "west.htm",
                               "within": square(72, 15, 76, 27),
                               "overlays": [square(72, 15, 76, 27)]}, ctx)
        assert data["features"] == 4
        page = (root / "west.htm").read_text(encoding="utf-8")
        config = json.loads(json.loads(_CONFIG_RE.search(page).group(1)))
        assert len(config["overlays"]) == 1

    def test_make_map_heatmap(self, ctx, root):
        ok("make_map", {"path": "cities.csv", "output": "heat.html", "kind": "heatmap",
                        "intensity_column": "population"}, ctx)
        page = (root / "heat.html").read_text(encoding="utf-8")
        assert "leaflet-heat" in page and "QOREgeo Heatmap" in page

    def test_make_map_png(self, ctx, root):
        data = ok("make_map", {"path": "cities.csv", "output": "img/cities.png",
                               "width": 160, "height": 120}, ctx)
        assert data["path"] == "img/cities.png" and data["view"] == "Static image."
        assert (root / "img" / "cities.png").read_bytes()[:8] == PNG_SIGNATURE

    def test_make_map_svg(self, ctx, root):
        ok("make_map", {"path": "cities.csv", "output": "cities.svg", "title": "Cities"}, ctx)
        svg = ET.parse(root / "cities.svg").getroot()
        assert svg.tag == "{http://www.w3.org/2000/svg}svg"

    def test_make_map_absolute_output_inside_root(self, ctx, root):
        target = root / "abs.html"
        data = ok("make_map", {"path": str(root / "cities.csv"), "output": str(target)}, ctx)
        assert data["path"] == "abs.html" and target.is_file()

    def test_export_csv_escapes_formulas_by_default(self, ctx, root):
        data = ok("export_features", {"path": "cities.csv", "output": "export/all.csv"}, ctx)
        assert data == {"path": "export/all.csv", "features": 7}
        with open(root / "export" / "all.csv", newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert rows[0]["name"] == "Delhi"
        assert rows[0]["note"] == "'=1+1"
        assert rows[1]["note"] == "ok"
        assert rows[0]["latitude"] == "28.6139" and rows[0]["population"] == "32000000"

    def test_export_csv_raw(self, ctx, root):
        ok("export_features", {"path": "cities.csv", "output": "raw.csv", "csv_safe": False}, ctx)
        with open(root / "raw.csv", newline="", encoding="utf-8") as fh:
            assert next(csv.DictReader(fh))["note"] == "=1+1"

    def test_export_geojson_round_trip(self, ctx, root):
        ok("export_features", {"path": "cities.csv", "output": "big.geojson",
                               "where": [{"column": "population", "op": ">=",
                                          "value": 10_000_000}]}, ctx)
        data = json.loads((root / "big.geojson").read_text(encoding="utf-8"))
        assert data["type"] == "FeatureCollection"
        assert [f["properties"]["name"] for f in data["features"]] == \
            ["Delhi", "Mumbai", "Chennai", "Kolkata"]
        assert data["features"][0]["properties"]["note"] == "=1+1"   # GeoJSON is not escaped
        again = ok("query_features", {"path": "big.geojson"}, ctx)
        assert again["features"] == ok("query_features", {"path": "cities.csv", "where": [
            {"column": "population", "op": ">=", "value": 10_000_000}]}, ctx)["features"]

    def test_export_inline_geojson_to_tsv(self, ctx, root):
        ok("export_features", {"geojson": points_fc(3), "output": "p.tsv"}, ctx)
        lines = (root / "p.tsv").read_text(encoding="utf-8").splitlines()
        assert lines[0].split("\t") == ["latitude", "longitude", "i"]
        assert len(lines) == 4


class TestOverwrite:
    @pytest.mark.parametrize("tool, output", [("make_map", "taken.html"),
                                              ("make_map", "taken.svg"),
                                              ("export_features", "taken.csv"),
                                              ("export_features", "taken.geojson")])
    def test_existing_output_needs_overwrite(self, ctx, root, tool, output):
        target = root / output
        target.write_text("precious", encoding="utf-8")
        message = err(tool, {"path": "cities.csv", "output": output}, ctx)
        assert "overwrite" in message and "exists" in message
        assert target.read_text(encoding="utf-8") == "precious"
        for not_true in (False, "true", 1):
            err(tool, {"path": "cities.csv", "output": output, "overwrite": not_true}, ctx)
            assert target.read_text(encoding="utf-8") == "precious"

        ok(tool, {"path": "cities.csv", "output": output, "overwrite": True}, ctx)
        assert target.read_text(encoding="utf-8") != "precious"

    def test_new_file_needs_no_overwrite(self, ctx, root):
        ok("export_features", {"path": "cities.csv", "output": "fresh.csv"}, ctx)
        ok("export_features", {"path": "cities.csv", "output": "fresh2.csv", "overwrite": True},
           ctx)
        assert (root / "fresh.csv").is_file() and (root / "fresh2.csv").is_file()


class TestGeocodingTools:
    def test_geocode_with_fake_geocoder(self, root):
        fake = FakeGeocoder()
        ctx = ToolContext(root=str(root), allow_network=True, geocoder=fake)
        data = ok("geocode", {"query": "Gateway of India", "limit": 3, "country_codes": ["in"]},
                  ctx)
        assert data["query"] == "Gateway of India"
        assert data["results"][0]["name"] == "Gateway of India, Mumbai"
        assert fake.calls == [("search", ("Gateway of India",),
                               {"limit": 3, "country_codes": ["in"]})]

    def test_geocode_defaults(self, root):
        fake = FakeGeocoder()
        ok("geocode", {"query": "x"}, ToolContext(root=str(root), allow_network=True,
                                                  geocoder=fake))
        assert fake.calls[0][2] == {"limit": 5, "country_codes": None}

    def test_reverse_geocode_with_fake_geocoder(self, root):
        fake = FakeGeocoder()
        ctx = ToolContext(root=str(root), allow_network=True, geocoder=fake)
        data = ok("reverse_geocode", {"point": {"lat": 18.92, "lng": 72.83}}, ctx)
        assert data["result"]["name"] == "Somewhere, Mumbai"
        assert ok("reverse_geocode", {"point": {"lat": 0, "lng": 0}}, ctx) == {"result": None}
        assert [c[1] for c in fake.calls] == [(18.92, 72.83), (0, 0)]

    @pytest.mark.parametrize("tool, args", [
        ("geocode", {"query": "Mumbai"}),
        ("reverse_geocode", {"point": {"lat": 1, "lng": 2}}),
    ])
    def test_disabled_without_network(self, root, tool, args):
        fake = FakeGeocoder()
        ctx = ToolContext(root=str(root), allow_network=False, geocoder=fake)
        message = err(tool, args, ctx)
        assert "disabled" in message.lower()
        assert fake.calls == []

    def test_disabled_by_default(self, ctx):
        assert ctx.allow_network is False
        assert "disabled" in err("geocode", {"query": "Mumbai"}, ctx).lower()

    @pytest.mark.parametrize("query", ["", "   ", 5, None])
    def test_geocode_bad_query(self, root, query):
        fake = FakeGeocoder()
        ctx = ToolContext(root=str(root), allow_network=True, geocoder=fake)
        assert "query" in err("geocode", {"query": query}, ctx)
        assert fake.calls == []


# ─────────────────────────────────────────────────────────────────────────────
# call_tool — error paths (never raise)
# ─────────────────────────────────────────────────────────────────────────────

class TestCallErrors:
    def test_unknown_tool(self, ctx):
        message = err("teleport", {}, ctx)
        assert "teleport" in message and "distance" in message

    @pytest.mark.parametrize("arguments", [[1, 2], "x", 5, True, ("a",)])
    def test_arguments_not_an_object(self, ctx, arguments):
        assert "object" in err("distance", arguments, ctx)

    def test_none_arguments_are_empty(self, ctx):
        assert "from" in err("distance", None, ctx)
        assert not call_tool("list_files", None, ctx).is_error

    def test_missing_required(self, ctx):
        message = err("distance", {"from": DELHI}, ctx)
        assert "to" in message and "Missing" in message
        message = err("destination", {}, ctx)
        assert "start" in message and "bearing" in message and "distance" in message

    @pytest.mark.parametrize("bad", ["abc", {"lat": 1}, [1, 2, 3], None, 5])
    def test_bad_point_shape(self, ctx, bad):
        assert "from" in err("distance", {"from": bad, "to": MUMBAI}, ctx)

    @pytest.mark.parametrize("bad", [{"lat": 100, "lng": 0}, {"lat": 0, "lng": 200},
                                     {"lat": "north", "lng": 0}])
    def test_bad_point_values(self, ctx, bad):
        message = err("distance", {"from": bad, "to": MUMBAI}, ctx)
        assert "Coordinate" in message or "lat" in message

    def test_bad_unit_and_method(self, ctx):
        assert "parsec" in err("distance", {"from": DELHI, "to": MUMBAI, "unit": "parsec"}, ctx)
        assert "euclid" in err("distance", {"from": DELHI, "to": MUMBAI, "method": "euclid"},
                               ctx)

    def test_bad_geometry(self, ctx):
        err("measure", {"geometry": {"type": "Point", "coordinates": [0, 0]}}, ctx)
        err("measure", {"geometry": "polygon"}, ctx)
        err("intersects", {"a": 5, "b": square(0, 0, 1, 1)}, ctx)
        err("contains", {"point": DELHI, "area": {"type": "LineString",
                                                  "coordinates": [[0, 0], [1, 1]]}}, ctx)

    def test_buffer_num_points_above_schema_maximum(self, ctx):
        assert TOOLS["buffer"].input_schema["properties"]["num_points"]["maximum"] == 4096
        assert "num_points" in err("buffer", {"center": DELHI, "radius": 1,
                                              "num_points": 4097}, ctx)
        assert "num_points" in err("buffer", {"center": DELHI, "radius": 1,
                                              "num_points": 10**12}, ctx)
        assert len(ok("buffer", {"center": DELHI, "radius": 1,
                                 "num_points": 4096}, ctx)["geometry"]["coordinates"][0]) == 4097

    def test_path_dot_dot_escape(self, tmp_path, ctx):
        (tmp_path / "outside.csv").write_text(CITIES_CSV, encoding="utf-8")
        message = err("describe_dataset", {"path": "../outside.csv"}, ctx)
        assert "outside the allowed directory" in message
        err("describe_dataset", {"path": "data/../../outside.csv"}, ctx)
        err("list_files", {"directory": ".."}, ctx)

    def test_path_absolute_escape(self, tmp_path, ctx):
        outside = tmp_path / "outside.csv"
        outside.write_text(CITIES_CSV, encoding="utf-8")
        assert "outside the allowed directory" in err("describe_dataset",
                                                      {"path": str(outside)}, ctx)

    def test_path_home_escape(self, ctx):
        assert "outside" in err("describe_dataset", {"path": "~/cities.csv"}, ctx)

    def test_path_symlink_escape(self, tmp_path, ctx, root):
        if not can_symlink(tmp_path):
            pytest.skip("symlinks are not available here")
        outside = tmp_path / "secret.csv"
        outside.write_text(CITIES_CSV, encoding="utf-8")
        os.symlink(outside, root / "link.csv")
        assert "outside the allowed directory" in err("describe_dataset",
                                                      {"path": "link.csv"}, ctx)
        os.symlink(tmp_path, root / "up")
        err("describe_dataset", {"path": "up/secret.csv"}, ctx)
        err("export_features", {"path": "cities.csv", "output": "up/stolen.csv"}, ctx)
        assert not (tmp_path / "stolen.csv").exists()

    def test_symlink_inside_root_is_fine(self, tmp_path, ctx, root):
        if not can_symlink(tmp_path):
            pytest.skip("symlinks are not available here")
        os.symlink(root / "cities.csv", root / "alias.csv")
        assert ok("describe_dataset", {"path": "alias.csv"}, ctx)["count"] == 7

    def test_absolute_path_inside_root(self, ctx, root):
        assert ok("describe_dataset", {"path": str(root / "cities.csv")}, ctx)["count"] == 7

    @pytest.mark.parametrize("path", ["", "   ", 5, ["cities.csv"]])
    def test_path_not_a_string(self, ctx, path):
        assert "path" in err("describe_dataset", {"path": path}, ctx)

    def test_missing_file(self, ctx):
        message = err("describe_dataset", {"path": "nope.csv"}, ctx)
        assert "nope.csv" in message and "list_files" in message

    def test_directory_is_not_a_file(self, ctx):
        err("describe_dataset", {"path": "data"}, ctx)

    def test_missing_directory(self, ctx):
        assert "not a directory" in err("list_files", {"directory": "nope"}, ctx)

    def test_file_too_large(self, root):
        ctx = ToolContext(root=str(root), max_file_mb=0.0001)   # 100 bytes
        message = err("describe_dataset", {"path": "cities.csv"}, ctx)
        assert "cities.csv" in message and "limit" in message

    @pytest.mark.parametrize("args", [
        {},
        {"path": "cities.csv", "geojson": points_fc(1)},
    ], ids=["neither", "both"])
    def test_path_xor_geojson(self, ctx, args):
        for tool in ("describe_dataset", "query_features", "preview_map"):
            assert "exactly one" in err(tool, args, ctx)

    def test_bad_inline_geojson(self, ctx):
        err("query_features", {"geojson": "not geojson"}, ctx)
        err("query_features", {"geojson": {"type": "FeatureCollection", "features": []}}, ctx)
        err("query_features", {"geojson": {"type": "Nope"}}, ctx)

    def test_bad_filters(self, ctx):
        src = {"path": "cities.csv"}
        assert "near" in err("query_features", {**src, "near": "Mumbai"}, ctx)
        assert "bbox" in err("query_features", {**src, "bbox": [1, 2]}, ctx)
        assert "where" in err("query_features", {**src, "where": [{"op": "=="}]}, ctx)
        assert "popularity" in err("query_features", {**src, "where": [
            {"column": "popularity", "value": 1}]}, ctx)
        err("query_features", {**src, "where": [{"column": "name", "op": "~", "value": 1}]}, ctx)
        err("query_features", {**src, "near": {"lat": 0, "lng": 0, "radius": -5}}, ctx)

    @pytest.mark.parametrize("k", [0, -1, 1.5, True, "3"])
    def test_nearest_bad_k(self, ctx, k):
        assert "'k'" in err("nearest", {"path": "cities.csv", "point": DELHI, "k": k}, ctx)

    @pytest.mark.parametrize("tool", sorted(WRITING_TOOLS))
    def test_writing_disabled(self, root, tool):
        ctx = ToolContext(root=str(root), allow_write=False)
        output = "x.html" if tool == "make_map" else "x.csv"
        message = err(tool, {"path": "cities.csv", "output": output}, ctx)
        assert "disabled" in message.lower()
        assert not (root / output).exists()
        # Reading still works.
        assert ok("describe_dataset", {"path": "cities.csv"}, ctx)["count"] == 7

    @pytest.mark.parametrize("spelling", ["cities.csv", "./cities.csv", "data/../cities.csv"])
    def test_output_is_input(self, ctx, root, spelling):
        for overwrite in (False, True):
            message = err("export_features", {"path": "cities.csv", "output": spelling,
                                              "overwrite": overwrite}, ctx)
            assert "input" in message
        assert (root / "cities.csv").read_text(encoding="utf-8") == CITIES_CSV

    def test_output_is_input_geojson(self, ctx, root):
        before = (root / "zones.geojson").read_bytes()
        assert "input" in err("export_features", {"path": "zones.geojson",
                                                  "output": "zones.geojson",
                                                  "overwrite": True}, ctx)
        assert (root / "zones.geojson").read_bytes() == before

    def test_output_is_input_other_case(self, ctx, root):
        probe = root / "CaseProbe.txt"
        probe.write_text("x", encoding="utf-8")
        if not (root / "caseprobe.txt").exists():
            pytest.skip("case-sensitive file system")
        assert "input" in err("export_features", {"path": "cities.csv", "output": "CITIES.csv",
                                                  "overwrite": True}, ctx)
        assert (root / "cities.csv").read_text(encoding="utf-8") == CITIES_CSV

    @pytest.mark.parametrize("tool, output, expected", [
        ("make_map", "map.txt", ".html"),
        ("make_map", "map.csv", ".svg"),
        ("make_map", "map", ".png"),
        ("export_features", "out.html", ".geojson"),
        ("export_features", "out.xlsx", ".csv"),
    ])
    def test_wrong_output_extension(self, ctx, root, tool, output, expected):
        assert expected in err(tool, {"path": "cities.csv", "output": output}, ctx)
        assert not (root / output).exists()

    @pytest.mark.parametrize("output", ["../escape.html", "data/../../escape.html"])
    def test_output_outside_root(self, tmp_path, ctx, output):
        assert "outside" in err("make_map", {"path": "cities.csv", "output": output}, ctx)
        assert not (tmp_path / "escape.html").exists()

    def test_output_absolute_outside_root(self, tmp_path, ctx):
        target = tmp_path / "escape.csv"
        assert "outside" in err("export_features", {"path": "cities.csv",
                                                    "output": str(target)}, ctx)
        assert not target.exists()

    def test_output_is_directory(self, ctx, root):
        (root / "dir.html").mkdir()
        assert "directory" in err("make_map", {"path": "cities.csv", "output": "dir.html",
                                               "overwrite": True}, ctx)

    def test_output_missing(self, ctx):
        assert "output" in err("make_map", {"path": "cities.csv"}, ctx)

    @pytest.mark.parametrize("output", ["", "   ", 5, None])
    def test_bad_output_names_output(self, ctx, output):
        message = err("export_features", {"path": "cities.csv", "output": output}, ctx)
        assert "'output'" in message and "'path'" not in message

    def test_bad_kind(self, ctx, root):
        assert "kind" in err("make_map", {"path": "cities.csv", "output": "k.html",
                                          "kind": "3d"}, ctx)

    def test_bad_tiles(self, ctx):
        err("make_map", {"path": "cities.csv", "output": "t.html", "tiles": "google"}, ctx)

    def test_bad_image_size(self, ctx):
        err("preview_map", {"path": "cities.csv", "width": 1}, ctx)
        err("make_map", {"path": "cities.csv", "output": "s.png", "width": 10**6}, ctx)

    def test_errors_never_raise(self, full_ctx):
        weird: list[Any] = [None, 0, -1, 1.5, float("nan"), float("inf"), "", "x", "../x.csv",
                            [], [None], [1, 2, 3, 4], {}, {"type": "Point"}, True]
        for name, tool in TOOLS.items():
            for key in tool.input_schema["properties"]:
                for value in weird:
                    result = call_tool(name, {**VALID_ARGS[name], key: value}, full_ctx)
                    assert isinstance(result, ToolResult)
                    json.dumps(result.data, allow_nan=False)
                    if result.is_error:
                        assert result.data["error"]

    def test_bad_root(self, tmp_path):
        with pytest.raises(InvalidArgumentError):
            ToolContext(root=str(tmp_path / "missing"))
        (tmp_path / "file.txt").write_text("x", encoding="utf-8")
        with pytest.raises(InvalidArgumentError):
            ToolContext(root=str(tmp_path / "file.txt"))

    def test_root_is_resolved(self, root, monkeypatch):
        monkeypatch.chdir(root)
        ctx = ToolContext(root=".")
        assert os.path.isabs(ctx.root)
        assert os.path.samefile(ctx.root, root)
        assert ToolContext().root == ctx.root   # default: the current directory


# ─────────────────────────────────────────────────────────────────────────────
# Top-level schema validation
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaValidation:
    def test_every_tool_has_a_valid_call(self, full_ctx):
        assert set(VALID_ARGS) == set(TOOLS)
        for name, args in VALID_ARGS.items():
            ok(name, dict(args), full_ctx)

    @pytest.mark.parametrize("name", sorted(TOOLS))
    def test_unknown_argument_rejected(self, full_ctx, root, name):
        message = err(name, {**VALID_ARGS[name], "colour": "red"}, full_ctx)
        assert "colour" in message and "Unknown" in message
        assert not (root / "every.svg").exists() and not (root / "every.csv").exists()

    @pytest.mark.parametrize("tool, key, value, words", [
        ("distance", "unit", 5, ["unit", "string"]),
        ("distance", "from", "Delhi", ["from", "object"]),
        ("distance", "to", None, ["to", "object"]),
        ("buffer", "num_points", True, ["num_points", "integer"]),
        ("buffer", "num_points", 8.5, ["num_points", "integer"]),
        ("buffer", "radius", "5", ["radius", "number"]),
        ("buffer", "radius", False, ["radius", "number"]),
        ("destination", "bearing", True, ["bearing", "number"]),
        ("query_features", "descending", "yes", ["descending", "boolean"]),
        ("query_features", "descending", 1, ["descending", "boolean"]),
        ("query_features", "where", {"column": "x"}, ["where", "array"]),
        ("query_features", "bbox", "68,6,98,36", ["bbox", "array"]),
        ("geocode", "country_codes", "in", ["country_codes", "array"]),
    ])
    def test_wrong_json_type(self, full_ctx, tool, key, value, words):
        message = err(tool, {**VALID_ARGS[tool], key: value}, full_ctx)
        for word in words:
            assert word in message

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_numbers(self, ctx, value):
        message = err("buffer", {**VALID_ARGS["buffer"], "radius": value}, ctx)
        assert "radius" in message and "finite" in message
        assert "bearing" in err("destination", {**VALID_ARGS["destination"], "bearing": value},
                                ctx)

    @pytest.mark.parametrize("tool, key, value", [
        ("distance", "unit", "parsec"),
        ("distance", "unit", "KM"),
        ("distance", "method", "euclid"),
        ("measure", "area_unit", "km"),
        ("make_map", "kind", "3d"),
        ("make_map", "tiles", "google"),
    ])
    def test_enum(self, ctx, tool, key, value):
        message = err(tool, {**VALID_ARGS[tool], key: value}, ctx)
        assert key in message and "one of" in message

    @pytest.mark.parametrize("tool, key, value, phrase", [
        ("destination", "distance", -1, "at least 0"),
        ("geohash", "precision", 0, "at least 1"),
        ("geohash", "precision", 13, "at most 12"),
        ("buffer", "radius", 0, "greater than 0"),
        ("buffer", "radius", -2.5, "greater than 0"),
        ("buffer", "num_points", 2, "at least 3"),
        ("nearest", "k", 0, "at least 1"),
        ("query_features", "limit", 0, "at least 1"),
        ("query_features", "offset", -1, "at least 0"),
        ("preview_map", "width", 63, "at least 64"),
        ("preview_map", "height", 2049, "at most 2048"),
        ("make_map", "width", 8193, "at most 8192"),
        ("geocode", "limit", 51, "at most 50"),
    ])
    def test_bounds(self, full_ctx, tool, key, value, phrase):
        message = err(tool, {**VALID_ARGS[tool], key: value}, full_ctx)
        assert key in message and phrase in message

    @pytest.mark.parametrize("bbox, phrase", [([1, 2, 3], "at least 4"),
                                              ([1, 2, 3, 4, 5], "at most 4"),
                                              ([], "at least 4")])
    def test_array_length(self, ctx, bbox, phrase):
        message = err("query_features", {"path": "cities.csv", "bbox": bbox}, ctx)
        assert "bbox" in message and phrase in message

    def test_boundary_values_pass(self, full_ctx):
        ok("destination", {**VALID_ARGS["destination"], "distance": 0}, full_ctx)
        ok("geohash", {"point": DELHI, "precision": 12}, full_ctx)
        ok("geohash", {"point": DELHI, "precision": 1}, full_ctx)
        ok("buffer", {**VALID_ARGS["buffer"], "radius": 0.001, "num_points": 3}, full_ctx)
        ok("query_features", {"path": "cities.csv", "offset": 0, "limit": 1}, full_ctx)
        ok("preview_map", {"path": "cities.csv", "width": 64, "height": 64}, full_ctx)
        ok("geocode", {"query": "x", "limit": 50}, full_ctx)

    def test_validation_runs_before_side_effects(self, root):
        fake = FakeGeocoder()
        ctx = ToolContext(root=str(root), allow_network=True, geocoder=fake)
        err("geocode", {"query": "x", "limit": 0}, ctx)
        err("make_map", {"path": "cities.csv", "output": "v.html", "tiles": "nope"}, ctx)
        assert fake.calls == []
        assert not (root / "v.html").exists()

    @pytest.mark.parametrize("tool, args", [
        ("buffer", {"center": DELHI, "radius": 10**400}),
        ("destination", {"start": DELHI, "bearing": 10**400, "distance": 1}),
    ], ids=["radius", "bearing"])
    def test_huge_integer_for_number(self, ctx, tool, args):
        err(tool, args, ctx)

    def test_huge_integer_for_integer(self, ctx):
        assert "num_points" in err("buffer", {**VALID_ARGS["buffer"], "num_points": 10**400},
                                   ctx)


# ─────────────────────────────────────────────────────────────────────────────
# Paging
# ─────────────────────────────────────────────────────────────────────────────

class TestPaging:
    def test_max_features_truncation(self, root):
        ctx = ToolContext(root=str(root), max_features=3)
        data = ok("query_features", {"path": "cities.csv"}, ctx)
        assert (data["count"], data["returned"], data["truncated"]) == (7, 3, True)
        assert data["offset"] == 0 and data["next_offset"] == 3
        assert names_of(data) == CITY_NAMES[:3]

    def test_not_truncated(self, ctx):
        data = ok("query_features", {"path": "cities.csv"}, ctx)
        assert (data["count"], data["returned"], data["truncated"]) == (7, 7, False)
        assert "next_offset" not in data

    def test_offset_pages_through_everything(self, root):
        ctx = ToolContext(root=str(root), max_features=3)
        seen: list[str] = []
        offset = 0
        for _ in range(10):
            data = ok("query_features", {"path": "cities.csv", "offset": offset}, ctx)
            assert data["offset"] == offset
            seen += names_of(data)
            if not data["truncated"]:
                assert "next_offset" not in data
                break
            offset = data["next_offset"]
        assert seen == CITY_NAMES

    def test_offset_past_the_end(self, ctx):
        data = ok("query_features", {"path": "cities.csv", "offset": 50}, ctx)
        assert data == {"count": 7, "offset": 50, "returned": 0, "truncated": False,
                        "features": []}

    def test_offset_after_filters(self, ctx):
        data = ok("query_features", {"path": "cities.csv", "sort_by": "population",
                                     "offset": 5}, ctx)
        assert names_of(data) == ["Mumbai", "Delhi"]

    @pytest.mark.parametrize("offset", [-1, 1.5, True, "1"])
    def test_bad_offset(self, ctx, offset):
        assert "offset" in err("query_features", {"path": "cities.csv", "offset": offset}, ctx)
        assert "offset" in err("nearest", {"path": "cities.csv", "point": DELHI,
                                           "offset": offset}, ctx)

    def test_nearest_pages(self, root):
        ctx = ToolContext(root=str(root), max_features=2)
        first = ok("nearest", {"path": "cities.csv", "point": DELHI, "k": 5}, ctx)
        assert (first["count"], first["returned"], first["truncated"]) == (5, 2, True)
        second = ok("nearest", {"path": "cities.csv", "point": DELHI, "k": 5,
                                "offset": first["next_offset"]}, ctx)
        assert second["offset"] == 2
        distances = [f["properties"]["_distance"] for f in first["features"] + second["features"]]
        assert distances == sorted(distances)
        assert names_of(first) == ["Delhi", "Jaipur"]

    def test_character_cap(self, ctx):
        data = ok("query_features", {"geojson": points_fc(100, pad=1000)}, ctx)
        result = call_tool("query_features", {"geojson": points_fc(100, pad=1000)}, ctx)
        assert len(result.text) <= 41_000
        assert data["count"] == 100
        assert 0 < data["returned"] < 100
        assert data["truncated"] is True
        assert data["next_offset"] == data["returned"]
        assert [f["properties"]["i"] for f in data["features"]] == list(range(data["returned"]))
        nxt = ok("query_features", {"geojson": points_fc(100, pad=1000),
                                    "offset": data["next_offset"]}, ctx)
        assert nxt["features"][0]["properties"]["i"] == data["returned"]

    def test_oversized_feature_does_not_stall_paging(self, ctx):
        fc = points_fc(3)
        fc["features"][1]["properties"]["pad"] = "y" * 50_000
        data = ok("query_features", {"geojson": fc}, ctx)
        assert data["returned"] == 1 and data["next_offset"] == 1   # stops before the giant
        data = ok("query_features", {"geojson": fc, "offset": 1}, ctx)
        assert data["returned"] == 1 and data["next_offset"] == 2   # the giant on its own
        assert len(data["features"][0]["properties"]["pad"]) == 50_000
        data = ok("query_features", {"geojson": fc, "offset": 2}, ctx)
        assert data["returned"] == 1 and data["truncated"] is False


# ─────────────────────────────────────────────────────────────────────────────
# ToolResult serialisation
# ─────────────────────────────────────────────────────────────────────────────

class TestToolResult:
    def test_to_mcp_success(self):
        result = ToolResult({"distance": 1.5, "name": "São Paulo"})
        mcp = result.to_mcp()
        assert set(mcp) == {"content", "isError", "structuredContent"}
        assert mcp["isError"] is False
        assert mcp["structuredContent"] == result.data
        (block,) = mcp["content"]
        assert block["type"] == "text"
        assert json.loads(block["text"]) == result.data
        assert "São Paulo" in block["text"]   # not \u-escaped

    def test_to_mcp_error(self):
        mcp = ToolResult({"error": "nope"}, is_error=True).to_mcp()
        assert mcp["isError"] is True
        assert "structuredContent" not in mcp
        assert json.loads(mcp["content"][0]["text"]) == {"error": "nope"}

    def test_to_mcp_images(self):
        result = ToolResult({"n": 1}, images=[("image/png", b"\x89PNG..."),
                                              ("image/svg+xml", b"<svg/>")])
        text, png, svg = result.to_mcp()["content"]
        assert text["type"] == "text"
        assert png == {"type": "image", "data": base64.b64encode(b"\x89PNG...").decode("ascii"),
                       "mimeType": "image/png"}
        assert base64.b64decode(svg["data"]) == b"<svg/>" and svg["mimeType"] == "image/svg+xml"

    def test_to_anthropic_success(self):
        block = ToolResult({"a": 1}).to_anthropic("toolu_123")
        assert block == {"type": "tool_result", "tool_use_id": "toolu_123",
                         "content": [{"type": "text", "text": '{"a":1}'}]}
        assert "is_error" not in block

    def test_to_anthropic_error(self):
        block = ToolResult({"error": "bad"}, is_error=True).to_anthropic("toolu_9")
        assert block["is_error"] is True
        assert json.loads(block["content"][0]["text"]) == {"error": "bad"}

    def test_to_anthropic_images(self):
        block = ToolResult({"n": 1}, images=[("image/png", b"PNGDATA")]).to_anthropic("t")
        image = block["content"][1]
        assert image == {"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.b64encode(b"PNGDATA").decode("ascii")}}

    def test_preview_map_round_trip(self, ctx):
        result = call_tool("preview_map", {"path": "cities.csv", "width": 64, "height": 64}, ctx)
        mcp = result.to_mcp()
        assert mcp["content"][1]["mimeType"] == "image/png"
        assert base64.b64decode(mcp["content"][1]["data"])[:8] == PNG_SIGNATURE
        claude = result.to_anthropic("toolu_1")
        assert base64.b64decode(claude["content"][1]["source"]["data"]) == result.images[0][1]
        assert mcp["structuredContent"] == result.data

    def test_error_result_from_call_tool(self, ctx):
        result = call_tool("nope", {}, ctx)
        assert result.to_mcp()["isError"] is True
        assert result.to_anthropic("x")["is_error"] is True
        assert result.images == []
