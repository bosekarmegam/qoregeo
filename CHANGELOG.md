# Changelog

All notable changes to QOREgeo are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).  
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.0] — 2025-01-01

### 🎉 Initial Release

**Core GeoEngine features:**

- `load()` — CSV and GeoJSON loading with auto-column detection  
- `load_data()` — Load raw GeoJSON Feature dicts in-memory  
- `save()` — Export to GeoJSON or CSV  
- `distance()` — Haversine great-circle distance (km, miles, m, ft)  
- `bearing()` — Compass direction (16-point + degrees)  
- `buffer()` — Circular geofence polygon creation  
- `point_in_polygon()` — Ray-casting geofencing  
- `nearest()` — Find the closest feature to any point  
- `filter()` — Filter by property value  
- `filter_by_radius()` — Filter by distance, sorted nearest-first  
- `map()` — Interactive Leaflet.js HTML map export  
- `heatmap()` — Density heatmap HTML export with optional intensity  
- `bounds()` — Bounding box of all features  
- `count()`, `get_features()`, `get_geojson()` — Data access  
- Full method chaining (`load().filter().save().map()`)  
- `len(geo)` and `repr(geo)` support  

**9 custom exceptions** with clear, actionable error messages:
- `NoDataError`  
- `InvalidCoordinateError`  
- `InvalidUnitError`  
- `ColumnNotFoundError`  
- `FileNotFoundError`  
- `UnsupportedFormatError`  
- `EmptyDatasetError`  
- `InvalidRadiusError`  
- `InvalidBufferError`  

**Quality:**
- 130+ tests, 100% passing  
- Zero external runtime dependencies  
- Python 3.8–3.12 compatible  
- Fully typed (PEP 484)  
- MIT licensed  

---

## [Unreleased] — v1.1 (In Progress)

### Planned
- Polygon boundary loading (district/state GeoJSON shapes)
- Street address geocoding (auto lat/lng from address string)
- Line buffer zones (draw radius around roads, rivers)
- PNG map export (static screenshot from any map)
- `intersects()` — test if two polygons overlap
- Improved type stubs for mypy strict mode
