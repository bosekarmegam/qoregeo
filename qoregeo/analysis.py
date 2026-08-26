"""
qoregeo.analysis
================
Spatial statistics and clustering.

These are the operations that turn a pile of coordinates into an answer:
where do the points bunch up, which polygon does each point belong to, and
is the pattern actually clustered or just random?

Every algorithm works on true great-circle distances rather than treating
latitude and longitude as a flat plane. The shortcut that quietly ruins
clustering results anywhere outside the tropics.
"""

from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .exceptions import EmptyDatasetError, InvalidRadiusError
from .geometry import centroid_of, point_in_geometry
from .index import SpatialIndex, km_per_degree_lng
from .utils import EARTH_RADIUS_KM, _haversine_km

Coord = Tuple[float, float]
Feature = Dict[str, Any]

#: Cluster label for points that belong to no cluster.
NOISE = -1


def _points_of(features: Sequence[Feature]) -> List[Coord]:
    """Representative ``(lat, lng)`` for each feature."""
    pts: List[Coord] = []
    for feat in features:
        geom = feat.get("geometry") or {}
        if geom.get("type") == "Point":
            c = geom.get("coordinates") or []
            pts.append((float(c[1]), float(c[0])))
        else:
            pts.append(centroid_of(geom))
    return pts


# ─────────────────────────────────────────────────────────────────────────────
# DBSCAN
# ─────────────────────────────────────────────────────────────────────────────

def dbscan(
    features: Sequence[Feature],
    eps_km: float = 1.0,
    min_samples: int = 3,
) -> List[int]:
    """
    Density-based clustering. Returns one cluster label per feature.

    DBSCAN's appeal over k-means is that you don't have to guess how many
    clusters exist, and outliers stay outliers instead of being forced into
    the nearest group. Points too isolated to join a cluster get :data:`NOISE`
    (``-1``).

    Parameters
    ----------
    eps_km      : neighbourhood radius, two points this close are neighbours
    min_samples : how many neighbours (including itself) make a point a "core"
    """
    if eps_km <= 0:
        raise InvalidRadiusError(eps_km)
    if not features:
        return []

    pts = _points_of(features)
    index = SpatialIndex(features, cell_size_km=max(eps_km, 0.05))

    labels = [NOISE] * len(pts)
    visited = [False] * len(pts)
    cluster_id = 0

    for i in range(len(pts)):
        if visited[i]:
            continue
        visited[i] = True

        neighbours = [j for _, j in index.within(pts[i], eps_km)]
        if len(neighbours) < min_samples:
            continue                      # not dense enough to seed a cluster

        labels[i] = cluster_id
        queue = list(neighbours)
        seen = set(neighbours)

        while queue:
            j = queue.pop()
            if not visited[j]:
                visited[j] = True
                j_neighbours = [n for _, n in index.within(pts[j], eps_km)]
                if len(j_neighbours) >= min_samples:
                    for n in j_neighbours:
                        if n not in seen:
                            seen.add(n)
                            queue.append(n)
            if labels[j] == NOISE:
                labels[j] = cluster_id    # border point joins this cluster

        cluster_id += 1

    return labels


# ─────────────────────────────────────────────────────────────────────────────
# k-means
# ─────────────────────────────────────────────────────────────────────────────

def kmeans(
    features: Sequence[Feature],
    k: int = 3,
    max_iterations: int = 100,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Spherical k-means. Returns ``{"labels", "centroids", "iterations", "inertia"}``.

    Seeded with k-means++ so the result is stable and well-spread rather than
    dependent on a lucky random draw. Centroids are averaged in 3-D Cartesian
    space and projected back to the sphere, which keeps them sensible across
    the antimeridian. A plain mean of longitudes would place a Fiji cluster
    in Africa.
    """
    if not features:
        raise EmptyDatasetError("<clustering input>")

    pts = _points_of(features)
    k = max(1, min(int(k), len(pts)))
    rng = random.Random(seed)

    centroids = _kmeanspp_init(pts, k, rng)
    labels = [0] * len(pts)
    iterations = 0

    for step in range(1, max_iterations + 1):
        iterations = step
        moved = False
        for i, p in enumerate(pts):
            best = min(range(k), key=lambda c: _haversine_km(p, centroids[c]))
            if labels[i] != best:
                labels[i] = best
                moved = True

        for c in range(k):
            members = [pts[i] for i in range(len(pts)) if labels[i] == c]
            if members:
                centroids[c] = spherical_mean(members)

        if not moved:
            break

    inertia = sum(_haversine_km(pts[i], centroids[labels[i]]) ** 2 for i in range(len(pts)))
    return {
        "labels": labels,
        "centroids": centroids,
        "iterations": iterations,
        "inertia": round(inertia, 4),
    }


def _kmeanspp_init(pts: List[Coord], k: int, rng: random.Random) -> List[Coord]:
    """k-means++ seeding: each new centre is drawn far from the existing ones."""
    centroids = [pts[rng.randrange(len(pts))]]
    while len(centroids) < k:
        weights = [min(_haversine_km(p, c) ** 2 for c in centroids) for p in pts]
        total = sum(weights)
        if total <= 0:
            centroids.append(pts[rng.randrange(len(pts))])
            continue
        threshold = rng.random() * total
        running = 0.0
        for p, w in zip(pts, weights):
            running += w
            if running >= threshold:
                centroids.append(p)
                break
    return centroids


def spherical_mean(points: Sequence[Coord]) -> Coord:
    """
    Average of coordinates done properly, via 3-D unit vectors.

    Averaging degrees directly breaks across the antimeridian and near the
    poles; this does not.
    """
    if not points:
        raise EmptyDatasetError("<empty point list>")

    x = y = z = 0.0
    for lat, lng in points:
        lat_r, lng_r = math.radians(lat), math.radians(lng)
        x += math.cos(lat_r) * math.cos(lng_r)
        y += math.cos(lat_r) * math.sin(lng_r)
        z += math.sin(lat_r)

    n = len(points)
    x, y, z = x / n, y / n, z / n
    hyp = math.sqrt(x * x + y * y)
    if hyp < 1e-12 and abs(z) < 1e-12:
        return (0.0, 0.0)                 # points cancel out exactly
    return (math.degrees(math.atan2(z, hyp)), math.degrees(math.atan2(y, x)))


# ─────────────────────────────────────────────────────────────────────────────
# Spatial join
# ─────────────────────────────────────────────────────────────────────────────

def spatial_join(
    points: Sequence[Feature],
    polygons: Sequence[Feature],
    prefix: str = "",
    keep: Optional[Sequence[str]] = None,
    how: str = "left",
) -> List[Feature]:
    """
    Attach polygon attributes to the points that fall inside them.

    This is the join that answers "which district is each customer in?",
    the single most common GIS operation after distance.

    Parameters
    ----------
    prefix : prepended to copied property names, to avoid clobbering
    keep   : only copy these polygon properties (default: all)
    how    : ``"left"`` keeps unmatched points, ``"inner"`` drops them
    """
    boxes = []
    for poly in polygons:
        geom = poly.get("geometry") or poly
        try:
            from .geometry import bbox_of

            boxes.append((bbox_of(geom), geom, poly.get("properties", {})))
        except Exception:
            continue

    out: List[Feature] = []
    for feat in points:
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") != "Point" or len(coords) < 2:
            if how == "left":
                out.append(feat)
            continue

        lng, lat = float(coords[0]), float(coords[1])
        matched: Optional[Dict[str, Any]] = None

        for box, poly_geom, props in boxes:
            if not (box[0] <= lng <= box[2] and box[1] <= lat <= box[3]):
                continue                  # bbox reject before the expensive test
            if point_in_geometry((lat, lng), poly_geom):
                matched = props
                break

        if matched is None:
            if how == "left":
                out.append(feat)
            continue

        extra = {
            f"{prefix}{k}": v
            for k, v in matched.items()
            if keep is None or k in keep
        }
        merged = dict(feat.get("properties", {}))
        merged.update(extra)
        out.append({**feat, "properties": merged})

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Density / hotspots
# ─────────────────────────────────────────────────────────────────────────────

def hotspots(
    features: Sequence[Feature],
    cell_km: float = 5.0,
    min_count: int = 2,
) -> List[Dict[str, Any]]:
    """
    Bin points into a square grid and return the busy cells, densest first.

    Each entry carries the cell polygon, its point count and its centre, so
    the result drops straight into a choropleth.
    """
    if cell_km <= 0:
        raise InvalidRadiusError(cell_km)

    pts = _points_of(features)
    if not pts:
        return []

    lat_step = cell_km / 111.32
    buckets: Dict[Tuple[int, int], List[Coord]] = {}

    for lat, lng in pts:
        lng_step = cell_km / km_per_degree_lng(lat)
        key = (int(math.floor(lat / lat_step)), int(math.floor(lng / lng_step)))
        buckets.setdefault(key, []).append((lat, lng))

    cells: List[Dict[str, Any]] = []
    for (row, col), members in buckets.items():
        if len(members) < min_count:
            continue
        min_lat = row * lat_step
        max_lat = min_lat + lat_step
        lng_step = cell_km / km_per_degree_lng(min_lat)
        min_lng = col * lng_step
        max_lng = min_lng + lng_step

        cells.append(
            {
                "count": len(members),
                "centre": spherical_mean(members),
                "bounds": {
                    "min_lat": min_lat,
                    "max_lat": max_lat,
                    "min_lng": min_lng,
                    "max_lng": max_lng,
                },
                "polygon": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [min_lng, min_lat],
                            [max_lng, min_lat],
                            [max_lng, max_lat],
                            [min_lng, max_lat],
                            [min_lng, min_lat],
                        ]
                    ],
                },
            }
        )

    cells.sort(key=lambda c: -int(c["count"]))
    return cells


def nearest_neighbour_ratio(features: Sequence[Feature]) -> Dict[str, Any]:
    """
    Clark & Evans nearest-neighbour statistic.

    Answers "is this pattern actually clustered, or does it just look that
    way?". A ratio below 1 means clustered, above 1 means dispersed, and
    around 1 means indistinguishable from random.
    """
    pts = _points_of(features)
    n = len(pts)
    if n < 2:
        raise EmptyDatasetError("<need at least 2 features>")

    index = SpatialIndex(features)
    observed = 0.0
    for i, p in enumerate(pts):
        neighbours = index.nearest(p, 2)
        d = next((dist for dist, j in neighbours if j != i), None)
        if d is None:
            d = 0.0
        observed += d
    observed /= n

    lats = [p[0] for p in pts]
    lngs = [p[1] for p in pts]
    mid_lat = (max(lats) + min(lats)) / 2
    height = (max(lats) - min(lats)) * 111.32
    width = (max(lngs) - min(lngs)) * km_per_degree_lng(mid_lat)
    area = max(height * width, 1e-9)

    expected = 0.5 / math.sqrt(n / area)
    ratio = observed / expected if expected else 0.0

    if ratio < 0.9:
        pattern = "clustered"
    elif ratio > 1.1:
        pattern = "dispersed"
    else:
        pattern = "random"

    return {
        "observed_mean_km": round(observed, 4),
        "expected_mean_km": round(expected, 4),
        "ratio": round(ratio, 4),
        "pattern": pattern,
        "area_km2": round(area, 4),
        "n": n,
    }


def centre_of_mass(features: Sequence[Feature], weight_col: Optional[str] = None) -> Coord:
    """
    Weighted geographic centre. The "where should the depot go?" answer.

    With ``weight_col`` it is the centre of gravity of that column (population,
    revenue, order count); without it, the plain mean centre.
    """
    pts = _points_of(features)
    if not pts:
        raise EmptyDatasetError("<empty dataset>")

    if not weight_col:
        return spherical_mean(pts)

    x = y = z = 0.0
    total = 0.0
    for feat, (lat, lng) in zip(features, pts):
        try:
            w = float(feat.get("properties", {}).get(weight_col, 0) or 0)
        except (TypeError, ValueError):
            w = 0.0
        if w <= 0:
            continue
        lat_r, lng_r = math.radians(lat), math.radians(lng)
        x += w * math.cos(lat_r) * math.cos(lng_r)
        y += w * math.cos(lat_r) * math.sin(lng_r)
        z += w * math.sin(lat_r)
        total += w

    if total == 0:
        return spherical_mean(pts)

    x, y, z = x / total, y / total, z / total
    return (math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x)))


def dispersion(features: Sequence[Feature]) -> Dict[str, float]:
    """
    How spread out the dataset is: mean, median and max distance from its centre,
    plus the standard distance (the spatial equivalent of a standard deviation).
    """
    pts = _points_of(features)
    if not pts:
        raise EmptyDatasetError("<empty dataset>")

    centre = spherical_mean(pts)
    dists = sorted(_haversine_km(centre, p) for p in pts)
    n = len(dists)
    mean = sum(dists) / n
    median = dists[n // 2] if n % 2 else (dists[n // 2 - 1] + dists[n // 2]) / 2
    std = math.sqrt(sum(d * d for d in dists) / n)

    return {
        "centre_lat": round(centre[0], 6),
        "centre_lng": round(centre[1], 6),
        "mean_km": round(mean, 4),
        "median_km": round(median, 4),
        "max_km": round(dists[-1], 4),
        "standard_distance_km": round(std, 4),
    }


def great_circle_area_of(radius_km: float) -> float:
    """Surface area of a spherical cap of the given radius, used by density maths."""
    if radius_km <= 0:
        raise InvalidRadiusError(radius_km)
    theta = radius_km / EARTH_RADIUS_KM
    return 2 * math.pi * EARTH_RADIUS_KM ** 2 * (1 - math.cos(theta))
