# Load and save data

This page covers how data gets into a `GeoEngine` and back out again. It explains delimited text
files (CSV, TSV, TXT), GeoJSON files, Python objects and single points. It then covers reading
features back, saving to GeoJSON, CSV or TSV, and the warnings and errors you get from bad
files, with the real messages.

Every example runs as written, in a folder that holds copies of the sample files in
[docs/data](data/). Run them from a folder with those files and you get the same output.

**On this page:** [Quick reference](#quick-reference) ·
[CSV, TSV and TXT files](#load-a-csv-tsv-or-txt-file) ·
[GeoJSON files](#load-a-geojson-file) ·
[Python objects](#load-python-objects-with-load_data) ·
[Single points](#add-points-one-at-a-time) ·
[Reading features](#read-the-features-back) ·
[Saving](#save-to-geojson-csv-or-tsv) ·
[csv_safe](#spreadsheet-safe-csv-with-csv_safe) ·
[Atomic writes](#atomic-writes) ·
[Command line](#from-the-command-line) ·
[Errors and warnings](#errors-and-warnings)

## Quick reference

| You have | Call | What happens |
|---|---|---|
| A `.csv`, `.tsv` or `.txt` file | `geo.load(path)` | Latitude and longitude columns and the delimiter are detected |
| A `.geojson` or `.json` file | `geo.load(path)` | A FeatureCollection, Feature, bare Geometry or JSON list loads |
| GeoJSON dicts in memory | `geo.load_data(obj)` | Your objects are copied into the engine |
| Coordinates one at a time | `geo.add_point(lat, lng, **props)` | A Point feature is appended |
| An engine, and want the data | `get_features()`, `get_geojson()`, `for f in geo` | You get the engine's own dicts |
| An engine, and want a file | `geo.save(path)` | `.geojson`, `.json`, `.csv` or `.tsv` is written atomically |

`load()` and `load_data()` replace whatever the engine held. `add_point()` appends. All four
methods above that change or write data return the engine, so calls chain:
`GeoEngine().load("cities.csv").save("cities.geojson")`.

Coordinates follow two conventions. The Python API takes and returns `(lat, lng)`. GeoJSON
positions are `[lng, lat]`, as RFC 7946 requires, so a loaded row with latitude 28.6139 and
longitude 77.2090 becomes `"coordinates": [77.209, 28.6139]`.

## Load a CSV, TSV or TXT file

```python
import json
import warnings
from pathlib import Path

from qoregeo import GeoEngine

geo = GeoEngine().load("cities.csv")

print(geo)  # <GeoEngine [12 features]>
print(len(geo))  # 12

first = geo.get_features()[0]
print(first["geometry"])  # {'type': 'Point', 'coordinates': [77.209, 28.6139]}
print(first["properties"])  # {'name': 'Delhi', 'state': 'Delhi', 'population': '32900000'}
```

Each row becomes one GeoJSON Feature. The latitude and longitude columns become a `Point`
geometry, and every other column becomes a property. Values stay strings unless you pass
`infer_types=True` (see [Convert values with infer_types](#convert-values-with-infer_types)).
Column names are stripped of surrounding spaces.

`load(path, lat_col=None, lng_col=None, encoding="utf-8-sig", delimiter=None, infer_types=False)`

| Parameter | Default | What it does |
|---|---|---|
| `path` | required | A `str` or `pathlib.Path`. The extension picks the reader, in any case: `.csv`, `.tsv` and `.txt` are delimited text, `.geojson` and `.json` are GeoJSON. |
| `lat_col` | `None` | The latitude column. `None` detects it from the names below. |
| `lng_col` | `None` | The longitude column. `None` detects it. |
| `encoding` | `"utf-8-sig"` | The text encoding, for CSV and GeoJSON. The default reads plain UTF-8 and also strips the byte order mark that Excel writes. |
| `delimiter` | `None` | One character. `None` detects comma, semicolon, tab or pipe. |
| `infer_types` | `False` | Turn CSV cell text into `int`, `float`, `bool` or `None` where that is unambiguous. |

### Latitude and longitude column names

With no `lat_col` or `lng_col`, `load()` looks for these exact names. Case and surrounding
spaces are ignored, so `Lat`, ` LATITUDE ` and `lat` all match.

| Column | Names recognized, in priority order |
|---|---|
| Latitude | `latitude`, `lat`, `y`, `ylat`, `lat_deg`, `latitude_deg`, `geo_lat`, `point_lat` |
| Longitude | `longitude`, `lng`, `lon`, `long`, `x`, `xlong`, `lng_deg`, `longitude_deg`, `geo_lng`, `geo_long`, `point_lng` |

When a file has more than one of these names, the one earlier in the list wins. A file with the
columns `y`, `x`, `lat` and `lon` uses `lat` and `lon`, and `y` and `x` stay ordinary
properties. Names outside the list, such as `north` or `Latitude (deg)`, are not guessed.

```python
Path("depots.csv").write_text(
    "depot;Lat;Long\n"
    "Okhla;28.5355;77.2732\n"
    "Andheri;19.1136;72.8697\n",
    encoding="utf-8",
)
depots = GeoEngine().load("depots.csv")

print(depots.get_features()[0]["geometry"]["coordinates"])  # [77.2732, 28.5355]
print(depots.get_features()[0]["properties"])  # {'depot': 'Okhla'}
```

This file uses semicolons and the names `Lat` and `Long`. Both were detected.

### Choose the columns yourself

```python
Path("survey.csv").write_text(
    "point,north,east\n"
    "P1,28.6129,77.2295\n",
    encoding="utf-8",
)
survey = GeoEngine().load("survey.csv", lat_col="north", lng_col="east")
print(survey.get_features()[0]["geometry"]["coordinates"])  # [77.2295, 28.6129]

survey = GeoEngine().load("survey.csv", lat_col="NORTH", lng_col="East")
print(survey.count())  # 1
```

An exact name is tried first, then a case-insensitive match. If you pass only one of the two,
the other is still detected from the table above. When no column matches, `load()` raises
`ColumnNotFoundError`, a subclass of the built-in `LookupError`:

```python
from qoregeo import ColumnNotFoundError

try:
    GeoEngine().load("survey.csv")
except ColumnNotFoundError as err:
    error = err

print(error.column)  # latitude
print(error.available)  # ['point', 'north', 'east']
print(isinstance(error, LookupError))  # True
```

The message lists the columns the file has and a call to copy:

```text
<<ColumnNotFoundError survey.csv>>
```

When you pass a name that is close to a real one, the suggested call uses the real name. Here
`lat_col="nort"` was passed:

```text
<<ColumnNotFoundError nort>>
```

### Delimiters

```python
Path("depots.txt").write_text("name|lat|lng\nOkhla|28.5355|77.2732\n", encoding="utf-8")
print(GeoEngine().load("depots.txt").count())  # 1

Path("excel.csv").write_text("sep=;\nname;lat;lng\nOkhla;28.5355;77.2732\n", encoding="utf-8")
print(GeoEngine().load("excel.csv").get_features()[0]["properties"])  # {'name': 'Okhla'}

Path("colon.csv").write_text("name:lat:lng\nOkhla:28.5355:77.2732\n", encoding="utf-8")
print(GeoEngine().load("colon.csv", delimiter=":").count())  # 1
```

How the delimiter is chosen when `delimiter=None`:

- A `.tsv` file is always tab-separated.
- An Excel `sep=;` first line sets the delimiter, and the line is skipped.
- Otherwise qoregeo reads the header and up to 20 more lines and tries comma, semicolon, tab and
  pipe. It picks the one that keeps the most rows the same width as the header, then the one that
  splits the header into the most columns. Quoted fields are honored, so a comma inside
  `"Kochi, Kerala"` does not count.
- For a `.txt` file, tab wins a tie. For other files, comma wins a tie.
- Blank lines before the header are skipped.

Pass `delimiter=` for anything else. It must be a single character other than a quote, `\r` or
`\n`; anything else raises `InvalidArgumentError`.

Coordinates must use a dot as the decimal separator. A cell such as `28,5355` is not a number
to qoregeo, so that row is skipped with the warning described in
[Rows that are skipped](#rows-that-are-skipped).

### Encodings

```python
Path("cafes.csv").write_bytes("name,lat,lng\nCafé Lota,28.6127,77.2426\n".encode("cp1252"))

from qoregeo import FileReadError

try:
    GeoEngine().load("cafes.csv")
except FileReadError as err:
    error = err

print(error.reason)  # the file is not valid utf-8-sig text (invalid continuation byte at byte 16)

cafes = GeoEngine().load("cafes.csv", encoding="cp1252")
print(cafes.get_features()[0]["properties"]["name"])  # Café Lota
```

The default, `utf-8-sig`, reads UTF-8 files with or without a byte order mark. Keep it for UTF-8
files: with `encoding="utf-8"`, a file saved by Excel keeps the mark at the start of its first
column name, so a first column called `lat` is no longer found. For files from older Windows
programs, try `cp1252` or `latin-1`. The encoding must be a Python text codec name; a name such
as `"utf-9"` raises `InvalidArgumentError` before the file is opened.

### Convert values with infer_types

```python
hospitals = GeoEngine().load("hospitals.csv")
print(hospitals.get_features()[2]["properties"])  # {'name': 'Lodhi Gardens Clinic', 'beds': '40', 'emergency': 'false'}

hospitals = GeoEngine().load("hospitals.csv", infer_types=True)
print(hospitals.get_features()[2]["properties"])  # {'name': 'Lodhi Gardens Clinic', 'beds': 40, 'emergency': False}
print(hospitals.filter("emergency", True).count())  # 7
```

`infer_types=True` converts a cell only when the result is unambiguous:

| Cell text | Value with `infer_types=True` |
|---|---|
| `40`, `-7` | `40`, `-7` (`int`) |
| `4.5`, `19.90`, `1e3`, `.5` | `4.5`, `19.9`, `1000.0`, `0.5` (`float`) |
| `true`, `FALSE` (any case) | `True`, `False` |
| empty cell | `None` |
| `011001` (a leading zero, as in PIN codes and IDs) | stays `'011001'` |
| `+911123456789` (a leading `+`, as in phone numbers) | stays a string |
| `0.12345678901234567` (more than 15 significant digits) | stays a string, so no digits are lost |
| `n/a`, `twelve`, `yes`, `nan` | stays a string |

```python
Path("types.csv").write_text(
    "pin,phone,beds,rating,open,note,lat,lng\n"
    "011001,+911123456789,40,4.5,TRUE,,28.6,77.2\n",
    encoding="utf-8",
)
row = GeoEngine().load("types.csv", infer_types=True).get_features()[0]["properties"]
print(row)  # {'pin': '011001', 'phone': '+911123456789', 'beds': 40, 'rating': 4.5, 'open': True, 'note': None}
```

You do not need `infer_types` for `filter()` or `sort_by()`. They compare number-like strings as
numbers, so `filter("beds", 300, op=">=")` finds 4 hospitals either way. Use it when your own
code does arithmetic on values, when you want real `true`/`false` values, and when you save to
GeoJSON and want numbers to stay numbers. `infer_types` applies to delimited text only. GeoJSON
values already have JSON types.

### Rows that are skipped

`messy.csv` has four kinds of trouble. Salt Lake Depot has blank coordinates, Guwahati Depot has
its latitude and longitude swapped (latitude 91.7362), Velachery Depot has the latitude `n/a`,
and Whitefield Depot has `twelve` staff.

```python
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    messy = GeoEngine().load("messy.csv")

print(len(messy))  # 4
print(len(caught))  # 1
message = str(caught[0].message)
print(message[:message.index(" with")])  # QOREgeo: skipped 2 row(s) in 'messy.csv'
print(message[message.index("line 6"):])  # line 6 (lat='91.7362', lng='26.1445'), line 8 (lat='n/a', lng='80.2209')

print([f["properties"]["name"] for f in messy])  # ['Okhla Depot', 'Andheri Depot', 'Whitefield Depot', 'Gachibowli Depot']
print(messy.filter("staff", 10, op=">").count())  # 2
```

The full warning, a `UserWarning`:

```text
<<messy warning>>
```

- A row with a blank latitude or longitude is skipped without a warning. A row with fewer values
  than the header counts as blank.
- A row whose coordinates are not numbers, or are outside -90 to 90 (latitude) and -180 to 180
  (longitude), is skipped. One warning per file lists the first 5 such lines.
- Whitefield Depot loads, because its coordinates are fine. A numeric comparison skips its
  `twelve`, which is why `filter("staff", 10, op=">")` returns 2 and not 3.
- Line numbers count from the top of the file, header included.

Swapped coordinates are caught only when the swapped latitude falls outside -90 to 90. In India,
a swapped pair such as (77.2090, 28.6139) is a valid point in the Arctic, so look at a map or
at `geo.bbox()` after loading data from a new source.

To stop on the first problem in a pipeline, turn qoregeo's warnings into errors:

```python
geo = GeoEngine()
with warnings.catch_warnings():
    warnings.filterwarnings("error", message="QOREgeo")
    try:
        geo.load("messy.csv")
    except UserWarning as warning:
        stopped = warning

print(type(stopped).__name__)  # UserWarning
print(geo)  # <GeoEngine [no data loaded]>
```

Every warning qoregeo raises starts with `QOREgeo:`, so the `message="QOREgeo"` filter matches
all of them and nothing else. The other load warnings for delimited text:

| Situation | What happens |
|---|---|
| Two columns share a name | The last column with that name is kept, with a warning |
| A row has more values than the header | The extra values are dropped, with one warning per file |
| A `geometry` column cell is not valid GeoJSON | The row's latitude and longitude are used instead, with a warning |
| The file has a header and no usable rows | `EmptyDatasetError` |

### Lines and polygons in a CSV

A CSV file can hold more than points. If it has a column named `geometry` whose cells hold
GeoJSON geometries, those geometries are loaded instead of a Point. Cells that are empty fall
back to the row's latitude and longitude. This is the format `save()` writes for lines and
polygons, so they survive a round trip through CSV
(see [Save to GeoJSON, CSV or TSV](#save-to-geojson-csv-or-tsv)).

## Load a GeoJSON file

```python
zones = GeoEngine().load("zones.geojson")
print(zones)  # <GeoEngine [3 features]>
print([f["properties"]["name"] for f in zones])  # ['Central', 'South', 'West']
print([f["geometry"]["type"] for f in zones])  # ['Polygon', 'Polygon', 'Polygon']

route = GeoEngine().load("route.geojson")
line = route.get_features()[0]
print(line["geometry"]["type"], len(line["geometry"]["coordinates"]))  # LineString 13
print(round(route.length(line), 1))  # 1266.2
```

All seven RFC 7946 geometry types load: `Point`, `MultiPoint`, `LineString`, `MultiLineString`,
`Polygon`, `MultiPolygon` and `GeometryCollection`. The `encoding` argument applies here too.
GeoJSON should be UTF-8, which the default reads.

The file can hold any of four shapes:

```python
india_gate = {"type": "Point", "coordinates": [77.2295, 28.6129]}
shapes = {
    "collection.geojson": {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": india_gate, "properties": {"name": "India Gate"}},
    ]},
    "feature.geojson": {"type": "Feature", "geometry": india_gate, "properties": {"name": "India Gate"}},
    "geometry.geojson": india_gate,
    "list.json": [india_gate, india_gate],
}
counts = {}
for name, obj in shapes.items():
    Path(name).write_text(json.dumps(obj), encoding="utf-8")
    counts[name] = GeoEngine().load(name).count()

print(counts)  # {'collection.geojson': 1, 'feature.geojson': 1, 'geometry.geojson': 1, 'list.json': 2}
print(GeoEngine().load("geometry.geojson").get_features()[0])  # {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [77.2295, 28.6129]}, 'properties': {}}
```

A bare geometry is wrapped in a Feature with empty properties. A list may mix Features and
geometries. A Feature with `"properties": null` gets `{}`. A Feature whose `type` is missing or
spelled `"feature"` is accepted when it has a `geometry` or `properties` member.

### Null and invalid geometries

```python
Path("sites.geojson").write_text(json.dumps({
    "type": "FeatureCollection",
    "name": "sites",
    "features": [
        {"type": "Feature", "geometry": india_gate, "properties": {"name": "India Gate"}},
        {"type": "Feature", "geometry": None, "properties": {"name": "Head office"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [26.1445, 91.7362]},
         "properties": {"name": "Guwahati, swapped"}},
        {"type": "Feature", "geometry": {"type": "Circle", "coordinates": [77.2, 28.6]},
         "properties": {"name": "Not GeoJSON"}},
    ],
}), encoding="utf-8")

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    sites = GeoEngine().load("sites.geojson")

print(len(sites))  # 2
print([f["properties"]["name"] for f in sites])  # ['India Gate', 'Head office']
print(sites.get_features()[1]["geometry"])  # None
print(sites.centroid())  # (28.6129, 77.2295)
print(sites.get_geojson()["name"])  # sites
print(len(caught))  # 1
```

The warning names each skipped feature by its index in the file:

```text
<<sites warning>>
```

- A feature with `"geometry": null` is kept with its properties. It counts in `len()`, but it
  has no location, so spatial methods such as `centroid()`, `nearest()` and the filters skip it.
- A feature with a broken geometry is skipped. Broken means an unknown type, wrong nesting,
  non-numeric or non-finite positions, a latitude outside -90 to 90, or a ring with fewer than 3
  positions. One warning lists the first 5.
- A `Point` longitude must be within -180 to 180. Other geometries may use -360 to 360, so
  rings drawn across the antimeridian load.
- Extra top-level members such as `"name"` are kept and written back by `save()`. A top-level
  `"bbox"` is dropped, because it would go stale after a filter. Call `geo.bbox()` for a fresh one.
- Members of each Feature, such as `"id"`, are kept as they are.

## Load Python objects with load_data

```python
features = [
    {"type": "Feature",
     "geometry": {"type": "Point", "coordinates": [77.2295, 28.6129]},
     "properties": {"name": "India Gate"}},
    {"type": "Feature",
     "geometry": {"type": "Point", "coordinates": [72.8347, 18.9220]},
     "properties": {"name": "Gateway of India"}},
]
monuments = GeoEngine().load_data(features)
print(monuments)  # <GeoEngine [2 features]>

features[0]["properties"]["name"] = "changed"
print(monuments.get_features()[0]["properties"]["name"])  # India Gate
```

`load_data()` takes the same shapes as a GeoJSON file, as Python objects: a list of Features or
geometries, a FeatureCollection dict, a single Feature, a bare geometry, or any iterable of
Features, such as a generator. It copies each geometry and each properties dict, so changing
your objects afterwards does not change the engine. Values nested inside properties, such as a
list, are shared.

Build features from rows you already have, for example from a database query. Remember that
GeoJSON wants `[lng, lat]`:

```python
rows = [("Delhi", 28.6139, 77.2090), ("Mumbai", 19.0760, 72.8777)]
metros = GeoEngine().load_data(
    {"type": "Feature",
     "geometry": {"type": "Point", "coordinates": [lng, lat]},
     "properties": {"name": name}}
    for name, lat, lng in rows
)
print(metros.count())  # 2

copy = GeoEngine().load_data(zones.get_geojson())
print(copy)  # <GeoEngine [3 features]>
```

Plain `(lat, lng)` tuples are not GeoJSON, so `load_data()` rejects them. Use `add_point()` for
those:

```python
from qoregeo import InvalidGeoJSONError

try:
    GeoEngine().load_data([(28.6129, 77.2295)])
except InvalidGeoJSONError as err:
    error = err

print(error.reason)  # item #0 is a tuple, not a dict
```

An empty list, or `None`, raises `EmptyDatasetError`. Invalid geometries are skipped with the
same warning as in a file.

## Add points one at a time

```python
trip = (GeoEngine()
        .add_point(28.6129, 77.2295, name="India Gate", stop=1)
        .add_point(28.5245, 77.1855, name="Qutub Minar", stop=2))

print(trip)  # <GeoEngine [2 features]>
print(trip.get_features()[1])  # {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [77.1855, 28.5245]}, 'properties': {'name': 'Qutub Minar', 'stop': 2}}

more = GeoEngine().load("cities.csv").add_point(15.4909, 73.8278, name="Panaji", state="Goa")
print(len(more))  # 13
```

`add_point(lat, lng, **properties)` appends one Point. It creates the dataset when nothing is
loaded, and adds to it otherwise. Keyword arguments become properties, with their Python types.
For a property name that is not a Python identifier, unpack a dict:
`add_point(28.6, 77.2, **{"opening hours": "09:00 to 18:00"})`.

The coordinates are checked before anything is added:

```python
from qoregeo import InvalidCoordinateError

try:
    trip.add_point(91.7362, 26.1445, name="Guwahati, swapped")
except InvalidCoordinateError as err:
    error = err

print(error.lat, error.lng)  # 91.7362 26.1445
print(len(trip))  # 2
```

## Read the features back

| Call | Returns | Notes |
|---|---|---|
| `geo.count()`, `len(geo)` | `int` | `0` when nothing is loaded |
| `bool(geo)` | `bool` | `False` for an engine with no features |
| `for feature in geo:` | Feature dicts | Iterates over a snapshot of the list |
| `geo.get_features()` | `list` of Feature dicts | The engine's own list; `[]` after a filter that matched nothing |
| `geo.get_geojson()` | FeatureCollection `dict` | The engine's own dict |
| `repr(geo)` | `str` | `<GeoEngine [12 features]>` or `<GeoEngine [no data loaded]>` |

```python
cities = GeoEngine().load("cities.csv", infer_types=True)
print(cities.count(), len(cities))  # 12 12
print(GeoEngine().count())  # 0

names = [feature["properties"]["name"] for feature in cities]
print(names[:3])  # ['Delhi', 'Mumbai', 'Bangalore']

collection = cities.get_geojson()
print(collection["type"], len(collection["features"]))  # FeatureCollection 12
```

`get_features()`, `get_geojson()` and iteration hand you the engine's own dicts, not copies.
That makes it cheap to add a computed property:

```python
for feature in cities:
    props = feature["properties"]
    props["millions"] = round(props["population"] / 1_000_000, 1)

print(cities.get_features()[0]["properties"])  # {'name': 'Delhi', 'state': 'Delhi', 'population': 32900000, 'millions': 32.9}
```

Filters return a new engine whose feature and properties dicts are copies, so editing a filtered
result leaves the original alone:

```python
gujarat = cities.filter("state", "Gujarat")
gujarat.get_features()[0]["properties"]["name"] = "Amdavad"
print(cities.get_features()[7]["properties"]["name"])  # Ahmedabad

goa = cities.filter("state", "Goa")
print(goa, bool(goa))  # <GeoEngine [0 features]> False
print(goa.get_features())  # []
```

`get_features()` and `get_geojson()` raise `NoDataError` (a `RuntimeError`) when nothing was
ever loaded. `count()` and `len()` return 0 instead. See [queries](queries.md) for filters and
sorting.

## Save to GeoJSON, CSV or TSV

```python
big = GeoEngine().load("cities.csv", infer_types=True).filter("population", 10_000_000, op=">")
big.save("big.geojson").save("big.csv").save("big.tsv")

lines = Path("big.csv").read_text(encoding="utf-8").splitlines()
print(lines[0])  # latitude,longitude,name,state,population
print(lines[1])  # 28.6139,77.209,Delhi,Delhi,32900000
print(len(lines))  # 7

saved = json.loads(Path("big.geojson").read_text(encoding="utf-8"))
print(saved["features"][0]["properties"])  # {'name': 'Delhi', 'state': 'Delhi', 'population': 32900000}
```

`save(path, encoding="utf-8", csv_safe=False)`

| Parameter | Default | What it does |
|---|---|---|
| `path` | required | A `str` or `pathlib.Path` ending in `.geojson`, `.json`, `.csv` or `.tsv` (any case). Missing parent folders are created. |
| `encoding` | `"utf-8"` | The text encoding of the file. |
| `csv_safe` | `False` | For CSV and TSV, escape cells that a spreadsheet would run as a formula. See [csv_safe](#spreadsheet-safe-csv-with-csv_safe). |

The extension picks the format. `.txt` loads but does not save; any other extension raises
`UnsupportedFormatError`:

```python
from qoregeo import UnsupportedFormatError

try:
    big.save("big.txt")
except UnsupportedFormatError as err:
    error = err

print(error.ext)  # .txt

big.save("exports/2026/big.geojson")
print(Path("exports/2026/big.geojson").is_file())  # True

cities.filter("state", "Goa").save("goa.geojson")
print(json.loads(Path("goa.geojson").read_text(encoding="utf-8")))  # {'type': 'FeatureCollection', 'features': []}
```

An empty result still writes a valid file: an empty FeatureCollection, or a CSV with only the
`latitude,longitude` header.

### What each format keeps

| | GeoJSON (`.geojson`, `.json`) | CSV (`.csv`) and TSV (`.tsv`) |
|---|---|---|
| Point geometry | Kept | `latitude` and `longitude`, the first two columns |
| Line and polygon geometry | Kept | A representative point in `latitude` and `longitude`, plus a `geometry` column with the full GeoJSON |
| `"geometry": null` | Kept as `null` | Empty coordinate cells; the row is skipped when the file is loaded again |
| Properties | JSON types, in a 2-space indented file | Text, one column per property in first-seen order |
| Lists and dicts in properties | Kept | Written as JSON text |
| `None` | `null` | An empty cell |
| `NaN`, `Infinity` | `null` (they are not valid JSON) | `nan`, `inf` |
| Dates, `Decimal`, sets, tuples | ISO strings, numbers, sorted lists, lists | Their text form |
| Top-level members and Feature `"id"` | Kept | Dropped |
| A character the encoding cannot store | `UnicodeEncodeError`; the old file is kept | Written as a backslash escape such as `\xe9` |
| Line endings | `\n` | `\r\n`, as Python's `csv` module writes |

The representative point is the area centroid for polygons and the mean of the vertices for
other geometries.

```python
import datetime
import math

log = GeoEngine().add_point(28.6129, 77.2295, seen=datetime.date(2026, 3, 14),
                            speed=math.nan, tags={"stop", "gate"})
log.save("log.geojson")
print(json.loads(Path("log.geojson").read_text(encoding="utf-8"))["features"][0]["properties"])  # {'seen': '2026-03-14', 'speed': None, 'tags': ['gate', 'stop']}

zones.save("zones.csv")
print(Path("zones.csv").read_text(encoding="utf-8").splitlines()[0])  # latitude,longitude,geometry,name,fee,note

back = GeoEngine().load("zones.csv", infer_types=True)
print([f["geometry"]["type"] for f in back])  # ['Polygon', 'Polygon', 'Polygon']
print(round(back.area(back.get_features()[0]), 2))  # 31.52
```

The polygons came back whole, so the Central zone still measures 31.52 km² with its hole. A
property named `latitude` or `longitude`, or `geometry` when that column is written, would clash
with the coordinate columns. It is left out of the CSV with a warning. Filters that measure
distance add a `_distance` property, and it is saved like any other.

## Spreadsheet-safe CSV with csv_safe

Excel, LibreOffice and Google Sheets treat a cell that starts with `=`, `+`, `-` or `@` as a
formula. A CSV file built from data you did not write can carry a formula that runs when someone
opens the file, which is called CSV or formula injection. `csv_safe=True` puts a `'` in front of
such text cells so the spreadsheet shows them as text.

```python
shared = GeoEngine()
shared.add_point(28.6315, 77.2167, name='=HYPERLINK("http://example.com","Click")', change="-12.5")
shared.add_point(19.0760, 72.8777, name="@desk", change="-3")
shared.save("raw.csv")
shared.save("safe.csv", csv_safe=True)

raw = Path("raw.csv").read_text(encoding="utf-8").splitlines()
safe = Path("safe.csv").read_text(encoding="utf-8").splitlines()
print(raw[1])  # 28.6315,77.2167,"=HYPERLINK(""http://example.com"",""Click"")",-12.5
print(safe[1])  # 28.6315,77.2167,"'=HYPERLINK(""http://example.com"",""Click"")",-12.5
print(safe[2])  # 19.076,72.8777,'@desk,-3
```

- Text cells starting with `=`, `+`, `-`, `@`, a tab or a carriage return get the `'`.
- Cells that are numbers, such as `-12.5`, are left alone, and so are the coordinates.
- Header cells are checked too, because property names can come from untrusted data.
- The `'` is part of the file. Loading `safe.csv` again gives the name `'@desk`.

```python
print(GeoEngine().load("safe.csv").get_features()[1]["properties"]["name"])  # '@desk
```

Use `csv_safe=True` for files people open in a spreadsheet. Keep a GeoJSON file, or a CSV saved
without it, for data you load again. `qoregeo export` turns this escaping on by default (see
[From the command line](#from-the-command-line)).

## Atomic writes

`save()` writes to a temporary file in the target folder and then renames it over the target in
one step. A save that fails part way leaves the old file as it was:

```python
import os

cities.save("cities.geojson")
size = os.path.getsize("cities.geojson")

cafe = GeoEngine().add_point(28.6127, 77.2426, name="Café Lota")
try:
    cafe.save("cities.geojson", encoding="ascii")
except UnicodeEncodeError as err:
    error = err

print(type(error).__name__)  # UnicodeEncodeError
print(os.path.getsize("cities.geojson") == size)  # True
print(GeoEngine().load("cities.geojson").count())  # 12
```

- The temporary file is named `.qg-<random>.tmp` and is removed when the write fails.
- A symlink is followed, so the file it points to is updated and the link stays.
- A read-only target raises `PermissionError`, as `open(path, "w")` would.
- A file with more than one hard link, or a folder where a temporary file cannot be created, is
  written in place instead, so those writes are not atomic.

The same writer saves maps, heatmaps and images.

### Writing over the file you loaded

`save()` writes wherever you point it, and that includes the file you loaded:

```python
import shutil

from qoregeo import InvalidArgumentError

shutil.copy("cities.csv", "draft.csv")
draft = GeoEngine().load("draft.csv")

try:
    draft.map("draft.csv")
except InvalidArgumentError as err:
    error = err

print(error.name)  # output_path

draft.filter("state", "Gujarat").save("draft.csv")
print(GeoEngine().load("draft.csv").count())  # 2
```

`map()`, `heatmap()` and `image()` refuse the path of the loaded file, because an HTML page or
an image written there would destroy your data. `save()` has no such guard, so the last call
replaced the 12 rows of `draft.csv` with the 2 Gujarat rows. The check compares real paths, so
`./draft.csv` and an absolute path to the same file are refused too.

`qoregeo export` refuses to write over its input file, and so does the `export_data` tool of
the [MCP server](ai-integration.md). In Python, write results to a new file name.

## From the command line

```bash
qoregeo info cities.csv
qoregeo info zones.geojson
qoregeo export cities.csv -o big.geojson --where "population>=10000000"
qoregeo export stores.csv -o delhi.csv --where "city==Delhi" --sort revenue --desc
qoregeo export stores.csv -o delhi_raw.csv --where "city==Delhi" --raw-csv
```

`qoregeo info cities.csv` prints:

```text
<<info cities>>
```

| Flag | Commands | What it does |
|---|---|---|
| `--lat-col NAME`, `--lng-col NAME` | `info`, `export`, `map`, `image` | Set the coordinate columns, like `lat_col` and `lng_col` |
| `-o FILE`, `--output FILE` | `export` (required) | `.csv`, `.tsv`, `.geojson` or `.json` |
| `--raw-csv` | `export` | Write CSV without the formula escaping that is on by default |
| `--near`, `--bbox`, `--within`, `--where`, `--sort`, `--desc`, `--limit` | `export`, `map`, `image` | Filter before writing; see [the CLI page](cli.md) |

The command line always loads with `infer_types=True`, detects the delimiter and reads UTF-8
with or without a byte order mark. It has no delimiter or encoding flags. For another encoding,
load the file in Python and save a UTF-8 copy. Problems in the file are printed as the same
warnings and messages, and the exit status is 1. `qoregeo export` with `-o` set to the input
file stops with exit status 2.

## Errors and warnings

Every exception qoregeo raises derives from `QOREgeoError` and from the closest built-in, so
`except FileNotFoundError` or `except ValueError` keeps working:

```python
from qoregeo import QOREgeoError

Path("notes.docx").write_text("not a data file", encoding="utf-8")

problems = {}
for name in ["missing.csv", "notes.docx", "survey.csv", "messy.csv"]:
    try:
        GeoEngine().load(name)
    except QOREgeoError as err:
        problems[name] = type(err).__name__
    except UserWarning:
        problems[name] = "warning only"

print(problems)  # {'missing.csv': 'GeoFileNotFoundError', 'notes.docx': 'UnsupportedFormatError', 'survey.csv': 'ColumnNotFoundError'}
```

`messy.csv` is missing from the result because it loads (with a warning) and raises nothing.

| Problem | Exception | Also a | Attributes |
|---|---|---|---|
| The file does not exist, or is a folder | `GeoFileNotFoundError` | `FileNotFoundError` | `.path` |
| The extension is not supported, by `load()` or `save()` | `UnsupportedFormatError` | `ValueError` | `.ext` |
| No latitude or longitude column was found | `ColumnNotFoundError` | `LookupError` | `.column`, `.available`, `.filepath` |
| The bytes do not decode with `encoding`, or the CSV cannot be parsed | `FileReadError` | `ValueError` | `.path`, `.reason` |
| The file is not JSON, or is JSON but not GeoJSON | `InvalidGeoJSONError` | `UnsupportedFormatError` | `.source`, `.reason` |
| The file or object has no usable features | `EmptyDatasetError` | `ValueError` | `.path` |
| A bad `delimiter`, `encoding` or path argument, or a map written over the loaded file | `InvalidArgumentError` | `ValueError` | `.name`, `.value`, `.expected` |
| `add_point()` got a latitude or longitude that is not a number or out of range | `InvalidCoordinateError` | `ValueError` | `.lat`, `.lng` |
| `save()`, `get_features()` or `get_geojson()` before anything was loaded | `NoDataError` | `RuntimeError` | `.method_name`, `.empty` |

Each message says what was found, what was expected and how to fix it. These are the messages
for the most common file problems.

A file that does not exist:

```text
<<GeoFileNotFoundError>>
```

A spreadsheet that was not exported to CSV:

```text
<<UnsupportedFormatError>>
```

A file that is not valid JSON:

```text
<<InvalidGeoJSONError broken>>
```

JSON that is not GeoJSON, such as TopoJSON:

```text
<<InvalidGeoJSONError topo>>
```

A CSV file with a header and no rows:

```text
<<EmptyDatasetError>>
```

A file in another encoding:

```text
<<FileReadError>>
```

Warnings do not stop a load. They are `UserWarning`s that start with `QOREgeo:`:

| Warning | Raised by |
|---|---|
| `skipped N row(s) ... with non-numeric or out-of-range coordinates` | `load()` of delimited text |
| `skipped N feature(s) with invalid geometry` | `load()` of GeoJSON, `load_data()` |
| `duplicate column name(s)` | `load()` of delimited text |
| `N row(s) ... had more values than header columns` | `load()` of delimited text |
| `N row(s) ... have an invalid GeoJSON 'geometry' cell` | `load()` of delimited text |
| `properties [...] clash with CSV coordinate columns` | `save()` to CSV or TSV |

For a dataset that loads but behaves oddly, see [troubleshooting](troubleshooting.md).

## See also

- [Getting started](getting-started.md): install qoregeo and make a first map.
- [Measurements](measurements.md): distance, area and length on loaded data.
- [Queries](queries.md): filter, sort and search what you loaded.
- [Maps](maps.md) and [images](images.md): draw it.
- [Command line](cli.md): every `qoregeo` command and flag.
- [API reference](api-reference.md): every `GeoEngine` method on one page.
