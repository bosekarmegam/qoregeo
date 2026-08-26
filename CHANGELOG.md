# Changelog

All notable changes to QOREgeo are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.1.0] — 2025-08-26

**The analysis release.** v1.0 could answer *where*. This answers *so what* —
and does it fast enough to matter on real datasets.

Everything below is still pure Python with zero runtime dependencies, and the
entire v1.0 API is unchanged.

### Added

**Geometry** (`qoregeo.geometry`)
- `area()` — spherical polygon area in km², m², hectares, acres or mi²,
  with interior rings subtracted
- `length()` — line length and polygon perimeter
- `centroid()` — area-weighted for polygons, vertex mean otherwise
- `convex_hull()` — Andrew's monotone chain
- `simplify()` — Ramer–Douglas–Peucker, ring-aware
- `intersects()` / `contains()` — polygon, line and mixed overlap tests
- `buffer_line()` — corridors around routes, rivers and pipelines
- `buffer_geometry()` — buffer any geometry type
- `destination()`, `midpoint()`, `interpolate()` — great-circle navigation
- `vincenty_km()` — WGS84 ellipsoidal distance, and `distance(method="vincenty")`
- `cross_track_km()`, `nearest_point_on_line()` — snap a point to a path
- Point-in-polygon now handles **holes and MultiPolygons**

**Spatial index** (`qoregeo.index`)
- Uniform-grid index; ~280× faster radius and nearest-neighbour queries at
  50 000 features
- Built automatically above `AUTO_INDEX_THRESHOLD`, or explicitly via
  `build_index()`
- `knn()` — k nearest neighbours with distances
- Correct across the poles and the antimeridian

**Analysis** (`qoregeo.analysis`)
- `dbscan()` / `GeoEngine.cluster()` — density clustering on great-circle
  distances, with outliers flagged rather than forced into a group
- `kmeans()` — k-means++ seeded, averaged in 3-D so it survives the antimeridian
- `spatial_join()` — tag points with the attributes of the polygon containing them
- `hotspots()` — grid density, densest first, with drawable cell polygons
- `nearest_neighbour_ratio()` / `pattern()` — Clark & Evans; is the clustering real?
- `centre_of_mass()` — weighted geographic centre
- `dispersion()` — mean, median, max and standard distance from the centre

**Routing** (`qoregeo.routing`)
- `optimise_route()` — greedy nearest-neighbour plus 2-opt; matches the
  brute-force optimum on small problems
- Per-leg distances and compass headings; `route_line()` for drawing
- `travel_time()` — driving plus dwell time estimates

**Query language** (`qoregeo.query`)
- `query()` / `where()` — `"population > 1e6 and state != 'Delhi'"`
- Operators: `== != > >= < <=`, `contains`, `startswith`, `endswith`,
  `in (…)`, `is null`, combined with `and` / `or` / `not` and parentheses
- Hand-written parser, **no `eval`** — safe for strings from config files,
  CLI arguments and web forms
- `filter(column, value, op=…)` for single comparisons

**Formats** (`qoregeo.formats`)
- **Esri Shapefile reader** — `.shp` + `.dbf` parsed with `struct`, no GDAL.
  Points, polylines, polygons and multipoints, including Z/M variants, null
  shapes and winding-based holes
- WKT / EWKT read and write, all geometry types
- GPX read and write — waypoints, routes, tracks, elevation, timestamps
- KML read and write — placemarks, ExtendedData, polygon holes
- NDJSON / GeoJSON Lines read and write

**Geohashes, tiles and projections**
- `qoregeo.geohash` — encode, decode, bounding boxes, neighbours, cell
  polygons, common prefixes; slippy tiles and Bing quadkeys
- `qoregeo.crs` — Web Mercator and UTM conversions, including the Norway and
  Svalbard zone exceptions
- `geohash_column()` and `project()` on the engine

**Table operations on `GeoEngine`**
- `sort_by`, `head`, `tail`, `sample`, `select`, `drop`, `rename`,
  `add_column`, `apply`, `concat`, `copy`
- `columns`, `unique`, `value_counts`, `stats`, `describe`, `to_records`,
  `to_wkt`, `to_geojson_string`
- `within()`, `outside()`, `filter_by_bbox()`, `filter_by()`
- `from_records()`, `from_points()`; `load_data()` now accepts tuples, bare
  geometries and lat/lng dicts
- `__iter__`, `__getitem__` (index and slice), `__add__`, `__bool__`, and
  `_repr_html_` for a table preview in Jupyter

**Data quality**
- `validate()` — report every problem without raising, including the classic
  latitude/longitude swap
- `clean()`, `dropna()`, `dedupe(tolerance_km=…)`, `fix_coordinates()`

**Visualisation**
- Polygons and lines now render on interactive maps (`L.geoJSON`), not just points
- Marker clustering, automatic above ~750 features
- Five basemaps — dark, light, streets, terrain, satellite — with a switcher
- `colour_by` categorical colouring with a legend, hover tooltips, a live
  filter box and a scale bar
- `choropleth()` — quantile-classed shading with a graduated legend
- `svg()` and `png()` — **static maps with no browser and no network**; the
  PNG encoder is built on `zlib` and `struct`

**Geocoding** (`qoregeo.geocode`)
- Opt-in `Geocoder` over `urllib`: forward, reverse and bulk lookup
- Rate limited to one request per second and cached, per Nominatim policy
- `GeoEngine.geocode(column, user_agent=…)`

**Command line**
- `qoregeo` console script and `python -m qoregeo`
- `info`, `map`, `heatmap`, `image`, `convert`, `distance`, `nearest`,
  `within`, `stats`, `geocode`
- `-q/--query` on the data commands, `--json` where machine output helps

**Packaging**
- `py.typed` marker — inline types now visible to type checkers (PEP 561)
- Python 3.13 classifier
- New exceptions: `InvalidGeometryError`, `InvalidQueryError`,
  `GeocodingError`, `MissingIndexError`

### Fixed

Four correctness bugs, all found by testing against independent references
rather than against the implementation's own output:

- **Line buffers excluded their own endpoints.** The semicircular end caps
  swept inward across the segment instead of outward around it, so a point at
  either end of a corridor read as outside it.
- **Line buffers were asymmetric.** The offset sides were drawn as straight
  chords in lng/lat space, but a great circle is a curve there — so a corridor
  bulged tens of kilometres on one side and pinched on the other over a
  1000 km leg. The sides are now densified along the arc; corridor area is
  within 0.02% of the analytic value.
- **The spatial index lost results near the poles.** The column sweep was
  bounded by `radius / km-per-degree`, which is wrong when the search circle
  crosses a pole and every longitude comes within reach. Column widths were
  also latitude-dependent, so "the next cell along" meant different things in
  different rows. The grid is now uniform in degrees with a spherical-cap
  column bound, verified feature-for-feature against brute force over 760
  randomised queries.
- **Static maps rendered mirrored.** The projector's vertical span was computed
  north-minus-south, making the scale factor negative and flipping both axes.

Also fixed:

- **2-opt skipped valid moves.** An off-by-one excluded the reversal of
  two-stop segments, stranding tours in worse local optima. Route quality on a
  30-stop problem improved from 7.8% to 8.8% better than greedy, and pathological
  starts now reach the true optimum.
- **Polygon boundaries are now inclusive.** A point exactly on an edge or
  vertex counts as inside. Previously a convex hull did not contain the points
  it was built from, and a geofence rejected addresses on its own boundary.
- **Web Mercator returned −7×10⁻¹⁰ m at the equator** from `log(tan(π/4))`
  rounding. Now uses `asinh(tan φ)`, which is exact there.
- **GPX track names were dropped**, because they live on `<trk>` while the
  points live in child `<trkseg>` elements.
- **Choropleths produced empty classes** on tied data — a legend row reading
  "5 – 5" that could never contain anything. Quantile breaks now collapse ties
  and the colour ramp shrinks to match.

### Changed

- `GeoEngine.VERSION` is now `1.1.0` and tracks `qoregeo.__version__`
- `nearest()` is backed by the spatial index and delegates to `knn()`; the
  return shape is unchanged
- `save()` accepts every supported format, dispatching on extension
- `load()` accepts `.shp`, `.gpx`, `.kml`, `.wkt` and `.ndjson`
- `distance()` and `filter_by_radius()` accept nautical miles (`nm`)
- `map()` and `heatmap()` accept `quiet=True` to suppress their output line
- Ruff configuration moved to `[tool.ruff.lint]` and widened to `B`, `C4`,
  `SIM`, with the Python 3.8 typing constraints documented in-place
- Development status raised to Production/Stable

### Quality

- **933 tests**, up from 121 — 94% coverage
- Lint (`ruff`) and type checking (`mypy`) both clean
- Still zero runtime dependencies, still Python 3.8–3.13

---

## [1.0.3] — 2025-03-02

### Fixed
- fix map loading error

## [1.0.2] — 2025-03-02

### Fixed
- Skip auto-detection when lat_col/lng_col are manually provided
- Fix southern hemisphere distance test range

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
`NoDataError`, `InvalidCoordinateError`, `InvalidUnitError`,
`ColumnNotFoundError`, `FileNotFoundError`, `UnsupportedFormatError`,
`EmptyDatasetError`, `InvalidRadiusError`, `InvalidBufferError`

**Quality:**
- 130+ tests, 100% passing
- Zero external runtime dependencies
- Python 3.8–3.12 compatible
- Fully typed (PEP 484)
- MIT licensed

---

## Unreleased

See [ROADMAP.md](ROADMAP.md) for the full plan. Next up in **v1.2**: raster
support (ASCII Grid, GeoTIFF, zonal statistics, contours), time-aware
trajectories, and streaming for larger-than-memory datasets.
