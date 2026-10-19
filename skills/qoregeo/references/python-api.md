# qoregeo Python API reference

`from qoregeo import GeoEngine` — every method below is on `GeoEngine`. Methods that
filter return a **new** engine (the original is unchanged); `load`, `load_data`,
`add_point`, `save`, `map`, `heatmap` and `image` return `self` so calls chain.

Points (`PointLike`) are `(lat, lng)` tuples/lists, `{"lat": …, "lng": …}` dicts,
GeoJSON Points or Point Features. Distance units: `km` (default), `m`, `miles`/`mi`,
`ft`, `yd`, `nmi`. Area units: `km2` (default), `m2`, `ha`, `acres`, `mi2`, `ft2`.

## Loading and saving

| Method | Notes |
|---|---|
| `load(path, lat_col=None, lng_col=None, encoding="utf-8-sig", delimiter=None, infer_types=False)` | CSV/TSV/TXT (lat/lng columns and delimiter auto-detected) or GeoJSON. `infer_types=True` turns `"42"` into `42`. |
| `load_data(features)` | FeatureCollection dict, list of Features/geometries, a Feature or a geometry. |
| `add_point(lat, lng, **properties)` | Append one point. |
| `save(path, encoding="utf-8", csv_safe=False)` | `.geojson`/`.json` or `.csv`/`.tsv`. `csv_safe=True` prefixes `= + - @` cells with `'` for spreadsheets. Atomic write. |
| `get_features()`, `get_geojson()`, `count()`, `len(geo)`, `for f in geo` | Access the data. |

## Measurements (no data needed)

| Method | Returns |
|---|---|
| `distance(a, b, unit="km", method="haversine")` | float; `method="vincenty"` = WGS84 ellipsoid (mm accuracy) |
| `bearing(a, b, as_degrees=False)` | `"South-Southwest"` or degrees 0–360 |
| `destination(point, distance, bearing, unit="km")` | `(lat, lng)` |
| `midpoint(a, b)` | `(lat, lng)` |
| `buffer(center, radius, unit="km", num_points=64)` | GeoJSON Polygon (exact circle for containment tests) |
| `point_in_polygon(point, polygon)` | bool; holes respected; MultiPolygon/Feature/buffer accepted |
| `intersects(a, b)` | bool; any GeoJSON (points, lines, polygons, collections) |
| `area(polygon, unit="km2")`, `length(line, unit="km")` | geodesic |
| `distance_to_line(point, line, unit="km")` | shortest distance to a path |
| `geohash(point, precision=9)`, `geohash_decode(hash)` | |

## Querying a dataset

| Method | Notes |
|---|---|
| `filter(column, value, op="==")` | ops: `== != > >= < <= in "not in" contains` |
| `where(predicate)` | `geo.where(lambda p: p["type"] == "cafe" and p["rating"] > 4)` |
| `filter_by_radius(lat, lng, radius, unit="km")` | |
| `filter_by_bbox(min_lat, min_lng, max_lat, max_lng)` | handles boxes across 180° |
| `filter_by_polygon(polygon)` | by each feature's location |
| `filter_intersects(geometry)` | by each feature's whole geometry (lines/polygons) |
| `filter_by_line(line, distance, unit="km")` | corridor along a route |
| `nearest(point)` | the closest Feature, with `_distance` |
| `k_nearest(point, k=5)` | engine, nearest first, `_distance` added |
| `sort_by(column, descending=False)`, `head(n=5)` | |
| `bounds()`, `bbox()`, `centroid()` | `bbox()` = `[west, south, east, north]` |

## Maps and images

| Method | Notes |
|---|---|
| `map(path="map.html", title, zoom, center, tiles="light", overlays=None, attribution=None)` | Leaflet map; popups list properties (HTML-escaped). |
| `heatmap(path="heatmap.html", title, intensity_col=None, zoom, center, tiles="light", radius=25, blur=18)` | |
| `map_html(...)`, `heatmap_html(...)` | same pages as strings (web apps, reports) |
| `show(heatmap=False, port=0, **options)` | serves the page on 127.0.0.1 and opens the browser; returns the URL |
| `image(path="map.png", title=None, width=1200, height=800, overlays=None, background="#ffffff")` | static `.png`/`.svg`, offline |
| `to_png(...)`, `to_svg(...)` | bytes / str |
| Notebook | a `GeoEngine` displays as an interactive map in Jupyter |

`tiles`: `light` (default, OpenStreetMap muted), `osm`, `dark`, `humanitarian`, `cyclosm`,
`osm-fr`, `osm-de` (non-commercial), `topo` (OpenTopoMap, z ≤ 17), `satellite` (NASA Blue
Marble, overview z ≤ 10), `none`, or a custom `https://…/{z}/{x}/{y}.png` template plus
`attribution=`. No API keys. OpenStreetMap-hosted styles need the page served over http
(`show()`, `qoregeo serve file.html`); when opened from disk they show a notice instead.

## Geocoding

```python
from qoregeo.geocode import Geocoder

g = Geocoder()                                   # OpenStreetMap Nominatim, 1 request/s, cached
g.geocode("Gateway of India, Mumbai")            # best match dict or None
g.search("Main Street", limit=5, country_codes="us")
g.reverse(18.922, 72.8347)
Geocoder(provider="photon")                      # komoot Photon
Geocoder(url="http://localhost:8080")            # your own Nominatim (bulk jobs)
```

Result dicts: `lat`, `lng`, `name`, `category`, `type`, `importance`, `bbox` (`[w, s, e, n]`),
`address`, `provider`, `attribution`. Errors raise `qoregeo.GeocodingError` with a hint.

## Errors

All errors subclass `qoregeo.QOREgeoError` and a matching built-in (`ValueError`,
`KeyError`, `FileNotFoundError`, …) and explain the fix: `ColumnNotFoundError` lists the
columns the file does have, out-of-range coordinates ask whether lat/lng are swapped, etc.

## AI tool registry

```python
from qoregeo.tools import ToolContext, call_tool, tool_definitions

tool_definitions("anthropic")   # or "mcp", "openai", "openai-responses"
call_tool("nearest", {"path": "stores.csv", "point": {"lat": 28.6, "lng": 77.2}, "k": 3},
          ToolContext(root="data", allow_write=False))
```
