"""
qoregeo.index
=============
A uniform-grid spatial index in pure Python.

Naive radius and nearest-neighbour queries scan every feature: O(n) per query,
which is fine for a few hundred points and painful for a few hundred thousand.
This index buckets features into fixed-size latitude/longitude cells so a query
only has to look at the cells its search radius actually touches.

The grid is deliberately simple, no R-tree, no rebalancing, no C extension.
It builds in one linear pass and answers typical city-scale queries in
microseconds.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .utils import EARTH_RADIUS_KM, _haversine_km

Coord = Tuple[float, float]
Feature = Dict[str, Any]

#: Datasets at or above this size get an index built automatically.
AUTO_INDEX_THRESHOLD = 500

#: One degree of latitude is always ~111.32 km.
KM_PER_DEGREE_LAT = 111.32


def km_per_degree_lng(lat: float) -> float:
    """Kilometres in one degree of longitude at a given latitude."""
    return max(1e-9, KM_PER_DEGREE_LAT * math.cos(math.radians(lat)))


class SpatialIndex:
    """
    Grid index over a list of point features.

    Parameters
    ----------
    features     : GeoJSON features to index (non-point geometries use their centroid)
    cell_size_km : grid resolution; chosen from the data when omitted

    Notes
    -----
    The grid is uniform **in degrees**, not in kilometres. That matters: an
    earlier design sized each column by the local km-per-degree, which made a
    point's column index depend on its own latitude, so "the cell next door"
    stopped meaning the same thing in different rows, and wide queries silently
    lost results. A fixed degree grid keeps neighbour arithmetic exact; the
    query simply widens its column sweep as it moves away from the equator,
    where meridians converge.

    Columns wrap at the antimeridian, so a query at +179.9° finds points at
    -179.9° without special-casing.

    The index stores *positions* into the original feature list, so it stays
    valid as long as that list is not reordered. ``GeoEngine`` rebuilds it
    whenever features change.
    """

    __slots__ = ("cell_size_km", "_cells", "_points", "_count", "_step", "_columns")

    def __init__(
        self,
        features: Iterable[Feature],
        cell_size_km: Optional[float] = None,
    ) -> None:
        self._points: List[Tuple[float, float, int]] = []

        for i, feat in enumerate(features):
            pt = _feature_point(feat)
            if pt is not None:
                self._points.append((pt[0], pt[1], i))

        self._count = len(self._points)
        self.cell_size_km = float(cell_size_km or self._auto_cell_size())

        # One step in degrees, used for both axes.
        self._step = self.cell_size_km / KM_PER_DEGREE_LAT
        self._columns = max(1, int(math.ceil(360.0 / self._step)))

        self._cells: Dict[Tuple[int, int], List[Tuple[float, float, int]]] = {}
        for lat, lng, i in self._points:
            self._cells.setdefault(self._cell_of(lat, lng), []).append((lat, lng, i))

    # ── construction helpers ────────────────────────────────────────────────

    def _auto_cell_size(self) -> float:
        """
        Pick a cell size that puts roughly a handful of points per cell.

        Too small and the dictionary explodes; too large and every query
        degenerates into a full scan.
        """
        if self._count < 2:
            return 10.0

        lats = [p[0] for p in self._points]
        lngs = [p[1] for p in self._points]
        span_lat = (max(lats) - min(lats)) * KM_PER_DEGREE_LAT
        mid_lat = (max(lats) + min(lats)) / 2.0
        span_lng = (max(lngs) - min(lngs)) * km_per_degree_lng(mid_lat)

        area = max(span_lat, 0.001) * max(span_lng, 0.001)
        target_cells = max(1.0, self._count / 4.0)
        size = math.sqrt(area / target_cells)
        return min(500.0, max(0.05, size))

    def _cell_of(self, lat: float, lng: float) -> Tuple[int, int]:
        row = int(math.floor(lat / self._step))
        col = int(math.floor(lng / self._step)) % self._columns
        return (row, col)

    def _column_reach(self, lat: float, radius_km: float) -> int:
        """
        How many columns either side of the query a search radius can reach.

        This is the longitude half-width of a spherical cap:
        ``asin(sin δ / cos φ)``. The special case that matters is a cap
        touching a pole, then *every* longitude is within reach, because the
        short way round goes over the top. Bounding the sweep by
        ``radius / km-per-degree`` instead would quietly drop points sitting
        just across the pole from the query.
        """
        delta = radius_km / EARTH_RADIUS_KM              # angular radius, radians
        lat_rad = math.radians(lat)

        if abs(lat_rad) + delta >= math.pi / 2:
            return self._columns                          # the cap covers a pole

        ratio = math.sin(delta) / math.cos(lat_rad)
        if ratio >= 1.0:
            return self._columns

        span_deg = math.degrees(math.asin(ratio))
        reach = int(math.ceil(span_deg / self._step)) + 1
        return min(reach, self._columns // 2 + 1)

    # ── queries ─────────────────────────────────────────────────────────────

    def within(self, point: Coord, radius_km: float) -> List[Tuple[float, int]]:
        """
        Every indexed feature within ``radius_km``, as ``(distance_km, position)``
        sorted nearest first.
        """
        if self._count == 0 or radius_km <= 0:
            return []

        lat, lng = point
        lat_reach = int(math.ceil(radius_km / KM_PER_DEGREE_LAT / self._step)) + 1

        # Probing more cells than the grid even has is slower than reading the
        # grid end to end, so switch strategies rather than grinding through
        # millions of empty coordinates.
        budget = 4 * len(self._cells) + 64
        if (2 * lat_reach + 1) ** 2 > budget:
            return self._scan_all(point, radius_km)

        col_reach = self._column_reach(lat, radius_km)
        if (2 * lat_reach + 1) * (2 * col_reach + 1) > budget:
            return self._scan_all(point, radius_km)

        base_row, base_col = self._cell_of(lat, lng)
        results: List[Tuple[float, int]] = []
        seen = set()

        for d_row in range(-lat_reach, lat_reach + 1):
            row = base_row + d_row

            for d_col in range(-col_reach, col_reach + 1):
                bucket = self._cells.get((row, (base_col + d_col) % self._columns))
                if not bucket:
                    continue
                for p_lat, p_lng, i in bucket:
                    if i in seen:
                        continue
                    seen.add(i)
                    d = _haversine_km(point, (p_lat, p_lng))
                    if d <= radius_km:
                        results.append((d, i))

        results.sort(key=lambda r: (r[0], r[1]))
        return results

    def _scan_all(self, point: Coord, radius_km: float) -> List[Tuple[float, int]]:
        """Linear fallback. Used when the grid sweep would cost more than it saves."""
        results = [
            (d, i)
            for d, i in (
                (_haversine_km(point, (p[0], p[1])), p[2]) for p in self._points
            )
            if d <= radius_km
        ]
        results.sort(key=lambda r: (r[0], r[1]))
        return results

    def nearest(self, point: Coord, k: int = 1) -> List[Tuple[float, int]]:
        """
        The ``k`` closest features to ``point``.

        Starts with a small search radius and widens it until enough
        candidates turn up, so a dense neighbourhood never scans the whole grid.
        """
        if self._count == 0:
            return []

        k = max(1, int(k))
        radius = self.cell_size_km
        half_earth = math.pi * EARTH_RADIUS_KM

        while radius < half_earth:
            found = self.within(point, radius)
            if len(found) >= k:
                return found[:k]
            radius *= 4

        # Sparse or antipodal data: one honest full scan beats more widening.
        every = sorted(
            ((_haversine_km(point, (p[0], p[1])), p[2]) for p in self._points),
            key=lambda r: (r[0], r[1]),
        )
        return every[:k]

    def in_bbox(self, bbox: Tuple[float, float, float, float]) -> List[int]:
        """Positions of features inside ``(min_lat, min_lng, max_lat, max_lng)``."""
        min_lat, min_lng, max_lat, max_lng = bbox
        return [
            i
            for lat, lng, i in self._points
            if min_lat <= lat <= max_lat and min_lng <= lng <= max_lng
        ]

    # ── introspection ───────────────────────────────────────────────────────

    @property
    def size(self) -> int:
        """Number of indexed features."""
        return self._count

    @property
    def cells(self) -> int:
        """Number of occupied grid cells."""
        return len(self._cells)

    def stats(self) -> Dict[str, Any]:
        """Index health: bucket counts tell you whether the resolution is sane."""
        loads = [len(v) for v in self._cells.values()] or [0]
        return {
            "features": self._count,
            "cells": len(self._cells),
            "cell_size_km": round(self.cell_size_km, 4),
            "avg_per_cell": round(sum(loads) / len(loads), 2),
            "max_per_cell": max(loads),
        }

    def __repr__(self) -> str:
        return (
            f"<SpatialIndex {self._count} features · {len(self._cells)} cells "
            f"· {self.cell_size_km:.2f} km>"
        )


def _feature_point(feature: Feature) -> Optional[Coord]:
    """Representative ``(lat, lng)`` for a feature, or None if it has no geometry."""
    geom = feature.get("geometry") or {}
    if geom.get("type") == "Point":
        coords = geom.get("coordinates")
        if coords and len(coords) >= 2:
            return (float(coords[1]), float(coords[0]))
        return None

    from .geometry import centroid_of  # local import avoids a cycle

    try:
        return centroid_of(geom)
    except Exception:
        return None
