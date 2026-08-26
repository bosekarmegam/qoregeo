<div align="center">

[← Recipes](recipes.md) · **FAQ &amp; Troubleshooting** · [Documentation home](README.md)

</div>

---

# FAQ & Troubleshooting

---

## Contents

[About the project](#about-the-project) ·
[Common errors](#common-errors) ·
[Data problems](#data-problems) ·
[Performance](#performance) ·
[Maps](#maps) ·
[Comparisons](#comparisons)

---

<a name="about-the-project"></a>
## About the project

### Is it really zero dependencies?

Yes. `qoregeo` imports nothing outside the Python standard library, and
`pyproject.toml` declares `dependencies = []`. Shapefiles are parsed with
`struct`, PNGs are encoded with `zlib`, XML uses `xml.etree`, and geocoding
uses `urllib`.

The one thing that reaches outside is the **interactive** map, whose HTML loads
Leaflet from a CDN in the viewer's browser. That is the viewer's network, not
your Python environment. `svg()` and `png()` need nothing at all.

### Which Python versions are supported?

3.8 through 3.13, on Windows, macOS and Linux. The test matrix covers every
combination.

3.8 is past end of life, and QOREgeo still supports it because the whole point
is running where other tools cannot. The floor rises in v2.0.

### Is it fast enough?

For the work most applications need, yes. A radius query over 50 000 indexed
points takes about 0.2 ms.

It is pure Python, so it will not match a C++ library on continent-scale raster
work or on a million-polygon overlay. Those are the jobs to keep the full stack
for. See [Comparisons](#comparisons).

### Why "quantum"?

The name comes from the QORE OS project the library belongs to. Quantum
spatial algorithms are a v2.0 research target, described honestly as a target
in the [roadmap](../ROADMAP.md). Nothing in v1.1 is quantum, and nothing claims
to be.

### How do I cite it?

The repository has a `CITATION.cff`, so GitHub's "Cite this repository" button
generates BibTeX and APA for you.

---

<a name="common-errors"></a>
## Common errors

### `NoDataError`

```
❌  QOREgeo: No Data Loaded
```

You called a method that needs data before loading any. Every filter returns a
**new** engine, so this is usually a lost result:

```python
geo = GeoEngine()
geo.load("cities.csv")
geo.query("population > 1e6")     # the result is discarded
geo.count()                        # still 8

filtered = geo.query("population > 1e6")   # keep it
```

### `ColumnNotFoundError`

The message lists the columns that do exist. Usually a typo, a case difference,
or an unexpected space in a CSV header.

```python
print(geo.columns())      # what is actually there
```

### `InvalidCoordinateError`

Latitude must be within ±90 and longitude within ±180. Three common causes:

1. **Arguments the wrong way round.** QOREgeo takes `(lat, lng)`; GeoJSON
   stores `[lng, lat]`.
2. **Projected coordinates.** A shapefile in metres has values like `715980`.
   Convert before treating them as degrees.
3. **The columns are swapped in the file.** Run `geo.validate()`, then
   `geo.fix_coordinates()`.

### `InvalidQueryError`

The message shows the grammar. The usual causes are a missing operator
(`population 1000000`), an unclosed bracket, or an operator the language does
not have. Full list: [Querying](querying.md#operators).

### `UnsupportedFormatError`

Either the extension is not supported, or the content does not match it. A
`.geojson` file containing a bare geometry rather than a `Feature` or
`FeatureCollection` raises this.

### `EmptyDatasetError`

The file loaded but produced no usable features. A CSV with only a header, a
CSV where every coordinate is blank, or an empty `FeatureCollection`.

### `ModuleNotFoundError: No module named 'qoregeo'`

The package installed into a different interpreter than the one you are
running. Check with `python -m pip show qoregeo`, and install with
`python -m pip install qoregeo` so the interpreter and the installer match.

### `qoregeo: command not found`

The console script directory is not on your `PATH`. `python -m qoregeo` is
exactly equivalent and always works.

---

<a name="data-problems"></a>
## Data problems

### My coordinates are in the sea off West Africa

Null Island, at `0, 0`. Rows whose coordinates failed to parse to a number
somewhere upstream. Find them:

```python
geo.query("latitude == 0 and longitude == 0")
```

QOREgeo skips unparseable coordinates on load with a warning rather than
turning them into zeros, so these usually arrive already broken.

### Everything is rotated or in the wrong hemisphere

Latitude and longitude are swapped.

```python
geo.validate()["likely_swapped"]     # how many are provably swapped
geo = geo.fix_coordinates()
```

`fix_coordinates()` only swaps where it is certain: latitude beyond ±90 with
longitude inside it. When both values are under 90 the swap is undetectable, so
check the map.

### My shapefile loads but the coordinates are huge

It is in a projected CRS, and QOREgeo does not read the `.prj` file. Values
like `715980, 3167204` are UTM metres.

```python
from qoregeo import from_utm
lat, lng = from_utm(715980, 3167204, zone=43, hemisphere="N")
```

For a whole file, reproject once with QGIS or `ogr2ogr` to EPSG:4326 and load
the result.

### Numbers from a CSV behave like text

They are text, because that is what a CSV holds. QOREgeo compares numerically
whenever both sides look numeric, so `query("pop > 1000000")` works. Where you
need a real number:

```python
geo.add_column("pop_num", lambda p: float(p["population"]))
geo.stats("population")     # already skips non-numeric values
```

### Duplicate records at almost the same place

Typical after geocoding.

```python
geo.dedupe(tolerance_km=0.05)                    # within 50 m
geo.dedupe(tolerance_km=0.05, subset=["name"])   # …and the same name
```

### `filter_by_radius` returns nothing

Check the argument order. It is `filter_by_radius(lat, lng, radius)`, with the
centre as two separate numbers rather than a tuple.

```python
geo.filter_by_radius(19.07, 72.87, radius=25)     # correct
```

---

<a name="performance"></a>
## Performance

### How do I make queries faster?

```python
geo.build_index()      # automatic at 500+ features
```

Then narrow cheaply before doing anything expensive: `filter_by_bbox()` before
`filter_by_radius()`, and `select()` away unused columns before rendering.

### Point-in-polygon is slow

Simplify the polygons. Boundary files carry far more vertices than any test
needs, and every vertex costs on every check.

```python
districts = districts.simplify(0.002)
```

### My map file is enormous

The whole dataset is embedded in the HTML. Drop the columns you do not show,
and simplify geometry:

```python
geo.select(["name", "value"]).simplify(0.005).map("light.html")
```

### Can I process a file too large for memory?

Not in one engine. NDJSON streams, so chunk it yourself:

```python
from qoregeo import GeoEngine
from qoregeo.formats import read_ndjson

results = []
for chunk_start in range(0, total, 50_000):
    ...
```

Out-of-core support is a v1.4 item on the [roadmap](../ROADMAP.md).

---

<a name="maps"></a>
## Maps

### The map is blank, or shows an error banner

The banner says which resource failed. Almost always no internet access in the
viewer's browser, so Leaflet could not load. Use `svg()` or `png()` for output
that needs no network.

### Tiles do not load but the markers do

Leaflet loaded; the tile server did not. A corporate proxy, an ad blocker, or a
tile host being unreachable. Try `basemap="streets"`, or use the layer switcher
in the top-left corner.

### Ten thousand markers make the page unusable

Clustering turns on automatically above 750 features. If you switched it off,
switch it back on:

```python
geo.map("out.html", cluster=True)
```

### My popup does not show a field

Property names beginning with `_` are hidden by default, since they are
internal columns like `_distance` and `_cluster`. Name them explicitly:

```python
geo.map("out.html", popup_fields=["name", "_distance"])
```

### Can I use my own tile server?

Add it to the basemap registry before rendering:

```python
from qoregeo.map_builder import BASEMAPS

BASEMAPS["mine"] = {
    "url": "https://tiles.example.com/{z}/{x}/{y}.png",
    "attribution": "© Example",
    "subdomains": "",
}
geo.map("out.html", basemap="mine")
```

---

<a name="comparisons"></a>
## Comparisons

### QOREgeo or GeoPandas?

They solve different problems.

| | QOREgeo | GeoPandas |
|---|---|---|
| Install | `pip install`, always works | needs GDAL, PROJ, GEOS |
| Dependencies | 0 | ~10, several native |
| Speed | pure Python | C-backed, much faster in bulk |
| Raster | no | via rasterio |
| Topology (union, dissolve) | not yet | full, via GEOS |
| CRS support | WGS84, Web Mercator, UTM | thousands, via PROJ |
| Shapefiles | reads, no GDAL | full read and write |
| Runs on Lambda or a Pi | yes | needs a layer or a build |

Use GeoPandas for heavy analytical work in an environment where you control the
install. Use QOREgeo when the install is the problem, when you need a small
dependency footprint, or when the spatial work is a feature of a larger
application rather than the whole job.

They coexist happily:

```python
import geopandas as gpd
from qoregeo import GeoEngine

gdf = gpd.read_file("complex.gpkg")
geo = GeoEngine.from_records(
    gdf.assign(lat=gdf.geometry.y, lng=gdf.geometry.x)
       .drop(columns="geometry").to_dict("records")
)
```

### QOREgeo or Shapely?

Shapely is a geometry engine with no I/O, no attributes and no maps, and it
needs GEOS. QOREgeo covers a narrower set of geometry operations with none of
the install cost, and adds loading, querying, analysis and rendering around it.

Shapely wins on topology: `union`, `difference`, `dissolve` and buffering with
proper joins. Those are on the QOREgeo roadmap for v1.3.

### QOREgeo or Folium?

Folium only makes maps, and does it very well with a large plugin ecosystem.
QOREgeo's map output is deliberately smaller in scope: a good default map with
no dependency. If maps are all you need and a dependency is fine, Folium has
more options.

### Can I use it commercially?

Yes. MIT licence, no attribution required in your product, no copyleft.

---

## Still stuck?

1. Read the error message in full. Every QOREgeo exception names the problem and
   shows a fix.
2. Run `geo.validate()` and `geo.describe()`. Most surprises are data, not code.
3. Search the [issues](https://github.com/bosekarmegam/qoregeo/issues).
4. Open a new issue with your Python version, your QOREgeo version, a few rows
   of sample data, and the full traceback.

---

<div align="center">

[← Recipes](recipes.md) · [Documentation home](README.md)

</div>
