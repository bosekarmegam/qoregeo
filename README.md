# QOREgeo

**Spatial intelligence for Python. Zero dependencies.**

[![PyPI version](https://img.shields.io/pypi/v/qoregeo.svg?logo=pypi&logoColor=white)](https://pypi.org/project/qoregeo)
[![Python versions](https://img.shields.io/pypi/pyversions/qoregeo.svg?logo=python&logoColor=white)](https://pypi.org/project/qoregeo)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://github.com/bosekarmegam/qoregeo/actions/workflows/tests.yml/badge.svg)](https://github.com/bosekarmegam/qoregeo/actions)
[![Zero dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)](https://pypi.org/project/qoregeo)

---

> *"The Pandas of Spatial Data Science."*

QOREgeo does real GIS — distance, geofencing, clustering, spatial joins,
shapefiles, routing, maps — in **pure Python, with nothing to compile**.

It installs and runs everywhere Python does: Windows, macOS, Linux, Alpine,
Raspberry Pi, AWS Lambda, locked-down corporate laptops. There is no GDAL, no
PROJ, no GEOS, and no C++ toolchain anywhere in the chain.

---

## Why QOREgeo?

```
$ pip install geopandas
ERROR: Failed building wheel for GDAL
note: This error originates from a subprocess.
error: legacy-install-failure
```

The heavyweight geospatial stack is excellent software with a brutal install
story. GDAL is a C++ library, and the wheel does not always exist for your
platform, your Python version, or your architecture.

```
$ pip install qoregeo
Successfully installed qoregeo-1.1.0

Done in 2 seconds.
```

QOREgeo trades raw speed for reach. If you are processing continent-scale
raster data, use the full stack. If you are doing the spatial work that most
applications actually need — and want it to install first time, everywhere —
this is built for you.

---

## Installation

```bash
pip install qoregeo
```

**Requirements:** Python 3.8+ · zero runtime dependencies

---

## Quick Start

```python
from qoregeo import GeoEngine

geo = GeoEngine().load("cities.csv")

# ── Distance ─────────────────────────────────────────
delhi  = (28.6139, 77.2090)
mumbai = (19.0760, 72.8777)

geo.distance(delhi, mumbai)                      # 1148.0965 km
geo.distance(delhi, mumbai, unit="miles")        # 713.3938
geo.distance(delhi, mumbai, method="vincenty")   # 1144.5264 — WGS84 ellipsoid

# ── Direction ────────────────────────────────────────
geo.bearing(delhi, mumbai)                       # 'South-Southwest'
geo.bearing(delhi, mumbai, as_degrees=True)      # 203.47
geo.destination(delhi, bearing=90, distance=100) # (28.61, 78.23)

# ── Geofencing ───────────────────────────────────────
zone = geo.buffer(delhi, radius=10, unit="km")
geo.point_in_polygon((28.65, 77.22), zone)       # True

corridor = geo.buffer_line([delhi, mumbai], radius=20)   # 20 km either side

# ── Query like a DataFrame ───────────────────────────
big = geo.query("population > 1e6 and state != 'Delhi'")
big.sort_by("population", reverse=True).head(5)

# ── Spatial analysis ─────────────────────────────────
geo.knn(delhi, k=5)                              # 5 nearest, with distances
geo.cluster(eps_km=2, min_samples=5)             # DBSCAN
geo.spatial_join(districts)                      # which district is each point in?
geo.hotspots(cell_km=5)                          # where does it bunch up?
geo.pattern()                                    # clustered, dispersed or random?

# ── Routing ──────────────────────────────────────────
route = geo.optimise_route(start=delhi, round_trip=True)
route["total_distance"]                          # ordered, 2-opt improved

# ── Visualise ────────────────────────────────────────
geo.map("map.html", colour_by="state")           # interactive Leaflet
geo.heatmap("heat.html", intensity_col="population")
geo.choropleth("choro.html", value_col="population")
geo.svg("map.svg")                               # static, no browser needed
geo.png("map.png")                               # static, no dependencies

# ── Save anywhere ────────────────────────────────────
geo.save("out.geojson")
geo.save("out.csv")
geo.save("out.gpx")
geo.save("out.kml")
```

---

## Command line

Installing the package also installs a `qoregeo` command.

```bash
qoregeo info stores.csv                    # summarise and validate a dataset
qoregeo map stores.csv -o map.html --colour-by region
qoregeo convert stores.csv stores.geojson  # any supported format to any other
qoregeo distance 28.6139,77.2090 19.0760,72.8777
qoregeo nearest hospitals.csv 28.61,77.20 -k 3
qoregeo within customers.csv 19.07,72.87 25 -o nearby.geojson
qoregeo stats stores.csv revenue
qoregeo image stores.csv -o map.png        # static image, no browser
```

Every command takes `-q/--query` to filter first, and `--json` where machine
output makes sense:

```bash
qoregeo convert stores.csv top.geojson -q "revenue > 500000 and region == 'West'"
```

---

## Method chaining

Every operation that narrows or transforms data returns a **new** engine, so
pipelines never mutate what came before:

```python
(GeoEngine()
    .load("all_stores.csv")
    .query("revenue > 250000")
    .filter_by_radius(19.07, 72.87, radius=100)
    .sort_by("revenue", reverse=True)
    .head(20)
    .save("results.geojson")
    .map("results.html", title="Top Mumbai Stores", colour_by="segment"))
```

---

## What's in it

### Loading and saving

| Format | Read | Write | Notes |
|--------|:----:|:-----:|-------|
| CSV | ✅ | ✅ | auto-detects latitude/longitude columns |
| GeoJSON / JSON | ✅ | ✅ | FeatureCollection or single Feature |
| NDJSON / GeoJSONL | ✅ | ✅ | one feature per line, streams |
| WKT | ✅ | ✅ | including PostGIS EWKT with SRID |
| GPX | ✅ | ✅ | waypoints, routes and tracks |
| KML | ✅ | ✅ | Google Earth, with ExtendedData |
| Esri Shapefile | ✅ | — | `.shp` + `.dbf`, **no GDAL** |
| SVG / PNG | — | ✅ | static maps |
| HTML | — | ✅ | interactive maps |

```python
geo.load("districts.shp")      # shapefiles, with no C library anywhere
geo.load("track.gpx")
geo.load("places.kml")
geo.save("out.ndjson")
```

### Geometry

| Method | What it does |
|--------|--------------|
| `distance(a, b, method=)` | Haversine, or Vincenty on the WGS84 ellipsoid |
| `bearing(a, b)` | 16-point compass direction, or degrees |
| `destination(origin, bearing, distance)` | where you end up |
| `midpoint(a, b)` · `interpolate(a, b, f)` | points along a great circle |
| `buffer(centre, radius)` | circular geofence |
| `buffer_line(path, radius)` | corridor around a route, river or pipeline |
| `point_in_polygon(point, polygon)` | handles holes and MultiPolygons |
| `intersects(a, b)` · `contains(a, b)` | polygon/line overlap tests |
| `area(geometry, unit=)` | spherical area — km², m², hectares, acres, mi² |
| `length(geometry)` | line length or polygon perimeter |
| `centroid()` · `centre_of_mass(weight_col)` | plain and weighted centres |
| `convex_hull()` | smallest polygon containing everything |
| `simplify(tolerance)` | Douglas–Peucker vertex reduction |

### Querying and filtering

```python
geo.query("population > 1e6 and state != 'Delhi'")
geo.query("name contains 'pur' or state in ('Delhi', 'Goa')")
geo.query("closed_date is null")

geo.filter("state", "Maharashtra")            # simple equality
geo.filter("revenue", 500000, op=">")         # any comparison
geo.filter_by_radius(28.61, 77.20, radius=50) # nearest-first, adds _distance
geo.filter_by_bbox(12, 72, 20, 81)
geo.within(zone)                              # inside a polygon
geo.outside(zone)
```

The query language is parsed, **never `eval`'d** — safe to accept from a config
file, a CLI argument or a web form. Supported operators: `== != > >= < <=`,
`contains`, `startswith`, `endswith`, `in (…)`, `is null`, combined with
`and` / `or` / `not` and parentheses.

### Table operations

`sort_by` · `head` · `tail` · `sample` · `select` · `drop` · `rename` ·
`add_column` · `apply` · `concat` · `unique` · `value_counts` · `stats` ·
`columns` · `to_records` · `to_wkt`

```python
geo.add_column("density", lambda p: float(p["pop"]) / float(p["area_km2"]))
geo.value_counts("state")        # {'Maharashtra': 12, 'Delhi': 4, …}
geo.stats("revenue")             # count, min, max, mean, median, sum
```

### Analysis

```python
geo.cluster(eps_km=2, min_samples=5)     # DBSCAN — finds groups, flags outliers
geo.kmeans(k=4)                          # exactly k groups, with centroids
geo.spatial_join(districts)              # tag points with polygon attributes
geo.hotspots(cell_km=5)                  # densest grid cells, first
geo.pattern()                            # clustered / dispersed / random
geo.dispersion()                         # how spread out the data is
geo.optimise_route(start=depot)          # stop ordering, 2-opt improved
```

Distances are great-circle throughout. Treating latitude and longitude as a
flat plane quietly ruins clustering anywhere outside the tropics.

### Speed

A spatial index makes radius and nearest-neighbour queries scale. It builds
automatically once a dataset is large enough to need one:

```python
geo.build_index()          # or let it happen automatically
geo.knn(delhi, k=10)       # ~280× faster than scanning at 50 000 features
geo.index_stats()
```

| Dataset | Brute force | Indexed | Speed-up |
|---------|------------:|--------:|---------:|
| 50 000 points, 25 km radius | 58 ms | 0.21 ms | **283×** |

Index build for 50 000 features: 79 ms.

### Geohashes, tiles and projections

```python
from qoregeo import encode, decode, neighbours, tile_of, quadkey
from qoregeo import to_web_mercator, to_utm

encode(28.6139, 77.2090, precision=7)    # 'ttnfucj'
neighbours('ttnfucj')                     # the eight surrounding cells
tile_of(28.6139, 77.2090, zoom=12)        # (2926, 1707, 12)
to_utm(28.6139, 77.2090)                  # zone 43N, easting/northing
geo.geohash_column(precision=6)           # add a geohash to every feature
```

Geohash prefixes are shared by nearby places, which turns any plain database
into one that can answer "near me" with a `LIKE 'ttnfu%'`.

### Data quality

Real data is broken in four predictable ways, so there is a method for each:

```python
geo.validate()        # what's wrong, without raising
geo.clean()           # drop unusable features
geo.dropna(["name"])  # drop blanks
geo.dedupe(tolerance_km=0.05)   # collapse the same place geocoded twice
geo.fix_coordinates()           # repair swapped lat/lng
```

`validate()` returns counts plus a per-feature list of problems, and flags the
classic latitude/longitude swap.

### Visualisation

```python
geo.map("map.html",
        basemap="dark",          # dark · light · streets · terrain · satellite
        colour_by="region",      # categorical colours plus a legend
        tooltip_field="name",
        cluster=True)            # automatic above ~750 features

geo.heatmap("heat.html", intensity_col="sales")
geo.choropleth("districts.html", value_col="population", bins=5)

geo.svg("map.svg", label_field="name")   # vector, prints at any size
geo.png("map.png", theme="light")        # raster, pure-Python encoder
```

Interactive maps are standalone HTML with a basemap switcher, scale bar, live
filter box and popups. Leaflet loads from a CDN with an automatic mirror
fallback, and a readable error instead of a blank page if both are blocked.

The SVG and PNG exports need **no browser and no network at all** — the right
output for reports, emails, README images and CI artefacts.

### Geocoding (opt-in)

Every other part of QOREgeo works offline. Geocoding cannot, so it is explicit:

```python
from qoregeo import Geocoder

coder = Geocoder(user_agent="my-app/1.0 (me@example.com)")
coder.geocode("India Gate, New Delhi")
coder.reverse(28.6129, 77.2295)

geo.geocode("address_column", user_agent="my-app/1.0 (me@example.com)")
```

Rate limited to one request per second and cached, per the public Nominatim
usage policy. Point `base_url` at your own instance for bulk work.

---

## Real-world examples

### Which branch serves each customer?

```python
from qoregeo import GeoEngine

customers = GeoEngine().load("customers.csv")
branches = {"Delhi HQ": (28.6139, 77.2090), "Mumbai Hub": (19.0760, 72.8777)}

for name, location in branches.items():
    served = customers.filter_by_radius(*location, radius=50)
    print(f"{name}: {served.count()} customers within 50 km")
    served.map(f"{name}.html", title=name)
```

### Delivery zone check

```python
geo = GeoEngine()
zone = geo.buffer((28.6315, 77.2167), radius=5)      # 5 km from the restaurant

for order in incoming_orders:
    deliverable = geo.point_in_polygon(order["location"], zone)
    print("DELIVER" if deliverable else "TOO FAR")
```

### Nearest hospital

```python
geo = GeoEngine().load("hospitals.csv")
patient = (28.6139, 77.2090)

for match in geo.knn(patient, k=3):
    print(f"{match['feature']['properties']['name']}: {match['distance']} km")

geo.filter_by_radius(*patient, radius=5).map("nearby.html")
```

### Where are the incident clusters?

```python
incidents = GeoEngine().load("incidents.csv")

clustered = incidents.cluster(eps_km=0.5, min_samples=10)
print(clustered.value_counts("_cluster"))

print(incidents.pattern())                # is the clustering even real?
incidents.map("clusters.html", colour_by="_cluster")
```

### Sales territory report

```python
sales = GeoEngine().load("sales.csv")
districts = GeoEngine().load("districts.shp")      # no GDAL needed

by_district = sales.spatial_join(districts, prefix="district_")
print(by_district.value_counts("district_name"))

districts.choropleth("territories.html", value_col="revenue", label_col="name")
```

### Optimise a delivery run

```python
stops = GeoEngine().load("deliveries.csv")
route = stops.optimise_route(start=(28.6139, 77.2090), round_trip=True)

print(f"{route['total_distance']} km, {route['improvement_pct']}% better than greedy")
for leg in route["legs"]:
    print(f"  → {leg['distance']} km {leg['direction']}")

route["engine"].map("route.html")
```

---

## Errors that teach

```
❌  QOREgeo — Column Not Found
────────────────────────────────────────────────
File: 'stores.csv'
Could not find a 'lat' column.

Columns in your file:
    'store_id', 'latitude', 'longitude'

Fix it:
    geo.load('stores.csv', lat_col='latitude', lng_col='longitude')
```

Every exception names what went wrong and shows the fix, copy-pasteable.

---

## Design decisions

**Zero dependencies is a feature, not a limitation.** It is why QOREgeo
installs on a Raspberry Pi, in a Lambda layer, and on a corporate laptop
without admin rights.

**Great-circle everywhere.** Latitude and longitude are not a flat plane, and
pretending otherwise breaks clustering, buffers and areas outside the tropics.

**New engine per operation.** Filters and transforms return a new `GeoEngine`;
the original is untouched. Operations that only write a file return `self`, so
chains keep flowing.

**Boundaries count as inside.** A point exactly on a polygon edge reads as
inside. Otherwise a convex hull would exclude the points it was built from, and
a geofence would reject an address on its own boundary.

**Parsed, not `eval`'d.** The query language is a real parser precisely so that
a query string from a config file or a web request is safe to run.

---

## Testing

```bash
pip install -e ".[dev]"
pytest                      # 933 tests
pytest --cov=qoregeo        # 94% coverage
ruff check qoregeo tests
mypy qoregeo
```

Numerical code is tested against analytic results or brute-force
implementations rather than against its own output: the spatial index is
verified feature-for-feature against a linear scan across hundreds of
randomised cases including polar and antimeridian queries, route optimisation
against a brute-force permutation search, buffer areas against the closed-form
stadium area, and the PNG encoder by decoding its own output and checking every
chunk CRC.

---

## Roadmap

The full plan — what is coming, why, and what "done" means for each item —
lives in **[ROADMAP.md](ROADMAP.md)**.

| Version | Status | Theme |
|---------|--------|-------|
| v1.0 | ✅ Shipped | Core spatial engine |
| v1.1 | ✅ Shipped | Analysis: geometry, indexing, clustering, formats, CLI |
| v1.2 | 🔜 Next | Raster, trajectories, streaming |
| v1.3 | 📋 Planned | Topological overlay, network routing |
| v2.0 | 📋 Planned | Performance, plugins, QORE OS bridge |
| v3.0 | 🔮 Research | Quantum spatial algorithms, GeoAI, WebAssembly |

---

## Contributing

See **[CONTRIBUTING.md](CONTRIBUTING.md)**. The most useful contribution is a
concrete case QOREgeo handles badly — open an issue describing what you were
trying to do and what you had to install instead.

---

## License

MIT © 2025 [ArcGX TechLabs Private Limited](https://arcgx.in)
Built by Suneel Bose K

---

## Links

- 📦 PyPI: [pypi.org/project/qoregeo](https://pypi.org/project/qoregeo)
- 🐙 GitHub: [github.com/bosekarmegam/qoregeo](https://github.com/bosekarmegam/qoregeo)
- 🗺️ Roadmap: [ROADMAP.md](ROADMAP.md)
- 🐛 Issues: [github.com/bosekarmegam/qoregeo/issues](https://github.com/bosekarmegam/qoregeo/issues)
- ✉️ Contact: suneelbosekarmegam@gmail.com
