"""Experiment 01: geodesic distance accuracy of qoregeo.

Reference: geographiclib ``Geodesic.WGS84`` (Karney 2013), which solves the
inverse and direct geodesic problems on the WGS-84 ellipsoid to about 15 nm.

What is measured:

1. ``GeoEngine.distance(a, b, unit="m")`` with ``method="haversine"`` and
   ``method="vincenty"`` on seeded random pairs uniform on the sphere.
2. The same on stratified distance classes (<1 km up to >10,000 km).
3. Near-antipodal pairs, where Vincenty's inverse iteration may not converge.
   Every call is classified as converged, fallback (warning), exception or NaN.
4. Pairs that touch a pole or cross the antimeridian.
5. ``bearing(as_degrees=True)`` against the geodesic initial azimuth.
6. ``destination()`` against geographiclib ``Direct`` and a public-API round trip.
7. ``length()`` of random polylines against the summed geodesic lengths.
8. geopy (``great_circle``, ``geodesic``), the ``haversine`` package and pyproj
   for context, with the Earth model each one uses.

Run from the repository root::

    python experiments/01_distance_accuracy.py            # full run (about 1 to 3 minutes)
    python experiments/01_distance_accuracy.py --quick    # reduced sizes, a few seconds

Outputs: ``experiments/results/distance-accuracy.json`` and
``docs/assets/experiments/distance-accuracy-*.svg``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import gc
import importlib.metadata as _md
import json
import math
import os
import platform
import random
import shlex
import statistics
import sys
import time
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from geographiclib.geodesic import Geodesic  # noqa: E402
from matplotlib.colors import LogNorm  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

import qoregeo  # noqa: E402
from qoregeo import GeoEngine  # noqa: E402
from qoregeo import utils as qutils  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
RESULTS_PATH = REPO / "experiments" / "results" / "distance-accuracy.json"
FIG_DIR = REPO / "docs" / "assets" / "experiments"
NAME = "distance-accuracy"

W = Geodesic.WGS84
INV_MASK = Geodesic.DISTANCE | Geodesic.AZIMUTH

# Distance classes in metres, [low, high).
CLASSES: list[tuple[str, float, float]] = [
    ("<1 km", 1.0, 1e3),
    ("1-100 km", 1e3, 1e5),
    ("100-1,000 km", 1e5, 1e6),
    ("1,000-10,000 km", 1e6, 1e7),
    (">10,000 km", 1e7, 2.0e7),
]

# Segment-length classes for the polyline experiment, metres.
LENGTH_CLASSES: list[tuple[str, float, float]] = [
    ("segments 1-100 m", 1.0, 100.0),
    ("segments 100 m-10 km", 100.0, 1e4),
    ("segments 10-500 km", 1e4, 5e5),
    ("segments 500-5,000 km", 5e5, 5e6),
]

SIZES = {
    "full": {
        "uniform_pairs": 100_000,
        "per_class": 20_000,
        "antipodal_random": 20_000,
        "antipodal_grid": 101,
        "pole_random": 2_000,
        "antimeridian_random": 5_000,
        "destination_per_class": 5_000,
        "roundtrip_per_class": 5_000,
        "polylines_per_class": 1_000,
        "polyline_max_vertices": 40,
        "context_pairs": 100_000,
        "timing_pairs": 2_000,
        "timing_repeats": 7,
    },
    "quick": {
        "uniform_pairs": 3_000,
        "per_class": 400,
        "antipodal_random": 400,
        "antipodal_grid": 21,
        "pole_random": 200,
        "antimeridian_random": 300,
        "destination_per_class": 200,
        "roundtrip_per_class": 200,
        "polylines_per_class": 40,
        "polyline_max_vertices": 20,
        "context_pairs": 1_000,
        "timing_pairs": 200,
        "timing_repeats": 3,
    },
}

# Chart colours: categorical slots 1 and 2 of the dataviz reference palette.
C_HAV = "#2a78d6"
C_VIN = "#eb6834"
C_GREY = "#8a8985"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e4e3df"


# Helpers

def _rng(seed: int, name: str) -> random.Random:
    """An independent, reproducible stream per experiment section."""
    return random.Random(f"{seed}:{name}")


def _uniform_point(rng: random.Random) -> tuple[float, float]:
    """A point uniform on the sphere, as (lat, lng) in degrees."""
    lat = math.degrees(math.asin(2.0 * rng.random() - 1.0))
    lng = rng.uniform(-180.0, 180.0)
    return lat, lng


def _log_uniform(rng: random.Random, lo: float, hi: float) -> float:
    return 10.0 ** rng.uniform(math.log10(lo), math.log10(hi))


def _wrap_lng(lng: float) -> float:
    w = ((lng + 180.0) % 360.0) - 180.0
    return 180.0 if w == -180.0 and lng > 0 else w


def _angle_diff(a: float, b: float) -> float:
    """Signed difference a - b wrapped to [-180, 180)."""
    return (a - b + 180.0) % 360.0 - 180.0


def _f(x: Any) -> Any:
    """Make a value JSON-safe (numpy scalars to float, NaN and inf to None)."""
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


def _pct(values: np.ndarray) -> dict[str, float | None]:
    if values.size == 0:
        return {"p50": None, "p95": None, "p99": None, "max": None, "mean": None}
    p50, p95, p99 = np.percentile(values, [50, 95, 99])
    return {"p50": p50, "p95": p95, "p99": p99, "max": values.max(), "mean": values.mean()}


def err_stats(est: np.ndarray, ref: np.ndarray) -> dict[str, Any]:
    """Absolute (m) and relative (%) error percentiles of est against ref."""
    est = np.asarray(est, dtype=float)
    ref = np.asarray(ref, dtype=float)
    ok = np.isfinite(est)
    signed = est[ok] - ref[ok]
    pos = ref[ok] > 0
    rel = 100.0 * signed[pos] / ref[ok][pos]
    out: dict[str, Any] = {
        "n": int(est.size),
        "n_finite": int(ok.sum()),
        "abs_m": _pct(np.abs(signed)),
        "rel_pct": _pct(np.abs(rel)),
    }
    if signed.size:
        out["signed_m"] = {"min": signed.min(), "max": signed.max(), "mean": signed.mean()}
    if rel.size:
        out["signed_rel_pct"] = {"min": rel.min(), "max": rel.max(), "mean": rel.mean()}
    return out


def angle_stats(est: np.ndarray, ref: np.ndarray) -> dict[str, Any]:
    d = np.array([_angle_diff(a, b) for a, b in zip(est, ref, strict=True)], dtype=float)
    d = d[np.isfinite(d)]
    return {"n": int(d.size), "abs_deg": _pct(np.abs(d))}


class WarnCounter:
    """Records every warning so each call can be classified as warned or not."""

    def __enter__(self) -> WarnCounter:
        self._cm = warnings.catch_warnings(record=True)
        self.log = self._cm.__enter__()
        warnings.simplefilter("always")
        return self

    def __exit__(self, *exc: Any) -> None:
        self._cm.__exit__(*exc)

    def mark(self) -> int:
        if len(self.log) > 10_000:
            self.log.clear()
        return len(self.log)

    def converge_warnings_since(self, mark: int) -> int:
        return sum("converge" in str(w.message) for w in self.log[mark:])


# Core measurement over a list of pairs

def measure_pairs(geo: GeoEngine, pairs: list[tuple[tuple[float, float], tuple[float, float]]]
                  ) -> dict[str, np.ndarray]:
    """Run qoregeo and the reference on every pair, recording outcomes per call."""
    n = len(pairs)
    out = {k: np.full(n, np.nan) for k in
           ("ref", "ref_az", "hav", "vin", "hav_raw", "vin_raw", "bearing")}
    warned = np.zeros(n, dtype=bool)
    exc_types: dict[str, int] = {}
    with WarnCounter() as wc:
        for i, (a, b) in enumerate(pairs):
            r = W.Inverse(a[0], a[1], b[0], b[1], INV_MASK)
            out["ref"][i] = r["s12"]
            out["ref_az"][i] = r["azi1"] % 360.0
            out["hav"][i] = geo.distance(a, b, unit="m", method="haversine")
            m = wc.mark()
            try:
                out["vin"][i] = geo.distance(a, b, unit="m", method="vincenty")
            except Exception as e:  # noqa: BLE001  (recorded, not hidden)
                exc_types[type(e).__name__] = exc_types.get(type(e).__name__, 0) + 1
            warned[i] = wc.converge_warnings_since(m) > 0
            out["hav_raw"][i] = qutils._haversine_km(a, b) * 1000.0
            out["vin_raw"][i] = qutils._vincenty_km(a, b) * 1000.0
            out["bearing"][i] = geo.bearing(a, b, as_degrees=True)
    out["warned"] = warned
    out["lat_a"] = np.array([p[0][0] for p in pairs])
    out["exceptions"] = exc_types  # type: ignore[assignment]
    return out


def summarize(meas: dict[str, Any], with_bearing: bool = True) -> dict[str, Any]:
    ref = meas["ref"]
    warned = meas["warned"]
    vin = meas["vin"]
    summary: dict[str, Any] = {
        "n": int(ref.size),
        "ref_distance_m": {"min": ref.min(), "max": ref.max(), "median": float(np.median(ref))},
        "haversine": err_stats(meas["hav"], ref),
        "vincenty": err_stats(vin, ref),
        "vincenty_converged_only": err_stats(vin[~warned], ref[~warned]),
        "vincenty_unrounded": err_stats(meas["vin_raw"], ref),
        "vincenty_unrounded_converged_only": err_stats(meas["vin_raw"][~warned], ref[~warned]),
        "haversine_unrounded": err_stats(meas["hav_raw"], ref),
        "vincenty_outcomes": {
            "converged": int((~warned & np.isfinite(vin)).sum()),
            "fallback_with_warning": int(warned.sum()),
            "fallback_equals_haversine": int((warned & (vin == meas["hav"])).sum()),
            "nan": int(np.isnan(vin).sum() - sum(meas["exceptions"].values())),
            "exceptions": meas["exceptions"],
        },
    }
    if with_bearing:
        at_pole = np.abs(meas["lat_a"]) == 90.0
        keep = ~at_pole & (ref > 0)
        summary["bearing_vs_geodesic_azimuth"] = angle_stats(meas["bearing"][keep],
                                                             meas["ref_az"][keep])
        if at_pole.any():
            # From an exact pole both libraries measure the azimuth against the
            # meridian of the pole's own longitude value.
            pk = at_pole & (ref > 0)
            summary["bearing_from_exact_pole"] = angle_stats(meas["bearing"][pk], meas["ref_az"][pk])
    return summary


# Sections

def haversine_theory() -> dict[str, Any]:
    """Local relative error of a sphere of radius R against WGS-84 for short paths.

    A short north-south step is scaled by R / M(lat) and an east-west step by
    R / N(lat), where M and N are the meridian and prime vertical radii of
    curvature. Their extremes bound the haversine error for short distances.
    """
    a, f = qutils.WGS84_A, qutils.WGS84_F
    e2 = f * (2 - f)
    r = qutils.EARTH_RADIUS_M

    def m_rad(lat: float) -> float:
        return a * (1 - e2) / (1 - e2 * math.sin(math.radians(lat)) ** 2) ** 1.5

    def n_rad(lat: float) -> float:
        return a / math.sqrt(1 - e2 * math.sin(math.radians(lat)) ** 2)

    return {
        "north_south_at_equator_pct": (r / m_rad(0.0) - 1) * 100,
        "east_west_at_equator_pct": (r / n_rad(0.0) - 1) * 100,
        "any_direction_at_pole_pct": (r / m_rad(90.0) - 1) * 100,
        "zero_error_lat_north_south_deg": _bisect_zero(lambda x: r / m_rad(x) - 1, 0.0, 90.0),
        "note": "short-path bounds: the sphere overestimates north-south steps near the equator "
                "and underestimates every step near the poles",
    }


def _bisect_zero(fn: Callable[[float], float], lo: float, hi: float) -> float:
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if (fn(lo) > 0) == (fn(mid) > 0):
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def run_uniform(geo: GeoEngine, seed: int, n: int) -> tuple[dict[str, Any], dict[str, Any], list]:
    rng = _rng(seed, "uniform")
    pairs = [(_uniform_point(rng), _uniform_point(rng)) for _ in range(n)]
    meas = measure_pairs(geo, pairs)
    return summarize(meas), meas, pairs


def run_classes(geo: GeoEngine, seed: int, n: int) -> tuple[dict[str, Any], dict[str, Any], list]:
    """Stratified pairs: random start, random azimuth, log-uniform length per class.

    Pairs are generated with geographiclib Direct and grouped by the class they
    were drawn for. The reference is the Inverse (shortest) distance, which
    differs from the drawn length only for geodesics near 20,000 km.
    """
    rng = _rng(seed, "classes")
    pairs = []
    cls = []
    for ci, (_, lo, hi) in enumerate(CLASSES):
        for _ in range(n):
            a = _uniform_point(rng)
            d = W.Direct(a[0], a[1], rng.uniform(0.0, 360.0), _log_uniform(rng, lo, hi))
            pairs.append((a, (d["lat2"], _wrap_lng(d["lon2"]))))
            cls.append(ci)
    meas = measure_pairs(geo, pairs)
    meas["cls"] = np.array(cls)
    out: dict[str, Any] = {}
    for ci, (name, _lo, _hi) in enumerate(CLASSES):
        sel = meas["cls"] == ci
        sub = {k: (v[sel] if isinstance(v, np.ndarray) else v) for k, v in meas.items()}
        sub["exceptions"] = {}
        out[name] = summarize(sub)
    out["_all_exceptions"] = meas["exceptions"]
    return out, meas, pairs


def _vincenty_outcome(geo: GeoEngine, wc: WarnCounter, a: tuple[float, float],
                      b: tuple[float, float]) -> tuple[float, str]:
    m = wc.mark()
    try:
        v = geo.distance(a, b, unit="m", method="vincenty")
    except Exception as e:  # noqa: BLE001
        return math.nan, "exception:" + type(e).__name__
    if wc.converge_warnings_since(m):
        return v, "fallback"
    if math.isnan(v):
        return v, "nan"
    if math.isinf(v):
        return v, "inf"
    return v, "converged"


def run_antipodal(geo: GeoEngine, seed: int, n: int, grid_n: int) -> tuple[dict[str, Any], dict]:
    rng = _rng(seed, "antipodal")
    offs_bins = [(1.0, 10.0), (10.0, 100.0), (100.0, 1e3), (1e3, 1e4), (1e4, 1e5), (1e5, 3e5)]
    lat_bins = [(0, 15), (15, 30), (30, 45), (45, 60), (60, 75), (75, 90.0001)]
    rows = []
    with WarnCounter() as wc:
        for _ in range(n):
            a = _uniform_point(rng)
            anti = (-a[0], _wrap_lng(a[1] + 180.0))
            off = _log_uniform(rng, 1.0, 3e5)
            d = W.Direct(anti[0], anti[1], rng.uniform(0.0, 360.0), off)
            b = (d["lat2"], _wrap_lng(d["lon2"]))
            ref = W.Inverse(a[0], a[1], b[0], b[1])["s12"]
            hav = geo.distance(a, b, unit="m", method="haversine")
            t0 = time.perf_counter()
            vin, outcome = _vincenty_outcome(geo, wc, a, b)
            dt = time.perf_counter() - t0
            rows.append((a, b, off, ref, hav, vin, outcome, dt))

    outcomes: dict[str, int] = {}
    for r in rows:
        outcomes[r[6]] = outcomes.get(r[6], 0) + 1
    ref = np.array([r[3] for r in rows])
    hav = np.array([r[4] for r in rows])
    vin = np.array([r[5] for r in rows])
    conv = np.array([r[6] == "converged" for r in rows])
    fb = np.array([r[6] == "fallback" for r in rows])
    offs = np.array([r[2] for r in rows])
    alat = np.array([abs(r[0][0]) for r in rows])
    times = np.array([r[7] for r in rows]) * 1e6

    err_conv = np.abs(vin[conv] - ref[conv])
    worst_i = None
    if conv.any():
        idx = np.flatnonzero(conv)
        worst_i = int(idx[np.argmax(err_conv)])
    by_offset = []
    for lo, hi in offs_bins:
        s = (offs >= lo) & (offs < hi)
        by_offset.append({"offset_from_antipode_m": [lo, hi], "n": int(s.sum()),
                          "fallback": int((fb & s).sum()),
                          "fallback_rate": float((fb & s).sum() / max(1, s.sum()))})
    by_lat = []
    for lo, hi in lat_bins:
        s = (alat >= lo) & (alat < hi)
        by_lat.append({"abs_lat_deg": [lo, min(hi, 90)], "n": int(s.sum()),
                       "fallback": int((fb & s).sum()),
                       "fallback_rate": float((fb & s).sum() / max(1, s.sum()))})

    random_summary: dict[str, Any] = {
        "n": len(rows),
        "construction": ("A uniform on the sphere; B = geographiclib Direct from the antipode of A "
                         "on a random azimuth, offset log-uniform in [1 m, 300 km]"),
        "outcomes": outcomes,
        "fallback_equals_haversine": int((fb & (vin == hav)).sum()),
        "vincenty_converged": err_stats(vin[conv], ref[conv]),
        "vincenty_fallback": err_stats(vin[fb], ref[fb]),
        "haversine_all": err_stats(hav, ref),
        "converged_with_error_over_1mm": int((err_conv > 1e-3).sum()),
        "worst_converged": None if worst_i is None else {
            "a": rows[worst_i][0], "b": rows[worst_i][1], "ref_m": rows[worst_i][3],
            "vincenty_m": rows[worst_i][5]},
        "fallback_rate_by_offset": by_offset,
        "fallback_rate_by_abs_lat_of_a": by_lat,
        "time_per_call_us": {
            "converged_median": float(np.median(times[conv])) if conv.any() else None,
            "fallback_median": float(np.median(times[fb])) if fb.any() else None,
            "note": "single-shot wall time per call, indicative only",
        },
    }

    # Equator: bisect the longitude gap at which the iteration starts to converge.
    with WarnCounter() as wc:
        def falls_back(x: float) -> bool:
            return _vincenty_outcome(geo, wc, (0.0, 0.0), (0.0, 180.0 - x))[1] == "fallback"
        lo_x, hi_x = 0.0, 1.0
        assert falls_back(lo_x) and not falls_back(hi_x)
        for _ in range(60):
            mid = 0.5 * (lo_x + hi_x)
            if falls_back(mid):
                lo_x = mid
            else:
                hi_x = mid
    f = qutils.WGS84_F
    equator = {
        "pair": "(0, 0) to (0, 180 - x)",
        "largest_x_deg_with_fallback": lo_x,
        "theory_180f_deg": 180.0 * f,
        "note": ("For points on the equator the shortest geodesic leaves the equator once the "
                 "longitude gap exceeds (1 - f) * 180 deg; Vincenty's lambda iteration fails "
                 "beyond that gap."),
    }

    # Grid of offsets around the antipode of A = (lat0, 0).
    grids = {}
    offs_deg = np.linspace(-1.0, 1.0, grid_n)
    with WarnCounter() as wc:
        for lat0 in (0.0, 45.0):
            err = np.full((grid_n, grid_n), np.nan)
            fbm = np.zeros((grid_n, grid_n), dtype=bool)
            for iy, dlat in enumerate(offs_deg):
                for ix, dlng in enumerate(offs_deg):
                    a = (lat0, 0.0)
                    b = (-lat0 + float(dlat), _wrap_lng(180.0 + float(dlng)))
                    ref_m = W.Inverse(a[0], a[1], b[0], b[1])["s12"]
                    v, outcome = _vincenty_outcome(geo, wc, a, b)
                    err[iy, ix] = abs(v - ref_m)
                    fbm[iy, ix] = outcome == "fallback"
            grids[lat0] = (err, fbm)
    grid_summary = {}
    for lat0, (err, fbm) in grids.items():
        conv_err = err[~fbm]
        dl = offs_deg[np.any(fbm, axis=0)]
        dp = offs_deg[np.any(fbm, axis=1)]
        grid_summary[f"lat0={lat0:g}"] = {
            "grid": f"{grid_n} x {grid_n}, offsets -1 to 1 deg in lat and lng from the antipode",
            "fallback_cells": int(fbm.sum()),
            "cells": int(fbm.size),
            "fallback_lng_offset_span_deg": [float(dl.min()), float(dl.max())] if dl.size else None,
            "fallback_lat_offset_span_deg": [float(dp.min()), float(dp.max())] if dp.size else None,
            "converged_abs_err_m": _pct(conv_err[np.isfinite(conv_err)]),
            "fallback_abs_err_m": _pct(err[fbm]),
        }
    return ({"random": random_summary, "equator_boundary": equator, "grid": grid_summary},
            {"grids": grids, "offs_deg": offs_deg})


def run_poles_antimeridian(geo: GeoEngine, seed: int, n_pole: int, n_am: int) -> dict[str, Any]:
    named = {
        "north pole to south pole": ((90.0, 0.0), (-90.0, 0.0)),
        "north pole, two longitudes (same point)": ((90.0, 0.0), (90.0, 123.0)),
        "south pole to equator": ((-90.0, 0.0), (0.0, 45.0)),
        "north pole to 89.999 N": ((90.0, 0.0), (89.999, 77.0)),
        "lng 180 vs -180 (same point)": ((10.0, 180.0), (10.0, -180.0)),
        "equator across antimeridian": ((0.0, 179.9), (0.0, -179.9)),
        "65 N across antimeridian": ((65.0, 179.5), (66.0, -179.5)),
        "Suva to Apia": ((-18.1416, 178.4415), (-13.8333, -171.7667)),
        "89.9 N across antimeridian": ((89.9, 179.0), (89.9, -179.0)),
        "equatorial antipodes": ((0.0, 0.0), (0.0, 180.0)),
        "exact antipodes near the poles": ((89.5, 0.0), (-89.5, 180.0)),
    }
    cases = {}
    with WarnCounter() as wc:
        for label, (a, b) in named.items():
            r = W.Inverse(a[0], a[1], b[0], b[1], INV_MASK)
            vin, outcome = _vincenty_outcome(geo, wc, a, b)
            hav = geo.distance(a, b, unit="m", method="haversine")
            cases[label] = {
                "a": a, "b": b, "ref_m": r["s12"], "haversine_m": hav, "vincenty_m": vin,
                "vincenty_outcome": outcome,
                "haversine_err_m": hav - r["s12"], "vincenty_err_m": vin - r["s12"],
                "bearing_deg": geo.bearing(a, b, as_degrees=True),
                "geodesic_azimuth_deg": r["azi1"] % 360.0,
            }

    rng = _rng(seed, "poles")
    sets: dict[str, list] = {"pole endpoint (lat exactly +-90)": [], "both points within 1 deg of a pole": [],
                             "antimeridian, short (<= 1 deg lng each side)": [],
                             "antimeridian, long (<= 10 deg lng each side)": []}
    for _ in range(n_pole):
        pole = (rng.choice((90.0, -90.0)), rng.uniform(-180.0, 180.0))
        sets["pole endpoint (lat exactly +-90)"].append((pole, _uniform_point(rng)))
        sgn = rng.choice((1.0, -1.0))
        sets["both points within 1 deg of a pole"].append(
            ((sgn * rng.uniform(89.0, 90.0), rng.uniform(-180.0, 180.0)),
             (sgn * rng.uniform(89.0, 90.0), rng.uniform(-180.0, 180.0))))
    for _ in range(n_am):
        lat = rng.uniform(-80.0, 80.0)
        sets["antimeridian, short (<= 1 deg lng each side)"].append(
            ((lat, rng.uniform(179.0, 180.0)), (lat + rng.uniform(-1.0, 1.0), rng.uniform(-180.0, -179.0))))
        sets["antimeridian, long (<= 10 deg lng each side)"].append(
            ((rng.uniform(-80.0, 80.0), rng.uniform(170.0, 180.0)),
             (rng.uniform(-80.0, 80.0), rng.uniform(-180.0, -170.0))))
    random_sets = {}
    for label, pairs in sets.items():
        meas = measure_pairs(geo, pairs)
        random_sets[label] = summarize(meas)
    return {"named_cases": cases, "random_sets": random_sets}


def run_destination(geo: GeoEngine, seed: int, n: int, class_pairs: list, class_meas: dict,
                    n_rt: int) -> dict[str, Any]:
    rng = _rng(seed, "destination")
    out: dict[str, Any] = {"direct": {}, "roundtrip_public_api": {}, "roundtrip_unrounded": {}}
    for name, lo, hi in CLASSES:
        errs, errs_raw, svals, self_cons = [], [], [], []
        for _ in range(n):
            a = _uniform_point(rng)
            az = rng.uniform(0.0, 360.0)
            s = _log_uniform(rng, lo, min(hi, 1.9e7))
            q = geo.destination(a, s, az, unit="m")
            q_raw = qutils._destination_point(a, s / 1000.0, az)
            d = W.Direct(a[0], a[1], az, s)
            errs.append(W.Inverse(q[0], q[1], d["lat2"], d["lon2"])["s12"])
            errs_raw.append(W.Inverse(q_raw[0], q_raw[1], d["lat2"], d["lon2"])["s12"])
            svals.append(s)
            self_cons.append(abs(qutils._haversine_km(a, q_raw) * 1000.0 - s))
        errs_a = np.array(errs)
        sv = np.array(svals)
        out["direct"][name] = {
            "n": n,
            "position_error_m": _pct(errs_a),
            "position_error_pct_of_distance": _pct(100.0 * errs_a / sv),
            "unrounded_position_error_m": _pct(np.array(errs_raw)),
            "unrounded_position_error_pct_of_distance": _pct(100.0 * np.array(errs_raw) / sv),
            "sphere_self_consistency_abs_m": _pct(np.array(self_cons)),
        }

    # Round trip through the public API: distance + bearing, then destination.
    ref = class_meas["ref"]
    for ci, (name, _lo, _hi) in enumerate(CLASSES):
        sel = np.flatnonzero(class_meas["cls"] == ci)[:n_rt]
        e_pub, e_raw, dist = [], [], []
        for i in sel:
            a, b = class_pairs[i]
            q = geo.destination(a, geo.distance(a, b, unit="m"), geo.bearing(a, b, as_degrees=True),
                                unit="m")
            q_raw = qutils._destination_point(a, qutils._haversine_km(a, b),
                                              qutils._initial_bearing_deg(a, b))
            e_pub.append(W.Inverse(q[0], q[1], b[0], b[1])["s12"])
            e_raw.append(W.Inverse(q_raw[0], q_raw[1], b[0], b[1])["s12"])
            dist.append(ref[i])
        e_pub_a, e_raw_a, d_a = np.array(e_pub), np.array(e_raw), np.array(dist)
        out["roundtrip_public_api"][name] = {
            "n": int(len(sel)),
            "position_error_m": _pct(e_pub_a),
            "position_error_pct_of_distance": _pct(100.0 * e_pub_a / d_a),
        }
        out["roundtrip_unrounded"][name] = {
            "n": int(len(sel)),
            "position_error_m": _pct(e_raw_a),
        }
    out["notes"] = [
        "direct: the same (start, azimuth, distance) into qoregeo destination() and "
        "geographiclib Direct; error is the geodesic distance between the two end points.",
        "roundtrip_public_api: B' = destination(A, distance(A, B), bearing(A, B, as_degrees=True)); "
        "error is |B' B|. bearing() rounds to 0.01 deg, destination() to 1e-6 deg.",
        "roundtrip_unrounded: the same chain on the unrounded internal helpers.",
    ]
    return out


def run_length(geo: GeoEngine, seed: int, n: int, max_v: int) -> dict[str, Any]:
    rng = _rng(seed, "length")
    out: dict[str, Any] = {}
    geojson_mismatch = 0
    checked = 0
    for name, lo, hi in LENGTH_CLASSES:
        ref, hav, vin, nverts = [], [], [], []
        with WarnCounter():
            for k in range(n):
                nv = rng.randint(2, max_v)
                lat, lng = _uniform_point(rng)
                az = rng.uniform(0.0, 360.0)
                pts = [(lat, lng)]
                for _ in range(nv - 1):
                    d = W.Direct(lat, lng, az, _log_uniform(rng, lo, hi))
                    lat, lng = d["lat2"], _wrap_lng(d["lon2"])
                    az = d["azi2"] + rng.gauss(0.0, 30.0)
                    pts.append((lat, lng))
                total = sum(W.Inverse(p[0], p[1], q[0], q[1], Geodesic.DISTANCE)["s12"]
                            for p, q in zip(pts, pts[1:], strict=False))
                ref.append(total)
                hav.append(geo.length(pts, unit="m"))
                vin.append(geo.length(pts, unit="m", method="vincenty"))
                nverts.append(nv)
                if k % 10 == 0:
                    line = {"type": "LineString", "coordinates": [[p[1], p[0]] for p in pts]}
                    checked += 1
                    geojson_mismatch += geo.length(line, unit="m") != hav[-1]
        r = np.array(ref)
        out[name] = {
            "n_polylines": n,
            "vertices": {"min": min(nverts), "max": max(nverts), "mean": statistics.fmean(nverts)},
            "ref_length_m": {"min": r.min(), "median": float(np.median(r)), "max": r.max()},
            "haversine": err_stats(np.array(hav), r),
            "vincenty": err_stats(np.array(vin), r),
        }
    out["geojson_vs_list_input"] = {"checked": checked, "mismatches": int(geojson_mismatch)}
    return out


def run_context(geo: GeoEngine, uniform_meas: dict[str, Any], pairs: list, n: int) -> dict[str, Any]:
    import geopy.distance as gd
    import haversine as hv
    from haversine import Unit
    from haversine import haversine as hv_fn
    from pyproj import Geod

    pairs = pairs[:n]
    ref = uniform_meas["ref"][:n]
    q_hav = uniform_meas["hav"][:n]
    q_vin = uniform_meas["vin"][:n]
    warned = uniform_meas["warned"][:n]

    gc_m = np.array([gd.great_circle(a, b).m for a, b in pairs])
    geod_m = np.array([gd.geodesic(a, b).m for a, b in pairs])
    hv_m = np.array([hv_fn(a, b, unit=Unit.METERS) for a, b in pairs])
    geod = Geod(ellps="WGS84")
    la1 = np.array([p[0][0] for p in pairs])
    lo1 = np.array([p[0][1] for p in pairs])
    la2 = np.array([p[1][0] for p in pairs])
    lo2 = np.array([p[1][1] for p in pairs])
    _, _, pj_m = geod.inv(lo1, la1, lo2, la2)

    hv_mod = sys.modules["haversine.haversine"]
    ell = gd.ELLIPSOIDS["WGS-84"]
    return {
        "n": len(pairs),
        "earth_models": {
            "qoregeo haversine": {"sphere_radius_km": qutils.EARTH_RADIUS_KM},
            "qoregeo vincenty": {"a_m": qutils.WGS84_A, "f": qutils.WGS84_F},
            "haversine package": {"sphere_radius_km": getattr(hv_mod, "_AVG_EARTH_RADIUS_KM", None),
                                  "version": _md.version("haversine")},
            "geopy great_circle": {"sphere_radius_km": gd.EARTH_RADIUS},
            "geopy geodesic": {"ellipsoid": "WGS-84", "a_km": ell[0], "b_km": ell[1],
                               "f": ell[2], "algorithm": "geographiclib (Karney)"},
            "pyproj Geod(ellps='WGS84')": {"a_m": geod.a, "f": geod.f, "algorithm": "PROJ geodesic (Karney)"},
        },
        "error_vs_reference": {
            "haversine package": err_stats(hv_m, ref),
            "geopy great_circle": err_stats(gc_m, ref),
            "geopy geodesic": err_stats(geod_m, ref),
            "pyproj Geod.inv": err_stats(pj_m, ref),
        },
        "max_abs_diff_m": {
            "qoregeo haversine vs haversine package": float(np.max(np.abs(q_hav - hv_m))),
            "qoregeo haversine vs geopy great_circle": float(np.max(np.abs(q_hav - gc_m))),
            "qoregeo haversine vs geopy great_circle, rel pct":
                float(np.max(np.abs(q_hav - gc_m) / np.maximum(gc_m, 1e-9)) * 100.0),
            "qoregeo vincenty (converged) vs geopy geodesic":
                float(np.max(np.abs(q_vin[~warned] - geod_m[~warned]))),
            "qoregeo vincenty (all, incl. fallback) vs geopy geodesic":
                float(np.nanmax(np.abs(q_vin - geod_m))),
            "pyproj vs geographiclib": float(np.max(np.abs(pj_m - ref))),
        },
        "haversine_package_module": hv.__name__,
    }


def run_timing(geo: GeoEngine, pairs: list, fallback_pairs: list, repeats: int) -> dict[str, Any]:
    import geopy.distance as gd
    from haversine import Unit
    from haversine import haversine as hv_fn
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    fns: dict[str, Callable[[tuple, tuple], Any]] = {
        "qoregeo distance(method='haversine')": lambda a, b: geo.distance(a, b, unit="m"),
        "qoregeo distance(method='vincenty')": lambda a, b: geo.distance(a, b, unit="m", method="vincenty"),
        "qoregeo internal _haversine_km": qutils._haversine_km,
        "qoregeo internal _vincenty_km": qutils._vincenty_km,
        "geographiclib Geodesic.WGS84.Inverse (distance only)":
            lambda a, b: W.Inverse(a[0], a[1], b[0], b[1], Geodesic.DISTANCE),
        "geopy geodesic": lambda a, b: gd.geodesic(a, b).m,
        "geopy great_circle": lambda a, b: gd.great_circle(a, b).m,
        "haversine package": lambda a, b: hv_fn(a, b, unit=Unit.METERS),
        "pyproj Geod.inv (scalar call)": lambda a, b: geod.inv(a[1], a[0], b[1], b[0]),
    }

    def bench(fn: Callable[[tuple, tuple], Any], ps: list) -> dict[str, float]:
        per_call = []
        gc_was = gc.isenabled()
        gc.disable()
        try:
            for _ in range(repeats):
                t0 = time.perf_counter()
                for a, b in ps:
                    fn(a, b)
                per_call.append((time.perf_counter() - t0) / len(ps) * 1e6)
        finally:
            if gc_was:
                gc.enable()
        return {"median_us": statistics.median(per_call), "min_us": min(per_call)}

    out: dict[str, Any] = {"pairs": len(pairs), "repeats": repeats, "per_call": {}}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for label, fn in fns.items():
            fn(*pairs[0])
            out["per_call"][label] = bench(fn, pairs)
        if fallback_pairs:
            out["near_antipodal_fallback_pairs"] = len(fallback_pairs)
            out["per_call"]["qoregeo distance(method='vincenty'), fallback pairs"] = bench(
                fns["qoregeo distance(method='vincenty')"], fallback_pairs)
            out["per_call"]["geographiclib Inverse, same fallback pairs"] = bench(
                fns["geographiclib Geodesic.WGS84.Inverse (distance only)"], fallback_pairs)
    # Vectorised pyproj for scale.
    la1 = np.array([p[0][0] for p in pairs])
    lo1 = np.array([p[0][1] for p in pairs])
    la2 = np.array([p[1][0] for p in pairs])
    lo2 = np.array([p[1][1] for p in pairs])
    vec = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        geod.inv(lo1, la1, lo2, la2)
        vec.append((time.perf_counter() - t0) / len(pairs) * 1e6)
    out["per_call"]["pyproj Geod.inv (numpy arrays, per pair)"] = {
        "median_us": statistics.median(vec), "min_us": min(vec)}
    out["note"] = ("Indicative only: measured on a shared 4-CPU machine while other jobs ran. "
                   "Median and min over repeats of a loop over the same pairs.")
    return out


def run_doc_claims(geo: GeoEngine, results: dict[str, Any]) -> list[dict[str, Any]]:
    delhi, mumbai, jaipur = (28.6139, 77.2090), (19.0760, 72.8777), (26.9124, 75.7873)
    ref_dm = W.Inverse(*delhi, *mumbai)
    route = [delhi, jaipur, mumbai]
    ref_route = sum(W.Inverse(*p, *q)["s12"] for p, q in zip(route, route[1:], strict=False))

    all_hav = [results["uniform_random"]["haversine"]["rel_pct"]["max"]]
    all_hav += [c["haversine"]["rel_pct"]["max"] for k, c in results["distance_classes"].items()
                if not k.startswith("_")]
    vin_conv = [results["uniform_random"]["vincenty_converged_only"]["abs_m"]["max"]]
    vin_conv += [c["vincenty_converged_only"]["abs_m"]["max"]
                 for k, c in results["distance_classes"].items() if not k.startswith("_")]
    vin_conv.append(results["near_antipodal"]["random"]["vincenty_converged"]["abs_m"]["max"])
    fb = results["near_antipodal"]["random"]["vincenty_fallback"]
    d_dest = geo.destination(delhi, 10, 90)
    ref_dest = W.Direct(delhi[0], delhi[1], 90.0, 10_000.0)
    return [
        {"claim": "README: haversine is 'within about 0.5 % of the true distance'",
         "measured": f"max |relative error| over all random sets = {max(all_hav):.4f} %",
         "max_rel_pct": max(all_hav)},
        {"claim": "README and docstring: Vincenty is 'accurate to about 0.5 mm'",
         "measured": (f"max |error| over converged calls = {max(v for v in vin_conv if v is not None) * 1000:.4f} mm "
                      "(includes the 0.05 mm rounding of the public API)"),
         "max_abs_m": max(v for v in vin_conv if v is not None)},
        {"claim": "README: near-antipodal Vincenty warns and falls back to haversine",
         "measured": (f"{results['near_antipodal']['random']['outcomes']} on the near-antipodal set; "
                      f"fallback values equal to haversine: "
                      f"{results['near_antipodal']['random']['fallback_equals_haversine']}"),
         "fallback_abs_err_m_max": fb["abs_m"]["max"], "fallback_rel_pct_max": fb["rel_pct"]["max"]},
        {"claim": "tools.py note: the fallback is 'accurate to about 0.5%'",
         "measured": f"fallback |relative error| max = {fb['rel_pct']['max']}"},
        {"claim": "README: Delhi to Mumbai haversine 1148.0965 km, vincenty 1144.5264 km",
         "qoregeo_haversine_km": geo.distance(delhi, mumbai),
         "qoregeo_vincenty_km": geo.distance(delhi, mumbai, method="vincenty"),
         "geographiclib_km": ref_dm["s12"] / 1000.0},
        {"claim": "README: Delhi to Mumbai bearing 203.47 deg",
         "qoregeo_bearing_deg": geo.bearing(delhi, mumbai, as_degrees=True),
         "geodesic_azimuth_deg": ref_dm["azi1"] % 360.0},
        {"claim": "README: route Delhi, Jaipur, Mumbai length 1156.0342 km (haversine), 1152.4909 km (vincenty)",
         "qoregeo_haversine_km": geo.length(route),
         "qoregeo_vincenty_km": geo.length(route, method="vincenty"),
         "geographiclib_km": ref_route / 1000.0},
        {"claim": "docstring: destination((28.6139, 77.2090), 10, 90) == (28.613861, 77.311444)",
         "qoregeo": d_dest, "geographiclib_direct": (ref_dest["lat2"], ref_dest["lon2"]),
         "position_difference_m": W.Inverse(d_dest[0], d_dest[1], ref_dest["lat2"], ref_dest["lon2"])["s12"]},
        {"claim": "README: bearing() of identical points returns 'North' / 0.0",
         "same_tuple": geo.bearing((10.0, 20.0), (10.0, 20.0), as_degrees=True),
         "same_point_lng_180_vs_minus_180": geo.bearing((10.0, 180.0), (10.0, -180.0), as_degrees=True),
         "same_point_lng_180_vs_minus_180_compass": geo.bearing((10.0, 180.0), (10.0, -180.0)),
         "same_point_north_pole_two_lngs": geo.bearing((90.0, 0.0), (90.0, 123.0), as_degrees=True),
         "distance_lng_180_vs_minus_180_m": geo.distance((10.0, 180.0), (10.0, -180.0), unit="m")},
    ]


# Figures

def _style() -> None:
    plt.rcParams.update({
        "svg.hashsalt": "qoregeo-experiment-01",
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


def fig_relative_error(meas_list: list[dict[str, Any]]) -> str:
    ref = np.concatenate([m["ref"] for m in meas_list])
    hav = np.concatenate([m["hav"] for m in meas_list])
    vin = np.concatenate([m["vin"] for m in meas_list])
    vraw = np.concatenate([m["vin_raw"] for m in meas_list])
    keep = ref > 0
    ref, hav, vin, vraw = ref[keep], hav[keep], vin[keep], vraw[keep]
    floor = 1e-12
    edges = np.logspace(0, math.log10(2.01e7), 49)
    centers = np.sqrt(edges[:-1] * edges[1:])

    def bands(est: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        rel = np.maximum(np.abs(est - ref) / ref * 100.0, floor)
        med = np.full(centers.size, np.nan)
        p95 = np.full(centers.size, np.nan)
        mx = np.full(centers.size, np.nan)
        idx = np.digitize(ref, edges) - 1
        for i in range(centers.size):
            v = rel[idx == i]
            v = v[np.isfinite(v)]
            if v.size >= 5:
                med[i], p95[i] = np.percentile(v, [50, 95])
                mx[i] = v.max()
        return med, p95, mx

    fig, ax = plt.subplots(figsize=(8, 5.4))
    for est, color, label in ((hav, C_HAV, "haversine (sphere, R = 6371.0088 km)"),
                              (vin, C_VIN, "vincenty (WGS-84), public API")):
        med, p95, mx = bands(est)
        ax.fill_between(centers, med, p95, color=color, alpha=0.18, linewidth=0)
        ax.plot(centers, med, color=color, linewidth=2, label=f"{label}: median, shaded to p95")
        ax.plot(centers, mx, color=color, linewidth=1, linestyle=(0, (1, 1.5)),
                label=f"{label}: max")
    med_raw, _, _ = bands(vraw)
    ax.plot(centers, med_raw, color=C_VIN, linewidth=1.2, linestyle=(0, (4, 2)), alpha=0.8,
            label="vincenty without the 0.1 mm output rounding: median")
    ax.plot(centers, 0.05e-3 / centers * 100.0, color=C_GREY, linewidth=1,
            label="0.05 mm (half the 0.1 mm output step)")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(1, 2.1e7)
    ax.set_ylim(1e-11, 3)
    ax.set_xticks([1, 10, 100, 1e3, 1e4, 1e5, 1e6, 1e7])
    ax.set_xticklabels(["1 m", "10 m", "100 m", "1 km", "10 km", "100 km", "1,000 km", "10,000 km"])
    ax.set_xlabel("Geodesic distance (WGS-84, log scale)")
    ax.set_ylabel("|relative error| vs geographiclib (%)")
    ax.annotate("near-antipodal pairs:\nvincenty falls back to haversine", xy=(1.95e7, 3e-2),
                xytext=(2.5e6, 1e-3), fontsize=7.5, color=INK2, ha="right",
                arrowprops={"arrowstyle": "-", "color": INK2, "linewidth": 0.6})
    fig.legend(*ax.get_legend_handles_labels(), loc="lower center", ncol=2, fontsize=8,
               bbox_to_anchor=(0.5, 0.02))
    fig.tight_layout(rect=(0, 0.17, 1, 1))
    _note(fig)
    return _save(fig, "relative-error")


def fig_haversine_latitude(class_meas: dict[str, Any], class_pairs: list, seed: int) -> str:
    ref = class_meas["ref"]
    sel = np.flatnonzero((ref >= 1e3) & (ref < 1e5))
    rng = np.random.default_rng(seed)
    if sel.size > 4000:
        sel = rng.choice(sel, 4000, replace=False)
    lat = np.array([(class_pairs[i][0][0] + class_pairs[i][1][0]) / 2 for i in sel])
    az = np.radians(class_meas["ref_az"][sel])
    rel = (class_meas["hav"][sel] - ref[sel]) / ref[sel] * 100.0
    cos2 = np.cos(az) ** 2
    ns = cos2 >= 0.95
    ew = cos2 <= 0.05
    mid = ~(ns | ew)

    phi = np.radians(np.linspace(-90, 90, 361))
    a, f = qutils.WGS84_A, qutils.WGS84_F
    e2 = f * (2 - f)
    w = np.sqrt(1 - e2 * np.sin(phi) ** 2)
    m_rad = a * (1 - e2) / w ** 3
    n_rad = a / w
    r = qutils.EARTH_RADIUS_M

    fig, ax = plt.subplots(figsize=(8, 4.4))
    ax.scatter(lat[mid], rel[mid], s=3, color=C_GREY, alpha=0.35, linewidths=0, rasterized=True,
               label="other directions")
    ax.scatter(lat[ns], rel[ns], s=5, color=C_HAV, alpha=0.7, linewidths=0, rasterized=True,
               label="north-south pairs (|cos az| >= 0.975)")
    ax.scatter(lat[ew], rel[ew], s=5, color=C_VIN, alpha=0.7, linewidths=0, rasterized=True,
               label="east-west pairs (|sin az| >= 0.975)")
    ax.plot(np.degrees(phi), (r / m_rad - 1) * 100, color=C_HAV, linewidth=1.2,
            label="R / M(lat) - 1 (meridian radius)")
    ax.plot(np.degrees(phi), (r / n_rad - 1) * 100, color=C_VIN, linewidth=1.2,
            label="R / N(lat) - 1 (prime vertical radius)")
    ax.axhline(0, color=INK2, linewidth=0.6)
    ax.set_xlim(-90, 90)
    ax.set_xticks(range(-90, 91, 30))
    ax.set_xlabel("Latitude of the pair midpoint (deg)")
    ax.set_ylabel("haversine relative error (%)")
    ax.legend(loc="upper center", fontsize=8, ncol=2, markerscale=2.5)
    ax.set_ylim(-0.6, 0.85)
    fig.tight_layout(rect=(0, 0.02, 1, 1))
    _note(fig)
    return _save(fig, "haversine-by-latitude")


def fig_antipodal(grid_data: dict[str, Any]) -> str:
    grids = grid_data["grids"]
    offs = grid_data["offs_deg"]
    fig, axes = plt.subplots(1, 2, figsize=(8, 3.7), sharey=True, layout="constrained")
    cmap = matplotlib.colormaps["Blues"]
    norm = LogNorm(vmin=1e-5, vmax=3e4)
    step = offs[1] - offs[0]
    ext = (offs[0] - step / 2, offs[-1] + step / 2, offs[0] - step / 2, offs[-1] + step / 2)
    im = None
    for ax, (lat0, (err, fbm)) in zip(axes, grids.items(), strict=True):
        im = ax.imshow(np.clip(err, 1e-5, None), origin="lower", extent=ext, cmap=cmap, norm=norm,
                       interpolation="none", aspect="equal")
        if fbm.any() and not fbm.all():
            ax.contour(offs, offs, fbm.astype(float), levels=[0.5], colors=[C_VIN], linewidths=1.2)
        ax.set_title(f"A = ({lat0:g}, 0)", fontsize=9.5, color=INK)
        ax.set_xlabel("B longitude offset from antipode (deg)")
        ax.grid(False)
    axes[0].set_ylabel("B latitude offset from antipode (deg)")
    cb = fig.colorbar(im, ax=list(axes), shrink=0.9, pad=0.02)
    cb.set_label("vincenty |error| vs geographiclib (m)")
    proxy = Line2D([], [], color=C_VIN, linewidth=1.2,
                   label="boundary of calls that warned and fell back to haversine")
    fig.legend(handles=[proxy], loc="outside lower center", fontsize=8)
    _note(fig)
    return _save(fig, "antipodal")


# Main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--quick", action="store_true", help="reduced sizes, runs in a few seconds")
    ap.add_argument("--seed", type=int, default=20261019, help="random seed (default 20261019)")
    args = ap.parse_args(argv)
    mode = "quick" if args.quick else "full"
    sz = SIZES[mode]
    t_start = time.perf_counter()
    _style()

    geo = GeoEngine(verbose=False)
    results: dict[str, Any] = {}

    print(f"[{mode}] uniform random pairs: {sz['uniform_pairs']}", flush=True)
    results["uniform_random"], uni_meas, uni_pairs = run_uniform(geo, args.seed, sz["uniform_pairs"])

    print(f"[{mode}] stratified classes: {sz['per_class']} per class", flush=True)
    results["distance_classes"], cls_meas, cls_pairs = run_classes(geo, args.seed, sz["per_class"])

    print(f"[{mode}] near-antipodal pairs", flush=True)
    results["near_antipodal"], grid_data = run_antipodal(geo, args.seed, sz["antipodal_random"],
                                                         sz["antipodal_grid"])

    print(f"[{mode}] poles and antimeridian", flush=True)
    results["poles_antimeridian"] = run_poles_antimeridian(geo, args.seed, sz["pole_random"],
                                                           sz["antimeridian_random"])

    results["haversine_theory"] = haversine_theory()

    results["bearing"] = {
        "uniform_random": results["uniform_random"]["bearing_vs_geodesic_azimuth"],
        "by_class": {k: v["bearing_vs_geodesic_azimuth"]
                     for k, v in results["distance_classes"].items() if not k.startswith("_")},
        "note": ("qoregeo bearing() is the initial great-circle bearing on a sphere, rounded to "
                 "0.01 deg; the reference is the WGS-84 geodesic initial azimuth. Origins exactly "
                 "at a pole are excluded."),
    }

    print(f"[{mode}] destination", flush=True)
    results["destination"] = run_destination(geo, args.seed, sz["destination_per_class"],
                                             cls_pairs, cls_meas, sz["roundtrip_per_class"])

    print(f"[{mode}] polyline length", flush=True)
    results["length"] = run_length(geo, args.seed, sz["polylines_per_class"],
                                   sz["polyline_max_vertices"])

    print(f"[{mode}] context libraries", flush=True)
    results["context_libraries"] = run_context(geo, uni_meas, uni_pairs, sz["context_pairs"])

    print(f"[{mode}] timing", flush=True)
    fallback_pairs = []
    with WarnCounter() as wc:
        rng = _rng(args.seed, "timing-fallback")
        while len(fallback_pairs) < min(200, sz["timing_pairs"]):
            a = _uniform_point(rng)
            b = (-a[0] + rng.uniform(-0.05, 0.05), _wrap_lng(a[1] + 180.0 + rng.uniform(-0.3, 0.3)))
            if _vincenty_outcome(geo, wc, a, b)[1] == "fallback":
                fallback_pairs.append((a, b))
    results["timing"] = run_timing(geo, uni_pairs[: sz["timing_pairs"]], fallback_pairs,
                                   sz["timing_repeats"])

    results["doc_claims"] = run_doc_claims(geo, results)

    print(f"[{mode}] figures", flush=True)
    figures = [
        fig_relative_error([uni_meas, cls_meas]),
        fig_haversine_latitude(cls_meas, cls_pairs, args.seed),
        fig_antipodal(grid_data),
    ]

    import geopy
    import pyproj

    meta = {
        "experiment": NAME,
        "mode": mode,
        "qoregeo_version": qoregeo.__version__,
        "python_version": sys.version,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "reference_libraries": {
            "geographiclib": _md.version("geographiclib"),
            "geopy": geopy.__version__,
            "haversine": _md.version("haversine"),
            "pyproj": pyproj.__version__,
            "proj": pyproj.proj_version_str,
            "numpy": np.__version__,
            "matplotlib": matplotlib.__version__,
        },
        "seed": args.seed,
        "sizes": sz,
        "distance_classes_m": {n: [lo, hi] for n, lo, hi in CLASSES},
        "utc_timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "command": shlex.join(["python", *sys.argv]),
        "python_executable": sys.executable,
        "runtime_seconds": None,
        "figures": figures,
        "notes": [
            "Reference: geographiclib Geodesic.WGS84 Inverse/Direct (about 15 nm accuracy).",
            "qoregeo distance() rounds to 4 decimals of the unit: 0.1 mm with unit='m'.",
            "Points uniform on the sphere (uniform in sin(lat)), not area-uniform on the ellipsoid.",
            "Timing numbers are indicative; the machine was shared with other jobs.",
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
