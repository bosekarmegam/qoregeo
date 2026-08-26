<div align="center">

[← Loading Data](loading-data.md) · **Geometry** · [Querying →](querying.md)

</div>

---

# Geometry

Distance, direction, buffers, area, hulls and overlap, computed on the sphere.

---

## Coordinate order

Two conventions, and the difference matters:

| Where | Order | Example |
|---|---|---|
| Arguments you pass in | `(latitude, longitude)` | `(28.6139, 77.2090)` |
| Inside a GeoJSON geometry | `[longitude, latitude]` | `[77.2090, 28.6139]` |

QOREgeo converts for you. You only notice when reading a raw geometry dict:

```python
lng, lat = feature["geometry"]["coordinates"]     # GeoJSON order
```

Passing them the wrong way round usually raises `InvalidCoordinateError`,
because a longitude above 90 is not a valid latitude. When both values happen
to be under 90 it cannot be detected. `validate()` reports what it can.

---

## Distance

```python
delhi  = (28.6139, 77.2090)
mumbai = (19.0760, 72.8777)

geo.distance(delhi, mumbai)                       # 1148.0965  km (default)
geo.distance(delhi, mumbai, unit="miles")         # 713.3938
geo.distance(delhi, mumbai, unit="m")             # 1148096.4589
geo.distance(delhi, mumbai, unit="nm")            # 619.9227   nautical miles
```

**Units:** `km` · `miles` (`mi`) · `m` · `ft` · `nm` (`nmi`)

### Haversine or Vincenty

```python
geo.distance(delhi, mumbai)                        # 1148.0965  Haversine (sphere)
geo.distance(delhi, mumbai, method="vincenty")     # 1144.5264  Vincenty (WGS84)
```

| Method | Model | Error | Speed | Use when |
|---|---|---|---|---|
| `haversine` *(default)* | sphere, R = 6371.0088 km | up to ~0.5 % | fast | almost always |
| `vincenty` | WGS84 ellipsoid | sub-millimetre | ~10× slower | surveying, aviation, legal boundaries |

The Earth is 21 km wider at the equator than pole to pole, which is where the
0.5 % comes from. For "which branch is nearest", it is noise. For a cadastral
boundary, it is not.

Vincenty does not converge for near-antipodal points; QOREgeo falls back to
Haversine there rather than returning nonsense.

---

## Direction

```python
geo.bearing(delhi, mumbai)                      # 'South-Southwest'
geo.bearing(delhi, mumbai, as_degrees=True)     # 203.47
```

Sixteen compass points, or degrees clockwise from north.

This is the **initial** bearing. On a great circle the heading changes as you
travel. Flying "east" from London to Tokyo means starting north-east. For a
constant heading you want a rhumb line, which QOREgeo does not currently
provide.

### Projecting a position

```python
geo.destination(delhi, bearing=90, distance=100)          # (28.61, 78.2334)
geo.destination(delhi, bearing=45, distance=62.1, unit="miles")
```

The inverse of `bearing()`: where do you end up travelling that far on that
heading? Useful for search cones, evacuation edges and synthetic test data.

### Points along a path

```python
geo.midpoint(delhi, mumbai)              # (23.8601, 74.9635)
geo.interpolate(delhi, mumbai, 0.25)     # a quarter of the way
geo.interpolate(delhi, mumbai, 0.0)      # exactly delhi
```

Spherical interpolation, so results sit on the great circle rather than cutting
through the planet.

---

## Buffers and geofences

### Around a point

```python
zone = geo.buffer(delhi, radius=10, unit="km")
```

Returns a GeoJSON `Polygon` with `_center` and `_radius_km` attached. Every
vertex is exactly 10 km from the centre, measured on the sphere, which is why
a buffer near the poles looks stretched on a Mercator map and is nonetheless
correct.

```python
geo.buffer(delhi, radius=10, num_points=256)     # smoother ring
```

`num_points` defaults to 64. Raise it for large radii or when you will compute
the area; lower it when generating thousands of buffers.

### Around a line: corridors

```python
corridor = geo.buffer_line([delhi, mumbai], radius=20, unit="km")
```

Everything within 20 km of the route. The query behind "which villages does
this pipeline pass through", "who lives near this proposed road", "which sensors
are within range of the flight path".

Returns a `MultiPolygon`: one capsule per segment, whose union is the true
buffer. Because each end cap is a full semicircle, the joins are already covered
with no mitring artefacts.

<details>
<summary>Why the sides are densified</summary>

A great circle is a *curve* in longitude/latitude space. Joining the two end
offsets with a straight line in that space would bulge into the corridor on one
side and out of it on the other, over a 1000 km leg, by tens of kilometres.

QOREgeo samples along the arc and offsets from the local tangent at each sample,
so both sides stay a true fixed distance from the path. Corridor area comes
within 0.02 % of the analytic stadium area.

</details>

### Around any geometry

```python
geo.buffer_geometry(some_polygon, radius=5)     # grow a shape by 5 km
geo.buffer_geometry(some_line, radius=5)
geo.buffer_geometry(some_point, radius=5)
```

---

## Point in polygon

```python
geo.point_in_polygon((28.65, 77.22), zone)     # True
geo.point_in_polygon(mumbai, zone)             # False
```

Accepts a `Polygon`, a `MultiPolygon`, a `Feature` wrapping either, or the
result of `buffer()`.

**Holes are respected.** A point inside an interior ring reads as outside.
That is what a hole means:

```python
donut = {
    "type": "Polygon",
    "coordinates": [
        [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]],   # outer
        [[4, 4], [4, 6], [6, 6], [6, 4], [4, 4]],       # hole
    ],
}
geo.point_in_polygon((5, 5), donut)     # False, in the hole
geo.point_in_polygon((1, 1), donut)     # True, in the body
```

**The boundary counts as inside.** A point exactly on an edge or vertex reads
as `True`. Plain ray casting leaves that case undefined, which produces two
results nobody expects: a convex hull that excludes the points it was built
from, and a geofence that rejects an address on its own boundary.

### Filtering a whole dataset

```python
geo.within(zone)      # features inside
geo.outside(zone)     # features outside
```

---

## Area

```python
geo.area(zone)                     # 313.654785      km²
geo.area(zone, unit="acres")       # 77505.784752
geo.area(zone, unit="ha")          # 31365.478466
geo.area()                         # every loaded feature, summed
```

**Units:** `km2` (`sqkm`, `km²`) · `m2` (`sqm`) · `ha` (`hectares`) · `acres` ·
`mi2` (`sqmi`)

Computed by spherical excess, so it is accurate for shapes of any size. The
planar shoelace formula that most quick implementations use degrades badly away
from the equator. A district in Norway comes out roughly half its true size.

Interior rings are subtracted automatically.

---

## Length and perimeter

```python
geo.length(some_line)                  # great-circle length, km
geo.length(some_polygon)               # perimeter, including holes
geo.length(some_line, unit="miles")
geo.length()                           # every loaded feature, summed
```

---

## Centroids

```python
geo.centroid(some_polygon)      # area-weighted. Stays inside a convex shape
geo.centroid()                  # mean centre of every loaded feature
```

The dataset-wide centroid is computed in 3-D and projected back to the sphere,
so it survives the antimeridian. Averaging longitudes directly would put the
centre of a Fiji dataset in Africa.

### Weighted: where should the depot go?

```python
geo.centre_of_mass("population")     # (21.0574, 77.7519)
geo.centre_of_mass()                 # same as centroid()
```

`center_of_mass` is an alias, for those who prefer it.

---

## Convex hull

```python
hull = geo.convex_hull()
geo.area(hull)                       # how much ground the dataset covers
geo.within(hull).count()             # every feature, a hull contains its own points
```

Andrew's monotone chain, O(n log n). The standard way to draw a service area,
a territory or a search boundary around a scatter of points.

Raises `InvalidGeometryError` if there are fewer than three distinct points, or
if they are collinear. Neither case has an interior.

---

## Simplification

```python
simplified = geo.simplify(tolerance=0.01)
```

Ramer-Douglas-Peucker. Boundary files are routinely 100× more detailed than any
map needs, and the vertices cost you on every render and every point-in-polygon
test.

`tolerance` is in **degrees**:

| Tolerance | Roughly | Good for |
|---|---|---|
| `0.0001` | 10 m | street-level |
| `0.001` | 100 m | city maps |
| `0.01` | 1 km | regional |
| `0.1` | 10 km | world overview |

Rings stay closed and polygons stay valid; a tolerance too large to keep a ring
returns the original rather than a broken shape.

```python
before = sum(len(c) for f in geo for c in [f["geometry"]["coordinates"][0]])
after  = sum(len(c) for f in geo.simplify(0.01) for c in [f["geometry"]["coordinates"][0]])
print(f"{before} → {after} vertices")
```

---

## Overlap tests

```python
geo.intersects(zone_a, zone_b)     # do they share any space?
geo.contains(outer, inner)         # is one fully inside the other?
```

Both handle polygon/polygon, polygon/line and line/line. A cheap bounding-box
rejection runs first, so non-overlapping shapes cost almost nothing.

```python
# Do two delivery zones clash?
north = geo.buffer((28.61, 77.20), radius=15)
south = geo.buffer((28.50, 77.25), radius=15)

if geo.intersects(north, south):
    print("Zones overlap. Orders in the middle could go to either.")
```

> **Not yet available:** the resulting shape. `intersects()` answers yes or no;
> true polygon clipping (`intersection`, `union`, `difference`, `dissolve`) is
> planned for v1.3, see the [roadmap](../ROADMAP.md#v13--topology-and-networks).

---

## Snapping to a path

```python
from qoregeo.geometry import nearest_point_on_line, cross_track_km

route = [(28.6139, 77.2090), (26.9124, 75.7873), (19.0760, 72.8777)]

nearest_point_on_line((25.0, 76.0), route)
# {'point': (25.65, 75.32), 'distance': 91.029, 'segment': 1, 'fraction': 0.21}

cross_track_km((25.0, 76.0), route[0], route[1])     # signed offset from the path
```

`nearest_point_on_line` gives you the snapped position, how far off the path it
was, which segment it belongs to, and how far along that segment, everything
needed to place a marker on a route or detect a deviation.

`cross_track_km` is signed: positive is left of travel, negative is right.

---

## Bounds

```python
geo.bounds()
# {'min_lat': 12.9716, 'max_lat': 28.6139, 'min_lng': 72.5714, 'max_lng': 88.3639}

geo.bounds_polygon()      # the same box as a drawable GeoJSON Polygon
```

---

## Working with the module directly

Everything above is available as plain functions, for when you have geometry
but no engine:

```python
from qoregeo.geometry import (
    bearing_degrees, destination, midpoint, interpolate, vincenty_km,
    geometry_area_km2, geometry_length_km, centroid_of, bbox_of,
    convex_hull, simplify, intersects, contains, point_in_geometry,
    circle_ring, line_buffer, geometry_buffer,
    convert_length, convert_area, coords_of,
)

geometry_area_km2({"type": "Polygon", "coordinates": [ring]})
point_in_geometry((28.65, 77.22), polygon)
convert_length(100, "miles")     # 62.1371
```

---

## Projections

Geometry stays in WGS84 degrees. That is what GeoJSON requires. When you need
metres:

```python
from qoregeo import to_web_mercator, from_web_mercator, to_utm, from_utm, utm_zone

to_web_mercator(28.6139, 77.2090)        # (8594866.57, 3326595.34)  EPSG:3857
to_utm(28.6139, 77.2090)
# {'easting': 715980.294, 'northing': 3167204.82, 'zone': 43,
#  'band': 'R', 'hemisphere': 'N', 'epsg': 32643}

from_utm(715980.294, 3167204.82, zone=43, hemisphere="N")   # back to degrees
```

Or project the whole dataset at once:

```python
geo.project()          # Web Mercator metres for every feature
geo.project("utm")     # UTM for every feature
```

UTM handles the two famous exceptions. Norway's widened zone 32 and the
Svalbard zones that skip 32, 34 and 36.

---

## Next

| You want to… | Go to |
|---|---|
| Filter and reshape the data | [Querying &amp; Filtering](querying.md) |
| Cluster, join, or plan a route | [Analysis](analysis.md) |
| Draw what you have measured | [Visualisation](visualisation.md) |
| Look up an exact signature | [API Reference](api-reference.md) |

---

<div align="center">

[← Loading Data](loading-data.md) · [Documentation home](README.md) · [Querying →](querying.md)

</div>
