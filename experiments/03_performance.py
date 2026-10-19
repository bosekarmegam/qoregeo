"""Experiment 03: speed and footprint of qoregeo.

What is measured:

1. Dataset operations on seeded synthetic point datasets (N = 1,000, 10,000
   and 100,000 rows of name, category, revenue, latitude, longitude inside
   India's bounding box), stored as CSV and GeoJSON in a temporary folder:
   load, attribute filters, ``where``, radius, bbox and polygon filters,
   ``nearest``, ``k_nearest``, ``sort_by`` + ``head``, ``save``,
   ``map_html()`` and ``image()`` (PNG and SVG). Each operation is timed
   with repeats (median and min) with the garbage collector on, as users
   run it, and in alternating loops with it off, to show how much of the
   cost at large N is cyclic garbage collection.
2. Context baselines on the same data: a plain-Python CSV reader that builds
   the same Feature dicts, ``json.load``, a numpy vectorized haversine radius
   mask and ``shapely.contains_xy`` on prepared arrays.
3. Micro-benchmarks in calls per second: ``distance()`` (haversine and
   vincenty) against geopy, the ``haversine`` package and geographiclib, and
   point-in-polygon on a 64-vertex polygon against shapely (``Point.within``,
   prepared geometry, scalar and vectorized ``contains_xy``).
4. Footprint: cold import time (``python -X importtime``, median of fresh
   processes), tracemalloc peak while loading the largest CSV, and the size
   of the qoregeo wheel against the manylinux x86_64 cp312 wheels of shapely,
   pyproj, numpy, pandas, pyogrio and geopandas (downloaded with pip; this
   is the only step that uses the network, and ``--no-network`` skips it).

Run from the repository root::

    python experiments/03_performance.py            # full run (about 5 to 7 minutes)
    python experiments/03_performance.py --quick    # reduced sizes, about 15 seconds

Outputs: ``experiments/results/performance.json`` and
``docs/assets/experiments/performance-*.svg``.

Timing numbers depend on the machine and on what else runs on it. Run the
script on an idle machine for numbers you want to quote.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as _dt
import gc
import importlib.metadata as _md
import json
import math
import os
import platform
import random
import re
import shlex
import statistics
import subprocess
import sys
import tempfile
import time
import warnings
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import matplotlib
import matplotlib.ticker
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

import qoregeo  # noqa: E402
from qoregeo import GeoEngine  # noqa: E402
from qoregeo import utils as qutils  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
NAME = "performance"
RESULTS_PATH = REPO / "experiments" / "results" / f"{NAME}.json"
FIG_DIR = REPO / "docs" / "assets" / "experiments"

# India's bounding box, [west, south, east, north].
INDIA_BBOX = [68.1, 6.5, 97.4, 35.7]

# Points cluster around these cities (75 %) on top of a uniform background (25 %).
CITIES: list[tuple[str, float, float]] = [
    ("Delhi", 28.6139, 77.2090),
    ("Mumbai", 19.0760, 72.8777),
    ("Bengaluru", 12.9716, 77.5946),
    ("Kolkata", 22.5726, 88.3639),
    ("Chennai", 13.0827, 80.2707),
    ("Hyderabad", 17.3850, 78.4867),
    ("Pune", 18.5204, 73.8567),
    ("Ahmedabad", 23.0225, 72.5714),
    ("Jaipur", 26.9124, 75.7873),
    ("Lucknow", 26.8467, 80.9462),
]
CATEGORIES = ["grocery", "pharmacy", "cafe", "fuel", "bank", "school", "clinic", "hotel"]
CATEGORY_WEIGHTS = [24, 14, 14, 10, 12, 10, 8, 8]
CLUSTER_SIGMA_KM = 40.0
REVENUE_MU, REVENUE_SIGMA = 11.0, 0.8     # lognormal, median about 59,900
REVENUE_THRESHOLD = 150_000               # about the 87th percentile

# Query parameters used for every N.
RADIUS_CENTER = (28.6139, 77.2090)        # Delhi
RADIUS_KM = 25.0
BBOX_QUERY = {"min_lat": 18.0, "min_lng": 72.5, "max_lat": 19.6, "max_lng": 74.2}   # Mumbai, Pune
BUFFER_CENTER = (19.0760, 72.8777)        # Mumbai
BUFFER_KM = 50.0
NEAREST_POINT = (12.9716, 77.5946)        # Bengaluru
K = 10

SIZES = {
    "full": {
        "n_values": [1_000, 10_000, 100_000],
        "repeats": 5,
        "min_time_s": 0.2,
        "gc_off_pass": True,
        "image_max_n": 100_000,
        "micro_pairs": 2_000,
        "micro_repeats": 7,
        "vector_n": [2_000, 100_000],
        "vector_min_time_s": 0.2,
        "pip_points": 2_000,
        "polygon_vertices": 64,
        "import_runs": 10,
        "memory_n": 100_000,
    },
    "quick": {
        "n_values": [500, 2_000],
        "repeats": 2,
        "min_time_s": 0.005,
        "gc_off_pass": True,
        "image_max_n": 2_000,
        "micro_pairs": 200,
        "micro_repeats": 3,
        "vector_n": [2_000],
        "vector_min_time_s": 0.02,
        "pip_points": 200,
        "polygon_vertices": 64,
        "import_runs": 2,
        "memory_n": 2_000,
    },
}

# Operations drawn in Figure 1, per panel: (key, label).
PANELS: list[tuple[str, list[tuple[str, str]], tuple[str, str] | None]] = [
    ("Load", [
        ("load_csv", "load CSV"),
        ("load_csv_infer_types", "load CSV, infer_types=True"),
        ("load_geojson", "load GeoJSON"),
    ], ("baseline_csv_feature_dicts", "plain-Python CSV reader (floor)")),
    ("Attribute filters and sort", [
        ("filter_eq", "filter category == 'cafe'"),
        ("filter_ge", "filter revenue >= 150,000"),
        ("where", "where(lambda)"),
        ("sort_by_head", "sort_by + head(10)"),
    ], None),
    ("Spatial queries", [
        ("filter_by_radius", "filter_by_radius 25 km"),
        ("filter_by_bbox", "filter_by_bbox"),
        ("filter_by_polygon_64", "filter_by_polygon, 64-vertex"),
        ("k_nearest", "k_nearest k=10"),
    ], ("baseline_shapely_contains_xy", "shapely.contains_xy, arrays ready")),
    ("Write and render", [
        ("save_csv", "save CSV"),
        ("save_geojson", "save GeoJSON"),
        ("map_html", "map_html()"),
        ("image_png", "image() PNG"),
        ("image_svg", "image() SVG"),
    ], None),
]

# Chart colours: categorical slots 1 to 5 of the dataviz reference palette
# (validated for adjacent pairs on a light surface), plus neutral greys.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
MARKERS = ["o", "s", "^", "D", "v"]
C_QG = SERIES[0]
C_VEC = SERIES[1]
C_GREY = "#8a8985"
C_GREY_LIGHT = "#c3c2b7"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e4e3df"


# Small helpers

def _rng(seed: int, name: str) -> random.Random:
    """An independent, reproducible stream per experiment section."""
    return random.Random(f"{seed}:{name}")


def _f(x: Any) -> Any:
    """Make a value JSON-safe (numpy scalars to Python, NaN and inf to None)."""
    if isinstance(x, (np.floating, float)):
        x = float(x)
        return x if math.isfinite(x) else None
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, dict):
        return {str(k): _f(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_f(v) for v in x]
    return x


def _version(dist: str) -> str | None:
    try:
        return _md.version(dist)
    except _md.PackageNotFoundError:
        return None


def _stats(values: list[float]) -> dict[str, float]:
    return {"median": statistics.median(values), "min": min(values), "max": max(values)}


@contextlib.contextmanager
def _gc_state(enabled: bool):  # type: ignore[no-untyped-def]
    was = gc.isenabled()
    gc.collect()
    if enabled:
        gc.enable()
    else:
        gc.disable()
    try:
        yield
    finally:
        if was:
            gc.enable()
        else:
            gc.disable()


def time_call(fn: Callable[[], Any], repeats: int, min_time: float,
              modes: tuple[bool, ...] = (True,),
              on_first: Callable[[Any], dict[str, Any]] | None = None) -> dict[bool, dict[str, Any]]:
    """Time ``fn()`` timeit-style: calibrate a loop count, then ``repeats`` timed loops.

    ``modes`` lists garbage-collector states (True = on). With two modes the
    loops alternate (on, off, on, off, ...), so both see the same background
    load. The first call is a warm-up; ``on_first`` extracts facts from its
    result, which is then dropped before timing. Each loop starts after a full
    ``gc.collect()``. Returns seconds per call, per mode.
    """
    with _gc_state(modes[0]):
        t0 = time.perf_counter()
        result = fn()
        first = time.perf_counter() - t0
    facts = on_first(result) if on_first is not None else {}
    del result
    number = 1 if first >= min_time else min(100_000, max(1, math.ceil(min_time / max(first, 1e-7))))
    per_call: dict[bool, list[float]] = {m: [] for m in modes}
    for _ in range(repeats):
        for m in modes:
            with _gc_state(m):
                t0 = time.perf_counter()
                for _ in range(number):
                    fn()
                per_call[m].append((time.perf_counter() - t0) / number)
    return {m: {"median_s": statistics.median(v), "min_s": min(v), "max_s": max(v),
                "first_call_s": first, "number": number, "repeats": repeats,
                "gc_enabled": m, **facts}
            for m, v in per_call.items()}


def time_per_item(fn: Callable[..., Any], items: list[tuple[Any, ...]], repeats: int
                  ) -> dict[str, float]:
    """Per-call time of ``fn(*item)`` over ``items``, garbage collector off (as timeit does)."""
    for it in items[:20]:
        fn(*it)
    per_call: list[float] = []
    with _gc_state(False):
        for _ in range(repeats):
            t0 = time.perf_counter()
            for it in items:
                fn(*it)
            per_call.append((time.perf_counter() - t0) / len(items))
    med = statistics.median(per_call)
    return {"median_us": med * 1e6, "min_us": min(per_call) * 1e6,
            "calls_per_s_median": 1.0 / med if med > 0 else None,
            "calls_per_s_best": 1.0 / min(per_call) if min(per_call) > 0 else None}


def _loglog_slope(ns: list[int], ts: list[float]) -> float | None:
    if len(ns) < 2 or any(t <= 0 for t in ts):
        return None
    x = np.log10(np.array(ns, dtype=float))
    y = np.log10(np.array(ts, dtype=float))
    return float(np.polyfit(x, y, 1)[0])


# Synthetic data

def make_rows(seed: int, n: int) -> list[tuple[str, str, str, str, str]]:
    """Seeded rows (name, category, revenue, latitude, longitude) as formatted strings.

    The rows for a smaller N are a prefix of the rows for a larger N, so every
    dataset is a subset of the largest one.
    """
    rng = _rng(seed, "dataset")
    west, south, east, north = INDIA_BBOX
    rows = []
    for i in range(n):
        if rng.random() < 0.75:
            _, c_lat, c_lng = CITIES[rng.randrange(len(CITIES))]
            while True:
                lat = c_lat + rng.gauss(0.0, CLUSTER_SIGMA_KM) / 111.32
                lng = c_lng + rng.gauss(0.0, CLUSTER_SIGMA_KM) / (111.32 * math.cos(math.radians(c_lat)))
                if south <= lat <= north and west <= lng <= east:
                    break
        else:
            lat = rng.uniform(south, north)
            lng = rng.uniform(west, east)
        cat = rng.choices(CATEGORIES, CATEGORY_WEIGHTS)[0]
        revenue = rng.lognormvariate(REVENUE_MU, REVENUE_SIGMA)
        rows.append((f"Site {i:06d}", cat, f"{revenue:.2f}", f"{lat:.6f}", f"{lng:.6f}"))
    return rows


def write_dataset(rows: list[tuple[str, ...]], folder: Path, n: int) -> dict[str, Any]:
    csv_path = folder / f"points_{n}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "category", "revenue", "latitude", "longitude"])
        w.writerows(rows[:n])
    gj_path = folder / f"points_{n}.geojson"
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [float(r[4]), float(r[3])]},
         "properties": {"name": r[0], "category": r[1], "revenue": float(r[2])}}
        for r in rows[:n]]}
    with open(gj_path, "w", encoding="utf-8") as fh:
        json.dump(fc, fh)
    return {"n": n, "csv": str(csv_path), "geojson": str(gj_path),
            "csv_bytes": csv_path.stat().st_size, "geojson_bytes": gj_path.stat().st_size}


def star_polygon(seed: int, vertices: int) -> dict[str, Any]:
    """A seeded, simple, non-convex star-shaped polygon in central India."""
    rng = _rng(seed, "polygon")
    c_lat, c_lng = 22.5, 79.0
    ring = []
    for i in range(vertices):
        ang = 2.0 * math.pi * i / vertices
        r = rng.uniform(3.0, 8.0)
        lat = c_lat + r * math.sin(ang)
        lng = c_lng + r * math.cos(ang) / math.cos(math.radians(c_lat))
        ring.append([round(lng, 6), round(lat, 6)])
    ring.append(list(ring[0]))
    return {"type": "Polygon", "coordinates": [ring]}


# Dataset operations

def _arrays(geo: GeoEngine) -> tuple[np.ndarray, np.ndarray]:
    feats = geo.get_features()
    lng = np.fromiter((f["geometry"]["coordinates"][0] for f in feats), float, count=len(feats))
    lat = np.fromiter((f["geometry"]["coordinates"][1] for f in feats), float, count=len(feats))
    return lat, lng


def _np_haversine_km(lat1: np.ndarray, lng1: np.ndarray, lat2: Any, lng2: Any) -> np.ndarray:
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlmb = np.radians(lng2) - np.radians(lng1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2.0 * qutils.EARTH_RADIUS_KM * np.arcsin(np.minimum(1.0, np.sqrt(a)))


def run_operations(ds: dict[str, Any], out_dir: Path, sz: dict[str, Any],
                   polygon64: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Time every dataset operation for one N, garbage collector on and off.

    Returns ``(gc_on, gc_off)``, each {op: timing + result facts}. The three
    loads run first, with no dataset in memory; the other operations then run
    on the engines loaded from the CSV and the GeoJSON file, both held in memory.
    """
    import shapely

    n = ds["n"]
    reps, mt = sz["repeats"], sz["min_time_s"]
    modes = (True, False) if sz["gc_off_pass"] else (True,)
    csv_path, gj_path = ds["csv"], ds["geojson"]
    out: dict[str, Any] = {}
    out_off: dict[str, Any] = {}

    def rec(key: str, fn: Callable[[], Any],
            info: Callable[[Any], dict[str, Any]] | None = None) -> None:
        res = time_call(fn, reps, mt, modes, info)
        for m, target in ((True, out), (False, out_off)):
            if m in res:
                target[key] = res[m]
                target[key]["features_per_s"] = n / res[m]["median_s"] if res[m]["median_s"] > 0 else None

    def count(r: Any) -> dict[str, Any]:
        return {"result_count": r.count()}

    rec("load_csv", lambda: GeoEngine(verbose=False).load(csv_path), count)
    rec("load_csv_infer_types", lambda: GeoEngine(verbose=False).load(csv_path, infer_types=True), count)
    rec("load_geojson", lambda: GeoEngine(verbose=False).load(gj_path), count)
    g = GeoEngine(verbose=False).load(csv_path)
    g_num = GeoEngine(verbose=False).load(gj_path)

    rec("filter_eq", lambda: g.filter("category", "cafe"), count)
    rec("filter_ge", lambda: g.filter("revenue", REVENUE_THRESHOLD, op=">="), count)
    rec("filter_ge_numeric_properties",
        lambda: g_num.filter("revenue", REVENUE_THRESHOLD, op=">="), count)
    rec("where", lambda: g.where(lambda p: float(p["revenue"]) >= REVENUE_THRESHOLD), count)
    rec("filter_by_radius", lambda: g.filter_by_radius(*RADIUS_CENTER, radius=RADIUS_KM), count)
    rec("filter_by_bbox", lambda: g.filter_by_bbox(**BBOX_QUERY), count)
    zone = g.buffer(BUFFER_CENTER, BUFFER_KM)
    rec("filter_by_polygon_buffer", lambda: g.filter_by_polygon(zone), count)
    zone_plain = {"type": "Polygon", "coordinates": zone["coordinates"]}
    rec("filter_by_polygon_buffer_as_plain_polygon", lambda: g.filter_by_polygon(zone_plain), count)
    rec("filter_by_polygon_64", lambda: g.filter_by_polygon(polygon64), count)
    rec("nearest", lambda: g.nearest(NEAREST_POINT),
        lambda r: {"result_index": r["index"], "result_distance_km": r["distance"]})
    rec("k_nearest", lambda: g.k_nearest(NEAREST_POINT, k=K), count)
    rec("sort_by_head", lambda: g.sort_by("revenue", descending=True).head(10), count)

    p_csv, p_gj = out_dir / "out.csv", out_dir / "out.geojson"
    rec("save_csv", lambda: g.save(p_csv), lambda _r: {"output_bytes": p_csv.stat().st_size})
    rec("save_geojson", lambda: g.save(p_gj), lambda _r: {"output_bytes": p_gj.stat().st_size})
    rec("map_html", g.map_html, lambda r: {"output_bytes": len(r.encode("utf-8"))})
    if n <= sz["image_max_n"]:
        p_png, p_svg = out_dir / "out.png", out_dir / "out.svg"
        rec("image_png", lambda: g.image(p_png), lambda _r: {"output_bytes": p_png.stat().st_size})
        rec("image_svg", lambda: g.image(p_svg), lambda _r: {"output_bytes": p_svg.stat().st_size})

    # Context baselines on the same file and the same queries.
    def csv_floor() -> list[dict[str, Any]]:
        # The same Feature dicts qoregeo builds (coordinates out of the properties),
        # with no delimiter sniffing, validation or error reporting.
        feats = []
        with open(csv_path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                lat, lng = float(r.pop("latitude")), float(r.pop("longitude"))
                feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lng, lat]},
                              "properties": r})
        return feats

    def json_floor() -> Any:
        with open(gj_path, encoding="utf-8") as fh:
            return json.load(fh)

    rec("baseline_csv_feature_dicts", csv_floor, lambda r: {"result_count": len(r)})
    rec("baseline_json_load", json_floor, lambda r: {"result_count": len(r["features"])})
    rec("baseline_build_numpy_arrays", lambda: _arrays(g), lambda r: {"result_count": len(r[0])})
    lat, lng = _arrays(g)
    rec("baseline_numpy_radius_mask",
        lambda: int(np.count_nonzero(_np_haversine_km(lat, lng, *RADIUS_CENTER) <= RADIUS_KM)),
        lambda r: {"result_count": r})
    spoly = shapely.Polygon(polygon64["coordinates"][0])
    shapely.prepare(spoly)
    rec("baseline_shapely_contains_xy",
        lambda: int(np.count_nonzero(shapely.contains_xy(spoly, lng, lat))),
        lambda r: {"result_count": r})

    # Sanity checks: the same question answered two ways must agree.
    out["_checks"] = {
        "buffer_polygon_equals_radius_50km": (
            g.filter_by_polygon(zone).count() == g.filter_by_radius(*BUFFER_CENTER, radius=BUFFER_KM).count()),
        "polygon64_equals_shapely": (
            g.filter_by_polygon(polygon64).count() == out["baseline_shapely_contains_xy"]["result_count"]),
        "radius_equals_numpy": (
            g.filter_by_radius(*RADIUS_CENTER, radius=RADIUS_KM).count()
            == out["baseline_numpy_radius_mask"]["result_count"]),
        "filter_ge_strings_equals_numbers": (
            out["filter_ge"]["result_count"] == out["filter_ge_numeric_properties"]["result_count"]),
    }
    return out, out_off


# Micro-benchmarks

def run_micro(seed: int, sz: dict[str, Any], polygon64: dict[str, Any]) -> dict[str, Any]:
    import geopy.distance as gd
    import shapely
    import shapely.prepared
    from geographiclib.geodesic import Geodesic
    from haversine import haversine as hv_fn
    from haversine import haversine_vector

    geo = GeoEngine(verbose=False)
    W = Geodesic.WGS84
    west, south, east, north = INDIA_BBOX
    rng = _rng(seed, "micro-pairs")

    def pt() -> tuple[float, float]:
        return (rng.uniform(south, north), rng.uniform(west, east))

    pairs = [(pt(), pt()) for _ in range(sz["micro_pairs"])]
    reps = sz["micro_repeats"]
    vmt = sz["vector_min_time_s"]

    # Every callable gets the same lambda wrapper, so wrapper cost is equal.
    dist_fns: dict[str, Callable[[Any, Any], Any]] = {
        "qoregeo distance() haversine": lambda a, b: geo.distance(a, b),
        "qoregeo distance() vincenty": lambda a, b: geo.distance(a, b, method="vincenty"),
        "qoregeo utils._haversine_km (internal, no validation)": lambda a, b: qutils._haversine_km(a, b),
        "qoregeo utils._vincenty_km (internal, no validation)": lambda a, b: qutils._vincenty_km(a, b),
        "haversine package haversine()": lambda a, b: hv_fn(a, b),
        "geopy great_circle().km": lambda a, b: gd.great_circle(a, b).km,
        "geopy geodesic().km": lambda a, b: gd.geodesic(a, b).km,
        "geographiclib Geodesic.WGS84.Inverse (distance only)":
            lambda a, b: W.Inverse(a[0], a[1], b[0], b[1], Geodesic.DISTANCE)["s12"],
    }
    distance: dict[str, Any] = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        distance["empty lambda (loop and call overhead)"] = time_per_item(lambda a, b: None, pairs, reps)
        for label, fn in dist_fns.items():
            distance[label] = time_per_item(fn, pairs, reps)

    # Vectorized references, per pair.
    vec: dict[str, Any] = {}
    for vn in sz["vector_n"]:
        vr = _rng(seed, f"micro-vector-{vn}")
        a = np.array([(vr.uniform(south, north), vr.uniform(west, east)) for _ in range(vn)])
        b = np.array([(vr.uniform(south, north), vr.uniform(west, east)) for _ in range(vn)])
        t_np = time_call(lambda a=a, b=b: _np_haversine_km(a[:, 0], a[:, 1], b[:, 0], b[:, 1]),
                         reps, vmt, (False,))[False]
        t_hv = time_call(lambda a=a, b=b: haversine_vector(a, b), reps, vmt, (False,))[False]
        vec[f"numpy haversine, n={vn}"] = {"median_us": t_np["median_s"] / vn * 1e6,
                                           "min_us": t_np["min_s"] / vn * 1e6,
                                           "calls_per_s_median": vn / t_np["median_s"]}
        vec[f"haversine package haversine_vector, n={vn}"] = {
            "median_us": t_hv["median_s"] / vn * 1e6, "min_us": t_hv["min_s"] / vn * 1e6,
            "calls_per_s_median": vn / t_hv["median_s"]}
    distance["_vectorized_per_pair"] = vec

    # Like-with-like check: the same sphere gives the same numbers.
    q = np.array([qutils._haversine_km(a, b) for a, b in pairs])
    h = np.array([hv_fn(a, b) for a, b in pairs])
    gc_km = np.array([gd.great_circle(a, b).km for a, b in pairs])
    distance["_agreement"] = {
        "max_abs_diff_km_qoregeo_vs_haversine_package": float(np.max(np.abs(q - h))),
        "max_rel_diff_qoregeo_vs_geopy_great_circle": float(np.max(np.abs(q - gc_km) / q)),
        "earth_radius_km": {"qoregeo": qutils.EARTH_RADIUS_KM, "haversine package": 6371.0088,
                            "geopy great_circle": gd.EARTH_RADIUS},
        "pair_distance_km": _stats(q.tolist()),
    }

    # Point in polygon, 64 vertices.
    ring = polygon64["coordinates"][0]
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    pr = _rng(seed, "micro-pip")
    pts = [(pr.uniform(min(ys), max(ys)), pr.uniform(min(xs), max(xs))) for _ in range(sz["pip_points"])]
    tester = qutils._polygon_tester(polygon64)
    s_plain = shapely.Polygon(ring)
    s_prepped_obj = shapely.prepared.prep(shapely.Polygon(ring))
    s_native = shapely.Polygon(ring)
    shapely.prepare(s_native)
    point_cls = shapely.Point
    pip_fns: dict[str, Callable[[float, float], Any]] = {
        "qoregeo point_in_polygon() (validates the polygon on every call)":
            lambda la, lo: geo.point_in_polygon((la, lo), polygon64),
        "qoregeo prepared tester (utils._polygon_tester, used by filter_by_polygon)":
            lambda la, lo: tester(la, lo),
        "shapely Point(x, y).within(polygon)": lambda la, lo: point_cls(lo, la).within(s_plain),
        "shapely prepared.prep(polygon).contains(Point(x, y))":
            lambda la, lo: s_prepped_obj.contains(point_cls(lo, la)),
        "shapely.contains_xy(prepared polygon, x, y), scalar":
            lambda la, lo: shapely.contains_xy(s_native, lo, la),
    }
    pip: dict[str, Any] = {"empty lambda (loop and call overhead)":
                           time_per_item(lambda la, lo: None, pts, reps)}
    for label, fn in pip_fns.items():
        pip[label] = time_per_item(fn, pts, reps)
    vec_pip: dict[str, Any] = {}
    for vn in sz["vector_n"]:
        vr = _rng(seed, f"micro-pip-vector-{vn}")
        vx = np.array([vr.uniform(min(xs), max(xs)) for _ in range(vn)])
        vy = np.array([vr.uniform(min(ys), max(ys)) for _ in range(vn)])
        t = time_call(lambda vx=vx, vy=vy: shapely.contains_xy(s_native, vx, vy), reps, vmt,
                      (False,))[False]
        vec_pip[f"shapely.contains_xy vectorized, n={vn}"] = {
            "median_us": t["median_s"] / vn * 1e6, "min_us": t["min_s"] / vn * 1e6,
            "calls_per_s_median": vn / t["median_s"]}
    pip["_vectorized_per_point"] = vec_pip
    q_in = [tester(la, lo) for la, lo in pts]
    q_pub = [geo.point_in_polygon((la, lo), polygon64) for la, lo in pts]
    s_in = [bool(v) for v in shapely.contains_xy(s_native, [p[1] for p in pts], [p[0] for p in pts])]
    pip["_agreement"] = {
        "points": len(pts),
        "inside_fraction": sum(s_in) / len(s_in),
        "mismatches_prepared_vs_shapely": sum(a != b for a, b in zip(q_in, s_in, strict=True)),
        "mismatches_public_vs_shapely": sum(a != b for a, b in zip(q_pub, s_in, strict=True)),
        "polygon_vertices": len(ring) - 1,
        "polygon_is_valid_in_shapely": bool(s_plain.is_valid),
    }
    return {"pairs": len(pairs), "repeats": reps, "distance": distance, "point_in_polygon": pip,
            "note": ("Per-call loops run with the garbage collector off, like timeit. Every scalar "
                     "callable is wrapped in the same kind of lambda; the empty-lambda row is that "
                     "shared overhead and is not subtracted. Vectorized rows exclude building the "
                     "numpy arrays.")}


# Footprint: import time

IMPORT_STATEMENTS = [
    ("import qoregeo", "import qoregeo"),
    ("from qoregeo import GeoEngine", "from qoregeo import GeoEngine"),
    ("import shapely", "import shapely"),
    ("import pyproj", "import pyproj"),
    ("import geopy", "import geopy"),
    ("from geopy.distance import geodesic", "from geopy.distance import geodesic"),
    ("import numpy (context)", "import numpy"),
]

_IMPORT_LINE = re.compile(r"^import time:\s*(\d+)\s*\|\s*(\d+)\s*\|( +)(\S+)\s*$")


def _importtime_rows(stmt: str, cwd: str, env: dict[str, str]) -> list[tuple[str, int, int, int]]:
    p = subprocess.run([sys.executable, "-X", "importtime", "-c", stmt], cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=120, check=False)
    if p.returncode != 0:
        raise RuntimeError(f"{stmt!r} failed: {p.stderr[-500:]}")
    rows = []
    for line in p.stderr.splitlines():
        m = _IMPORT_LINE.match(line)
        if m:
            depth = (len(m.group(3)) - 1) // 2
            rows.append((m.group(4), int(m.group(1)), int(m.group(2)), depth))
    return rows


def _wall(stmt: str, cwd: str, env: dict[str, str]) -> float:
    t0 = time.perf_counter()
    subprocess.run([sys.executable, "-c", stmt], cwd=cwd, env=env, capture_output=True,
                   timeout=120, check=True)
    return time.perf_counter() - t0


def run_import_times(runs: int, seed: int, cwd: str) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHONPROFILEIMPORTTIME")}
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    available = []
    for label, stmt in IMPORT_STATEMENTS:
        try:
            _importtime_rows(stmt, cwd, env)       # warm-up, writes .pyc files
            available.append((label, stmt))
        except (RuntimeError, subprocess.SubprocessError):
            pass
    baseline_names = {r[0] for r in _importtime_rows("pass", cwd, env)}
    cum: dict[str, list[float]] = {label: [] for label, _ in available}
    wall: dict[str, list[float]] = {label: [] for label, _ in available}
    wall_pass: list[float] = []
    selfs: dict[str, dict[str, list[int]]] = {label: {} for label, _ in available}
    order = list(available)
    rng = _rng(seed, "import-order")
    for _ in range(runs):
        rng.shuffle(order)
        wall_pass.append(_wall("pass", cwd, env))
        for label, stmt in order:
            rows = _importtime_rows(stmt, cwd, env)
            new = [r for r in rows if r[0] not in baseline_names]
            cum[label].append(sum(r[2] for r in new if r[3] == 0) / 1000.0)
            for name, s_us, _c, _d in new:
                selfs[label].setdefault(name, []).append(s_us)
            wall[label].append(_wall(stmt, cwd, env))
    out: dict[str, Any] = {}
    pass_med = statistics.median(wall_pass)
    for label, stmt in available:
        top = sorted(((statistics.median(v), k) for k, v in selfs[label].items()), reverse=True)[:8]
        mods = {k for k, v in selfs[label].items()}
        out[label] = {
            "statement": stmt,
            "importtime_cumulative_ms": _stats(cum[label]),
            "process_wall_ms": _stats([w * 1000 for w in wall[label]]),
            "process_wall_minus_empty_interpreter_ms": statistics.median(wall[label]) * 1000 - pass_med * 1000,
            "new_modules_imported": len(mods),
            "top_self_time_modules_ms": {k: v / 1000.0 for v, k in top},
        }
    out["_empty_interpreter_wall_ms"] = _stats([w * 1000 for w in wall_pass])
    out["_method"] = (f"Median of {runs} fresh interpreter processes per statement, in shuffled "
                      "order. importtime_cumulative_ms sums the top-level cumulative times that "
                      "python -X importtime reports for modules not already loaded by an empty "
                      "interpreter. .pyc files exist and the OS file cache is warm, so this is a "
                      "cold interpreter, not a cold disk. qoregeo is an editable install (a .pth "
                      "path entry), so its import reads the repository source tree.")
    return out


# Footprint: memory

_TRACE_CODE = r"""
import csv, gc, json, sys, time, tracemalloc
kind, path = sys.argv[1], sys.argv[2]
from qoregeo import GeoEngine
def load():
    if kind == "qoregeo load CSV":
        return GeoEngine(verbose=False).load(path)
    if kind == "qoregeo load CSV, infer_types=True":
        return GeoEngine(verbose=False).load(path, infer_types=True)
    if kind == "qoregeo load GeoJSON":
        return GeoEngine(verbose=False).load(path)
    if kind == "plain-Python CSV reader (same Feature dicts)":
        feats = []
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                lat, lng = float(r.pop("latitude")), float(r.pop("longitude"))
                feats.append({"type": "Feature", "geometry": {"type": "Point",
                              "coordinates": [lng, lat]}, "properties": r})
        return feats
    if kind == "json.load of the GeoJSON file":
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    raise SystemExit("unknown kind")
gc.collect()
mode = sys.argv[3]
if mode == "tracemalloc":
    tracemalloc.start()
    t0 = time.perf_counter()
    obj = load()
    dt = time.perf_counter() - t0
    cur, peak = tracemalloc.get_traced_memory()
    print(json.dumps({"current_bytes": cur, "peak_bytes": peak, "elapsed_s_under_tracemalloc": dt}))
else:
    def status():
        # Linux: VmHWM is the peak resident set of this process image (reset by exec,
        # unlike ru_maxrss, which keeps the parent's peak from before the fork).
        try:
            with open("/proc/self/status", encoding="ascii") as fh:
                rows = dict(line.split(":", 1) for line in fh if ":" in line)
            return {k: int(rows[k].split()[0]) * 1024 for k in ("VmRSS", "VmHWM")}
        except OSError:
            return None
    s0 = status()
    obj = load()
    s1 = status()
    if s0 is None:
        print(json.dumps({"rss_note": "no /proc/self/status on this platform"}))
    else:
        print(json.dumps({"rss_before_bytes": s0["VmRSS"], "rss_after_bytes": s1["VmRSS"],
                          "peak_rss_after_bytes": s1["VmHWM"]}))
"""


def run_memory(ds: dict[str, Any], cwd: str) -> dict[str, Any]:
    cases = [
        ("qoregeo load CSV", ds["csv"]),
        ("qoregeo load CSV, infer_types=True", ds["csv"]),
        ("qoregeo load GeoJSON", ds["geojson"]),
        ("plain-Python CSV reader (same Feature dicts)", ds["csv"]),
        ("json.load of the GeoJSON file", ds["geojson"]),
    ]
    out: dict[str, Any] = {"n": ds["n"], "csv_bytes": ds["csv_bytes"],
                           "geojson_bytes": ds["geojson_bytes"], "cases": {}}
    for kind, path in cases:
        res: dict[str, Any] = {}
        for mode in ("tracemalloc", "rss"):
            p = subprocess.run([sys.executable, "-c", _TRACE_CODE, kind, path, mode], cwd=cwd,
                               capture_output=True, text=True, timeout=600, check=False)
            if p.returncode != 0:
                res[f"{mode}_error"] = p.stderr[-500:]
                continue
            res.update(json.loads(p.stdout.strip().splitlines()[-1]))
        if "peak_bytes" in res:
            res["peak_bytes_per_feature"] = res["peak_bytes"] / ds["n"]
            res["retained_bytes_per_feature"] = res["current_bytes"] / ds["n"]
        if res.get("rss_after_bytes") is not None:
            res["rss_growth_bytes"] = res["rss_after_bytes"] - res["rss_before_bytes"]
            res["peak_rss_growth_bytes"] = res["peak_rss_after_bytes"] - res["rss_before_bytes"]
        out["cases"][kind] = res
    out["_method"] = ("Each case runs in a fresh interpreter after qoregeo is imported. tracemalloc "
                      "counts Python allocations only; peak is the high-water mark during the load, "
                      "current is what the loaded object keeps. rss_growth and peak_rss_growth come "
                      "from /proc/self/status (VmRSS, VmHWM) in a second, untraced run (Linux "
                      "only); they include allocator overhead and memory the allocator keeps.")
    return out


# Footprint: wheels

def _wheel_info(path: Path) -> dict[str, Any]:
    """Size facts of a wheel, read from its zip directory and METADATA (no code is run)."""
    info: dict[str, Any] = {"file": path.name, "bytes": path.stat().st_size}
    parts = path.name[:-4].split("-")
    info["distribution"], info["version"] = parts[0], parts[1]
    info["tags"] = "-".join(parts[2:])
    with zipfile.ZipFile(path) as z:
        entries = z.infolist()
        info["unpacked_bytes"] = sum(e.file_size for e in entries)
        info["files"] = len(entries)
        meta = next((e for e in entries if e.filename.endswith(".dist-info/METADATA")), None)
        if meta is not None:
            text = z.read(meta).decode("utf-8", "replace")
            m = re.search(r"^Requires-Python:\s*(.+)$", text, re.M)
            info["requires_python"] = m.group(1).strip() if m else None
        so = [e for e in entries if e.filename.endswith((".so", ".pyd", ".dylib")) or ".so." in e.filename]
        info["native_library_bytes_unpacked"] = sum(e.file_size for e in so)
    return info


def build_qoregeo_wheel(tmp: Path) -> dict[str, Any]:
    out_dir = tmp / "qoregeo-wheel"
    out_dir.mkdir()
    try:
        import hatchling  # noqa: F401
        isolation = ["--no-isolation"]
    except ImportError:
        isolation = []
    cmd = [sys.executable, "-m", "build", "--wheel", *isolation, "--outdir", str(out_dir), str(REPO)]
    try:
        p = subprocess.run(cmd, cwd=str(tmp), capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"{type(e).__name__}: {e}"}
    wheels = sorted(out_dir.glob("*.whl"))
    if p.returncode != 0 or not wheels:
        return {"error": (p.stderr or p.stdout)[-800:], "command": shlex.join(cmd)}
    info = _wheel_info(wheels[0])
    with zipfile.ZipFile(wheels[0]) as z:
        groups: dict[str, dict[str, int]] = {}
        for e in z.infolist():
            if e.filename.endswith(".dist-info/METADATA"):
                g = "METADATA (embeds README.md as the long description)"
            elif e.filename.startswith("qoregeo/_skill/"):
                g = "Agent Skill files"
            elif e.filename.startswith("qoregeo/") and e.filename.endswith(".py"):
                g = "Python modules"
            else:
                g = "other"
            d = groups.setdefault(g, {"compressed_bytes": 0, "unpacked_bytes": 0, "files": 0})
            d["compressed_bytes"] += e.compress_size
            d["unpacked_bytes"] += e.file_size
            d["files"] += 1
    info["composition"] = groups
    info["build_command"] = shlex.join(["python", "-m", "build", "--wheel", *isolation,
                                        "--outdir", "<tmp>", "<repo>"])
    info["build_isolation"] = not isolation
    return info


STACK = ["shapely", "pyproj", "numpy", "pandas", "pyogrio", "geopandas"]
PLATFORM_VARIANTS = {
    "manylinux2014": ["manylinux2014_x86_64"],
    "manylinux_2_28": ["manylinux_2_28_x86_64", "manylinux2014_x86_64"],
}


def _pip_download(pkgs: list[str], dest: Path, platforms: list[str], no_deps: bool, cwd: Path
                  ) -> tuple[bool, str]:
    cmd = [sys.executable, "-I", "-m", "pip", "download", "--disable-pip-version-check",
           "--only-binary", ":all:", "--python-version", "3.12", "--timeout", "30", "-d", str(dest)]
    for plat in platforms:
        cmd += ["--platform", plat]
    if no_deps:
        cmd.append("--no-deps")
    cmd += pkgs
    last = "timed out after 300 s"
    for _attempt in range(2):
        try:
            p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=300,
                               check=False)
        except subprocess.TimeoutExpired:
            continue
        if p.returncode == 0:
            return True, ""
        last = (p.stderr or p.stdout)[-400:]
    return False, last


def download_stack(tmp: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for variant, platforms in PLATFORM_VARIANTS.items():
        v: dict[str, Any] = {"platforms": platforms, "python_version": "3.12",
                             "packages": {}, "errors": {}}
        for pkg in STACK:
            dest = tmp / f"dl-{variant}-{pkg}"      # a new, empty folder per download
            dest.mkdir()
            ok, err = _pip_download([pkg], dest, platforms, True, tmp)
            files = sorted(dest.glob("*.whl"))
            if ok and files:
                v["packages"][pkg] = _wheel_info(files[0])
            else:
                v["errors"][pkg] = err or "no wheel downloaded"
        dest = tmp / f"dl-{variant}-geopandas-with-deps"
        dest.mkdir()
        ok, err = _pip_download(["geopandas"], dest, platforms, False, tmp)
        if ok:
            files = [_wheel_info(p) for p in sorted(dest.glob("*.whl"))]
            v["geopandas_with_dependencies"] = {
                "wheels": len(files),
                "bytes": sum(f["bytes"] for f in files),
                "unpacked_bytes": sum(f["unpacked_bytes"] for f in files),
                "files": {f["distribution"]: {"version": f["version"], "bytes": f["bytes"],
                                              "unpacked_bytes": f["unpacked_bytes"]} for f in files},
            }
        else:
            v["errors"]["geopandas with dependencies"] = err
        pk = v["packages"]
        if all(p in pk for p in STACK):
            v["six_wheels_total_bytes"] = sum(pk[p]["bytes"] for p in STACK)
            v["six_wheels_total_unpacked_bytes"] = sum(pk[p]["unpacked_bytes"] for p in STACK)
        out[variant] = v
    out["_method"] = ("pip download --no-deps --only-binary :all: --python-version 3.12 "
                      "--platform <tag> for each package, into a new empty folder each; the "
                      "'manylinux2014' variant is the platform tag the task names and picks the "
                      "newest release that still ships manylinux2014 wheels; the 'manylinux_2_28' "
                      "variant also allows glibc 2.28 wheels and so gets the newest releases. "
                      "geopandas_with_dependencies is what pip fetches for 'pip install geopandas'. "
                      "unpacked_bytes is the sum of file sizes inside the wheel (no .pyc).")
    return out


# Figures

def _style() -> None:
    plt.rcParams.update({
        "svg.hashsalt": "qoregeo-experiment-03",
        "svg.fonttype": "path",
        "font.size": 10,
        "axes.edgecolor": INK2,
        "axes.labelcolor": INK,
        "axes.linewidth": 0.6,
        "xtick.color": INK2,
        "ytick.color": INK2,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "legend.frameon": False,
    })


def _note(fig: plt.Figure) -> None:
    fig.text(0.995, 0.005, f"qoregeo {qoregeo.__version__}", ha="right", va="bottom",
             fontsize=7.5, color=INK2)


def _save(fig: plt.Figure, name: str) -> str:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    path = FIG_DIR / f"{NAME}-{name}.svg"
    fig.savefig(path, format="svg", facecolor="white", metadata={"Date": None}, dpi=100)
    plt.close(fig)
    return str(path.relative_to(REPO))


def _ms_formatter(v: float, _pos: Any) -> str:
    if v >= 1000:
        return f"{v / 1000:g} s"
    if v >= 1:
        return f"{v:g} ms"
    return f"{v * 1000:g} µs"


def fig_ops_vs_n(ops: dict[int, dict[str, Any]]) -> str:
    ns = sorted(ops)
    fig, axes = plt.subplots(2, 2, figsize=(8.0, 6.6), sharex=True, sharey=True)
    all_vals = []
    for _title, series, ctx in PANELS:
        for key, _label in series + ([ctx] if ctx else []):
            all_vals += [ops[n][key]["median_s"] * 1e3 for n in ns if key in ops[n]]
    lo, hi = min(all_vals), max(all_vals)
    for (title, series, ctx), ax in zip(PANELS, axes.flat, strict=True):
        handles = []
        if ctx is not None:
            key, label = ctx
            xs = [n for n in ns if key in ops[n]]
            ys = [ops[n][key]["median_s"] * 1e3 for n in xs]
            ax.plot(xs, ys, color=C_GREY_LIGHT, lw=2, solid_capstyle="round", zorder=1)
            ax.scatter(xs, ys, s=22, color=C_GREY_LIGHT, edgecolor="white", linewidth=1.2, zorder=2)
            handles.append(Line2D([], [], color=C_GREY_LIGHT, lw=2, marker="o", ms=4.5, label=label))
        for i, (key, label) in enumerate(series):
            xs = [n for n in ns if key in ops[n]]
            ys = [ops[n][key]["median_s"] * 1e3 for n in xs]
            ax.plot(xs, ys, color=SERIES[i], lw=2, solid_capstyle="round", zorder=3)
            ax.scatter(xs, ys, s=30, marker=MARKERS[i], color=SERIES[i], edgecolor="white",
                       linewidth=1.2, zorder=4)
            handles.insert(i, Line2D([], [], color=SERIES[i], lw=2, marker=MARKERS[i], ms=5,
                                     markeredgecolor="white", label=label))
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_title(title, loc="left", fontsize=10, color=INK, pad=4)
        ax.legend(handles=handles, loc="upper left", fontsize=7.6, handlelength=1.8,
                  borderaxespad=0.3, labelspacing=0.3)
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_ms_formatter))
        ax.set_xticks(ns)
        ax.set_xticklabels([f"{n:,}" for n in ns])
        ax.minorticks_off()
        ax.tick_params(length=0)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    axes[0, 0].set_ylim(lo / 2, hi * 30)
    for ax in axes[1]:
        ax.set_xlabel("dataset size N (features)")
    for ax in axes[:, 0]:
        ax.set_ylabel("time per call (median)")
    fig.tight_layout(rect=(0, 0.015, 1, 1))
    _note(fig)
    return _save(fig, "ops-vs-n")


def _dots(ax: plt.Axes, labels: list[str], values: list[float], colors: list[str],
          fmt: Callable[[float], str]) -> None:
    """Dot plot on a log axis: bars would need an arbitrary baseline there."""
    y = np.arange(len(labels))[::-1]
    ax.scatter(values, y, s=52, color=colors, edgecolor="white", linewidth=1.5, zorder=3)
    for yi, v in zip(y, values, strict=True):
        ax.text(v * 1.22, yi, fmt(v), va="center", ha="left", fontsize=8.2, color=INK)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=8.6)
    ax.set_ylim(-0.6, len(labels) - 0.4)
    ax.set_xscale("log")
    ax.tick_params(length=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)


def _barh(ax: plt.Axes, labels: list[str], values: list[float], colors: list[str],
          fmt: Callable[[float], str]) -> None:
    y = np.arange(len(labels))[::-1]
    ax.barh(y, values, height=0.56, color=colors, zorder=2)
    vmax = max(values)
    for yi, v in zip(y, values, strict=True):
        ax.text(v + vmax * 0.012, yi, fmt(v), va="center", ha="left", fontsize=8.2, color=INK)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=8.6)
    ax.tick_params(length=0)
    ax.grid(axis="y", visible=False)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)


def fig_footprint(wheel: dict[str, Any], stack: dict[str, Any] | None) -> str | None:
    if stack is None or "bytes" not in wheel:
        return None
    v = stack.get("manylinux2014", {})
    pk = v.get("packages", {})
    if not all(p in pk for p in STACK):
        return None
    labels = [f"qoregeo {wheel['version']}"]
    dl = [wheel["bytes"] / 1e6]
    up = [wheel["unpacked_bytes"] / 1e6]
    for p in sorted(STACK, key=lambda p: -pk[p]["bytes"]):
        labels.append(f"{p} {pk[p]['version']}")
        dl.append(pk[p]["bytes"] / 1e6)
        up.append(pk[p]["unpacked_bytes"] / 1e6)
    full = v.get("geopandas_with_dependencies")
    if full:
        labels.append(f"pip install geopandas\n(all {full['wheels']} wheels)")
        dl.append(full["bytes"] / 1e6)
        up.append(full["unpacked_bytes"] / 1e6)
    colors = [C_QG] + [C_GREY] * (len(labels) - 1)
    if full:
        colors[-1] = INK2
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(8.0, 4.4), sharey=True)

    def mb(x: float) -> str:
        return f"{x * 1000:.0f} KB" if x < 1 else f"{x:.1f} MB"

    _barh(a1, labels, dl, colors, mb)
    _barh(a2, labels, up, colors, mb)
    a1.set_xlabel("wheel download size (MB)")
    a2.set_xlabel("unpacked size (MB, files in the wheel)")
    a1.set_xlim(0, max(dl) * 1.25)
    a2.set_xlim(0, max(up) * 1.25)
    fig.text(0.01, 0.01, "manylinux2014 x86_64 wheels for CPython 3.12, as pip downloads them",
             fontsize=7.5, color=INK2, ha="left", va="bottom")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    _note(fig)
    return _save(fig, "footprint")


def fig_imports(imports: dict[str, Any]) -> str | None:
    rows = [(k, v) for k, v in imports.items() if not k.startswith("_")]
    if not rows:
        return None
    labels = [k for k, _ in rows]
    vals = [v["importtime_cumulative_ms"]["median"] for _, v in rows]
    colors = [C_QG if k.startswith(("import qoregeo", "from qoregeo")) else C_GREY for k in labels]
    fig, ax = plt.subplots(figsize=(8.0, 3.4))
    _barh(ax, labels, vals, colors, lambda x: f"{x:.0f} ms")
    ax.set_xlim(0, max(vals) * 1.18)
    ax.set_xlabel("cold import time, median of fresh processes (ms, python -X importtime)")
    fig.tight_layout(rect=(0, 0.02, 1, 1))
    _note(fig)
    return _save(fig, "import-time")


def fig_micro(micro: dict[str, Any]) -> str:
    def collect(section: dict[str, Any], vec_key: str, wanted: list[tuple[str, str]]
                ) -> tuple[list[str], list[float], list[str]]:
        labels, vals, cols = [], [], []
        for key, label in wanted:
            src = section[vec_key] if key.startswith("vec:") else section
            k = key[4:] if key.startswith("vec:") else key
            if k not in src:
                continue
            labels.append(label)
            vals.append(src[k]["calls_per_s_median"])
            cols.append(C_VEC if key.startswith("vec:") else (C_QG if label.startswith("qoregeo") else C_GREY))
        return labels, vals, cols

    d = micro["distance"]
    big = max(int(k.rsplit("=", 1)[1]) for k in d["_vectorized_per_pair"])
    dist_rows = [
        ("qoregeo utils._haversine_km (internal, no validation)", "qoregeo _haversine_km (internal)"),
        ("qoregeo distance() haversine", "qoregeo distance() haversine"),
        ("haversine package haversine()", "haversine package"),
        ("geopy great_circle().km", "geopy great_circle"),
        ("qoregeo utils._vincenty_km (internal, no validation)", "qoregeo _vincenty_km (internal)"),
        ("qoregeo distance() vincenty", "qoregeo distance() vincenty"),
        ("geographiclib Geodesic.WGS84.Inverse (distance only)", "geographiclib Inverse"),
        ("geopy geodesic().km", "geopy geodesic"),
        (f"vec:numpy haversine, n={big}", f"numpy haversine, n={big:,}"),
        (f"vec:haversine package haversine_vector, n={big}", f"haversine_vector, n={big:,}"),
    ]
    p = micro["point_in_polygon"]
    pip_rows = [
        ("qoregeo prepared tester (utils._polygon_tester, used by filter_by_polygon)",
         "qoregeo prepared tester (internal)"),
        ("qoregeo point_in_polygon() (validates the polygon on every call)",
         "qoregeo point_in_polygon()"),
        ("shapely Point(x, y).within(polygon)", "shapely Point.within"),
        ("shapely prepared.prep(polygon).contains(Point(x, y))", "shapely prepared contains"),
        ("shapely.contains_xy(prepared polygon, x, y), scalar", "shapely.contains_xy, scalar"),
        (f"vec:shapely.contains_xy vectorized, n={big}", f"shapely.contains_xy, n={big:,}"),
    ]
    l1, v1, c1 = collect(d, "_vectorized_per_pair", dist_rows)
    l2, v2, c2 = collect(p, "_vectorized_per_point", pip_rows)
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(8.0, 7.2),
                                 gridspec_kw={"height_ratios": [len(l1), len(l2)]})

    def rate(x: float) -> str:
        if x >= 1e6:
            return f"{x / 1e6:.1f} M/s"
        return f"{x / 1e3:.0f} k/s"

    _dots(a1, l1, v1, c1, rate)
    _dots(a2, l2, v2, c2, rate)
    lo = min(v1 + v2) / 2
    hi = max(v1 + v2) * 8
    for ax in (a1, a2):
        ax.set_xlim(lo, hi)
    a1.set_title("distance between two points", loc="left", fontsize=10, color=INK, pad=4)
    a2.set_title(f"point in polygon, {p['_agreement']['polygon_vertices']} vertices",
                 loc="left", fontsize=10, color=INK, pad=4)
    a2.set_xlabel("calls per second, median (log scale; vectorized rows: points per second)")
    def key(color: str, label: str) -> Line2D:
        return Line2D([], [], ls="none", marker="o", ms=7, color=color, markeredgecolor="white",
                      label=label)

    fig.legend(handles=[key(C_QG, "qoregeo, one call per point"),
                        key(C_GREY, "other library, one call per point"),
                        key(C_VEC, "numpy-vectorized, arrays ready")],
               loc="upper center", ncol=3, fontsize=8, bbox_to_anchor=(0.5, 0.995))
    fig.tight_layout(rect=(0, 0.015, 1, 0.955))
    _note(fig)
    return _save(fig, "micro")


# Summary and documentation claims

def summarize(results: dict[str, Any]) -> dict[str, Any]:
    ops = results["operations"]
    ns = sorted(int(n) for n in ops)
    nmax = ns[-1]
    om = ops[nmax]
    s: dict[str, Any] = {"n_max": nmax}
    s["median_ms_at_n_max"] = {k: v["median_s"] * 1e3 for k, v in om.items() if not k.startswith("_")}
    s["loglog_slope_1k_to_n_max"] = {
        k: _loglog_slope([n for n in ns if k in ops[n]], [ops[n][k]["median_s"] for n in ns if k in ops[n]])
        for k in om if not k.startswith("_")}
    gc_off = results.get("operations_gc_disabled")
    if gc_off and nmax in gc_off:
        s["gc_share_at_n_max"] = {
            k: 1.0 - gc_off[nmax][k]["median_s"] / om[k]["median_s"]
            for k in om if not k.startswith("_") and k in gc_off[nmax] and om[k]["median_s"] > 0}
    s["ratios_at_n_max"] = {
        "filter_by_polygon_64 / shapely.contains_xy (arrays ready)":
            om["filter_by_polygon_64"]["median_s"] / om["baseline_shapely_contains_xy"]["median_s"],
        "filter_by_radius / numpy haversine mask (arrays ready)":
            om["filter_by_radius"]["median_s"] / om["baseline_numpy_radius_mask"]["median_s"],
        "building numpy arrays from qoregeo features / filter_by_radius":
            om["baseline_build_numpy_arrays"]["median_s"] / om["filter_by_radius"]["median_s"],
        "load_csv / plain-Python CSV reader":
            om["load_csv"]["median_s"] / om["baseline_csv_feature_dicts"]["median_s"],
        "load_geojson / json.load":
            om["load_geojson"]["median_s"] / om["baseline_json_load"]["median_s"],
    }
    d = results["micro"]["distance"]
    p = results["micro"]["point_in_polygon"]

    def us(sec: dict[str, Any], k: str) -> float:
        return float(sec[k]["median_us"])

    s["micro_ratios"] = {
        "qoregeo distance() haversine / haversine package":
            us(d, "qoregeo distance() haversine") / us(d, "haversine package haversine()"),
        "qoregeo _haversine_km / haversine package":
            us(d, "qoregeo utils._haversine_km (internal, no validation)") / us(d, "haversine package haversine()"),
        "geopy great_circle / qoregeo distance() haversine":
            us(d, "geopy great_circle().km") / us(d, "qoregeo distance() haversine"),
        "qoregeo distance() vincenty / geographiclib Inverse":
            us(d, "qoregeo distance() vincenty") / us(d, "geographiclib Geodesic.WGS84.Inverse (distance only)"),
        "geopy geodesic / qoregeo distance() vincenty":
            us(d, "geopy geodesic().km") / us(d, "qoregeo distance() vincenty"),
        "qoregeo prepared tester / shapely Point.within":
            us(p, "qoregeo prepared tester (utils._polygon_tester, used by filter_by_polygon)")
            / us(p, "shapely Point(x, y).within(polygon)"),
        "qoregeo point_in_polygon() / shapely Point.within":
            us(p, "qoregeo point_in_polygon() (validates the polygon on every call)")
            / us(p, "shapely Point(x, y).within(polygon)"),
    }
    vp = p["_vectorized_per_point"]
    vk = max(vp, key=lambda k: int(k.rsplit("=", 1)[1]))
    s["micro_ratios"][f"qoregeo prepared tester / {vk}"] = (
        us(p, "qoregeo prepared tester (utils._polygon_tester, used by filter_by_polygon)") / us(vp, vk))
    return s


def doc_claims(results: dict[str, Any]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    wheel = results["footprint"].get("qoregeo_wheel", {})
    if "bytes" in wheel:
        kb = wheel["bytes"] / 1000
        claims.append({"where": "README.md, 'Why QOREgeo?'",
                       "claim": "a ~160 KB pure-Python wheel",
                       "measured": f"{kb:.1f} KB ({wheel['bytes']} bytes), "
                                   f"{wheel['unpacked_bytes'] / 1000:.0f} KB unpacked",
                       "consistent": 140 <= kb <= 180})
    stack = results["footprint"].get("stack_wheels")
    if stack:
        parts = []
        ok = []
        for variant in PLATFORM_VARIANTS:
            v = stack.get(variant, {})
            if "six_wheels_total_bytes" in v:
                mb = v["six_wheels_total_bytes"] / 1e6
                parts.append(f"{variant}: {mb:.1f} MB for the six wheels")
                ok.append(65 <= mb <= 80)
            if "geopandas_with_dependencies" in v:
                full = v["geopandas_with_dependencies"]
                parts.append(f"{variant}: {full['bytes'] / 1e6:.1f} MB for all {full['wheels']} "
                             "wheels of 'pip install geopandas'")
        if parts:
            claims.append({"where": "README.md, 'Why QOREgeo?'",
                           "claim": "the GeoPandas core stack is about 74 MB of wheels",
                           "measured": "; ".join(parts), "consistent": all(ok) if ok else None})
        latest = stack.get("manylinux_2_28", {}).get("packages", {})
        rp = {p: (latest[p]["version"], latest[p].get("requires_python")) for p in ("shapely", "pyproj")
              if p in latest}
        if rp:
            expected = {"shapely": ("2.2", ">=3.11"), "pyproj": ("3.8", ">=3.12")}
            verdicts = [r == expected[p][1] for p, (v, r) in rp.items() if v.startswith(expected[p][0])]
            claims.append({"where": "README.md, 'Why QOREgeo?'",
                           "claim": "shapely 2.2 needs 3.11+, pyproj 3.8 needs 3.12+",
                           "measured": {p: {"version": v, "requires_python": r} for p, (v, r) in rp.items()},
                           "consistent": all(verdicts) if len(verdicts) == len(rp) else None,
                           "note": "Requires-Python read from the METADATA of the newest wheels pip found"})
    return claims


# Main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--quick", action="store_true", help="reduced sizes, runs in about 15 seconds")
    ap.add_argument("--seed", type=int, default=20261019, help="random seed (default 20261019)")
    ap.add_argument("--no-network", action="store_true",
                    help="skip downloading the reference wheels (the only network step)")
    ap.add_argument("--network", action="store_true",
                    help="download the reference wheels even with --quick")
    ap.add_argument("--tmpdir", default=None, help="parent folder for the temporary data")
    args = ap.parse_args(argv)
    mode = "quick" if args.quick else "full"
    sz = SIZES[mode]
    use_network = (not args.no_network) and (mode == "full" or args.network)
    t_start = time.perf_counter()
    _style()
    results: dict[str, Any] = {}

    with tempfile.TemporaryDirectory(prefix="qoregeo-perf-", dir=args.tmpdir) as tmp_s:
        tmp = Path(tmp_s)
        data_dir = tmp / "data"
        out_dir = tmp / "out"
        data_dir.mkdir()
        out_dir.mkdir()
        nmax = max(sz["n_values"] + [sz["memory_n"]])
        print(f"[{mode}] generating {nmax:,} rows", flush=True)
        rows = make_rows(args.seed, nmax)
        datasets = {n: write_dataset(rows, data_dir, n) for n in sorted(set(sz["n_values"] + [sz["memory_n"]]))}
        del rows
        results["datasets"] = {str(n): {k: v for k, v in d.items() if k not in ("csv", "geojson")}
                               for n, d in datasets.items()}
        polygon64 = star_polygon(args.seed, sz["polygon_vertices"])

        results["operations"] = {}
        results["operations_gc_disabled"] = {}
        for n in sz["n_values"]:
            print(f"[{mode}] dataset operations, N={n:,}", flush=True)
            on, off = run_operations(datasets[n], out_dir, sz, polygon64)
            results["operations"][n] = on
            if off:
                results["operations_gc_disabled"][n] = off

        print(f"[{mode}] micro-benchmarks", flush=True)
        results["micro"] = run_micro(args.seed, sz, polygon64)

        footprint: dict[str, Any] = {}
        print(f"[{mode}] import times ({sz['import_runs']} runs each)", flush=True)
        footprint["import_time"] = run_import_times(sz["import_runs"], args.seed, str(tmp))
        print(f"[{mode}] memory while loading N={sz['memory_n']:,}", flush=True)
        footprint["memory_load"] = run_memory(datasets[sz["memory_n"]], str(tmp))
        print(f"[{mode}] building the qoregeo wheel", flush=True)
        footprint["qoregeo_wheel"] = build_qoregeo_wheel(tmp)
        if use_network:
            print(f"[{mode}] downloading reference wheels (network)", flush=True)
            footprint["stack_wheels"] = download_stack(tmp)
        else:
            footprint["stack_wheels"] = None
            footprint["stack_wheels_skipped"] = "--quick or --no-network"
        results["footprint"] = footprint

    results["operations"] = {int(k): v for k, v in results["operations"].items()}
    results["summary"] = summarize(results)
    results["doc_claims"] = doc_claims(results)

    print(f"[{mode}] figures", flush=True)
    figures = [fig_ops_vs_n(results["operations"]), fig_micro(results["micro"])]
    for f in (fig_footprint(results["footprint"]["qoregeo_wheel"], results["footprint"]["stack_wheels"]),
              fig_imports(results["footprint"]["import_time"])):
        if f:
            figures.append(f)

    import geopy
    import pyproj
    import shapely

    meta = {
        "experiment": NAME,
        "mode": mode,
        "qoregeo_version": qoregeo.__version__,
        "python_version": sys.version,
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "reference_libraries": {
            "shapely": shapely.__version__,
            "geos": shapely.geos_version_string,
            "pyproj": pyproj.__version__,
            "geopy": geopy.__version__,
            "geographiclib": _version("geographiclib"),
            "haversine": _version("haversine"),
            "numpy": np.__version__,
            "matplotlib": matplotlib.__version__,
            "build": _version("build"),
            "pip": _version("pip"),
        },
        "seed": args.seed,
        "sizes": sz,
        "dataset": {
            "columns": ["name", "category", "revenue", "latitude", "longitude"],
            "bbox_west_south_east_north": INDIA_BBOX,
            "points": f"75 % Gaussian clusters (sigma {CLUSTER_SIGMA_KM:g} km) around 10 cities, "
                      "25 % uniform in the bbox",
            "categories": dict(zip(CATEGORIES, CATEGORY_WEIGHTS, strict=True)),
            "revenue": f"lognormal(mu={REVENUE_MU}, sigma={REVENUE_SIGMA}), 2 decimals",
            "smaller datasets are prefixes of the largest": True,
        },
        "queries": {
            "filter_eq": "filter('category', 'cafe')",
            "filter_ge": f"filter('revenue', {REVENUE_THRESHOLD}, op='>=') on CSV strings",
            "filter_ge_numeric_properties": "the same on the GeoJSON-loaded engine (numbers)",
            "where": f"where(lambda p: float(p['revenue']) >= {REVENUE_THRESHOLD})",
            "filter_by_radius": f"filter_by_radius({RADIUS_CENTER[0]}, {RADIUS_CENTER[1]}, radius={RADIUS_KM})",
            "filter_by_bbox": f"filter_by_bbox(**{BBOX_QUERY})",
            "filter_by_polygon_buffer": f"filter_by_polygon(buffer({BUFFER_CENTER}, {BUFFER_KM})): "
                                        "tested as an exact circle",
            "filter_by_polygon_buffer_as_plain_polygon": "the same 64-gon ring as a plain Polygon "
                                                         "(ray casting)",
            "filter_by_polygon_64": f"a seeded non-convex {sz['polygon_vertices']}-vertex polygon",
            "nearest": f"nearest({NEAREST_POINT})",
            "k_nearest": f"k_nearest({NEAREST_POINT}, k={K})",
            "sort_by_head": "sort_by('revenue', descending=True).head(10)",
            "map_html": "map_html() with default options",
            "image_png / image_svg": "image('out.png' / 'out.svg') with default options, 1200x800",
            "operations run on": "the engine loaded from CSV with default options (string values); "
                                 "the CSV- and GeoJSON-loaded engines are both in memory while the "
                                 "non-load operations run, the three loads run with neither",
        },
        "utc_timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "command": shlex.join(["python", *sys.argv]),
        "python_executable": sys.executable,
        "network_used": use_network,
        "runtime_seconds": None,
        "figures": figures,
        "notes": [
            "Timing numbers are indicative: this run shared a 4-CPU machine with other jobs.",
            "Dataset operations: median and min of repeats, each repeat a calibrated loop after "
            "a gc.collect(). 'operations' has the garbage collector on; "
            "'operations_gc_disabled' comes from loops with it off, alternating with the on loops.",
            "Per-call micro-benchmarks run with the garbage collector off, like timeit.",
            "The Python under test differs from the cp312 wheels used for the footprint "
            "comparison; wheel sizes do not depend on the interpreter running this script.",
        ],
    }
    meta["runtime_seconds"] = round(time.perf_counter() - t_start, 1)
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "results": results}
    RESULTS_PATH.write_text(json.dumps(_f(payload), indent=1, allow_nan=False) + "\n", encoding="utf-8")
    print(f"wrote {RESULTS_PATH.relative_to(REPO)} and {len(figures)} figures "
          f"in {meta['runtime_seconds']} s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
