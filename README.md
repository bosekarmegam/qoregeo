# QOREgeo

**Zero-dependency geospatial toolkit for Python — and for AI assistants.**

[![PyPI version](https://img.shields.io/pypi/v/qoregeo.svg?logo=pypi&logoColor=white)](https://pypi.org/project/qoregeo)
[![Python versions](https://img.shields.io/pypi/pyversions/qoregeo.svg?logo=python&logoColor=white)](https://pypi.org/project/qoregeo)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://github.com/bosekarmegam/qoregeo/actions/workflows/tests.yml/badge.svg)](https://github.com/bosekarmegam/qoregeo/actions)
[![Zero dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)](https://pypi.org/project/qoregeo)
[![MCP server](https://img.shields.io/badge/MCP-server-8A2BE2)](#ai-assistants-mcp-skills-claude-codex)

<!-- mcp-name: io.github.bosekarmegam/qoregeo -->

---

Distances, nearest places, geofences, buffers, intersections, CSV/GeoJSON filtering,
OpenStreetMap maps and heatmaps, static PNG/SVG map images and geocoding — in **pure Python**.
No GDAL, GEOS or PROJ, no API keys, no compiled wheels: QOREgeo installs wherever Python 3.10+
runs — Windows, macOS, Linux, Raspberry Pi, AWS Lambda, slim Docker images, Pyodide, and AI
code sandboxes without internet access.

It also ships everything an AI assistant needs to do GIS work: an **MCP server** (Claude Code,
Claude Desktop, OpenAI Codex, Cursor, VS Code, Gemini CLI, …), an **Agent Skill**, **Claude /
OpenAI tool schemas** and a **CLI**.

## Why QOREgeo?

```
$ pip install gdal
  ...
      Could not find gdal-config. Make sure you have installed the GDAL native library and development headers.
ERROR: Failed to build 'gdal' when getting requirements to build wheel
```

The classic Python GIS stack sits on GDAL, GEOS and PROJ — large C/C++ libraries. Pre-built
wheels cover the common platforms (the GeoPandas core stack is about 74 MB of wheels), but
whenever none matches your platform or Python version, pip has to compile them; newer releases
also drop older Pythons (shapely 2.2 needs 3.11+, pyproj 3.8 needs 3.12+).

QOREgeo covers the everyday 80 % — distance, nearest, radius / polygon / attribute filters,
geofencing, maps — with a ~160 KB pure-Python wheel and no dependencies at all.

| You need | QOREgeo | Typical alternative |
|---|---|---|
| Distance, bearing, buffers, point-in-polygon, area | ✅ stdlib only | shapely + pyproj (compiled) |
| Load CSV / GeoJSON, filter, nearest, export | ✅ | geopandas (+ numpy, pandas, pyogrio, GDAL) |
| Interactive maps with no API key | ✅ OpenStreetMap, 9 open basemaps (+ `none`) | folium (+ jinja2, requests, numpy …) |
| Static map images without a browser | ✅ PNG / SVG, pure Python | matplotlib / contextily |
| Geocoding | ✅ Nominatim / Photon / self-hosted | geopy |
| Tools for AI assistants | ✅ MCP server, Agent Skill, Claude/OpenAI schemas | — |

---

## Installation

```bash
pip install qoregeo          # library + `qoregeo` command
uvx qoregeo --help           # or run the CLI / MCP server without installing (uv)
```

**Requirements:** Python 3.10+ · Zero external dependencies

> Python 3.8 / 3.9 are end-of-life; pip on those versions automatically installs QOREgeo 1.0.3.

---

## Quick Start

```python
from qoregeo import GeoEngine

geo = GeoEngine()
geo.load("cities.csv")

# ── Distance ─────────────────────────────────────────
delhi  = (28.6139, 77.2090)
mumbai = (19.0760, 72.8777)

km = geo.distance(delhi, mumbai)
print(km)          # 1148.0965

miles = geo.distance(delhi, mumbai, unit="miles")
print(miles)       # 713.3941

exact = geo.distance(delhi, mumbai, method="vincenty")   # WGS-84 ellipsoid
print(exact)       # 1144.5264

# ── Compass bearing ───────────────────────────────────
direction = geo.bearing(delhi, mumbai)
print(direction)   # South-Southwest

degrees = geo.bearing(delhi, mumbai, as_degrees=True)
print(degrees)     # 203.47

# ── Geofencing ────────────────────────────────────────
zone = geo.buffer(delhi, radius=10, unit="km")

inside = geo.point_in_polygon(mumbai, zone)
print(inside)      # False

close  = (28.65, 77.22)
inside = geo.point_in_polygon(close, zone)
print(inside)      # True

# ── Filter by radius ──────────────────────────────────
nearby = geo.filter_by_radius(19.07, 72.87, radius=700)
print(nearby.count())  # 3   (Mumbai, Pune, Hyderabad — nearest first)

# ── Find nearest ──────────────────────────────────────
result = geo.nearest(delhi)
print(result["feature"]["properties"]["name"])  # Delhi
print(result["distance"])                        # 0.0

# ── Filter by property ────────────────────────────────
mh = geo.filter("state", "Maharashtra")
print(mh.count())  # 2

big = geo.filter("population", 15_000_000, op=">")
print(big.count()) # 2

# ── Maps ──────────────────────────────────────────────
geo.map("output.html", title="India Cities")          # interactive (OpenStreetMap)
geo.heatmap("heat.html", intensity_col="population")
geo.image("cities.png", title="India Cities")          # static PNG/SVG, works offline

# ── Save data ─────────────────────────────────────────
geo.save("output.geojson")
geo.save("output.csv")

# ── View the map in the browser ───────────────────────
geo.show()   # serves it on http://127.0.0.1 — a script keeps serving until Ctrl+C, so call it last
```

<details>
<summary><code>cities.csv</code> used in the examples</summary>

```csv
name,state,population,latitude,longitude
Delhi,Delhi,32900000,28.6139,77.2090
Mumbai,Maharashtra,20700000,19.0760,72.8777
Bangalore,Karnataka,13200000,12.9716,77.5946
Kolkata,West Bengal,14900000,22.5726,88.3639
Chennai,Tamil Nadu,11300000,13.0827,80.2707
Pune,Maharashtra,7400000,18.5204,73.8567
Hyderabad,Telangana,10500000,17.3850,78.4867
```

</details>

> **Viewing maps:** the default basemap is OpenStreetMap, whose tile servers only serve web pages —
> open maps with `geo.show()` or `qoregeo serve output.html` rather than double-clicking the file.
> In a script both keep serving until you press Ctrl+C. Only `tiles="satellite"`, `"topo"`,
> `"osm-de"` and `"none"` work when the file is opened from disk. See [Basemaps](#basemaps).

---

## Method Chaining

Loaders (`load`, `load_data`, `add_point`) and outputs (`save`, `map`, `heatmap`, `image`) return `self`; filters return a **new** `GeoEngine` and never modify the original. Together that gives Pandas-style pipelines:

```python
from qoregeo import GeoEngine

(GeoEngine()
    .load("all_stores.csv")
    .filter("state", "Maharashtra")
    .filter_by_radius(19.07, 72.87, radius=100)
    .save("results.geojson")
    .map("results.html", title="Mumbai Stores")
)
```

```python
geo = GeoEngine().load("cities.csv")
mh  = geo.filter("state", "Maharashtra")
print(geo.count(), mh.count())   # 7 2  — the original is untouched
```

---

## Command Line

Every common task works from the shell (`qoregeo --help`, `qoregeo <command> --help`):

```bash
qoregeo info stores.csv                                    # rows, columns, bbox, centre
qoregeo distance 28.6139,77.2090 19.0760,72.8777 --unit miles
qoregeo export stores.csv -o near.geojson --near 28.61,77.21,5 --sort revenue --desc --limit 20
qoregeo export stores.csv -o big.csv --where "revenue>=100000" --where 'city in ["Delhi","Pune"]'
qoregeo map stores.csv -o stores.html --tiles topo --serve # interactive map, served until Ctrl+C
qoregeo map stores.csv -o heat.html --heatmap --intensity revenue
qoregeo image stores.csv -o stores.png --within zone.geojson --labels name
qoregeo serve stores.html                                  # view any saved map (until Ctrl+C)
qoregeo geocode "Gateway of India, Mumbai" --country in
qoregeo reverse 18.9220,72.8347
qoregeo tiles                                              # list basemaps
```

Filters for `map`, `image` and `export`: `--near LAT,LNG,RADIUS [--unit]`, `--bbox W,S,E,N`,
`--within FILE.geojson`, `--where EXPR` (repeatable; `== != > >= < <= in "not in" contains`),
`--sort COLUMN [--desc]`, `--limit N`. `export` escapes spreadsheet formulas in CSV cells
(`--raw-csv` turns that off) and never overwrites its input.

---

## AI Assistants (MCP, Skills, Claude, Codex)

QOREgeo gives AI assistants reliable, offline GIS: instead of guessing distances or writing
throw-away GeoPandas code, the model calls tested tools and can **look at a rendered map** of
its result.

### MCP server

`qoregeo mcp` is a zero-dependency [Model Context Protocol](https://modelcontextprotocol.io)
server on stdio. It speaks every protocol revision from 2024-11-05 to 2026-07-28, so current
and upcoming clients all work.

| Tool | What it does |
|---|---|
| `distance`, `bearing`, `destination`, `midpoint` | point-to-point geodesy (haversine or Vincenty) |
| `buffer`, `contains`, `intersects`, `measure`, `geohash` | zones, geofencing, overlap tests, area / length |
| `list_files`, `describe_dataset` | find data files; columns, types, bbox, sample rows |
| `query_features`, `nearest` | filter by radius / bbox / area / attributes, sort, page; k nearest |
| `preview_map` | renders the data as a labelled PNG **the model can see** |
| `make_map`, `export_features` | write HTML maps, heatmaps, PNG/SVG, CSV or GeoJSON *(write access)* |
| `geocode`, `reverse_geocode` | OpenStreetMap Nominatim or your own server *(opt-in)* |

**Safety:** files are confined to one folder (`--root`, else `$QOREGEO_ROOT`,
`$CLAUDE_PROJECT_DIR` or the working directory; `..` and symlinks can't escape); writers never
overwrite without `"overwrite": true` and are hidden with `--read-only`; geocoding is off unless
whoever starts the server opts in (`--geocoder-url` for a self-hosted server, or
`--allow-network` for the public OpenStreetMap service and its
[usage policy](https://operations.osmfoundation.org/policies/nominatim/)); results are paged and
capped below clients' context limits.

**Claude Code**

```bash
claude mcp add --scope user qoregeo -- uvx qoregeo mcp
```

or commit a project `.mcp.json` (this repository has one):

```json
{ "mcpServers": { "qoregeo": { "command": "uvx", "args": ["qoregeo", "mcp"] } } }
```

**Claude Code plugin** (MCP server + skill in one step):

```
/plugin marketplace add bosekarmegam/qoregeo
/plugin install qoregeo@qoregeo
```

**Claude Desktop** — `claude_desktop_config.json` (Settings → Developer → Edit Config); use
absolute paths:

```json
{
  "mcpServers": {
    "qoregeo": {
      "command": "/absolute/path/to/uvx",
      "args": ["qoregeo", "mcp", "--root", "/Users/you/gis-data"]
    }
  }
}
```

**OpenAI Codex**

```bash
codex mcp add qoregeo -- uvx qoregeo mcp
# or in ~/.codex/config.toml:
#   [mcp_servers.qoregeo]
#   command = "uvx"
#   args = ["qoregeo", "mcp"]
```

Codex can also install the plugin: `codex plugin marketplace add bosekarmegam/qoregeo`, then
`codex plugin add qoregeo@qoregeo`.

**Gemini CLI**

```bash
gemini mcp add -s user qoregeo uvx qoregeo mcp
# or as an extension (MCP server + skill):
gemini extensions install https://github.com/bosekarmegam/qoregeo
```

**Cursor** — `.cursor/mcp.json` · **VS Code / GitHub Copilot** — `.vscode/mcp.json`:

```json
{ "mcpServers": { "qoregeo": { "command": "uvx", "args": ["qoregeo", "mcp"] } } }
```

```json
{ "servers": { "qoregeo": { "type": "stdio", "command": "uvx", "args": ["qoregeo", "mcp", "--root", "${workspaceFolder}"] } } }
```

Without `uv`, use `"command": "qoregeo"` (after `pip install qoregeo`) or
`"command": "python", "args": ["-m", "qoregeo", "mcp"]`. Options: `qoregeo mcp --help`.

### Agent Skill

The [`qoregeo` skill](https://github.com/bosekarmegam/qoregeo/blob/main/skills/qoregeo/SKILL.md)
teaches an agent when and how to use QOREgeo — CLI, MCP tools or Python — including the
coordinate-order and tile/geocoding policy pitfalls.

```bash
qoregeo skill install                 # ~/.claude/skills (Claude Code) + ~/.agents/skills (Codex, Gemini CLI, Copilot)
qoregeo skill install --scope project # ./.claude/skills + ./.agents/skills
qoregeo skill zip                     # qoregeo-skill.zip for claude.ai (Settings → Capabilities → Skills)
```

The zip **includes the QOREgeo package itself**, so the skill also works in sandboxes with no
internet and no pip — such as Claude's code-execution environment.

### Claude API and OpenAI function calling

```python
from qoregeo.tools import ToolContext, call_tool, tool_definitions

tools = tool_definitions("anthropic")       # or "openai", "openai-responses", "mcp"
ctx = ToolContext(root="data", allow_write=False)

# inside your tool-use loop, for each tool_use block:
result = call_tool(block.name, block.input, ctx)
tool_result = result.to_anthropic(block.id)  # text + the PNG from preview_map
```

Complete, runnable loops:
[`examples/claude_tools.py`](https://github.com/bosekarmegam/qoregeo/blob/main/examples/claude_tools.py)
(Messages API) and
[`examples/claude_mcp.py`](https://github.com/bosekarmegam/qoregeo/blob/main/examples/claude_mcp.py)
(MCP + the SDK's tool runner). Any agent with a shell can also run a tool directly:
`qoregeo call nearest '{"path": "stores.csv", "point": {"lat": 28.6, "lng": 77.2}, "k": 3}'`.

Also in the repository: [`AGENTS.md`](https://github.com/bosekarmegam/qoregeo/blob/main/AGENTS.md)
(for coding agents contributing here), [`llms.txt`](https://github.com/bosekarmegam/qoregeo/blob/main/llms.txt),
and the MCP Registry manifest [`server.json`](https://github.com/bosekarmegam/qoregeo/blob/main/server.json).

---

## API Reference

The examples below assume:

```python
from qoregeo import GeoEngine

geo    = GeoEngine().load("cities.csv")
delhi  = (28.6139, 77.2090)
mumbai = (19.0760, 72.8777)
```

Points are always `(lat, lng)` tuples (a `(lat, lng, alt)` tuple, a `{"lat": …, "lng": …}` dict, a GeoJSON `Point` or a Point `Feature` also work). GeoJSON coordinates are `[lng, lat]`, as RFC 7946 requires.

### `GeoEngine(verbose=True)`

The single entry point for all operations. With `verbose=False` nothing is printed (by default `map()` / `heatmap()` print a one-line `✅ Map saved → …` confirmation).

```python
geo = GeoEngine(verbose=False)
```

---

### Loading Data

#### `geo.load(path, lat_col=None, lng_col=None, encoding='utf-8-sig', delimiter=None, infer_types=False)` → `self`

Load a delimited text file (`.csv`, `.tsv`, `.txt`) or a GeoJSON file (`.geojson`, `.json`). `path` may be a `str` or a `pathlib.Path`.

```python
geo.load("data.csv")
geo.load("data.geojson")
geo.load("data.csv", lat_col="y_coord", lng_col="x_coord")
geo.load("data.txt", delimiter="|")
geo.load("data.csv", infer_types=True)
```

- **Coordinate columns** are auto-detected (case- and whitespace-insensitive). Pass `lat_col` / `lng_col` to override either one; the other is still auto-detected.

  **Latitude:** `latitude`, `lat`, `y`, `ylat`, `lat_deg`, `latitude_deg`, `geo_lat`, `point_lat`

  **Longitude:** `longitude`, `lng`, `lon`, `long`, `x`, `xlong`, `lng_deg`, `longitude_deg`, `geo_lng`, `geo_long`, `point_lng`

- **Delimiter** — comma, semicolon, tab or pipe, sniffed from the header line (`.tsv` files are always tab-separated). Pass `delimiter=` to force one.
- **Values** stay strings by default. `infer_types=True` converts them to `int` / `float` / `bool` / `None` where that is unambiguous — values with leading zeros such as ZIP codes and IDs (`"00123"`), a leading `+` (phone numbers), and decimals with more significant digits than a float keeps (> 15) stay strings. Floats are normalised (`"19.90"` → `19.9`).
- **Bad rows** — rows with blank coordinates are skipped silently; rows with non-numeric or out-of-range coordinates are skipped with **one** summary warning (often a sign the lat/lng columns are swapped); extra values beyond the header are dropped with a warning.
- The default `utf-8-sig` encoding transparently handles the BOM that Excel writes.

#### GeoJSON support

Every RFC 7946 geometry type loads, filters, saves and renders: `Point`, `MultiPoint`, `LineString`, `MultiLineString`, `Polygon`, `MultiPolygon` and `GeometryCollection`.

- A file may hold a `FeatureCollection`, a single `Feature`, a bare Geometry, or a list of Features.
- `"geometry": null` features are kept (with their properties) but have no location, so spatial operations skip them; `"properties": null` becomes `{}`.
- Spatial operations on non-Point features (`nearest`, `filter_by_radius`, `filter_by_bbox`, `filter_by_polygon`, `filter_by_line`, `k_nearest`, `centroid`, heatmaps) use a representative point — the area centroid for polygons, the vertex mean otherwise. `bounds()` / `bbox()` use every vertex.
- Features with an unknown geometry type or an out-of-range Point are skipped with one warning. Invalid JSON raises `InvalidGeoJSONError`.

#### `geo.load_data(features)` → `self`

Load GeoJSON directly from Python objects (no file needed). Accepts a list of Feature or Geometry dicts, a `FeatureCollection` dict, a single `Feature`, a bare Geometry, or any iterable (e.g. a generator) of Features. Geometries and the top-level property dicts are copied, so adding or replacing properties on your objects afterwards doesn't affect the engine (nested values such as lists inside properties are shared).

```python
geo.load_data([
    {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [77.20, 28.61]},
        "properties": {"name": "Delhi"}
    }
])
geo.load_data({"type": "Point", "coordinates": [77.20, 28.61]})   # bare geometry
```

#### `geo.add_point(lat, lng, **properties)` → `self`

Append a single Point feature, creating the dataset if nothing is loaded yet.

```python
geo = (GeoEngine()
       .add_point(28.6139, 77.2090, name="Delhi")
       .add_point(19.0760, 72.8777, name="Mumbai"))
geo.count()   # 2
```

---

### Geometry

#### `geo.distance(point_a, point_b, unit='km', method='haversine')` → `float`

Great-circle distance, rounded to 4 decimals.

- `method='haversine'` (default) — fast spherical formula using the IUGG mean Earth radius (6371.0088 km); within about 0.5 % of the true distance.
- `method='vincenty'` — Vincenty's inverse formula on the WGS-84 ellipsoid, accurate to about 0.5 mm. For nearly antipodal points, where the iteration cannot converge, QOREgeo warns and falls back to haversine.

```python
geo.distance(delhi, mumbai)                      # 1148.0965
geo.distance(delhi, mumbai, unit="m")            # 1148096.4589
geo.distance(delhi, mumbai, method="vincenty")   # 1144.5264
```

**Distance units** — one table shared by every method that takes `unit=`:

| Unit           | Argument            | Delhi → Mumbai |
|----------------|---------------------|---------------:|
| kilometres     | `'km'` (default)    | 1148.0965      |
| metres         | `'m'`               | 1148096.4589   |
| statute miles  | `'miles'` or `'mi'` | 713.3941       |
| feet           | `'ft'`              | 3766720.6655   |
| yards          | `'yd'`              | 1255573.5552   |
| nautical miles | `'nmi'`             | 619.9225       |

Units are case- and whitespace-insensitive, and spelled-out names work too (`'kilometres'`, `'meters'`, `'mile'`, `'feet'`, `'yards'`, `'nautical_miles'`, …). Conversions are exact (1 mi = 1.609344 km, 1 nmi = 1.852 km). An unknown unit raises `InvalidUnitError`.

#### `geo.bearing(point_a, point_b, as_degrees=False)` → `str | float`

Initial compass bearing from A to B: a 16-point compass name, or degrees in `[0, 360)` rounded to 2 decimals. Identical points return `'North'` / `0.0`.

```python
geo.bearing(delhi, mumbai)                    # 'South-Southwest'
geo.bearing(delhi, mumbai, as_degrees=True)   # 203.47
```

#### `geo.destination(point, distance, bearing, unit='km')` → `(lat, lng)`

The point reached by travelling `distance` from `point` on an initial `bearing` (degrees; 0 = north, 90 = east). Rounded to 6 decimals (~0.1 m).

```python
geo.destination(delhi, 10, 90)                    # (28.613861, 77.311444)
geo.destination(delhi, 100, 225, unit="miles")    # (27.585609, 76.054375)
```

#### `geo.midpoint(point_a, point_b)` → `(lat, lng)`

Great-circle midpoint, rounded to 6 decimals.

```python
geo.midpoint(delhi, mumbai)   # (23.86007, 74.963456)
```

#### `geo.buffer(center, radius, unit='km', num_points=64)` → `dict`

Create a circular buffer zone as a GeoJSON `Polygon`.

- The ring has `num_points` vertices plus the closing one, and runs **counter-clockwise** (RFC 7946 right-hand rule). `num_points` must be ≥ 3.
- `radius` must be > 0 and at most half the Earth's circumference (≈ 20,015 km).
- The result also carries `_center` and `_radius_km`, so `point_in_polygon()` and `filter_by_polygon()` test buffers **exactly** against the true circle (a great-circle distance check, not the 64-gon) — also correct near the poles and across the antimeridian.

```python
zone = geo.buffer((28.61, 77.20), radius=10)             # 10 km radius
zone = geo.buffer((28.61, 77.20), radius=10, unit="m")   # 10 metre radius
len(geo.buffer((28.61, 77.20), radius=10)["coordinates"][0])   # 65
```

#### `geo.point_in_polygon(point, polygon)` → `bool`

Test if a point is inside a polygon. Accepts a `buffer()` result (exact circle test), a GeoJSON `Polygon` (holes respected), a `MultiPolygon`, a `Feature` wrapping either, or a `FeatureCollection` (the union of its polygon features). Anything else raises `InvalidBufferError`.

Edges are straight lines in longitude/latitude — the standard GeoJSON reading (RFC 7946 §3.1.1) that Turf, Shapely and Leaflet's own drawing use. To describe a shape that **crosses the antimeridian**, either keep its longitudes continuous (e.g. `177 → 182`, as `buffer()` does) or split it into a `MultiPolygon` at ±180° (RFC 7946 §3.1.9). A ring that jumps from `177` to `-178` is read as spanning the rest of the globe.

```python
zone = geo.buffer((28.61, 77.20), radius=50)
geo.point_in_polygon((28.65, 77.22), zone)   # True
geo.point_in_polygon((19.07, 72.87), zone)   # False

park = {
    "type": "Polygon",
    "coordinates": [
        [[77.0, 28.0], [78.0, 28.0], [78.0, 29.0], [77.0, 29.0], [77.0, 28.0]],   # outer ring
        [[77.4, 28.4], [77.6, 28.4], [77.6, 28.6], [77.4, 28.6], [77.4, 28.4]],   # hole
    ],
}
geo.point_in_polygon((28.2, 77.2), park)   # True
geo.point_in_polygon((28.5, 77.5), park)   # False — inside the hole

fiji = {"type": "Polygon", "coordinates": [[[177.0, -20.0], [182.0, -20.0], [182.0, -16.0],
                                            [177.0, -16.0], [177.0, -20.0]]]}   # continuous longitudes
geo.point_in_polygon((-18.0, 179.5), fiji)    # True — crosses the antimeridian
geo.point_in_polygon((-18.0, -179.5), fiji)   # True
```

#### `geo.intersects(geometry_a, geometry_b)` → `bool`

True if two shapes share at least one point — touching counts. Accepts any GeoJSON geometry,
`Feature`, `FeatureCollection` or `buffer()` zone (circles are tested exactly), with the same
planar edges as `point_in_polygon()`.

```python
zone = geo.buffer((28.61, 77.20), radius=50)
highway = {"type": "LineString", "coordinates": [[77.2090, 28.6139], [75.7873, 26.9124]]}
geo.intersects(zone, highway)                                     # True
geo.intersects(zone, geo.buffer((19.07, 72.87), radius=100))      # False
```

#### `geo.area(polygon, unit='km2')` → `float`

Spherical area of a `Polygon`, `MultiPolygon` or `Feature`, holes subtracted (Chamberlain & Duquette's method, as used by Turf.js and OpenLayers). A `buffer()` result is measured as the exact circle it represents (a spherical cap), matching `point_in_polygon()`. Rounded to 4 decimals.

Area units: `'km2'` (default), `'m2'`, `'ha'`, `'acres'`, `'mi2'`, `'ft2'` (aliases such as `'sqkm'`, `'hectares'`, `'acre'`, `'sqmi'` and `'sqft'` also work).

```python
geo.area(geo.buffer(delhi, radius=1))   # 3.1416   (π km²)
geo.area(park)                          # 10431.2211
geo.area(park, unit="ha")               # 1043122.1092
```

#### `geo.length(line, unit='km', method='haversine')` → `float`

Length of a path: a list of `(lat, lng)` points, or a GeoJSON `LineString` / `MultiLineString` (or a `Feature` wrapping one).

```python
route = [delhi, (26.9124, 75.7873), mumbai]   # Delhi → Jaipur → Mumbai
geo.length(route)                       # 1156.0342
geo.length(route, unit="miles")         # 718.3263
geo.length(route, method="vincenty")    # 1152.4909
```

#### `geo.distance_to_line(point, line, unit='km')` → `float`

Shortest great-circle distance from a point to a path (a road, river or pipeline) — the cross-track distance where the perpendicular falls on a segment, otherwise the distance to the nearest end.

```python
geo.distance_to_line((53.2611, -0.7972), [(53.3206, -1.7297), (53.1887, 0.1334)])   # 0.3075
```

#### `geo.geohash(point, precision=9)` → `str` · `geo.geohash_decode(geohash)` → `(lat, lng)`

Encode a point as a [geohash](https://en.wikipedia.org/wiki/Geohash) (precision 1–12 characters), or decode a geohash (case-insensitive) to the centre of its cell, rounded to 6 decimals.

```python
geo.geohash(delhi)                              # 'ttnfucjbh'
geo.geohash(delhi, precision=7)                 # 'ttnfucj'
geo.geohash_decode("ttnfucj")                   # (28.614578, 77.208481)
geo.geohash((57.64911, 10.40744), precision=11) # 'u4pruydqqvj'
```

---

### Nearest Neighbours

#### `geo.nearest(point, unit='km', method='haversine')` → `dict`

Find the loaded feature nearest to a point. Features without a geometry are ignored.

Returns: `{"feature": ..., "distance": float, "index": int}`

```python
result = geo.nearest((28.61, 77.20))
print(result["distance"])                         # 0.9797
print(result["feature"]["properties"]["name"])    # Delhi
print(result["index"])                            # 0
```

#### `geo.k_nearest(point, k=5, unit='km', method='haversine')` → `GeoEngine`

The `k` nearest features (or all of them, if there are fewer), nearest first, each with a `_distance` property in `unit`.

```python
for f in geo.k_nearest(mumbai, k=3):
    print(f["properties"]["name"], f["properties"]["_distance"])
# Mumbai 0.0
# Pune 120.1525
# Hyderabad 621.4637
```

---

### Filtering

Every filter returns a **new** `GeoEngine`; the original is never modified. A filter that matches nothing returns an empty engine (count `0`) rather than raising.

#### `geo.filter(column, value, op='==')` → `GeoEngine`

Filter by a property value. `op` is one of `'=='`, `'!='`, `'>'`, `'>='`, `'<'`, `'<='`, `'in'`, `'not in'`, `'contains'`.

- String comparisons are case-insensitive; `'contains'` is a case-insensitive substring match; `'in'` / `'not in'` take a list.
- When `value` is a number, comparisons are numeric — so CSV strings like `"7400000"` match `7400000` — and integers are compared exactly, so large IDs never collide.
- Ordering operators skip features whose property is missing or not comparable.
- A column that no feature has (nor the dataset a result was filtered from) raises `ColumnNotFoundError`.
- Like every filter, it returns a new engine with copied feature and property dicts, so setting properties on the result never changes the original (geometries and nested values are shared, as in a pandas shallow copy).

```python
geo.filter("state", "maharashtra").count()                    # 2
geo.filter("population", 15_000_000, op=">").count()          # 2   (Delhi, Mumbai)
geo.filter("state", ["Karnataka", "Telangana"], op="in").count()   # 2
geo.filter("name", "bad", op="contains").count()              # 1   (Hyderabad)
```

#### `geo.where(predicate)` → `GeoEngine`

Filter with any function of a feature's properties.

```python
geo.where(lambda p: int(p["population"]) > 10_000_000 and p["state"] != "Delhi").count()   # 5
```

#### `geo.filter_by_radius(lat, lng, radius, unit='km', method='haversine')` → `GeoEngine`

Features within a radius of a point, sorted nearest-first, each with a `_distance` property in `unit`.

```python
nearby = geo.filter_by_radius(19.07, 72.87, radius=700)
for f in nearby:
    print(f["properties"]["name"], f["properties"]["_distance"])
# Mumbai 1.0488
# Pune 120.5103
# Hyderabad 622.0474
```

#### `geo.filter_by_bbox(min_lat, min_lng, max_lat, max_lng)` → `GeoEngine`

Features inside a bounding box (south, west, north, east). If `min_lng > max_lng` the box crosses the antimeridian. Accepts the output of `bounds()` directly.

```python
geo.filter_by_bbox(18.0, 72.0, 20.0, 74.0).count()   # 2   (Mumbai, Pune)
geo.filter_by_bbox(**other.bounds())                  # everything inside another dataset's extent
```

#### `geo.filter_by_polygon(polygon)` → `GeoEngine`

Features inside a polygon — anything `point_in_polygon()` accepts, e.g. every customer inside a delivery zone.

```python
zone = geo.buffer((19.07, 72.87), radius=200)
geo.filter_by_polygon(zone).count()   # 2   (Mumbai, Pune)
```

#### `geo.filter_intersects(geometry)` → `GeoEngine`

Features whose **whole geometry** intersects `geometry` — e.g. the districts a pipeline crosses,
or parcels touching a flood zone. (`filter_by_polygon()` tests each feature's single location.)

```python
corridor = {"type": "LineString", "coordinates": [[72.88, 19.08], [73.86, 18.52], [78.49, 17.39]]}
crossed = districts.filter_intersects(corridor)
```

#### `geo.filter_by_line(line, distance, unit='km')` → `GeoEngine`

A **line buffer**: features within `distance` of a path (list of `(lat, lng)` points, `LineString`, `MultiLineString` or a `Feature`), sorted nearest-first with a `_distance` property.

```python
nh48 = [delhi, (26.9124, 75.7873), (23.0225, 72.5714), mumbai]   # Delhi → Jaipur → Ahmedabad → Mumbai
for f in geo.filter_by_line(nh48, distance=150):
    print(f["properties"]["name"], f["properties"]["_distance"])
# Delhi 0.0
# Mumbai 0.0
# Pune 120.1525
```

#### `geo.sort_by(column, descending=False)` → `GeoEngine` · `geo.head(n=5)` → `GeoEngine`

`sort_by` sorts numbers numerically (numeric strings included) and everything else case-insensitively; features missing the property go last. `head` keeps the first `n` features.

```python
top3 = geo.sort_by("population", descending=True).head(3)
[f["properties"]["name"] for f in top3]   # ['Delhi', 'Mumbai', 'Kolkata']
```

---

### Data Access

| Method | Returns | Description |
|--------|---------|-------------|
| `geo.count()` / `len(geo)` | `int` | Number of features |
| `for feature in geo:` | `dict` | Iterate over the GeoJSON Feature dicts |
| `geo.get_features()` | `list` | Raw GeoJSON feature list (`[]` for an empty result) |
| `geo.get_geojson()` | `dict` | Full FeatureCollection |
| `geo.bounds()` | `dict` | `{min_lat, max_lat, min_lng, max_lng}` — plain min / max |
| `geo.bbox()` | `list` | `[west, south, east, north]` (RFC 7946 order), antimeridian aware |
| `geo.centroid()` | `(lat, lng)` | Spherical mean of all feature locations, antimeridian safe |
| `repr(geo)` | `str` | e.g. `<GeoEngine [7 features]>` |

```python
geo.bounds()     # {'min_lat': 12.9716, 'max_lat': 28.6139, 'min_lng': 72.8777, 'max_lng': 88.3639}
geo.bbox()       # [72.8777, 12.9716, 88.3639, 28.6139]
geo.centroid()   # (18.94374, 78.356521)
```

**`bounds()` vs `bbox()`** — `bounds()` is a plain min/max whose keys match `filter_by_bbox()`. `bbox()` follows GeoJSON order and, for data spanning the antimeridian, returns the tight box with `west > east`:

```python
fiji = GeoEngine().add_point(-18.14, 178.44).add_point(-16.80, -179.95)
fiji.bounds()     # {'min_lat': -18.14, 'max_lat': -16.8, 'min_lng': -179.95, 'max_lng': 178.44}
fiji.bbox()       # [178.44, -18.14, -179.95, -16.8]
fiji.centroid()   # (-17.471619, 179.247963)
```

---

### Visualisation

#### `geo.map(output_path='map.html', title='QOREgeo Map', zoom=None, center=None, tiles='light', overlays=None, attribution=None)` → `self`

Interactive Leaflet.js map saved as a standalone HTML file. Points, lines and polygons are all
drawn, and clicking a feature shows its properties.

```python
geo.map("output.html")
geo.map("output.html", title="My Cities", zoom=6, tiles="osm")
geo.map("output.html", center=(19.07, 72.87), zoom=8, tiles="topo")

zone = geo.buffer((19.07, 72.87), radius=200)
geo.filter_by_polygon(zone).map("zone.html", title="Within 200 km of Mumbai", overlays=[zone])
```

- **`zoom` / `center`** — with `center`, `zoom` is the initial zoom (default 10). Without `center`, the map fits all features and `zoom` caps how far it zooms in (default 15). `zoom` must be an integer from 0 to 22.
- **`tiles`** — a [basemap](#basemaps) name, or your own `https://…/{z}/{x}/{y}.png` URL template with `attribution=` (HTML) crediting it.
- **`overlays`** — extra GeoJSON shapes (e.g. `buffer()` zones) drawn as dashed outlines and included in the auto-fit.
- `map()` refuses to write over the data file the engine was loaded from.

#### `geo.heatmap(output_path='heatmap.html', title='QOREgeo Heatmap', intensity_col=None, zoom=None, center=None, tiles='light', radius=25, blur=18, attribution=None)` → `self`

Density heatmap with optional intensity weighting. `intensity_col` must be a numeric property; weights are normalised to the largest value, and rows with a missing, negative or non-numeric value add no heat. An unknown column raises `ColumnNotFoundError`. `radius` and `blur` are in pixels; `zoom`, `center`, `tiles` and `attribution` work as in `map()`.

```python
geo.heatmap("heat.html")
geo.heatmap("heat.html", intensity_col="population", radius=30, blur=20)
```

#### Viewing maps: `geo.show()` · `qoregeo serve map.html`

OpenStreetMap's tile servers require an HTTP `Referer`, which browsers never send for pages
opened straight from disk (`file://`). So QOREgeo serves maps from your own machine:
`geo.show()` serves `map_html()` on `http://127.0.0.1`, opens it in the browser and returns
the URL. In a script it keeps serving until you press Ctrl+C, so make it the last line; in a
notebook or the REPL it serves in the background and returns at once.

```python
url = geo.show(block=False)   # serve in the background and carry on (stops when Python exits)
geo.show(heatmap=True, intensity_col="population", tiles="dark")   # in a script: until Ctrl+C
```

```bash
qoregeo serve output.html                    # any saved map, re-read on every reload; Ctrl+C stops
```

Only that one page is served, only to `127.0.0.1`. A map using an OpenStreetMap-hosted basemap
that *is* opened from disk doesn't request tiles at all (that would break the tile policy);
it shows the data on a plain background with a note explaining how to view it. Only
`tiles="satellite"`, `"topo"`, `"osm-de"` and `"none"` work when the file is opened from disk.

**Jupyter / notebooks:** a `GeoEngine` displays itself as an interactive map (up to 20,000
features). **Web apps / reports:** `geo.map_html()` and `geo.heatmap_html()` return the page as
a string.

#### Basemaps

Every preset is open and needs no API key:

| `tiles=` | Basemap | Notes |
|---|---|---|
| `"light"` *(default)* | OpenStreetMap, muted colours | data stands out; needs http (see above) |
| `"osm"` | OpenStreetMap standard | needs http |
| `"dark"` | OpenStreetMap, inverted | needs http |
| `"humanitarian"` | Humanitarian OSM Team style (OSM France) | needs http |
| `"cyclosm"` | CyclOSM cycling map (OSM France) | needs http |
| `"osm-fr"` | OpenStreetMap France style | needs http |
| `"osm-de"` | OpenStreetMap Germany style (FOSSGIS) | non-commercial / small sites only; works from disk |
| `"topo"` | OpenTopoMap topographic | up to zoom 17; works from disk |
| `"satellite"` | NASA Blue Marble imagery (public domain) | overview only (zoom ≤ 10); works from disk |
| `"none"` | no basemap | nothing is downloaded; works from disk |

`qoregeo tiles` or `qoregeo.tile_presets()` list them. Attribution required by each provider is
always shown. Please respect the [OpenStreetMap tile usage policy](https://operations.osmfoundation.org/policies/tiles/)
— for heavy use, run your own tile server or use a commercial provider via a custom `tiles=` URL
(keys in a URL are written into the HTML file).

> **Why not CARTO?** CARTO basemaps require an API key since 2026 (tiles without one carry an
> "API key required" watermark), so QOREgeo 1.1 switched to open, keyless maps. Keyed providers
> (CARTO, Stadia/Stamen, Thunderforest, MapTiler, Mapbox, Esri) still work as custom `tiles=` URLs.

#### `geo.image(output_path='map.png', title=None, width=1200, height=800, overlays=None, background='#ffffff', labels=None, grid=True, scale_bar=True)` → `self`

A static **PNG or SVG** map (by file extension) rendered in pure Python — no browser, no network,
no matplotlib. Points, lines, polygons (with holes) and overlays use the same colours as the
HTML maps, over a lat/lng grid with a scale bar; `labels="name"` writes a property next to each
feature (overlapping labels are skipped). Data across the antimeridian stays together.

```python
geo.image("cities.png", title="India Cities", labels="name")
geo.filter_by_polygon(zone).image("zone.svg", overlays=[zone])
png_bytes = geo.to_png(width=800, height=600)    # bytes — for e-mail, Slack, PDFs, AI vision models
svg_text = geo.to_svg()                          # str
```

---

### Geocoding

Addresses and place names ↔ coordinates with OpenStreetMap's
[Nominatim](https://nominatim.org) or [Photon](https://photon.komoot.io), standard library only.

```python
from qoregeo import Geocoder

g = Geocoder(email="you@example.com")            # identifies you to the public service
hit = g.geocode("Gateway of India, Mumbai")      # best match or None
hit["lat"], hit["lng"], hit["name"], hit["attribution"]

g.search("Main Street", limit=5, country_codes="us")
g.reverse(18.9220, 72.8347)

Geocoder(provider="photon")                       # komoot Photon
Geocoder(url="http://localhost:8080")             # your own Nominatim server — use this for bulk work
```

The public servers are run by volunteers: QOREgeo enforces **at most 1 request per second** per
server — across every process on your machine, through a small shared SQLite file in your user
cache folder that also remembers answers for 30 days — sends an identifying `User-Agent` and maps
errors (403 blocked, 429 too many requests, …) to `GeocodingError` with a hint. Move the cache
with `Geocoder(cache_dir=…)` / `QOREGEO_CACHE_DIR=<folder>`, or turn it off with
`cache_dir=None` / `QOREGEO_CACHE_DIR=off`; self-hosted servers (`url=…`) never use it. Their policies forbid bulk jobs, autocomplete
and systematic queries — for more than a handful of addresses, run
[Nominatim](https://github.com/mediagis/nominatim-docker) or Photon yourself. Results carry the
required "© OpenStreetMap contributors, ODbL" attribution; keep it with anything you publish.

---

### Saving

#### `geo.save(path, encoding='utf-8', csv_safe=False)` → `self`

Save to `.geojson` / `.json` or `.csv` / `.tsv`. Missing parent folders are created, an empty result still writes a valid file, and the write is atomic — a failed save never truncates an existing file. Symlinks are followed, and a read-only target raises `PermissionError` as with a plain `open()`.

```python
geo.save("output.geojson")
geo.save("output.csv")
geo.save("for_excel.csv", csv_safe=True)   # '=SUM(…)' is written as "'=SUM(…)"
```

With `csv_safe=True`, text cells starting with `=`, `+`, `-`, `@`, tab or carriage return get a
leading `'` so spreadsheet apps show them as text instead of running them as formulas
([CSV injection](https://owasp.org/www-community/attacks/CSV_Injection)); numbers such as `-5`
are left alone. Use it for files that will be opened in Excel or Google Sheets.

CSV details:

- The first columns are `latitude` and `longitude`, followed by every property in first-seen order (including `_distance` after a radius or line filter).
- Non-Point features get their representative point in `latitude` / `longitude`, plus a `geometry` column holding the full GeoJSON geometry, which `load()` reads back — so polygons and lines survive a CSV round trip. Null geometries leave these cells empty.
- Nested property values (lists, dicts) are written as JSON and `None` as an empty cell.
- Properties named `latitude` or `longitude` (or `geometry`, when that column is written) would clash with those columns, so they are skipped with a warning.
- GeoJSON output replaces `NaN` / `Infinity` (not valid JSON) with `null`; dates become ISO strings, `Decimal`s numbers and sets lists.

---

### Exceptions

All exceptions live in `qoregeo` (and `qoregeo.exceptions`), derive from `QOREgeoError`, **and** inherit the matching built-in — so `except ValueError:` or `except FileNotFoundError:` keep working. Each one exposes the offending values as attributes.

| Exception | Also a | Raised when | Attributes |
|-----------|--------|-------------|------------|
| `QOREgeoError` | `Exception` | Base class of everything below | |
| `NoDataError` | `RuntimeError` | Nothing is loaded, or the dataset is empty (e.g. `nearest()` after a filter matched nothing) | `.method_name`, `.empty` |
| `InvalidCoordinateError` | `ValueError` | A lat/lng is missing, non-numeric, NaN or out of range | `.lat`, `.lng`, `.context` |
| `InvalidUnitError` | `ValueError` | Unknown distance or area unit | `.unit`, `.valid` |
| `InvalidRadiusError` | `ValueError` | A `buffer()` / `filter_by_radius()` radius or `filter_by_line()` distance is zero, negative, non-finite or not a number | `.radius` |
| `InvalidBufferError` | `ValueError` | A polygon argument is missing, malformed or not a polygon | `.reason` |
| `InvalidArgumentError` | `ValueError` | Any other bad argument (`op`, `k`, `zoom`, `tiles`, `precision`, `method`, `encoding`, a buffer radius beyond half the Earth's circumference, heatmap `radius`/`blur`, …) | `.name`, `.value`, `.expected` |
| `ColumnNotFoundError` | `LookupError` | Lat/lng columns can't be found, or a filter / heatmap column doesn't exist | `.column`, `.available`, `.filepath` |
| `GeoFileNotFoundError` | `FileNotFoundError` | The data file doesn't exist | `.path` |
| `UnsupportedFormatError` | `ValueError` | Unsupported file extension | `.ext` |
| `InvalidGeoJSONError` | `UnsupportedFormatError` | Invalid JSON, or JSON that isn't GeoJSON | `.source`, `.reason` |
| `EmptyDatasetError` | `ValueError` | A file or object contains no usable features | `.path` |
| `FileReadError` | `ValueError` | A file exists but can't be decoded or parsed (wrong `encoding`, broken CSV) | `.path`, `.reason` |
| `GeocodingError` | `RuntimeError` | A geocoding request failed (blocked, rate-limited, unreachable, bad response) | `.reason`, `.status`, `.hint` |

`GeoFileNotFoundError` was called `FileNotFoundError` in 1.0. `qoregeo.FileNotFoundError` still works but is deprecated (it emits a `DeprecationWarning`), and it is not exported by `from qoregeo import *`, so it never shadows the built-in. All QOREgeo exceptions can be pickled, so errors raised in `multiprocessing` / `ProcessPoolExecutor` workers arrive intact.

```python
import qoregeo

try:
    geo.distance(delhi, mumbai, unit="furlongs")
except ValueError as e:                          # also catches qoregeo.InvalidUnitError
    print(e.valid)   # ['km', 'm', 'miles', 'mi', 'ft', 'yd', 'nmi']
```

---

## Security

- **Generated maps escape all data.** Titles are HTML-escaped, popups are built with the DOM's `textContent` (property keys and values are never interpreted as HTML), and data is embedded with `JSON.parse("…")` with `<`, `>`, `&`, U+2028 and U+2029 escaped — a malicious value can't inject script, and a property named `__proto__` stays plain data.
- **Leaflet is loaded with Subresource Integrity.** Leaflet 1.9.4 and leaflet.heat 0.2.0 are pinned and loaded with SHA-256 `integrity` hashes from unpkg, with a jsDelivr fallback, so a tampered CDN file won't run.
- **No surprise network traffic.** The library only goes online when you call the geocoder; the browser fetches Leaflet and tiles when a map is opened. `tiles="none"` maps fetch no tiles at all.
- **The MCP server is sandboxed** to one folder, writes only when allowed, never overwrites without being asked, and geocodes only after an explicit opt-in (see [AI Assistants](#ai-assistants-mcp-skills-claude-codex)).
- **Spreadsheet-safe exports** with `save(csv_safe=True)` / `qoregeo export`.
- `show()` / `qoregeo serve` bind to `127.0.0.1` and serve a single page.

---

## Error Messages

QOREgeo errors are designed to teach, not confuse:

```
❌  QOREgeo — Column Not Found
────────────────────────────────────────────
File: 'stores.csv'
Could not find a 'latitude' column.

Columns in your file:
    'store_id', 'y_coord', 'x_coord'

Fix it:
    geo.load('stores.csv', lat_col='<your lat column>', lng_col='<your lng column>')
```

---

## Real-World Examples

### Delivery Zone Analysis

```python
from qoregeo import GeoEngine

geo = GeoEngine().load("customers.csv")

branches = {
    "Delhi HQ":   (28.6139, 77.2090),
    "Mumbai Hub": (19.0760, 72.8777),
}

for name, coords in branches.items():
    nearby = geo.filter_by_radius(*coords, radius=50)
    print(f"{name}: {nearby.count()} customers within 50 km")
```

### Restaurant Geofencing

```python
from qoregeo import GeoEngine

geo = GeoEngine()
restaurant = (28.6315, 77.2167)   # Connaught Place, Delhi
zone = geo.buffer(restaurant, radius=5)   # 5 km delivery zone

for order in incoming_orders:
    inside = geo.point_in_polygon(order["location"], zone)
    status = "DELIVER" if inside else "TOO FAR"
    print(status)
```

### Stores Along a Highway

```python
from qoregeo import GeoEngine

geo = GeoEngine().load("stores.geojson")
highway = {"type": "LineString", "coordinates": [[77.2090, 28.6139], [75.7873, 26.9124]]}

corridor = geo.filter_by_line(highway, distance=2)   # within 2 km of the road
corridor.map("corridor.html", title="Stores on the highway", overlays=[highway])
```

### Ambulance Dispatch

```python
from qoregeo import GeoEngine

geo = GeoEngine().load("hospitals.csv")
patient = (28.6139, 77.2090)

result = geo.nearest(patient)
print(f"Nearest hospital: {result['feature']['properties']['name']}")
print(f"Distance: {result['distance']} km")

# Show nearby hospitals on a map
geo.filter_by_radius(*patient, 5).map("nearby_hospitals.html")
```

---

## Roadmap

| Version | Status | Focus |
|---------|--------|-------|
| v1.0.x | ✅ Released | Core spatial engine — loading, distance, bearing, buffers, geofencing, nearest, filters, Leaflet maps, 120+ tests |
| v1.1   | ✅ Released | Polygon / line / null GeoJSON, line buffers, geohash, Vincenty, area / length, k-nearest, filter operators, `intersects()`, open keyless basemaps (OpenStreetMap default), `show()` / `qoregeo serve`, PNG / SVG export, geocoding, CLI, MCP server, Agent Skill, Claude / OpenAI tool schemas, map XSS fix + SRI |
| v1.2+  | 📋 Planned | Interactive maps inside AI chats (MCP Apps), offline reverse geocoding (country / region), choropleth maps, spatial index for large files |
| v2.0   | 📋 Planned | QORE OS integration, quantum nearest-neighbour |
| v3.0   | 🔮 Future | Quantum routing, GeoAI, WebAssembly |

See the [CHANGELOG](https://github.com/bosekarmegam/qoregeo/blob/main/CHANGELOG.md) for details.

---

## Contributing

1. Fork the repo at [github.com/bosekarmegam/qoregeo](https://github.com/bosekarmegam/qoregeo)
2. Create a branch: `git checkout -b feature/your-feature`
3. Set up a dev environment (needs pip ≥ 25.1 for `--group`):
   ```bash
   pip install -e . --group dev
   ```
4. Add tests for new functionality
5. Check your work — everything must pass:
   ```bash
   pytest
   ruff check .
   mypy
   ```
6. Open a pull request

---

## License

MIT © 2025 [ArcGX TechLabs Private Limited](https://arcgx.in)  
Built by Suneel Bose K

---

## Links

- 📦 PyPI: [pypi.org/project/qoregeo](https://pypi.org/project/qoregeo)
- 🐙 GitHub: [github.com/bosekarmegam/qoregeo](https://github.com/bosekarmegam/qoregeo)
- 🐛 Issues: [github.com/bosekarmegam/qoregeo/issues](https://github.com/bosekarmegam/qoregeo/issues)
- ✉️ Contact: suneelbosekarmegam@gmail.com
