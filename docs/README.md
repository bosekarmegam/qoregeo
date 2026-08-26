<div align="center">

<img src="../assets/qoregeo-mark.png" width="76" alt="QOREgeo">

# QOREgeo Documentation

**Spatial intelligence for Python. Zero dependencies.**

[Getting Started](getting-started.md) ·
[Loading Data](loading-data.md) ·
[Geometry](geometry.md) ·
[Querying](querying.md) ·
[Analysis](analysis.md) ·
[Visualisation](visualisation.md) ·
[CLI](cli.md) ·
[API](api-reference.md) ·
[Recipes](recipes.md) ·
[FAQ](faq.md)

</div>

---

## Start here

<table>
<tr>
<td width="50%" valign="top">

### 🚀 [Getting Started](getting-started.md)
Install, load a file, run your first query, export a map. Five minutes end to end.

</td>
<td width="50%" valign="top">

### 📂 [Loading &amp; Saving Data](loading-data.md)
CSV, GeoJSON, shapefiles, GPX, KML, WKT, NDJSON. Column detection, validation and cleaning.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 📐 [Geometry](geometry.md)
Distance, bearing, buffers, corridors, area, hulls, simplification, intersection.

</td>
<td width="50%" valign="top">

### 🔎 [Querying &amp; Filtering](querying.md)
The query language, spatial filters, and the DataFrame-style table operations.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 📊 [Analysis](analysis.md)
Clustering, spatial joins, hotspots, point-pattern statistics, route optimisation, indexing.

</td>
<td width="50%" valign="top">

### 🗺️ [Visualisation](visualisation.md)
Interactive maps, heatmaps, choropleths, and static SVG/PNG export.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### ⌨️ [Command Line](cli.md)
Every subcommand, flag and example for the `qoregeo` CLI.

</td>
<td width="50%" valign="top">

### 📖 [API Reference](api-reference.md)
Every public method, parameter and return type in one page.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🍳 [Recipes](recipes.md)
Complete worked solutions: territory analysis, delivery zones, route planning, data cleaning.

</td>
<td width="50%" valign="top">

### ❓ [FAQ &amp; Troubleshooting](faq.md)
Common errors, performance questions, and how QOREgeo compares to GeoPandas.

</td>
</tr>
</table>

---

## The 60-second version

```bash
pip install qoregeo
```

```python
from qoregeo import GeoEngine

geo = GeoEngine().load("cities.csv")

geo.distance((28.6139, 77.2090), (19.0760, 72.8777))   # 1148.0965 km
geo.query("population > 1e7").count()                   # 5
geo.knn((28.6139, 77.2090), k=3)                        # 3 nearest, with distances
geo.map("cities.html", colour_by="state")               # interactive map
```

---

## Core ideas

Four decisions shape the whole library. Knowing them makes the rest predictable.

**1 · Zero dependencies is the product.**
Everything is standard library. That is why `pip install qoregeo` works on a
Raspberry Pi, in a Lambda layer, and on a laptop without admin rights. No GDAL,
no PROJ, no GEOS, no compiler.

**2 · Coordinates are `(lat, lng)`; GeoJSON is `[lng, lat]`.**
Anything you *pass in* is `(latitude, longitude)`, the order people say out
loud. Anything stored in a geometry follows the GeoJSON spec and is
`[longitude, latitude]`. The library handles the conversion; you only need to
notice it when reading raw geometry dicts.

**3 · Filters return a new engine.**
`query()`, `filter()`, `head()` and friends return a **new** `GeoEngine`, so the
original is never modified. Methods that only write a file (`map()`, `save()`,
`png()`) return `self`, so chains keep flowing.

```python
big = geo.query("population > 1e7")   # new engine
geo.count()                            # unchanged
```

**4 · Everything is on a sphere.**
Distances, areas, buffers and clustering all use great-circle maths. Treating
latitude and longitude as a flat plane is the single most common source of
quietly wrong spatial results outside the tropics.

---

## Where to go next

- New to the library → **[Getting Started](getting-started.md)**
- Have a file and want it loaded → **[Loading &amp; Saving Data](loading-data.md)**
- Know what you want, need the signature → **[API Reference](api-reference.md)**
- Want a finished solution to copy → **[Recipes](recipes.md)**
- Hit an error → **[FAQ &amp; Troubleshooting](faq.md)**

---

<div align="center">

[⬅ Back to the project README](../README.md) ·
[Roadmap](../ROADMAP.md) ·
[Changelog](../CHANGELOG.md) ·
[Contributing](../CONTRIBUTING.md)

</div>
