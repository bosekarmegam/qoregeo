<div align="center">

<img src="assets/qoregeo-mark.png" width="72" alt="QOREgeo">

# Roadmap

**The plan, why each piece is on it, and what "done" means.**

[Home](README.md) ·
[Documentation](docs/README.md) ·
[Changelog](CHANGELOG.md) ·
[Contributing](CONTRIBUTING.md)

</div>

---

QOREgeo exists to remove one specific obstacle: **installing a GIS library
should not be a project in itself.** GeoPandas needs GDAL, GDAL needs a C++
toolchain, and that chain breaks constantly, on Windows laptops, on Alpine
containers, on AWS Lambda, on Raspberry Pi, on locked-down corporate machines.
QOREgeo is pure Python with zero dependencies, so `pip install qoregeo`
finishes in two seconds everywhere Python runs.

Every item below is judged against that mission. A feature earns its place if
it is something people currently install a heavyweight stack to get, and it can
be done well in pure Python. A feature that would require a C extension does
not go in the core, however useful. It goes in an optional companion package
or it does not ship.

---

## Where things stand

| Version | Status | Theme |
|---------|--------|-------|
| v1.0 | ✅ Shipped | Core spatial engine, distance, bearing, buffers, geofencing, maps |
| v1.1 | ✅ Shipped | **Analysis release**, geometry, indexing, clustering, formats, CLI |
| v1.2 | 🔜 Next | Raster, time and streaming |
| v1.3 | 📋 Planned | Topology and network analysis |
| v2.0 | 📋 Planned | Performance, plugins, and the QORE OS bridge |
| v3.0 | 🔮 Research | Quantum spatial algorithms, GeoAI, WebAssembly |

---

## v1.1: the analysis release (shipped)

v1.0 could answer *where*. v1.1 answers *so what*.

The gap was that v1.0 stopped exactly where real work begins. You could
measure a distance and draw a map, but not read a shapefile someone emailed
you, not find out which district each customer sits in, not tell whether a
cluster on the map was real or a trick of the eye, and not do any of it fast
enough on a dataset above a few thousand rows.

**Geometry**: area and length on the sphere, centroids, convex hulls,
Douglas-Peucker simplification, polygon intersection and containment,
corridors around lines, and Vincenty distance for when 0.5% matters.

**Spatial index**: a uniform-grid index that makes radius and
nearest-neighbour queries ~280× faster at 50 000 features, built automatically
once a dataset is large enough to need one.

**Analysis**: DBSCAN and k-means on true great-circle distances, spatial
joins, hotspot grids, weighted centres of mass, and the Clark & Evans statistic
for whether a pattern is genuinely clustered.

**Formats**: WKT, GPX, KML, NDJSON, and a pure-Python Esri shapefile reader.
The shapefile reader is the headline: `.shp` is a documented binary format, so
reading it needs `struct`, not GDAL.

**Query language**: `geo.query("population > 1e6 and state != 'Delhi'")`,
parsed rather than `eval`'d, so a query from a config file or a web form is
safe to run.

**Rendering**: polygons and lines on interactive maps, marker clustering,
five basemaps, choropleths, and static SVG/PNG export with a PNG encoder built
on `zlib`.

**CLI**: `qoregeo info`, `map`, `convert`, `distance`, `nearest`, `within`,
`stats`, `image`, `geocode`.

**Geocoding**: opt-in, rate-limited, cached, over `urllib`. The only part of
QOREgeo that touches the network, and it says so.

---

## v1.2: raster, time and streaming

Three gaps that turn up repeatedly once vector analysis is solved.

### Raster support (`qoregeo.raster`)

Elevation, rainfall, population density and satellite indices all arrive as
grids, and reading one currently means installing `rasterio`, which means GDAL
again.

- **ASCII Grid (`.asc`) and ESRI `.flt` readers**: plain text and plain
  IEEE floats. No excuse for a dependency.
- **GeoTIFF reader, uncompressed and DEFLATE**: TIFF is a documented tag
  format and DEFLATE is `zlib`. LZW is a stretch goal; JPEG-in-TIFF is out of
  scope.
- `sample(lat, lng)`: the value at a point, with bilinear interpolation.
- `zonal_stats(polygon)`: mean, min, max and sum inside a shape. This is the
  operation people install a raster stack for.
- `contour(levels)`: marching squares from a grid to GeoJSON lines.
- `to_points()` / `resample()`, bridge back to the vector API.

**Done when** a DEM can be loaded, sampled along a route to produce an
elevation profile, and summarised per district, with no third-party package.

### Time-aware geometry (`qoregeo.temporal`)

Every GPS track, delivery run and asset feed is a *trajectory*, not a point
cloud, and treating it as points throws away most of the information.

- `Trajectory`: points plus timestamps, from GPX and from any CSV with a
  time column.
- Speed, acceleration and heading per segment; stop and dwell detection.
- `position_at(t)`: interpolate where something was at a given moment.
- `simplify_temporal()`: trajectory-aware compression that keeps stops.
- `co_location(a, b, within_km, within_minutes)`: were these two things in
  the same place at the same time? Contact tracing, fleet handovers, and
  meeting detection are all this one query.
- Time-sliced maps: an animated HTML timeline of a day's movement.

**Done when** a day of GPS logs yields a stop-and-move summary and an animated
map without pandas.

### Streaming and larger-than-memory data

Today everything is a list in RAM, which caps usable dataset size well below
what the algorithms could handle.

- `GeoEngine.iter_load()`: stream features from NDJSON and CSV, processing
  without materialising.
- Chunked `save()` for the same formats.
- An on-disk index for datasets past a few million features.
- `--stream` on the CLI for pipelines.

**Done when** a 5 GB NDJSON extract can be filtered by bounding box on a
machine with 1 GB free.

---

## v1.3: topology and networks

Two capabilities that are conspicuously missing once you try to do real GIS.

### Topological overlay (`qoregeo.overlay`)

Right now `intersects()` answers yes or no. Real work needs the resulting
shape.

- Polygon clipping (Greiner-Hormann or Vatti) giving true
  `intersection`, `union`, `difference` and `symmetric_difference`.
- `dissolve(by=column)`: merge adjacent polygons sharing an attribute. The
  single most requested GIS operation after the spatial join.
- Voronoi diagrams and Delaunay triangulation, service areas from point
  locations, without a Voronoi library.
- `validate_topology()` / `repair()`, self-intersections, unclosed rings,
  wrong winding order. Real boundary files are full of these.

**Risk, stated plainly:** robust polygon clipping is genuinely hard. Degenerate
cases (touching edges, collinear vertices, coincident points) are where these
algorithms break, and a subtly wrong result is worse than none. This ships with
an exhaustive property-based test suite or it does not ship.

### Network analysis (`qoregeo.network`)

Routing along a real network rather than in a straight line.

- Load a graph from OSM XML/PBF extracts or any edge list.
- Dijkstra and A* shortest paths with pluggable cost functions.
- Isochrones. Everywhere reachable within *n* minutes. This is what powers
  "which branch actually serves this address".
- Multi-stop routing on the network, replacing the straight-line TSP.
- Map matching, snap a noisy GPS trace to the road network.

**Done when** a city extract loads and answers 10 000 shortest-path queries a
second on a laptop.

---

## v2.0: performance, plugins, and QORE OS

The first release that may make breaking changes. Reserved for things that
cannot be done compatibly.

### Breaking changes, and why

- **Drop Python 3.8** (end-of-life since October 2024). This unlocks builtin
  generics, `X | Y` unions, `functools.cache` and the walrus operator
  throughout, and removes the type-annotation compromises documented in
  `pyproject.toml`.
- **`GeoEngine` becomes immutable throughout.** Today most operations return a
  new engine but `load()` mutates in place. One rule is easier to reason about
  than two.
- **Consistent British spelling with US aliases** across the whole API, rather
  than the current partial coverage.
- A documented migration guide, and a `qoregeo.compat` shim for one full minor
  cycle.

### Performance

Pure Python has a ceiling, and the honest answer is to reach it before blaming
the language.

- Optional `array`/`memoryview` storage for coordinates, cutting memory per
  feature by roughly 10×.
- Vectorised batch distance paths that avoid per-call overhead.
- Optional `multiprocessing` for embarrassingly parallel work (clustering,
  batch geocoding, zonal statistics).
- **An optional, never-required NumPy fast path.** If NumPy is importable,
  bulk operations use it; if not, everything still works. The zero-dependency
  promise is about what you *must* install, not what you *may*.
- A published benchmark suite versus GeoPandas/Shapely, with the losses
  reported as prominently as the wins.

### Plugin architecture

- Entry-point discovery so third parties can register formats, basemaps,
  distance metrics and analysis functions without patching the core.
- A stable public API contract, versioned separately from the package.

### QORE OS integration

This is the part of the name that has not yet been earned, and it should be
described precisely rather than aspirationally.

- A client for QORE OS spatial services, so the same API can dispatch to a
  remote engine when one is available.
- Transparent local fallback: if QORE OS is unreachable, the pure-Python
  implementation runs. **No operation ever requires a network.**
- Shared result caching between local and remote execution.

---

## v3.0: research track

Genuinely speculative. Listed to be honest about the ambition, and honest that
none of it is scheduled.

### Quantum spatial algorithms

The claim worth being careful about: quantum computing does not make every
spatial problem faster, and current hardware makes almost none of them faster
in practice. What is theoretically sound:

- **Grover-style search** offers a quadratic speed-up on unstructured nearest
  neighbour, but a classical spatial index already beats brute force by more
  than that on real data. The honest use case is high-dimensional similarity,
  not lat/lng.
- **QAOA for routing** is a genuine research direction for the TSP and vehicle
  routing problems in `qoregeo.routing`. It is not yet competitive with 2-opt
  plus Or-opt on the problem sizes businesses actually plan.
- **Quantum annealing for facility location and districting**: combinatorial
  problems where the classical versions are already NP-hard and the heuristics
  are already approximate.

The commitment: any quantum feature ships **behind a classical implementation
that is always available**, with published benchmarks showing where the quantum
path actually wins. If it never wins, that gets published too.

### GeoAI

- Learned spatial embeddings from geohash sequences.
- Anomaly detection on trajectories.
- Natural-language spatial queries compiled to the existing `query()` grammar.
  The parser already provides the safe target language.

### WebAssembly

- Compile the core to WASM via Pyodide so QOREgeo runs in the browser.
- A JavaScript binding sharing the same GeoJSON contract.
- Interactive maps that compute in the page instead of shipping precomputed
  output.

---

## Standing commitments

These hold across every release. They are the product.

1. **Zero required dependencies.** Optional accelerators may exist; nothing is
   ever mandatory.
2. **No C extensions in the core.** If it needs a compiler, it is not QOREgeo.
3. **Everything works offline** except geocoding, which is opt-in and says so.
4. **Errors teach.** Every exception names the problem and shows the fix.
5. **Tests before features.** Numerical code is tested against analytic results
   or brute-force implementations, not against its own output.
6. **Semantic versioning.** Breaking changes only at majors, with a migration
   guide.
7. **Python support follows upstream.** Currently 3.8+; the floor rises only at
   a major version.

---

## Contributing to the roadmap

The most useful contribution is a concrete use case that QOREgeo handles badly.
Open an issue describing what you were trying to do, what you had to install
instead, and what the data looked like. Priority follows real blockers, not
feature-list length.

- Issues: <https://github.com/bosekarmegam/qoregeo/issues>
- Contributing guide: [CONTRIBUTING.md](CONTRIBUTING.md)
