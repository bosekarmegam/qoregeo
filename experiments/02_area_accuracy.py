#!/usr/bin/env python3
"""Experiment 02: area, buffer and length accuracy of qoregeo against WGS84.

qoregeo measures polygon areas on a sphere of mean Earth radius (Chamberlain
and Duquette, the method Turf.js uses) and measures a buffer() zone as the
exact spherical cap it stands for. length() sums haversine (sphere) or
Vincenty (WGS84) segment lengths. This script compares those numbers with
GeographicLib on the WGS84 ellipsoid.

How the comparison isolates the area formula: every shape is turned into one
list of densified vertices, and qoregeo and GeographicLib both receive that
same list. Lat/lng boxes are densified with equal longitude steps along the
parallels (and equal latitude steps along the meridians); circles are sampled
with GeographicLib Direct at equal azimuth steps. GeographicLib joins the
vertices with geodesics and qoregeo with its own edge rule, so each case is
computed at two densities (n and 2n) to show that the edge rule no longer
matters, and the GeographicLib box areas are checked against the closed-form
WGS84 area of a box bounded by parallels and meridians.

Run from the repository root:

    python experiments/02_area_accuracy.py           # full run (about one minute)
    python experiments/02_area_accuracy.py --quick   # reduced sizes, a few seconds

Writes experiments/results/area-accuracy.json and
docs/assets/experiments/area-accuracy-*.svg.
"""

from __future__ import annotations

import argparse
import datetime as _dt
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
import timeit
from pathlib import Path
from typing import Any, Callable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

import pyproj  # noqa: E402
from geographiclib.geodesic import Geodesic  # noqa: E402

import qoregeo  # noqa: E402
from qoregeo import GeoEngine  # noqa: E402
from qoregeo.utils import EARTH_RADIUS_M  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
NAME = "area-accuracy"
RESULTS_PATH = REPO / "experiments" / "results" / f"{NAME}.json"
FIG_DIR = REPO / "docs" / "assets" / "experiments"
SEED = 20261019

GEO = GeoEngine(verbose=False)
W = Geodesic.WGS84
GEOD = pyproj.Geod(ellps="WGS84")
DIRECT_MASK = Geodesic.STANDARD | Geodesic.LONG_UNROLL

A = W.a
F = W.f
E2 = F * (2 - F)
E = math.sqrt(E2)
B = A * (1 - F)
R = EARTH_RADIUS_M   # qoregeo's sphere: 6371008.8 m

SQ_MI_KM2 = 2.589988110336          # 1 international square mile in km2
COLORADO_CENSUS_SQ_MI = 104_094     # Census 2010 total area (land + water)
COLORADO_TASK_KM2 = 269_837         # figure given in the experiment brief

# Colors: the validated categorical order (blue, orange, aqua, yellow).
C_SIZE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
MARKERS = ["o", "s", "^", "D"]
C_GREY = "#8a8985"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e4e3df"
BAND = "#efeeea"


# Closed-form WGS84 and sphere quantities

def meridian_radius(lat: float) -> float:
    s = math.sin(math.radians(lat))
    return A * (1 - E2) / (1 - E2 * s * s) ** 1.5


def normal_radius(lat: float) -> float:
    s = math.sin(math.radians(lat))
    return A / math.sqrt(1 - E2 * s * s)


def small_area_error(lat: float) -> float:
    """Relative error of a sphere area for a vanishing shape at `lat`: R^2/(M N) - 1."""
    return R * R / (meridian_radius(lat) * normal_radius(lat)) - 1


def _zone(lat: float) -> float:
    """WGS84 area between the equator and `lat`, per radian of longitude (m2)."""
    s = math.sin(math.radians(lat))
    return B * B / 2 * (s / (1 - E2 * s * s) + math.atanh(E * s) / E)


def box_area_wgs84(south: float, north: float, dlng: float) -> float:
    """Exact WGS84 area of a box bounded by two parallels and two meridians."""
    return math.radians(dlng) * (_zone(north) - _zone(south))


def box_area_sphere(south: float, north: float, dlng: float) -> float:
    """Exact area of the same box on qoregeo's sphere."""
    return R * R * math.radians(dlng) * (math.sin(math.radians(north)) - math.sin(math.radians(south)))


# Shapes (rings are lists of (lat, lng), counter-clockwise, not closed)

def square_box(lat: float, side_km: float) -> tuple[list[tuple[float, float]], float, float, float]:
    """A lat/lng box centred on (lat, 0): side_km along the meridian and along the central parallel."""
    half = side_km * 500.0
    south = W.Direct(lat, 0.0, 180.0, half)["lat2"]
    north = W.Direct(lat, 0.0, 0.0, half)["lat2"]
    dlng = math.degrees(side_km * 1000.0 / (normal_radius(lat) * math.cos(math.radians(lat))))
    w, e = -dlng / 2, dlng / 2
    return [(south, w), (south, e), (north, e), (north, w)], south, north, dlng


def densify(ring: list[tuple[float, float]], n: int) -> list[tuple[float, float]]:
    """Split every edge into n equal steps in latitude and longitude."""
    out = []
    for i, a in enumerate(ring):
        b = ring[(i + 1) % len(ring)]
        for k in range(n):
            t = k / n
            out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
    return out


def geodesic_circle(lat: float, lng: float, r_m: float, n: int) -> list[tuple[float, float]]:
    """n points at geodesic distance r_m from the centre on WGS84, azimuths N, then W (CCW)."""
    out = []
    for k in range(n):
        az = (360.0 - 360.0 * k / n) % 360.0
        d = W.Direct(lat, lng, az, r_m, DIRECT_MASK)
        out.append((d["lat2"], d["lon2"]))
    return out


def contains_pole(lat: float, r_m: float) -> bool:
    return (W.Inverse(lat, 0.0, 90.0, 0.0)["s12"] < r_m
            or W.Inverse(lat, 0.0, -90.0, 0.0)["s12"] < r_m)


def pole_detour_ring(ring: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """
    GeoJSON form of a ring that winds around a pole: continuous longitudes,
    then up the meridian to the pole, along the pole and back down. This is
    the usual way to write such a polygon in lng/lat coordinates.
    """
    pts = [(lat, (lng + 180.0) % 360.0 - 180.0) for lat, lng in ring]
    i0 = min(range(len(pts)), key=lambda i: pts[i][1])
    pts = pts[i0:] + pts[:i0]
    out = [pts[0]]
    for lat, lng in pts[1:]:
        prev = out[-1][1]
        out.append((lat, prev + (lng - prev + 180.0) % 360.0 - 180.0))
    first = out[0][1]
    close = (first - out[-1][1] + 180.0) % 360.0 - 180.0
    end = out[-1][1] + close
    winding = end - first
    if abs(abs(winding) - 360.0) > 1e-6:
        raise ValueError("ring does not wind around a pole")
    pole = 90.0 if winding > 0 else -90.0
    return out + [(out[0][0], end), (pole, end), (pole, first)]


# Area and length front ends

def gj_ring(ring: list[tuple[float, float]]) -> list[list[float]]:
    coords = [[lng, lat] for lat, lng in ring]
    coords.append(list(coords[0]))
    return coords


def qg_area(*rings: list[tuple[float, float]]) -> float:
    """qoregeo area in m2 (unit='m2' so the 4-decimal rounding is 0.0001 m2)."""
    return GEO.area({"type": "Polygon", "coordinates": [gj_ring(r) for r in rings]}, unit="m2")


def gl_area(ring: list[tuple[float, float]]) -> float:
    """GeographicLib PolygonArea on WGS84, geodesic edges, signed (CCW positive)."""
    poly = W.Polygon()
    for lat, lng in ring:
        poly.AddPoint(lat, lng)
    _, _, area = poly.Compute(False, True)
    return float(area)


def pyproj_area(ring: list[tuple[float, float]]) -> float:
    area, _ = GEOD.polygon_area_perimeter([p[1] for p in ring], [p[0] for p in ring])
    return float(area)


def gl_length(pts: list[tuple[float, float]]) -> float:
    return sum(W.Inverse(a[0], a[1], b[0], b[1], Geodesic.DISTANCE)["s12"] for a, b in zip(pts, pts[1:]))


def richardson(a_n: float, a_2n: float) -> float:
    """Remove the 1/n^2 term of a polygon-approximation error from two densities."""
    return a_2n + (a_2n - a_n) / 3.0


def rel(x: float, ref: float) -> float:
    return (x - ref) / ref


def pct(x: float) -> float:
    return float(f"{x * 100:.6g}")


# Experiments

def run_squares(lats: list[float], sizes: list[float], n_seg: int) -> list[dict[str, Any]]:
    rows = []
    for lat in lats:
        for size in sizes:
            corners, south, north, dlng = square_box(lat, size)
            dense2 = densify(corners, 2 * n_seg)
            dense1 = dense2[::2]
            qg_corners = qg_area(corners)
            qg_dense = qg_area(dense2)
            gl1, gl2 = gl_area(dense1), gl_area(dense2)
            ref = richardson(gl1, gl2)
            exact = box_area_wgs84(south, north, dlng)
            sphere = box_area_sphere(south, north, dlng)
            rows.append({
                "lat": lat, "size_km": size,
                "box": {"south": south, "north": north, "dlng": dlng},
                "qoregeo_m2": qg_dense,
                "qoregeo_corners_only_m2": qg_corners,
                "geographiclib_m2": ref,
                "wgs84_closed_form_m2": exact,
                "sphere_closed_form_m2": sphere,
                "error_pct": pct(rel(qg_dense, ref)),
                "error_vs_closed_form_pct": pct(rel(qg_dense, exact)),
                "small_shape_theory_pct": pct(small_area_error(lat)),
                "check_qoregeo_vs_sphere_closed_form": rel(qg_dense, sphere),
                "check_corners_vs_dense": rel(qg_corners, qg_dense),
                "check_geographiclib_vs_closed_form": rel(ref, exact),
                "check_geographiclib_unextrapolated_vs_closed_form": rel(gl2, exact),
                "check_pyproj_vs_geographiclib": rel(pyproj_area(dense2), gl2),
            })
    return rows


def circle_case(lat: float, r_km: float, n: int) -> dict[str, Any]:
    r_m = r_km * 1000.0
    ring2 = geodesic_circle(lat, 0.0, r_m, 2 * n)
    ring1 = ring2[::2]
    pole = contains_pole(lat, r_m)
    shape = pole_detour_ring if pole else (lambda ring: ring)
    qg1, qg2 = qg_area(shape(ring1)), qg_area(shape(ring2))
    gl1, gl2 = gl_area(ring1), gl_area(ring2)
    ratio1, ratio2 = qg1 / gl1, qg2 / gl2
    return {
        "lat": lat, "radius_km": r_km, "contains_pole": pole, "vertices": 2 * n,
        "qoregeo_m2": qg2,
        "geographiclib_m2": gl2,
        "wgs84_circle_m2": richardson(gl1, gl2),
        "error_pct": pct(ratio2 - 1),
        "small_shape_theory_pct": pct(small_area_error(lat)),
        "check_ratio_change_n_to_2n": ratio2 - ratio1,
        "check_pyproj_vs_geographiclib": rel(pyproj_area(ring2), gl2),
    }


def run_circles(lats: list[float], sizes: list[float], n: int) -> list[dict[str, Any]]:
    return [circle_case(lat, r, n) for lat in lats for r in sizes]


def run_holes(n_seg: int, n_circ: int) -> list[dict[str, Any]]:
    rows = []
    for lat in (0.0, 45.0, 75.0):
        outer, s1, n1, d1 = square_box(lat, 100.0)
        hole, s2, n2, d2 = square_box(lat, 50.0)
        o2, h2 = densify(outer, 2 * n_seg), densify(hole, 2 * n_seg)
        o1, h1 = o2[::2], h2[::2]
        hole_cw = list(reversed(h2))
        q_cw = qg_area(o2, hole_cw)
        q_ccw = qg_area(o2, h2)
        q_outer = qg_area(o2)
        ref = richardson(gl_area(o1), gl_area(o2)) - richardson(gl_area(h1), gl_area(h2))
        exact = box_area_wgs84(s1, n1, d1) - box_area_wgs84(s2, n2, d2)
        feature = GEO.area({"type": "Feature", "properties": {},
                            "geometry": {"type": "Polygon", "coordinates": [gj_ring(o2), gj_ring(hole_cw)]}},
                           unit="m2")
        rows.append({
            "case": f"100 km box with a 50 km box hole at {lat:g} N",
            "qoregeo_m2": q_cw, "reference_m2": ref, "wgs84_closed_form_m2": exact,
            "error_pct": pct(rel(q_cw, ref)),
            "outer_only_error_pct": pct(rel(q_outer, richardson(gl_area(o1), gl_area(o2)))),
            "hole_clockwise_vs_counterclockwise_rel_diff": rel(q_ccw, q_cw),
            "feature_wrapper_vs_polygon_rel_diff": rel(feature, q_cw),
            "check_reference_vs_closed_form": rel(ref, exact),
        })
    for lat in (30.0, 60.0):
        o2 = geodesic_circle(lat, 0.0, 100e3, 2 * n_circ)
        h2 = geodesic_circle(lat, 0.0, 50e3, 2 * n_circ)
        q = qg_area(o2, list(reversed(h2)))
        ref = gl_area(o2) - gl_area(h2)
        rows.append({
            "case": f"annulus, geodesic circles r = 100 km and 50 km at {lat:g} N",
            "qoregeo_m2": q, "reference_m2": ref, "error_pct": pct(rel(q, ref)),
            "outer_only_error_pct": pct(rel(qg_area(o2), gl_area(o2))),
        })
    a, *_ = square_box(10.0, 100.0)
    b, *_ = square_box(70.0, 100.0)
    a2, b2 = densify(a, 2 * n_seg), densify(b, 2 * n_seg)
    multi = GEO.area({"type": "MultiPolygon", "coordinates": [[gj_ring(a2)], [gj_ring(b2)]]}, unit="m2")
    ref = gl_area(a2) + gl_area(b2)
    rows.append({
        "case": "MultiPolygon: 100 km boxes at 10 N and 70 N",
        "qoregeo_m2": multi, "reference_m2": ref, "error_pct": pct(rel(multi, ref)),
        "parts_error_pct": [pct(rel(qg_area(a2), gl_area(a2))), pct(rel(qg_area(b2), gl_area(b2)))],
    })
    return rows


def run_colorado(n_seg: int) -> dict[str, Any]:
    south, north, west, east = 37.0, 41.0, -109.05, -102.05
    corners = [(south, west), (south, east), (north, east), (north, west)]
    dense2 = densify(corners, 2 * n_seg)
    dense1 = dense2[::2]
    as_typed_km2 = GEO.area({"type": "Polygon", "coordinates": [gj_ring(corners)]})   # default unit km2
    qg = qg_area(dense2) / 1e6
    gl_dense = richardson(gl_area(dense1), gl_area(dense2)) / 1e6
    gl_corners = gl_area(corners) / 1e6
    exact = box_area_wgs84(south, north, east - west) / 1e6
    sphere = box_area_sphere(south, north, east - west) / 1e6
    census = COLORADO_CENSUS_SQ_MI * SQ_MI_KM2
    return {
        "box": {"south": south, "north": north, "west": west, "east": east},
        "qoregeo_corners_km2": as_typed_km2,
        "qoregeo_densified_km2": qg,
        "sphere_closed_form_km2": sphere,
        "geographiclib_densified_km2": gl_dense,
        "geographiclib_corners_only_km2": gl_corners,
        "wgs84_closed_form_km2": exact,
        "pyproj_densified_km2": pyproj_area(dense2) / 1e6,
        "official_task_figure_km2": COLORADO_TASK_KM2,
        "official_task_figure_in_sq_mi": COLORADO_TASK_KM2 / SQ_MI_KM2,
        "census_2010_total_sq_mi": COLORADO_CENSUS_SQ_MI,
        "census_2010_total_km2": census,
        "qoregeo_vs_wgs84_box_pct": pct(rel(qg, gl_dense)),
        "qoregeo_vs_wgs84_box_km2": qg - gl_dense,
        "geographiclib_corners_vs_box_km2": gl_corners - gl_dense,
        "census_vs_wgs84_box_km2": census - gl_dense,
        "census_vs_wgs84_box_pct": pct(rel(census, gl_dense)),
        "task_figure_vs_wgs84_box_km2": COLORADO_TASK_KM2 - gl_dense,
        "task_figure_vs_wgs84_box_pct": pct(rel(COLORADO_TASK_KM2, gl_dense)),
        "qoregeo_vs_census_pct": pct(rel(qg, census)),
        "qoregeo_vs_task_figure_pct": pct(rel(qg, COLORADO_TASK_KM2)),
        "small_shape_theory_at_39N_pct": pct(small_area_error(39.0)),
    }


def run_buffers(lats: list[float], radii: list[float], circles: dict[tuple[float, float], float]) -> list[dict[str, Any]]:
    rows = []
    for lat in lats:
        for r_km in radii:
            r_m = r_km * 1000.0
            zone = GEO.buffer((lat, 0.0), r_km)
            n = len(zone["coordinates"][0]) - 1
            cap = GEO.area(zone, unit="m2")
            plain = GEO.area({"type": "Polygon", "coordinates": zone["coordinates"]}, unit="m2")
            wgs = circles[(lat, r_km)]
            pir2 = math.pi * r_m * r_m
            verts = [(p[1], p[0]) for p in zone["coordinates"][0][:-1]]
            dists = [W.Inverse(lat, 0.0, v[0], v[1], Geodesic.DISTANCE)["s12"] / r_m - 1 for v in verts]
            true_poly = geodesic_circle(lat, 0.0, r_m, n)
            region = rel(abs(gl_area(verts)), abs(gl_area(true_poly)))
            rows.append({
                "lat": lat, "radius_km": r_km, "num_points": n,
                "contains_pole": contains_pole(lat, r_m),
                "qoregeo_area_m2": cap,
                "pi_r2_m2": pir2,
                "wgs84_circle_m2": wgs,
                "area_error_pct": pct(rel(cap, wgs)),
                "pi_r2_error_pct": pct(rel(pir2, wgs)),
                "cap_vs_pi_r2_pct": pct(rel(cap, pir2)),
                "plain_polygon_vs_cap_pct": pct(rel(plain, cap)),
                "vertex_distance_error_pct": {"min": pct(min(dists)), "max": pct(max(dists))},
                "drawn_region_vs_true_circle_pct": pct(region),
            })
    return rows


def run_lengths(lats: list[float], sizes: list[float], n_seg: int) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"meridian": [], "parallel": []}
    for lat in lats:
        for size in sizes:
            size_m = size * 1000.0
            dlng = math.degrees(size_m / (normal_radius(lat) * math.cos(math.radians(lat))))
            par = [(lat, -dlng / 2 + dlng * k / n_seg) for k in range(n_seg + 1)]
            south = W.Direct(lat, 0.0, 180.0, size_m / 2)["lat2"]
            north = W.Direct(lat, 0.0, 0.0, size_m / 2)["lat2"]
            mer = [(south + (north - south) * k / n_seg, 0.0) for k in range(n_seg + 1)]
            for kind, pts, theory in (("parallel", par, R / normal_radius(lat) - 1),
                                      ("meridian", mer, R / meridian_radius(lat) - 1)):
                gl = gl_length(pts)
                hav = GEO.length(pts, unit="m")
                vin = GEO.length(pts, unit="m", method="vincenty")
                out[kind].append({
                    "lat": lat, "size_km": size, "segments": n_seg,
                    "geographiclib_m": gl, "haversine_m": hav, "vincenty_m": vin,
                    "haversine_error_pct": pct(rel(hav, gl)),
                    "vincenty_error_pct": pct(rel(vin, gl)),
                    "theory_pct": pct(theory),
                    "chords_vs_true_arc_pct": pct(rel(gl, size_m)),
                })
    return out


def run_random(count: int, m: int, n_check: int) -> dict[str, Any]:
    rng = random.Random(SEED)
    rows = []
    for i in range(count):
        lat_c = rng.uniform(-85.0, 85.0)
        lng_c = rng.uniform(-180.0, 180.0)
        rho = 10 ** rng.uniform(0.0, math.log10(500.0))
        k = rng.randint(6, 16)
        az = sorted((rng.uniform(0.0, 360.0) for _ in range(k)), reverse=True)
        verts = []
        for a in az:
            d = W.Direct(lat_c, lng_c, a, rho * 1000.0 * rng.uniform(0.5, 1.0), DIRECT_MASK)
            verts.append((d["lat2"], d["lon2"]))
        dense = densify(verts, m)
        q, g = qg_area(dense), gl_area(dense)
        row = {"lat": lat_c, "radius_km": rho, "vertices": k,
               "error_pct": pct(rel(q, g)), "theory_pct": pct(small_area_error(lat_c))}
        if i < n_check:
            dense2 = densify(verts, 2 * m)
            row["check_error_change_m_to_2m"] = rel(qg_area(dense2), gl_area(dense2)) - rel(q, g)
        rows.append(row)
    bins = []
    for lo in range(0, 90, 10):
        sel = [r for r in rows if lo <= abs(r["lat"]) < lo + 10]
        if sel:
            bins.append({"abs_lat_from": lo, "abs_lat_to": lo + 10, "count": len(sel),
                         "min_error_pct": min(r["error_pct"] for r in sel),
                         "max_error_pct": max(r["error_pct"] for r in sel)})
    checks = [abs(r["check_error_change_m_to_2m"]) for r in rows if "check_error_change_m_to_2m" in r]
    return {
        "count": count, "segments_per_edge": m,
        "description": ("Star-shaped polygons, 6 to 16 vertices, radius log-uniform from 1 to 500 km, "
                        "center uniform in latitude -85 to 85 and longitude -180 to 180, vertices placed "
                        "with GeographicLib Direct, edges split into equal lat/lng steps."),
        "by_abs_latitude": bins,
        "max_abs_error_pct_lat_le_55": max(abs(r["error_pct"]) for r in rows if abs(r["lat"]) <= 55),
        "max_abs_error_pct_all": max(abs(r["error_pct"]) for r in rows),
        "max_abs_deviation_from_theory_pct": max(abs(r["error_pct"] - r["theory_pct"]) for r in rows),
        "check_max_abs_error_change_m_to_2m": max(checks) if checks else None,
        "rows": rows,
    }


def run_edge_cases() -> dict[str, Any]:
    out: dict[str, Any] = {}
    zone = GEO.buffer((85.0, 10.0), 1000)
    plain = {"type": "Polygon", "coordinates": zone["coordinates"]}
    lngs = [p[0] for p in zone["coordinates"][0]]
    out["buffer_covering_north_pole"] = {
        "call": "geo.buffer((85.0, 10.0), 1000)",
        "area_of_buffer_km2": GEO.area(zone),
        "area_of_its_coordinates_km2": GEO.area(plain),
        "coordinate_lng_range": [min(lngs), max(lngs)],
        "point_in_polygon_89.9N_buffer": GEO.point_in_polygon((89.9, 0.0), zone),
        "point_in_polygon_89.9N_coordinates": GEO.point_in_polygon((89.9, 0.0), plain),
        "geographiclib_area_of_its_vertices_km2": abs(gl_area([(p[1], p[0]) for p in zone["coordinates"][0][:-1]])) / 1e6,
    }
    zone = GEO.buffer((28.61, 77.20), 10)
    fc = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": zone}]}
    out["buffer_inside_feature_collection"] = {
        "area_of_buffer_m2": GEO.area(zone, unit="m2"),
        "area_of_feature_m2": GEO.area(fc["features"][0], unit="m2"),
        "area_of_feature_collection_m2": GEO.area(fc, unit="m2"),
    }
    ring = [[-180.0, 85.0], [180.0, 85.0], [180.0, 90.0], [-180.0, 90.0], [-180.0, 85.0]]
    out["polar_cap_written_with_pole_vertices"] = {
        "qoregeo_km2": GEO.area({"type": "Polygon", "coordinates": [ring]}),
        "sphere_cap_km2": 2 * math.pi * R * R * (1 - math.sin(math.radians(85.0))) / 1e6,
        "wgs84_cap_km2": box_area_wgs84(85.0, 90.0, 360.0) / 1e6,
    }
    return out


def run_symmetry(n_seg: int, n_circ: int) -> list[dict[str, Any]]:
    rows = []
    for lat in (45.0, 85.0):
        sq = {s: run_squares([s * lat], [100.0], n_seg)[0]["error_pct"] for s in (1, -1)}
        ci = {s: circle_case(s * lat, 100.0, n_circ)["error_pct"] for s in (1, -1)}
        rows.append({"lat": lat, "square_100km_north_pct": sq[1], "square_100km_south_pct": sq[-1],
                     "circle_100km_north_pct": ci[1], "circle_100km_south_pct": ci[-1]})
    return rows


def run_timing(repeats: int) -> dict[str, Any]:
    ring = geodesic_circle(45.0, 7.0, 100e3, 1024)
    poly = {"type": "Polygon", "coordinates": [gj_ring(ring)]}
    lats = [p[0] for p in ring]
    lngs = [p[1] for p in ring]
    cases: dict[str, Callable[[], object]] = {
        "qoregeo.area (pure Python, sphere)": lambda: GEO.area(poly, unit="m2"),
        "geographiclib PolygonArea (pure Python, WGS84)": lambda: gl_area(ring),
        "pyproj Geod.polygon_area_perimeter (C, WGS84)": lambda: GEOD.polygon_area_perimeter(lngs, lats),
    }
    out = {}
    for label, fn in cases.items():
        timer = timeit.Timer(fn)
        number, _ = timer.autorange()
        runs = [t / number for t in timer.repeat(repeat=repeats, number=number)]
        out[label] = {"median_us": round(statistics.median(runs) * 1e6, 2),
                      "min_us": round(min(runs) * 1e6, 2), "calls_per_run": number, "runs": repeats}
    return {"polygon": "geodesic circle, r = 100 km at 45 N, 1,024 vertices", "per_call": out}


# Tables and claim checks

def table(rows: list[dict[str, Any]], size_key: str, value_key: str = "error_pct") -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        out.setdefault(f"{r[size_key]:g} km", {})[f"{r['lat']:g}"] = r[value_key]
    return out


def claim_check(squares: list[dict[str, Any]], circles: list[dict[str, Any]],
                random_res: dict[str, Any]) -> dict[str, Any]:
    fine = [i / 10 for i in range(0, 901)]
    theory = [abs(small_area_error(x)) * 100 for x in fine]
    low = max(t for x, t in zip(fine, theory) if x <= 55)
    high = max(theory)
    meas = squares + circles
    m_low = max(abs(r["error_pct"]) for r in meas if abs(r["lat"]) <= 55)
    m_all = max(abs(r["error_pct"]) for r in meas)
    zero = next(x for x, y in zip(fine, (small_area_error(x) for x in fine)) if y < 0)
    return {
        "claim": ("skills/qoregeo/SKILL.md: within about 0.5 % of WGS84 values up to ~55 degrees "
                  "latitude and up to ~0.9 % near the poles"),
        "theory_max_abs_pct_lat_0_to_55": round(low, 4),
        "theory_max_abs_pct_lat_0_to_90": round(high, 4),
        "theory_sign_change_lat": zero,
        "measured_max_abs_pct_lat_0_to_55": m_low,
        "measured_max_abs_pct_all": m_all,
        "random_polygons_max_abs_pct_lat_0_to_55": random_res["max_abs_error_pct_lat_le_55"],
        "random_polygons_max_abs_pct_all": random_res["max_abs_error_pct_all"],
        "holds": bool(m_low <= 0.5 and m_all <= 0.9
                      and random_res["max_abs_error_pct_lat_le_55"] <= 0.5
                      and random_res["max_abs_error_pct_all"] <= 0.9),
        "engine_docstring_claim": ("qoregeo/engine.py GeoEngine.area: 'within about 0.5% of ellipsoidal "
                                   "areas', with no latitude limit"),
        "engine_docstring_claim_holds": bool(m_all <= 0.55),
    }


# Figures

def _style() -> None:
    plt.rcParams.update({
        "svg.hashsalt": "qoregeo-experiment-02",
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


def _claim_band(ax: plt.Axes) -> None:
    ax.fill_between([0, 55], [-0.5, -0.5], [0.5, 0.5], color=BAND, linewidth=0, zorder=0)
    ax.fill_between([55, 90], [-0.9, -0.9], [0.9, 0.9], color=BAND, linewidth=0, zorder=0)


def _theory(ax: plt.Axes, fn: Callable[[float], float], label: str) -> None:
    xs = [i / 2 for i in range(0, 181)]
    ax.plot(xs, [fn(x) * 100 for x in xs], color=INK2, linewidth=1.1, linestyle=(0, (4, 2.5)),
            label=label, zorder=2)


def _size_lines(ax: plt.Axes, rows: list[dict[str, Any]], key: str, sizes: list[float],
                size_key: str = "size_km", label_fmt: str = "{:g} km") -> None:
    for i, size in enumerate(sizes):
        sel = sorted((r for r in rows if r[size_key] == size and r["lat"] >= 0), key=lambda r: r["lat"])
        ax.plot([r["lat"] for r in sel], [r[key] for r in sel], color=C_SIZE[i], marker=MARKERS[i],
                markersize=4.5, linewidth=1.6, label=label_fmt.format(size), zorder=3,
                markeredgecolor="white", markeredgewidth=0.6)


def fig_area(squares: list[dict[str, Any]], circles: list[dict[str, Any]], random_res: dict[str, Any],
             sizes: list[float]) -> str:
    fig, axes = plt.subplots(1, 3, figsize=(8, 3.7), sharey=True)
    panels = [(axes[0], "Lat/lng squares (side)"), (axes[1], "Geodesic circles (radius)"),
              (axes[2], f"Random polygons ({random_res['count']})")]
    for ax, title in panels:
        _claim_band(ax)
        ax.axhline(0, color=INK2, linewidth=0.6, zorder=1)
        _theory(ax, small_area_error, "small-shape limit R²/(MN) - 1")
        ax.set_title(title, fontsize=9.5, color=INK, loc="left")
        ax.set_xlim(0, 90)
        ax.set_xticks([0, 15, 30, 45, 60, 75, 90])
        ax.set_xlabel("Latitude (degrees)")
    _size_lines(axes[0], squares, "error_pct", sizes)
    _size_lines(axes[1], circles, "error_pct", sizes, size_key="radius_km")
    rr = random_res["rows"]
    axes[2].scatter([abs(r["lat"]) for r in rr], [r["error_pct"] for r in rr], s=9, color=C_SIZE[0],
                    alpha=0.55, linewidths=0, zorder=3, label="random polygon")
    axes[0].set_ylabel("Area error vs WGS84 (%)")
    axes[0].set_ylim(-1.0, 0.6)
    handles, labels = axes[0].get_legend_handles_labels()
    h2, l2 = axes[2].get_legend_handles_labels()
    band = plt.Rectangle((0, 0), 1, 1, color=BAND)
    fig.legend(handles + [h2[-1], band], labels + [l2[-1], "SKILL.md claim (0.5 % to 55°, 0.9 % above)"],
               loc="lower center", ncol=4, fontsize=8, bbox_to_anchor=(0.5, 0.0))
    fig.tight_layout(rect=(0, 0.13, 1, 1))
    _note(fig)
    return _save(fig, "latitude")


def fig_length(lengths: dict[str, list[dict[str, Any]]], sizes: list[float]) -> str:
    fig, axes = plt.subplots(1, 2, figsize=(8, 3.5), sharey=True)
    for ax, kind, fn, title in (
        (axes[0], "meridian", lambda x: R / meridian_radius(x) - 1, "Along a meridian"),
        (axes[1], "parallel", lambda x: R / normal_radius(x) - 1, "Along a parallel"),
    ):
        ax.axhline(0, color=INK2, linewidth=0.6, zorder=1)
        _theory(ax, fn, "theory R/M - 1 and R/N - 1")
        _size_lines(ax, lengths[kind], "haversine_error_pct", sizes)
        ax.set_title(title, fontsize=9.5, color=INK, loc="left")
        ax.set_xlim(0, 90)
        ax.set_xticks([0, 15, 30, 45, 60, 75, 90])
        ax.set_xlabel("Latitude (degrees)")
    axes[0].set_ylabel("length() haversine error vs WGS84 (%)")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=5, fontsize=8, bbox_to_anchor=(0.5, 0.0))
    fig.tight_layout(rect=(0, 0.08, 1, 1))
    _note(fig)
    return _save(fig, "length")


def fig_buffer(buffers: list[dict[str, Any]], radii: list[float]) -> str:
    fig, axes = plt.subplots(1, 2, figsize=(8, 3.5))
    ax = axes[0]
    ax.axhline(0, color=INK2, linewidth=0.6, zorder=1)
    for i, r_km in enumerate(radii):
        sel = sorted((b for b in buffers if b["radius_km"] == r_km and b["lat"] >= 0), key=lambda b: b["lat"])
        ax.plot([b["lat"] for b in sel], [b["area_error_pct"] for b in sel], color=C_SIZE[i],
                marker=MARKERS[i], markersize=4.5, linewidth=1.6, label=f"r = {r_km:g} km", zorder=3,
                markeredgecolor="white", markeredgewidth=0.6)
    ax.set_title("area(buffer) vs WGS84 geodesic circle", fontsize=9.5, color=INK, loc="left")
    ax.set_ylabel("Error (%)")
    ax = axes[1]
    _claim_band(ax)
    ax.axhline(0, color=INK2, linewidth=0.6, zorder=1)
    _theory(ax, small_area_error, "small-shape limit R²/(MN) - 1")
    for i, r_km in enumerate(radii):
        sel = sorted((b for b in buffers if b["radius_km"] == r_km and b["lat"] >= 0
                      and not b["contains_pole"]), key=lambda b: b["lat"])
        ax.plot([b["lat"] for b in sel], [b["drawn_region_vs_true_circle_pct"] for b in sel],
                color=C_SIZE[i], marker=MARKERS[i], markersize=4.5, linewidth=1.6, zorder=3,
                markeredgecolor="white", markeredgewidth=0.6)
    ax.set_title("buffer polygon vs true circle, on WGS84", fontsize=9.5, color=INK, loc="left")
    for ax in axes:
        ax.set_xlim(0, 90)
        ax.set_xticks([0, 15, 30, 45, 60, 75, 90])
        ax.set_xlabel("Latitude (degrees)")
    handles, labels = axes[0].get_legend_handles_labels()
    h2, l2 = axes[1].get_legend_handles_labels()
    fig.legend(handles + h2[:1], labels + l2[:1], loc="lower center", ncol=5, fontsize=8,
               bbox_to_anchor=(0.5, 0.0))
    fig.tight_layout(rect=(0, 0.08, 1, 1))
    _note(fig)
    return _save(fig, "buffer")


# JSON helpers

def _f(obj: Any) -> Any:
    """Round floats for a readable JSON file (areas keep sub-square-metre precision)."""
    if isinstance(obj, float):
        if obj == 0 or not math.isfinite(obj):
            return obj
        return float(f"{obj:.12g}")
    if isinstance(obj, dict):
        return {k: _f(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_f(v) for v in obj]
    return obj


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--quick", action="store_true", help="reduced sizes; runs in a few seconds")
    args = parser.parse_args()
    t_start = time.perf_counter()
    quick = args.quick
    lats = [0.0, 15.0, 30.0, 45.0, 60.0, 75.0, 85.0] if quick else [float(x) for x in range(0, 90, 5)]
    sizes = [1.0, 10.0, 100.0, 1000.0]
    sz = {
        "latitudes": lats,
        "sizes_km": sizes,
        "box_segments_per_edge": [16, 32] if quick else [512, 1024],
        "circle_vertices": [64, 128] if quick else [1024, 2048],
        "length_segments": 16 if quick else 64,
        "random_polygons": 40 if quick else 400,
        "random_segments_per_edge": 8 if quick else 16,
        "random_convergence_subset": 10 if quick else 50,
        "colorado_segments_per_edge": [32, 64] if quick else [1024, 2048],
        "timing_repeats": 3 if quick else 7,
    }
    n_seg = sz["box_segments_per_edge"][0]
    n_circ = sz["circle_vertices"][0]

    print("squares", flush=True)
    squares = run_squares(lats, sizes, n_seg)
    print("circles", flush=True)
    circles = run_circles(lats, sizes, n_circ)
    circle_area = {(c["lat"], c["radius_km"]): c["wgs84_circle_m2"] for c in circles}
    print("holes, Colorado, buffers, lengths", flush=True)
    holes = run_holes(n_seg, n_circ)
    colorado = run_colorado(sz["colorado_segments_per_edge"][0])
    buffers = run_buffers(lats, sizes, circle_area)
    lengths = run_lengths(lats, sizes, sz["length_segments"])
    print("random polygons", flush=True)
    random_res = run_random(sz["random_polygons"], sz["random_segments_per_edge"], sz["random_convergence_subset"])
    symmetry = run_symmetry(n_seg, n_circ)
    edge = run_edge_cases()
    print("timing", flush=True)
    timing = run_timing(sz["timing_repeats"])

    req = {0.0, 15.0, 30.0, 45.0, 60.0, 75.0, 85.0}
    pick = [r for r in squares if r["lat"] in req]
    pick_c = [r for r in circles if r["lat"] in req]
    claims = claim_check(squares, circles, random_res)
    len_all = lengths["meridian"] + lengths["parallel"]
    results: dict[str, Any] = {
        "area_error_pct_by_size_and_latitude": {
            "squares_side": table(pick, "size_km"),
            "circles_radius": table(pick_c, "radius_km"),
            "small_shape_theory": {f"{x:g}": pct(small_area_error(x)) for x in sorted(req)},
        },
        "docs_claim_check": claims,
        "reference_checks": {
            "squares_max_abs_geographiclib_vs_closed_form": max(abs(r["check_geographiclib_vs_closed_form"]) for r in squares),
            "squares_max_abs_unextrapolated_geographiclib_vs_closed_form": max(
                abs(r["check_geographiclib_unextrapolated_vs_closed_form"]) for r in squares),
            "squares_max_abs_qoregeo_vs_sphere_closed_form": max(abs(r["check_qoregeo_vs_sphere_closed_form"]) for r in squares),
            "squares_max_abs_corners_only_vs_densified": max(abs(r["check_corners_vs_dense"]) for r in squares),
            "circles_max_abs_ratio_change_n_to_2n": max(
                abs(r["check_ratio_change_n_to_2n"]) for r in circles if not r["contains_pole"]),
            "pole_circle_ratio_change_n_to_2n": [r["check_ratio_change_n_to_2n"] for r in circles
                                                 if r["contains_pole"]],
            "max_abs_pyproj_vs_geographiclib": max(abs(r["check_pyproj_vs_geographiclib"]) for r in squares + circles),
        },
        "colorado": colorado,
        "buffer": {
            "rows": buffers,
            "max_abs_area_error_pct_by_radius": {
                f"{r:g} km": max(abs(b["area_error_pct"]) for b in buffers if b["radius_km"] == r) for r in sizes},
            "max_abs_pi_r2_error_pct_by_radius": {
                f"{r:g} km": max(abs(b["pi_r2_error_pct"]) for b in buffers if b["radius_km"] == r) for r in sizes},
            "drawn_region_vs_true_circle_pct_range": [
                min(b["drawn_region_vs_true_circle_pct"] for b in buffers if not b["contains_pole"]),
                max(b["drawn_region_vs_true_circle_pct"] for b in buffers if not b["contains_pole"])],
            "vertex_distance_error_pct_range": [
                min(b["vertex_distance_error_pct"]["min"] for b in buffers),
                max(b["vertex_distance_error_pct"]["max"] for b in buffers)],
        },
        "length": {
            "haversine_error_pct": {
                "meridian": table([r for r in lengths["meridian"] if r["lat"] in req], "size_km", "haversine_error_pct"),
                "parallel": table([r for r in lengths["parallel"] if r["lat"] in req], "size_km", "haversine_error_pct"),
            },
            "haversine_error_pct_range": [min(r["haversine_error_pct"] for r in len_all),
                                          max(r["haversine_error_pct"] for r in len_all)],
            "vincenty_max_abs_error_pct": max(abs(r["vincenty_error_pct"]) for r in len_all),
            "rows": lengths,
        },
        "holes": holes,
        "symmetry_north_south": symmetry,
        "edge_cases": edge,
        "random_polygons": random_res,
        "timing": timing,
        "squares": squares,
        "circles": circles,
    }

    _style()
    figures = [
        fig_area(squares, circles, random_res, sizes),
        fig_length(lengths, sizes),
        fig_buffer(buffers, sizes),
    ]

    meta = {
        "experiment": NAME,
        "mode": "quick" if quick else "full",
        "qoregeo_version": qoregeo.__version__,
        "python_version": sys.version,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "reference_libraries": {
            "geographiclib": _md.version("geographiclib"),
            "pyproj": pyproj.__version__,
            "proj": pyproj.proj_version_str,
            "matplotlib": matplotlib.__version__,
        },
        "seed": SEED,
        "sizes": sz,
        "utc_timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "command": shlex.join(["python", *sys.argv]),
        "python_executable": sys.executable,
        "runtime_seconds": None,
        "figures": figures,
        "method": [
            "Error = (qoregeo - reference) / reference. Reference: GeographicLib PolygonArea on WGS84.",
            "Both libraries receive the same densified vertex list. Box edges are split into equal "
            "lat/lng steps (so the north and south edges follow parallels); circles are sampled with "
            "GeographicLib Direct at equal azimuth steps.",
            "Each case runs at two densities. Box references are Richardson-extrapolated from the two "
            "and checked against the closed-form WGS84 area of a parallel/meridian box.",
            "Squares: side_km along the central meridian and side_km along the central parallel (WGS84). "
            "Circles: geodesic radius = size. Circles that contain a pole are given to qoregeo with "
            "explicit pole vertices (GeoJSON has no other way to close such a ring in lng/lat).",
            "qoregeo is called with unit='m2' and unit='m' so its 4-decimal rounding is negligible.",
            "area() of a buffer() result is the spherical cap 2 pi R^2 (1 - cos(r/R)); qoregeo detects a "
            "buffer by the _center and _radius_km keys on the geometry (or on a Feature's geometry).",
        ],
        "notes": [
            "The Colorado 'official' figure from the brief (269,837 km2) equals 104,185 sq mi. The Census "
            "2010 total is 104,094 sq mi (269,601 km2). Both are recorded; the Census table itself could "
            "not be fetched from this machine.",
            "Timing numbers are indicative; the machine was shared with other jobs.",
        ],
    }
    meta["runtime_seconds"] = round(time.perf_counter() - t_start, 1)
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "results": results}
    RESULTS_PATH.write_text(json.dumps(_f(payload), indent=1, allow_nan=False) + "\n", encoding="utf-8")
    print(f"wrote {RESULTS_PATH.relative_to(REPO)} and {len(figures)} figures "
          f"in {meta['runtime_seconds']} s", flush=True)


if __name__ == "__main__":
    main()
