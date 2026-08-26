<div align="center">

[← Geometry](geometry.md) · **Querying &amp; Filtering** · [Analysis →](analysis.md)

</div>

---

# Querying & Filtering

The query language, spatial filters, and the DataFrame-style table operations.

---

## Everything returns a new engine

```python
big = geo.query("population > 1e7")

big.count()      # 5
geo.count()      # 8, untouched
```

Filters and transforms never modify what they were called on. Methods that only
write a file, `map()`, `save()`, `png()`, return `self`, so chains keep
flowing:

```python
(geo.query("population > 1e7")
    .sort_by("population", reverse=True)
    .head(3)
    .save("top3.geojson")
    .map("top3.html"))
```

---

## The query language

```python
geo.query("population > 1000000")
geo.query("state == 'Maharashtra'")
geo.query("population > 1e6 and state != 'Delhi'")
geo.query("name contains 'pur' or state in ('Delhi', 'Goa')")
geo.query("closed_date is null")
```

`where()` is an alias, if that reads better in your pipeline.

### Operators

| Operator | Example | Notes |
|---|---|---|
| `==` | `state == 'Goa'` | strings compare case-insensitively |
| `!=` `<>` | `state != 'Goa'` | both spellings work |
| `>` `>=` `<` `<=` | `population >= 1e6` | numeric when both sides are numeric |
| `contains` | `name contains 'pur'` | substring, case-insensitive |
| `startswith` | `code startswith 'MH'` | case-insensitive |
| `endswith` | `name endswith 'bad'` | case-insensitive |
| `in (…)` | `state in ('Goa', 'Delhi')` | strings or numbers |
| `is null` | `closed is null` | `None` **or** an empty string |
| `is not null` | `closed is not null` | |

Combine with `and`, `or`, `not`, and group with parentheses. Keywords are
case-insensitive, so `AND` works too.

### Values

```python
geo.query("population > 1e6")         # scientific notation
geo.query("tier == 1")                # numbers
geo.query("state == 'New Delhi'")     # single quotes
geo.query('state == "New Delhi"')     # double quotes
geo.query("state == Maharashtra")     # bare words are strings
geo.query("`odd column name` > 5")    # backticks for awkward names
```

### Numbers stored as text

CSV columns load as strings. QOREgeo compares numerically whenever **both**
sides look numeric, so this does the right thing:

```python
geo.query("population > 1000000")     # not a text comparison
```

Text comparison would sort `"951000"` above `"20700000"`, which is the kind of
bug that survives a long time in production.

### Precedence

`and` binds tighter than `or`, as in SQL and Python:

```python
geo.query("tier == 3 or tier == 1 and state == 'Delhi'")
# reads as:  tier == 3  or  (tier == 1 and state == 'Delhi')

geo.query("(tier == 3 or tier == 1) and state == 'Delhi'")
# parenthesise when you mean the other thing
```

<a name="safety"></a>
### Safety

The query is handled by a small hand-written tokeniser and recursive-descent
parser. It is **never** passed to `eval()`.

```python
geo.query("__import__('os').system('rm -rf /')")
```
```
❌  QOREgeo. Invalid Query
────────────────────────────────────────────
Could not parse: "__import__('os').system('rm -rf /')"
```

The grammar in [`qoregeo/query.py`](../qoregeo/query.py) is the entire language;
there is nothing else it can be made to do. That is deliberate, a query string
usually arrives from a config file, a CLI argument or a web form, and `eval` on
any of those is a remote-code-execution bug waiting to happen.

### Compiling once

Filtering repeatedly with the same expression? Compile it:

```python
from qoregeo import compile_query

is_large = compile_query("population > 1e7")
large = [f for f in geo if is_large(f["properties"])]
```

---

## Single-condition filters

When a full expression is overkill:

```python
geo.filter("state", "Maharashtra")              # equality, case-insensitive
geo.filter("population", 1000000, op=">")       # any comparison
geo.filter("name", "pur", op="contains")
```

**Operators:** `==` `!=` `>` `>=` `<` `<=` `contains` `startswith` `endswith`

Filtering on a column that does not exist raises `ColumnNotFoundError` listing
the columns that do. A typo tells you immediately rather than silently
returning nothing.

### Your own predicate

```python
geo.filter_by(lambda props: props["state"] in {"Goa", "Kerala"}
                            and len(props["name"]) < 8)
```

The escape hatch for logic the query language does not cover.

---

## Spatial filters

### By radius

```python
nearby = geo.filter_by_radius(19.07, 72.87, radius=200)

[(f["properties"]["name"], f["properties"]["_distance"]) for f in nearby]
# [('Mumbai', 1.0488), ('Pune', 120.5103)]
```

Sorted nearest-first, with a `_distance` property injected in the unit you
asked for. Units: `km` `miles` `m` `ft` `nm`.

Backed by the spatial index on larger datasets, so it stays fast as the data
grows, see [Analysis → Indexing](analysis.md#spatial-indexing).

### By polygon

```python
zone = geo.buffer((19.07, 72.87), radius=100)

geo.within(zone)      # inside, holes and MultiPolygons respected
geo.outside(zone)     # outside
```

Works with any polygon: a buffer, a district boundary from a shapefile, a
hand-drawn KML zone.

### By bounding box

```python
geo.filter_by_bbox(min_lat=12, min_lng=72, max_lat=20, max_lng=81)
```

The cheapest spatial filter. Use it to narrow a large dataset before an
expensive operation.

### Nearest neighbours

```python
geo.knn(delhi, k=3)
# [{'feature': …, 'distance': 0.0,      'index': 0},
#  {'feature': …, 'distance': 775.7108, 'index': 7},
#  {'feature': …, 'distance': 1148.0965,'index': 1}]

geo.nearest(delhi)          # just the closest, same dict shape
geo.knn(delhi, k=5, unit="miles")
```

---

## Table operations

If you know pandas, these will feel familiar.

### Sorting and slicing

```python
geo.sort_by("population")                  # ascending
geo.sort_by("population", reverse=True)    # descending
geo.sort_by("name")                        # text sorts case-insensitively

geo.head(5)
geo.tail(5)
geo.sample(10, seed=42)                    # reproducible with a seed
geo[2]                                     # one feature dict
geo[1:4]                                   # a new engine
```

Numeric-looking values sort numerically; everything else sorts as lower-cased
text. Blanks always sort last, in both directions, so `head()` and `tail()`
both show you real data.

### Columns

```python
geo.columns()                              # ['name', 'state', 'population']
geo.select(["name", "population"])         # keep only these
geo.drop(["population"])                   # remove these
geo.rename({"state": "region"})
```

### Computed columns

```python
geo.add_column("country", "India")                                  # constant
geo.add_column("millions", lambda p: round(float(p["population"]) / 1e6, 1))
geo.add_column("label", lambda p: f"{p['name']} ({p['state']})")
```

The callable receives the feature's properties dict.

### Whole-feature transforms

```python
geo.apply(lambda f: {**f, "properties": {"name": f["properties"]["name"]}})
```

### Combining

```python
combined = north + south          # or north.concat(south)
snapshot = geo.copy()             # independent deep copy
```

---

## Summarising

```python
geo.count()                       # 8
geo.unique("state")               # distinct values, first-seen order
geo.value_counts("state")
# {'Maharashtra': 2, 'Delhi': 1, 'Gujarat': 1, 'Karnataka': 1, …}

geo.stats("population")
# {'count': 8, 'min': 7400000.0, 'max': 32900000.0,
#  'mean': 14912500.0, 'median': 12250000.0, 'sum': 119300000.0}

geo.describe()                    # size, geometry mix, columns, extent, spread
```

`stats()` skips non-numeric values rather than raising, because real columns
contain `"N/A"`. A wholly text column returns `{'count': 0}`. That is the
signal to reach for `value_counts()` instead.

---

## Getting data back out

```python
geo.get_features()          # the raw list of GeoJSON Feature dicts
geo.get_geojson()           # the full FeatureCollection dict
geo.to_records()            # flat dicts: latitude, longitude, then properties
geo.to_wkt()                # every geometry as a WKT string
geo.to_geojson_string()     # JSON text

for feature in geo:                         # engines are iterable
    print(feature["properties"]["name"])

len(geo)                                    # same as count()
if geo: ...                                 # False when empty
```

---

## Worked example

```python
from qoregeo import GeoEngine

report = (GeoEngine()
    .load("stores.csv")
    .clean()
    .dropna(["revenue"])
    .query("revenue > 250000 and status != 'closed'")
    .filter_by_radius(19.0760, 72.8777, radius=50)
    .add_column("revenue_m", lambda p: round(float(p["revenue"]) / 1e6, 2))
    .sort_by("revenue", reverse=True)
    .head(20))

print(f"{report.count()} stores, {report.stats('revenue')['sum']:,.0f} total revenue")
print(report.value_counts("segment"))

report.save("mumbai_top20.geojson")
report.map("mumbai_top20.html", colour_by="segment", tooltip_field="name")
```

Reading top to bottom: load, drop unusable rows, drop rows missing revenue,
filter by business rules, filter by geography, add a display column, rank,
take the top 20, then report and draw.

---

## Next

| You want to… | Go to |
|---|---|
| Cluster, join to polygons, plan a route | [Analysis](analysis.md) |
| Style and export maps | [Visualisation](visualisation.md) |
| Do this from the shell | [Command Line](cli.md) |
| Look up an exact signature | [API Reference](api-reference.md) |

---

<div align="center">

[← Geometry](geometry.md) · [Documentation home](README.md) · [Analysis →](analysis.md)

</div>
