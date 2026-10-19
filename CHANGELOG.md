# Changelog

All notable changes to QOREgeo are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).  
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.1.0] — 2026-10-19

### ⚠️ Compatibility notes
- **Python 3.10+ is now required** (3.8 and 3.9 are end-of-life; current pytest, mypy
  and hatchling no longer run on them). Python 3.10 – 3.15 are tested. Older Pythons
  keep installing 1.0.3 automatically.
- `point_in_polygon()` on a `buffer()` result is now an exact great-circle test
  against the circle (previously the inscribed 64-gon), so points within ~0.1 % of the
  radius of the edge may now test as inside — consistent with `filter_by_radius()`.
- Rows with out-of-range coordinates in a CSV are now skipped with a warning instead of
  aborting the whole load.
- `heatmap(intensity_col=...)` raises `ColumnNotFoundError` for an unknown column, and
  rows with missing / non-numeric / negative intensity no longer add heat.
- `map()` / `heatmap()`: `zoom` now defaults to `None` (auto-fit). With `center` it is
  the initial zoom; without, it caps the fit zoom so every feature stays visible.
- Filters on an empty result now return an empty engine instead of raising, and
  `get_features()` returns `[]`; `save()` writes empty results.

### Security
- **Fixed XSS in generated maps**: the title is HTML-escaped, popups are built with
  `textContent`, and embedded JSON escapes `<`, `>`, `&`, U+2028/U+2029 so data can
  never break out of the `<script>` block.
- Leaflet and leaflet.heat are loaded with **Subresource Integrity** hashes (verified
  against the npm-published files; v1.0.2 shipped a mistyped hash, which v1.0.3 worked
  around by dropping SRI) and fall back to a second CDN.
- The error overlay no longer uses `innerHTML`.

### Fixed
- `map()` / `heatmap()` ignored `center`; heatmap ignored `zoom`; single-point maps
  zoomed to the maximum; empty maps never set a view.
- Heatmaps crashed in the browser above ~125 k points (`Math.min(...spread)`), and
  intensities were only normalised when the maximum exceeded 1.
- `map()` printing `✅` crashed with `UnicodeEncodeError` on non-UTF-8 consoles
  (e.g. Windows cp1252) after the file was written.
- Non-Point features (Polygon, LineString, …) and `"geometry": null` /
  `"properties": null` (valid RFC 7946) crashed `nearest`, `filter`,
  `filter_by_radius`, `bounds`, `save(csv)`, `map` and `heatmap`.
- `point_in_polygon()` ignored holes, rejected MultiPolygons, gave wrong answers across
  the antimeridian and near the poles, and leaked raw `KeyError`/`IndexError` for
  malformed polygons.
- String coordinates passed validation then crashed; `(lat, lng, alt)` tuples, `None`,
  booleans, NaN/inf radii and invalid `num_points` raised raw Python errors.
- Inconsistent unit tables (`buffer()` had no `ft`, mile factors differed); unit
  matching is now exact (1 mi = 1.609344 km), case- and whitespace-insensitive.
- `nearest()` / `filter_by_radius()` compared distances rounded to 4 dp.
- Compass sector boundaries used banker's rounding; `bearing(as_degrees=True)` could
  return `360.0`.
- Haversine could raise `math domain error` for near-antipodal points.
- Providing only one of `lat_col` / `lng_col` re-ran auto-detection for both; a
  misspelled override produced a misleading `EmptyDatasetError`.
- CSV headers with surrounding spaces were not detected; extra columns produced a
  `None` property key.
- `save()` to CSV wrote duplicate / overwritten `latitude`/`longitude` columns when
  properties used those names, and wrote nested values as Python reprs.
- `load_data()` kept a stale source path and aliased the caller's property dicts.
- `load()` on a directory raised `IsADirectoryError`; invalid JSON was reported as an
  "unsupported format".
- "No data loaded" was reported after a filter that simply matched nothing.
- `ColumnNotFoundError` raised by `load()` suggested a `geo.filter(...)` fix.
- The buffer ring's closing vertex was the same list object as the first vertex.
- README / docstring example outputs (1153.54 km → 1148.0965 km, 202.4° → 203.47°).

### Added
- **GeoJSON geometry support**: Polygons, MultiPolygons, LineStrings, Multi*, null
  geometries and bare Geometry objects load, filter, save and render on maps.
- `distance(method="vincenty")` — WGS-84 ellipsoidal distance (~0.5 mm).
- Units `yd` and `nmi`, plus spelled-out aliases (`kilometres`, `feet`, …).
- `destination()`, `midpoint()`, `area()`, `length()`, `distance_to_line()`.
- `filter_by_line()` (line buffers), `filter_by_polygon()`, `filter_by_bbox()`
  (antimeridian aware), `k_nearest()`, `where()`, `sort_by()`, `head()`.
- `filter(column, value, op=...)` with `==`, `!=`, `>`, `>=`, `<`, `<=`, `in`,
  `not in`, `contains`.
- `bbox()` (RFC 7946 order, antimeridian aware) and `centroid()`.
- `geohash()` / `geohash_decode()`.
- `add_point()`, iteration over features, `GeoEngine(verbose=False)`.
- `load(delimiter=..., infer_types=...)`: comma / semicolon / tab / pipe detection,
  `.tsv` and `.txt` files, opt-in lossless type inference.
- `load_data()` accepts FeatureCollections, Features, Geometries and any iterable.
- Maps: all geometry types, `tiles=` (`dark`, `light`, `voyager`, `osm`, or a custom
  URL with `attribution=`), `tile_api_key=` / `QOREGEO_CARTO_API_KEY` (CARTO basemaps
  require a free key since August 2026 — QOREgeo warns when it is missing),
  `overlays=` (e.g. draw a buffer), canvas-rendered markers, OSM/CARTO attribution
  as their terms require; heatmap `radius` / `blur`.
- `buffer()` rings follow the RFC 7946 right-hand rule (counter-clockwise).
- Exceptions inherit the matching built-ins (`ValueError`, `LookupError`,
  `RuntimeError`, `FileNotFoundError`) and expose attributes (`.lat`, `.unit`,
  `.column`, …). New `InvalidGeoJSONError` and `InvalidArgumentError`.
  `GeoFileNotFoundError` is the new name; `qoregeo.FileNotFoundError` remains an alias
  but is no longer exported by `from qoregeo import *`, so it can't shadow the built-in.
- `py.typed` marker — the inline type hints are now visible to type checkers
  (package passes `mypy --strict`).

### Changed
- Version is single-sourced from `qoregeo/_version.py`.
- Packaging: PEP 639 license metadata, hatchling ≥ 1.27, PEP 735 dependency groups
  (`pip install -e . --group dev`), ruff `lint` table.
- CI: Python 3.10 – 3.15 on Linux / Windows / macOS, ruff + strict mypy job, build +
  `twine check`; actions bumped to current majors (checkout v7, setup-python v7,
  codecov v7, upload/download-artifact v7/v8).
- Release: separate build and publish jobs, tag/version check, wheel tested before
  upload, `pypi` environment for trusted publishing; Dependabot for actions.

## [1.0.3] — 2026-03-02

### Fixed
- fix map loading error

## [1.0.2] — 2026-03-02

### Fixed
- Skip auto-detection when lat_col/lng_col are manually provided
- Fix southern hemisphere distance test range

## [1.0.0] — 2026-03-02

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

## [Unreleased] — planned

### Planned
- Street address geocoding (opt-in, network)
- PNG / SVG static map export
- `intersects()` — test if two polygons overlap
- QORE OS integration (v2.0)
