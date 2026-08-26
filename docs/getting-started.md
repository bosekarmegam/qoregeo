<div align="center">

[← Documentation home](README.md) · **Getting Started** · [Loading Data →](loading-data.md)

</div>

---

# Getting Started

From nothing to an interactive map in five minutes.

---

## Install

```bash
pip install qoregeo
```

That is the whole installation. There is no GDAL to build, no PROJ to locate,
and no compiler involved. QOREgeo is pure Python and pulls in nothing else.

**Requires** Python 3.8 or newer.

<details>
<summary>Verify the install</summary>

```bash
qoregeo --version
# qoregeo 1.1.0

python -c "import qoregeo; print(qoregeo.__version__)"
# 1.1.0
```

If `qoregeo` is not found but the import works, the console script directory is
not on your `PATH`. Use `python -m qoregeo` instead, which is equivalent.

</details>

---

## 1 · Make a data file

Save this as `cities.csv`:

```csv
name,state,population,latitude,longitude
Delhi,Delhi,32900000,28.6139,77.2090
Mumbai,Maharashtra,20700000,19.0760,72.8777
Kolkata,West Bengal,14900000,22.5726,88.3639
Bangalore,Karnataka,13200000,12.9716,77.5946
Chennai,Tamil Nadu,11300000,13.0827,80.2707
Hyderabad,Telangana,10500000,17.3850,78.4867
Pune,Maharashtra,7400000,18.5204,73.8567
Ahmedabad,Gujarat,8400000,23.0225,72.5714
```

Every example on this page uses it.

---

## 2 · Load it

```python
from qoregeo import GeoEngine

geo = GeoEngine().load("cities.csv")

geo.count()      # 8
geo.columns()    # ['name', 'state', 'population']
geo
```
```
<GeoEngine [8 features]>
```

The latitude and longitude columns were found automatically. QOREgeo recognises
`latitude`, `lat`, `y`, `geo_lat` and several other spellings, see
[Loading Data](loading-data.md#column-detection) for the full list, and how to
override it when your file uses something unusual.

> **In a Jupyter notebook**, the engine renders as a table preview rather than a
> `repr` string. Just put `geo` on the last line of a cell.

---

## 3 · Measure something

```python
delhi  = (28.6139, 77.2090)
mumbai = (19.0760, 72.8777)

geo.distance(delhi, mumbai)                      # 1148.0965  (km)
geo.distance(delhi, mumbai, unit="miles")        # 713.3938
geo.distance(delhi, mumbai, method="vincenty")   # 1144.5264  (WGS84 ellipsoid)

geo.bearing(delhi, mumbai)                       # 'South-Southwest'
geo.bearing(delhi, mumbai, as_degrees=True)      # 203.47
```

Coordinates always go in as `(latitude, longitude)`. The order you would say
them out loud.

---

## 4 · Ask a question

```python
big = geo.query("population > 1.2e7")
[f["properties"]["name"] for f in big]
```
```
['Delhi', 'Mumbai', 'Kolkata', 'Bangalore']
```

The query language reads like a sentence and handles combinations:

```python
geo.query("state == 'Maharashtra' and population > 1e7")   # ['Mumbai']
geo.query("name contains 'a' and population < 1.2e7")      # Chennai, Hyderabad, Ahmedabad
geo.query("state in ('Delhi', 'Gujarat')")                 # Delhi, Ahmedabad
```

> **Safe by construction.** The query is parsed by a small hand-written parser,
> never `eval`'d, so a query string from a config file, a CLI argument or a web
> form cannot execute code. See [Querying](querying.md#safety).

---

## 5 · Ask a *spatial* question

```python
# The three closest cities to Delhi
for match in geo.knn(delhi, k=3):
    print(match["feature"]["properties"]["name"], match["distance"], "km")
```
```
Delhi 0.0 km
Ahmedabad 775.7108 km
Mumbai 1148.0965 km
```

```python
# Everything within 200 km of Mumbai, nearest first
nearby = geo.filter_by_radius(19.07, 72.87, radius=200)
[(f["properties"]["name"], f["properties"]["_distance"]) for f in nearby]
```
```
[('Mumbai', 1.0488), ('Pune', 120.5103)]
```

`filter_by_radius` sorts nearest-first and injects a `_distance` property, so
the ranking is ready to use immediately.

---

## 6 · Draw a geofence

```python
zone = geo.buffer(delhi, radius=10, unit="km")     # a GeoJSON Polygon

geo.point_in_polygon((28.65, 77.22), zone)         # True, inside
geo.point_in_polygon(mumbai, zone)                 # False, 1148 km away

geo.area(zone)                                     # 313.654785 km²
geo.area(zone, unit="acres")                       # 77505.784752
```

Buffers are geodesic: every point on the ring is exactly the requested distance
from the centre, on the sphere.

---

## 7 · Make a map

```python
geo.map("cities.html", title="Indian Cities", colour_by="state")
```
```
✅  Map saved → cities.html  (8 features)
```

Open `cities.html` in any browser. It is a single self-contained file with a
basemap switcher, a scale bar, popups, hover tooltips, a colour legend and a
live filter box. There is no server to run.

Need an image instead of a page?

```python
geo.png("cities.png")     # raster, encoded in pure Python
geo.svg("cities.svg")     # vector, prints at any size
```

Those two need **no browser and no network at all**, the right output for a
report, an email, a README or a CI artefact.

---

## 8 · Save the result

```python
geo.save("cities.geojson")
geo.save("cities.gpx")       # into a GPS device
geo.save("cities.kml")       # into Google Earth
geo.save("cities.ndjson")    # into a streaming pipeline
```

The extension picks the format. See [Loading Data](loading-data.md#saving) for
the full matrix.

---

## Putting it together

Every operation that narrows or transforms data returns a new engine, so this
reads top to bottom with no intermediate variables:

```python
from qoregeo import GeoEngine

(GeoEngine()
    .load("cities.csv")
    .query("population > 1e7")
    .sort_by("population", reverse=True)
    .add_column("millions", lambda p: round(float(p["population"]) / 1e6, 1))
    .save("large_cities.geojson")
    .map("large_cities.html", colour_by="state", tooltip_field="name"))
```

---

## The same thing from your shell

Installing the package also installs a `qoregeo` command:

```bash
qoregeo info cities.csv
```
```
File        cities.csv
Features    8
Geometry    Point × 8
Columns     name, state, population
Extent      lat 12.9716 … 28.6139   lng 72.5714 … 88.3639
Centre      19.4663, 77.6470   (mean spread 686.0125 km, max 1164.3778 km)
Validity    all valid
```

```bash
qoregeo distance 28.6139,77.2090 19.0760,72.8777
# 1148.0965 km  ·  South-Southwest (203.47°)

qoregeo map cities.csv -o map.html --colour-by state
qoregeo convert cities.csv cities.geojson -q "population > 1e7"
```

Full reference: **[Command Line](cli.md)**.

---

## When something goes wrong

QOREgeo errors are written to be read:

```python
geo.load("cities.csv", lat_col="lat")
```
```
❌  QOREgeo. Column Not Found
────────────────────────────────────────────
File: 'cities.csv'
Could not find a 'lat' column.

Columns in your file:
    'name', 'state', 'population', 'latitude', 'longitude'

Fix it:
    geo.load('cities.csv', lat_col='latitude', lng_col='longitude')
```

Every exception names the problem and shows the fix. If one does not, that is a
bug worth reporting.

---

## Next steps

| You want to… | Go to |
|---|---|
| Load a shapefile, GPX or KML | [Loading &amp; Saving Data](loading-data.md) |
| Measure areas, build corridors, simplify shapes | [Geometry](geometry.md) |
| Learn the full query language | [Querying &amp; Filtering](querying.md) |
| Cluster points, join to polygons, plan a route | [Analysis](analysis.md) |
| Style maps and export images | [Visualisation](visualisation.md) |
| Copy a finished solution | [Recipes](recipes.md) |

---

<div align="center">

[← Documentation home](README.md) · [Loading Data →](loading-data.md)

</div>
