# Measure distances, directions and shapes

This page covers the measurement methods of `GeoEngine`: the distance between two points (in
seven units, on a sphere or on the WGS84 ellipsoid), compass bearings, destination points and
midpoints, circles made with `buffer()`, point-in-polygon and intersection tests, area, length,
the distance from a point to a line, and geohashes. Each section shows runnable code with its
printed output, the options in a table, and what happens with bad input.

Every example runs as written, in a folder that holds copies of the sample files in
[docs/data](data/). The test suite runs the blocks of this page in order and compares each
`print()` with the comment next to it, so the comments show real output.

**On this page:** [Quick reference](#quick-reference) ·
[Points](#points-you-can-pass) ·
[Distance](#measure-a-distance) ·
[Haversine or Vincenty](#haversine-or-vincenty) ·
[Bearing](#get-a-compass-bearing) ·
[Destination](#find-a-destination-point) ·
[Midpoint](#find-the-midpoint) ·
[Buffer](#draw-a-circle-with-buffer) ·
[Point in polygon](#test-a-point-against-a-polygon) ·
[Intersects](#test-whether-two-shapes-intersect) ·
[Area](#measure-an-area) ·
[Length](#measure-the-length-of-a-path) ·
[Distance to a line](#measure-the-distance-to-a-line) ·
[Geohash](#encode-points-as-geohashes) ·
[Accuracy](#accuracy) ·
[Command line and AI tools](#from-the-command-line-and-ai-tools) ·
[Errors](#errors)

## Quick reference

| Method | Returns | Earth model |
|---|---|---|
| `distance(point_a, point_b, unit="km", method="haversine")` | `float` in `unit`, 4 decimals | sphere, or WGS84 with `method="vincenty"` |
| `bearing(point_a, point_b, as_degrees=False)` | a 16-point compass name, or `float` degrees from 0 to 360 (2 decimals) | sphere |
| `destination(point, distance, bearing, unit="km")` | `(lat, lng)`, 6 decimals | sphere |
| `midpoint(point_a, point_b)` | `(lat, lng)`, 6 decimals | sphere |
| `buffer(center, radius, unit="km", num_points=64)` | a GeoJSON Polygon `dict` that also remembers its circle | sphere |
| `point_in_polygon(point, polygon)` | `bool` | straight edges in lng/lat; buffers are exact circles |
| `intersects(geometry_a, geometry_b)` | `bool` | straight edges in lng/lat; buffers are exact circles |
| `area(polygon, unit="km2")` | `float` in `unit`, 4 decimals | sphere |
| `length(line, unit="km", method="haversine")` | `float` in `unit`, 4 decimals | sphere, or WGS84 with `method="vincenty"` |
| `distance_to_line(point, line, unit="km")` | `float` in `unit`, 4 decimals | sphere |
| `geohash(point, precision=9)` | `str` of 1 to 12 characters | lng/lat grid |
| `geohash_decode(geohash)` | `(lat, lng)` of the cell center, 6 decimals | lng/lat grid |

"Sphere" means a sphere with the mean Earth radius, 6371.0088 km.

None of these methods reads the loaded dataset, so they work on an empty engine. They also
accept loaded features as points, which is how you measure your own data. For "which features
are near this point" questions (`nearest()`, `filter_by_radius()`, `filter_by_polygon()`), see
[Queries](queries.md).

## Points you can pass

```python
import math
import warnings
from collections import Counter

from qoregeo import GeoEngine, InvalidBufferError, InvalidCoordinateError

geo = GeoEngine()

delhi = (28.6139, 77.2090)
mumbai = (19.0760, 72.8777)
india_gate = (28.6129, 77.2295)

cities = GeoEngine().load("cities.csv")
delhi_feature, mumbai_feature = cities.get_features()[:2]

forms = [
    (28.6139, 77.2090),
    [28.6139, 77.2090],
    (28.6139, 77.2090, 216.0),
    {"lat": 28.6139, "lng": 77.2090},
    {"lat": 28.6139, "lon": 77.2090},
    {"latitude": 28.6139, "longitude": 77.2090},
    {"lat": "28.6139", "lng": "77.2090"},
    {"type": "Point", "coordinates": [77.2090, 28.6139]},
    delhi_feature,
]
print({geo.distance(point, mumbai) for point in forms})  # {1148.0965}
```

Every method on this page that takes a point accepts all of these forms. The type hint for them
is `qoregeo.PointLike`.

| Form | Example (Delhi) | Notes |
|---|---|---|
| tuple or list | `(28.6139, 77.2090)` | `(lat, lng)`, latitude first |
| tuple with altitude | `(28.6139, 77.2090, 216.0)` | items after the second are ignored |
| mapping | `{"lat": 28.6139, "lng": 77.2090}` | latitude key `lat` or `latitude`; longitude key `lng`, `lon` or `longitude`, all lower case |
| GeoJSON Point | `{"type": "Point", "coordinates": [77.2090, 28.6139]}` | `[lng, lat]`, longitude first, as GeoJSON requires |
| Point Feature | `geo.get_features()[0]` | any Feature whose geometry is a Point, such as a row loaded from a CSV |

Numbers can be strings, so a `csv.DictReader` row with `lat` and `lng` columns works as a point.
The forms mix freely:

```python
print(geo.bearing({"lat": 28.6139, "lng": 77.2090}, mumbai_feature))  # South-Southwest
print(geo.midpoint(delhi_feature, [19.0760, 72.8777]))  # (23.86007, 74.963456)
print(geo.geohash({"type": "Point", "coordinates": [77.2090, 28.6139]}))  # ttnfucjbh
```

A latitude outside -90 to 90, a longitude outside -180 to 180, `NaN`, or a value that is not a
point raises `InvalidCoordinateError`, a subclass of `ValueError`:

```python
try:
    geo.distance((128.6139, 77.2090), mumbai)
except InvalidCoordinateError as exc:
    error = exc

print(error.lat, error.lng)  # 128.6139 77.209
print(isinstance(error, ValueError))  # True
```

The message says what was received and how to fix it:

```text
❌  QOREgeo: Invalid Coordinate
────────────────────────────────────────────
Received: lat=128.6139, lng=77.209

Valid ranges are:
    latitude  : -90.0  to  90.0
    longitude : -180.0 to 180.0

Fix it:
    Make sure you're passing (latitude, longitude), not (longitude, latitude).
    Example: (28.6139, 77.2090)  ← Delhi
```

Swapped coordinates are only caught when they fall out of range. For a point in Delhi, the
swapped pair is still valid, because 77.2 is a possible latitude. It lands in the Barents Sea,
north of Norway:

```python
print(geo.distance((77.2090, 28.6139), mumbai))  # 6900.7153  not 1148.0965
```

If a distance looks thousands of kilometers too large, check the order first.

## Measure a distance

```python
print(geo.distance(delhi, mumbai))  # 1148.0965
print(geo.distance(delhi, mumbai, unit="m"))  # 1148096.4589
print(geo.distance(delhi, mumbai, unit="miles"))  # 713.3941
print(geo.distance(delhi, mumbai, unit="nmi"))  # 619.9225
print(geo.distance(delhi, mumbai, unit="Kilometres"))  # 1148.0965
```

`distance()` returns the great-circle distance as a `float`, rounded to 4 decimals of the unit:
0.1 m in `km`, 0.1 mm in `m`.

Distance units are the same for every method that takes one (`distance()`, `destination()`,
`buffer()`, `length()`, `distance_to_line()`, and the query methods such as `nearest()` and
`filter_by_radius()`):

| Unit | Also accepted | One unit is |
|---|---|---|
| `km` (default) | `kilometer`, `kilometers`, `kilometre`, `kilometres` | 1,000 m |
| `m` | `meter`, `meters`, `metre`, `metres` | 1 m |
| `miles` or `mi` | `mile` | 1,609.344 m |
| `ft` | `foot`, `feet` | 0.3048 m |
| `yd` | `yard`, `yards` | 0.9144 m |
| `nmi` | `nautical_mile`, `nautical_miles` | 1,852 m |

Case and surrounding spaces are ignored. Any other name raises `InvalidUnitError`, whose
`valid` attribute lists the names above.

Loaded features are points too, so measuring your data needs no conversion. The nearest
hospital to central Delhi, by brute force:

```python
hospitals = GeoEngine().load("hospitals.csv")
by_distance = sorted((geo.distance(delhi, f), f["properties"]["name"]) for f in hospitals)
print(by_distance[0])  # (3.0382, 'Lodhi Gardens Clinic')
print(by_distance[1])  # (3.9711, 'Karol Bagh Medical Centre')
```

`hospitals.nearest(delhi)` and `hospitals.k_nearest(delhi, k=2)` do the same job with fewer
lines; see [Queries](queries.md).

### Haversine or Vincenty

```python
print(geo.distance(delhi, mumbai, method="vincenty"))  # 1144.5264


def sphere_minus_ellipsoid(a, b):
    """Haversine minus Vincenty, in meters and in percent."""
    sphere = geo.distance(a, b, unit="m")
    ellipsoid = geo.distance(a, b, unit="m", method="vincenty")
    return round(sphere - ellipsoid, 1), round((sphere - ellipsoid) / ellipsoid * 100, 3)


connaught_place = (28.6315, 77.2167)
oslo, helsinki = (59.9139, 10.7522), (60.1699, 24.9384)

print(sphere_minus_ellipsoid(connaught_place, india_gate))  # (4.6, 0.19)
print(sphere_minus_ellipsoid(delhi, mumbai))  # (3570.0, 0.312)
print(sphere_minus_ellipsoid(oslo, helsinki))  # (-2864.8, -0.363)
print(sphere_minus_ellipsoid((0, 0), (1, 0)))  # (620.7, 0.561)  1 degree north from the equator
print(sphere_minus_ellipsoid((0, 0), (0, 1)))  # (-124.4, -0.112)  1 degree east along the equator
```

`method` picks the Earth model:

| `method` | Model | How it works |
|---|---|---|
| `"haversine"` (default) | a sphere with the mean Earth radius, 6371.0088 km | one closed formula |
| `"vincenty"` | the WGS84 ellipsoid, the model GPS uses | Vincenty's inverse formula, an iteration of up to 200 steps |

The name ignores case and surrounding spaces. Anything else raises `InvalidArgumentError`.

The Earth is flatter at the poles than a sphere, so the sphere gets some directions too long
and others too short. In the examples above it is 0.561 % too long for a north-south step at the
equator, 0.112 % too short for an east-west step there, and 0.363 % too short between Oslo and
Helsinki, at 60° north. The error is a share of the distance: 0.2 % is 5 m across central Delhi,
and 0.3 % is 3.6 km from Delhi to Mumbai.

| Use the default `haversine` when | Use `vincenty` when |
|---|---|
| you rank or filter by distance (nearest store, everything within 5 km) | you report the number as a measurement: a survey, a per-km charge, a legal distance |
| the distances are short and a few meters do not matter | you compare with GPS devices, GIS software or other libraries that use WGS84 |
| you compute millions of distances | the distances are long, where 0.5 % is kilometers |

In [Experiments](experiments.md), Vincenty agreed with GeographicLib, a reference geodesic
library, to under a millimeter wherever it converged, and cost about three times as much per
call as haversine.

### Nearly antipodal points

Vincenty's iteration does not converge for some pairs of points on nearly opposite sides of the
Earth. qoregeo then returns the haversine distance and warns:

```python
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    far = geo.distance((0, 0), (0.5, 179.7), method="vincenty")

print(far)  # 19950.2773
print(far == geo.distance((0, 0), (0.5, 179.7)))  # True  the haversine value
print(caught[0].category.__name__)  # UserWarning
print(caught[0].message)  # QOREgeo: Vincenty's formula did not converge for these nearly antipodal points; the spherical (haversine) distance was used instead.
```

To get an exception instead, call `warnings.simplefilter("error")` inside the
`warnings.catch_warnings()` block.

## Get a compass bearing

```python
print(geo.bearing(delhi, mumbai))  # South-Southwest
print(geo.bearing(delhi, mumbai, as_degrees=True))  # 203.47
print(geo.bearing(mumbai, delhi, as_degrees=True))  # 21.71  not 23.47
print(geo.bearing((0, 0), (0, 1)), geo.bearing((0, 0), (1, 0)))  # East North
print(geo.bearing(delhi, delhi), geo.bearing(delhi, delhi, as_degrees=True))  # North 0.0
```

`bearing()` returns the initial great-circle bearing from the first point to the second: the
direction you set off in. Degrees run clockwise from north (0) through east (90). With
`as_degrees=True` you get a `float` rounded to 2 decimals, from 0 up to (not including) 360.
Without it you get one of 16 compass names.

On a great circle the direction changes along the way, so the bearing back is not the bearing
out plus 180°. Delhi to Mumbai starts at 203.47°; Mumbai to Delhi starts at 21.71°, not 23.47°.

Each compass name covers 22.5°, centered on its angle. An angle exactly on a boundary goes to
the next name clockwise: 11.25° is North-Northeast, 11.24° is North.

| Name | Center | Name | Center |
|---|---|---|---|
| North | 0° | South | 180° |
| North-Northeast | 22.5° | South-Southwest | 202.5° |
| Northeast | 45° | Southwest | 225° |
| East-Northeast | 67.5° | West-Southwest | 247.5° |
| East | 90° | West | 270° |
| East-Southeast | 112.5° | West-Northwest | 292.5° |
| Southeast | 135° | Northwest | 315° |
| South-Southeast | 157.5° | North-Northwest | 337.5° |

## Find a destination point

```python
east = geo.destination(delhi, 10, 90)
print(east)  # (28.613861, 77.311444)
print(geo.distance(delhi, east), geo.bearing(delhi, east, as_degrees=True))  # 10.0 90.0
print(geo.destination(delhi, 5, 0, unit="miles"))  # (28.686266, 77.209)
print(geo.destination(delhi, 10, 450))  # (28.613861, 77.311444)  450 degrees is 90
print(geo.destination(delhi, -10, 90))  # (28.613861, 77.106556)  a negative distance goes the other way
```

`destination(point, distance, bearing, unit="km")` answers "where do I end up if I travel this
far in this direction?" It follows a great circle on the sphere and returns `(lat, lng)` rounded
to 6 decimals (about 0.1 m). `distance` and `bearing` must be finite numbers; otherwise you get
`InvalidArgumentError`. Any angle works, and longitudes wrap into -180 to 180:

```python
print(geo.destination((-17.0, 179.9), 50, 90))  # (-16.999461, -179.629795)  across 180 degrees
print(geo.destination((89.9, 0), 50, 0))  # (89.65034, 180.0)  over the North Pole
```

Because it works on a sphere, the point is close to, but not exactly, where a WGS84 calculation
puts it. Measured on the ellipsoid:

```python
north = geo.destination(delhi, 10, 0)
print(geo.distance(delhi, east, method="vincenty"))  # 10.0189  19 m too far
print(geo.distance(delhi, north, method="vincenty"))  # 9.9672  33 m short
```

That is the same fraction of a percent as the haversine distance error. `distance()`,
`destination()` and `buffer()` share one sphere, so they agree with each other:
`geo.distance(delhi, east)` is 10.0.

## Find the midpoint

```python
mid = geo.midpoint(delhi, mumbai)
print(mid)  # (23.86007, 74.963456)
print(geo.distance(delhi, mid), geo.distance(mid, mumbai))  # 574.0483 574.0482

london, new_york = (51.5074, -0.1278), (40.7128, -74.0060)
print(geo.midpoint(london, new_york))  # (52.36844, -41.290307)  north of both cities

suva, tubou = (-18.1416, 178.4419), (-18.2333, -178.8000)
print(geo.midpoint(suva, tubou))  # (-18.192372, 179.820587)  in Fiji, not in the Atlantic
```

`midpoint()` returns the point halfway along the great circle, rounded to 6 decimals. It is not
the average of the coordinates. The shortest path from London to New York bends north, so its
midpoint lies north of both cities. Averaging Suva and Tubou, which sit on either side of 180°,
would give a longitude near 0, on the other side of the world; `midpoint()` stays in Fiji.

For the center of a whole dataset, call `centroid()` on a loaded engine; see the
[API reference](api-reference.md).

## Draw a circle with buffer

```python
zone = geo.buffer(india_gate, 5)
print(zone["type"])  # Polygon
print(len(zone["coordinates"][0]))  # 65  64 vertices plus the closing one
print([round(x, 6) for x in zone["coordinates"][0][0]])  # [77.2295, 28.657866]  due north, [lng, lat]
print(zone["_center"], zone["_radius_km"])  # (28.6129, 77.2295) 5.0
print({geo.distance(india_gate, (lat, lng)) for lng, lat in zone["coordinates"][0]})  # {5.0}
print(geo.buffer(india_gate, 500, unit="m")["_radius_km"])  # 0.5
```

`buffer(center, radius, unit="km", num_points=64)` returns a GeoJSON Polygon that approximates a
circle: every vertex lies exactly `radius` from the center along a great circle. The ring is
closed (the first position is repeated at the end), runs counter-clockwise as RFC 7946 asks,
and starts due north of the center. Positions are `[lng, lat]`.

| Parameter | Default | What it does |
|---|---|---|
| `center` | required | the center, as any [point form](#points-you-can-pass) |
| `radius` | required | a finite number greater than 0, at most 20015 km (half the Earth's circumference) |
| `unit` | `"km"` | the unit of `radius` |
| `num_points` | `64` | how many vertices the ring has, an integer of 3 or more |

The polygon also carries two extra keys: `_center`, the `(lat, lng)` center, and `_radius_km`.
`point_in_polygon()`, `filter_by_polygon()`, `intersects()` and `area()` read them and treat the
buffer as an exact circle on the sphere. A point is inside when its haversine distance to the
center is at most the radius, whatever `num_points` is.

### Choose num_points

```python
def drawn_area(num_points):
    """Area of the polygon alone, without the circle it remembers."""
    ring = geo.buffer(india_gate, 5, num_points=num_points)["coordinates"]
    return geo.area({"type": "Polygon", "coordinates": ring})


print({n: drawn_area(n) for n in (8, 16, 64, 256)})  # {8: 70.7107, 16: 76.5367, 64: 78.4137, 256: 78.5319}
print(geo.area(zone))  # 78.5398  the exact circle
print(round(math.pi * 5 ** 2, 4))  # 78.5398
```

The vertices are on the circle, so the polygon sits inside it and its area is a little smaller.
`num_points` changes how the circle looks on a map and how a copy without the extra keys
behaves. It does not change how qoregeo tests or measures the buffer itself. The default of 64
gives a polygon within 0.2 % of the circle's area; use more for large circles drawn on a map,
fewer for small files.

### Save and reload a buffer

```python
GeoEngine().load_data(zone).save("zone.geojson")
saved = GeoEngine().load("zone.geojson").get_features()[0]["geometry"]
print(sorted(saved))  # ['_center', '_radius_km', 'coordinates', 'type']
print(geo.area(saved))  # 78.5398
```

The extra keys survive `save()`, `load()` and `json`, so a reloaded buffer is still an exact
circle. Tools that do not know the keys see a plain polygon.

### Buffers across 180° and at the poles

```python
fiji_zone = geo.buffer((-17.0, 179.9), 50)
lngs = [lng for lng, lat in fiji_zone["coordinates"][0]]
print(round(min(lngs), 4), round(max(lngs), 4))  # 179.4298 180.3702
print(geo.point_in_polygon((-17.0, -179.8), fiji_zone))  # True
```

Near the antimeridian the ring keeps its longitudes continuous, so some go past 180. That keeps
it one shape on a map, and qoregeo's point-in-polygon and intersection tests handle it. Some
other tools expect every longitude between -180 and 180. A buffer centered on a pole has its vertices on one circle of
latitude.

## Test a point against a polygon

```python
zones = GeoEngine().load("zones.geojson")
central, south, west = zones.get_features()
rashtrapati_bhavan = (28.6143, 77.1994)

print(central["properties"]["name"], central["properties"]["note"])  # Central excludes President's Estate
print(geo.point_in_polygon(india_gate, central))  # True
print(geo.point_in_polygon(rashtrapati_bhavan, central))  # False  inside the hole

outer_only = {"type": "Polygon", "coordinates": central["geometry"]["coordinates"][:1]}
print(geo.point_in_polygon(rashtrapati_bhavan, outer_only))  # True
```

`point_in_polygon(point, polygon)` returns `True` when the point is inside. Holes count: the
Central zone in [zones.geojson](data/zones.geojson) has a hole for the President's Estate, so
Rashtrapati Bhavan is outside it. The `polygon` can be any of these:

| `polygon` | How it is tested |
|---|---|
| a `buffer()` result | exact circle: haversine distance to the center is at most the radius |
| a GeoJSON Polygon | inside the outer ring and outside every hole |
| a GeoJSON MultiPolygon | inside any of its polygons |
| a Feature with one of those geometries | its geometry |
| a FeatureCollection | inside any of its Polygon or MultiPolygon features; other geometry types are skipped |

```python
south_and_west = {
    "type": "MultiPolygon",
    "coordinates": [south["geometry"]["coordinates"], west["geometry"]["coordinates"]],
}
lajpat_nagar = (28.5677, 77.2433)
print(geo.point_in_polygon(lajpat_nagar, south_and_west))  # True
print(geo.point_in_polygon(india_gate, south_and_west))  # False
print(geo.point_in_polygon(india_gate, zones.get_geojson()))  # True  any zone in the collection
print(geo.point_in_polygon(india_gate, geo.buffer(delhi, 3)))  # True  2.0042 km from the center


def zone_of(point):
    return next((f["properties"]["name"] for f in zones if geo.point_in_polygon(point, f)), None)


print(zone_of(lajpat_nagar), zone_of(india_gate), zone_of(rashtrapati_bhavan))  # South Central None
```

Polygon edges are straight lines in longitude and latitude, as RFC 7946 defines them. For a polygon
that crosses 180°, write its longitudes continuously (177 to 182) or split it into a
MultiPolygon at 180°. Anything that is not a polygon, such as a LineString, raises
`InvalidBufferError`.

To test many points against one polygon, use `filter_by_polygon()`, which prepares the polygon
once:

```python
customers = GeoEngine().load("customers.csv")
print(customers.filter_by_polygon(central).count())  # 13
```

## Test whether two shapes intersect

```python
route = GeoEngine().load("route.geojson").get_features()[0]
print([f["properties"]["name"] for f in zones if geo.intersects(route, f)])  # ['Central']
print(geo.intersects(central, south))  # False
print(geo.intersects(geo.buffer(delhi, 3), south))  # True
print(geo.intersects({"type": "Point", "coordinates": [77.2295, 28.6129]}, central))  # True
print(geo.intersects(route, zones.get_geojson()))  # True
```

`intersects(geometry_a, geometry_b)` returns `True` when two shapes share at least one point.
Both arguments can be any GeoJSON geometry (Point, MultiPoint, LineString, MultiLineString,
Polygon, MultiPolygon, GeometryCollection), a Feature, a FeatureCollection or a `buffer()`
result. A point is GeoJSON here, not a `(lat, lng)` tuple: a tuple raises
`InvalidArgumentError`.

Touching counts, and so does meeting at 180° from either side:

```python
left = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
right = {"type": "Polygon", "coordinates": [[[1, 0], [2, 0], [2, 1], [1, 1], [1, 0]]]}
print(geo.intersects(left, right))  # True  they share an edge

west_of_180 = {"type": "Polygon", "coordinates": [[[179, -18], [180, -18], [180, -17], [179, -17], [179, -18]]]}
east_of_180 = {"type": "Polygon", "coordinates": [[[-180, -18], [-179, -18], [-179, -17], [-180, -17], [-180, -18]]]}
print(geo.intersects(west_of_180, east_of_180))  # True
```

Buffers are tested as exact circles on the sphere, against other circles and against every
point, edge and polygon of the other shape. To keep the features of a dataset that meet a shape,
use `filter_intersects()`; see [Queries](queries.md).

## Measure an area

```python
print({f["properties"]["name"]: geo.area(f) for f in zones})  # {'Central': 31.5184, 'South': 33.6642, 'West': 18.2924}
print(geo.area(south, unit="ha"))  # 3366.4173
print(geo.area(south, unit="acres"))  # 8318.5984
print(geo.area(south, unit="sq_mi"))  # 12.9978
```

`area(polygon, unit="km2")` returns the area on the sphere, rounded to 4 decimals of the unit.
It takes the same shapes as `point_in_polygon()`: a Polygon, a MultiPolygon, a Feature, a
FeatureCollection or a `buffer()` result.

```python
hole = {"type": "Polygon", "coordinates": central["geometry"]["coordinates"][1:]}
print(geo.area(outer_only))  # 33.2117  Central without its hole
print(geo.area(hole))  # 1.6933  the President's Estate
print(geo.area(south_and_west))  # 51.9566  a MultiPolygon adds its parts
print(geo.area(zones.get_geojson()))  # 83.4749  a FeatureCollection adds its polygons
print(geo.area(geo.buffer(delhi, 1)))  # 3.1416  pi square kilometers
```

How the parts combine:

- Holes are subtracted: 33.2117 minus 1.6933 is the 31.5184 of Central.
- The parts of a MultiPolygon and the polygons of a FeatureCollection are added. Overlapping
  parts are counted twice.
- The direction of a ring does not matter, and a ring that is not closed is closed for you.
- A `buffer()` result is measured as the exact spherical cap, not as its polygon (see
  [Choose num_points](#choose-num_points)).

| Unit | Also accepted | One unit is |
|---|---|---|
| `km2` (default) | `sqkm`, `sq_km` | 1,000,000 m² |
| `m2` | `sqm`, `sq_m` | 1 m² |
| `ha` | `hectare`, `hectares` | 10,000 m² |
| `acres` | `acre` | 4,046.8564224 m² |
| `mi2` | `sqmi`, `sq_mi` | 2,589,988.110336 m² |
| `ft2` | `sqft`, `sq_ft` | 0.09290304 m² |

Case and surrounding spaces are ignored. A distance unit such as `"km"` raises
`InvalidUnitError`.

The area uses the method of Chamberlain and Duquette (JPL, 2007), as Turf.js does, on a sphere
with the mean Earth radius. It does not use the WGS84 ellipsoid, so the result is a little off
from an ellipsoidal area, and how much depends mostly on latitude, not on size.
[Experiments](experiments.md) has the measured error by latitude.

Positions must be `[lng, lat]`. A position whose latitude is outside -90 to 90 raises
`InvalidBufferError`, which usually means the pairs are swapped:

```python
try:
    geo.area({"type": "Polygon", "coordinates": [[[28.6, 77.2], [28.7, 95.2], [28.7, 77.3], [28.6, 77.2]]]})
except InvalidBufferError as exc:
    reason = exc.reason

print(reason)  # position [28.7, 95.2] is out of range. Positions are [lng, lat] with latitude -90..90 and longitude -180..180 (are lng/lat swapped?)
```

## Measure the length of a path

```python
print(route["properties"]["name"])  # Delhi to Mumbai freight route
print(geo.length(route))  # 1266.2342
print(geo.length(route, method="vincenty"))  # 1263.1735
print(geo.length(route, unit="miles"))  # 786.8015
print(geo.length([delhi, (26.9124, 75.7873), mumbai]))  # 1156.0342  Delhi, Jaipur, Mumbai
```

`length(line, unit="km", method="haversine")` adds up the great-circle distances between
consecutive points. `unit` and `method` work as in [`distance()`](#measure-a-distance). The
`line` can be:

| `line` | Example |
|---|---|
| a list of points, in any [point form](#points-you-can-pass) | `[(28.6139, 77.2090), (19.0760, 72.8777)]` |
| a GeoJSON LineString, positions `[lng, lat]` | `{"type": "LineString", "coordinates": [[77.209, 28.6139], ...]}` |
| a GeoJSON MultiLineString | its parts are measured and added; the gaps between parts are not |
| a Feature with one of those geometries | the [route.geojson](data/route.geojson) Feature above |

A list with fewer than two points has length `0.0`. A Polygon raises `InvalidArgumentError`.

A list of loaded Point features is a path too. [track.csv](data/track.csv) is a delivery van's
GPS track, one fix a minute:

```python
track = GeoEngine().load("track.csv")
fixes = track.get_features()
print(track.count())  # 30
print(geo.length(fixes, unit="m"))  # 12159.9079

steps = [geo.distance(fixes[i - 1], fixes[i], unit="m") for i in range(1, len(fixes))]
print(round(max(steps)), steps.count(0.0))  # 1101 2  longest minute in meters, minutes stopped
```

Parts of a MultiLineString are added without the gap between them:

```python
legs = {
    "type": "MultiLineString",
    "coordinates": [
        [[77.2090, 28.6139], [75.7873, 26.9124]],
        [[72.5714, 23.0225], [72.8777, 19.0760]],
    ],
}
print(geo.length(legs))  # 675.271  Delhi to Jaipur plus Ahmedabad to Mumbai
```

`length()` does not take polygons. For a perimeter, measure the ring as a LineString:

```python
south_ring = south["geometry"]["coordinates"][0]
print(geo.length({"type": "LineString", "coordinates": south_ring}))  # 23.3289
```

## Measure the distance to a line

```python
pune = (18.5204, 73.8567)
lucknow = (26.8467, 80.9462)
print(geo.distance_to_line(pune, route))  # 120.1525
print(geo.distance_to_line(lucknow, route))  # 416.9928
print(geo.distance_to_line(pune, route, unit="miles"))  # 74.6593
```

`distance_to_line(point, line, unit="km")` returns the shortest distance from the point to any
segment of the path, rounded to 4 decimals. The `line` takes the same forms as in
[`length()`](#measure-the-length-of-a-path). Each segment is a great-circle arc. When the
closest point falls between the two ends of a segment you get the perpendicular (cross-track)
distance; otherwise you get the distance to the nearer end:

```python
delhi_jaipur = [delhi, (26.9124, 75.7873)]
print(geo.distance_to_line((27.5, 76.6), delhi_jaipur))  # 26.0302  beside the segment
print(geo.distance_to_line((30.0, 78.0), delhi_jaipur))  # 172.1548  past the Delhi end
print(geo.distance((30.0, 78.0), delhi))  # 172.1548
```

A line with no points raises `InvalidArgumentError`; a single point gives the distance to it.

To keep the features within some distance of a path, use `filter_by_line()`. It adds a
`_distance` property and sorts nearest first:

```python
corridor = cities.filter_by_line(route, 150)
print([f["properties"]["name"] for f in corridor])  # ['Delhi', 'Mumbai', 'Ahmedabad', 'Surat', 'Jaipur', 'Pune']
print(corridor.get_features()[-1]["properties"]["_distance"])  # 120.1525
```

## Encode points as geohashes

```python
print(geo.geohash(delhi))  # ttnfucjbh
print(geo.geohash(delhi, precision=5))  # ttnfu
print(geo.geohash_decode("ttnfucjbh"))  # (28.613913, 77.209017)
print(geo.geohash_decode("ttnfu"))  # (28.630371, 77.189941)
print(geo.distance(delhi, geo.geohash_decode("ttnfu"), unit="m"))  # 2610.5638
print(geo.geohash_decode(" TTNFU "))  # (28.630371, 77.189941)
```

A geohash names a cell of a fixed grid in a short string. Each extra character divides the cell
into 32, and points in the same cell share the same string. `geohash(point, precision=9)`
returns the cell that holds the point; `precision` is an integer from 1 to 12.
`geohash_decode()` returns the center of the cell, rounded to 6 decimals, not the point you
encoded. At precision 5 the center is 2.6 km from Delhi; at the default precision 9 it is about
2 m away. Decoding ignores case and surrounding spaces. A character outside
`0123456789bcdefghjkmnpqrstuvwxyz` raises `InvalidArgumentError`.

A short prefix groups points by area:

```python
stores = GeoEngine().load("stores.csv")
print(Counter(geo.geohash(f, precision=3) for f in stores))  # Counter({'ttn': 11, 'te7': 10, 'tdr': 9})
print(Counter(geo.geohash(f, precision=4) for f in stores if f["properties"]["city"] == "Mumbai"))  # Counter({'te7u': 7, 'te7g': 2, 'te7v': 1})
```

At precision 3 each city falls in one cell. At precision 4 the 10 Mumbai stores fall in three
cells, because the grid has fixed edges: two points a few meters apart on either side of an
edge get different strings. To find neighbors, compare distances, not prefixes.

Cell size by precision, at the equator. Cells keep the same height everywhere and get narrower
toward the poles (at Delhi's latitude, about 12 % narrower).

| Precision | Width | Height |
|---|---|---|
| 1 | 5,004 km | 5,004 km |
| 2 | 1,251 km | 625 km |
| 3 | 156 km | 156 km |
| 4 | 39.1 km | 19.5 km |
| 5 | 4.89 km | 4.89 km |
| 6 | 1.22 km | 611 m |
| 7 | 153 m | 153 m |
| 8 | 38.2 m | 19.1 m |
| 9 | 4.77 m | 4.77 m |
| 10 | 1.19 m | 59.7 cm |
| 11 | 14.9 cm | 14.9 cm |
| 12 | 3.7 cm | 1.9 cm |

## Accuracy

| Method | Model | What to expect |
|---|---|---|
| `distance()`, `length()` | sphere, radius 6371.0088 km | off from WGS84 by up to about 0.56 %, depending on direction and latitude ([examples](#haversine-or-vincenty)) |
| `distance()`, `length()` with `method="vincenty"` | WGS84 ellipsoid | under 1 mm from GeographicLib where it converges; [falls back](#nearly-antipodal-points) to haversine where it does not |
| `bearing()` | sphere | the initial great-circle bearing on the sphere, not the WGS84 azimuth |
| `destination()`, `midpoint()` | sphere | the same sphere as `distance()`, so the results agree with it |
| `buffer()` | sphere | vertices exactly `radius` from the center on the sphere |
| `distance_to_line()` | sphere | segments are great-circle arcs |
| `area()` | sphere | off from WGS84 by less than 1 % in the experiments, by an amount that depends on latitude |
| `point_in_polygon()`, `intersects()` | straight edges in lng/lat | exact for edges as GeoJSON defines them; buffers are exact circles on the sphere |
| `geohash()`, `geohash_decode()` | lng/lat grid | exact cell; decoding returns the center |

`point_in_polygon()` and `intersects()` read a polygon edge as a straight line in longitude and
latitude, while `distance_to_line()` and `length()` read a segment as a great-circle arc. For
short segments the two paths are almost the same. For a long east-west segment far from the
equator they part, because the great circle bends toward the pole:

```python
a, b = (60.0, 10.0), (60.0, 12.0)
print(geo.distance(a, b))  # 111.1908
print(geo.midpoint(a, b))  # (60.003779, 11.0)
print(geo.distance((60.0, 11.0), geo.midpoint(a, b), unit="m"))  # 420.2062
```

Along this 111 km edge at 60° north, the great circle passes 420 m north of the straight line at
the middle. If that matters, add vertices to long polygon edges.

The input is often a larger source of error than the model: coordinates written with four
decimals have a step of 0.0001°, about 11 m of latitude.

[Experiments](experiments.md) measures these methods against GeographicLib and other libraries
on many random points and shapes, with the scripts to rerun them.

## From the command line and AI tools

`qoregeo distance` prints the distance and the bearing between two `LAT,LNG` points:

```bash
qoregeo distance 28.6139,77.2090 19.0760,72.8777
qoregeo distance 28.6139,77.2090 19.0760,72.8777 --method vincenty --unit miles
qoregeo distance -18.1416,178.4419 -18.2333,-178.8 --json
```

```text
1148.0965 km  (bearing 203.47°, South-Southwest)
711.1758 miles  (bearing 203.47°, South-Southwest)
{
  "distance": 291.5408,
  "unit": "km",
  "method": "haversine",
  "bearing": 92.43,
  "compass": "East"
}
```

The other measurements are available as AI tools. `qoregeo call` runs one with JSON arguments,
the same way an MCP client does. Points are `{"lat": …, "lng": …}` objects:

```bash
qoregeo call destination '{"start": {"lat": 28.6139, "lng": 77.2090}, "distance": 10, "bearing": 90}'
qoregeo call measure '{"geometry": {"type": "LineString", "coordinates": [[77.2090, 28.6139], [75.7873, 26.9124], [72.8777, 19.0760]]}}'
qoregeo call geohash '{"point": {"lat": 28.6139, "lng": 77.2090}, "precision": 7}'
```

```text
{
  "lat": 28.613861,
  "lng": 77.311444
}
{
  "type": "LineString",
  "length": 1156.0342,
  "length_unit": "km"
}
{
  "geohash": "ttnfucj",
  "center": {
    "lat": 28.614578,
    "lng": 77.208481
  }
}
```

| Python method | Tool | Arguments |
|---|---|---|
| `distance()` | `distance` | `from`, `to`, `unit`, `method` |
| `bearing()` | `bearing` | `from`, `to` |
| `destination()` | `destination` | `start`, `distance`, `bearing`, `unit` |
| `midpoint()` | `midpoint` | `a`, `b` |
| `buffer()` | `buffer` | `center`, `radius`, `unit`, `num_points` |
| `point_in_polygon()` | `contains` | `point`, `area` |
| `intersects()` | `intersects` | `a`, `b` |
| `area()`, `length()` | `measure` | `geometry`, `area_unit`, `length_unit` |
| `geohash()` | `geohash` | `point`, `precision` |

The tools take the main unit names only (`km`, `m`, `miles`, `ft`, `yd`, `nmi`, and `km2`, `m2`,
`ha`, `acres`, `mi2`, `ft2`), not the aliases. [Command line](cli.md) lists every command and
flag, and [AI integration](ai-integration.md) covers the MCP server and the tool schemas.

## Errors

All errors on this page subclass `ValueError` and `qoregeo.QOREgeoError`, and each message
says what was wrong and shows a fix.

```python
problems = []
for bad_call in (
    lambda: geo.distance(delhi, mumbai, unit="furlong"),
    lambda: geo.distance(delhi, mumbai, method="great-circle"),
    lambda: geo.buffer(delhi, 0),
    lambda: geo.area(route),
    lambda: geo.geohash(delhi, precision=13),
):
    try:
        bad_call()
    except ValueError as exc:
        problems.append(type(exc).__name__)

print(problems)  # ['InvalidUnitError', 'InvalidArgumentError', 'InvalidRadiusError', 'InvalidBufferError', 'InvalidArgumentError']
```

| Error | Raised when | Attributes |
|---|---|---|
| `InvalidCoordinateError` | a point is out of range, not a number or not a point | `lat`, `lng`, `context` |
| `InvalidUnitError` | a unit name is unknown, or an area unit is given for a distance (or the reverse) | `unit`, `valid` |
| `InvalidArgumentError` | an unknown `method`; a `destination()` distance or bearing that is not a finite number; `num_points` below 3; a `buffer()` radius over 20015 km; a `precision` outside 1 to 12; an empty geohash or one with a bad character; a `line` that is not a list of points or a LineString; a `distance_to_line()` line with no points; an `intersects()` argument that is not GeoJSON | `name`, `value`, `expected` |
| `InvalidRadiusError` | a `buffer()` radius that is zero, negative or not a number | `radius` |
| `InvalidBufferError` | a `point_in_polygon()` or `area()` shape that is not a polygon, has a ring of fewer than 3 positions, or has a position out of range | `reason` |

For example, an unknown unit:

```text
❌  QOREgeo: Invalid Unit 'furlong'
────────────────────────────────────────────
'furlong' is not a recognised unit here.

Supported units: 'km', 'm', 'miles', 'mi', 'ft', 'yd', 'nmi'

Fix it:
    geo.distance(a, b, unit='km')
```

[Troubleshooting](troubleshooting.md) covers the errors from loading and querying data.

## See also

- [Getting started](getting-started.md): install qoregeo and make a first map.
- [Load and save data](data.md): get points and shapes into an engine.
- [Queries](queries.md): nearest features, radius, polygon and line filters.
- [Maps](maps.md) and [images](images.md): draw buffers, zones and routes.
- [Cookbook](cookbook.md): complete recipes that combine these methods.
- [API reference](api-reference.md): every `GeoEngine` method and parameter.
- [Experiments](experiments.md): measured accuracy and speed.
