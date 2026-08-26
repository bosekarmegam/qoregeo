<div align="center">

[← Visualisation](visualisation.md) · **Command Line** · [API Reference →](api-reference.md)

</div>

---

# Command Line

Spatial work without writing a script. Installing the package installs a
`qoregeo` command.

```bash
qoregeo --help
qoregeo --version        # qoregeo 1.1.0
```

If the console script is not on your `PATH`, `python -m qoregeo` is exactly
equivalent.

---

## Commands at a glance

| Command | Does |
|---|---|
| [`info`](#info) | summarise and validate a dataset |
| [`convert`](#convert) | translate between any supported formats |
| [`map`](#map) | render an interactive HTML map |
| [`heatmap`](#heatmap) | render a density heatmap |
| [`image`](#image) | render a static SVG or PNG |
| [`distance`](#distance) | distance and bearing between two coordinates |
| [`nearest`](#nearest) | find the closest features to a point |
| [`within`](#within) | features inside a radius |
| [`stats`](#stats) | summarise one column |
| [`geocode`](#geocode) | look up an address *(needs network)* |

---

## Shared options

Every command that reads a file accepts:

```
--lat-col NAME     latitude column name (CSV only)
--lng-col NAME     longitude column name (CSV only)
```

Every data command accepts:

```
-q, --query EXPR   filter before doing anything else
```

Several accept `--json` for machine-readable output.

---

<a name="info"></a>
## `info`: summarise a dataset

The first thing to run on a file you have not seen before.

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

Works on every readable format, including shapefiles:

```bash
qoregeo info districts.shp
qoregeo info track.gpx
qoregeo info places.kml
```

Broken data is reported rather than hidden:

```
Validity    3 invalid
            · feature 17: coordinate out of range: lat=128.4, lng=77.2
            · feature 42: Point has no coordinates
```

```bash
qoregeo info cities.csv --json      # the full describe() dict
```

---

<a name="convert"></a>
## `convert`: translate formats

```bash
qoregeo convert cities.csv cities.geojson
qoregeo convert districts.shp districts.geojson     # shapefile out of GDAL's world
qoregeo convert track.gpx track.csv
qoregeo convert cities.csv cities.kml
```

The extensions pick both the reader and the writer. Filter on the way through:

```bash
qoregeo convert stores.csv top.geojson -q "revenue > 500000 and region == 'West'"
```
```
✅  3 features → top.geojson
```

<details>
<summary>Converting a folder</summary>

```bash
for f in shapefiles/*.shp; do
  qoregeo convert "$f" "geojson/$(basename "${f%.shp}").geojson"
done
```

</details>

---

<a name="map"></a>
## `map`: interactive HTML

```bash
qoregeo map cities.csv -o cities.html
```

```
-o, --output PATH     output file (default map.html)
-t, --title TEXT      map title
    --basemap NAME    dark | light | streets | terrain | satellite
    --colour-by COL   colour features by this property (--color-by also works)
    --tooltip COL     property shown on hover
    --cluster         force marker clustering on
-q, --query EXPR      filter first
```

```bash
qoregeo map stores.csv -o network.html \
  --title "Store Network" \
  --basemap light \
  --colour-by segment \
  --tooltip name \
  --cluster \
  -q "status != 'closed'"
```

---

<a name="heatmap"></a>
## `heatmap`: density

```bash
qoregeo heatmap incidents.csv -o density.html
qoregeo heatmap sales.csv -o sales.html --intensity revenue --basemap dark
```

```
-o, --output PATH     output file (default heatmap.html)
-t, --title TEXT      map title
    --intensity COL   weight each point by this property
    --basemap NAME    dark | light | streets | terrain | satellite
-q, --query EXPR      filter first
```

---

<a name="image"></a>
## `image`: static SVG or PNG

No browser, no network. The extension of `--output` picks the renderer.

```bash
qoregeo image cities.csv -o map.png
qoregeo image cities.csv -o map.svg --label name
```

```
-o, --output PATH     .svg or .png (default map.svg)
-t, --title TEXT      image title
    --width N         pixels (default 1200)
    --height N        pixels (default 800)
    --theme NAME      dark | light
    --label COL       draw this property beside each point (SVG only)
-q, --query EXPR      filter first
```

Ideal in CI, generate a map image as a build artefact with nothing installed
but QOREgeo:

```bash
qoregeo image data/sites.csv -o artifacts/coverage.png --theme light --width 1600
```

---

<a name="distance"></a>
## `distance`: between two coordinates

No data file needed.

```bash
qoregeo distance 28.6139,77.2090 19.0760,72.8777
```
```
1148.0965 km  ·  South-Southwest (203.47°)
```

```
-u, --unit UNIT       km | miles | m | ft | nm
    --method NAME     haversine | vincenty
    --json            machine-readable
```

```bash
qoregeo distance 28.6139,77.2090 19.0760,72.8777 -u miles --method vincenty --json
```
```json
{
  "distance": 711.1755,
  "unit": "miles",
  "method": "vincenty",
  "bearing_degrees": 203.47,
  "direction": "South-Southwest"
}
```

Coordinates are `lat,lng` with no space. Quote them if your shell objects.

---

<a name="nearest"></a>
## `nearest`: closest features

```bash
qoregeo nearest hospitals.csv 28.6139,77.2090 -k 3
```
```
  1. AIIMS New Delhi,  1.42 km
  2. Safdarjung Hospital,  2.08 km
  3. RML Hospital,  2.91 km
```

```
-k N                  how many results (default 5)
-u, --unit UNIT       km | miles | m | ft | nm
    --json            machine-readable, with full properties
```

The label comes from a `name` property when there is one, otherwise the feature
index.

---

<a name="within"></a>
## `within`: inside a radius

```bash
qoregeo within customers.csv 19.0760,72.8777 25
```
```
1.0488 km  Mumbai
12.5103 km  Andheri
...
```

```
-u, --unit UNIT       km | miles | m | ft | nm
-o, --output PATH     save the result instead of printing it
```

```bash
qoregeo within customers.csv 19.0760,72.8777 25 -o nearby.geojson
```
```
✅  118 features within 25.0 km → nearby.geojson
```

---

<a name="stats"></a>
## `stats`: summarise a column

```bash
qoregeo stats cities.csv population
```
```
count   8
min     7400000.0
max     32900000.0
mean    14912500.0
median  12250000.0
sum     119300000.0
```

Text columns fall back to value counts automatically:

```bash
qoregeo stats cities.csv state
```
```
Maharashtra  2
Delhi        1
Gujarat      1
...
```

`--json` for machine output.

---

<a name="geocode"></a>
## `geocode`: address to coordinates

The only command that needs a network connection.

```bash
qoregeo geocode "India Gate, New Delhi" --user-agent "my-app/1.0 (me@example.com)"
```
```
28.6129, 77.2295    India Gate, New Delhi, Delhi, 110001, India
```

```
    --user-agent TEXT   required, identify your app, with contact details
    --country CODE      ISO 3166-1 alpha-2, e.g. in, us, gb
    --json              full result including address components
```

`--user-agent` is mandatory because the public Nominatim service rejects
anonymous clients. Include a real contact address. Requests are rate limited to
one per second, per its usage policy.

Exits `1` and prints to stderr when nothing matches.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | a QOREgeo error. The message explains it, on stderr |
| `2` | bad arguments, argparse usage message |
| `130` | interrupted with Ctrl-C |

Errors print the same guidance as the Python API:

```bash
qoregeo stats cities.csv nope
```
```
❌  QOREgeo. Column Not Found
────────────────────────────────────────────
File: 'cities.csv'
Could not find a 'nope' column.

Columns in your file:
    'name', 'state', 'population'
```

---

## Piping and scripting

`--json` makes the CLI composable:

```bash
# Nearest depot to each incident
qoregeo nearest depots.csv 28.61,77.20 -k 1 --json | jq -r '.[0].properties.name'

# Guard a build on data quality
if [ "$(qoregeo info sites.csv --json | jq '.features')" -lt 100 ]; then
  echo "Site file looks truncated" >&2
  exit 1
fi

# Nightly export
qoregeo convert /data/live.csv /exports/$(date +%F).geojson -q "status == 'active'"
```

<details>
<summary>A complete report script</summary>

```bash
#!/usr/bin/env bash
set -euo pipefail

SRC="data/stores.csv"
OUT="reports/$(date +%F)"
mkdir -p "$OUT"

qoregeo info    "$SRC" --json > "$OUT/summary.json"
qoregeo stats   "$SRC" revenue --json > "$OUT/revenue.json"
qoregeo map     "$SRC" -o "$OUT/map.html" --colour-by region --tooltip name
qoregeo heatmap "$SRC" -o "$OUT/density.html" --intensity revenue
qoregeo image   "$SRC" -o "$OUT/overview.png" --theme light --width 1600
qoregeo convert "$SRC" "$OUT/active.geojson" -q "status == 'active'"

echo "Report written to $OUT"
```

</details>

---

## Next

| You want to… | Go to |
|---|---|
| Use the Python API | [Getting Started](getting-started.md) |
| Look up an exact signature | [API Reference](api-reference.md) |
| Copy a finished solution | [Recipes](recipes.md) |

---

<div align="center">

[← Visualisation](visualisation.md) · [Documentation home](README.md) · [API Reference →](api-reference.md)

</div>
