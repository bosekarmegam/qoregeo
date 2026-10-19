---
name: qoregeo
description: Geospatial analysis and maps with the zero-dependency qoregeo Python library, its CLI and MCP server. Use for distances and bearings between coordinates, nearest-location and radius searches, geofencing (point in polygon, buffers, intersections), filtering CSV/GeoJSON by place or attribute, converting CSV to GeoJSON, interactive OpenStreetMap HTML maps and heatmaps, static PNG/SVG map images, and geocoding addresses with OpenStreetMap Nominatim. Triggers include latitude/longitude columns, "map this CSV", "stores within 5 km", "delivery zone", "heatmap", "geocode these addresses", GeoJSON files.
license: MIT
compatibility: Python 3.10+ with `pip install qoregeo` (or `uvx qoregeo`). Maps and geocoding need internet; everything else works offline.
metadata:
  author: Suneel Bose
  homepage: https://github.com/bosekarmegam/qoregeo
---

# qoregeo: geospatial work without GDAL

`qoregeo` is a pure-Python (stdlib only) GIS toolkit. Pick the first surface available:

1. **MCP tools** named `distance`, `nearest`, `query_features`, `preview_map`, `make_map`, … (the `qoregeo` MCP server). Call them directly.
2. **CLI**: `qoregeo <command>` (or `uvx qoregeo <command>` / `python -m qoregeo <command>` if not installed).
3. **Python**: `from qoregeo import GeoEngine`.

**No pip or no internet** (e.g. Claude's code-execution sandbox): if this skill folder has a
`scripts/qoregeo/` package (bundles made with `qoregeo skill zip` do), use it directly:

```python
import sys; sys.path.insert(0, "<this skill's folder>/scripts")
from qoregeo import GeoEngine          # pure standard library, no install needed
```

The CLI works too: `cd <skill folder>/scripts && python -m qoregeo …`.

## Golden rules

- **Coordinate order.** Python API and CLI take `(lat, lng)` / `LAT,LNG`. GeoJSON stores `[lng, lat]`. MCP tools take `{"lat": …, "lng": …}` objects. Double-check before every call: India is lat ≈ 8 to 37, lng ≈ 68 to 97.
- **Look before you report.** After filtering, render `preview_map` (MCP) or `qoregeo image data.csv -o check.png` and inspect the image: points in the sea or on the wrong continent mean swapped or bad coordinates.
- **Viewing HTML maps.** The default basemap is OpenStreetMap, whose servers only serve web pages: open maps with `qoregeo serve map.html` or `geo.show()`, never by double-clicking. Only `tiles="satellite"`, `"topo"`, `"osm-de"` (non-commercial use) and `"none"` (no network at all) work when the file is opened from disk.
- **`show()` and `serve` block.** In a script, `geo.show()` and `qoregeo serve` keep serving until Ctrl+C, so put `show()` last (or pass `block=False` in a long-running process). With no browser to look at (a sandbox, CI), render `image()` / `preview_map` instead.
- **Geocoding follows the [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/).** Tell the user when you use the public OpenStreetMap service and what it allows: at most 1 request per second, no bulk jobs, no autocomplete, results cached, "© OpenStreetMap contributors" attribution, and no personal data. Geocode a handful of addresses; for more, point `--url` (CLI) / `Geocoder(url=…)` / `--geocoder-url` (MCP) at a self-hosted Nominatim or Photon server. The MCP geocoding tools exist only when whoever started the server opted in.
- **Exports are spreadsheet-safe.** `qoregeo export` escapes cells starting with `= + - @` so Excel won't run them as formulas (`--raw-csv` disables this).
- **Never overwrite the input file.** Write results to a new file.

## CLI quick reference

```bash
qoregeo info stores.csv                                   # rows, columns, bbox, centre
qoregeo distance 28.6139,77.2090 19.0760,72.8777 --unit km
qoregeo export stores.csv -o near.geojson --near 28.61,77.21,5 --sort revenue --desc --limit 20
qoregeo export stores.csv -o big.csv --where "revenue>=100000" --where "city in [\"Delhi\",\"Pune\"]"
qoregeo export stores.csv -o zone.csv --within delivery_zone.geojson
qoregeo map stores.csv -o stores.html --title "Stores" --serve          # interactive map; serves until Ctrl+C
qoregeo map stores.csv -o heat.html --heatmap --intensity revenue
qoregeo image stores.csv -o stores.png --near 28.61,77.21,25 --labels name   # static image, offline
qoregeo geocode "Connaught Place, New Delhi" --country in
qoregeo reverse 28.6315,77.2167
qoregeo tiles                                                            # basemap names
qoregeo call nearest '{"path": "stores.csv", "point": {"lat": 28.61, "lng": 77.21}, "k": 3}'
```

Filters (`--near LAT,LNG,RADIUS [--unit]`, `--bbox W,S,E,N`, `--within FILE`, `--where EXPR`, `--sort/--desc/--limit`) work on `map`, `image` and `export`. `--where` operators: `== != > >= < <= in "not in" contains`.

## Python quick reference

```python
from qoregeo import GeoEngine

geo = GeoEngine().load("stores.csv")            # lat/lng columns auto-detected; GeoJSON too
geo.distance((28.61, 77.21), (19.08, 72.88))    # km (unit="miles", method="vincenty")
zone = geo.buffer((28.61, 77.21), 5)            # 5 km circle polygon
near = geo.filter_by_polygon(zone).sort_by("revenue", descending=True).head(20)
near.save("near.geojson")                       # or .csv (csv_safe=True for spreadsheets)
near.image("near.png", labels="name")           # static PNG/SVG with grid + scale bar, no network
near.map("near.html", overlays=[zone])          # tiles="light" | osm | dark | humanitarian | topo | satellite | none
geo.k_nearest((28.61, 77.21), k=3)              # adds a _distance property
geo.filter("revenue", 100000, op=">=")          # == != > >= < <= in not in contains
geo.intersects(zone, geo.buffer((28.65, 77.25), 3))   # any GeoJSON → True if they share a point
near.show()                                     # serve + open the browser; blocks until Ctrl+C in a script
```

More: [references/python-api.md](references/python-api.md) (every method and parameter) and [references/recipes.md](references/recipes.md) (store locator, delivery zones, geocoding a CSV, route corridors, antimeridian data).

## Interpreting results

- Distances are great-circle (haversine) unless `method="vincenty"`; both are accurate for logistics, neither follows roads.
- Areas are spherical (mean Earth radius, Chamberlain & Duquette as in Turf.js), not ellipsoidal: within about 0.5 % of WGS84 values up to ~55° latitude and up to ~0.9 % near the poles. A `buffer()` zone is measured as its exact spherical cap.
- `bbox` returns `[west, south, east, north]`; `west > east` means the data crosses the 180° meridian. That is correct, not a bug.
- Results from geocoding carry an `attribution` (OpenStreetMap, ODbL) that must stay with published data.
