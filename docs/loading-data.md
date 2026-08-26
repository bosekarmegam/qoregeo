<div align="center">

[← Getting Started](getting-started.md) · **Loading &amp; Saving Data** · [Geometry →](geometry.md)

</div>

---

# Loading & Saving Data

Every format QOREgeo reads and writes, how column detection works, and what to
do when a file is dirty.

---

## Format support

| Format | Extension | Read | Write | Notes |
|---|---|:---:|:---:|---|
| CSV | `.csv` | ✅ | ✅ | latitude/longitude columns detected automatically |
| GeoJSON | `.geojson` `.json` | ✅ | ✅ | `FeatureCollection` or a single `Feature` |
| GeoJSON Lines | `.ndjson` `.geojsonl` `.jsonl` | ✅ | ✅ | one feature per line, streams |
| WKT / EWKT | `.wkt` | ✅ | ✅ | all geometry types, PostGIS `SRID=` prefix accepted |
| GPX | `.gpx` | ✅ | ✅ | waypoints, routes, tracks, elevation, timestamps |
| KML | `.kml` | ✅ | ✅ | placemarks, `ExtendedData`, polygon holes |
| Esri Shapefile | `.shp` | ✅ | - | **no GDAL**, `.shp` + `.dbf` parsed with `struct` |
| SVG | `.svg` | - | ✅ | static vector map |
| PNG | `.png` | - | ✅ | static raster map |
| HTML | `.html` | - | ✅ | interactive map |

```python
from qoregeo import GeoEngine

geo = GeoEngine().load("anything.csv")      # the extension picks the reader
geo.save("anything.geojson")                # and the writer
```

---

## CSV

```python
geo = GeoEngine().load("stores.csv")
```

Every column other than latitude and longitude becomes a property on the
feature. Values stay as strings. That is what a CSV contains, but numeric
comparisons still work, because QOREgeo compares numerically whenever both
sides look like numbers:

```python
geo.query("revenue > 500000")     # works even though revenue is text
geo.stats("revenue")              # skips anything non-numeric
```

### Column detection

QOREgeo matches these names, case-insensitively:

| For | Accepted names |
|---|---|
| Latitude | `latitude` `lat` `y` `ylat` `lat_deg` `latitude_deg` `geo_lat` `point_lat` |
| Longitude | `longitude` `lng` `lon` `long` `x` `xlong` `lng_deg` `longitude_deg` `geo_lng` `geo_long` `point_lng` |

If your file uses something else, name them:

```python
geo = GeoEngine().load("survey.csv", lat_col="northing_deg", lng_col="easting_deg")
```

### Encoding

The default is `utf-8-sig`, which handles the byte-order mark Excel writes.
For anything else, say so:

```python
geo = GeoEngine().load("legacy.csv", encoding="latin-1")
```

### Rows that cannot be read

Rows with blank coordinates are skipped silently. That is normal in exported
data. Rows with coordinates that will not parse as numbers are skipped with a
warning naming the row and the value, so you can find them.

Coordinates outside ±90 / ±180 raise `InvalidCoordinateError` rather than being
skipped, because they usually mean the columns are swapped or the file is in a
projected system.

---

## GeoJSON

```python
geo = GeoEngine().load("districts.geojson")
```

Accepts a `FeatureCollection` or a lone `Feature`. All geometry types are
preserved, points, lines, polygons, their multi-variants and
`GeometryCollection`.

```python
geo.save("out.geojson")                        # indented, UTF-8, human-readable
text = geo.to_geojson_string(indent=None)      # compact, as a string
```

---

## Shapefiles: without GDAL

```python
geo = GeoEngine().load("districts.shp")
```

This is the headline capability. `.shp` is a documented binary format and
`.dbf` is dBase III, so both are read with `struct`, no C library, no wheel to
find, no install step.

| Supported | Details |
|---|---|
| Geometry | Point, PolyLine, Polygon, MultiPoint |
| Variants | Z and M forms. The extra ordinates are read past and dropped |
| Attributes | the sibling `.dbf`, typed as text, number, float or boolean |
| Holes | detected from ring winding, per the Esri specification |
| Null shapes | skipped, keeping attribute alignment intact |

<details>
<summary>What is <em>not</em> supported, and what to do about it</summary>

- **Writing shapefiles.** The format is legacy. Write GeoJSON instead. If you
  must produce one, convert with QGIS or `ogr2ogr` at the boundary.
- **Projected coordinate systems.** A shapefile stores its CRS in a separate
  `.prj` file, which QOREgeo does not read. If yours holds metres rather than
  degrees, the values load as-is and are **not** valid latitude/longitude.
  Check with `geo.validate()` and reproject before treating them as such.
- **`.shx` index and `.cpg` encoding files.** Not needed; the `.shp` is read
  sequentially. Pass `encoding=` to `read_shapefile` if the `.dbf` is not
  Latin-1.

</details>

---

## GPX: GPS tracks

```python
geo = GeoEngine().load("morning-ride.gpx")
```

| GPX element | Becomes | Marked with |
|---|---|---|
| `<wpt>` | `Point` | `_kind: "waypoint"` |
| `<rte>` | `LineString` | `_kind: "route"` |
| `<trkseg>` | `LineString` | `_kind: "track"`, `_points: n` |

Names, descriptions, elevation (`ele`, as a number) and timestamps (`time`)
carry across. A track's name lives on `<trk>` while its points live in child
`<trkseg>` elements, so multi-segment tracks all inherit the track name.

```python
tracks = geo.query("_kind == 'track'")
print(f"{tracks.length()} km across {tracks.count()} segments")
```

---

## KML: Google Earth

```python
geo = GeoEngine().load("my-places.kml")
```

Placemarks become features. `<name>` and `<description>` become properties,
as do `<ExtendedData>` `<Data>` and `<SimpleData>` fields. Polygon inner
boundaries are read as holes. Altitude in the coordinate triples is dropped,
since GeoJSON positions here are 2-D.

Namespaces are matched by local name, so files from Google Earth, My Maps and
various exporters all work despite declaring different namespace URIs.

---

## WKT

```python
from qoregeo import parse_wkt, to_wkt

parse_wkt("POINT (77.209 28.6139)")
# {'type': 'Point', 'coordinates': [77.209, 28.6139]}

to_wkt({"type": "Point", "coordinates": [77.209, 28.6139]})
# 'POINT (77.209 28.6139)'
```

All types are handled: `POINT`, `LINESTRING`, `POLYGON`, the `MULTI` variants,
and `GEOMETRYCOLLECTION`. `Z`/`M` ordinates and a PostGIS `SRID=4326;` prefix
are accepted and dropped.

A `.wkt` file holds one geometry per line, optionally with a label after a tab
or semicolon:

```
POINT (77.2090 28.6139)	Delhi
POINT (72.8777 19.0760)	Mumbai
# lines starting with # are ignored
```

```python
geo = GeoEngine().load("places.wkt")
geo.to_wkt()          # every geometry back out as WKT strings
```

Handy for moving geometry in and out of PostGIS, DuckDB or any database with a
WKT column.

---

## NDJSON: one feature per line

```python
geo = GeoEngine().load("export.ndjson")
geo.save("out.ndjson")
```

The format large spatial exports use, because it appends without rewriting the
file. Each line may be a `Feature`, a `FeatureCollection`, or a bare geometry.
Blank lines are skipped; a malformed line raises an error naming the line
number.

---

## Loading from memory

No file needed, and QOREgeo is deliberately relaxed about shape:

```python
from qoregeo import GeoEngine

# (lat, lng) tuples
GeoEngine.from_points([(28.6139, 77.2090), (19.0760, 72.8777)])

# dicts with lat/lng keys: 'lat'/'lng', 'lon', or 'latitude'/'longitude'
GeoEngine.from_records([
    {"lat": 28.6139, "lng": 77.2090, "name": "Delhi"},
    {"lat": 19.0760, "lng": 72.8777, "name": "Mumbai"},
])

# GeoJSON features, or bare geometries
GeoEngine().load_data([
    {"type": "Feature",
     "geometry": {"type": "Point", "coordinates": [77.2090, 28.6139]},
     "properties": {"name": "Delhi"}},
    {"type": "Point", "coordinates": [72.8777, 19.0760]},
])
```

All four shapes are accepted by `load_data()`; the classmethods just name the
common cases.

### Coming from pandas

There is no pandas integration, because that would be a dependency. Going both
ways is one line:

```python
geo = GeoEngine.from_records(df.to_dict("records"))   # in
df  = pd.DataFrame(geo.to_records())                   # out
```

`to_records()` returns flat dicts with `latitude` and `longitude` keys plus
every property.

---

## Saving

```python
geo.save("out.geojson")     # GeoJSON, indented
geo.save("out.csv")         # latitude, longitude, then every property
geo.save("out.ndjson")      # one feature per line
geo.save("out.wkt")         # one geometry per line
geo.save("out.gpx")         # points as waypoints, lines as tracks
geo.save("out.kml")         # placemarks with ExtendedData
geo.save("out.html")        # interactive map
geo.save("out.svg")         # static vector map
geo.save("out.png")         # static raster map
```

`save()` returns `self`, so it sits mid-chain:

```python
(geo.query("revenue > 1e6")
    .save("top.geojson")
    .map("top.html"))
```

Missing directories are created for you. An unrecognised extension raises
`UnsupportedFormatError` listing what is supported.

---

## Checking data before you trust it

Real files are broken in a small number of predictable ways. There is a method
for each.

### `validate()`: report without raising

```python
geo.validate()
```
```python
{'valid': 8, 'invalid': 0, 'issues': [], 'likely_swapped': 0}
```

`issues` lists up to 100 entries of `{"index": …, "problem": …}` covering
missing geometry, empty coordinate arrays and out-of-range values.
`likely_swapped` counts coordinates that look like latitude and longitude the
wrong way round.

### `clean()`: drop what cannot be used

```python
geo = geo.clean()      # removes features with no geometry or no coordinates
```

### `fix_coordinates()`: repair a swap

```python
geo = geo.fix_coordinates()
```

Only swaps a point when the stored latitude exceeds ±90 while the longitude
does not. An unambiguous signal, since no real latitude is above 90. Ambiguous
cases are left alone rather than guessed at.

### `dropna()`: drop blanks

```python
geo.dropna()                  # any property missing or blank
geo.dropna(["name", "city"])  # only these
```

### `dedupe()`: collapse duplicates

```python
geo.dedupe()                                    # identical coordinates
geo.dedupe(tolerance_km=0.05)                   # within 50 m of a kept point
geo.dedupe(tolerance_km=0.05, subset=["name"])  # …and the same name
```

The tolerance form is what you want after geocoding, where the same shop comes
back twice a few metres apart.

### A cleaning pipeline

```python
clean = (GeoEngine()
    .load("messy_export.csv")
    .clean()
    .fix_coordinates()
    .dropna(["name"])
    .dedupe(tolerance_km=0.02))

print(f"{clean.count()} usable of {GeoEngine().load('messy_export.csv').count()}")
```

---

## Inspecting an unfamiliar file

```python
geo.describe()
```
```python
{'features': 8,
 'geometry_types': {'Point': 8},
 'columns': ['name', 'state', 'population'],
 'bounds': {'min_lat': 12.9716, 'max_lat': 28.6139,
            'min_lng': 72.5714, 'max_lng': 88.3639},
 'source': 'cities.csv',
 'dispersion': {'centre_lat': 19.466269, 'centre_lng': 77.646996,
                'mean_km': 686.0125, 'median_km': 690.0823,
                'max_km': 1164.3778, 'standard_distance_km': 742.6971}}
```

Or from the shell, which also runs `validate()` for you:

```bash
qoregeo info mystery.shp
```

---

## Next

| You want to… | Go to |
|---|---|
| Measure distances, areas and buffers | [Geometry](geometry.md) |
| Filter and reshape the data | [Querying &amp; Filtering](querying.md) |
| Cluster, join or route | [Analysis](analysis.md) |
| Draw it | [Visualisation](visualisation.md) |

---

<div align="center">

[← Getting Started](getting-started.md) · [Documentation home](README.md) · [Geometry →](geometry.md)

</div>
