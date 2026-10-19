# Query a dataset

This page covers every way to pick features out of a `GeoEngine`. You filter by property values
with `filter()` and `where()`. You filter by place with a radius, a bounding box, a polygon, a
line or any shape. You find the nearest features, sort, take the top rows and chain all of
these. The page ends with the extent of a dataset (`bounds()`, `bbox()`, `centroid()`), data
that crosses the 180° meridian, the same queries on the command line, and the errors you can get.

Every example runs as written, in a folder that holds copies of the sample files in
[docs/data](data/). The test suite runs the blocks in order and compares each `print()` with the
comment next to it, so the comments show real output.

**On this page:** [Quick reference](#quick-reference) ·
[Property filters](#filter-by-a-property-value) ·
[Comparison rules](#how-filter-compares-values) ·
[where](#filter-with-your-own-function) ·
[Radius](#within-a-radius) ·
[Bounding box](#inside-a-bounding-box) ·
[Polygon](#inside-a-polygon) ·
[Line](#along-a-line) ·
[Intersects](#shapes-that-touch-a-shape) ·
[Nearest](#the-nearest-feature) ·
[k nearest](#the-k-nearest-features) ·
[_distance](#the-_distance-property) ·
[Sorting](#sort-and-take-the-top-rows) ·
[Chaining](#chain-queries) ·
[Extent](#extent-bounds-bbox-and-centroid) ·
[Antimeridian](#data-across-the-antimeridian) ·
[Command line](#from-the-command-line) ·
[Errors](#errors)

## Quick reference

| Method | Keeps | Returns | Adds `_distance` | Result order |
|---|---|---|---|---|
| `filter(column, value, op="==")` | features whose property matches `value` | new `GeoEngine` | no | as before |
| `where(predicate)` | features for which `predicate(properties)` is true | new `GeoEngine` | no | as before |
| `filter_by_radius(lat, lng, radius, unit="km", method="haversine")` | features within `radius` of a point | new `GeoEngine` | yes | nearest first |
| `filter_by_bbox(min_lat, min_lng, max_lat, max_lng)` | features inside a box | new `GeoEngine` | no | as before |
| `filter_by_polygon(polygon)` | features whose location is inside a polygon | new `GeoEngine` | no | as before |
| `filter_by_line(line, distance, unit="km")` | features within `distance` of a path | new `GeoEngine` | yes | nearest first |
| `filter_intersects(geometry)` | features whose whole geometry touches a shape | new `GeoEngine` | no | as before |
| `nearest(point, unit="km", method="haversine")` | the closest feature | `dict` with `feature`, `distance`, `index` | no | |
| `k_nearest(point, k=5, unit="km", method="haversine")` | the `k` closest features | new `GeoEngine` | yes | nearest first |
| `sort_by(column, descending=False)` | every feature | new `GeoEngine` | no | sorted |
| `head(n=5)` | the first `n` features | new `GeoEngine` | no | as before |
| `intersects(a, b)` | | `bool` | | |
| `bounds()`, `bbox()`, `centroid()` | | `dict`, `list`, `tuple` | | |

Every query returns a new engine and leaves the one you called it on unchanged, so calls chain:
`stores.filter("city", "Mumbai").sort_by("revenue", descending=True).head(3)`.

Points follow the Python API convention, `(lat, lng)`. GeoJSON positions inside features are
`[lng, lat]`. Bounding boxes from `bbox()` are `[west, south, east, north]`.

## Set up

```python
import warnings

from qoregeo import GeoEngine

stores = GeoEngine().load("stores.csv")
customers = GeoEngine().load("customers.csv")
zones = GeoEngine().load("zones.geojson")
route = GeoEngine().load("route.geojson")
cities = GeoEngine().load("cities.csv")


def names(geo, column="name"):
    """The values of one property, in feature order."""
    return [feature["properties"][column] for feature in geo]


print(stores)  # <GeoEngine [30 features]>
print(stores.get_features()[0]["properties"])  # {'name': 'Haldi Fresh Connaught Place', 'city': 'Delhi', 'category': 'grocery', 'revenue': '412000'}
```

`stores.csv` holds 30 fictional stores in Delhi, Mumbai and Bengaluru. `customers.csv` holds 40
customers in central Delhi, `zones.geojson` three delivery zones, `route.geojson` a Delhi to
Mumbai road and `cities.csv` 12 Indian cities. The `names()` helper is used through the rest of
the page.

CSV values load as strings, so `revenue` is `'412000'`, not `412000`. `filter()` and `sort_by()`
still compare numbers as numbers, as the next sections show. Pass `infer_types=True` to
`load()` to get real numbers (see [data](data.md)).

## Filter by a property value

```python
print(stores.filter("city", "Delhi").count())  # 11
print(stores.filter("city", "delhi").count())  # 11
print(stores.filter("city", "Delhi", op="!=").count())  # 19
print(stores.filter("revenue", 338000, op=">").count())  # 5
print(stores.filter("revenue", 338000, op=">=").count())  # 6
print(stores.filter("revenue", 99000, op="<").count())  # 2
print(stores.filter("revenue", 99000, op="<=").count())  # 3
print(stores.filter("category", ["cafe", "pharmacy"], op="in").count())  # 12
print(stores.filter("category", ["cafe", "pharmacy"], op="not in").count())  # 18
print(stores.filter("name", "haldi", op="contains").count())  # 8
```

`filter(column, value, op="==")` keeps the features whose property `column` matches `value`
and returns them as a new engine, in their original order.

| `op` | Keeps features whose property | Example on `stores.csv` | Count |
|---|---|---|---|
| `"=="` (default) | equals `value` | `filter("city", "delhi")` | 11 |
| `"!="` | does not equal `value`, including features without the property | `filter("city", "Delhi", op="!=")` | 19 |
| `">"` | is greater than `value` | `filter("revenue", 338000, op=">")` | 5 |
| `">="` | is greater than or equal to `value` | `filter("revenue", 338000, op=">=")` | 6 |
| `"<"` | is less than `value` | `filter("revenue", 99000, op="<")` | 2 |
| `"<="` | is less than or equal to `value` | `filter("revenue", 99000, op="<=")` | 3 |
| `"in"` | equals any item of the list `value` | `filter("category", ["cafe", "pharmacy"], op="in")` | 12 |
| `"not in"` | equals no item of the list `value` | `filter("category", ["cafe", "pharmacy"], op="not in")` | 18 |
| `"contains"` | contains `value` as text, ignoring case | `filter("name", "haldi", op="contains")` | 8 |

`"in"` and `"not in"` take a list, tuple or set. Any other `op`, such as `"="`, raises
`InvalidArgumentError`, and so does a plain string for `"in"`.

### How filter compares values

The type of `value` decides how the comparison works:

| `value` you pass | `==`, `!=`, `in`, `not in` | `>`, `>=`, `<`, `<=` | `contains` |
|---|---|---|---|
| a number (`int` or `float`) | numeric: `"412000"`, `412000` and `412000.0` all equal `412000` | numeric: text such as `"412000"` is read as a number, and properties that are not numbers are left out | text match |
| a string | text, ignoring case and surrounding spaces | text order, ignoring case | text match |
| `True` or `False` | text: `True` matches `True`, `"true"` and `"TRUE"` | text order | text match |

For the ordering operators and `contains`, a feature that lacks the property, or holds `None`,
is left out. `==` treats a missing property as an empty string, so `!=` and `not in` keep
features that lack it.

Pass numbers as numbers. A number written as a string is compared as text, and text order is
not number order:

```python
print(stores.filter("revenue", 300000, op=">").count())  # 7
print(stores.filter("revenue", "300000", op=">").count())  # 10
print("96000" > "300000")  # True
```

The second call kept `'96000'` and `'99000'` because the character `9` sorts after `3`.

Properties that are not numbers drop out of numeric comparisons. In `messy.csv`, one depot has
`staff` set to `twelve`:

```python
with warnings.catch_warnings():
    warnings.simplefilter("ignore")  # messy.csv also has rows with bad coordinates
    depots = GeoEngine().load("messy.csv")

print(names(depots, "staff"))  # ['12', '9', 'twelve', '11']
print(names(depots.filter("staff", 10, op=">")))  # ['Okhla Depot', 'Gachibowli Depot']
print(names(depots.filter("staff", 10, op="<=")))  # ['Andheri Depot']
print(names(depots.filter("staff", 12, op="!=")))  # ['Andheri Depot', 'Whitefield Depot', 'Gachibowli Depot']
```

`twelve` is neither greater than 10 nor less than or equal to 10, so both ordering filters
leave it out. It does not equal 12, so `!=` keeps it. The warning that `load()` gives for this
file is explained in [data](data.md).

Text order is what you want for ISO 8601 timestamps that share a time zone offset. This keeps
the five GPS fixes from 09:30 up to, but not including, 09:35:

```python
track = GeoEngine().load("track.csv")
window = track.filter("time", "2026-03-14T09:30", op=">=").filter("time", "2026-03-14T09:35", op="<")

print(window.count())  # 5
print([t[11:16] for t in names(window, "time")])  # ['09:30', '09:31', '09:32', '09:33', '09:34']
print(track.filter("time", "T09:3", op="contains").count())  # 10
```

### Booleans and typed values

```python
hospitals = GeoEngine().load("hospitals.csv")
print(names(hospitals, "emergency")[:3])  # ['true', 'true', 'false']
print(hospitals.filter("emergency", True).count())  # 7

typed = GeoEngine().load("hospitals.csv", infer_types=True)
print(typed.get_features()[0]["properties"])  # {'name': 'Ridge Valley Hospital', 'beds': 420, 'emergency': True}
print(typed.filter("emergency", True).count())  # 7
print(typed.filter("beds", 300, op=">=").count())  # 4
```

`filter("emergency", True)` gives the same 7 hospitals whether the column holds the text
`'true'` or the Python value `True`. Numeric filters also give the same answer with or without
`infer_types`. Typed values matter for `where()`, which runs your own Python.

### Column names are exact

```python
from qoregeo import ColumnNotFoundError

try:
    stores.filter("City", "Delhi")
except ColumnNotFoundError as err:
    error = err

print(error.column)  # City
print(error.available)  # ['name', 'city', 'category', 'revenue']
print(isinstance(error, LookupError))  # True
```

Values ignore case, but column names do not. A column that no feature has raises
`ColumnNotFoundError`, even when an earlier filter left no features, so a typo never passes
silently as an empty result. The latitude and longitude columns are not properties after
loading, so they are not in the list.

## Filter with your own function

```python
big = stores.where(lambda p: int(p["revenue"]) >= 400000)
print(names(big))  # ['Haldi Fresh Connaught Place', 'Volt Junction Lower Parel', 'Volt Junction MG Road']


def mumbai_food(p):
    return p["city"] == "Mumbai" and p["category"] in ("cafe", "grocery")


print(stores.where(mumbai_food).count())  # 5
print(typed.where(lambda p: p["emergency"] and p["beds"] >= 300).count())  # 4
print(depots.where(lambda p: p["staff"].isdigit() and int(p["staff"]) > 10).count())  # 2
```

`where(predicate)` calls `predicate` with each feature's properties dict and keeps the features
for which it returns a true value. Use it for conditions `filter()` cannot express: several
columns at once, arithmetic, or your own parsing.

The predicate is plain Python, so the rules differ from `filter()`:

- Comparisons are exact. `p["city"] == "mumbai"` is false for `'Mumbai'`.
- CSV values are strings unless you loaded with `infer_types=True`, so convert them yourself
  (`int(p["revenue"])`).
- An exception in the predicate stops the query. Use `p.get("column")` for a property that
  some features lack, and guard conversions as the `depots` line does.
- The predicate gets the source engine's own dict. Read it; do not change it.
- `where()` does not check column names. A misspelled key raises `KeyError` from your function.

Passing something that is not callable raises `InvalidArgumentError`.

## Filter by place

The spatial filters test one location per feature. For a Point that is the point itself. For a
Polygon or MultiPolygon it is the area centroid of the outer rings, and for other geometries the
mean of their vertices. [Locate non-point features](#how-non-point-features-are-located) shows
what that means in practice. `filter_intersects()` is the exception: it tests the whole geometry.

### Within a radius

```python
near_cp = stores.filter_by_radius(28.6315, 77.2167, radius=5)

print(near_cp.count())  # 4
print(names(near_cp))  # ['Haldi Fresh Connaught Place', 'Loom Lane Chandni Chowk', 'Volt Junction Karol Bagh', 'Brew Bazaar Lodhi Colony']
print(names(near_cp, "_distance"))  # [0.0, 2.5044, 3.3889, 4.2536]

in_miles = stores.filter_by_radius(28.6315, 77.2167, radius=3, unit="miles")
print(names(in_miles, "_distance"))  # [0.0, 1.5562, 2.1058, 2.6431]
```

`filter_by_radius()` keeps the features within `radius` of a point, sorts them nearest first
and adds a `_distance` property in `unit`. Distances are great-circle distances, so the circle
works at any latitude and across the 180° meridian.

| Parameter | Default | What it does |
|---|---|---|
| `lat`, `lng` | required | The center, as two numbers. (`nearest()` takes one point instead.) |
| `radius` | required | The search radius, a finite number greater than 0, in `unit`. |
| `unit` | `"km"` | `"km"`, `"m"`, `"miles"` or `"mi"`, `"ft"`, `"yd"`, `"nmi"`, plus spelled-out names such as `"meters"`. |
| `method` | `"haversine"` | `"haversine"` (sphere) or `"vincenty"` (WGS84 ellipsoid). See [measurements](measurements.md). |

A feature exactly `radius` away is kept.

### Inside a bounding box

```python
south_mumbai = stores.filter_by_bbox(18.89, 72.80, 19.07, 72.86)
print(names(south_mumbai))  # ['Haldi Fresh Bandra West', 'Brew Bazaar Colaba', 'Volt Junction Lower Parel', 'Loom Lane Dadar', 'Tulsi Pharmacy Fort']

same = stores.filter_by_bbox(min_lat=18.89, min_lng=72.80, max_lat=19.07, max_lng=72.86)
print(same.count())  # 5
```

`filter_by_bbox(min_lat, min_lng, max_lat, max_lng)` keeps features whose location lies inside
the box, edges included. The arguments are south, west, north, east. That is not the order of
`bbox()`, which returns `[west, south, east, north]`, so unpack it or pass `bounds()` by keyword:

```python
delhi = stores.filter("city", "Delhi")
print(delhi.bounds())  # {'min_lat': 28.52, 'max_lat': 28.6519, 'min_lng': 77.057, 'max_lng': 77.2433}
print(customers.filter_by_bbox(**delhi.bounds()).count())  # 27

west, south, east, north = delhi.bbox()
print(customers.filter_by_bbox(south, west, north, east).count())  # 27
```

The keys of `bounds()` match the parameter names, so `**other.bounds()` keeps the features
that fall inside another dataset's extent.

Rules for the box:

- `min_lat` greater than `max_lat` raises `InvalidArgumentError`.
- `min_lng` greater than `max_lng` means the box crosses the 180° meridian. See
  [Data across the antimeridian](#data-across-the-antimeridian).
- Longitudes from -360 to 360 are accepted and wrapped, so a box written as 179 to 181 works.
- A box 360° wide or wider keeps every feature in its latitude band.

### Inside a polygon

```python
counts = {zone["properties"]["name"]: customers.filter_by_polygon(zone).count() for zone in zones}
print(counts)  # {'Central': 13, 'South': 10, 'West': 7}

print(customers.filter_by_polygon(zones.get_geojson()).count())  # 30
```

`filter_by_polygon(polygon)` keeps the features whose location is inside `polygon`. Here each
zone is a Feature, and the whole FeatureCollection counts as the union of its zones. 30 of the
40 customers are in a zone.

Holes are respected. The Central zone has a hole for the President's Estate, with 2 customers in it:

```python
central = zones.filter("name", "Central").get_features()[0]
outer_ring = central["geometry"]["coordinates"][0]
no_hole = {"type": "Polygon", "coordinates": [outer_ring]}

print(customers.filter_by_polygon(no_hole).count())  # 15
print(customers.filter_by_polygon(central).count())  # 13
```

A circle from `buffer()` works too. It is tested as an exact circle on the sphere, so it keeps
the same features as `filter_by_radius()`, without the `_distance` property or the sorting:

```python
circle = customers.buffer((28.6139, 77.2090), radius=3)
print(customers.filter_by_polygon(circle).count())  # 12
print(customers.filter_by_radius(28.6139, 77.2090, radius=3).count())  # 12
```

`filter_by_polygon()` accepts:

| Input | How it is tested |
|---|---|
| a `buffer()` result, or a Feature wrapping one | exact circle on the sphere |
| a GeoJSON `Polygon` or `MultiPolygon` | ray casting, holes excluded |
| a `Feature` whose geometry is one of those | the same |
| a `FeatureCollection` | the union of its Polygon and MultiPolygon features; other geometry types are ignored |

Anything else raises `InvalidBufferError`. Polygon edges are straight lines in longitude and
latitude, which is what GeoJSON tools draw. For zones a few kilometers across this makes no
practical difference.

### Along a line

```python
nh48 = route.get_features()[0]
print(nh48["properties"]["name"])  # Delhi to Mumbai freight route
print(round(cities.length(nh48), 1))  # 1266.2

along = cities.filter_by_line(nh48, distance=50)
print(names(along))  # ['Delhi', 'Mumbai', 'Ahmedabad', 'Surat', 'Jaipur']

wider = cities.filter_by_line(nh48, distance=200)
print(names(wider)[-1], names(wider, "_distance")[-1])  # Pune 120.1525

print(names(stores.filter_by_line(nh48, distance=3)))  # ['Haldi Fresh Connaught Place', 'Brew Bazaar Lodhi Colony', 'Haldi Fresh Powai']
```

`filter_by_line(line, distance, unit="km")` keeps features within `distance` of a path, a
"line buffer". Results come nearest first, with `_distance` set to the great-circle distance to
the closest segment. The five cities on the route are 0 km from it, so they keep their file
order.

`line` can be a list of `(lat, lng)` points, a GeoJSON `LineString` or `MultiLineString`, or a
Feature that wraps one:

```python
delhi_jaipur = [(28.6139, 77.2090), (26.9124, 75.7873)]
print(names(cities.filter_by_line(delhi_jaipur, distance=100)))  # ['Delhi', 'Jaipur']
```

`distance` must be greater than 0. There is no `method` parameter: line distances use the sphere.

### Shapes that touch a shape

```python
near_cp_zone = zones.buffer((28.6315, 77.2167), radius=1)

print(names(zones.filter_intersects(near_cp_zone)))  # ['Central']
print(names(zones.filter_by_polygon(near_cp_zone)))  # []

south = zones.filter("name", "South").get_features()[0]
print(zones.intersects(central, nh48))  # True
print(zones.intersects(central, south))  # False
print(names(zones.filter_intersects(nh48)))  # ['Central']
```

`filter_intersects(geometry)` keeps the features whose whole geometry shares at least one
point with `geometry`. Touching counts. `intersects(a, b)` answers the same question for two
shapes and returns `True` or `False`.

The first two lines show the difference from `filter_by_polygon()`. The 1 km circle around
Connaught Place lies inside the Central zone, so the two shapes intersect. The circle does not
contain Central's centroid, so `filter_by_polygon()` keeps nothing.

Both methods accept any GeoJSON geometry, a Feature, a FeatureCollection or a `buffer()`
result. Buffers are tested as exact circles on the sphere, and shapes that meet at 180° and
-180° touch. For point data, `filter_intersects()` and `filter_by_polygon()` keep the same
features.

### How non-point features are located

```python
inside = zones.filter_intersects({"type": "Point", "coordinates": [77.2090, 28.6139]})
print(names(inside))  # ['Central']

closest = zones.nearest((28.6139, 77.2090))
print(closest["feature"]["properties"]["name"], closest["distance"])  # Central 1.1897
```

The point `(28.6139, 77.2090)` is inside the Central zone, yet `nearest()` reports 1.19 km. The
distance is measured to Central's centroid, not to its edge. This applies to `nearest()`,
`k_nearest()`, `filter_by_radius()`, `filter_by_bbox()`, `filter_by_polygon()`,
`filter_by_line()` and `centroid()`. To find the zone that contains a point, use
`filter_intersects()` with a Point, as above, or `point_in_polygon()`.

## Find the nearest features

### The nearest feature

```python
here = (28.6139, 77.2090)
result = stores.nearest(here)

print(sorted(result))  # ['distance', 'feature', 'index']
print(result["feature"]["properties"]["name"])  # Haldi Fresh Connaught Place
print(result["distance"], result["index"])  # 2.0964 0
```

`nearest(point, unit="km", method="haversine")` returns a dict:

| Key | Value |
|---|---|
| `feature` | the closest feature (the engine's own dict, not a copy) |
| `distance` | the distance to it in `unit`, rounded to 4 decimals |
| `index` | its position in the engine you called `nearest()` on |

The point can be a `(lat, lng)` tuple or list, a `{"lat": ..., "lng": ...}` dict (`"lon"`,
`"latitude"` and `"longitude"` work too), a GeoJSON Point or a Feature with a Point geometry:

```python
print(stores.nearest({"lat": 19.0760, "lng": 72.8777}, unit="m")["distance"])  # 3570.0742
print(stores.nearest({"type": "Point", "coordinates": [72.8777, 19.0760]})["index"])  # 20
```

Filter first to ask "nearest of a kind". The closest hospital is a clinic without an emergency
department:

```python
print(hospitals.nearest(here)["feature"]["properties"]["name"])  # Lodhi Gardens Clinic

emergency = hospitals.filter("emergency", True).nearest(here)
print(emergency["feature"]["properties"]["name"], emergency["distance"])  # Karol Bagh Medical Centre 3.9711
print(emergency["index"])  # 3
```

`index` is 3 because Karol Bagh is the fourth feature of the filtered engine. When two features
are equally close, `nearest()` returns the one that comes first.

### The k nearest features

```python
bengaluru = (12.9716, 77.5946)
top3 = stores.k_nearest(bengaluru, k=3)

print(top3)  # <GeoEngine [3 features]>
print(names(top3))  # ['Volt Junction MG Road', 'Loom Lane Malleshwaram', 'Haldi Fresh Indiranagar']
print(names(top3, "_distance"))  # [1.3742, 4.8007, 5.0628]
print(names(stores.k_nearest(bengaluru, k=3, method="vincenty"), "_distance"))  # [1.3751, 4.7906, 5.0686]
print(stores.k_nearest(bengaluru, k=100).count())  # 30
```

`k_nearest(point, k=5, unit="km", method="haversine")` returns a new engine with the `k`
closest features, nearest first, each with a `_distance` property. Ties keep file order. `k`
must be an integer of 1 or more; a `k` larger than the dataset returns every feature. It takes
the same point forms as `nearest()`.

On an empty dataset, `k_nearest()` returns an empty engine and `nearest()` raises
`NoDataError` (see [Empty results](#empty-results)).

### The _distance property

`filter_by_radius()`, `filter_by_line()` and `k_nearest()` add a `_distance` property to each
feature they return. It is in the `unit` you passed, rounded to 4 decimals.

```python
ring = stores.filter_by_radius(28.6315, 77.2167, radius=10)
print(ring.count())  # 8
print("_distance" in stores.get_features()[0]["properties"])  # False

print(names(ring.filter("_distance", 3, op="<")))  # ['Haldi Fresh Connaught Place', 'Loom Lane Chandni Chowk']

hauz_khas = (28.5494, 77.2001)
print(names(ring.k_nearest(hauz_khas, k=1), "_distance"))  # [0.0]
```

- The source engine never gets the property. Only the returned features have it.
- It is an ordinary property, so `filter()` and `sort_by()` can use it, and `save()` writes it
  as a column.
- A later distance query replaces it. Above, `k_nearest()` measured from Hauz Khas and
  overwrote the distances from Connaught Place.

## Sort and take the top rows

```python
top = stores.sort_by("revenue", descending=True).head(3)
print(names(top))  # ['Volt Junction Lower Parel', 'Haldi Fresh Connaught Place', 'Volt Junction MG Road']
print(names(top, "revenue"))  # ['451000', '412000', '402000']

print(names(stores.sort_by("name").head(3)))  # ['Brew Bazaar Colaba', 'Brew Bazaar Hauz Khas', 'Brew Bazaar HSR Layout']
print(stores.head().count(), stores.head(0).count(), stores.head(100).count())  # 5 0 30
```

`sort_by(column, descending=False)` returns a new engine sorted by one property:

- Numbers sort as numbers, and that includes numeric strings from a CSV file, so `'96000'`
  comes before `'412000'`.
- Other values sort as text, ignoring case.
- In ascending order numbers come before text. `descending=True` reverses that, so text comes first.
- Features where the property is missing or empty go last in both directions.
- Features with equal values keep their order.

`head(n=5)` keeps the first `n` features. `n` must be an integer of 0 or more.

```python
print(names(depots.sort_by("staff")))  # ['Andheri Depot', 'Gachibowli Depot', 'Okhla Depot', 'Whitefield Depot']
print(names(depots.sort_by("staff", descending=True)))  # ['Whitefield Depot', 'Okhla Depot', 'Gachibowli Depot', 'Andheri Depot']
```

The text value `twelve` sorts after the numbers, so a descending sort puts it first. Filter it
out first (`depots.filter("staff", 0, op=">=")`) when you want the top numbers only.

`sort_by()` checks the column name like `filter()` does and raises `ColumnNotFoundError` for a
column no feature has.

## Chain queries

```python
picks = (
    stores
    .filter("city", "Mumbai")
    .filter("revenue", 150000, op=">=")
    .sort_by("revenue", descending=True)
    .head(2)
)
print(names(picks))  # ['Volt Junction Lower Parel', 'Haldi Fresh Bandra West']
print(stores.count())  # 30
```

Each query returns a new `GeoEngine`, so you can chain them in any order. Attribute filters,
spatial filters and nearest-neighbor queries mix freely. These are the premium customers in the
Central zone within 3 km of the Connaught Place store, nearest first:

```python
premium_near_cp = (
    customers
    .filter("segment", "premium")
    .filter_by_polygon(central)
    .filter_by_radius(28.6315, 77.2167, radius=3)
)
print(names(premium_near_cp, "id"))  # ['C034', 'C013', 'C019']
print(names(premium_near_cp, "_distance"))  # [0.393, 1.7244, 2.7333]
```

Filter with the cheap, selective condition first when you can. Every query scans the features it
is given once, and there is no spatial index, so a smaller input makes each later step faster.
See [experiments](experiments.md) for measured timings.

### What a new engine shares

```python
delhi_stores = stores.filter("city", "Delhi")
delhi_stores.get_features()[0]["properties"]["revenue"] = "0"

print(stores.get_features()[0]["properties"]["revenue"])  # 412000
print(delhi_stores is stores)  # False
print(delhi_stores.get_features()[0]["geometry"] is stores.get_features()[0]["geometry"])  # True
```

A query copies each feature dict and its properties dict, like a shallow copy in pandas.
Setting a property on a result does not change the engine it came from. Geometries and nested
property values are shared, so do not edit coordinates in place.

`load()`, `load_data()`, `add_point()` and `save()` are different: they change or write the
engine you call them on and return that same engine. See [data](data.md).

### Empty results

```python
from qoregeo import NoDataError

vip = customers.filter("segment", "vip")
print(vip)  # <GeoEngine [0 features]>
print(vip.filter("orders", 10, op=">").count())  # 0
print(vip.k_nearest((28.61, 77.21), k=3).count())  # 0

try:
    vip.nearest((28.61, 77.21))
except NoDataError as err:
    error = err

print(error.method_name, error.empty)  # nearest True
print(isinstance(error, RuntimeError))  # True
```

A filter that matches nothing returns an empty engine, and the filters that follow it return
empty engines too. The engine still knows its columns, so a misspelled column raises
`ColumnNotFoundError` even here. `nearest()`, `bounds()`, `bbox()` and `centroid()` have no
answer for zero features and raise `NoDataError`. Check `count()` first:

```python
result = customers.filter("segment", "premium")
if result.count():
    print(result.nearest((28.61, 77.21))["feature"]["properties"]["id"])
```

## Extent: bounds, bbox and centroid

```python
print(customers.bounds())  # {'min_lat': 28.5354, 'max_lat': 28.6824, 'min_lng': 77.1601, 'max_lng': 77.2692}
print(customers.bbox())  # [77.1601, 28.5354, 77.2692, 28.6824]
print(customers.centroid())  # (28.610415, 77.211283)

print(zones.bbox())  # [77.14, 28.54, 77.265, 28.675]
print(zones.centroid())  # (28.614654, 77.205523)
```

| Method | Returns | Built from | Across the 180° meridian |
|---|---|---|---|
| `bounds()` | `{"min_lat", "max_lat", "min_lng", "max_lng"}`, keys that match `filter_by_bbox()` | every vertex of every geometry | plain minimum and maximum, nearly 360° wide |
| `bbox()` | `[west, south, east, north]`, the GeoJSON (RFC 7946) order | every vertex of every geometry | the tight box, with `west > east` |
| `centroid()` | `(lat, lng)`, rounded to 6 decimals | one location per feature | spherical mean, safe across 180° |

For the zones, `bbox()` covers every polygon vertex, while `centroid()` averages the three zone
centroids. All three methods raise `NoDataError` on an empty dataset.

## Data across the antimeridian

Fiji's islands sit on both sides of the 180° meridian, so some towns have longitudes near 178
and others near -179. `fiji.csv` holds nine of them.

```python
fiji = GeoEngine().load("fiji.csv")

print(fiji.bbox())  # [177.4356, -19.05, -178.8, -16.4167]
print(fiji.bounds())  # {'min_lat': -19.05, 'max_lat': -16.4167, 'min_lng': -179.9922, 'max_lng': 179.3833}

west, south, east, north = fiji.bbox()
print(round(east + 360 - west, 4))  # 3.7644
b = fiji.bounds()
print(round(b["max_lng"] - b["min_lng"], 4))  # 359.3755
```

`bbox()` returns a box from 177.4356° east across 180° to 178.8° west. West is greater than
east, which is how RFC 7946 writes a box that crosses the meridian. It is 3.76° wide.
`bounds()` returns the plain minimum and maximum, which describe a box 359.38° wide that runs
the long way round the globe.

The center needs the same care:

```python
print(fiji.centroid())  # (-17.569325, 179.16099)

lngs = [feature["geometry"]["coordinates"][0] for feature in fiji]
print(round(sum(lngs) / len(lngs), 4))  # 59.1594
print(round(fiji.distance(fiji.centroid(), (-17.5693, 59.1594))))  # 12377
```

`centroid()` averages the points as 3D vectors and lands in Fiji. A plain average of the
longitudes gives 59.16°, a point in the Indian Ocean 12,377 km away.

### Boxes that cross 180°

```python
print(names(fiji.filter_by_bbox(-17.5, 179.0, -16.0, -179.5)))  # ['Labasa', 'Savusavu', 'Waiyevo']
print(names(fiji.filter_by_bbox(-17.5, 179.0, -16.0, 180.5)))  # ['Labasa', 'Savusavu', 'Waiyevo']
print(names(fiji.filter_by_bbox(-17.5, -179.5, -16.0, 179.0)))  # ['Lomaloma']

print(fiji.filter_by_bbox(south, west, north, east).count())  # 9
```

When `min_lng` is greater than `max_lng`, `filter_by_bbox()` reads the box as crossing the
meridian. The first call keeps the towns between 179° east and 179.5° west. The second writes
the same box with a longitude past 180, which is wrapped to -179.5. The third swaps west and
east, which selects the 358.5° wide box on the other side of the globe, and only Lomaloma lies
in it within the latitude band. The last line passes the tight `bbox()` back in, unpacked to
south, west, north, east, and keeps all nine towns.

### Distances across 180°

```python
print(fiji.nearest((-16.8, 180.0))["feature"]["properties"]["name"])  # Waiyevo
print(fiji.nearest((-16.8, -180.0))["distance"])  # 1.4418

across = fiji.filter_by_radius(-16.8, 179.9, radius=100)
print(names(across), names(across, "_distance"))  # ['Waiyevo', 'Savusavu', 'Labasa'] [11.5359, 60.4363, 69.6267]
```

Distances are measured on the sphere, so 180° and -180° are the same meridian, and Waiyevo at
-179.99° is 11.5 km from a point at 179.9°. `filter_by_radius()`, `nearest()`, `k_nearest()`
and `filter_by_line()` need no special handling. Polygons and `intersects()` also accept rings
drawn with longitudes past 180 (for example 175 to 185).

## From the command line

`qoregeo export` runs the same queries and writes the result to a CSV, TSV or GeoJSON file.
`qoregeo map` and `qoregeo image` take the same filter flags.

```bash
qoregeo export stores.csv -o top.csv --where "revenue>=300000" --sort revenue --desc --limit 3
qoregeo export stores.csv -o near_cp.geojson --near 28.6315,77.2167,5
qoregeo export stores.csv -o near_cp_miles.csv --near 28.6315,77.2167,3 --unit miles
qoregeo export stores.csv -o south_mumbai.csv --bbox 72.80,18.89,72.86,19.07
qoregeo export stores.csv -o mumbai_food.csv --where "city==Mumbai" --where 'category in ["cafe","grocery"]'
qoregeo export stores.csv -o haldi.csv --where "name contains haldi"
qoregeo export customers.csv -o premium_in_zones.csv --within zones.geojson --where "segment==premium"
qoregeo export fiji.csv -o east_of_180.csv --bbox 179,-17.5,-179.5,-16
```

`top.csv` holds the three stores with the highest revenue:

```csv
latitude,longitude,name,city,category,revenue
18.998,72.83,Volt Junction Lower Parel,Mumbai,electronics,451000
28.6315,77.2167,Haldi Fresh Connaught Place,Delhi,grocery,412000
12.9756,77.6066,Volt Junction MG Road,Bengaluru,electronics,402000
```

| Flag | Python equivalent |
|---|---|
| `--near LAT,LNG,RADIUS` with `--unit` | `filter_by_radius(lat, lng, radius, unit=...)` |
| `--bbox W,S,E,N` | `filter_by_bbox(s, w, n, e)`; `W` greater than `E` crosses 180° |
| `--within FILE` | `filter_intersects()` with the GeoJSON in `FILE` |
| `--where EXPR`, repeatable | `filter(column, value, op)`; every condition must hold |
| `--sort COLUMN`, `--desc` | `sort_by(column, descending=True)` |
| `--limit N` | `head(N)` |

The flags apply in the order of the table, whatever order you type them in. The command loads
CSV files with `infer_types=True`. A `--where` expression is `column`, an operator, then a
value: `==`, `=` (the same as `==`), `!=`, `>`, `>=`, `<` or `<=` with or without spaces, or
` in `, ` not in ` or ` contains ` with spaces around them. The value is read as JSON when it
parses (`300000`, `true`, `["cafe","grocery"]`) and as text otherwise (`Mumbai`). Quote each
expression so the shell does not read `>` as a redirect.

`qoregeo info` prints the extent, with the tight box for data across 180°:

```bash
qoregeo info fiji.csv
```

```text
fiji.csv: 9 features
  geometry : Point × 9
  bbox     : west 177.4356, south -19.05, east -178.8, north -16.4167
  centre   : -17.569325, 179.16099
  columns  : name (str), island (str)
```

See [the command line](cli.md) for every flag.

### From an AI assistant

The `query_features` and `nearest` tools of the MCP server take the same filters as JSON. Points
are `{"lat": ..., "lng": ...}` objects and `bbox` is `[west, south, east, north]`:

```json
{
  "path": "stores.csv",
  "near": {"lat": 28.6315, "lng": 77.2167, "radius": 5},
  "where": [{"column": "category", "op": "in", "value": ["cafe", "grocery"]}],
  "sort_by": "revenue",
  "descending": true,
  "limit": 10
}
```

You can run a tool from the shell with `qoregeo call`:

```bash
qoregeo call nearest '{"path": "hospitals.csv", "point": {"lat": 28.6139, "lng": 77.209}, "k": 2, "where": [{"column": "emergency", "value": true}]}'
```

It prints Karol Bagh Medical Centre at 3.9711 km and Ridge Valley Hospital at 6.1797 km. See
[AI integration](ai-integration.md) for the full tool list.

## Errors

Every error is a subclass of `QOREgeoError` and of a built-in exception, so `except ValueError`
works too. The message names the bad value, what was expected and a call to copy.

| Error | Built-in base | Raised when | Attributes |
|---|---|---|---|
| `ColumnNotFoundError` | `LookupError` | `filter()` or `sort_by()` names a column that no feature has (names are case-sensitive) | `.column`, `.available`, `.filepath` |
| `InvalidArgumentError` | `ValueError` | an unknown `op`; a string for `"in"`; `where()` gets something not callable; `k` below 1; `n` below 0; `min_lat` above `max_lat`; an unknown `method`; a bad `line` or `geometry` | `.name`, `.value`, `.expected` |
| `InvalidRadiusError` | `ValueError` | a `radius` or line `distance` that is zero, negative, not finite or not a number | `.radius` |
| `InvalidUnitError` | `ValueError` | an unknown `unit`, such as `"kms"` | `.unit`, `.valid` |
| `InvalidCoordinateError` | `ValueError` | a point or box edge out of range or not a number | `.lat`, `.lng`, `.context` |
| `InvalidBufferError` | `ValueError` | `filter_by_polygon()` gets something that is not a polygon | `.reason` |
| `NoDataError` | `RuntimeError` | nothing is loaded yet, or `nearest()`, `bounds()`, `bbox()` or `centroid()` runs on zero features | `.method_name`, `.empty` |

```python
from qoregeo import InvalidArgumentError, InvalidUnitError

try:
    stores.filter("city", "Delhi", op="=")
except InvalidArgumentError as err:
    error = err

print(error.name, error.value)  # op =
print(isinstance(error, ValueError))  # True

try:
    stores.filter_by_radius(28.6315, 77.2167, radius=5, unit="kms")
except InvalidUnitError as err:
    error = err

print(error.valid)  # ['km', 'm', 'miles', 'mi', 'ft', 'yd', 'nmi']
```

The message for the first error reads:

```text
❌  QOREgeo: Invalid Argument 'op'
────────────────────────────────────────────
Received: '='
Expected: '==' | '!=' | '>' | '>=' | '<' | '<=' | 'in' | 'not in' | 'contains'

Fix it:
    geo.filter('population', 1000000, op='>')
```

[Troubleshooting](troubleshooting.md) covers the errors from loading files.

## Next steps

- [Measurements](measurements.md): distances, bearings, buffers, areas and lengths.
- [Maps](maps.md) and [images](images.md): draw what a query returned.
- [Cookbook](cookbook.md): longer tasks that combine these queries.
- [API reference](api-reference.md): every method and parameter.
