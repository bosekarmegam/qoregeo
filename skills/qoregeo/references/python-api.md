# qoregeo Python API reference

Every method below is on `GeoEngine` (`from qoregeo import GeoEngine`). Methods that
filter return a **new** engine (the original is unchanged); `load`, `load_data`,
`add_point`, `save`, `map`, `heatmap` and `image` return `self` so calls chain.
`GeoEngine(verbose=False)` silences the one-line "saved" / "serving" messages.

Points (`PointLike`) are `(lat, lng)` tuples/lists (`(lat, lng, alt)` too),
`{"lat": …, "lng": …}` dicts, GeoJSON Points or Point Features. Distance units: `km`
(default), `m`, `miles`/`mi`, `ft`, `yd`, `nmi`. Area units: `km2` (default), `m2`, `ha`,
`acres`, `mi2`, `ft2`.

## Loading and saving

| Method | Notes |
|---|---|
| `load(path, lat_col=None, lng_col=None, encoding="utf-8-sig", delimiter=None, infer_types=False)` | CSV/TSV/TXT (lat/lng columns and delimiter auto-detected) or GeoJSON. CSV values stay strings unless `infer_types=True` (`"42"` → `42`). |
| `load_data(features)` | FeatureCollection dict, list of Features/geometries, a Feature or a geometry. |
| `add_point(lat, lng, **properties)` | Append one point. |
| `save(path, encoding="utf-8", csv_safe=False)` | `.geojson`/`.json` or `.csv`/`.tsv`. `csv_safe=True` prefixes `= + - @` cells with `'` for spreadsheets. Atomic write. |
| `get_features()`, `get_geojson()`, `count()`, `len(geo)`, `for f in geo` | Access the data. |

## Measurements (no data needed)

| Method | Returns |
|---|---|
| `distance(a, b, unit="km", method="haversine")` | float; `method="vincenty"` = WGS84 ellipsoid (mm accuracy) |
| `bearing(a, b, as_degrees=False)` | `"South-Southwest"` or degrees 0-360 |
| `destination(point, distance, bearing, unit="km")` | `(lat, lng)` |
| `midpoint(a, b)` | `(lat, lng)` |
| `buffer(center, radius, unit="km", num_points=64)` | GeoJSON Polygon (exact circle for containment tests) |
| `point_in_polygon(point, polygon)` | bool; holes respected; MultiPolygon/Feature/buffer accepted |
| `intersects(a, b)` | bool; any GeoJSON (points, lines, polygons, collections) |
| `area(polygon, unit="km2")` | float; spherical (mean Earth radius, Chamberlain & Duquette as in Turf.js), holes subtracted; a `buffer()` zone is measured as its exact spherical cap |
| `length(line, unit="km", method="haversine")` | float; sum of great-circle segments (`method="vincenty"` = WGS84 ellipsoid) |
| `distance_to_line(point, line, unit="km")` | shortest distance to a path |
| `geohash(point, precision=9)`, `geohash_decode(geohash)` | |

## Querying a dataset

| Method | Notes |
|---|---|
| `filter(column, value, op="==")` | ops: `== != > >= < <= in "not in" contains`; a number `value` compares numerically, also against CSV strings |
| `where(predicate)` | `geo.where(lambda p: p["type"] == "cafe" and float(p["rating"]) > 4)`: `p` is the properties dict, and CSV values are strings unless loaded with `infer_types=True` |
| `filter_by_radius(lat, lng, radius, unit="km", method="haversine")` | nearest first, `_distance` added |
| `filter_by_bbox(min_lat, min_lng, max_lat, max_lng)` | handles boxes across 180° |
| `filter_by_polygon(polygon)` | by each feature's location |
| `filter_intersects(geometry)` | by each feature's whole geometry (lines/polygons) |
| `filter_by_line(line, distance, unit="km")` | corridor along a route |
| `nearest(point, unit="km", method="haversine")` | `{"feature": …, "distance": float, "index": int}` |
| `k_nearest(point, k=5, unit="km", method="haversine")` | engine, nearest first, `_distance` added |
| `sort_by(column, descending=False)`, `head(n=5)` | |
| `bounds()`, `bbox()`, `centroid()` | `bbox()` = `[west, south, east, north]` |

## Maps and images

| Method | Notes |
|---|---|
| `map(output_path="map.html", title="QOREgeo Map", zoom=None, center=None, tiles="light", overlays=None, attribution=None)` | Leaflet map; popups list properties (HTML-escaped). |
| `heatmap(output_path="heatmap.html", title="QOREgeo Heatmap", intensity_col=None, zoom=None, center=None, tiles="light", radius=25, blur=18, attribution=None)` | |
| `map_html(...)`, `heatmap_html(...)` | same pages as strings (web apps, reports) |
| `show(heatmap=False, port=0, block=None, open_browser=True, **options)` | serves the page on 127.0.0.1, opens the browser and returns the URL. `block=None`: in a script it serves until Ctrl+C (so call it last), in notebooks / the REPL it returns at once; `block=False` serves in the background until Python exits |
| `image(output_path="map.png", title=None, width=1200, height=800, overlays=None, background="#ffffff", labels=None, grid=True, scale_bar=True)` | static `.png`/`.svg`, offline; `labels` = property to write next to features |
| `to_png(...)`, `to_svg(...)` | bytes / str |
| Notebook | a `GeoEngine` displays as an interactive map in Jupyter |

`tiles`: `light` (default, OpenStreetMap muted), `osm`, `dark`, `humanitarian`, `cyclosm`,
`osm-fr`, `osm-de` (non-commercial), `topo` (OpenTopoMap, z ≤ 17), `satellite` (NASA Blue
Marble, overview z ≤ 10), `none`, or a custom `https://…/{z}/{x}/{y}.png` template plus
`attribution=`. No API keys. The OpenStreetMap and OSM France styles need the page served
over http (`show()`, `qoregeo serve file.html`); opened from disk they show a notice instead.

Only `"satellite"`, `"topo"`, `"osm-de"` and `"none"` work when the file is opened from disk.

## Geocoding

```python
from qoregeo.geocode import Geocoder

g = Geocoder()                                   # OpenStreetMap Nominatim, 1 request/s, cached on disk
g.geocode("Gateway of India, Mumbai")            # best match dict or None
g.search("Main Street", limit=5, country_codes="us")
g.reverse(18.922, 72.8347)
Geocoder(provider="photon")                      # komoot Photon
Geocoder(url="http://localhost:8080")            # your own Nominatim (bulk jobs)
Geocoder(cache_dir=None)                         # no shared disk cache (or QOREGEO_CACHE_DIR=off)
```

Result dicts: `lat`, `lng`, `name`, `category`, `type`, `importance`, `bbox` (`[w, s, e, n]`),
`address`, `provider`, `attribution`. Errors raise `qoregeo.GeocodingError` with a hint.

## Errors

All errors subclass `qoregeo.QOREgeoError` and a matching built-in (`ValueError`,
`LookupError`, `RuntimeError`, `FileNotFoundError`) and explain the fix:
`ColumnNotFoundError` (a `LookupError`, not a `KeyError`) lists the columns the file does
have, out-of-range coordinates ask whether lat/lng are swapped, etc.

## AI tool registry

```python
from qoregeo.tools import ToolContext, call_tool, tool_definitions

tool_definitions("anthropic")   # or "mcp", "openai", "openai-responses"
call_tool("nearest", {"path": "stores.csv", "point": {"lat": 28.6, "lng": 77.2}, "k": 3},
          ToolContext(root="data", allow_write=False))
```
