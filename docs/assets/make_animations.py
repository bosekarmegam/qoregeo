#!/usr/bin/env python3
"""
Make the animated maps shown at the top of the README.

    python docs/assets/make_animations.py

The script writes three looping SVG animations to docs/assets/animated/:

measure.svg
    The cities in docs/data/cities.csv appear one after another. A line from
    Delhi to Mumbai then draws itself, and a label shows the distance that
    GeoEngine.distance() returns.
geofence.svg
    The Delhi stores in docs/data/stores.csv appear, a 5 km GeoEngine.buffer()
    zone draws around central Delhi, and the stores inside it (found with
    filter_by_polygon) turn teal one by one while a counter adds them up.
corridor.svg
    The route in docs/data/route.geojson draws itself, a 150 km corridor opens
    around it, and the cities that filter_by_line() keeps light up. The other
    cities stay gray.

How a picture is made
---------------------
1. qoregeo draws the map. GeoEngine.to_svg() renders the data with its
   graticule, scale bar and labels. The line, the zone and the route are
   passed as overlays, so qoregeo frames the map around them as well.
2. The script parses that SVG with xml.etree. It keeps the background, the
   grid and the scale bar as qoregeo wrote them, and reads the pixel position
   of every marker, label and overlay path. Markers come in feature order: a
   Web Mercator fit through all of them checks this, and the script stops
   with an error if the renderer ever draws them differently.
3. The map goes into a rounded white card beside a panel with the code and the
   result, and every moving part gets its own CSS @keyframes rule.

Every number in the pictures comes from qoregeo when the script runs.

Rules for README images
-----------------------
GitHub and PyPI show README images with <img>, so the files use no scripts,
no external fonts or files and no <foreignObject>. Text uses the system font
stack. Each loop lasts 10 or 11 seconds, entrances ease out, and nothing
flashes. Under prefers-reduced-motion: reduce the final frame shows without
motion. Moving parts have at least 3:1 contrast against white (the script
checks this), and the output is the same bytes on every run.

Standard library and qoregeo only. In a checkout the script imports the
qoregeo package next to it, so the pictures match the code.
"""

from __future__ import annotations

import math
import re
import sys
import xml.etree.ElementTree as ET
from itertools import pairwise
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DATA = ROOT / "docs" / "data"
OUT = HERE / "animated"

if (ROOT / "qoregeo" / "__init__.py").is_file():
    sys.path.insert(0, str(ROOT))

from qoregeo import GeoEngine  # noqa: E402

Pt = tuple[float, float]
Box = tuple[float, float, float, float]          # x0, y0, x1, y1
Frames = list[tuple[float, dict[str, str]]]      # (seconds, CSS properties)

# Card layout, in px of the 800 x 450 viewBox.
WIDTH, HEIGHT = 800, 450
MAP_X, MAP_Y, MAP_W, MAP_H = 12, 12, 472, 426    # qoregeo's map, on the left
TEXT_X, TEXT_R = 514, 778                        # the side panel's text column

# Colors. qoregeo draws points in #00d4aa and overlays in #ffd700; both are
# too light on white for the parts that move, so those use darker shades.
WHITE = "#ffffff"
MAP_BG = "#f6f8fa"
BORDER = "#d0d7de"
INK = "#1f2933"
MUTED = "#57606a"
FOOTER = "#6e7781"
TEAL = "#00d4aa"          # qoregeo's point color: the halo behind a selected marker
TEAL_DARK = "#00977a"     # markers and counts
AMBER = "#b7791f"         # lines and zones
AMBER_TEXT = "#8a5a12"    # text set in amber
GRAY = "#7d8590"          # markers and labels that are not selected
MOVING = {"TEAL_DARK": TEAL_DARK, "AMBER": AMBER, "AMBER_TEXT": AMBER_TEXT, "GRAY": GRAY}

SANS = "system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace"
EASE_OUT = "cubic-bezier(.22,.61,.36,1)"
FADE_OUT = (0.93, 0.98)   # the whole picture fades out between these fractions of the loop


# Small helpers

def num(value: float) -> str:
    """A coordinate or a time with at most 2 decimals: 12.50 -> 12.5, -0.00 -> 0."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def esc(text: str) -> str:
    """Text or an attribute value, escaped for XML."""
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))


def luminance(color: str) -> float:
    def channel(i: int) -> float:
        c = int(color[i:i + 2], 16) / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5)


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def check_colors() -> None:
    for name, color in MOVING.items():
        for background in (WHITE, MAP_BG):
            ratio = contrast(color, background)
            if ratio < 3:
                raise SystemExit(f"{name} {color} has {ratio:.2f}:1 contrast on {background}; "
                                 "moving parts need at least 3:1")


def text_width(text: str, size: float, mono: bool = False, bold: bool = False) -> float:
    """A generous estimate of the rendered width (no font metrics in an <img>)."""
    return len(text) * size * (0.61 if mono else 0.6 if bold else 0.56)


def place_of(engine: GeoEngine, name: str) -> tuple[float, float]:
    """(lat, lng) of the feature called ``name``."""
    for feature in engine.get_features():
        if feature["properties"].get("name") == name:
            lng, lat = feature["geometry"]["coordinates"][:2]
            return lat, lng
    raise SystemExit(f"no feature named {name!r}")


# Geometry in map pixels

def length(points: list[Pt]) -> float:
    return sum(math.dist(a, b) for a, b in pairwise(points))


def along(points: list[Pt], fraction: float) -> tuple[Pt, Pt]:
    """The point ``fraction`` of the way along a polyline and the unit direction there."""
    target = fraction * length(points)
    for a, b in pairwise(points):
        step = math.dist(a, b)
        if step and target <= step:
            t = target / step
            return ((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t),
                    ((b[0] - a[0]) / step, (b[1] - a[1]) / step))
        target -= step
    a, b = points[-2], points[-1]
    step = math.dist(a, b) or 1.0
    return b, ((b[0] - a[0]) / step, (b[1] - a[1]) / step)


def position_on(points: list[Pt], p: Pt) -> float:
    """How far along a polyline (0 to 1) its point nearest to ``p`` lies."""
    best, best_at, travelled = math.inf, 0.0, 0.0
    for a, b in pairwise(points):
        dx, dy = b[0] - a[0], b[1] - a[1]
        step2 = dx * dx + dy * dy
        t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / step2 if step2 else 0.0
        t = max(0.0, min(1.0, t))
        d = math.dist(p, (a[0] + dx * t, a[1] + dy * t))
        if d < best - 1e-9:
            best, best_at = d, travelled + t * math.sqrt(step2)
        travelled += math.sqrt(step2)
    return best_at / travelled if travelled else 0.0


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def path_points(d: str) -> list[list[Pt]]:
    """The vertices of each subpath of qoregeo's path data ("M x yL x y x y...Z")."""
    out = []
    for chunk in d.split("M")[1:]:
        values = [float(v) for v in _NUMBER.findall(chunk)]
        points = list(zip(values[0::2], values[1::2], strict=True))
        if chunk.rstrip().endswith("Z") and points:
            points.append(points[0])
        out.append(points)
    return out


class Space:
    """Boxes already taken on the map, so that added text stays clear of them."""

    def __init__(self) -> None:
        self.boxes: list[Box] = []

    def add(self, box: Box) -> None:
        self.boxes.append(box)

    def free(self, box: Box, gap: float = 3.0) -> bool:
        x0, y0, x1, y1 = box
        if x0 < 6 or y0 < 6 or x1 > MAP_W - 6 or y1 > MAP_H - 6:
            return False
        return not any(x0 < b[2] + gap and b[0] < x1 + gap and y0 < b[3] + gap and b[1] < y1 + gap
                       for b in self.boxes)


# Motion

class Motion:
    """One loop of ``period`` seconds in which each moving element has its own @keyframes."""

    def __init__(self, period: float) -> None:
        self.period = period
        self.rules: list[str] = []

    def at(self, seconds: float) -> str:
        if not -1e-9 <= seconds <= self.period + 1e-9:
            raise SystemExit(f"{seconds:g} s is outside the {self.period:g} s loop")
        return num(100 * seconds / self.period) + "%"

    def keyframes(self, frames: Frames) -> str:
        """
        The class names for an element that moves through ``frames``. Each CSS
        property keeps its first value until the first frame that sets it, and its
        last value until the end of the loop.
        """
        names = list(dict.fromkeys(p for _, props in frames for p in props))
        first = {p: next(props[p] for _, props in frames if p in props) for p in names}
        last = {p: next(props[p] for _, props in reversed(frames) if p in props) for p in names}
        stops = [(t, dict(props)) for t, props in frames]
        stops[0] = (stops[0][0], {**first, **stops[0][1]})
        stops[-1] = (stops[-1][0], {**stops[-1][1], **last})
        body = []
        for i, (t, props) in enumerate(stops):
            selector = self.at(t)
            if i == 0 and selector != "0%":
                selector = "0%," + selector
            if i == len(stops) - 1 and selector != "100%":
                selector += ",100%"
            body.append(selector + "{" + ";".join(f"{k}:{v}" for k, v in props.items()) + "}")
        name = f"k{len(self.rules)}"
        self.rules.append(f".{name}{{animation-name:{name}}}@keyframes {name}{{{''.join(body)}}}")
        return f"a {name}"

    def appear(self, t: float, duration: float = 0.5, grow: bool = True) -> str:
        hidden, shown = {"opacity": "0"}, {"opacity": "1"}
        if grow:
            hidden["transform"], shown["transform"] = "scale(.3)", "scale(1)"
        return self.keyframes([(t, hidden), (t + duration, shown)])

    def draw(self, t: float, duration: float, path_length: float) -> tuple[str, str]:
        """
        Class and attributes for a stroke of ``path_length`` px that draws itself
        from its first vertex. One dash covers the whole path; the offsets keep both
        ends away from a dash boundary, so no round cap shows before the line does.
        """
        dash = path_length + 6
        css = self.keyframes([(t, {"stroke-dashoffset": f"{num(dash + 2)}px"}),
                              (t + duration, {"stroke-dashoffset": "0px"})])
        return css, f'stroke-dasharray="{num(dash)} {num(dash)}"'

    def step(self, t: float) -> str:
        """A panel step: faint until ``t``, then on until the picture fades out."""
        end, gone = (f * self.period for f in FADE_OUT)
        return self.keyframes([(t, {"opacity": ".3"}), (t + 0.4, {"opacity": "1"}),
                               (end, {"opacity": "1"}), (gone, {"opacity": ".3"})])

    def css(self) -> str:
        p = num(self.period)
        end, gone = (num(100 * f) for f in FADE_OUT)
        return "\n".join([
            f".ui{{font-family:{SANS}}}",
            f".mono{{font-family:{MONO}}}",
            f".a{{animation-duration:{p}s;animation-iteration-count:infinite;"
            f"animation-timing-function:{EASE_OUT};transform-box:fill-box;transform-origin:center}}",
            f".cycle{{animation:cycle {p}s linear infinite}}",
            f"@keyframes cycle{{0%,{end}%{{opacity:1}}{gone}%,100%{{opacity:0}}}}",
            *self.rules,
            "@media (prefers-reduced-motion:reduce){*{animation:none!important}}",
        ])


# qoregeo's map

class QMap:
    """GeoEngine.to_svg() output for the left half of the card, parsed into layers."""

    MOVING_LAYERS = ("qg-shapes", "qg-points", "qg-overlays", "qg-overlay-points", "qg-labels")
    DROPPED = ("qg-title", "qg-credit")      # the card has its own title and credit

    def __init__(self, engine: GeoEngine, overlays: list[dict[str, Any]] | None = None,
                 labels: str | None = None) -> None:
        svg = engine.to_svg(width=MAP_W, height=MAP_H, overlays=overlays, background=MAP_BG,
                            labels=labels)
        self.root = ET.fromstring(svg)
        for el in self.root.iter():
            el.tag = el.tag.rpartition("}")[2]
        self.features = engine.get_features()
        self.markers = [(float(c.get("cx", "")), float(c.get("cy", "")))
                        for c in self.layer("qg-points", required=True)]
        self.radius = float(self.layer("qg-points", required=True)[0].get("r", "4"))
        self._check_order()

    def layer(self, gid: str, required: bool = False) -> ET.Element:
        for el in self.root:
            if el.get("id") == gid:
                return el
        if required:
            raise SystemExit(f"qoregeo's SVG has no {gid!r} layer: update {Path(__file__).name}")
        return ET.Element("g")

    def _check_order(self) -> None:
        """
        Fit Web Mercator through every marker: x grows with the longitude and y
        falls with the Mercator latitude, at one shared scale. If the markers are
        in feature order the fit is exact to the 0.01 px that qoregeo rounds to;
        otherwise stop instead of animating the wrong points.
        """
        places = [f["geometry"]["coordinates"] for f in self.features]
        if len(places) != len(self.markers):
            raise SystemExit(f"{len(self.markers)} markers for {len(places)} features")
        us = [math.radians(lng) for lng, _ in places]
        vs = [math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) for _, lat in places]
        xs = [x for x, _ in self.markers]
        ys = [y for _, y in self.markers]
        n = len(us)
        mu, mv, mx, my = sum(us) / n, sum(vs) / n, sum(xs) / n, sum(ys) / n
        b = (sum((x - mx) * (u - mu) for x, u in zip(xs, us, strict=True))
             - sum((y - my) * (v - mv) for y, v in zip(ys, vs, strict=True))) / (
            sum((u - mu) ** 2 for u in us) + sum((v - mv) ** 2 for v in vs))
        a, c = mx - b * mu, my + b * mv
        worst = max(math.hypot(a + b * u - x, c - b * v - y)
                    for u, v, x, y in zip(us, vs, xs, ys, strict=True))
        if worst > 0.05:
            raise SystemExit(f"markers are not in feature order (fit is off by {worst:.2f} px)")
        self.px_per_radian = b

    def px_per_km(self, engine: GeoEngine, lat: float) -> float:
        """Map pixels per kilometer at latitude ``lat`` (the same in every direction)."""
        km_per_degree = engine.distance((lat, 0.0), (lat, 1.0))
        return self.px_per_radian * math.pi / 180 / km_per_degree

    def overlay_paths(self) -> list[ET.Element]:
        return list(self.layer("qg-overlays", required=True))

    def overlay_markers(self) -> list[Pt]:
        return [(float(c.get("cx", "")), float(c.get("cy", "")))
                for c in self.layer("qg-overlay-points")]

    def labels(self) -> ET.Element:
        return self.layer("qg-labels")

    def base(self) -> str:
        """The layers that stay still: background, grid, grid labels, scale bar."""
        keep = [el for el in self.root
                if el.tag not in ("title", "desc")
                and el.get("id") not in self.MOVING_LAYERS + self.DROPPED]
        return "\n".join(ET.tostring(el, encoding="unicode") for el in keep)

    def taken(self) -> Space:
        """Markers, labels, grid labels and the scale bar, as boxes."""
        space = Space()
        reach = self.radius + 3
        for x, y in self.markers + self.overlay_markers():
            space.add((x - reach, y - reach, x + reach, y + reach))
        for gid, size in (("qg-labels", 11.0), ("qg-grid-labels", 10.0)):
            for t in self.layer(gid):
                x, y, w = float(t.get("x", "")), float(t.get("y", "")), \
                    text_width(t.text or "", size)
                anchor = t.get("text-anchor", "start")
                x0 = x - w if anchor == "end" else x - w / 2 if anchor == "middle" else x
                space.add((x0, y - size * 0.8, x0 + w, y + size * 0.25))
        bar = list(self.layer("qg-scale-bar").iter("rect"))
        if bar:
            x0 = min(float(r.get("x", "")) for r in bar)
            x1 = max(float(r.get("x", "")) + float(r.get("width", "")) for r in bar)
            y1 = max(float(r.get("y", "")) + float(r.get("height", "")) for r in bar)
            space.add((x0, y1 - 24, x1, y1))
        return space


def label_texts(qmap: QMap, names: list[str]) -> dict[int, ET.Element]:
    """
    qoregeo's label elements by feature index, matched on their text (``names``
    holds each feature's label value). qoregeo skips labels that would collide.
    """
    out = {}
    for t in qmap.labels():
        if (t.text or "") not in names:
            raise SystemExit(f"label {t.text!r} matches no feature")
        out[names.index(t.text or "")] = t
    return out


# The card

def panel(motion: Motion, title: str, steps: list[tuple[float, str, str]],
          results: list[tuple[float, str, str, str]], caption: str) -> str:
    """
    The right half: a title, numbered steps (code and a note) that light up in
    turn, and the result. ``results`` holds (seconds, number, rest, color): each
    one replaces the previous, so a list of them counts up.
    """
    step_gap, title_size, result_size = 58, 22, 38
    top = 0.0                                     # baselines below, relative to the title
    rows = [top + 50 + i * step_gap for i in range(len(steps))]
    result_y = rows[-1] + 19 + 64
    caption_y = result_y + 26
    height = caption_y + 5 - (top - title_size * 0.75)
    shift = (HEIGHT - 20 - height) / 2 + title_size * 0.75

    def y(v: float) -> str:
        return num(v + shift)

    widest = max([text_width(code, 13, mono=True) + 30 for _, code, _ in steps]
                 + [text_width(note, 12.5) + 30 for _, _, note in steps]
                 + [text_width(title, title_size, bold=True), text_width(caption, 13)])
    if TEXT_X + widest > TEXT_R:
        raise SystemExit(f"the panel text is too wide: {title!r}")

    out = [f'<text x="{TEXT_X}" y="{y(top)}" font-size="{title_size}" font-weight="600" '
           f'fill="{INK}">{esc(title)}</text>']
    for i, ((t, code, note), row) in enumerate(zip(steps, rows, strict=True), 1):
        out.append(
            f'<g class="{motion.step(t)}">'
            f'<circle cx="{TEXT_X + 10}" cy="{y(row - 4.5)}" r="10" fill="{TEAL_DARK}"/>'
            f'<text x="{TEXT_X + 10}" y="{y(row)}" text-anchor="middle" font-size="11.5" '
            f'font-weight="700" fill="{WHITE}">{i}</text>'
            f'<text class="mono" x="{TEXT_X + 30}" y="{y(row)}" font-size="13" '
            f'fill="{INK}">{esc(code)}</text>'
            f'<text x="{TEXT_X + 30}" y="{y(row + 19)}" font-size="12.5" '
            f'fill="{MUTED}">{esc(note)}</text></g>')
    shown = []
    for i, (t, number, rest, color) in enumerate(results):
        nxt = results[i + 1][0] if i + 1 < len(results) else None
        rise = 0.4 if i == 0 else 0.15
        frames: Frames = [(t, {"opacity": "0", "transform": "translateY(8px)"}),
                          (t + rise, {"opacity": "1", "transform": "translateY(0)"})]
        if nxt is not None:
            # The old value is gone by the time the next one starts: no double image.
            if nxt - 0.08 < t + rise:
                raise SystemExit("results follow each other too closely")
            frames += [(nxt - 0.08, {"opacity": "1"}), (nxt, {"opacity": "0"})]
        hidden = ' opacity="0"' if nxt is not None else ""
        shown.append(
            f'<text class="{motion.keyframes(frames)}"{hidden} x="{TEXT_X}" y="{y(result_y)}" '
            f'font-size="{result_size}" font-weight="700" fill="{INK}">'
            f'<tspan fill="{color}">{esc(number)}</tspan>{esc(rest)}</text>')
    last = results[-1][0]
    shown.append(f'<text class="{motion.appear(last, 0.5, grow=False)}" x="{TEXT_X}" '
                 f'y="{y(caption_y)}" font-size="13" fill="{MUTED}">{esc(caption)}</text>')
    out.append('<g class="cycle">' + "".join(shown) + "</g>")
    return '<g class="ui">\n' + "\n".join(out) + "\n</g>"


def card(name: str, title: str, desc: str, motion: Motion, qmap: QMap, moving: list[str],
         side: str, defs: str = "") -> str:
    return "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="{name}-title {name}-desc">',
        f'<title id="{name}-title">{esc(title)}</title>',
        f'<desc id="{name}-desc">{esc(desc)}</desc>',
        "<style>", motion.css(), "</style>",
        f'<defs><clipPath id="map-clip"><rect width="{MAP_W}" height="{MAP_H}" rx="10"/>'
        f"</clipPath>{defs}</defs>",
        f'<rect x="0.5" y="0.5" width="{WIDTH - 1}" height="{HEIGHT - 1}" rx="14" '
        f'fill="{WHITE}" stroke="{BORDER}"/>',
        f'<g transform="translate({MAP_X} {MAP_Y})" clip-path="url(#map-clip)">',
        qmap.base(),
        '<g class="cycle">', *moving, "</g>",
        "</g>",
        side,
        f'<text class="ui" x="{TEXT_R}" y="{HEIGHT - 18}" text-anchor="end" font-size="11" '
        f'fill="{FOOTER}">made with qoregeo</text>',
        "</svg>",
        "",
    ])


def marker_group(qmap: QMap, fill: str, circles: list[str]) -> str:
    points = qmap.layer("qg-points", required=True)
    return (f'<g fill="{fill}" stroke="{points.get("stroke", WHITE)}" '
            f'stroke-width="{points.get("stroke-width", "1.5")}">' + "".join(circles) + "</g>")


def label_group(qmap: QMap, texts: list[str]) -> str:
    """
    The labels in qoregeo's own style. They keep its font too: qoregeo places
    them with metrics that match Helvetica and Arial, so a wider face could run
    a label off the edge of the map.
    """
    keep = "".join(f' {k}="{esc(v)}"' for k, v in qmap.labels().attrib.items() if k != "id")
    return f"<g{keep}>" + "".join(texts) + "</g>"


def label_text(t: ET.Element, css: str, fill: str | None = None) -> str:
    anchor = t.get("text-anchor")
    return (f'<text class="{css}" x="{t.get("x")}" y="{t.get("y")}"'
            + (f' text-anchor="{anchor}"' if anchor else "")
            + (f' fill="{fill}"' if fill else "") + f">{esc(t.text or '')}</text>")


# 1. measure.svg

def measure() -> tuple[str, list[str]]:
    geo = GeoEngine(verbose=False)
    cities = GeoEngine(verbose=False).load(DATA / "cities.csv")
    names = [f["properties"]["name"] for f in cities.get_features()]
    delhi, mumbai = place_of(cities, "Delhi"), place_of(cities, "Mumbai")
    km = geo.distance(delhi, mumbai)
    heading = geo.bearing(delhi, mumbai, as_degrees=True)
    # The great circle, as 16 segments from GeoEngine.destination().
    arc = [geo.destination(delhi, km * i / 16, heading) for i in range(16)] + [mumbai]
    line = {"type": "LineString", "coordinates": [[lng, lat] for lat, lng in arc]}
    qmap = QMap(cities, overlays=[line], labels="name")
    [line_el] = qmap.overlay_paths()
    [line_px] = path_points(line_el.get("d", ""))

    motion = Motion(10.0)
    shows = [0.4 + 0.2 * i for i in range(len(names))]
    ring_delhi, draw_at, draw_for = shows[-1] + 0.7, shows[-1] + 0.9, 2.0
    ring_mumbai, label_at = draw_at + draw_for - 0.2, draw_at + draw_for + 0.1

    circles = [f'<circle class="{motion.appear(t)}" cx="{num(x)}" cy="{num(y)}" '
               f'r="{num(qmap.radius)}"/>' for t, (x, y) in zip(shows, qmap.markers, strict=True)]
    labels = [label_text(el, motion.appear(shows[i] + 0.1, grow=False))
              for i, el in sorted(label_texts(qmap, names).items())]
    rings = []
    for city, t in (("Delhi", ring_delhi), ("Mumbai", ring_mumbai)):
        x, y = qmap.markers[names.index(city)]
        rings.append(f'<circle class="{motion.appear(t)}" cx="{num(x)}" cy="{num(y)}" r="7.5" '
                     f'fill="none" stroke="{AMBER}" stroke-width="1.75"/>')
    draw_css, dash = motion.draw(draw_at, draw_for, length(line_px))
    path = (f'<path class="{draw_css}" d="{line_el.get("d")}" fill="none" stroke="{AMBER}" '
            f'stroke-width="3" stroke-linecap="round" stroke-linejoin="round" {dash}/>')

    # The distance label sits on the line, like a dimension on a drawing: at the
    # middle if that is clear of markers and labels, else a little up or down it.
    text = f"{round(km):,} km"
    w, h = text_width(text, 15, bold=True) + 22, 26.0
    space = qmap.taken()
    spots = []
    for fraction in (0.5, 0.44, 0.56, 0.38, 0.62, 0.32, 0.68):
        (cx, cy), _ = along(line_px, fraction)
        spots.append((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))
    x0, y0, x1, _ = next((box for box in spots if space.free(box)), spots[0])
    pill = (f'<g class="{motion.appear(label_at)}">'
            f'<rect x="{num(x0)}" y="{num(y0)}" width="{num(w)}" height="{num(h)}" '
            f'rx="{num(h / 2)}" fill="{WHITE}" stroke="{AMBER}" stroke-width="1.5"/>'
            f'<text class="ui" x="{num((x0 + x1) / 2)}" y="{num(y0 + h / 2 + 5)}" '
            f'text-anchor="middle" font-size="15" font-weight="700" fill="{AMBER_TEXT}">'
            f"{esc(text)}</text></g>")

    moving = [path, marker_group(qmap, TEAL_DARK, circles), *rings,
              label_group(qmap, labels), pill]
    side = panel(motion, "Measure a distance",
                 [(0.3, 'geo.load("cities.csv")', f"{len(names)} cities"),
                  (ring_delhi, "geo.distance(delhi, mumbai)", "great circle, in km")],
                 [(label_at, text, "", AMBER_TEXT)], f"Delhi to Mumbai, {km} km")
    svg = card("measure", f"Delhi to Mumbai is {text}",
               f"{len(names)} Indian cities appear one by one. A line then draws from Delhi to "
               f"Mumbai and a label shows {text}, the great-circle distance from "
               f"GeoEngine.distance(). Drawn by qoregeo.", motion, qmap, moving, side)
    return svg, [f"{len(names)} cities", f"distance {km} km, shown as {text}"]


# 2. geofence.svg

RADIUS_KM = 5


def geofence() -> tuple[str, list[str]]:
    geo = GeoEngine(verbose=False)
    stores = GeoEngine(verbose=False).load(DATA / "stores.csv")
    delhi_stores = stores.filter("city", "Delhi")
    center = place_of(GeoEngine(verbose=False).load(DATA / "cities.csv"), "Delhi")
    zone = geo.buffer(center, RADIUS_KM)
    inside = [f["properties"]["name"] for f in delhi_stores.filter_by_polygon(zone)]
    nearest = [f["properties"]["name"]
               for f in delhi_stores.filter_by_radius(center[0], center[1], RADIUS_KM)]
    if sorted(inside) != sorted(nearest):
        raise SystemExit("filter_by_polygon(buffer) and filter_by_radius disagree")

    # Label each store with its neighborhood: the name without the two-word brand.
    features = [{**f, "properties": {**f["properties"],
                                     "area": " ".join(f["properties"]["name"].split()[2:])}}
                for f in delhi_stores.get_features()]
    shown = GeoEngine(verbose=False).load_data(features)
    names = [f["properties"]["name"] for f in shown.get_features()]
    hub = {"type": "Point", "coordinates": [center[1], center[0]]}
    qmap = QMap(shown, overlays=[zone, hub], labels="area")
    zone_el = next(el for el in qmap.overlay_paths() if el.get("d", "").endswith("Z"))
    [ring_px] = path_points(zone_el.get("d", ""))
    [(hx, hy)] = qmap.overlay_markers()
    dashes = qmap.layer("qg-overlays").get("stroke-dasharray", "6 4")

    motion = Motion(11.0)
    shows = [0.4 + 0.15 * i for i in range(len(names))]
    hub_at = shows[-1] + 0.6
    trace_at, trace_for = hub_at + 0.3, 1.8
    radius_at = trace_at + trace_for + 0.1
    count_at = radius_at + 0.3
    lights = {names.index(n): count_at + 0.5 + 0.5 * k for k, n in enumerate(nearest)}

    circles, halos = [], []
    for i, (x, y) in enumerate(qmap.markers):
        t = shows[i]
        if i in lights:
            h = lights[i]
            css = motion.keyframes([
                (t, {"opacity": "0", "transform": "scale(.3)", "fill": GRAY}),
                (t + 0.5, {"opacity": "1", "transform": "scale(1)"}),
                (h, {"fill": GRAY, "transform": "scale(1)"}),
                (h + 0.25, {"fill": TEAL_DARK, "transform": "scale(1.35)"}),
                (h + 0.6, {"transform": "scale(1)"})])
            circles.append(f'<circle class="{css}" cx="{num(x)}" cy="{num(y)}" '
                           f'r="{num(qmap.radius)}" fill="{TEAL_DARK}"/>')
            halos.append(f'<circle class="{motion.appear(h, 0.6)}" cx="{num(x)}" cy="{num(y)}" '
                         f'r="9"/>')
        else:
            circles.append(f'<circle class="{motion.appear(t)}" cx="{num(x)}" cy="{num(y)}" '
                           f'r="{num(qmap.radius)}"/>')
    ink = qmap.labels().get("fill", INK)
    labels = []
    areas = [f["properties"]["area"] for f in shown.get_features()]
    for i, el in sorted(label_texts(qmap, areas).items()):
        if i in lights:
            css = motion.keyframes([(shows[i] + 0.1, {"opacity": "0", "fill": GRAY}),
                                    (shows[i] + 0.6, {"opacity": "1"}),
                                    (lights[i], {"fill": GRAY}), (lights[i] + 0.4, {"fill": ink})])
            labels.append(label_text(el, css))
        else:
            labels.append(label_text(el, motion.appear(shows[i] + 0.1, grow=False), GRAY))

    # The zone: qoregeo's dashed outline, uncovered by a mask whose solid stroke
    # draws itself, so the dashes stay put while the circle closes. An unmasked
    # copy fades in on top when the trace ends, so a browser that does not repaint
    # an animated mask still shows the zone.
    trace_css, trace_dash = motion.draw(trace_at, trace_for, length(ring_px))
    defs = (f'<mask id="zone-trace" maskUnits="userSpaceOnUse" x="0" y="0" width="{MAP_W}" '
            f'height="{MAP_H}"><path class="{trace_css}" d="{zone_el.get("d")}" fill="none" '
            f'stroke="{WHITE}" stroke-width="8" {trace_dash}/></mask>')
    zone_fill = (f'<path class="{motion.appear(trace_at + trace_for - 0.4, 0.6, grow=False)}" '
                 f'd="{zone_el.get("d")}" fill="{AMBER}" fill-opacity=".08"/>')
    outline = (f'd="{zone_el.get("d")}" fill="none" stroke="{AMBER}" stroke-width="2" '
               f'stroke-dasharray="{dashes}" stroke-linejoin="round"')
    settle = motion.appear(trace_at + trace_for, 0.3, grow=False)
    zone_line = f'<path {outline} mask="url(#zone-trace)"/><path class="{settle}" {outline}/>'
    hub_mark = (f'<g class="{motion.appear(hub_at)}"><circle cx="{num(hx)}" cy="{num(hy)}" r="7" '
                f'fill="none" stroke="{INK}" stroke-width="1.5"/><circle cx="{num(hx)}" '
                f'cy="{num(hy)}" r="2.5" fill="{INK}"/></g>')

    # The radius, toward the side of the circle with room for its label.
    space = qmap.taken()
    text = f"{RADIUS_KM} km"
    tw, th = text_width(text, 12, bold=True), 12.0
    sides = [max(ring_px, key=lambda p: p[0]), min(ring_px, key=lambda p: p[0]),
             max(ring_px, key=lambda p: p[1]), min(ring_px, key=lambda p: p[1])]
    options = []
    for ex, ey in sides:
        mx, my = (hx + ex) / 2, (hy + ey) / 2
        if abs(ex - hx) > abs(ey - hy):            # east or west: label above the line
            box = (mx - tw / 2, my - 5 - th, mx + tw / 2, my - 5)
        else:                                      # north or south: label beside it
            box = (mx + 6, my - th / 2, mx + 6 + tw, my + th / 2)
        options.append(((ex, ey), box))
    (ex, ey), box = next((o for o in options if space.free(o[1])), options[0])
    radius = (f'<g class="{motion.appear(radius_at, grow=False)}">'
              f'<path d="M{num(hx)} {num(hy)}L{num(ex)} {num(ey)}" stroke="{AMBER}" '
              f'stroke-width="1.5"/><text class="ui" x="{num(box[0])}" y="{num(box[3] - 2)}" '
              f'font-size="12" font-weight="700" fill="{AMBER_TEXT}" stroke="{WHITE}" '
              f'stroke-width="3" stroke-linejoin="round" paint-order="stroke">'
              f"{esc(text)}</text></g>")

    moving = [zone_fill, zone_line, radius, hub_mark,
              f'<g fill="{TEAL}" fill-opacity=".35">' + "".join(halos) + "</g>",
              marker_group(qmap, GRAY, circles), label_group(qmap, labels)]
    total = len(names)
    counts = [(count_at, "0", f" of {total}", TEAL_DARK)]
    counts += [(lights[names.index(n)], str(k), f" of {total}", TEAL_DARK)
               for k, n in enumerate(nearest, 1)]
    steps = [(0.3, 'geo.load("stores.csv")',
              f"{total} of the {stores.count()} stores are in Delhi"),
             (hub_at, f"zone = geo.buffer(center, {RADIUS_KM})",
              f"{RADIUS_KM} km around central Delhi"),
             (count_at, ".filter_by_polygon(zone)", "keeps the stores inside it")]
    side = panel(motion, "Filter by a zone", steps, counts, f"Delhi stores within {RADIUS_KM} km")
    found = len(nearest)
    svg = card("geofence", f"{found} of {total} Delhi stores are within {RADIUS_KM} km",
               f"{total} stores in Delhi appear. A {RADIUS_KM} km zone from GeoEngine.buffer() "
               f"draws around central Delhi, and the {found} stores inside it turn teal one by "
               f"one. Drawn by qoregeo.", motion, qmap, moving, side, defs)
    return svg, [f"{total} Delhi stores of {stores.count()}", f"{found} within {RADIUS_KM} km: "
                 + ", ".join(nearest)]


# 3. corridor.svg

CORRIDOR_KM = 150


def corridor() -> tuple[str, list[str]]:
    geo = GeoEngine(verbose=False)
    cities = GeoEngine(verbose=False).load(DATA / "cities.csv")
    route = GeoEngine(verbose=False).load(DATA / "route.geojson").get_features()[0]
    route_km = geo.length(route["geometry"])
    names = [f["properties"]["name"] for f in cities.get_features()]
    near = [f["properties"]["name"] for f in cities.filter_by_line(route, CORRIDOR_KM)]
    qmap = QMap(cities, overlays=[route], labels="name")
    [route_el] = qmap.overlay_paths()
    [route_px] = path_points(route_el.get("d", ""))
    lats = [lat for _, lat in route["geometry"]["coordinates"]]
    band = 2 * CORRIDOR_KM * qmap.px_per_km(geo, (min(lats) + max(lats)) / 2)

    motion = Motion(11.0)
    shows = [0.4 + 0.13 * i for i in range(len(names))]
    draw_at, draw_for = shows[-1] + 0.7, 2.4
    band_at, band_for = draw_at + draw_for + 0.1, 0.9
    count_at = band_at + 0.6
    order = sorted((names.index(n) for n in near),
                   key=lambda i: (position_on(route_px, qmap.markers[i]), i))
    lights = {i: count_at + 0.5 + 0.45 * k for k, i in enumerate(order)}

    circles, halos = [], []
    for i, (x, y) in enumerate(qmap.markers):
        if i in lights:
            h = lights[i]
            css = motion.keyframes([
                (shows[i], {"opacity": "0", "transform": "scale(.3)", "fill": GRAY}),
                (shows[i] + 0.5, {"opacity": "1", "transform": "scale(1)"}),
                (h, {"fill": GRAY, "transform": "scale(1)"}),
                (h + 0.25, {"fill": TEAL_DARK, "transform": "scale(1.35)"}),
                (h + 0.6, {"transform": "scale(1)"})])
            circles.append(f'<circle class="{css}" cx="{num(x)}" cy="{num(y)}" '
                           f'r="{num(qmap.radius)}" fill="{TEAL_DARK}"/>')
            halos.append(f'<circle class="{motion.appear(h, 0.6)}" cx="{num(x)}" cy="{num(y)}" '
                         f'r="9"/>')
        else:
            circles.append(f'<circle class="{motion.appear(shows[i])}" cx="{num(x)}" '
                           f'cy="{num(y)}" r="{num(qmap.radius)}"/>')
    ink = qmap.labels().get("fill", INK)
    labels = []
    for i, el in sorted(label_texts(qmap, names).items()):
        if i in lights:
            css = motion.keyframes([(shows[i] + 0.1, {"opacity": "0", "fill": GRAY}),
                                    (shows[i] + 0.6, {"opacity": "1"}),
                                    (lights[i], {"fill": GRAY}), (lights[i] + 0.4, {"fill": ink})])
            labels.append(label_text(el, css))
        else:
            labels.append(label_text(el, motion.appear(shows[i] + 0.1, grow=False), GRAY))

    draw_css, dash = motion.draw(draw_at, draw_for, length(route_px))
    path = (f'<path class="{draw_css}" d="{route_el.get("d")}" fill="none" stroke="{AMBER}" '
            f'stroke-width="3" stroke-linecap="round" stroke-linejoin="round" {dash}/>')
    band_css = motion.keyframes([(band_at, {"stroke-width": "0px", "opacity": "0"}),
                                 (band_at + band_for, {"stroke-width": f"{num(band)}px",
                                                       "opacity": "1"})])
    corridor_band = (f'<path class="{band_css}" d="{route_el.get("d")}" fill="none" '
                     f'stroke="{AMBER}" stroke-opacity=".14" stroke-width="{num(band)}" '
                     f'stroke-linecap="round" stroke-linejoin="round"/>')

    moving = [corridor_band, path,
              f'<g fill="{TEAL}" fill-opacity=".35">' + "".join(halos) + "</g>",
              marker_group(qmap, GRAY, circles), label_group(qmap, labels)]
    total = len(names)
    counts = [(count_at, "0", f" of {total}", TEAL_DARK)]
    counts += [(lights[i], str(k), f" of {total}", TEAL_DARK) for k, i in enumerate(order, 1)]
    steps = [(0.3, 'geo.load("cities.csv")', f"{total} cities"),
             (draw_at, 'geo.load("route.geojson")', f"Delhi to Mumbai, {round(route_km):,} km"),
             (band_at, f".filter_by_line(route, {CORRIDOR_KM})",
              f"within {CORRIDOR_KM} km of the route")]
    side = panel(motion, "Search along a route", steps, counts,
                 f"cities within {CORRIDOR_KM} km of the route")
    found = len(near)
    svg = card("corridor", f"{found} of {total} cities are within {CORRIDOR_KM} km of the route",
               f"The Delhi to Mumbai route draws itself, a {CORRIDOR_KM} km corridor opens around "
               f"it, and the {found} cities that filter_by_line() keeps light up. The other "
               f"cities stay gray. Drawn by qoregeo.", motion, qmap, moving, side)
    named = [names[i] for i in order]
    return svg, [f"route {route_km} km, shown as {round(route_km):,} km",
                 f"{found} of {total} cities within {CORRIDOR_KM} km: " + ", ".join(named)]


def main() -> None:
    check_colors()
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in (("measure", measure), ("geofence", geofence), ("corridor", corridor)):
        svg, facts = build()
        path = OUT / f"{name}.svg"
        data = svg.encode("ascii", "xmlcharrefreplace")
        path.write_bytes(data)
        print(f"{path.relative_to(ROOT)}  {len(data) / 1024:.1f} KB  " + "; ".join(facts))


if __name__ == "__main__":
    main()
