<div align="center">

[← Command Line](cli.md) · **API Reference** · [Recipes →](recipes.md)

</div>

---

# API Reference

Every public method in one page. For explanation and worked examples, follow
the links in the right-hand column.

```python
from qoregeo import GeoEngine
geo = GeoEngine()
```

---

## Contents

[Loading](#loading) ·
[Saving](#saving) ·
[Geometry](#geometry) ·
[Filtering](#filtering) ·
[Table operations](#table-operations) ·
[Inspection](#inspection) ·
[Cleaning](#cleaning) ·
[Analysis](#analysis) ·
[Indexing](#indexing) ·
[Visualisation](#visualisation) ·
[Protocol](#protocol) ·
[Module functions](#module-level-functions) ·
[Exceptions](#exceptions) ·
[Constants](#constants)

---

<a name="loading"></a>
## Loading

| Method | Returns | Description |
|---|---|---|
| `load(path, lat_col=None, lng_col=None, encoding='utf-8-sig')` | `self` | Read a file. Format from the extension: csv, geojson, json, ndjson, geojsonl, jsonl, wkt, gpx, kml, shp. |
| `load_data(features)` | `self` | Load from memory. Accepts Features, geometries, `(lat, lng)` tuples, or dicts with lat/lng keys. |
| `GeoEngine.from_records(records)` | `GeoEngine` | Classmethod. Build from dicts with lat/lng keys. |
| `GeoEngine.from_points(points)` | `GeoEngine` | Classmethod. Build from `(lat, lng)` tuples. |

Guide: [Loading &amp; Saving Data](loading-data.md)

---

<a name="saving"></a>
## Saving

| Method | Returns | Description |
|---|---|---|
| `save(path, encoding='utf-8')` | `self` | Write a file. Format from the extension: geojson, json, csv, ndjson, wkt, gpx, kml, html, svg, png. |
| `to_records()` | `list[dict]` | Flat dicts with `latitude`, `longitude`, then every property. |
| `to_geojson_string(indent=2)` | `str` | The FeatureCollection as JSON text. |
| `to_wkt()` | `list[str]` | Every geometry as a WKT string. |
| `get_features()` | `list[dict]` | The raw GeoJSON Feature list. |
| `get_geojson()` | `dict` | The full FeatureCollection dict. |

---

<a name="geometry"></a>
## Geometry

| Method | Returns | Description |
|---|---|---|
| `distance(a, b, unit='km', method='haversine')` | `float` | Great-circle distance. `method='vincenty'` for the WGS84 ellipsoid. |
| `bearing(a, b, as_degrees=False)` | `str` \| `float` | Initial bearing, as a 16-point compass string or degrees. |
| `destination(origin, bearing, distance, unit='km')` | `(lat, lng)` | Where you arrive travelling that far on that heading. |
| `midpoint(a, b)` | `(lat, lng)` | Great-circle midpoint. |
| `interpolate(a, b, fraction)` | `(lat, lng)` | Point at `fraction` (0 to 1) along the path. |
| `buffer(center, radius, unit='km', num_points=64)` | `dict` | Circular geofence as a GeoJSON Polygon. |
| `buffer_line(line, radius, unit='km')` | `dict` | Corridor around a polyline, as a MultiPolygon. |
| `buffer_geometry(geometry, radius, unit='km')` | `dict` | Buffer any geometry type. |
| `point_in_polygon(point, polygon)` | `bool` | Inside test. Holes and MultiPolygons respected; the boundary counts as inside. |
| `intersects(a, b)` | `bool` | Do two geometries share any space? |
| `contains(outer, inner)` | `bool` | Is one fully inside the other? |
| `area(geometry=None, unit='km2')` | `float` | Spherical area. No argument sums every feature. |
| `length(geometry=None, unit='km')` | `float` | Line length or polygon perimeter. No argument sums every feature. |
| `centroid(geometry=None)` | `(lat, lng)` | Area-weighted for polygons; no argument gives the dataset mean centre. |
| `centre_of_mass(weight_col=None)` | `(lat, lng)` | Weighted centre. Alias: `center_of_mass`. |
| `convex_hull()` | `dict` | Smallest convex polygon containing every feature. |
| `simplify(tolerance=0.001)` | `GeoEngine` | Douglas-Peucker. Tolerance in degrees. |
| `bounds()` | `dict` | `min_lat`, `max_lat`, `min_lng`, `max_lng`. |
| `bounds_polygon()` | `dict` | The bounding box as a drawable Polygon. |
| `project(crs='3857')` | `list[dict]` | Web Mercator metres, or `'utm'` for UTM. |

**Distance units:** `km` `miles` `mi` `m` `ft` `nm` `nmi`
**Area units:** `km2` `sqkm` `m2` `sqm` `ha` `hectares` `acres` `mi2` `sqmi`

Guide: [Geometry](geometry.md)

---

<a name="filtering"></a>
## Filtering

| Method | Returns | Description |
|---|---|---|
| `query(expression)` | `GeoEngine` | Expression filter. Alias: `where`. |
| `filter(column, value, op='==')` | `GeoEngine` | Single condition. Ops: `==` `!=` `>` `>=` `<` `<=` `contains` `startswith` `endswith`. |
| `filter_by(predicate)` | `GeoEngine` | Your own function over the properties dict. |
| `filter_by_radius(lat, lng, radius, unit='km')` | `GeoEngine` | Within a radius, nearest first, with `_distance` injected. |
| `filter_by_bbox(min_lat, min_lng, max_lat, max_lng)` | `GeoEngine` | Inside a bounding box. |
| `within(polygon)` | `GeoEngine` | Inside a polygon. |
| `outside(polygon)` | `GeoEngine` | Outside a polygon. |
| `nearest(point, unit='km')` | `dict` | Closest feature: `{feature, distance, index}`. |
| `knn(point, k=5, unit='km')` | `list[dict]` | The `k` closest, nearest first. |

**Query operators:** `==` `!=` `<>` `>` `>=` `<` `<=` `contains` `startswith`
`endswith` `in (…)` `is null` `is not null`, combined with `and` `or` `not`
and parentheses.

Guide: [Querying &amp; Filtering](querying.md)

---

<a name="table-operations"></a>
## Table operations

| Method | Returns | Description |
|---|---|---|
| `sort_by(column, reverse=False)` | `GeoEngine` | Numeric where possible, text otherwise. Blanks last. |
| `head(n=5)` | `GeoEngine` | First `n` features. |
| `tail(n=5)` | `GeoEngine` | Last `n` features. |
| `sample(n=10, seed=None)` | `GeoEngine` | Random subset; `seed` makes it reproducible. |
| `select(columns)` | `GeoEngine` | Keep only these properties. |
| `drop(columns)` | `GeoEngine` | Remove these properties. |
| `rename(mapping)` | `GeoEngine` | Rename properties from `{old: new}`. |
| `add_column(name, value)` | `GeoEngine` | Constant, or a callable receiving the properties dict. |
| `apply(function)` | `GeoEngine` | Transform each whole feature. |
| `concat(other)` | `GeoEngine` | Combine two engines. Also `a + b`. |
| `copy()` | `GeoEngine` | Independent deep copy. |

---

<a name="inspection"></a>
## Inspection

| Method | Returns | Description |
|---|---|---|
| `count()` | `int` | Number of features. Also `len(geo)`. |
| `columns()` | `list[str]` | Every property name present. |
| `unique(column)` | `list` | Distinct values, first-seen order. |
| `value_counts(column)` | `dict` | Frequency per value, most common first. |
| `stats(column)` | `dict` | `count`, `min`, `max`, `mean`, `median`, `sum`. Non-numeric values skipped. |
| `describe()` | `dict` | Size, geometry mix, columns, extent, dispersion, source. |
| `validate()` | `dict` | `valid`, `invalid`, `issues`, `likely_swapped`. Never raises. |

---

<a name="cleaning"></a>
## Cleaning

| Method | Returns | Description |
|---|---|---|
| `clean(drop_invalid=True)` | `GeoEngine` | Drop features with no geometry or no coordinates. |
| `dropna(columns=None)` | `GeoEngine` | Drop features with a missing or blank property. |
| `dedupe(tolerance_km=0.0, subset=None)` | `GeoEngine` | Remove duplicates; a tolerance merges near-identical points. |
| `fix_coordinates()` | `GeoEngine` | Swap latitude and longitude where the swap is unambiguous. |

Guide: [Loading Data → Checking data](loading-data.md#checking-data-before-you-trust-it)

---

<a name="analysis"></a>
## Analysis

| Method | Returns | Description |
|---|---|---|
| `cluster(eps_km=1.0, min_samples=3, column='_cluster')` | `GeoEngine` | DBSCAN. Noise is labelled `-1`. |
| `kmeans(k=3, column='_cluster', seed=None)` | `dict` | `{engine, centroids, inertia, iterations}`. |
| `spatial_join(polygons, prefix='', keep=None, how='left')` | `GeoEngine` | Tag points with the containing polygon's properties. |
| `hotspots(cell_km=5.0, min_count=2)` | `list[dict]` | Grid cells with `count`, `centre`, `bounds`, `polygon`. |
| `pattern()` | `dict` | Clark and Evans ratio: `clustered`, `random` or `dispersed`. |
| `dispersion()` | `dict` | Mean, median, max distance from the centre, plus standard distance. |
| `optimise_route(start=None, round_trip=False, unit='km')` | `dict` | Nearest-neighbour plus 2-opt. Alias: `optimize_route`. |
| `geohash_column(precision=7, column='_geohash')` | `GeoEngine` | Add a geohash to every feature. |
| `geocode(column, user_agent, country=None, skip_failures=True)` | `GeoEngine` | Fill coordinates from an address column. Needs network. |

Guide: [Analysis](analysis.md)

---

<a name="indexing"></a>
## Indexing

| Method | Returns | Description |
|---|---|---|
| `build_index(cell_size_km=None)` | `self` | Build the grid index. Automatic at 500+ features. |
| `index_stats()` | `dict` \| `None` | `features`, `cells`, `cell_size_km`, `avg_per_cell`, `max_per_cell`. |

---

<a name="visualisation"></a>
## Visualisation

| Method | Returns | Description |
|---|---|---|
| `map(output_path='map.html', …)` | `self` | Interactive Leaflet map. |
| `heatmap(output_path='heatmap.html', …)` | `self` | Density heatmap. |
| `choropleth(output_path, value_col, …)` | `self` | Quantile-shaded map. |
| `svg(output_path, …)` | `self` | Static vector map. No browser, no network. |
| `png(output_path, …)` | `self` | Static raster map. No browser, no network. |

<details>
<summary>Full signatures</summary>

```python
map(output_path="map.html", title="QOREgeo Map", zoom=5, center=None,
    basemap="dark", cluster=None, colour_by=None, color_by=None,
    tooltip_field=None, popup_fields=None, search=True, quiet=False)

heatmap(output_path="heatmap.html", title="QOREgeo Heatmap", intensity_col=None,
        zoom=5, center=None, basemap="dark", radius=25, blur=18, quiet=False)

choropleth(output_path, value_col, title="QOREgeo Choropleth", label_col=None,
           bins=5, zoom=5, center=None, basemap="dark", quiet=False)

svg(output_path, title="QOREgeo Map", width=1200, height=800,
    theme="dark", label_field=None)

png(output_path, title="QOREgeo Map", width=1200, height=800, theme="dark")
```

</details>

**Basemaps:** `dark` `light` `streets` `terrain` `satellite`
**Themes:** `dark` `light`

Guide: [Visualisation](visualisation.md)

---

<a name="protocol"></a>
## Python protocol

| Expression | Meaning |
|---|---|
| `len(geo)` | Feature count. |
| `for f in geo` | Iterate over Feature dicts. |
| `geo[3]` | One Feature dict. |
| `geo[1:5]` | A new `GeoEngine`. |
| `a + b` | Concatenate two engines. |
| `bool(geo)` | `False` when empty. |
| `repr(geo)` | `<GeoEngine [8 features]>`. |
| `geo` in Jupyter | A table preview of the first ten rows. |

---

<a name="module-level-functions"></a>
## Module-level functions

Available directly from `qoregeo`, for when you have geometry but no engine.

### Geometry

```python
from qoregeo import (
    destination, midpoint, vincenty_km, centroid_of, convex_hull, simplify,
    intersects, line_buffer, point_in_geometry,
    geometry_area_km2, geometry_length_km,
)
```

More in `qoregeo.geometry`: `bearing_degrees`, `interpolate`, `cross_track_km`,
`along_track_km`, `nearest_point_on_line`, `circle_ring`, `segment_buffer_ring`,
`geometry_buffer`, `bbox_of`, `bbox_polygon`, `coords_of`, `ring_area_km2`,
`ring_is_clockwise`, `segments_intersect`, `bbox_overlaps`, `contains`,
`convert_length`, `convert_area`, `to_km`.

### Analysis

```python
from qoregeo import (
    dbscan, kmeans, spatial_join, hotspots,
    nearest_neighbour_ratio, centre_of_mass, dispersion, spherical_mean,
)
```

### Geohash, tiles and projections

```python
from qoregeo import (
    encode, decode, neighbours, neighbors, quadkey, tile_of,
    to_web_mercator, from_web_mercator, to_utm, from_utm, utm_zone,
)
```

More in `qoregeo.geohash`: `bbox`, `decode_exactly`, `geohash_polygon`,
`common_prefix`, `tile_bounds`, `quadkey_to_tile`, `PRECISION_METRES`.

### Formats, routing, query and rendering

```python
from qoregeo import (
    parse_wkt, to_wkt,
    optimise_route, optimize_route, travel_time,
    compile_query, run_query,
    build_svg, build_png,
    SpatialIndex, Geocoder,
)
```

More in `qoregeo.formats`: `read_shapefile`, `read_gpx`, `read_kml`,
`read_ndjson`, `read_wkt_file`, `write_gpx`, `write_kml`, `write_ndjson`,
`write_wkt_file`, `read_any`, `write_any`, `iter_features`, `READABLE`,
`WRITABLE`.

More in `qoregeo.routing`: `nearest_neighbour_tour`, `two_opt`, `route_line`.

More in `qoregeo.map_builder`: `build_map`, `build_heatmap`,
`build_choropleth`, `BASEMAPS`, `CATEGORY_COLOURS`, `SEQUENTIAL_RAMP`.

---

<a name="exceptions"></a>
## Exceptions

All inherit from `QOREgeoError`, so one `except` catches everything.

| Exception | Raised when |
|---|---|
| `NoDataError` | A method needing data is called before loading any. |
| `InvalidCoordinateError` | Latitude or longitude out of range, or unparseable. |
| `InvalidUnitError` | An unrecognised unit or operator. |
| `ColumnNotFoundError` | A named column does not exist. Lists the ones that do. |
| `FileNotFoundError` | The path does not exist. Shadows the builtin inside `qoregeo`. |
| `UnsupportedFormatError` | An extension or file content QOREgeo cannot read. |
| `EmptyDatasetError` | A file or dataset holds no usable features. |
| `InvalidRadiusError` | A radius of zero or less. |
| `InvalidBufferError` | A polygon argument that is not a usable polygon. |
| `InvalidGeometryError` | A geometry structurally unusable for the operation. |
| `InvalidQueryError` | A query expression that will not parse. |
| `GeocodingError` | A lookup failed, or the service could not be reached. |
| `MissingIndexError` | An index-only operation without an index. |

```python
from qoregeo import QOREgeoError

try:
    geo = GeoEngine().load(path).query(expression)
except QOREgeoError as exc:
    print(exc)      # already explains the problem and the fix
```

---

<a name="constants"></a>
## Constants

| Name | Value | Meaning |
|---|---|---|
| `qoregeo.__version__` | `"1.1.0"` | Package version. |
| `GeoEngine.VERSION` | `"1.1.0"` | Same, on the class. |
| `GeoEngine.AUTO_INDEX_THRESHOLD` | `500` | Features above which an index is built automatically. |
| `map_builder.AUTO_CLUSTER_THRESHOLD` | `750` | Features above which map markers cluster. |
| `utils.EARTH_RADIUS_KM` | `6371.0088` | Mean Earth radius, IUGG. |
| `geometry.WGS84_A` | `6378137.0` | WGS84 semi-major axis, metres. |
| `geometry.WGS84_F` | `1/298.257223563` | WGS84 flattening. |
| `analysis.NOISE` | `-1` | DBSCAN label for unclustered points. |

---

<div align="center">

[← Command Line](cli.md) · [Documentation home](README.md) · [Recipes →](recipes.md)

</div>
