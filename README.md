# QOREgeo

**Quantum-Powered Spatial Intelligence for Python.**

[![PyPI version](https://badge.fury.io/py/qoregeo.svg)](https://pypi.org/project/qoregeo)
[![Python versions](https://img.shields.io/pypi/pyversions/qoregeo.svg)](https://pypi.org/project/qoregeo)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://github.com/bosekarmegam/qoregeo/actions/workflows/tests.yml/badge.svg)](https://github.com/bosekarmegam/qoregeo/actions)
[![Zero dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)](https://pypi.org/project/qoregeo)

---

> *"The Pandas of Spatial Data Science."*

QOREgeo is the only Python GIS library that **works everywhere Python works** — Windows, Mac, Linux, Raspberry Pi, AWS Lambda — with zero C++ dependencies and a roadmap to quantum spatial algorithms via [QORE OS](https://github.com/bosekarmegam).

---

## Why QOREgeo?

```
$ pip install geopandas
ERROR: Failed building wheel for GDAL
note: This error originates from a subprocess.
error: legacy-install-failure
```

GeoPandas requires GDAL, a C++ library that fails to install on Windows 40% of the time.  
QOREgeo fixes the root cause — not the symptoms.

```
$ pip install qoregeo
Successfully installed qoregeo-1.0.0

Done in 2 seconds.
```

---

## Installation

```bash
pip install qoregeo
```

**Requirements:** Python 3.8+ · Zero external dependencies

---

## Quick Start

```python
from qoregeo import GeoEngine

geo = GeoEngine()
geo.load("cities.csv")

# ── Distance ─────────────────────────────────────────
delhi  = (28.6139, 77.2090)
mumbai = (19.0760, 72.8777)

km = geo.distance(delhi, mumbai)
print(km)          # 1153.54

miles = geo.distance(delhi, mumbai, unit="miles")
print(miles)       # 716.84

# ── Compass bearing ───────────────────────────────────
direction = geo.bearing(delhi, mumbai)
print(direction)   # South-Southwest

degrees = geo.bearing(delhi, mumbai, as_degrees=True)
print(degrees)     # 202.4

# ── Geofencing ────────────────────────────────────────
zone = geo.buffer(delhi, radius=10, unit="km")

inside = geo.point_in_polygon(mumbai, zone)
print(inside)      # False

close  = (28.65, 77.22)
inside = geo.point_in_polygon(close, zone)
print(inside)      # True

# ── Filter by radius ──────────────────────────────────
nearby = geo.filter_by_radius(28.61, 77.20, radius=400)
print(nearby.count())  # 3

# ── Find nearest ──────────────────────────────────────
result = geo.nearest(delhi)
print(result["feature"]["properties"]["name"])  # Delhi
print(result["distance"])                        # 0.0

# ── Filter by property ────────────────────────────────
mh = geo.filter("state", "Maharashtra")
print(mh.count())  # 2

# ── Export map ────────────────────────────────────────
geo.map("output.html", title="India Cities")

# ── Heatmap ───────────────────────────────────────────
geo.heatmap("heat.html", intensity_col="population")

# ── Save data ─────────────────────────────────────────
geo.save("output.geojson")
geo.save("output.csv")
```

---

## Method Chaining

Every method returns `self`, enabling Pandas-style pipelines:

```python
from qoregeo import GeoEngine

(GeoEngine()
    .load("all_stores.csv")
    .filter("state", "Maharashtra")
    .filter_by_radius(19.07, 72.87, radius=100)
    .save("results.geojson")
    .map("results.html", title="Mumbai Stores")
)
```

---

## API Reference

### `GeoEngine()`

The single entry point for all operations.

---

### Loading Data

#### `geo.load(path, lat_col=None, lng_col=None, encoding='utf-8-sig')`

Load from `.csv` or `.geojson`. Auto-detects `latitude`/`longitude` columns.

```python
geo.load("data.csv")
geo.load("data.geojson")
geo.load("data.csv", lat_col="y_coord", lng_col="x_coord")
```

**Auto-detected column names for latitude:**  
`latitude`, `lat`, `y`, `ylat`, `lat_deg`, `geo_lat`, `point_lat`

**Auto-detected column names for longitude:**  
`longitude`, `lng`, `lon`, `long`, `x`, `xlong`, `lng_deg`, `geo_lng`, `point_lng`

#### `geo.load_data(features)`

Load raw GeoJSON Feature dicts (no file needed).

```python
geo.load_data([
    {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [77.20, 28.61]},
        "properties": {"name": "Delhi"}
    }
])
```

---

### Geometry

#### `geo.distance(point_a, point_b, unit='km')` → `float`

Haversine great-circle distance.

| Unit   | Argument  |
|--------|-----------|
| km     | `'km'`    |
| miles  | `'miles'` |
| metres | `'m'`     |
| feet   | `'ft'`    |

```python
geo.distance((28.61, 77.20), (19.07, 72.87))           # 1153.54
geo.distance((28.61, 77.20), (19.07, 72.87), unit='m') # 1153540.0
```

#### `geo.bearing(point_a, point_b, as_degrees=False)` → `str | float`

Compass direction from A to B.

```python
geo.bearing((28.61, 77.20), (19.07, 72.87))                    # 'South-Southwest'
geo.bearing((28.61, 77.20), (19.07, 72.87), as_degrees=True)   # 202.4
```

#### `geo.buffer(center, radius, unit='km', num_points=64)` → `dict`

Create a circular buffer polygon (GeoJSON Polygon).

```python
zone = geo.buffer((28.61, 77.20), radius=10)            # 10 km radius
zone = geo.buffer((28.61, 77.20), radius=10, unit='m')  # 10 metre radius
```

#### `geo.point_in_polygon(point, polygon)` → `bool`

Test if a point is inside a polygon (ray-casting).

```python
zone = geo.buffer((28.61, 77.20), radius=50)
geo.point_in_polygon((28.65, 77.22), zone)   # True
geo.point_in_polygon((19.07, 72.87), zone)   # False
```

#### `geo.nearest(point, unit='km')` → `dict`

Find the nearest loaded feature to a point.

Returns: `{"feature": ..., "distance": float, "index": int}`

```python
result = geo.nearest((28.61, 77.20))
print(result["distance"])                         # 0.12
print(result["feature"]["properties"]["name"])    # 'Delhi'
```

---

### Filtering

#### `geo.filter(column, value)` → `GeoEngine`

Filter by property value (case-insensitive string match).

```python
mh = geo.filter("state", "Maharashtra")
```

#### `geo.filter_by_radius(lat, lng, radius, unit='km')` → `GeoEngine`

Filter features within a radius. Results sorted nearest-first. Injects `_distance` property.

```python
nearby = geo.filter_by_radius(28.61, 77.20, radius=400)
for f in nearby.get_features():
    print(f["properties"]["name"], f["properties"]["_distance"])
```

---

### Data Access

| Method | Returns | Description |
|--------|---------|-------------|
| `geo.count()` | `int` | Number of features |
| `geo.get_features()` | `list` | Raw GeoJSON feature list |
| `geo.get_geojson()` | `dict` | Full FeatureCollection |
| `geo.bounds()` | `dict` | `{min_lat, max_lat, min_lng, max_lng}` |
| `len(geo)` | `int` | Same as `count()` |

---

### Visualisation

#### `geo.map(output_path, title, zoom, center)` → `GeoEngine`

Interactive Leaflet.js marker map, saved as a standalone HTML file.

```python
geo.map("output.html")
geo.map("output.html", title="My Cities", zoom=6)
```

#### `geo.heatmap(output_path, title, intensity_col, zoom, center)` → `GeoEngine`

Density heatmap with optional intensity weighting.

```python
geo.heatmap("heat.html")
geo.heatmap("heat.html", intensity_col="sales_volume")
```

---

### Saving

#### `geo.save(path)` → `GeoEngine`

Save to `.geojson`, `.json`, or `.csv`.

```python
geo.save("output.geojson")
geo.save("output.csv")
```

---

## Error Messages

QOREgeo errors are designed to teach, not confuse:

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

---

## Real-World Examples

### Delivery Zone Analysis

```python
from qoregeo import GeoEngine

geo = GeoEngine().load("customers.csv")

branches = {
    "Delhi HQ":   (28.6139, 77.2090),
    "Mumbai Hub": (19.0760, 72.8777),
}

for name, coords in branches.items():
    nearby = geo.filter_by_radius(*coords, radius=50)
    print(f"{name}: {nearby.count()} customers within 50 km")
```

### Restaurant Geofencing

```python
from qoregeo import GeoEngine

geo = GeoEngine()
restaurant = (28.6315, 77.2167)   # Connaught Place, Delhi
zone = geo.buffer(restaurant, radius=5)   # 5 km delivery zone

for order in incoming_orders:
    inside = geo.point_in_polygon(order["location"], zone)
    status = "DELIVER" if inside else "TOO FAR"
    print(status)
```

### Ambulance Dispatch

```python
from qoregeo import GeoEngine

geo = GeoEngine().load("hospitals.csv")
patient = (28.6139, 77.2090)

result = geo.nearest(patient)
print(f"Nearest hospital: {result['feature']['properties']['name']}")
print(f"Distance: {result['distance']} km")

# Show nearby hospitals on a map
geo.filter_by_radius(*patient, 5).map("nearby_hospitals.html")
```

---

## Running Tests

```bash
# Install dev dependencies
pip install qoregeo[dev]

# Run all tests
pytest

# With coverage
pytest --cov=qoregeo --cov-report=term-missing

# Run a specific test
pytest tests/test_qoregeo.py::TestDistance -v
```

---

## Roadmap

| Version | Status | Focus |
|---------|--------|-------|
| v1.0.0 | ✅ Released | Core spatial engine, 9 features, 130+ tests |
| v1.1   | 🔄 In progress | Polygon loading, geocoding, PNG export |
| v2.0   | 📋 Planned | QORE OS integration, quantum nearest-neighbour |
| v3.0   | 🔮 Future | Quantum routing, GeoAI, WebAssembly |

---

## Contributing

1. Fork the repo at [github.com/bosekarmegam/qoregeo](https://github.com/bosekarmegam/qoregeo)
2. Create a branch: `git checkout -b feature/your-feature`
3. Add tests for new functionality
4. Run `pytest` — all tests must pass
5. Open a pull request

---

## License

MIT © 2025 [ArcGX TechLabs Private Limited](https://arcgx.in)  
Built by Suneel Bose 

---

## Links

- 📦 PyPI: [pypi.org/project/qoregeo](https://pypi.org/project/qoregeo)
- 🐙 GitHub: [github.com/bosekarmegam/qoregeo](https://github.com/bosekarmegam/qoregeo)
- 🐛 Issues: [github.com/bosekarmegam/qoregeo/issues](https://github.com/bosekarmegam/qoregeo/issues)
- ✉️ Contact: suneelbosekarmegam@gmail.com
