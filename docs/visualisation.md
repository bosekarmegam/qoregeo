<div align="center">

[← Analysis](analysis.md) · **Visualisation** · [Command Line →](cli.md)

</div>

---

# Visualisation

Interactive maps, heatmaps, choropleths, and static images that need no browser
at all.

---

## Which output do you want?

| Method | Output | Needs a browser | Needs the network | Best for |
|---|---|:---:|:---:|---|
| [`map()`](#interactive-maps) | HTML | to view | for the basemap | exploring, sharing |
| [`heatmap()`](#heatmaps) | HTML | to view | for the basemap | density |
| [`choropleth()`](#choropleths) | HTML | to view | for the basemap | value by area |
| [`svg()`](#static-images) | SVG | no | **no** | reports, print |
| [`png()`](#static-images) | PNG | no | **no** | READMEs, email, CI |

The HTML maps pull Leaflet from a CDN, with an automatic mirror fallback and a
readable error banner instead of a blank page if both are unreachable. The SVG
and PNG exports render entirely in Python.

---

<a name="interactive-maps"></a>
## Interactive maps

```python
geo.map("cities.html")
```
```
✅  Map saved → cities.html  (8 features)
```

One self-contained file. Open it, mail it, drop it in a bucket. There is no
server behind it.

Points, lines and polygons all render, in one layer, from one call.

### Styling by category

```python
geo.map("cities.html",
        title="Indian Cities",
        colour_by="state",         # a colour per distinct value, plus a legend
        tooltip_field="name",      # shown on hover
        basemap="dark")
```

`color_by` works too, if you prefer.

Colours come from a ten-entry colour-blind-safe palette and cycle beyond that.
Features whose `colour_by` property is missing get an `-` entry in the legend.

### Basemaps

```python
geo.map("out.html", basemap="satellite")
```

| Name | Style |
|---|---|
| `dark` *(default)* | CARTO dark, the house style |
| `light` | CARTO light, good for print |
| `streets` | OpenStreetMap standard |
| `terrain` | OpenTopoMap, contours and relief |
| `satellite` | Esri World Imagery |

Whichever you pick renders first; the rest are offered in a layer switcher, so
viewers can change it themselves.

### Clustering

```python
geo.map("out.html", cluster=True)      # force on
geo.map("out.html", cluster=False)     # force off
geo.map("out.html")                    # automatic above 750 features
```

Above `AUTO_CLUSTER_THRESHOLD` (750), markers group into counted bubbles that
expand as you zoom. Without it, ten thousand markers is an unreadable blob and a
slow page.

If the clustering plugin fails to load, the map falls back to plain markers
rather than failing.

### Popups and search

```python
geo.map("out.html", popup_fields=["name", "population"])   # restrict and order
geo.map("out.html", search=False)                          # hide the filter box
```

Popups show every property by default, except those starting with `_`, so
internal fields like `_distance`, `_cluster` and `_geohash` stay out of the way.
Reference them explicitly in `popup_fields` if you want them shown.

The search box filters live in the browser, dimming non-matches and updating the
feature count. It is plain JavaScript over the embedded data, no server, no
plugin.

### Everything together

```python
geo.map("stores.html",
        title="Store Network Q3",
        basemap="light",
        colour_by="segment",
        tooltip_field="store_name",
        popup_fields=["store_name", "segment", "revenue", "opened"],
        cluster=True,
        zoom=6)
```

---

<a name="heatmaps"></a>
## Heatmaps

```python
geo.heatmap("density.html")
geo.heatmap("sales.html", intensity_col="revenue")
```

Where points bunch up, weighted by a column if you give one. Values are
normalised to 0-1, so the gradient always uses its full range regardless of
your units.

```python
geo.heatmap("out.html",
            intensity_col="revenue",
            radius=35,        # heat radius in pixels (raise it for sparse data)
            blur=25,
            basemap="dark")
```

Non-point geometries contribute their centroid, so a mixed collection still
produces a sensible surface.

**Heatmap or hotspot grid?** A heatmap is for looking; `hotspots()` is for
counting. If you need the numbers, how many, exactly where, use
[`hotspots()`](analysis.md#density-and-hotspots) and render the result as a
choropleth.

---

<a name="choropleths"></a>
## Choropleths

Features shaded by a numeric property.

```python
districts.choropleth("population.html",
                     value_col="population",
                     label_col="district_name",
                     bins=5)
```

### Why quantiles

Classes are **quantiles**, not equal intervals: each colour holds roughly the
same number of features.

Equal intervals collapse to one colour the moment the data is skewed, and
spatial data is nearly always skewed. One metropolis with ten million people
next to fifty towns of twenty thousand gives you one dark polygon and fifty
identical pale ones. Quantiles keep the map readable.

Where values tie, classes collapse rather than producing an empty legend row
that can never contain anything. Ask for 5 bins on data with 3 distinct values
and you get 3 classes.

`bins` is clamped to 2-7. Beyond seven, readers cannot reliably tell the
colours apart.

### Works on points too

```python
geo.choropleth("cities.html", value_col="population", label_col="name")
```

Points become graduated circles using the same ramp and legend.

---

<a name="static-images"></a>
## Static images

For anywhere an HTML page is the wrong shape: a PDF report, an email, a README,
a CI artefact, a printed page.

```python
geo.svg("map.svg")
geo.png("map.png")
```

Neither needs a browser, a headless renderer, matplotlib, or a network
connection. The PNG encoder is built on `zlib` and `struct` from the standard
library.

```python
geo.svg("map.svg",
        title="Store Network",
        width=1600, height=900,
        theme="light",             # 'dark' (default) or 'light'
        label_field="name")        # draw a text label beside each point

geo.png("map.png", width=1200, height=800, theme="dark")
```

| | SVG | PNG |
|---|---|---|
| Type | vector | raster |
| Scales without blurring | ✅ | ✗ |
| Text labels | ✅ | ✗ |
| Diffs sensibly in git | ✅ | ✗ |
| Universally embeddable | mostly | ✅ |

Prefer SVG for print and documentation; PNG where SVG is not accepted.

Data is projected to Web Mercator, the same projection as web tiles, so
shapes match what the interactive map shows. There is no basemap imagery,
because fetching tiles would require the network these functions exist to avoid.

<details>
<summary>Rendering a map inside a notebook</summary>

`GeoEngine.svg()` writes a file and returns the engine, so display the file:

```python
from IPython.display import SVG, display

geo.svg("preview.svg", width=900, height=600)
display(SVG(filename="preview.svg"))
```

Or skip the file entirely. `build_svg` returns the source when you omit the
output path:

```python
from qoregeo import build_svg
from IPython.display import SVG, display

display(SVG(build_svg(geo.get_features(), title="Preview", width=900, height=600)))
```

</details>

---

## Saving by extension

`save()` reaches all three renderers:

```python
geo.save("out.html")     # interactive map, default options
geo.save("out.svg")      # static vector
geo.save("out.png")      # static raster
```

Handy in a loop over formats, or when the output path comes from a config file.

---

## Quiet mode

Every map method prints a confirmation line. In a script or a test, silence it:

```python
geo.map("out.html", quiet=True)
```

---

## Practical notes

**File size.** The whole dataset is embedded in the HTML. Ten thousand features
with rich properties makes a large page; `select()` down to the columns you
actually show first.

```python
geo.select(["name", "revenue"]).map("light.html")
```

**Offline viewers.** If the audience has no internet, send SVG or PNG. The HTML
maps need the CDN for Leaflet and the tile server for imagery.

**Reproducible output.** The generated HTML is deterministic for the same input
and options, so it diffs cleanly and is safe to commit as a fixture.

**Custom rendering.** The builders are plain functions if you want to wrap them:

```python
from qoregeo.map_builder import build_map, build_heatmap, build_choropleth, BASEMAPS
from qoregeo.static_map import build_svg, build_png, THEMES

build_map(geo.get_features(), "out.html", colour_by="state", quiet=True)
print(list(BASEMAPS))     # ['dark', 'light', 'streets', 'terrain', 'satellite']
```

---

## Worked example: a report pack

```python
from qoregeo import GeoEngine

stores = GeoEngine().load("stores.csv").clean()

# Interactive, for the team to explore
stores.map("reports/interactive.html",
           title="Store Network",
           colour_by="segment",
           tooltip_field="name",
           popup_fields=["name", "segment", "revenue"])

# Density, for the slide about coverage
stores.heatmap("reports/density.html", intensity_col="revenue")

# Static, for the PDF
stores.svg("reports/network.svg", title="Store Network", theme="light",
           width=1600, height=900)
stores.png("reports/network.png", theme="light")

# Per-region breakouts
for region in stores.unique("region"):
    subset = stores.filter("region", region)
    subset.map(f"reports/{region}.html", title=f"{region}, {subset.count()} stores")
```

---

## Next

| You want to… | Go to |
|---|---|
| Do this from a shell script | [Command Line](cli.md) |
| Look up an exact signature | [API Reference](api-reference.md) |
| Copy a finished solution | [Recipes](recipes.md) |

---

<div align="center">

[← Analysis](analysis.md) · [Documentation home](README.md) · [Command Line →](cli.md)

</div>
