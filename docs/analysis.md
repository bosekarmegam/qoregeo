<div align="center">

[← Querying](querying.md) · **Analysis** · [Visualisation →](visualisation.md)

</div>

---

# Analysis

Clustering, spatial joins, density, point-pattern statistics, route
optimisation, and the index that makes it all fast.

---

<a name="spatial-indexing"></a>
## Spatial indexing

Radius and nearest-neighbour queries scan every feature by default: fine for a
few hundred points, painful for a few hundred thousand. The index buckets
features into a grid so a query only looks at the cells its search radius
actually touches.

```python
geo.build_index()
geo.index_stats()
# {'features': 50000, 'cells': 13237, 'cell_size_km': 24.4477,
#  'avg_per_cell': 3.78, 'max_per_cell': 14}
```

You rarely need to call it, datasets at or above `AUTO_INDEX_THRESHOLD`
(500 features) get an index built automatically on the first spatial query.

| Dataset | Brute force | Indexed | Speed-up |
|---|---:|---:|---:|
| 50 000 points, 25 km radius | 58 ms | 0.21 ms | **283×** |

Build time for 50 000 features: 79 ms.

```python
geo.build_index(cell_size_km=5)     # tune it if you know your data
```

Smaller cells mean fewer candidates per query but more memory; the automatic
size targets about four points per cell, which is a good default. `index_stats()`
tells you whether the resolution is sane, a `max_per_cell` in the thousands
means the cells are too big for your data.

The index is exact, not approximate. It returns precisely what a linear scan
would, including for searches that cross a pole or the antimeridian, and the
test suite verifies that feature-for-feature against brute force.

---

## Clustering

### DBSCAN: find groups without guessing how many

```python
clustered = geo.cluster(eps_km=2, min_samples=5)

clustered.value_counts("_cluster")
# {'0': 143, '1': 88, '-1': 12, '2': 31}
```

Every feature gains a `_cluster` property. Label `-1` is **noise**, points too
isolated to belong anywhere.

| Parameter | Meaning | How to choose |
|---|---|---|
| `eps_km` | how close counts as neighbouring | the distance at which two things are "in the same place" for your problem |
| `min_samples` | neighbours needed to form a dense core | roughly the smallest group you would call a cluster |

DBSCAN's advantage over k-means is that you do not have to know the number of
clusters in advance, and genuine outliers stay outliers instead of being forced
into the nearest group. That matters for incident data, illegal dumping
reports, fraud locations. Anywhere the interesting thing is *where the
clumping is*, not *how many clumps there are*.

```python
# Isolate one cluster
core = clustered.query("_cluster == 0")

# Drop the noise
signal = clustered.query("_cluster != -1")

# Look at only the outliers: often the point of the exercise
outliers = clustered.query("_cluster == -1")
```

### k-means: exactly *k* groups

```python
result = geo.kmeans(k=4, seed=42)

result["engine"]        # a GeoEngine with a _cluster property
result["centroids"]     # [(lat, lng), …], one per cluster
result["inertia"]       # total squared distance to assigned centres
result["iterations"]    # how many passes it took
```

Use this when the number of groups is fixed by the problem, four depots, six
sales territories, three shift zones.

Seeded with k-means++, so the result is stable and well spread rather than
dependent on a lucky draw. Pass `seed=` for reproducibility.

Centroids are averaged in 3-D and projected back to the sphere, so a cluster
straddling the antimeridian gets a centre in the right ocean.

<details>
<summary>Choosing k with an elbow plot</summary>

```python
for k in range(1, 9):
    print(k, geo.kmeans(k=k, seed=1)["inertia"])
```

Inertia always falls as `k` rises. Look for the point where it stops falling
steeply. That is usually a defensible choice.

</details>

---

## Spatial joins

The most common GIS operation after distance: **which polygon does each point
fall in?**

```python
customers = GeoEngine().load("customers.csv")
districts = GeoEngine().load("districts.shp")

tagged = customers.spatial_join(districts, prefix="district_")

tagged.value_counts("district_name")
# {'Central': 412, 'North': 288, 'Riverside': 96, …}
```

| Parameter | Effect |
|---|---|
| `prefix` | prepended to copied names, so `code` does not clobber your own `code` |
| `keep` | copy only these polygon properties |
| `how` | `"left"` keeps unmatched points (default), `"inner"` drops them |

```python
# Only the district name, and drop anyone outside every district
inside = customers.spatial_join(districts, keep=["name"], how="inner")

# Which customers fell outside every polygon? Usually worth knowing.
orphans = customers.count() - inside.count()
```

A bounding-box rejection runs before the expensive point-in-polygon test, so
joins against detailed boundary files stay quick. Where polygons overlap, the
first match wins.

---

## Density and hotspots

```python
cells = geo.hotspots(cell_km=5, min_count=10)

for cell in cells[:5]:
    lat, lng = cell["centre"]
    print(f"{cell['count']:4d} at {lat:.4f}, {lng:.4f}")
```

Each entry carries:

| Key | What it is |
|---|---|
| `count` | how many features fell in the cell |
| `centre` | `(lat, lng)` mean of the members |
| `bounds` | the cell's extent |
| `polygon` | a drawable GeoJSON Polygon |

Sorted densest-first, so `cells[0]` is the busiest square in your data.

Straight into a choropleth:

```python
from qoregeo import GeoEngine

grid = GeoEngine().load_data([
    {"type": "Feature", "geometry": cell["polygon"], "properties": {"count": cell["count"]}}
    for cell in geo.hotspots(cell_km=5, min_count=2)
])
grid.choropleth("density.html", value_col="count")
```

---

## Is the pattern real?

Points on a map almost always *look* clustered. The Clark & Evans
nearest-neighbour ratio tells you whether they actually are.

```python
geo.pattern()
# {'observed_mean_km': 464.7773, 'expected_mean_km': 299.0541,
#  'ratio': 1.5542, 'pattern': 'dispersed', 'area_km2': 2861866.6602, 'n': 8}
```

| Ratio | Verdict | Reading |
|---|---|---|
| `< 0.9` | `clustered` | closer together than random chance |
| `0.9 - 1.1` | `random` | indistinguishable from random |
| `> 1.1` | `dispersed` | more evenly spread than random |

Worth running before you present a clustering result. If the ratio says
`random`, the clusters your eye found are an artefact of the map.

### Spread

```python
geo.dispersion()
# {'centre_lat': 19.466269, 'centre_lng': 77.646996,
#  'mean_km': 686.0125, 'median_km': 690.0823,
#  'max_km': 1164.3778, 'standard_distance_km': 742.6971}
```

`standard_distance_km` is the spatial equivalent of a standard deviation, one
number for how concentrated the dataset is.

---

## Route optimisation

```python
route = geo.optimise_route(start=(28.6139, 77.2090), round_trip=True)

route["total_distance"]      # 5293.9627
route["improvement_pct"]     # how much 2-opt beat the greedy tour
route["order"]               # indices into your features, in visiting order
route["engine"]              # a GeoEngine with the features reordered
```

`optimize_route` is an alias.

### The legs

```python
for leg in route["legs"]:
    print(f"{leg['distance']:8.1f} km  {leg['direction']}")
```
```
   775.7 km  Southwest
   441.0 km  South-Southeast
   ...
```

Each leg carries `from`, `to`, `from_index`, `to_index`, `distance`, `bearing`
and `direction`.

### How it works, and what it is not

Greedy nearest-neighbour for a fast starting tour, then 2-opt local search to
untangle crossings. On problems small enough to check exhaustively it reaches
the true optimum; on larger ones it lands within a few percent, which is what
practical route planning needs.

> **These are straight-line distances.** `optimise_route` decides the **order**
> of stops, not the roads between them. For road distances you need a routing
> engine with a street network, planned for v1.3, see the
> [roadmap](../ROADMAP.md#v13--topology-and-networks).

### Timing a run

```python
from qoregeo import travel_time

travel_time(route["total_distance"], speed_kmh=45, stop_minutes=12,
            stops=route["engine"].count())
# {'driving_minutes': …, 'stop_minutes': …, 'total_minutes': …, 'total_hours': …}
```

`speed_kmh` is an average including traffic, 40 for dense urban, 60-80
intercity.

### Drawing it

```python
from qoregeo.routing import route_line

line = route_line(route)
GeoEngine().load_data(route["engine"].get_features() +
                      [{"type": "Feature", "geometry": line, "properties": {"name": "route"}}]
).map("route.html")
```

---

## Weighted centres

```python
geo.centre_of_mass("population")     # (21.0574, 77.7519)
geo.centroid()                       # (19.4663, 77.6470), unweighted
```

The "where should the hub go?" answer. Weight by whatever drives the decision:
population, revenue, order volume, incident count.

The unweighted centroid of Indian cities sits well south of the
population-weighted one, because the north is where the people are.

---

## Geohashes for grouping and storage

```python
tagged = geo.geohash_column(precision=6)
tagged.value_counts("_geohash")
```

Geohashes share a prefix when they are close together, which turns any plain
database into one that can answer "near me":

```sql
SELECT * FROM places WHERE geohash LIKE 'ttnfu%';
```

| Precision | Cell size | Good for |
|---|---|---|
| 4 | ~40 km | regional grouping |
| 5 | ~5 km | city districts |
| 6 | ~1.2 km | neighbourhoods |
| 7 | ~150 m | street blocks |
| 9 | ~5 m | building level |

```python
from qoregeo import encode, decode, neighbours, tile_of, quadkey

encode(28.6139, 77.2090, 7)     # 'ttnfucj'
decode('ttnfucj')                # back to (lat, lng)
neighbours('ttnfucj')            # the eight surrounding cells
tile_of(28.6139, 77.2090, 12)    # (2926, 1707, 12), slippy map tile
```

Query a cell **plus its neighbours** to cover the case where the nearest thing
sits just across a boundary.

---

## Using the module directly

```python
from qoregeo.analysis import (
    dbscan, kmeans, spatial_join, hotspots,
    nearest_neighbour_ratio, centre_of_mass, dispersion, spherical_mean, NOISE,
)

labels = dbscan(features, eps_km=1.0, min_samples=3)   # one label per feature
spherical_mean([(0.0, 179.0), (0.0, -179.0)])          # (0.0, 180.0), not 0.0
```

---

## Worked example: territory review

```python
from qoregeo import GeoEngine

sales     = GeoEngine().load("sales.csv").clean()
districts = GeoEngine().load("districts.shp")

# 1 · Which district is each sale in?
tagged = sales.spatial_join(districts, keep=["name"], prefix="d_")

# 2 · Where does business actually concentrate?
for cell in tagged.hotspots(cell_km=10, min_count=25)[:5]:
    print(f"{cell['count']:5d} sales near {cell['centre'][0]:.3f}, {cell['centre'][1]:.3f}")

# 3 · Is that concentration real, or just how maps look?
print(tagged.pattern()["pattern"])

# 4 · Natural clusters, ignoring the district lines
clusters = tagged.cluster(eps_km=3, min_samples=15)
print(clusters.value_counts("_cluster"))

# 5 · Where should the new depot go?
print("Suggested depot:", tagged.centre_of_mass("revenue"))

# 6 · A route around the top accounts
top = tagged.sort_by("revenue", reverse=True).head(12)
route = top.optimise_route(start=tagged.centre_of_mass("revenue"), round_trip=True)
print(f"{route['total_distance']} km round trip")

# 7 · Show the work
clusters.map("territories.html", colour_by="_cluster", tooltip_field="d_name")
```

---

## Next

| You want to… | Go to |
|---|---|
| Style and export the maps | [Visualisation](visualisation.md) |
| Run this from a shell script | [Command Line](cli.md) |
| Look up an exact signature | [API Reference](api-reference.md) |
| Copy a finished solution | [Recipes](recipes.md) |

---

<div align="center">

[← Querying](querying.md) · [Documentation home](README.md) · [Visualisation →](visualisation.md)

</div>
