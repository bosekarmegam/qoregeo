"""
qoregeo.static_map
==================
Static map images — SVG and PNG — rendered without a browser, a plotting
library or a network connection.

The interactive HTML maps are the right output for exploring data. They are
the wrong output for a PDF report, an email attachment, a CI artefact or a
README. Those need a flat image, and getting one normally means matplotlib
or a headless browser.

Both renderers here are self-contained: SVG is text, and the PNG encoder is
built on :mod:`zlib` and :mod:`struct` from the standard library. Data is
projected to Web Mercator — the same projection as web tiles — so shapes
match what the interactive map shows.

There is no basemap imagery: these draw your geometry on a plain background,
because tile imagery would require the network these functions exist to avoid.
"""

from __future__ import annotations

import math
import os
import struct
import zlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .crs import MERCATOR_MAX_LAT
from .exceptions import EmptyDatasetError

Feature = Dict[str, Any]
RGB = Tuple[int, int, int]

#: QORE house palette — teal on near-black, matching the interactive maps.
THEME_DARK = {
    "background": (15, 17, 26),
    "point": (0, 212, 170),
    "line": (0, 212, 170),
    "fill": (0, 212, 170),
    "stroke": (255, 255, 255),
    "text": (200, 200, 210),
    "grid": (40, 44, 60),
}

THEME_LIGHT = {
    "background": (250, 250, 252),
    "point": (0, 150, 122),
    "line": (0, 150, 122),
    "fill": (0, 150, 122),
    "stroke": (40, 40, 50),
    "text": (60, 60, 70),
    "grid": (222, 226, 235),
}

THEMES = {"dark": THEME_DARK, "light": THEME_LIGHT}


# ─────────────────────────────────────────────────────────────────────────────
# Projection
# ─────────────────────────────────────────────────────────────────────────────

class Projector:
    """
    Maps ``(lat, lng)`` onto pixel coordinates for a fixed canvas.

    Fits the data's bounding box into the canvas with padding, preserving
    aspect ratio so nothing is stretched.
    """

    def __init__(
        self,
        bounds: Dict[str, float],
        width: int,
        height: int,
        padding: int = 24,
    ) -> None:
        self.width = int(width)
        self.height = int(height)
        self.padding = int(padding)

        min_lat = max(-MERCATOR_MAX_LAT, min(MERCATOR_MAX_LAT, bounds["min_lat"]))
        max_lat = max(-MERCATOR_MAX_LAT, min(MERCATOR_MAX_LAT, bounds["max_lat"]))

        # Both spans must run low → high so the scale stays positive. Mercator y
        # grows northward while pixel y grows downward, so the flip happens in
        # project(), not here — storing them the other way round silently
        # mirrors the whole image.
        self._x0 = self._merc_x(bounds["min_lng"])
        self._x1 = self._merc_x(bounds["max_lng"])
        self._y0 = self._merc_y(min_lat)          # south edge
        self._y1 = self._merc_y(max_lat)          # north edge

        span_x = self._x1 - self._x0
        span_y = self._y1 - self._y0

        # A single point, or a perfectly straight row of them, has zero span —
        # give it an arbitrary window so the scale stays finite.
        if span_x <= 0:
            self._x0 -= 0.001
            self._x1 += 0.001
            span_x = self._x1 - self._x0
        if span_y <= 0:
            self._y0 -= 0.001
            self._y1 += 0.001
            span_y = self._y1 - self._y0

        usable_w = max(1, self.width - 2 * self.padding)
        usable_h = max(1, self.height - 2 * self.padding)
        self.scale = min(usable_w / span_x, usable_h / span_y)

        self._offset_x = self.padding + (usable_w - span_x * self.scale) / 2
        self._offset_y = self.padding + (usable_h - span_y * self.scale) / 2

    @staticmethod
    def _merc_x(lng: float) -> float:
        return math.radians(lng)

    @staticmethod
    def _merc_y(lat: float) -> float:
        lat = max(-MERCATOR_MAX_LAT, min(MERCATOR_MAX_LAT, lat))
        return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))

    def project(self, lat: float, lng: float) -> Tuple[float, float]:
        """``(lat, lng)`` → ``(x, y)`` pixels, y growing downwards."""
        x = (self._merc_x(lng) - self._x0) * self.scale + self._offset_x
        y = (self._y1 - self._merc_y(lat)) * self.scale + self._offset_y
        return (x, y)

    def project_coord(self, coord: Sequence[float]) -> Tuple[float, float]:
        """GeoJSON ``[lng, lat]`` → ``(x, y)`` pixels."""
        return self.project(coord[1], coord[0])


def bounds_of(features: Sequence[Feature]) -> Dict[str, float]:
    """Bounding box across every coordinate in a feature list."""
    from .geometry import coords_of

    lats: List[float] = []
    lngs: List[float] = []
    for feat in features:
        for lng, lat in coords_of(feat.get("geometry") or {}):
            lats.append(lat)
            lngs.append(lng)

    if not lats:
        raise EmptyDatasetError("<no coordinates to draw>")

    return {
        "min_lat": min(lats),
        "max_lat": max(lats),
        "min_lng": min(lngs),
        "max_lng": max(lngs),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SVG
# ─────────────────────────────────────────────────────────────────────────────

def build_svg(
    features: Sequence[Feature],
    output_path: Optional[str] = None,
    title: str = "QOREgeo Map",
    width: int = 1200,
    height: int = 800,
    theme: str = "dark",
    point_radius: float = 3.5,
    stroke_width: float = 1.4,
    fill_opacity: float = 0.25,
    label_field: Optional[str] = None,
    show_title: bool = True,
) -> str:
    """
    Render features to an SVG document.

    Returns the SVG source, and writes it to ``output_path`` when given.
    SVG is vector, so it scales to any print size and stays a text diff in
    version control.

    Parameters
    ----------
    label_field : draw this property as a text label beside each point
    theme       : ``"dark"`` or ``"light"``
    """
    palette = THEMES.get(theme, THEME_DARK)

    # The caption sits in a strip along the bottom. Shrink the drawing area by
    # that much so the southernmost labels do not collide with it.
    caption_strip = 34 if show_title else 0
    proj = Projector(bounds_of(features), width, height - caption_strip)

    parts: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(title)}">',
        f"<title>{_esc(title)}</title>",
        f'<rect width="{width}" height="{height}" fill="{_hex(palette["background"])}"/>',
    ]

    labels: List[str] = []

    for feat in features:
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if not gtype or coords is None:
            continue

        if gtype == "Point":
            x, y = proj.project_coord(coords)
            parts.append(
                f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{point_radius}" '
                f'fill="{_hex(palette["point"])}" fill-opacity="0.9" '
                f'stroke="{_hex(palette["stroke"])}" stroke-width="0.6"/>'
            )
            if label_field:
                text = feat.get("properties", {}).get(label_field)
                if text is not None:
                    labels.append(
                        f'<text x="{x + point_radius + 3:.2f}" y="{y + 3:.2f}" '
                        f'font-family="system-ui,sans-serif" font-size="10" '
                        f'fill="{_hex(palette["text"])}">{_esc(str(text))}</text>'
                    )

        elif gtype == "MultiPoint":
            for coord in coords:
                x, y = proj.project_coord(coord)
                parts.append(
                    f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{point_radius}" '
                    f'fill="{_hex(palette["point"])}" fill-opacity="0.9"/>'
                )

        elif gtype in ("LineString", "MultiLineString"):
            lines = [coords] if gtype == "LineString" else coords
            for line in lines:
                path = _svg_path(line, proj, close=False)
                if path:
                    parts.append(
                        f'<path d="{path}" fill="none" '
                        f'stroke="{_hex(palette["line"])}" '
                        f'stroke-width="{stroke_width}" stroke-linejoin="round"/>'
                    )

        elif gtype in ("Polygon", "MultiPolygon"):
            polygons = [coords] if gtype == "Polygon" else coords
            for rings in polygons:
                path = " ".join(_svg_path(ring, proj, close=True) for ring in rings)
                if path.strip():
                    parts.append(
                        f'<path d="{path}" fill="{_hex(palette["fill"])}" '
                        f'fill-opacity="{fill_opacity}" fill-rule="evenodd" '
                        f'stroke="{_hex(palette["line"])}" '
                        f'stroke-width="{stroke_width}"/>'
                    )

    parts.extend(labels)

    if show_title:
        parts.append(
            f'<text x="16" y="{height - 16}" font-family="system-ui,sans-serif" '
            f'font-size="13" fill="{_hex(palette["text"])}">'
            f'<tspan font-weight="700" fill="{_hex(palette["point"])}">QOREgeo</tspan>'
            f" · {_esc(title)} · {len(features)} features</text>"
        )

    parts.append("</svg>")
    svg = "\n".join(parts)

    if output_path:
        _ensure_dir(output_path)
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(svg)

    return svg


def _svg_path(
    coords: Sequence[Sequence[float]],
    proj: Projector,
    close: bool,
) -> str:
    if not coords:
        return ""
    points = [proj.project_coord(c) for c in coords]
    path = f"M {points[0][0]:.2f} {points[0][1]:.2f}"
    path += "".join(f" L {x:.2f} {y:.2f}" for x, y in points[1:])
    return path + (" Z" if close else "")


def _hex(rgb: RGB) -> str:
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ─────────────────────────────────────────────────────────────────────────────
# Raster canvas + PNG encoder
# ─────────────────────────────────────────────────────────────────────────────

class Canvas:
    """
    A plain RGB pixel buffer with just enough drawing primitives for maps.

    Deliberately minimal: filled circles for points, anti-aliasing-free
    Bresenham lines with width, and scanline polygon fill. That is the whole
    vocabulary a dot-and-shape map needs.
    """

    def __init__(self, width: int, height: int, background: RGB = (255, 255, 255)) -> None:
        self.width = int(width)
        self.height = int(height)
        self.pixels = bytearray(bytes(background) * (self.width * self.height))

    def set(self, x: int, y: int, colour: RGB, alpha: float = 1.0) -> None:
        """Set one pixel, blending against what's already there when translucent."""
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        i = (y * self.width + x) * 3
        if alpha >= 1.0:
            self.pixels[i : i + 3] = bytes(colour)
            return
        if alpha <= 0.0:
            return
        for channel in range(3):
            existing = self.pixels[i + channel]
            self.pixels[i + channel] = int(
                existing + (colour[channel] - existing) * alpha
            )

    def circle(self, cx: float, cy: float, radius: float, colour: RGB, alpha: float = 1.0) -> None:
        """Filled circle, drawn by testing every pixel in the bounding square."""
        r = max(0.5, float(radius))
        x0, x1 = int(math.floor(cx - r)), int(math.ceil(cx + r))
        y0, y1 = int(math.floor(cy - r)), int(math.ceil(cy + r))
        r_sq = r * r
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r_sq:
                    self.set(x, y, colour, alpha)

    def line(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        colour: RGB,
        thickness: float = 1.0,
        alpha: float = 1.0,
    ) -> None:
        """Bresenham line; thickness above 1 stamps a small disc at each step."""
        xi, yi = int(round(x0)), int(round(y0))
        xf, yf = int(round(x1)), int(round(y1))
        dx = abs(xf - xi)
        dy = -abs(yf - yi)
        sx = 1 if xi < xf else -1
        sy = 1 if yi < yf else -1
        err = dx + dy
        half = max(0.0, (thickness - 1) / 2.0)

        # Guard against pathological coordinates producing a multi-million
        # step loop when a projected point lands far outside the canvas.
        budget = (self.width + self.height) * 4

        while budget > 0:
            budget -= 1
            if half <= 0:
                self.set(xi, yi, colour, alpha)
            else:
                self.circle(xi, yi, half + 0.5, colour, alpha)
            if xi == xf and yi == yf:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                xi += sx
            if e2 <= dx:
                err += dx
                yi += sy

    def polygon(
        self,
        points: Sequence[Tuple[float, float]],
        colour: RGB,
        alpha: float = 0.3,
    ) -> None:
        """Scanline fill using the even-odd rule, so holes come out as holes."""
        if len(points) < 3:
            return

        ys = [p[1] for p in points]
        y_start = max(0, int(math.floor(min(ys))))
        y_end = min(self.height - 1, int(math.ceil(max(ys))))

        for y in range(y_start, y_end + 1):
            crossings = []
            j = len(points) - 1
            for i in range(len(points)):
                y_i, y_j = points[i][1], points[j][1]
                if (y_i > y) != (y_j > y):
                    x = points[i][0] + (y - y_i) / (y_j - y_i) * (points[j][0] - points[i][0])
                    crossings.append(x)
                j = i

            crossings.sort()
            for k in range(0, len(crossings) - 1, 2):
                x_start = max(0, int(math.ceil(crossings[k])))
                x_end = min(self.width - 1, int(math.floor(crossings[k + 1])))
                for x in range(x_start, x_end + 1):
                    self.set(x, y, colour, alpha)

    def to_png(self) -> bytes:
        """
        Encode the buffer as a PNG.

        PNG is a handful of length-prefixed, CRC-checked chunks wrapping
        zlib-compressed scanlines — about thirty lines of standard library
        calls, which is why this needs no imaging dependency.
        """
        raw = bytearray()
        stride = self.width * 3
        for y in range(self.height):
            raw.append(0)                       # filter type 0: None
            raw.extend(self.pixels[y * stride : (y + 1) * stride])

        def chunk(kind: bytes, payload: bytes) -> bytes:
            body = kind + payload
            return struct.pack(">I", len(payload)) + body + struct.pack(
                ">I", zlib.crc32(body) & 0xFFFFFFFF
            )

        header = struct.pack(">2I5B", self.width, self.height, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b"")
        )


def build_png(
    features: Sequence[Feature],
    output_path: str,
    title: str = "QOREgeo Map",
    width: int = 1200,
    height: int = 800,
    theme: str = "dark",
    point_radius: float = 3.0,
    stroke_width: float = 1.5,
    fill_opacity: float = 0.25,
) -> str:
    """
    Render features to a PNG file.

    Same projection and palette as :func:`build_svg`, rasterised instead of
    vector — for places that will not take an SVG.

    Returns the output path.
    """
    palette = THEMES.get(theme, THEME_DARK)
    proj = Projector(bounds_of(features), width, height)
    canvas = Canvas(width, height, palette["background"])

    def draw_ring(ring: Sequence[Sequence[float]], fill: bool) -> None:
        pts = [proj.project_coord(c) for c in ring]
        if fill and len(pts) >= 3:
            canvas.polygon(pts, palette["fill"], fill_opacity)
        for i in range(len(pts) - 1):
            canvas.line(
                pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1],
                palette["line"], stroke_width,
            )

    for feat in features:
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if not gtype or coords is None:
            continue

        if gtype == "Point":
            x, y = proj.project_coord(coords)
            canvas.circle(x, y, point_radius, palette["point"])
        elif gtype == "MultiPoint":
            for coord in coords:
                x, y = proj.project_coord(coord)
                canvas.circle(x, y, point_radius, palette["point"])
        elif gtype == "LineString":
            draw_ring(coords, fill=False)
        elif gtype == "MultiLineString":
            for line in coords:
                draw_ring(line, fill=False)
        elif gtype == "Polygon":
            for ring in coords:
                draw_ring(ring, fill=True)
        elif gtype == "MultiPolygon":
            for rings in coords:
                for ring in rings:
                    draw_ring(ring, fill=True)

    _ensure_dir(output_path)
    with open(output_path, "wb") as handle:
        handle.write(canvas.to_png())
    return output_path


def _ensure_dir(path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
