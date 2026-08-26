<div align="center">

<img src="assets/qoregeo-banner.png" alt="QOREgeo" width="100%">

<br>

**Real GIS in Python. Nothing to compile.**

Distance, geofencing, shapefiles, clustering, spatial joins, routing and maps,
in pure Python with **zero dependencies**.

<br>

[![PyPI version](https://img.shields.io/pypi/v/qoregeo?logo=pypi&logoColor=white&color=00D4AA&labelColor=0B0D14)](https://pypi.org/project/qoregeo)
[![Python](https://img.shields.io/pypi/pyversions/qoregeo?logo=python&logoColor=white&color=00D4AA&labelColor=0B0D14)](https://pypi.org/project/qoregeo)
[![Tests](https://img.shields.io/github/actions/workflow/status/bosekarmegam/qoregeo/tests.yml?branch=main&logo=github&logoColor=white&label=tests&color=00D4AA&labelColor=0B0D14)](https://github.com/bosekarmegam/qoregeo/actions/workflows/tests.yml)
[![Dependencies](https://img.shields.io/badge/dependencies-0-00D4AA?labelColor=0B0D14)](https://github.com/bosekarmegam/qoregeo/blob/main/pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-00D4AA?labelColor=0B0D14)](LICENSE)
[![Downloads](https://img.shields.io/pypi/dm/qoregeo?color=00D4AA&labelColor=0B0D14)](https://pypi.org/project/qoregeo)

<br>

[**Documentation**](docs/README.md) ·
[**Getting Started**](docs/getting-started.md) ·
[**API Reference**](docs/api-reference.md) ·
[**Recipes**](docs/recipes.md) ·
[**Roadmap**](ROADMAP.md) ·
[**Changelog**](CHANGELOG.md)

</div>

<br>

```bash
pip install qoregeo
```

<br>

---

## The problem

```console
$ pip install geopandas
ERROR: Failed building wheel for GDAL
note: This error originates from a subprocess.
error: legacy-install-failure
```

The heavyweight geospatial stack is excellent software with a brutal install
story. GDAL is a C++ library, and a working wheel does not always exist for
your platform, your Python version, or your architecture.

```console
$ pip install qoregeo
Successfully installed qoregeo-1.1.0

Done in 2 seconds.
```

QOREgeo runs everywhere Python does: Windows, macOS, Linux, Alpine, Raspberry
Pi, AWS Lambda, locked-down corporate laptops. No GDAL, no PROJ, no GEOS, no
C++ toolchain anywhere in the chain.

It trades raw throughput for reach. For continent-scale raster work, use the
full stack. For the spatial work most applications actually need, and where the
install has to succeed first time everywhere, this is built for you.

<br>

---

## What you get

<table>
<tr>
<td width="33%" valign="top">

### 📐 Geometry
Haversine and Vincenty distance, bearings, geodesic buffers, route corridors,
spherical area, convex hulls, simplification, overlap tests.

[Guide →](docs/geometry.md)

</td>
<td width="33%" valign="top">

### 📂 Formats
CSV, GeoJSON, NDJSON, WKT, GPX, KML, and **Esri shapefiles parsed without
GDAL**. Write to any of them plus SVG, PNG and HTML.

[Guide →](docs/loading-data.md)

</td>
<td width="33%" valign="top">

### 🔎 Querying
A safe, parsed expression language, spatial filters, and DataFrame-style table
operations that feel like pandas.

[Guide →](docs/querying.md)

</td>
</tr>
<tr>
<td width="33%" valign="top">

### 📊 Analysis
DBSCAN, k-means, spatial joins, hotspot grids, point-pattern statistics,
weighted centres, TSP route optimisation.

[Guide →](docs/analysis.md)

</td>
<td width="33%" valign="top">

### 🗺️ Maps
Interactive Leaflet HTML, heatmaps, choropleths, plus SVG and PNG rendered with
**no browser and no network**.

[Guide →](docs/visualisation.md)

</td>
<td width="33%" valign="top">

### ⚡ Speed
A grid spatial index makes radius and nearest-neighbour queries **283x faster**
at 50 000 features, and it is exact.

[Guide →](docs/analysis.md#spatial-indexing)

</td>
</tr>
</table>

<br>

---

## Quick start

```python
from qoregeo import GeoEngine

geo = GeoEngine().load("cities.csv")

delhi, mumbai = (28.6139, 77.2090), (19.0760, 72.8777)
```

**Measure**

```python
geo.distance(delhi, mumbai)                      # 1148.0965 km
geo.distance(delhi, mumbai, unit="miles")        # 713.3938
geo.distance(delhi, mumbai, method="vincenty")   # 1144.5264 on the WGS84 ellipsoid
geo.bearing(delhi, mumbai)                       # 'South-Southwest'
geo.destination(delhi, bearing=90, distance=100) # (28.61, 78.2334)
```

**Geofence**

```python
zone = geo.buffer(delhi, radius=10, unit="km")
geo.point_in_polygon((28.65, 77.22), zone)       # True
geo.area(zone, unit="acres")                     # 77505.784752

corridor = geo.buffer_line([delhi, mumbai], radius=20)   # 20 km either side
```

**Query**

```python
geo.query("population > 1e6 and state != 'Delhi'")
geo.query("name contains 'pur' or state in ('Delhi', 'Goa')")
geo.filter_by_radius(28.61, 77.20, radius=50)    # nearest first, adds _distance
geo.within(zone)                                  # inside a polygon
```

**Analyse**

```python
geo.knn(delhi, k=5)                   # 5 nearest, with distances
geo.cluster(eps_km=2, min_samples=5)  # DBSCAN, outliers flagged not forced
geo.spatial_join(districts)           # which district is each point in?
geo.hotspots(cell_km=5)               # where does it bunch up?
geo.pattern()                         # is that clustering even real?
geo.optimise_route(start=delhi, round_trip=True)
```

**Visualise and save**

```python
geo.map("map.html", colour_by="state", tooltip_field="name")
geo.heatmap("heat.html", intensity_col="population")
geo.choropleth("districts.html", value_col="population")
geo.png("map.png")                    # no browser, no network, no dependency

geo.save("out.geojson")               # or .csv .gpx .kml .wkt .ndjson .svg
```

**Chain it**

Every operation that narrows or transforms data returns a **new** engine, so
pipelines never mutate what came before.

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

<br>

---

## From your shell

Installing the package installs a `qoregeo` command.

```bash
qoregeo info stores.csv                     # summarise and validate a dataset
qoregeo map stores.csv -o map.html --colour-by region
qoregeo convert districts.shp out.geojson   # shapefiles, without GDAL
qoregeo distance 28.6139,77.2090 19.0760,72.8777
qoregeo nearest hospitals.csv 28.61,77.20 -k 3
qoregeo within customers.csv 19.07,72.87 25 -o nearby.geojson
qoregeo image stores.csv -o map.png         # static image, no browser
```

Every data command takes `-q/--query` to filter first, and `--json` where
machine output makes sense.

```bash
qoregeo convert stores.csv top.geojson -q "revenue > 500000 and region == 'West'"
```

[Full CLI reference →](docs/cli.md)

<br>

---

## Documentation

<table>
<tr>
<td width="50%">

📖 **[Documentation home](docs/README.md)**
Everything, organised.

🚀 **[Getting Started](docs/getting-started.md)**
Install to interactive map in five minutes.

📂 **[Loading &amp; Saving Data](docs/loading-data.md)**
Every format, column detection, cleaning.

📐 **[Geometry](docs/geometry.md)**
Distance, buffers, area, hulls, overlap.

🔎 **[Querying &amp; Filtering](docs/querying.md)**
The query language and table operations.

</td>
<td width="50%">

📊 **[Analysis](docs/analysis.md)**
Clustering, joins, hotspots, routing, indexing.

🗺️ **[Visualisation](docs/visualisation.md)**
Maps, heatmaps, choropleths, static export.

⌨️ **[Command Line](docs/cli.md)**
Every subcommand and flag.

📋 **[API Reference](docs/api-reference.md)**
Every method in one page.

🍳 **[Recipes](docs/recipes.md)** · ❓ **[FAQ](docs/faq.md)**
Finished solutions, and fixes for common errors.

</td>
</tr>
</table>

<br>

---

## Capabilities in detail

<details>
<summary><b>Formats: read and write</b></summary>

<br>

| Format | Read | Write | Notes |
|--------|:----:|:-----:|-------|
| CSV | ✅ | ✅ | auto-detects latitude and longitude columns |
| GeoJSON / JSON | ✅ | ✅ | FeatureCollection or a single Feature |
| NDJSON / GeoJSONL | ✅ | ✅ | one feature per line, streams |
| WKT | ✅ | ✅ | including PostGIS EWKT with SRID |
| GPX | ✅ | ✅ | waypoints, routes, tracks, elevation, timestamps |
| KML | ✅ | ✅ | Google Earth, with ExtendedData and holes |
| Esri Shapefile | ✅ | | `.shp` + `.dbf`, **no GDAL** |
| SVG / PNG | | ✅ | static maps |
| HTML | | ✅ | interactive maps |

```python
geo.load("districts.shp")      # shapefiles, with no C library anywhere
geo.load("track.gpx")
geo.load("places.kml")
geo.save("out.ndjson")
```

The shapefile reader parses `.shp` with `struct` and the `.dbf` attribute table
alongside it. Point, PolyLine, Polygon and MultiPoint are supported, including
their Z and M variants, with holes detected from ring winding.

[Guide →](docs/loading-data.md)

</details>

<details>
<summary><b>Geometry</b></summary>

<br>

| Method | What it does |
|--------|--------------|
| `distance(a, b, method=)` | Haversine, or Vincenty on the WGS84 ellipsoid |
| `bearing(a, b)` | 16-point compass direction, or degrees |
| `destination(origin, bearing, distance)` | where you end up |
| `midpoint(a, b)` · `interpolate(a, b, f)` | points along a great circle |
| `buffer(centre, radius)` | circular geofence |
| `buffer_line(path, radius)` | corridor around a route, river or pipeline |
| `point_in_polygon(point, polygon)` | handles holes and MultiPolygons |
| `intersects(a, b)` · `contains(a, b)` | polygon and line overlap tests |
| `area(geometry, unit=)` | spherical area in km², m², hectares, acres, mi² |
| `length(geometry)` | line length or polygon perimeter |
| `centroid()` · `centre_of_mass(weight_col)` | plain and weighted centres |
| `convex_hull()` | smallest polygon containing everything |
| `simplify(tolerance)` | Douglas-Peucker vertex reduction |

Everything is great-circle. Treating latitude and longitude as a flat plane
quietly ruins clustering, buffers and areas outside the tropics.

[Guide →](docs/geometry.md)

</details>

<details>
<summary><b>Querying and filtering</b></summary>

<br>

```python
geo.query("population > 1e6 and state != 'Delhi'")
geo.query("name contains 'pur' or state in ('Delhi', 'Goa')")
geo.query("closed_date is null")

geo.filter("state", "Maharashtra")             # simple equality
geo.filter("revenue", 500000, op=">")          # any comparison
geo.filter_by_radius(28.61, 77.20, radius=50)  # nearest first, adds _distance
geo.filter_by_bbox(12, 72, 20, 81)
geo.within(zone)                               # inside a polygon
geo.outside(zone)
```

Operators: `==` `!=` `>` `>=` `<` `<=` `contains` `startswith` `endswith`
`in (…)` `is null`, combined with `and`, `or`, `not` and parentheses.

The query language is parsed, **never `eval`'d**, so it is safe to accept from
a config file, a CLI argument or a web form.

**Table operations:** `sort_by` · `head` · `tail` · `sample` · `select` ·
`drop` · `rename` · `add_column` · `apply` · `concat` · `unique` ·
`value_counts` · `stats` · `columns` · `to_records` · `to_wkt`

```python
geo.add_column("density", lambda p: float(p["pop"]) / float(p["area_km2"]))
geo.value_counts("state")        # {'Maharashtra': 12, 'Delhi': 4, …}
geo.stats("revenue")             # count, min, max, mean, median, sum
```

[Guide →](docs/querying.md)

</details>

<details>
<summary><b>Analysis</b></summary>

<br>

```python
geo.cluster(eps_km=2, min_samples=5)     # DBSCAN, finds groups, flags outliers
geo.kmeans(k=4)                          # exactly k groups, with centroids
geo.spatial_join(districts)              # tag points with polygon attributes
geo.hotspots(cell_km=5)                  # densest grid cells first
geo.pattern()                            # clustered, dispersed or random
geo.dispersion()                         # how spread out the data is
geo.centre_of_mass("revenue")            # where should the depot go?
geo.optimise_route(start=depot)          # stop ordering, 2-opt improved
```

DBSCAN needs no guess at the number of clusters and leaves genuine outliers as
outliers. k-means is k-means++ seeded and averages centroids in 3-D, so a
cluster straddling the antimeridian gets a centre in the right ocean.

[Guide →](docs/analysis.md)

</details>

<details>
<summary><b>Speed and indexing</b></summary>

<br>

A grid spatial index makes radius and nearest-neighbour queries scale. It
builds automatically once a dataset is large enough to need one.

```python
geo.build_index()          # or let it happen automatically at 500+ features
geo.knn(delhi, k=10)
geo.index_stats()
```

| Dataset | Brute force | Indexed | Speed-up |
|---------|------------:|--------:|---------:|
| 50 000 points, 25 km radius | 58 ms | 0.21 ms | **283x** |

Index build for 50 000 features: 79 ms.

The index is exact rather than approximate. It returns precisely what a linear
scan would, and the test suite verifies that feature-for-feature across
hundreds of randomised cases, including queries that cross a pole or the
antimeridian.

</details>

<details>
<summary><b>Geohashes, tiles and projections</b></summary>

<br>

```python
from qoregeo import encode, decode, neighbours, tile_of, quadkey
from qoregeo import to_web_mercator, to_utm

encode(28.6139, 77.2090, precision=7)    # 'ttnfucj'
neighbours('ttnfucj')                     # the eight surrounding cells
tile_of(28.6139, 77.2090, zoom=12)        # (2926, 1707, 12)
to_utm(28.6139, 77.2090)                  # zone 43N, easting and northing
geo.geohash_column(precision=6)           # add a geohash to every feature
```

Nearby places share a geohash prefix, which turns any plain database into one
that can answer "near me" with a `LIKE 'ttnfu%'`.

UTM handles the two famous exceptions: Norway's widened zone 32 and the
Svalbard zones that skip 32, 34 and 36.

</details>

<details>
<summary><b>Data quality</b></summary>

<br>

Real data is broken in four predictable ways, so there is a method for each.

```python
geo.validate()                  # what is wrong, without raising
geo.clean()                     # drop unusable features
geo.dropna(["name"])            # drop blanks
geo.dedupe(tolerance_km=0.05)   # collapse the same place geocoded twice
geo.fix_coordinates()           # repair swapped latitude and longitude
```

`validate()` returns counts plus a per-feature list of problems, and flags the
classic latitude and longitude swap.

</details>

<details>
<summary><b>Visualisation</b></summary>

<br>

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

The SVG and PNG exports need **no browser and no network at all**, which is the
right output for reports, emails, README images and CI artefacts.

[Guide →](docs/visualisation.md)

</details>

<details>
<summary><b>Geocoding (opt-in)</b></summary>

<br>

Every other part of QOREgeo works offline. Geocoding cannot, so it is explicit.

```python
from qoregeo import Geocoder

coder = Geocoder(user_agent="my-app/1.0 (me@example.com)")
coder.geocode("India Gate, New Delhi")
coder.reverse(28.6129, 77.2295)

geo.geocode("address_column", user_agent="my-app/1.0 (me@example.com)")
```

Rate limited to one request per second and cached, per the public Nominatim
usage policy. Point `base_url` at your own instance for bulk work.

</details>

<br>

---

## Worked examples

<details>
<summary><b>Which branch serves each customer?</b></summary>

<br>

```python
from qoregeo import GeoEngine

customers = GeoEngine().load("customers.csv")
branches = {"Delhi HQ": (28.6139, 77.2090), "Mumbai Hub": (19.0760, 72.8777)}

for name, location in branches.items():
    served = customers.filter_by_radius(*location, radius=50)
    print(f"{name}: {served.count()} customers within 50 km")
    served.map(f"{name}.html", title=name)
```

</details>

<details>
<summary><b>Delivery zone check</b></summary>

<br>

```python
geo = GeoEngine()
zone = geo.buffer((28.6315, 77.2167), radius=5)      # 5 km from the restaurant

for order in incoming_orders:
    deliverable = geo.point_in_polygon(order["location"], zone)
    print("DELIVER" if deliverable else "TOO FAR")
```

</details>

<details>
<summary><b>Nearest hospital</b></summary>

<br>

```python
geo = GeoEngine().load("hospitals.csv")
patient = (28.6139, 77.2090)

for match in geo.knn(patient, k=3):
    print(f"{match['feature']['properties']['name']}: {match['distance']} km")

geo.filter_by_radius(*patient, radius=5).map("nearby.html")
```

</details>

<details>
<summary><b>Where are the incident clusters?</b></summary>

<br>

```python
incidents = GeoEngine().load("incidents.csv")

clustered = incidents.cluster(eps_km=0.5, min_samples=10)
print(clustered.value_counts("_cluster"))

print(incidents.pattern())                # is the clustering even real?
incidents.map("clusters.html", colour_by="_cluster")
```

</details>

<details>
<summary><b>Sales territory report</b></summary>

<br>

```python
sales = GeoEngine().load("sales.csv")
districts = GeoEngine().load("districts.shp")      # no GDAL needed

by_district = sales.spatial_join(districts, prefix="district_")
print(by_district.value_counts("district_name"))

districts.choropleth("territories.html", value_col="revenue", label_col="name")
```

</details>

<details>
<summary><b>Optimise a delivery run</b></summary>

<br>

```python
stops = GeoEngine().load("deliveries.csv")
route = stops.optimise_route(start=(28.6139, 77.2090), round_trip=True)

print(f"{route['total_distance']} km, {route['improvement_pct']}% better than greedy")
for leg in route["legs"]:
    print(f"  {leg['distance']} km {leg['direction']}")

route["engine"].map("route.html")
```

</details>

More complete solutions in **[Recipes](docs/recipes.md)**.

<br>

---

## Errors that teach

```
❌  QOREgeo: Column Not Found
────────────────────────────────────────────
File: 'stores.csv'
Could not find a 'lat' column.

Columns in your file:
    'store_id', 'latitude', 'longitude'

Fix it:
    geo.load('stores.csv', lat_col='latitude', lng_col='longitude')
```

Every exception names what went wrong and shows the fix, copy-pasteable.

<br>

---

## Design decisions

**Zero dependencies is a feature, not a limitation.** It is why QOREgeo
installs on a Raspberry Pi, in a Lambda layer, and on a corporate laptop
without admin rights.

**Great-circle everywhere.** Latitude and longitude are not a flat plane, and
pretending otherwise breaks clustering, buffers and areas outside the tropics.

**A new engine per operation.** Filters and transforms return a new
`GeoEngine`; the original is untouched. Operations that only write a file
return `self`, so chains keep flowing.

**Boundaries count as inside.** A point exactly on a polygon edge reads as
inside. Otherwise a convex hull would exclude the points it was built from, and
a geofence would reject an address on its own boundary.

**Parsed, not `eval`'d.** The query language is a real parser precisely so that
a query string from a config file or a web request is safe to run.

<br>

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
implementations rather than against its own output. The spatial index is
verified feature-for-feature against a linear scan across hundreds of
randomised cases including polar and antimeridian queries, route optimisation
against a brute-force permutation search, buffer areas against the closed-form
stadium area, and the PNG encoder by decoding its own output and checking every
chunk CRC.

<br>

---

## Roadmap

What is coming, why, and what "done" means for each item lives in
**[ROADMAP.md](ROADMAP.md)**.

| Version | Status | Theme |
|---------|--------|-------|
| v1.0 | ✅ Shipped | Core spatial engine |
| v1.1 | ✅ Shipped | Analysis: geometry, indexing, clustering, formats, CLI |
| v1.2 | 🔜 Next | Raster, trajectories, streaming |
| v1.3 | 📋 Planned | Topological overlay, network routing |
| v2.0 | 📋 Planned | Performance, plugins, QORE OS bridge |
| v3.0 | 🔮 Research | Quantum spatial algorithms, GeoAI, WebAssembly |

<br>

---

## Contributing

See **[CONTRIBUTING.md](CONTRIBUTING.md)**. The most useful contribution is a
concrete case QOREgeo handles badly. Open an issue describing what you were
trying to do and what you had to install instead.

<br>

---

<div align="center">

**MIT licensed** · © 2025 [ArcGX TechLabs Private Limited](https://arcgx.in) · Built by Suneel Bose K

[PyPI](https://pypi.org/project/qoregeo) ·
[Documentation](docs/README.md) ·
[Roadmap](ROADMAP.md) ·
[Changelog](CHANGELOG.md) ·
[Issues](https://github.com/bosekarmegam/qoregeo/issues) ·
[suneelbosekarmegam@gmail.com](mailto:suneelbosekarmegam@gmail.com)

<sub>If QOREgeo saved you a GDAL install, a ⭐ helps other people find it.</sub>

</div>
