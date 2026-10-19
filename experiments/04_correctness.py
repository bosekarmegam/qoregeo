"""Experiment 04: correctness of qoregeo on hard cases and against independent code.

References:

* shapely (GEOS) for planar point-in-polygon and intersects. qoregeo reads
  polygon edges as straight lines in lng/lat, the same planar model as
  shapely, so the two are compared like with like.
* geographiclib ``Geodesic.WGS84`` (Karney 2013) for distances on the
  WGS-84 ellipsoid.
* Brute force in numpy for longitude spans, bounding-box membership and the
  spherical mean.

Sections:

1. ``point_in_polygon()`` and ``filter_by_polygon()`` against
   ``shapely.contains_xy`` on seeded random polygons (convex, concave, with
   1 to 3 holes, MultiPolygons, grid-aligned) and 1,000,000 test points, many
   of them on vertices, on edges or on a vertex's latitude. Each disagreement
   is classified as vertex, boundary (within 1e-12 deg) or numerical.
   Polygons drawn across 180 deg with continuous longitudes, and the same
   polygons split at 180 deg into a MultiPolygon, are tested separately.
2. ``intersects()`` against ``shapely.intersects`` for 10,000 pairs of
   points, LineStrings and Polygons: random, touching, nested and near-miss
   pairs, plus pairs that meet across the antimeridian.
3. ``filter_by_radius()`` against geographiclib distances for points close
   to the circle edge, with ``method="haversine"`` and ``"vincenty"``.
4. Antimeridian: a Fiji and Tonga dataset across 180 deg. ``bbox()``,
   ``centroid()``, ``filter_by_bbox()`` with west > east, and the framing of
   ``map_html()``, ``heatmap_html()`` and ``image()``, each against a naive
   min/max approach and a brute-force reference. Randomized clusters
   generalize the check.
5. Poles, null geometries, empty collections and out-of-range coordinates:
   each call's outcome against the behavior the README documents.

Run from the repository root::

    python experiments/04_correctness.py            # full run (a few minutes)
    python experiments/04_correctness.py --quick    # reduced sizes, a few seconds

Outputs: ``experiments/results/correctness.json`` and
``docs/assets/experiments/correctness-*.svg``.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import html
import importlib.metadata as _md
import json
import math
import os
import platform
import re
import shlex
import sys
import tempfile
import time
import traceback
import warnings
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import shapely  # noqa: E402
import shapely.affinity  # noqa: E402
from geographiclib.geodesic import Geodesic  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
from shapely.geometry import box as sbox  # noqa: E402
from shapely.geometry import mapping  # noqa: E402
from shapely.geometry import shape as to_shape  # noqa: E402

import qoregeo  # noqa: E402
from qoregeo import GeoEngine  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
NAME = "correctness"
RESULTS_PATH = REPO / "experiments" / "results" / f"{NAME}.json"
FIG_DIR = REPO / "docs" / "assets" / "experiments"

W = Geodesic.WGS84
TOL = 1e-12            # degrees: "on the boundary" threshold used for classification
R_KM = 6371.0088       # qoregeo's haversine radius (IUGG mean radius), for the numpy reference

SIZES: dict[str, dict[str, int]] = {
    "full": {
        "pip_polygons_per_kind": 400,
        "pip_points_total": 1_000_000,
        "pip_intersects_per_polygon": 20,
        "wrap_polygons": 200,
        "wrap_points_per_polygon": 500,
        "intersects_pairs": 10_000,
        "intersects_wrap_pairs": 1_000,
        "radius_points": 3_000,
        "am_scatter": 400,
        "am_random_sets": 500,
        "am_points_per_set": 60,
        "am_render_sets": 60,
        "pole_points": 2_000,
    },
    "quick": {
        "pip_polygons_per_kind": 8,
        "pip_points_total": 4_000,
        "pip_intersects_per_polygon": 5,
        "wrap_polygons": 6,
        "wrap_points_per_polygon": 100,
        "intersects_pairs": 300,
        "intersects_wrap_pairs": 60,
        "radius_points": 150,
        "am_scatter": 60,
        "am_random_sets": 30,
        "am_points_per_set": 30,
        "am_render_sets": 5,
        "pole_points": 200,
    },
}

PIP_KINDS = ("convex", "concave", "holes", "multipolygon", "grid")
POINT_MIX = (("uniform", 0.70), ("edge", 0.08), ("vertex", 0.04), ("vertex_lat", 0.08),
             ("near_edge", 0.10))

# Chart colours: categorical slots of the dataviz reference palette.
C_A = "#2a78d6"
C_B = "#eb6834"
C_C = "#1f9e74"
C_D = "#8e5cc8"
C_GREY = "#8a8985"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e4e3df"


# Helpers

def _rng(seed: int, section: int) -> np.random.Generator:
    """An independent, reproducible stream per experiment section."""
    return np.random.default_rng([seed, section])


def _f(x: Any) -> Any:
    """Make a value JSON-safe (numpy scalars to Python, NaN and inf to None)."""
    if isinstance(x, (np.floating, float)):
        x = float(x)
        return x if math.isfinite(x) else None
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    if isinstance(x, np.ndarray):
        return [_f(v) for v in x.tolist()]
    if isinstance(x, dict):
        return {str(k): _f(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_f(v) for v in x]
    return x


def _wrap(x: np.ndarray | float) -> Any:
    """Wrap longitudes into [-180, 180)."""
    return (np.asarray(x) + 180.0) % 360.0 - 180.0


def _outcome(fn: Callable[[], Any]) -> dict[str, Any]:
    """Run ``fn`` and record its value or exception plus every warning."""
    with warnings.catch_warnings(record=True) as log:
        warnings.simplefilter("always")
        try:
            value = fn()
            out: dict[str, Any] = {"raised": None, "value": value}
        except Exception as exc:  # noqa: BLE001 (the outcome is the data)
            out = {"raised": type(exc).__name__,
                   "bases": [c.__name__ for c in type(exc).__mro__[1:-1]],
                   "message": str(exc).strip(), "exc": exc}
    out["warnings"] = [str(w.message) for w in log]
    return out


def _closed(ring: np.ndarray) -> list[list[float]]:
    pts = [[float(x), float(y)] for x, y in ring]
    if pts[0] != pts[-1]:
        pts.append(list(pts[0]))
    return pts


def _star(rng: np.random.Generator, n: int, rmin: float) -> np.ndarray:
    """``n`` vertices around the origin at increasing angles (a simple, star-shaped ring)."""
    while True:
        ang = np.sort(rng.uniform(0.0, 2.0 * math.pi, int(n)))
        gaps = np.diff(np.append(ang, ang[0] + 2.0 * math.pi))
        if gaps.min() > 1e-6 and gaps.max() < math.pi * 0.98:
            break
    rad = np.ones(int(n)) if rmin >= 1.0 else rng.uniform(rmin, 1.0, int(n))
    return np.column_stack([rad * np.cos(ang), rad * np.sin(ang)])


def _regular(rng: np.random.Generator, m: int) -> np.ndarray:
    """A convex ring of ``m`` >= 8 jittered vertices on the unit circle (inradius > 0.8)."""
    ang = np.sort(2.0 * math.pi * (np.arange(m) + rng.uniform(-0.3, 0.3, m)) / m)
    return np.column_stack([np.cos(ang), np.sin(ang)])


def _inradius(ring: np.ndarray) -> float:
    """Smallest distance from the origin to the closed ring's edges."""
    a = ring
    b = np.roll(ring, -1, axis=0)
    ab = b - a
    t = np.clip(-(a * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-300), 0.0, 1.0)
    p = a + t[:, None] * ab
    return float(np.sqrt((p * p).sum(1)).min())


class Frame:
    """Affine map from a normalized drawing space to lng/lat degrees."""

    def __init__(self, cx: float, cy: float, s: float, aspect: float, rot: float) -> None:
        self.cx, self.cy, self.s = cx, cy, s
        c, si = math.cos(rot), math.sin(rot)
        self.m = np.array([[c * s, -si * s * aspect], [si * s, c * s * aspect]])

    def pt(self, p: np.ndarray) -> np.ndarray:
        p = np.asarray(p, dtype=float)
        return p @ self.m.T + np.array([self.cx, self.cy])

    def vec(self, v: np.ndarray) -> np.ndarray:
        w = np.asarray(v, dtype=float) @ self.m.T
        return w / np.linalg.norm(w)


def _geom_vertices(gj: dict[str, Any]) -> np.ndarray:
    pts: list[list[float]] = []

    def walk(c: Any) -> None:
        if isinstance(c, list) and c and isinstance(c[0], (int, float)):
            pts.append([float(c[0]), float(c[1])])
        elif isinstance(c, list):
            for x in c:
                walk(x)

    walk(gj["coordinates"])
    return np.array(pts, dtype=float).reshape(-1, 2)


def _rings_of(gj: dict[str, Any]) -> list[list[list[float]]]:
    if gj["type"] == "Polygon":
        return list(gj["coordinates"])
    if gj["type"] == "MultiPolygon":
        return [r for poly in gj["coordinates"] for r in poly]
    if gj["type"] == "LineString":
        return [gj["coordinates"]]
    return []


def _edges(gj: dict[str, Any]) -> np.ndarray:
    segs = []
    for ring in _rings_of(gj):
        for i in range(1, len(ring)):
            segs.append([ring[i - 1], ring[i]])
    return np.array(segs, dtype=float).reshape(-1, 2, 2)


def _shapely_to_geojson(geom: Any) -> dict[str, Any]:
    gj = mapping(geom)

    def lists(c: Any) -> Any:
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            return [float(c[0]), float(c[1])]
        return [lists(x) for x in c]

    return {"type": gj["type"], "coordinates": lists(gj["coordinates"])}


def _points_engine(xs: np.ndarray, ys: np.ndarray) -> GeoEngine:
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [x, y]},
              "properties": {"i": i}}
             for i, (x, y) in enumerate(zip(xs.tolist(), ys.tolist(), strict=True))]
    return GeoEngine(verbose=False).load_data(feats)


def _kept(engine: GeoEngine, n: int) -> np.ndarray:
    mask = np.zeros(n, dtype=bool)
    for f in engine.get_features():
        mask[f["properties"]["i"]] = True
    return mask


def _dist_to_vertices(verts: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    out = np.empty(xs.size)
    for k in range(xs.size):
        out[k] = np.hypot(verts[:, 0] - xs[k], verts[:, 1] - ys[k]).min()
    return out


# 1. point_in_polygon against shapely

def _pip_polygon(rng: np.random.Generator, kind: str) -> dict[str, Any]:
    """A seeded random valid polygon of ``kind`` placed somewhere on the globe."""
    s = 10.0 ** rng.uniform(-4.0, math.log10(12.0))
    cy = rng.uniform(-75.0, 75.0)
    cx = rng.uniform(-165.0, 165.0)
    if kind == "grid":
        return _grid_polygon(rng, cx, cy, s)
    fr = Frame(cx, cy, s, rng.uniform(0.3, 1.0), rng.uniform(0.0, math.pi))
    polys = _norm_polygons(rng, kind)
    out_polys = []
    for rings in polys:
        out_rings = []
        for ring in rings:
            r = _closed(fr.pt(ring))
            if rng.random() < 0.5:
                r = r[::-1]
            out_rings.append(r)
        out_polys.append(out_rings)
    gj: dict[str, Any] = ({"type": "Polygon", "coordinates": out_polys[0]} if len(out_polys) == 1
                          else {"type": "MultiPolygon", "coordinates": out_polys})
    return _finish_polygon(gj, s, kind)


def _norm_polygons(rng: np.random.Generator, kind: str) -> list[list[np.ndarray]]:
    if kind == "convex":
        return [[_star(rng, int(rng.integers(3, 41)), 1.0)]]
    if kind == "concave":
        return [[_star(rng, int(rng.integers(5, 201)), rng.uniform(0.2, 0.8))]]
    if kind == "holes":
        outer = _star(rng, int(rng.integers(12, 121)), rng.uniform(0.7, 0.9))
        r_in = _inradius(outer)
        k = int(rng.integers(1, 4))
        holes: list[np.ndarray] = []
        placed: list[tuple[np.ndarray, float]] = []
        for _ in range(400):
            if len(holes) == k:
                break
            rh = rng.uniform(0.08, 0.2) * r_in
            rho = rng.uniform(0.0, r_in - 1.05 * rh)
            phi = rng.uniform(0.0, 2.0 * math.pi)
            c = rho * np.array([math.cos(phi), math.sin(phi)])
            if any(np.linalg.norm(c - c2) < rh + r2 + 0.02 * r_in for c2, r2 in placed):
                continue
            placed.append((c, rh))
            holes.append(c + rh * _star(rng, int(rng.integers(3, 25)), rng.uniform(0.4, 1.0)))
        return [[outer, *holes]]
    if kind == "multipolygon":
        k = int(rng.integers(2, 5))
        cells = rng.permutation(4)[:k]
        parts = []
        for cell in cells:
            off = np.array([-0.5 if cell % 2 == 0 else 0.5, -0.5 if cell < 2 else 0.5])
            sub = _norm_polygons(rng, str(rng.choice(["convex", "concave", "holes"])))[0]
            parts.append([off + 0.45 * r for r in sub])
        return parts
    raise ValueError(kind)


def _grid_polygon(rng: np.random.Generator, cx: float, cy: float, s: float) -> dict[str, Any]:
    """A union of grid cells whose coordinates are exact binary fractions."""
    for _ in range(100):
        m = int(rng.integers(4, 13))
        k = int(np.clip(round(math.log2(2.0 * s / m)), -20, 3))
        step = 2.0 ** k
        ox = math.floor((cx - m * step / 2) / step) * step
        oy = math.floor((cy - m * step / 2) / step) * step
        cells = {(m // 2, m // 2)}
        target = int(rng.integers(m, max(m + 1, int(m * m * 0.6))))
        while len(cells) < target:
            i, j = list(cells)[int(rng.integers(len(cells)))]
            di, dj = [(1, 0), (-1, 0), (0, 1), (0, -1)][int(rng.integers(4))]
            if 0 <= i + di < m and 0 <= j + dj < m:
                cells.add((i + di, j + dj))
        boxes = [sbox(ox + i * step, oy + j * step, ox + (i + 1) * step, oy + (j + 1) * step)
                 for i, j in sorted(cells)]
        geom = shapely.unary_union(boxes)
        if geom.is_valid and geom.geom_type in ("Polygon", "MultiPolygon"):
            out = _finish_polygon(_shapely_to_geojson(geom), s, "grid")
            out.update(grid_origin=(ox, oy), grid_step=step)
            return out
    raise RuntimeError("could not build a grid polygon")


def _finish_polygon(gj: dict[str, Any], s: float, kind: str) -> dict[str, Any]:
    geom = to_shape(gj)
    if not geom.is_valid:
        raise RuntimeError(f"generated an invalid {kind} polygon: {shapely.is_valid_reason(geom)}")
    shapely.prepare(geom)
    verts = _geom_vertices(gj)
    return {"geojson": gj, "geom": geom, "verts": verts, "edges": _edges(gj),
            "bbox": geom.bounds, "scale": s, "kind": kind,
            "n_vertices": int(verts.shape[0])}


def _pip_points(rng: np.random.Generator, poly: dict[str, Any], n: int
                ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Test points: uniform in the padded bbox plus hard cases. Returns xs, ys, category index."""
    counts = [int(round(n * frac)) for _, frac in POINT_MIX]
    counts[0] += n - sum(counts)
    minx, miny, maxx, maxy = poly["bbox"]
    w, h = maxx - minx, maxy - miny
    verts, edges = poly["verts"], poly["edges"]
    xs_parts, ys_parts, cats = [], [], []
    for ci, ((name, _), c) in enumerate(zip(POINT_MIX, counts, strict=True)):
        if c == 0:
            continue
        if name == "uniform":
            x = rng.uniform(minx - 0.05 * w, maxx + 0.05 * w, c)
            y = rng.uniform(miny - 0.05 * h, maxy + 0.05 * h, c)
            if poly["kind"] == "grid":
                (ox, oy), step = poly["grid_origin"], poly["grid_step"]
                sx = rng.random(c) < 0.5
                sy = rng.random(c) < 0.5
                x = np.where(sx, ox + step * np.round((x - ox) / step), x)
                y = np.where(sy, oy + step * np.round((y - oy) / step), y)
        elif name in ("edge", "near_edge"):
            e = edges[rng.integers(0, edges.shape[0], c)]
            t = rng.uniform(0.0, 1.0, c)[:, None]
            p = e[:, 0] + t * (e[:, 1] - e[:, 0])
            if name == "near_edge":
                d = e[:, 1] - e[:, 0]
                nrm = np.column_stack([-d[:, 1], d[:, 0]]) / np.linalg.norm(d, axis=1)[:, None]
                off = 10.0 ** rng.uniform(-11.0, -6.0, c) * rng.choice([-1.0, 1.0], c)
                p = p + nrm * off[:, None]
            x, y = p[:, 0], p[:, 1]
        elif name == "vertex":
            v = verts[rng.integers(0, verts.shape[0], c)]
            x, y = v[:, 0].copy(), v[:, 1].copy()
        else:   # vertex_lat: the horizontal ray passes exactly through a vertex
            y = verts[rng.integers(0, verts.shape[0], c), 1].copy()
            x = rng.uniform(minx - 0.05 * w, maxx + 0.05 * w, c)
        xs_parts.append(np.clip(x, -180.0, 180.0))
        ys_parts.append(np.clip(y, -90.0, 90.0))
        cats.append(np.full(c, ci))
    return np.concatenate(xs_parts), np.concatenate(ys_parts), np.concatenate(cats)


def _classify(geom: Any, verts: np.ndarray, xs: np.ndarray, ys: np.ndarray
              ) -> tuple[list[str], np.ndarray]:
    """vertex (within TOL of a vertex), boundary (within TOL of an edge) or numerical."""
    if xs.size == 0:
        return [], np.zeros(0)
    d_b = shapely.distance(geom.boundary, shapely.points(xs, ys))
    d_v = _dist_to_vertices(verts, xs, ys)
    cls = ["vertex" if dv <= TOL else "boundary" if db <= TOL else "numerical"
           for dv, db in zip(d_v, d_b, strict=True)]
    return cls, d_b


def run_pip(seed: int, sz: dict[str, int]) -> dict[str, Any]:
    rng = _rng(seed, 1)
    geo = GeoEngine(verbose=False)
    per_kind = sz["pip_polygons_per_kind"]
    per_poly = sz["pip_points_total"] // (per_kind * len(PIP_KINDS))
    k_int = sz["pip_intersects_per_polygon"]
    cat_names = [c for c, _ in POINT_MIX]

    def blank() -> dict[str, Any]:
        return {"n": 0, "on_boundary_geos": 0, "qoregeo_true_on_boundary": 0,
                "vs_contains": Counter(), "vs_covers": Counter(), "public_vs_filter": 0}

    stats = {k: {c: blank() for c in cat_names} for k in PIP_KINDS}
    poly_info = {k: {"n_polygons": 0, "vertices": [], "scales": []} for k in PIP_KINDS}
    numerical_examples: list[dict[str, Any]] = []
    max_db_numerical = 0.0
    inter = {"n": 0, "disagree": Counter(), "beyond_tol": []}
    t_pub = t_fil = t_shp = 0.0

    for kind in PIP_KINDS:
        for _ in range(per_kind):
            poly = _pip_polygon(rng, kind)
            gj, geom = poly["geojson"], poly["geom"]
            poly_info[kind]["n_polygons"] += 1
            poly_info[kind]["vertices"].append(poly["n_vertices"])
            poly_info[kind]["scales"].append(poly["scale"])
            xs, ys, cats = _pip_points(rng, poly, per_poly)
            n = xs.size

            t0 = time.perf_counter()
            q_pub = np.fromiter((geo.point_in_polygon((y, x), gj)
                                 for x, y in zip(xs.tolist(), ys.tolist(), strict=True)),
                                dtype=bool, count=n)
            t1 = time.perf_counter()
            q_fil = _kept(_points_engine(xs, ys).filter_by_polygon(gj), n)
            t2 = time.perf_counter()
            s_in = shapely.contains_xy(geom, xs, ys)
            s_cov = shapely.intersects_xy(geom, xs, ys)
            t3 = time.perf_counter()
            t_pub += t1 - t0
            t_fil += t2 - t1
            t_shp += t3 - t2

            on_b = s_cov & ~s_in
            dis_in = q_pub != s_in
            dis_cov = q_pub != s_cov
            idx = np.flatnonzero(dis_in | dis_cov)
            cls, d_b = _classify(geom, poly["verts"], xs[idx], ys[idx])
            cls_of = dict(zip(idx.tolist(), cls, strict=True))
            db_of = dict(zip(idx.tolist(), d_b.tolist(), strict=True))
            for ci, cname in enumerate(cat_names):
                sel = cats == ci
                st = stats[kind][cname]
                st["n"] += int(sel.sum())
                st["on_boundary_geos"] += int((on_b & sel).sum())
                st["qoregeo_true_on_boundary"] += int((on_b & sel & q_pub).sum())
                st["public_vs_filter"] += int(((q_pub != q_fil) & sel).sum())
                for i in np.flatnonzero(dis_in & sel).tolist():
                    st["vs_contains"][cls_of[i]] += 1
                for i in np.flatnonzero(dis_cov & sel).tolist():
                    st["vs_covers"][cls_of[i]] += 1
            for i, c in cls_of.items():
                if c == "numerical" and (dis_in[i] or dis_cov[i]):
                    max_db_numerical = max(max_db_numerical, db_of[i])
                    if len(numerical_examples) < 5:
                        numerical_examples.append({
                            "kind": kind, "point_lng_lat": [xs[i], ys[i]],
                            "category": cat_names[int(cats[i])],
                            "qoregeo": bool(q_pub[i]), "shapely_contains": bool(s_in[i]),
                            "distance_to_boundary_deg": db_of[i], "polygon": gj})

            # intersects(Point, polygon) on a subsample, half of it near the edges.
            near = np.flatnonzero(cats == cat_names.index("near_edge"))
            pick = np.concatenate([rng.choice(near, min(k_int // 2, near.size), replace=False),
                                   rng.choice(n, k_int - min(k_int // 2, near.size), replace=False)])
            for i in pick.tolist():
                q = geo.intersects({"type": "Point", "coordinates": [xs[i], ys[i]]}, gj)
                inter["n"] += 1
                if q != bool(s_cov[i]):
                    d = float(shapely.distance(geom, shapely.points(xs[i], ys[i])))
                    label = ("boundary (<= 1e-12 deg)" if d <= TOL else
                             "qoregeo True, > 1e-12 deg away" if q else "qoregeo False, shapely True")
                    inter["disagree"][label] += 1
                    if d > TOL and q:
                        e = poly["edges"]
                        seg_len = np.linalg.norm(e[:, 1] - e[:, 0], axis=1)
                        dseg = shapely.distance(shapely.linestrings(e), shapely.points(xs[i], ys[i]))
                        inter["beyond_tol"].append({
                            "distance_deg": d, "polygon_scale_deg": poly["scale"],
                            "nearest_edge_length_deg": float(seg_len[int(np.argmin(dseg))])})

    # Totals and tables.
    totals = {"n": 0, "on_boundary_geos": 0, "vs_contains": Counter(), "vs_covers": Counter(),
              "public_vs_filter": 0, "qoregeo_true_on_boundary": 0}
    by_kind: dict[str, Any] = {}
    by_category: dict[str, Any] = {c: {"n": 0, "on_boundary_geos": 0, "qoregeo_true_on_boundary": 0,
                                       "vs_contains": Counter(), "vs_covers": Counter()}
                                   for c in cat_names}
    for kind in PIP_KINDS:
        kt = {"n": 0, "vs_contains": Counter(), "vs_covers": Counter(), "public_vs_filter": 0,
              "on_boundary_geos": 0}
        for cname in cat_names:
            st = stats[kind][cname]
            for tgt in (kt, totals, by_category[cname]):
                tgt["n"] += st["n"]
                tgt["on_boundary_geos"] += st["on_boundary_geos"]
                tgt["vs_contains"].update(st["vs_contains"])
                tgt["vs_covers"].update(st["vs_covers"])
            kt["public_vs_filter"] += st["public_vs_filter"]
            totals["public_vs_filter"] += st["public_vs_filter"]
            totals["qoregeo_true_on_boundary"] += st["qoregeo_true_on_boundary"]
            by_category[cname]["qoregeo_true_on_boundary"] += st["qoregeo_true_on_boundary"]
        info = poly_info[kind]
        by_kind[kind] = {**kt, "n_polygons": info["n_polygons"],
                         "vertices_median": float(np.median(info["vertices"])),
                         "vertices_max": int(np.max(info["vertices"])),
                         "scale_deg_min": float(np.min(info["scales"])),
                         "scale_deg_max": float(np.max(info["scales"]))}
    tol_rows = inter.pop("beyond_tol")
    inter["beyond_tolerance_max_distance_deg"] = max((r["distance_deg"] for r in tol_rows), default=None)
    inter["beyond_tolerance_examples"] = sorted(tol_rows, key=lambda r: -r["distance_deg"])[:5]
    inter["beyond_tolerance_count"] = len(tol_rows)
    return {
        "points_per_polygon": per_poly,
        "totals": totals,
        "by_kind": by_kind,
        "by_category": by_category,
        "by_kind_and_category": stats,
        "numerical_disagreement_max_distance_deg": max_db_numerical,
        "numerical_examples": numerical_examples,
        "intersects_point_vs_polygon": inter,
        "seconds": {"qoregeo_point_in_polygon": t_pub, "qoregeo_filter_by_polygon": t_fil,
                    "shapely_contains_and_intersects": t_shp},
        "notes": [
            "Reference: shapely.contains_xy (interior only) and shapely.intersects_xy "
            "(interior or boundary). qoregeo's point_in_polygon() is ray casting with straight "
            "lng/lat edges, the same planar model as shapely.",
            "A point counts as 'vertex' within 1e-12 deg of a vertex, as 'boundary' within "
            "1e-12 deg of an edge, else 'numerical'.",
            "public_vs_filter counts points where point_in_polygon() and filter_by_polygon() "
            "(an indexed implementation) give different answers.",
        ],
    }


# 1b. polygons across the antimeridian

def _wrap_polygon(rng: np.random.Generator) -> dict[str, Any]:
    """A polygon crossing 180 deg written with continuous longitudes, and its split form."""
    for _ in range(200):
        kind = str(rng.choice(["convex", "concave", "holes"]))
        s = 10.0 ** rng.uniform(-2.0, 1.0)
        side = 1.0 if rng.random() < 0.5 else -1.0
        cx = 180.0 + rng.uniform(-0.6, 0.6) * s
        if side < 0:
            cx -= 360.0
        fr = Frame(cx, rng.uniform(-70.0, 70.0), s, rng.uniform(0.3, 1.0), rng.uniform(0, math.pi))
        rings = [_closed(fr.pt(r)) for r in _norm_polygons(rng, kind)[0]]
        gj = {"type": "Polygon", "coordinates": rings}
        geom = to_shape(gj)
        seam = 180.0 * side
        minx, _, maxx, _ = geom.bounds
        if not (geom.is_valid and minx < seam < maxx):
            continue
        west = geom.intersection(sbox(minx - 1.0, -90.0, seam, 90.0))
        east = geom.intersection(sbox(seam, -90.0, maxx + 1.0, 90.0))
        if side > 0:
            east = shapely.affinity.translate(east, xoff=-360.0)
        else:
            west = shapely.affinity.translate(west, xoff=360.0)
        parts = [g for piece in (west, east) for g in getattr(piece, "geoms", [piece])
                 if g.geom_type == "Polygon" and g.area > 0]
        split = _shapely_to_geojson(shapely.MultiPolygon(parts))

        def snap(c: Any) -> Any:
            if isinstance(c[0], float):
                x = c[0]
                if abs(abs(x) - 180.0) < 1e-9:
                    x = math.copysign(180.0, x)
                return [x, c[1]]
            return [snap(v) for v in c]

        split["coordinates"] = snap(split["coordinates"])
        shapely.prepare(geom)
        return {"continuous": gj, "split": split, "geom": geom, "verts": _geom_vertices(gj),
                "bbox": geom.bounds, "scale": s, "kind": kind, "side": side}
    raise RuntimeError("could not build a polygon across the antimeridian")


def run_pip_wrap(seed: int, sz: dict[str, int]) -> dict[str, Any]:
    rng = _rng(seed, 2)
    geo = GeoEngine(verbose=False)
    n = sz["wrap_points_per_polygon"]
    res: dict[str, Any] = {}
    counters = {form: {"n": 0, "vs_contains": Counter(), "vs_covers": Counter(),
                       "public_vs_filter": 0} for form in ("continuous", "split")}
    seam_answers = {form: Counter() for form in ("continuous", "split")}
    for _ in range(sz["wrap_polygons"]):
        poly = _wrap_polygon(rng)
        minx, miny, maxx, maxy = poly["bbox"]
        n_seam, n_vert = n // 10, n // 20
        n_uni = n - n_seam - n_vert
        xu = rng.uniform(minx, maxx, n_uni)
        yu = rng.uniform(miny, maxy, n_uni)
        xs_seam = rng.choice([180.0, -180.0], n_seam)
        ys_seam = rng.uniform(miny, maxy, n_seam)
        v = poly["verts"][rng.integers(0, poly["verts"].shape[0], n_vert)]
        xs_raw = np.concatenate([xu, xs_seam, v[:, 0]])
        ys = np.concatenate([yu, ys_seam, v[:, 1]])
        xs = np.where(np.abs(xs_raw) == 180.0, xs_raw, _wrap(xs_raw))
        is_seam = np.abs(xs) == 180.0
        geom = poly["geom"]
        ref_in = np.zeros(xs.size, dtype=bool)
        ref_cov = np.zeros(xs.size, dtype=bool)
        d_b = np.full(xs.size, np.inf)
        d_v = np.full(xs.size, np.inf)
        for k in (-360.0, 0.0, 360.0):
            ref_in |= shapely.contains_xy(geom, xs + k, ys)
            ref_cov |= shapely.intersects_xy(geom, xs + k, ys)
        for form in ("continuous", "split"):
            gj = poly[form]
            q = np.fromiter((geo.point_in_polygon((y, x), gj)
                             for x, y in zip(xs.tolist(), ys.tolist(), strict=True)),
                            dtype=bool, count=xs.size)
            qf = _kept(_points_engine(xs, ys).filter_by_polygon(gj), xs.size)
            c = counters[form]
            c["n"] += xs.size
            c["public_vs_filter"] += int((q != qf).sum())
            idx = np.flatnonzero((q != ref_in) | (q != ref_cov))
            if idx.size and not np.isfinite(d_b[idx]).all():
                for k in (-360.0, 0.0, 360.0):
                    d_b[idx] = np.minimum(d_b[idx], shapely.distance(
                        geom.boundary, shapely.points(xs[idx] + k, ys[idx])))
                    d_v[idx] = np.minimum(d_v[idx], _dist_to_vertices(poly["verts"], xs[idx] + k, ys[idx]))
            for i in idx.tolist():
                label = ("vertex" if d_v[i] <= TOL else "boundary" if d_b[i] <= TOL
                         else "seam (lng exactly +-180)" if is_seam[i] else "numerical")
                if q[i] != ref_in[i]:
                    c["vs_contains"][label] += 1
                if q[i] != ref_cov[i]:
                    c["vs_covers"][label] += 1
            interior_seam = is_seam & ref_in
            for i in np.flatnonzero(interior_seam).tolist():
                seam_answers[form][f"lng {xs[i]:+.0f}: qoregeo {'inside' if q[i] else 'outside'}"] += 1
    res["counts"] = counters
    res["seam_points_inside_the_shape"] = seam_answers
    res["notes"] = [
        "Reference: the point is inside if (lng + k * 360, lat) is inside the continuous "
        "polygon for some k in -1, 0, 1 (shapely). qoregeo receives the point with its "
        "longitude in -180..180.",
        "'continuous' rings use longitudes beyond +-180 (as buffer() does); 'split' is the "
        "same shape cut at 180 deg into a MultiPolygon (RFC 7946 section 3.1.9).",
        "Seam points lie exactly on lng +-180 inside the physical shape. In the split form "
        "that meridian is a planar boundary of both parts.",
    ]
    return res


# 2. intersects against shapely

COMBOS = (("Point", "Point"), ("Point", "LineString"), ("Point", "Polygon"),
          ("LineString", "LineString"), ("LineString", "Polygon"), ("Polygon", "Polygon"))
REGIMES = (("random", 0.40), ("touch", 0.25), ("nested", 0.15), ("near", 0.20))
GAPS = (1e-5, 1e-7, 1e-9, 1e-10, 1e-11, 1e-13)


def _npoly(rng: np.random.Generator, center: np.ndarray, radius: float,
           convex: bool | None = None, hole: bool | None = None) -> dict[str, Any]:
    """A star-shaped polygon (its centre in the kernel), optionally with one convex hole."""
    convex = bool(rng.random() < 0.4) if convex is None else convex
    hole = bool(rng.random() < 0.3) if hole is None else hole
    unit = _star(rng, int(rng.integers(3, 30)) if convex else int(rng.integers(5, 60)),
                 1.0 if convex else rng.uniform(0.3, 0.8))
    r_in = radius * _inradius(unit)
    out: dict[str, Any] = {"outer": center + radius * unit, "center": center, "r_in": r_in,
                           "hole": None, "convex": convex}
    if hole and r_in > 0:
        rh = rng.uniform(0.15, 0.35) * r_in
        rho = rng.uniform(1.2 * rh, r_in - 1.1 * rh)
        phi = rng.uniform(0, 2 * math.pi)
        hc = center + rho * np.array([math.cos(phi), math.sin(phi)])
        out["hole"] = (hc, rh, hc + rh * _regular(rng, int(rng.integers(8, 16))))
    return out


def _nline(rng: np.random.Generator) -> np.ndarray:
    """A random-walk LineString of 2 to 8 vertices in [-1, 1]^2, no zero-length segment."""
    k = int(rng.integers(2, 9))
    p = rng.uniform(-1, 1, 2)
    pts = [p]
    while len(pts) < k:
        ang = rng.uniform(0, 2 * math.pi)
        q = np.clip(p + rng.uniform(0.1, 0.8) * np.array([math.cos(ang), math.sin(ang)]), -1, 1)
        if np.linalg.norm(q - p) > 0.05:
            pts.append(q)
            p = q
    return np.array(pts)


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)


def _rot(v: np.ndarray, a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([c * v[0] - s * v[1], s * v[0] + c * v[1]])


def _random_geom(rng: np.random.Generator, t: str) -> tuple[str, Any]:
    if t == "Point":
        return "Point", rng.uniform(-1, 1, 2)
    if t == "LineString":
        return "LineString", _nline(rng)
    return "Polygon", _npoly(rng, rng.uniform(-0.5, 0.5, 2), rng.uniform(0.15, 0.6))


def _contact(rng: np.random.Generator, base: tuple[str, Any], t: str
             ) -> tuple[tuple[str, Any], np.ndarray, str]:
    """A geometry of type ``t`` touching ``base``, the direction away from ``base``, and how."""
    btype, b = base
    d = rng.uniform(0.1, 0.5)
    if btype == "Point":
        anchor, out, how = b, _unit(rng.normal(size=2)), "same point"
    elif btype == "LineString":
        i = int(rng.integers(0, len(b) - 1))
        seg = b[i + 1] - b[i]
        out = _unit(np.array([-seg[1], seg[0]])) * rng.choice([-1.0, 1.0])
        if t == "LineString" and rng.random() < 0.3:
            t1, t2 = sorted(rng.uniform(0, 1, 2))
            return ("LineString", np.array([b[i] + t1 * seg, b[i] + t2 * seg])), out, "collinear overlap"
        if rng.random() < 0.5:
            anchor, how = b[i].copy(), "exact vertex"
        else:
            anchor, how = b[i] + rng.uniform(0, 1) * seg, "interpolated on a segment"
    else:
        ring = b["outer"]
        if t == "Polygon" and b["convex"] and rng.random() < 0.4:
            i = int(rng.integers(0, len(ring)))
            a0, a1 = ring[i], ring[(i + 1) % len(ring)]
            mid = (a0 + a1) / 2
            nrm = _unit(np.array([-(a1 - a0)[1], (a1 - a0)[0]]))
            if np.dot(nrm, mid - b["center"]) < 0:
                nrm = -nrm
            return ("Polygon", {"outer": np.array([a0, a1, mid + d * nrm]), "hole": None}), nrm, "shared edge"
        i = int(rng.integers(0, len(ring)))
        if rng.random() < 0.5:
            anchor, how = ring[i].copy(), "exact vertex"
        else:
            anchor = ring[i] + rng.uniform(0, 1) * (ring[(i + 1) % len(ring)] - ring[i])
            how = "interpolated on an edge"
        out = _unit(anchor - b["center"])
    if t == "Point":
        return ("Point", anchor), out, how
    if t == "LineString":
        pts = [anchor, anchor + d * _rot(out, rng.uniform(-1.0, 1.0))]
        if rng.random() < 0.5:
            pts.append(pts[-1] + d * _unit(rng.normal(size=2)))
        return ("LineString", np.array(pts)), out, how
    th = rng.uniform(math.radians(5), math.radians(30))
    tri = np.array([anchor, anchor + d * _rot(out, th), anchor + d * _rot(out, -th)])
    return ("Polygon", {"outer": tri, "hole": None}), out, how


def _nested(rng: np.random.Generator, base: dict[str, Any], t: str) -> tuple[tuple[str, Any], str]:
    c, r_in, hole = base["center"], base["r_in"], base["hole"]
    in_hole = hole is not None and rng.random() < 0.4
    if t == "Point":
        return ("Point", hole[0] if in_hole else c), "in hole" if in_hole else "inside"
    if t == "LineString":
        if in_hole:
            hc, rh, _ = hole
            u1, u2 = _unit(rng.normal(size=2)), _unit(rng.normal(size=2))
            return ("LineString", np.array([hc + 0.4 * rh * u1, hc + 0.4 * rh * u2])), "in hole"
        u = _unit(rng.normal(size=2))
        return ("LineString", np.array([c, c + 0.5 * r_in * u])), "inside"
    if in_hole:
        hc, _, hring = hole
        return ("Polygon", {"outer": hc + 0.5 * (hring - hc), "hole": None}), "in hole"
    tt = rng.uniform(0.1, 0.9)
    return ("Polygon", {"outer": c + tt * (base["outer"] - c), "hole": None}), "inside"


def _to_geojson(g: tuple[str, Any], fr: Frame, shift: np.ndarray | None = None) -> dict[str, Any]:
    gtype, v = g
    off = np.zeros(2) if shift is None else shift
    if gtype == "Point":
        p = fr.pt(v) + off
        return {"type": "Point", "coordinates": [float(p[0]), float(p[1])]}
    if gtype == "LineString":
        return {"type": "LineString",
                "coordinates": [[float(x), float(y)] for x, y in fr.pt(v) + off]}
    rings = [_closed(fr.pt(v["outer"]) + off)]
    if v.get("hole") is not None:
        rings.append(_closed(fr.pt(v["hole"][2]) + off))
    return {"type": "Polygon", "coordinates": rings}


def _pair(rng: np.random.Generator) -> dict[str, Any]:
    ta, tb = COMBOS[int(rng.integers(len(COMBOS)))]
    regime = str(rng.choice([r for r, _ in REGIMES], p=[p for _, p in REGIMES]))
    if regime == "nested" and tb != "Polygon":
        regime = "touch"
    s = 10.0 ** rng.uniform(-5.0, 1.0)
    fr = Frame(rng.uniform(-150, 150), rng.uniform(-70, 70), s, rng.uniform(0.3, 1.0),
               rng.uniform(0, math.pi))
    gap, how = None, ""
    if regime == "random":
        a, b = _random_geom(rng, ta), _random_geom(rng, tb)
        ga, gb = _to_geojson(a, fr), _to_geojson(b, fr)
    elif regime == "nested":
        base = ("Polygon", _npoly(rng, np.zeros(2), rng.uniform(0.4, 0.9)))
        inner, how = _nested(rng, base[1], ta)
        ga, gb = _to_geojson(base, fr), _to_geojson(inner, fr)
    else:
        base = _random_geom(rng, tb)
        if tb == "Polygon":
            base = ("Polygon", _npoly(rng, np.zeros(2), rng.uniform(0.3, 0.8)))
        attached, out, how = _contact(rng, base, ta)
        shift = None
        if regime == "near":
            gap = float(rng.choice(GAPS))
            shift = gap * fr.vec(out)
        ga, gb = _to_geojson(base, fr), _to_geojson(attached, fr, shift)
    if rng.random() < 0.5:
        ga, gb = gb, ga
    return {"a": ga, "b": gb, "combo": f"{ta}-{tb}", "regime": regime, "how": how,
            "gap_deg": gap, "scale_deg": s}


def _contact_edge(ga: dict[str, Any], gb: dict[str, Any], sa: Any, sb: Any) -> float | None:
    """Length of the shorter of the two edges (one per shape) nearest to the other shape."""
    best = None
    for g, other in ((ga, sb), (gb, sa)):
        e = _edges(g)
        if e.shape[0] == 0:
            continue
        k = int(np.argmin(shapely.distance(shapely.linestrings(e), other)))
        length = float(np.linalg.norm(e[k, 1] - e[k, 0]))
        best = length if best is None else min(best, length)
    return best


def run_intersects(seed: int, sz: dict[str, int]) -> dict[str, Any]:
    rng = _rng(seed, 3)
    geo = GeoEngine(verbose=False)
    table: dict[str, dict[str, Counter]] = {}
    disagree = Counter()
    asym = 0
    examples: dict[str, list[dict[str, Any]]] = {}
    near_rows: list[dict[str, Any]] = []
    t_q = t_s = 0.0
    for _ in range(sz["intersects_pairs"]):
        p = _pair(rng)
        t0 = time.perf_counter()
        q = geo.intersects(p["a"], p["b"])
        t1 = time.perf_counter()
        sa, sb = to_shape(p["a"]), to_shape(p["b"])
        ref = bool(shapely.intersects(sa, sb))
        t2 = time.perf_counter()
        t_q += t1 - t0
        t_s += t2 - t1
        if geo.intersects(p["b"], p["a"]) != q:
            asym += 1
        d = float(shapely.distance(sa, sb))
        cell = table.setdefault(p["combo"], {}).setdefault(p["regime"], Counter())
        cell["n"] += 1
        cell["shapely_true"] += int(ref)
        cell["qoregeo_true"] += int(q)
        label = None
        if q and not ref:
            label = "qoregeo True, shapely False, distance <= 1e-12 deg" if d <= TOL \
                else "qoregeo True, shapely False, distance > 1e-12 deg"
        elif ref and not q:
            label = ("qoregeo False, shapely True (boundary contact only)"
                     if shapely.touches(sa, sb) else "qoregeo False, shapely True (interiors meet)")
        if label:
            cell["disagree"] += 1
            disagree[label] += 1
            ex = examples.setdefault(label, [])
            if len(ex) < 3:
                ex.append({**p, "shapely_distance_deg": d, "qoregeo": q, "shapely": ref})
        if p["regime"] == "near":
            near_rows.append({"gap_deg": p["gap_deg"], "distance_deg": d,
                              "scale_deg": p["scale_deg"], "qoregeo": q, "shapely": ref,
                              "contact_edge_deg": _contact_edge(p["a"], p["b"], sa, sb)})
    near_summary = {}
    for g in GAPS:
        rows = [r for r in near_rows if r["gap_deg"] == g]
        for lo, hi, name in ((1e-5, 1e-3, "scale 1e-5 to 1e-3 deg"), (1e-3, 1e-1, "scale 1e-3 to 0.1 deg"),
                             (1e-1, 11.0, "scale 0.1 to 10 deg")):
            sub = [r for r in rows if lo <= r["scale_deg"] < hi]
            near_summary.setdefault(f"gap {g:g} deg", {})[name] = {
                "n": len(sub), "shapely_true": sum(r["shapely"] for r in sub),
                "qoregeo_true": sum(r["qoregeo"] for r in sub)}
    wrap = run_intersects_wrap(rng, geo, sz["intersects_wrap_pairs"])
    return {
        "n_pairs": sz["intersects_pairs"],
        "table": {k: {r: dict(c) for r, c in v.items()} for k, v in table.items()},
        "disagreements": dict(disagree),
        "disagreement_examples": examples,
        "asymmetric_answers": asym,
        "near_miss_by_gap_and_scale": near_summary,
        "near_rows": near_rows,
        "antimeridian": wrap,
        "seconds": {"qoregeo_intersects": t_q, "shapely_intersects": t_s},
        "notes": [
            "Reference: shapely.intersects (GEOS, exact predicates). Touching counts in both.",
            "Regimes: random (both shapes in one window), touch (constructed contact at a vertex, "
            "on an edge, a shared edge or a collinear overlap), nested (inside a polygon or "
            "inside its hole), near (a touch pair moved apart by a gap of 1e-13 to 1e-5 deg).",
            "Shape sizes (scale) are log-uniform from 1e-5 deg (about 1 m) to 10 deg.",
        ],
    }


def run_intersects_wrap(rng: np.random.Generator, geo: GeoEngine, n: int) -> dict[str, Any]:
    """Shapes meeting across 180 deg. The reference compares the shapes on one continuous range."""
    counts: dict[str, Counter] = {}
    examples = []
    for _ in range(n):
        s = 10.0 ** rng.uniform(-3, 1)
        cy = rng.uniform(-60, 60)
        mode = str(rng.choice(["random", "seam_touch", "seam_gap"]))
        h = s * rng.uniform(0.2, 1.0)
        if mode == "random":
            wa = s * rng.uniform(0.1, 1.0)
            a_poly = [[180.0 - wa, cy - h], [180.0 + wa * rng.uniform(-0.5, 0.8), cy - h * rng.uniform(0.2, 1)],
                      [180.0 + wa * rng.uniform(-0.5, 0.8), cy + h], [180.0 - wa, cy + h * rng.uniform(0.2, 1)]]
            a = {"type": "Polygon", "coordinates": [a_poly + [a_poly[0]]]}
            bx = -180.0 + s * rng.uniform(0.0, 0.5)
            kind = str(rng.choice(["Point", "LineString"]))
            if kind == "Point":
                b = {"type": "Point", "coordinates": [bx, cy + rng.uniform(-h, h)]}
            else:
                b = {"type": "LineString", "coordinates": [[bx, cy + rng.uniform(-h, h)],
                                                           [bx + s * rng.uniform(0.1, 1), cy + rng.uniform(-h, h)]]}
        else:
            gap = 0.0 if mode == "seam_touch" else float(rng.choice(GAPS))
            wa, wb = s * rng.uniform(0.1, 1), s * rng.uniform(0.1, 1)
            a = {"type": "Polygon", "coordinates": [[[180.0 - wa, cy - h], [180.0, cy - h], [180.0, cy + h],
                                                     [180.0 - wa, cy + h], [180.0 - wa, cy - h]]]}
            y0, y1 = cy + h * rng.uniform(-0.9, 0.5), cy + h * rng.uniform(0.6, 1.4)
            x0 = -180.0 + gap
            kind = str(rng.choice(["Point", "LineString", "Polygon"]))
            if kind == "Point":
                b = {"type": "Point", "coordinates": [x0, y0]}
            elif kind == "LineString":
                b = {"type": "LineString", "coordinates": [[x0, y0], [x0 + wb, y1]]}
            else:
                b = {"type": "Polygon", "coordinates": [[[x0, y0], [x0 + wb, y0], [x0 + wb, y1],
                                                         [x0, y1], [x0, y0]]]}
        sa, sb = to_shape(a), to_shape(b)
        ref = any(bool(shapely.intersects(sa, shapely.affinity.translate(sb, xoff=k)))
                  for k in (-360.0, 0.0, 360.0))
        d = min(float(shapely.distance(sa, shapely.affinity.translate(sb, xoff=k)))
                for k in (-360.0, 0.0, 360.0))
        q = geo.intersects(a, b)
        c = counts.setdefault(f"{mode} ({kind})", Counter())
        c["n"] += 1
        c["reference_true"] += int(ref)
        c["qoregeo_true"] += int(q)
        if q != ref:
            c["disagree"] += 1
            c["disagree_within_1e-12"] += int(d <= TOL)
            if len(examples) < 3:
                examples.append({"a": a, "b": b, "qoregeo": q, "reference": ref, "distance_deg": d})
    return {"counts": {k: dict(v) for k, v in counts.items()}, "disagreement_examples": examples,
            "notes": ["Shape A ends at or crosses lng 180 (continuous longitudes); shape B starts "
                      "at or just east of lng -180. Reference: shapely after moving B by -360, 0 "
                      "or +360 deg; distance is the smallest of the three."]}


# 3. filter_by_radius against geographiclib

RADIUS_LATS = (0.0, 30.0, 60.0, 85.0)
RADII_KM = (1.0, 10.0, 100.0, 1000.0)


def _np_haversine_km(lat1: float, lng1: float, lat2: np.ndarray, lng2: np.ndarray) -> np.ndarray:
    p1, p2 = math.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dl = np.radians(lng2 - lng1)
    a = np.sin(dphi / 2) ** 2 + math.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * R_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def run_radius(seed: int, sz: dict[str, int]) -> dict[str, Any]:
    rng = _rng(seed, 4)
    n = sz["radius_points"]
    rows = []
    curves: dict[str, Any] = {}
    for lat0 in RADIUS_LATS:
        lng0 = float(rng.uniform(-180, 180))
        for r_km in RADII_KM:
            for mode in ("near_edge", "area_uniform"):
                if mode == "near_edge":
                    s = r_km * 1000.0 * (1.0 + rng.uniform(-0.01, 0.01, n))
                else:
                    s = 2.0 * r_km * 1000.0 * np.sqrt(rng.uniform(0, 1, n))
                az = rng.uniform(-180, 180, n)
                lat = np.empty(n)
                lng = np.empty(n)
                ref = np.empty(n)
                azi = np.empty(n)
                for i in range(n):
                    dr = W.Direct(lat0, lng0, float(az[i]), float(s[i]))
                    lat[i], lng[i] = dr["lat2"], _wrap(dr["lon2"])
                    inv = W.Inverse(lat0, lng0, lat[i], lng[i])
                    ref[i], azi[i] = inv["s12"], inv["azi1"]
                truth = ref <= r_km * 1000.0
                eng = _points_engine(lng, lat)
                res = {"latitude": lat0, "radius_km": r_km, "points": mode, "n": n,
                       "truly_inside": int(truth.sum())}
                hav = _np_haversine_km(lat0, lng0, lat, lng)
                for method in ("haversine", "vincenty"):
                    kept = _kept(eng.filter_by_radius(lat0, lng0, r_km, method=method), n)
                    false_in = kept & ~truth
                    false_out = ~kept & truth
                    res[method] = {"included_but_outside": int(false_in.sum()),
                                   "excluded_but_inside": int(false_out.sum()),
                                   "misclassified_pct": 100.0 * float((kept != truth).mean())}
                    if method == "haversine":
                        sphere = hav <= r_km
                        res[method]["mismatches_explained_by_sphere_model"] = int(
                            ((kept != truth) & (kept == sphere)).sum())
                        res[method]["mismatches_not_explained"] = int(
                            ((kept != truth) & (kept != sphere)).sum())
                    else:
                        bad = np.flatnonzero(kept != truth)
                        res[method]["max_abs_margin_m_of_mismatches"] = (
                            float(np.abs(ref[bad] - r_km * 1000.0).max()) if bad.size else None)
                rel = (hav * 1000.0 - ref) / ref * 100.0
                ok = ref > 0
                res["haversine_rel_error_pct"] = {"min": float(rel[ok].min()), "max": float(rel[ok].max())}
                if mode == "near_edge":
                    lo, hi = rel[ok].min() / 100.0, rel[ok].max() / 100.0
                    res["risk_band_m"] = {"from": r_km * 1000.0 / (1 + hi), "to": r_km * 1000.0 / (1 + lo),
                                          "width": r_km * 1000.0 * (1 / (1 + lo) - 1 / (1 + hi))}
                    if r_km == 100.0:
                        order = np.argsort(azi)
                        curves[f"{lat0:g}"] = {"azimuth": azi[order][::max(1, n // 300)],
                                               "rel_pct": rel[order][::max(1, n // 300)]}
                rows.append(res)
    pooled: dict[str, Any] = {}
    for lat0 in RADIUS_LATS:
        for mode in ("near_edge", "area_uniform"):
            sub = [r for r in rows if r["latitude"] == lat0 and r["points"] == mode]
            n_all = sum(r["n"] for r in sub)
            pooled.setdefault(mode, {})[f"{lat0:g}"] = {
                "n": n_all,
                "haversine_misclassified": sum(r["haversine"]["included_but_outside"]
                                               + r["haversine"]["excluded_but_inside"] for r in sub),
                "haversine_misclassified_pct": 100.0 * sum(
                    r["haversine"]["included_but_outside"] + r["haversine"]["excluded_but_inside"]
                    for r in sub) / n_all,
                "vincenty_misclassified": sum(r["vincenty"]["included_but_outside"]
                                              + r["vincenty"]["excluded_but_inside"] for r in sub),
                "vincenty_misclassified_pct": 100.0 * sum(
                    r["vincenty"]["included_but_outside"] + r["vincenty"]["excluded_but_inside"]
                    for r in sub) / n_all,
                "haversine_included_but_outside": sum(r["haversine"]["included_but_outside"] for r in sub),
                "haversine_excluded_but_inside": sum(r["haversine"]["excluded_but_inside"] for r in sub),
            }
    return {"rows": rows, "curves_r100km": curves, "near_edge_by_latitude": pooled["near_edge"],
            "area_uniform_by_latitude": pooled["area_uniform"],
            "notes": [
                "Truth: geographiclib geodesic distance on WGS-84 <= radius. Test points come "
                "from Geodesic.Direct; their distance is recomputed with Geodesic.Inverse.",
                "near_edge: geodesic distance uniform within +-1 % of the radius. area_uniform: "
                "uniform by area in a disk of twice the radius.",
                "A haversine mismatch is 'explained by the sphere model' when an independent "
                "numpy haversine with R = 6371.0088 km gives the same in/out answer as qoregeo.",
            ]}


# 4. Antimeridian

ANCHORS = (   # name, territory, lat, lng (approximate town coordinates)
    ("Suva", "Fiji", -18.1416, 178.4419), ("Nadi", "Fiji", -17.7765, 177.4356),
    ("Lautoka", "Fiji", -17.6167, 177.4667), ("Labasa", "Fiji", -16.4167, 179.3833),
    ("Savusavu", "Fiji", -16.7769, 179.3328), ("Vunisea", "Fiji", -19.0500, 178.1500),
    ("Waiyevo", "Fiji", -16.7894, -179.9922), ("Lomaloma", "Fiji", -17.2833, -178.9833),
    ("Tubou", "Fiji", -18.2333, -178.8000), ("Ahau", "Fiji (Rotuma)", -12.5000, 177.0500),
    ("Nuku'alofa", "Tonga", -21.1393, -175.2049), ("Neiafu", "Tonga", -18.6496, -173.9833),
    ("Pangai", "Tonga", -19.8000, -174.3500), ("'Ohonua", "Tonga", -21.3500, -174.9500),
    ("Hihifo", "Tonga", -15.9600, -173.7800), ("Mata-Utu", "Wallis and Futuna", -13.2825, -176.1736),
    ("Leava", "Wallis and Futuna", -14.2933, -178.1583),
)
ROUTE = {"type": "LineString", "coordinates": [[178.4419, -18.1416], [-178.9833, -17.2833],
                                               [-178.8000, -18.2333], [-175.2049, -21.1393]]}
ZONE = {"type": "MultiPolygon", "coordinates": [
    [[[179.5, -19.5], [180.0, -19.5], [180.0, -16.5], [179.5, -16.5], [179.5, -19.5]]],
    [[[-180.0, -19.5], [-178.0, -19.5], [-178.0, -16.5], [-180.0, -16.5], [-180.0, -19.5]]]]}


def _min_arc(lngs: np.ndarray) -> tuple[float, float]:
    """Brute force: the narrowest longitude interval covering every value. (west, width)."""
    x = np.unique(_wrap(np.asarray(lngs, dtype=float)))
    widths = ((x[None, :] - x[:, None]) % 360.0).max(axis=1)
    i = int(np.argmin(widths))
    return float(x[i]), float(widths[i])


def _covered(lngs: np.ndarray, west: float, east: float) -> np.ndarray:
    return ((np.asarray(lngs) - west) % 360.0) <= ((east - west) % 360.0) + 1e-9


def _vector_mean(lat: np.ndarray, lng: np.ndarray) -> tuple[float, float]:
    la, lo = np.radians(lat), np.radians(lng)
    x, y, z = (np.cos(la) * np.cos(lo)).mean(), (np.cos(la) * np.sin(lo)).mean(), np.sin(la).mean()
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def _page_config(page: str) -> dict[str, Any]:
    key = "var CONFIG = JSON.parse("
    i = page.index(key) + len(key)
    literal, _ = json.JSONDecoder().raw_decode(page, i)
    return json.loads(literal)


def _positions(obj: Any) -> list[list[float]]:
    out: list[list[float]] = []

    def walk(c: Any) -> None:
        if isinstance(c, dict):
            for k in ("coordinates", "geometries", "features", "geometry"):
                if k in c and c[k] is not None:
                    walk(c[k])
        elif isinstance(c, list) and c and isinstance(c[0], (int, float)):
            out.append([float(c[0]), float(c[1])])
        elif isinstance(c, list):
            for v in c:
                walk(v)

    walk(obj)
    return out


def _svg_circles(svg: str) -> np.ndarray:
    m = re.search(r'<g id="qg-points"[^>]*>(.*?)</g>', svg, re.S)
    if not m:
        return np.zeros((0, 2))
    return np.array(re.findall(r'<circle cx="([-0-9.e]+)" cy="([-0-9.e]+)"', m.group(1)),
                    dtype=float).reshape(-1, 2)


def _svg_grid_labels(svg: str) -> list[str]:
    m = re.search(r'<g id="qg-grid-labels"[^>]*>(.*?)</g>', svg, re.S)
    return [html.unescape(t) for t in re.findall(r">([^<]+)</text>", m.group(1))] if m else []


def _linear_fit(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """Least squares y = a + b x; returns a, b and the largest residual."""
    b, a = np.polyfit(x, y, 1)
    return float(a), float(b), float(np.abs(a + b * x - y).max())


def _bbox_polygon(bbox: list[float]) -> dict[str, Any]:
    w, s, e, n = bbox
    if e < w:
        e += 360.0
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def _fiji_tonga(rng: np.random.Generator, n_scatter: int) -> tuple[list[dict[str, Any]], np.ndarray, np.ndarray]:
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [lng, lat]},
              "properties": {"name": name, "territory": terr}} for name, terr, lat, lng in ANCHORS]
    idx = rng.integers(0, len(ANCHORS), n_scatter)
    for k in idx.tolist():
        _, terr, lat, lng = ANCHORS[k]
        x = float(_wrap(lng + rng.normal(0, 0.15)))
        y = float(lat + rng.normal(0, 0.15))
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [x, y]},
                      "properties": {"territory": terr}})
    xs = np.array([f["geometry"]["coordinates"][0] for f in feats])
    ys = np.array([f["geometry"]["coordinates"][1] for f in feats])
    return feats, xs, ys


def run_antimeridian(seed: int, sz: dict[str, int]) -> tuple[dict[str, Any], dict[str, Any]]:
    rng = _rng(seed, 5)
    out: dict[str, Any] = {}
    feats, xs, ys = _fiji_tonga(rng, sz["am_scatter"])
    pts = GeoEngine(verbose=False).load_data(feats)
    n = len(feats)

    # bbox
    q_bbox = pts.bbox()
    west_ref, width_ref = _min_arc(xs)
    q_width = (q_bbox[2] - q_bbox[0]) % 360.0
    naive = [float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())]
    out["bbox"] = {
        "qoregeo_bbox": q_bbox, "qoregeo_width_deg": q_width,
        "brute_force_west": west_ref, "brute_force_width_deg": width_ref,
        "qoregeo_covers_all_points": bool(_covered(xs, q_bbox[0], q_bbox[2]).all()),
        "qoregeo_width_minus_brute_force_deg": q_width - width_ref,
        "naive_min_max_bbox": naive, "naive_width_deg": naive[2] - naive[0],
        "bounds": pts.bounds(),
    }

    # centroid
    q_c = pts.centroid()
    v_c = _vector_mean(ys, xs)
    unwrapped = np.where(xs < west_ref, xs + 360.0, xs)
    u_c = (float(ys.mean()), float(_wrap(unwrapped.mean())))
    n_c = (float(ys.mean()), float(xs.mean()))

    def dist_km(a: tuple[float, float], b: tuple[float, float]) -> float:
        return W.Inverse(a[0], a[1], b[0], b[1])["s12"] / 1000.0

    out["centroid"] = {
        "qoregeo": list(q_c), "numpy_vector_mean": list(v_c), "unwrapped_arithmetic_mean": list(u_c),
        "naive_arithmetic_mean": list(n_c),
        "qoregeo_to_vector_mean_m": dist_km(q_c, v_c) * 1000.0,
        "qoregeo_to_unwrapped_mean_km": dist_km(q_c, u_c),
        "naive_to_vector_mean_km": dist_km(n_c, v_c),
    }

    # filter_by_bbox with west > east
    boxes = {"Fiji main islands and Lau": (-19.5, 177.0, -16.0, -178.5),
             "Lau and Tonga": (-22.0, 179.0, -15.0, -173.0)}
    fb: dict[str, Any] = {}
    for name, (s, w, nn, e) in boxes.items():
        truth = (ys >= s) & (ys <= nn) & _covered(xs, w, e)
        kept = {tuple(f["geometry"]["coordinates"]) for f in pts.filter_by_bbox(s, w, nn, e).get_features()}
        q_mask = np.array([(x, y) in kept for x, y in zip(xs.tolist(), ys.tolist(), strict=True)])
        cont = {tuple(f["geometry"]["coordinates"]) for f in pts.filter_by_bbox(s, w, nn, e + 360.0).get_features()}
        naive_mask = (ys >= s) & (ys <= nn) & (xs >= w) & (xs <= e)
        swapped = (ys >= s) & (ys <= nn) & (xs >= min(w, e)) & (xs <= max(w, e))
        fb[name] = {
            "box_south_west_north_east": [s, w, nn, e], "brute_force_count": int(truth.sum()),
            "qoregeo_count": int(q_mask.sum()), "qoregeo_matches_brute_force": bool((q_mask == truth).all()),
            "continuous_east_plus_360_same_result": cont == kept,
            "naive_w_le_lng_le_e_count": int(naive_mask.sum()),
            "naive_swapped_count": int(swapped.sum()),
            "naive_swapped_true_positives": int((swapped & truth).sum()),
        }
    fb["filter_by_bbox(**bounds())"] = {"count": pts.filter_by_bbox(**pts.bounds()).count(), "of": n}
    out["filter_by_bbox"] = fb

    # Framing: map, heatmap and image, for the dataset with a route and a split zone.
    all_feats = feats + [{"type": "Feature", "geometry": ROUTE, "properties": {"name": "ferry route"}},
                         {"type": "Feature", "geometry": ZONE, "properties": {"name": "Lau zone"}}]
    full = GeoEngine(verbose=False).load_data(all_feats)
    full_bbox = full.bbox()
    all_x = np.array([p[0] for f in all_feats for p in _positions(f["geometry"])])
    fw, fwidth = _min_arc(all_x)
    bbox_poly = _bbox_polygon(full_bbox)
    cfg = _page_config(full.map_html(tiles="none", overlays=[bbox_poly]))
    map_x = np.array([p[0] for p in _positions(cfg["data"]) + _positions(cfg["overlays"])])
    hcfg = _page_config(pts.heatmap_html(tiles="none"))
    hb = hcfg["bounds"]
    width, height = 800, 560
    svg = full.to_svg(width=width, height=height, overlays=[bbox_poly], labels="name")
    circ = _svg_circles(svg)
    x_unw = np.where(xs < full_bbox[0], xs + 360.0, xs)   # point order is feature order
    a, b, res_unw = _linear_fit(x_unw, circ[:, 0])
    _, _, res_raw = _linear_fit(xs, circ[:, 0])
    vis = (float(_wrap((0 - a) / b)), float(_wrap((width - a) / b)))
    i_w = [k for k, f in enumerate(feats) if f["properties"].get("name") == "Waiyevo"][0]
    i_l = [k for k, f in enumerate(feats) if f["properties"].get("name") == "Labasa"][0]
    pad = 24 + 4
    naive_px = (width - 2 * pad) * abs(xs[i_w] - xs[i_l]) / (xs.max() - xs.min())
    out["framing"] = {
        "full_dataset_bbox": full_bbox, "full_dataset_brute_force_width_deg": fwidth,
        "full_dataset_brute_force_west": fw,
        "map_html_data_lng_range": [float(map_x.min()), float(map_x.max())],
        "map_html_width_deg": float(map_x.max() - map_x.min()),
        "map_world_copy_jump": cfg.get("worldCopyJump"),
        "heatmap_html_bounds": hb, "heatmap_width_deg": hb[1][1] - hb[0][1],
        "naive_min_max_lng_range": [float(all_x.min()), float(all_x.max())],
        "image_size_px": [width, height],
        "image_markers_inside_canvas": bool(((circ[:, 0] >= 0) & (circ[:, 0] <= width)
                                             & (circ[:, 1] >= 0) & (circ[:, 1] <= height)).all()),
        "image_cx_linear_in_unwrapped_lng_max_residual_px": res_unw,
        "image_cx_linear_in_raw_lng_max_residual_px": res_raw,
        "image_visible_lng_range": vis,
        "image_grid_labels": _svg_grid_labels(svg),
        "waiyevo_to_labasa_km": dist_km((ys[i_w], xs[i_w]), (ys[i_l], xs[i_l])),
        "waiyevo_to_labasa_px_qoregeo": float(abs(circ[i_w, 0] - circ[i_l, 0])),
        "waiyevo_to_labasa_px_naive_min_max": float(naive_px),
    }

    # README claims (the Fiji example in the Data access section).
    fiji = GeoEngine(verbose=False).add_point(-18.14, 178.44).add_point(-16.80, -179.95)
    zone = {"type": "Polygon", "coordinates": [[[177.0, -20.0], [182.0, -20.0], [182.0, -16.0],
                                                [177.0, -16.0], [177.0, -20.0]]]}
    geo = GeoEngine(verbose=False)
    out["readme_claims"] = [
        {"claim": "fiji.bounds()", "readme": {"min_lat": -18.14, "max_lat": -16.8, "min_lng": -179.95, "max_lng": 178.44},
         "actual": fiji.bounds()},
        {"claim": "fiji.bbox()", "readme": [178.44, -18.14, -179.95, -16.8], "actual": fiji.bbox()},
        {"claim": "fiji.centroid()", "readme": [-17.471619, 179.247963], "actual": list(fiji.centroid())},
        {"claim": "point_in_polygon((-18.0, 179.5), fiji zone)", "readme": True,
         "actual": geo.point_in_polygon((-18.0, 179.5), zone)},
        {"claim": "point_in_polygon((-18.0, -179.5), fiji zone)", "readme": True,
         "actual": geo.point_in_polygon((-18.0, -179.5), zone)},
    ]
    for c in out["readme_claims"]:
        c["matches"] = c["readme"] == c["actual"]

    out["linestring_across_180"] = _linestring_across_180(geo)
    out["random_clusters"] = run_random_clusters(rng, sz)
    fig_data = {"feats": feats, "xs": xs, "ys": ys, "bbox": q_bbox, "naive": naive,
                "all_feats": all_feats, "full_bbox": full_bbox, "bbox_poly": bbox_poly}
    return out, fig_data


def _linestring_across_180(geo: GeoEngine) -> dict[str, Any]:
    """How each function reads one LineString segment stored with normalized longitudes."""
    seg = {"type": "LineString", "coordinates": ROUTE["coordinates"][:2]}
    (x0, y0), (x1, y1) = seg["coordinates"]
    y_mid = y0 + (0.0 - x0) / (x1 - x0) * (y1 - y0)   # where the planar (long-way) edge meets lng 0
    probe = (GeoEngine(verbose=False)
             .add_point(y_mid, 0.0, name="lng 0, on the long-way edge")
             .add_point(-17.7, 179.9, name="lng 179.9, 1 km from the short-way segment"))

    def names(e: GeoEngine) -> list[str]:
        return [f["properties"]["name"] for f in e.get_features()]

    cfg = _page_config(GeoEngine(verbose=False).load_data([seg]).map_html(tiles="none"))
    mx = [p[0] for p in _positions(cfg["data"])]
    return {
        "segment": seg,
        "geodesic_length_km": W.Inverse(y0, x0, y1, x1)["s12"] / 1000.0,
        "length_km": geo.length(seg),
        "point_lng0_on_long_way_edge": [y_mid, 0.0],
        "intersects_point_lng0": geo.intersects(seg, {"type": "Point", "coordinates": [0.0, y_mid]}),
        "intersects_buffer_20km_on_the_dateline_crossing": geo.intersects(seg, geo.buffer((-17.7, 180.0), 20)),
        "distance_to_line_km_from_point_lng0": geo.distance_to_line((y_mid, 0.0), seg),
        "distance_to_line_km_from_buffer_centre": geo.distance_to_line((-17.7, 180.0), seg),
        "filter_intersects_keeps": names(probe.filter_intersects(seg)),
        "filter_by_line_50km_keeps": names(probe.filter_by_line(seg, 50)),
        "map_html_lng_range": [min(mx), max(mx)],
        "notes": ["The README documents the planar long-way reading for polygon rings that jump "
                  "across 180 deg (point_in_polygon) and says intersects() uses the same planar "
                  "edges. length(), distance_to_line(), filter_by_line(), map() and image() read "
                  "the same segment the short way."],
    }


def run_random_clusters(rng: np.random.Generator, sz: dict[str, int]) -> dict[str, Any]:
    n_sets, n_pts = sz["am_random_sets"], sz["am_points_per_set"]
    c = Counter()
    naive_err_km: list[float] = []
    render_rows = Counter()
    worst: dict[str, float] = {"bbox_width_diff_deg": 0.0, "centroid_diff_m": 0.0}
    for k in range(n_sets):
        cx = rng.uniform(150, 210)
        width = 10.0 ** rng.uniform(-2, math.log10(300))
        cy, hh = rng.uniform(-60, 60), rng.uniform(0.01, 20)
        xs = _wrap(cx + width * (rng.uniform(0, 1, n_pts) - 0.5))
        ys = np.clip(cy + hh * (rng.uniform(0, 1, n_pts) - 0.5), -85, 85)
        if rng.random() < 0.2:
            xs[: 2] = [180.0, -180.0]
        eng = _points_engine(xs, ys)
        west, arc = _min_arc(xs)
        crosses = bool(np.ptp(xs) - arc > 1e-9)   # the naive min/max box is wider than the tightest one
        c["sets"] += 1
        c["sets_where_naive_min_max_is_wider"] += int(crosses)
        qb = eng.bbox()
        qw = (qb[2] - qb[0]) % 360.0
        worst["bbox_width_diff_deg"] = max(worst["bbox_width_diff_deg"], abs(qw - arc))
        c["bbox_width_equals_brute_force"] += int(abs(qw - arc) <= 1e-9)
        c["bbox_covers_all_points"] += int(_covered(xs, qb[0], qb[2]).all())
        qc = eng.centroid()
        vc = _vector_mean(ys, xs)
        dm = W.Inverse(qc[0], qc[1], vc[0], vc[1])["s12"]
        worst["centroid_diff_m"] = max(worst["centroid_diff_m"], dm)
        c["centroid_within_0.2_m_of_vector_mean"] += int(dm <= 0.2)
        if crosses:
            naive_err_km.append(W.Inverse(ys.mean(), xs.mean(), vc[0], vc[1])["s12"] / 1000.0)
        # A random box crossing 180 deg.
        w, e = rng.uniform(170, 180), rng.uniform(-180, -170)
        s0, n0 = sorted(rng.uniform(cy - hh, cy + hh, 2))
        truth = (ys >= s0) & (ys <= n0) & _covered(xs, w, e)
        got = _kept(eng.filter_by_bbox(s0, w, n0, e), n_pts)
        got2 = _kept(eng.filter_by_bbox(s0, w, n0, e + 360.0), n_pts)
        c["filter_by_bbox_matches_brute_force"] += int((got == truth).all())
        c["filter_by_bbox_continuous_form_matches"] += int((got2 == truth).all())
        c["filter_by_bbox_points_selected"] += int(truth.sum())
        if k < sz["am_render_sets"]:
            render_rows["rendered"] += 1
            unwrap = arc < 180.0
            svg = GeoEngine(verbose=False).load_data(
                [{"type": "Point", "coordinates": [float(x), float(y)]}
                 for x, y in zip(xs, ys, strict=True)]).to_svg(width=400, height=300)
            circ = _svg_circles(svg)
            inside = ((circ[:, 0] >= 0) & (circ[:, 0] <= 400) & (circ[:, 1] >= 0) & (circ[:, 1] <= 300)).all()
            render_rows["image_all_markers_inside_canvas"] += int(inside)
            xw = _wrap(xs)
            xu = np.where(xw < west, xw + 360.0, xw) if unwrap else xs
            _, slope, resid = _linear_fit(xu, circ[:, 0]) if np.ptp(xu) > 0 else (0.0, 0.0, 0.0)
            render_rows["image_x_linear_in_expected_lng_within_0.05_px"] += int(resid <= 0.05)
            cfg = _page_config(eng.map_html(tiles="none"))
            mx = np.array([p[0] for p in _positions(cfg["data"])])
            expect = arc if unwrap else float(np.ptp(xs))
            render_rows["map_lng_range_equals_expected"] += int(abs((mx.max() - mx.min()) - expect) <= 1e-9)
            render_rows["sets_with_tightest_arc_under_180_deg"] += int(unwrap)
            render_rows["sets_drawn_across_180"] += int(unwrap and crosses)
    return {"counts": dict(c), "worst": worst,
            "naive_centroid_error_km_where_min_max_is_wider": {
                "median": float(np.median(naive_err_km)) if naive_err_km else None,
                "max": float(np.max(naive_err_km)) if naive_err_km else None,
                "min": float(np.min(naive_err_km)) if naive_err_km else None},
            "rendering": dict(render_rows),
            "notes": ["Each set: points uniform in a longitude window centred between 150 and "
                      "210 deg, 0.01 to 300 deg wide; 20 % of the sets also get points at "
                      "exactly lng 180 and -180. Expected framing: the narrowest covering "
                      "longitude arc when it is under 180 deg wide (lng 180 and -180 are one "
                      "meridian), else the raw min/max (a world map, the documented rule)."]}


# 5. Poles, nulls, empty collections and out-of-range coordinates

def run_edge_cases(seed: int, sz: dict[str, int]) -> dict[str, Any]:
    rng = _rng(seed, 6)
    geo = GeoEngine(verbose=False)
    cases: list[dict[str, Any]] = []
    pt = {"type": "Point", "coordinates": [0.0, 0.0]}
    square = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}

    def case(group: str, name: str, call: str, documented: str, fn: Callable[[], Any],
             expect: Callable[[dict[str, Any]], bool] | None, note: str = "") -> dict[str, Any]:
        """Record one call. ``expect=None`` marks a case that is reported, not judged."""
        o = _outcome(fn)
        row = {"group": group, "case": name, "call": call, "documented": documented,
               "raised": o["raised"], "value": None if o["raised"] else _f(_jsonable(o["value"])),
               "warnings": [w[:200] for w in o["warnings"]],
               "pass": None if expect is None else bool(expect(o)), "note": note}
        if o["raised"]:
            row["exception_bases"] = o["bases"]
            row["message_first_lines"] = [ln for ln in o["message"].splitlines() if ln.strip()][:3]
        cases.append(row)
        return o

    def raises(name: str) -> Callable[[dict[str, Any]], bool]:
        return lambda o: o["raised"] == name

    def value(v: Any) -> Callable[[dict[str, Any]], bool]:
        return lambda o: o["raised"] is None and o["value"] == v

    # Poles
    case("poles", "two spellings of the North Pole", "geo.distance((90, 0), (90, 123), unit='m')",
         "points at a pole with different longitudes are the same place",
         lambda: geo.distance((90, 0), (90, 123), unit="m"), value(0.0))
    ref = W.Inverse(90, 0, 89, 0)["s12"]
    case("poles", "haversine from the pole vs geographiclib", "geo.distance((90, 0), (89, 0), unit='m')",
         "haversine is within about 0.5 % of the true distance",
         lambda: geo.distance((90, 0), (89, 0), unit="m"),
         lambda o: o["raised"] is None and abs(o["value"] / ref - 1) < 0.005,
         note=f"geographiclib: {ref:.1f} m")

    n = sz["pole_points"]
    s = 100e3 * (1 + rng.uniform(-0.01, 0.01, n))
    az = rng.uniform(-180, 180, n)
    plat, plng = np.empty(n), np.empty(n)
    for i in range(n):
        d = W.Direct(90.0, 0.0, float(az[i]), float(s[i]))
        plat[i], plng[i] = d["lat2"], _wrap(d["lon2"])
    zone = geo.buffer((90.0, 0.0), 100)
    q = np.array([geo.point_in_polygon((float(a), float(b)), zone) for a, b in zip(plat, plng, strict=True)])
    truth = s <= 100e3
    hav = _np_haversine_km(90.0, 0.0, plat, plng) <= 100.0
    pole_buffer = {"n": n, "mismatch_vs_geographiclib": int((q != truth).sum()),
                   "mismatch_vs_numpy_haversine": int((q != hav).sum()),
                   "included_but_geodesically_outside": int((q & ~truth).sum()),
                   "max_geodesic_m_included": float(s[q].max()) if q.any() else None}
    case("poles", "buffer at the North Pole vs exact circle", "geo.point_in_polygon(p, geo.buffer((90, 0), 100))",
         "buffers are tested as exact circles (haversine), also near the poles",
         lambda: pole_buffer, lambda o: o["value"]["mismatch_vs_numpy_haversine"] == 0,
         note="points at geodesic 99 to 101 km from the pole; mismatches vs geographiclib come "
              "from the sphere model (meridian radius of curvature at the pole is 6399.6 km)")
    eng = _points_engine(plng, plat)
    for method in ("haversine", "vincenty"):
        kept = _kept(eng.filter_by_radius(90.0, 0.0, 100, method=method), n)
        case("poles", f"filter_by_radius at the North Pole, {method}",
             f"geo.filter_by_radius(90, 0, 100, method='{method}')",
             "vincenty is accurate to about 0.5 mm",
             lambda kept=kept: {"mismatch_vs_geographiclib": int((kept != truth).sum()), "n": n},
             None if method == "haversine" else (lambda o: o["value"]["mismatch_vs_geographiclib"] == 0))
    ring = GeoEngine(verbose=False)
    for k in range(8):
        ring.add_point(89.0, -180.0 + 45.0 * k)
    case("poles", "centroid of 8 points on the 89 deg N parallel", "ring.centroid()",
         "spherical mean of the locations (naive mean: lat 89)", ring.centroid,
         lambda o: o["raised"] is None and o["value"][0] > 89.99)
    case("poles", "bbox of 8 points around the pole", "ring.bbox()",
         "tightest longitude span; here 315 deg (largest gap 45 deg)", ring.bbox,
         lambda o: o["raised"] is None and abs(((o["value"][2] - o["value"][0]) % 360) - 315) < 1e-9)
    north_cap = {"type": "Polygon", "coordinates": [[[-180, 80], [180, 80], [180, 90], [-180, 90], [-180, 80]]]}
    south_cap = {"type": "Polygon", "coordinates": [[[-180, -90], [180, -90], [180, -80], [-180, -80], [-180, -90]]]}
    case("poles", "North Pole in a polygon covering lat >= 80", "geo.point_in_polygon((90, 0), north_cap)",
         "planar lng/lat edges: the pole lies on the top edge (boundary); the boundary rule is not documented",
         lambda: geo.point_in_polygon((90, 0), north_cap), None,
         note="compare with the South Pole case")
    case("poles", "South Pole in a polygon covering lat <= -80", "geo.point_in_polygon((-90, 0), south_cap)",
         "same as above, mirrored", lambda: geo.point_in_polygon((-90, 0), south_cap), None)
    case("poles", "lng 180 vs -180 inside a polygon spanning -180..180",
         "[geo.point_in_polygon((85, 180), north_cap), geo.point_in_polygon((85, -180), north_cap)]",
         "the same physical point; planar reading puts both on the polygon's side edges",
         lambda: [geo.point_in_polygon((85, 180), north_cap), geo.point_in_polygon((85, -180), north_cap)],
         None)
    case("poles", "buffers near the pole meet", "geo.intersects(geo.buffer((90, 0), 10), geo.buffer((89.95, 77), 1))",
         "buffers are exact circles on the sphere, also around the poles",
         lambda: geo.intersects(geo.buffer((90, 0), 10), geo.buffer((89.95, 77), 1)), value(True),
         note="centres 5.6 km apart, radii 10 + 1 km")
    pole_img = GeoEngine(verbose=False).add_point(90, 0).add_point(89.9, 45).add_point(80, -100)

    def pole_svg() -> dict[str, Any]:
        circ = _svg_circles(pole_img.to_svg(width=400, height=300))
        return {"markers": int(circ.shape[0]),
                "inside_canvas": bool(((circ >= 0) & (circ <= [400, 300])).all())}

    case("poles", "image of points at the pole", "GeoEngine().add_point(90, 0)...to_svg()",
         "Web Mercator with latitude clamped to +-85.0511 deg", pole_svg,
         lambda o: o["raised"] is None and o["value"]["inside_canvas"])

    # Null geometries
    nullf = [{"type": "Feature", "geometry": None, "properties": {"a": 1}},
             {"type": "Feature", "geometry": {"type": "Point", "coordinates": [10.0, 20.0]}, "properties": {"a": 2}}]
    mixed = GeoEngine(verbose=False).load_data(nullf)
    nulldoc = '"geometry": null features are kept (with their properties) but have no location, so spatial operations skip them'
    case("null", "null geometry is kept", "GeoEngine().load_data(nullf).count()", nulldoc,
         mixed.count, value(2))
    case("null", "bbox skips null geometry", "mixed.bbox()", nulldoc, mixed.bbox, value([10.0, 20.0, 10.0, 20.0]))
    case("null", "centroid skips null geometry", "mixed.centroid()", nulldoc, mixed.centroid, value((20.0, 10.0)))
    case("null", "filter_by_radius skips null geometry", "mixed.filter_by_radius(20, 10, 5).count()", nulldoc,
         lambda: mixed.filter_by_radius(20, 10, 5).count(), value(1))
    case("null", "filter_intersects skips null geometry", "mixed.filter_intersects(zone).count()", nulldoc,
         lambda: mixed.filter_intersects(geo.buffer((20, 10), 5)).count(), value(1))
    case("null", "nearest skips null geometry", "mixed.nearest((0, 0))['feature']['properties']['a']",
         nulldoc, lambda: mixed.nearest((0, 0))["feature"]["properties"]["a"], value(2))
    case("null", "map and image skip null geometry", "mixed.map_html(tiles='none'); mixed.to_svg()",
         "a None geometry is skipped",
         lambda: (len(mixed.map_html(tiles="none")) > 0, _svg_circles(mixed.to_svg()).shape[0]),
         value((True, 1)))
    allnull = GeoEngine(verbose=False).load_data(nullf[:1])
    case("null", "bbox of an all-null dataset", "allnull.bbox()",
         "NoDataError: nothing is loaded, or the dataset is empty", allnull.bbox, raises("NoDataError"))
    case("null", "point_in_polygon with polygon None", "geo.point_in_polygon((0, 0), None)",
         "InvalidBufferError: a polygon argument is missing, malformed or not a polygon",
         lambda: geo.point_in_polygon((0, 0), None), raises("InvalidBufferError"))
    case("null", "intersects with None", "geo.intersects(None, pt)",
         "InvalidArgumentError: any other bad argument", lambda: geo.intersects(None, pt),
         raises("InvalidArgumentError"))
    case("null", "intersects with a null-geometry Feature", "geo.intersects({'type': 'Feature', 'geometry': None}, pt)",
         "shapely: an empty geometry intersects nothing",
         lambda: geo.intersects({"type": "Feature", "geometry": None, "properties": {}}, pt), value(False))

    # Empty collections
    empty_doc = "EmptyDatasetError: a file or object contains no usable features"
    case("empty", "load_data([])", "GeoEngine().load_data([])", empty_doc,
         lambda: GeoEngine(verbose=False).load_data([]), raises("EmptyDatasetError"))
    case("empty", "load_data(empty FeatureCollection)", "GeoEngine().load_data({'type': 'FeatureCollection', 'features': []})",
         empty_doc, lambda: GeoEngine(verbose=False).load_data({"type": "FeatureCollection", "features": []}),
         raises("EmptyDatasetError"))
    empties = [{"type": "LineString", "coordinates": []}, {"type": "Polygon", "coordinates": []},
               {"type": "MultiPoint", "coordinates": []}, {"type": "GeometryCollection", "geometries": []}]
    e_eng = _outcome(lambda: GeoEngine(verbose=False).load_data(empties))
    case("empty", "empty geometries load", "GeoEngine().load_data(empties).count()",
         "RFC 7946 allows empty coordinate arrays", lambda: e_eng["value"].count(), value(4))
    case("empty", "bbox of empty geometries", "eng.bbox()", "NoDataError: the dataset is empty",
         lambda: e_eng["value"].bbox(), raises("NoDataError"))
    for g in empties[:2] + [{"type": "MultiPolygon", "coordinates": []}, empties[3],
                            {"type": "FeatureCollection", "features": []}]:
        case("empty", f"intersects with empty {g['type']}", f"geo.intersects({json.dumps(g)}, pt)",
             "shapely: an empty geometry intersects nothing", lambda g=g: geo.intersects(g, pt), value(False))
    case("empty", "point_in_polygon with empty Polygon", "geo.point_in_polygon((0, 0), {'type': 'Polygon', 'coordinates': []})",
         "InvalidBufferError for a malformed polygon (shapely: Polygon() contains nothing)",
         lambda: geo.point_in_polygon((0, 0), {"type": "Polygon", "coordinates": []}),
         lambda o: o["raised"] is not None or o["value"] is False)
    case("empty", "point_in_polygon with empty MultiPolygon", "geo.point_in_polygon((0, 0), {'type': 'MultiPolygon', 'coordinates': []})",
         "same as the empty Polygon",
         lambda: geo.point_in_polygon((0, 0), {"type": "MultiPolygon", "coordinates": []}),
         lambda o: o["raised"] is not None or o["value"] is False,
         note="compare with the empty Polygon case: one raises, the other returns False")
    case("empty", "area of empty MultiPolygon", "geo.area({'type': 'MultiPolygon', 'coordinates': []})",
         "area of nothing", lambda: geo.area({"type": "MultiPolygon", "coordinates": []}),
         lambda o: o["raised"] is not None or o["value"] == 0.0)
    case("empty", "area of empty Polygon", "geo.area({'type': 'Polygon', 'coordinates': []})",
         "area of nothing", lambda: geo.area({"type": "Polygon", "coordinates": []}),
         lambda o: o["raised"] is not None or o["value"] == 0.0)
    nothing = GeoEngine(verbose=False).add_point(0, 0).filter_by_radius(50, 50, 1)
    case("empty", "bbox after a filter matched nothing", "geo.filter_by_radius(50, 50, 1).bbox()",
             "NoDataError with empty=True", nothing.bbox,
             lambda o: o["raised"] == "NoDataError" and getattr(o["exc"], "empty", None) is True)
    case("empty", "image of an empty result", "geo.filter_by_radius(50, 50, 1).to_svg()",
         "an empty map gets no annotations", lambda: len(nothing.to_svg()) > 0, None)

    # Out of range
    coord_doc = "InvalidCoordinateError: a lat/lng is missing, non-numeric, NaN or out of range"
    for name, call, fn in (
            ("latitude 91", "geo.distance((91, 0), (0, 0))", lambda: geo.distance((91, 0), (0, 0))),
            ("longitude 181", "geo.distance((0, 181), (0, 0))", lambda: geo.distance((0, 181), (0, 0))),
            ("NaN latitude", "geo.distance((nan, 0), (0, 0))", lambda: geo.distance((float("nan"), 0), (0, 0))),
            ("infinite longitude", "geo.distance((0, inf), (0, 0))", lambda: geo.distance((0, float("inf")), (0, 0))),
            ("boolean coordinate", "geo.distance((True, 0), (0, 0))", lambda: geo.distance((True, 0), (0, 0))),
            ("Sydney as (lng, lat)", "geo.distance((151.2093, -33.8688), (0, 0))",
             lambda: geo.distance((151.2093, -33.8688), (0, 0))),
            ("add_point lng 181", "GeoEngine().add_point(0, 181)", lambda: GeoEngine(verbose=False).add_point(0, 181)),
            ("filter_by_radius lat 100", "geo.filter_by_radius(100, 0, 5)",
             lambda: GeoEngine(verbose=False).add_point(0, 0).filter_by_radius(100, 0, 5)),
            ("filter_by_bbox lng 400", "geo.filter_by_bbox(-1, 0, 1, 400)",
             lambda: GeoEngine(verbose=False).add_point(0, 0).filter_by_bbox(-1, 0, 1, 400)),
            ("filter_by_bbox lat 95", "geo.filter_by_bbox(-1, 0, 95, 1)",
             lambda: GeoEngine(verbose=False).add_point(0, 0).filter_by_bbox(-1, 0, 95, 1)),
            ("point_in_polygon point lat 95", "geo.point_in_polygon((95, 0), square)",
             lambda: geo.point_in_polygon((95, 0), square)),
            ("buffer centre lat 91", "geo.buffer((91, 0), 10)", lambda: geo.buffer((91, 0), 10))):
        case("range", name, call, coord_doc, fn, raises("InvalidCoordinateError"))
    case("range", "lng 180 and -180 are the same meridian", "geo.distance((0, 180), (0, -180))",
         "longitude -180 to 180 inclusive", lambda: geo.distance((0, 180), (0, -180)), value(0.0))
    case("range", "Point lng 200 in load_data", "GeoEngine().load_data([Point [200, 10], Point [10, 10]]).count()",
         "out-of-range Points are skipped with one warning",
         lambda: GeoEngine(verbose=False).load_data([{"type": "Point", "coordinates": [200, 10]},
                                                     {"type": "Point", "coordinates": [10, 10]}]).count(),
         lambda o: o["raised"] is None and o["value"] == 1 and len(o["warnings"]) == 1)
    case("range", "only Point lat 95 in load_data", "GeoEngine().load_data([Point [10, 95]])",
         "skipped with a warning; then no usable features",
         lambda: GeoEngine(verbose=False).load_data([{"type": "Point", "coordinates": [10, 95]}]),
         lambda o: o["raised"] == "EmptyDatasetError" and len(o["warnings"]) == 1)

    def csv_load() -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "pts.csv")
            with open(p, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerows([["name", "lat", "lng"], ["ok", "28.6", "77.2"], ["lat95", "95", "77.2"],
                             ["text", "abc", "77.2"], ["blank", "", ""]])
            return {"count": GeoEngine(verbose=False).load(p).count()}

    case("range", "CSV rows out of range or non-numeric", "GeoEngine().load('pts.csv')",
         "blank coordinates skipped silently; non-numeric or out-of-range rows skipped with one summary warning",
         csv_load, lambda o: o["raised"] is None and o["value"]["count"] == 1 and len(o["warnings"]) == 1)
    bad_poly = {"type": "Polygon", "coordinates": [[[0, 0], [1, 95], [1, 0], [0, 0]]]}
    case("range", "polygon position lat 95 in point_in_polygon", "geo.point_in_polygon((0, 0), bad_poly)",
         "InvalidBufferError: malformed polygon", lambda: geo.point_in_polygon((0, 0), bad_poly),
         raises("InvalidBufferError"))
    case("range", "polygon position lat 95 in intersects", "geo.intersects(bad_poly, pt)",
         "InvalidArgumentError", lambda: geo.intersects(bad_poly, pt), raises("InvalidArgumentError"))

    syd = _outcome(lambda: geo.distance((151.2093, -33.8688), (0, 0)))
    quoted = syd["message"] if syd["raised"] else None
    summary = Counter((c["group"], {True: "pass", False: "fail", None: "reported"}[c["pass"]])
                      for c in cases)
    return {"cases": cases, "quoted_message": {"call": "geo.distance((151.2093, -33.8688), (0, 0))",
                                               "exception": syd["raised"], "message": quoted},
            "pole_buffer": pole_buffer,
            "summary": {f"{g}: {p}": k for (g, p), k in sorted(summary.items())}}


def _jsonable(v: Any) -> Any:
    if isinstance(v, GeoEngine):
        return repr(v)
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return repr(v)


# Figures

def _style() -> None:
    plt.rcParams.update({
        "svg.hashsalt": "qoregeo-experiment-04",
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


def fig_antimeridian_image(fd: dict[str, Any]) -> str:
    """The required figure: rendered by qoregeo itself, with its bbox() as an overlay."""
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    path = FIG_DIR / f"{NAME}-antimeridian.svg"
    GeoEngine(verbose=False).load_data(fd["all_feats"]).image(
        str(path), width=800, height=560, overlays=[fd["bbox_poly"]], labels="name")
    return str(path.relative_to(REPO))


def fig_antimeridian_naive(fd: dict[str, Any]) -> str:
    xs, ys, bbox, naive = fd["xs"], fd["ys"], fd["bbox"], fd["naive"]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(8, 3.5), gridspec_kw={"width_ratios": [1.35, 1]})
    a1.scatter(xs, ys, s=4, color=C_A, linewidths=0, rasterized=True)
    a1.add_patch(Rectangle((naive[0], naive[1]), naive[2] - naive[0], naive[3] - naive[1],
                           fill=False, edgecolor=C_B, linewidth=1.4))
    a1.set_xlim(-185, 185)
    a1.set_xticks(range(-180, 181, 60))
    a1.set_ylim(-24, -10)
    a1.set_xlabel("Longitude as stored (deg)")
    a1.set_ylabel("Latitude (deg)")
    a1.text(0, -11.2, f"naive min/max box: {naive[2] - naive[0]:.1f} deg wide", ha="center",
            fontsize=8, color=C_B)
    w, e = bbox[0], bbox[2] + 360.0
    xu = np.where(xs < w, xs + 360.0, xs)
    a2.scatter(xu, ys, s=4, color=C_A, linewidths=0, rasterized=True)
    a2.add_patch(Rectangle((w, bbox[1]), e - w, bbox[3] - bbox[1], fill=False, edgecolor=C_C, linewidth=1.4))
    a2.axvline(180, color=INK2, linewidth=0.8, linestyle=(0, (3, 2)))
    a2.set_xlim(w - 0.8, e + 0.8)
    a2.set_ylim(-24, -10)
    ticks = [176, 178, 180, 182, 184, 186]
    a2.set_xticks(ticks)
    a2.set_xticklabels([f"{t}" if t <= 180 else f"{t - 360}" for t in ticks])
    a2.set_xlabel("Longitude (deg), continuous across 180")
    a2.text((w + e) / 2, -11.2, f"qoregeo bbox(): {(bbox[2] - bbox[0]) % 360:.1f} deg wide",
            ha="center", fontsize=8, color=C_C)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    _note(fig)
    return _save(fig, "antimeridian-naive")


def fig_radius(radius: dict[str, Any]) -> str:
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(8, 3.6), gridspec_kw={"width_ratios": [1.4, 1]})
    colors = {"0": C_A, "30": C_B, "60": C_C, "85": C_D}
    for lat, cur in radius["curves_r100km"].items():
        a1.plot(cur["azimuth"], cur["rel_pct"], color=colors.get(lat, C_GREY), linewidth=1.3,
                label=f"centre at {lat} deg")
    a1.axhline(0, color=INK2, linewidth=0.6)
    a1.set_xlim(-180, 180)
    a1.set_xticks(range(-180, 181, 90))
    a1.set_xlabel("Azimuth from the centre (deg)")
    a1.set_ylabel("haversine / geodesic - 1 (%)")
    a1.legend(fontsize=7.5, loc="upper left", ncol=2)
    pooled = radius["near_edge_by_latitude"]
    lats = [float(k) for k in pooled]
    hv = [v["haversine_misclassified_pct"] for v in pooled.values()]
    vv = [v["vincenty_misclassified_pct"] for v in pooled.values()]
    xpos = np.arange(len(lats))
    a2.bar(xpos - 0.2, hv, width=0.38, color=C_A, label="haversine (default)")
    a2.bar(xpos + 0.2, vv, width=0.38, color=C_B, label="vincenty")
    for x, v in zip(xpos, vv, strict=True):
        a2.text(x + 0.2, max(hv) * 0.01, f"{v:.0f}", ha="center", va="bottom", fontsize=7, color=C_B)
    a2.set_xticks(xpos)
    a2.set_xticklabels([f"{v:g}" for v in lats])
    a2.set_xlabel("Latitude of the centre (deg)")
    a2.set_ylabel("misclassified, within +-1 % of radius (%)")
    a2.legend(fontsize=7.5, loc="upper right")
    a2.grid(axis="x", visible=False)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    _note(fig)
    return _save(fig, "radius-band")


def fig_tolerance(inter: dict[str, Any]) -> str:
    rows = [r for r in inter["near_rows"] if r["distance_deg"] > 0 and r["contact_edge_deg"]]
    fig, ax = plt.subplots(figsize=(8, 4.2))
    edge = np.array([r["contact_edge_deg"] for r in rows])
    dist = np.array([r["distance_deg"] for r in rows])
    q = np.array([r["qoregeo"] for r in rows])
    ref = np.array([r["shapely"] for r in rows])
    agree_f = ~q & ~ref
    fp = q & ~ref
    ax.scatter(edge[agree_f], dist[agree_f], s=6, color=C_GREY, alpha=0.5, linewidths=0, rasterized=True,
               label="qoregeo False (agrees with shapely)")
    ax.scatter(edge[fp], dist[fp], s=8, color=C_B, alpha=0.8, linewidths=0, rasterized=True,
               label="qoregeo True, shapely False")
    xx = np.logspace(-7, 1.2, 50)
    ax.plot(xx, np.full_like(xx, TOL), color=INK2, linewidth=1, linestyle=(0, (4, 2)),
            label="1e-12 deg")
    ax.plot(xx, TOL / np.minimum(xx, 1.0), color=C_A, linewidth=1.2, label="1e-12 / edge length (deg)")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Length of the edge at the contact (deg, log scale)")
    ax.set_ylabel("Gap between the shapes (deg, log scale)")
    ax.legend(fontsize=7.5, loc="upper right")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    _note(fig)
    return _save(fig, "intersects-tolerance")


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
    results: dict[str, Any] = {}
    timings: dict[str, float] = {}

    def section(name: str, fn: Callable[[], Any]) -> Any:
        print(f"[{mode}] {name}", flush=True)
        t0 = time.perf_counter()
        value = fn()
        timings[name] = time.perf_counter() - t0
        print(f"    {timings[name]:.1f} s", flush=True)
        return value

    results["point_in_polygon"] = section("point_in_polygon vs shapely", lambda: run_pip(args.seed, sz))
    results["point_in_polygon_antimeridian"] = section("point_in_polygon across 180 deg",
                                                       lambda: run_pip_wrap(args.seed, sz))
    results["intersects"] = section("intersects vs shapely", lambda: run_intersects(args.seed, sz))
    results["filter_by_radius"] = section("filter_by_radius vs geographiclib", lambda: run_radius(args.seed, sz))
    am, fig_data = section("antimeridian", lambda: run_antimeridian(args.seed, sz))
    results["antimeridian"] = am
    results["edge_cases"] = section("poles, nulls, empties, ranges", lambda: run_edge_cases(args.seed, sz))

    print(f"[{mode}] figures", flush=True)
    figures = [fig_antimeridian_image(fig_data), fig_antimeridian_naive(fig_data),
               fig_radius(results["filter_by_radius"]), fig_tolerance(results["intersects"])]
    # The per-pair rows were only needed for the figure.
    near_rows = results["intersects"].pop("near_rows")
    results["intersects"]["near_pairs"] = len(near_rows)

    meta = {
        "experiment": NAME,
        "mode": mode,
        "qoregeo_version": qoregeo.__version__,
        "python_version": sys.version,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "reference_libraries": {
            "shapely": shapely.__version__,
            "geos": shapely.geos_version_string,
            "geographiclib": _md.version("geographiclib"),
            "numpy": np.__version__,
            "matplotlib": matplotlib.__version__,
        },
        "seed": args.seed,
        "sizes": sz,
        "boundary_tolerance_deg": TOL,
        "utc_timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "command": shlex.join(["python", *sys.argv]),
        "python_executable": sys.executable,
        "section_seconds": timings,
        "runtime_seconds": None,
        "figures": figures,
        "notes": [
            "Timings are incidental to this experiment and only indicative (shared 4-CPU machine).",
        ],
    }
    meta["runtime_seconds"] = time.perf_counter() - t_start
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "results": _f(_jsonable_tree(results))}
    RESULTS_PATH.write_text(json.dumps(payload, indent=1, ensure_ascii=False, allow_nan=False) + "\n",
                            encoding="utf-8")
    print(f"wrote {RESULTS_PATH.relative_to(REPO)} in {meta['runtime_seconds']:.1f} s", flush=True)
    for f in figures:
        print(f"  {f} ({(REPO / f).stat().st_size / 1024:.1f} KB)")
    return 0


def _jsonable_tree(v: Any) -> Any:
    if isinstance(v, Counter):
        return dict(v)
    if isinstance(v, dict):
        return {str(k): _jsonable_tree(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable_tree(x) for x in v]
    if isinstance(v, np.ndarray):
        return v.tolist()
    return v


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
