"""
qoregeo.cli
===========
Command-line interface, spatial work without writing a script.

Converting a CSV to GeoJSON, checking a distance, or eyeballing an unfamiliar
dataset are all one-liners that do not deserve a Python file::

    qoregeo info stores.csv
    qoregeo map stores.csv -o stores.html --colour-by region
    qoregeo convert stores.csv stores.geojson
    qoregeo distance 28.6139,77.2090 19.0760,72.8777

Run ``qoregeo --help`` for the full list, or ``python -m qoregeo`` if the
console script is not on your PATH.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence, Tuple

from . import __version__
from .engine import GeoEngine
from .exceptions import QOREgeoError

Coord = Tuple[float, float]


def parse_point(text: str) -> Coord:
    """Parse ``"lat,lng"`` into a coordinate tuple."""
    parts = str(text).replace(" ", "").split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a coordinate. Use lat,lng, for example 28.6139,77.2090"
        )
    try:
        return (float(parts[0]), float(parts[1]))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a coordinate. Both parts must be numbers."
        ) from None


def _load(args: argparse.Namespace) -> GeoEngine:
    return GeoEngine().load(
        args.input,
        lat_col=getattr(args, "lat_col", None),
        lng_col=getattr(args, "lng_col", None),
    )


def _emit(payload: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    elif isinstance(payload, dict):
        width = max((len(str(k)) for k in payload), default=0)
        for key, value in payload.items():
            print(f"{str(key).ljust(width)}  {value}")
    else:
        print(payload)


# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────

def cmd_info(args: argparse.Namespace) -> int:
    """Summarise a dataset: size, geometry mix, columns, extent, spread."""
    geo = _load(args)
    summary = geo.describe()

    if args.json:
        _emit(summary, True)
        return 0

    box = summary["bounds"]
    print(f"File        {args.input}")
    print(f"Features    {summary['features']}")
    print(f"Geometry    {', '.join(f'{k} × {v}' for k, v in summary['geometry_types'].items())}")
    print(f"Columns     {', '.join(summary['columns']) or '(none)'}")
    print(
        f"Extent      lat {box['min_lat']:.4f} … {box['max_lat']:.4f}   "
        f"lng {box['min_lng']:.4f} … {box['max_lng']:.4f}"
    )
    if "dispersion" in summary:
        spread = summary["dispersion"]
        print(
            f"Centre      {spread['centre_lat']:.4f}, {spread['centre_lng']:.4f}   "
            f"(mean spread {spread['mean_km']} km, max {spread['max_km']} km)"
        )

    checks = geo.validate()
    status = "all valid" if checks["invalid"] == 0 else f"{checks['invalid']} invalid"
    print(f"Validity    {status}")
    if checks["issues"]:
        for issue in checks["issues"][:5]:
            print(f"            · feature {issue['index']}: {issue['problem']}")
    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    """Convert between any supported input and output formats."""
    geo = _load(args)
    if args.query:
        geo = geo.query(args.query)
    geo.save(args.output)
    print(f"✅  {geo.count()} features → {args.output}")
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    """Render an interactive HTML map."""
    geo = _load(args)
    if args.query:
        geo = geo.query(args.query)
    geo.map(
        args.output,
        title=args.title,
        basemap=args.basemap,
        colour_by=args.colour_by,
        tooltip_field=args.tooltip,
        cluster=args.cluster,
    )
    return 0


def cmd_heatmap(args: argparse.Namespace) -> int:
    """Render a density heatmap."""
    geo = _load(args)
    if args.query:
        geo = geo.query(args.query)
    geo.heatmap(
        args.output,
        title=args.title,
        intensity_col=args.intensity,
        basemap=args.basemap,
    )
    return 0


def cmd_image(args: argparse.Namespace) -> int:
    """Render a static SVG or PNG, no browser, no network."""
    geo = _load(args)
    if args.query:
        geo = geo.query(args.query)

    if args.output.lower().endswith(".png"):
        geo.png(args.output, title=args.title, width=args.width, height=args.height,
                theme=args.theme)
    else:
        geo.svg(args.output, title=args.title, width=args.width, height=args.height,
                theme=args.theme, label_field=args.label)
    print(f"✅  {geo.count()} features → {args.output}")
    return 0


def cmd_distance(args: argparse.Namespace) -> int:
    """Distance and bearing between two coordinates."""
    geo = GeoEngine()
    km = geo.distance(args.point_a, args.point_b, unit=args.unit, method=args.method)
    degrees = geo.bearing(args.point_a, args.point_b, as_degrees=True)
    compass = geo.bearing(args.point_a, args.point_b)

    if args.json:
        _emit(
            {
                "distance": km,
                "unit": args.unit,
                "method": args.method,
                "bearing_degrees": degrees,
                "direction": compass,
            },
            True,
        )
    else:
        print(f"{km} {args.unit}  ·  {compass} ({degrees}°)")
    return 0


def cmd_nearest(args: argparse.Namespace) -> int:
    """Find the features closest to a coordinate."""
    geo = _load(args)
    matches = geo.knn(args.point, k=args.k, unit=args.unit)

    if args.json:
        _emit(
            [
                {
                    "distance": m["distance"],
                    "unit": args.unit,
                    "index": m["index"],
                    "properties": m["feature"].get("properties", {}),
                }
                for m in matches
            ],
            True,
        )
        return 0

    for rank, match in enumerate(matches, start=1):
        props = match["feature"].get("properties", {})
        label = props.get("name") or props.get("Name") or f"feature {match['index']}"
        print(f"{rank:>3}. {label},  {match['distance']} {args.unit}")
    return 0


def cmd_within(args: argparse.Namespace) -> int:
    """Filter a dataset to the features inside a radius, and optionally save."""
    geo = _load(args)
    nearby = geo.filter_by_radius(
        args.point[0], args.point[1], radius=args.radius, unit=args.unit
    )
    if args.output:
        nearby.save(args.output)
        print(f"✅  {nearby.count()} features within {args.radius} {args.unit} → {args.output}")
        return 0

    for feat in nearby:
        props = feat.get("properties", {})
        label = props.get("name") or props.get("Name") or "feature"
        print(f"{props.get('_distance')} {args.unit}  {label}")
    return 0


def cmd_geocode(args: argparse.Namespace) -> int:
    """Look up an address. Needs network access."""
    from .geocode import Geocoder

    coder = Geocoder(user_agent=args.user_agent)
    result = coder.geocode(args.address, country=args.country)
    if result is None:
        print(f"No match for {args.address!r}", file=sys.stderr)
        return 1

    if args.json:
        _emit(result, True)
    else:
        print(f"{result['lat']}, {result['lng']}    {result['display_name']}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Summarise one column: numeric stats, or value counts for text."""
    geo = _load(args)
    numeric = geo.stats(args.column)
    if numeric.get("count"):
        _emit(numeric, args.json)
    else:
        _emit(geo.value_counts(args.column), args.json)
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    """Assemble the full argument parser."""
    parser = argparse.ArgumentParser(
        prog="qoregeo",
        description="QOREgeo: spatial intelligence for Python, from the command line.",
        epilog="Docs: https://github.com/bosekarmegam/qoregeo",
    )
    parser.add_argument("--version", action="version", version=f"qoregeo {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    def with_input(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument("input", help="input file (.csv, .geojson, .gpx, .kml, .shp, …)")
        sub.add_argument("--lat-col", help="latitude column name (CSV only)")
        sub.add_argument("--lng-col", help="longitude column name (CSV only)")
        return sub

    def with_query(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument(
            "-q", "--query",
            help="filter before running, e.g. \"population > 1e6 and state == 'Goa'\"",
        )
        return sub

    info = with_input(subparsers.add_parser("info", help="summarise a dataset"))
    info.add_argument("--json", action="store_true", help="machine-readable output")
    info.set_defaults(func=cmd_info)

    convert = with_query(with_input(subparsers.add_parser("convert", help="convert between formats")))
    convert.add_argument("output", help="output file, the extension picks the format")
    convert.set_defaults(func=cmd_convert)

    mapper = with_query(with_input(subparsers.add_parser("map", help="render an interactive HTML map")))
    mapper.add_argument("-o", "--output", default="map.html", help="output HTML file")
    mapper.add_argument("-t", "--title", default="QOREgeo Map", help="map title")
    mapper.add_argument(
        "--basemap", default="dark",
        choices=["dark", "light", "streets", "terrain", "satellite"],
    )
    mapper.add_argument("--colour-by", "--color-by", dest="colour_by",
                        help="colour features by this property")
    mapper.add_argument("--tooltip", help="property shown on hover")
    mapper.add_argument("--cluster", action="store_true", default=None,
                        help="force marker clustering on")
    mapper.set_defaults(func=cmd_map)

    heat = with_query(with_input(subparsers.add_parser("heatmap", help="render a density heatmap")))
    heat.add_argument("-o", "--output", default="heatmap.html", help="output HTML file")
    heat.add_argument("-t", "--title", default="QOREgeo Heatmap", help="map title")
    heat.add_argument("--intensity", help="property weighting each point")
    heat.add_argument(
        "--basemap", default="dark",
        choices=["dark", "light", "streets", "terrain", "satellite"],
    )
    heat.set_defaults(func=cmd_heatmap)

    image = with_query(with_input(subparsers.add_parser("image", help="render a static SVG or PNG")))
    image.add_argument("-o", "--output", default="map.svg", help="output .svg or .png file")
    image.add_argument("-t", "--title", default="QOREgeo Map", help="image title")
    image.add_argument("--width", type=int, default=1200)
    image.add_argument("--height", type=int, default=800)
    image.add_argument("--theme", default="dark", choices=["dark", "light"])
    image.add_argument("--label", help="property drawn as a label (SVG only)")
    image.set_defaults(func=cmd_image)

    distance = subparsers.add_parser("distance", help="distance between two coordinates")
    distance.add_argument("point_a", type=parse_point, metavar="lat,lng")
    distance.add_argument("point_b", type=parse_point, metavar="lat,lng")
    distance.add_argument("-u", "--unit", default="km",
                          choices=["km", "miles", "m", "ft", "nm"])
    distance.add_argument("--method", default="haversine",
                          choices=["haversine", "vincenty"])
    distance.add_argument("--json", action="store_true")
    distance.set_defaults(func=cmd_distance)

    nearest = with_input(subparsers.add_parser("nearest", help="find the closest features"))
    nearest.add_argument("point", type=parse_point, metavar="lat,lng")
    nearest.add_argument("-k", type=int, default=5, help="how many results")
    nearest.add_argument("-u", "--unit", default="km",
                         choices=["km", "miles", "m", "ft", "nm"])
    nearest.add_argument("--json", action="store_true")
    nearest.set_defaults(func=cmd_nearest)

    within = with_input(subparsers.add_parser("within", help="features within a radius"))
    within.add_argument("point", type=parse_point, metavar="lat,lng")
    within.add_argument("radius", type=float)
    within.add_argument("-u", "--unit", default="km",
                        choices=["km", "miles", "m", "ft", "nm"])
    within.add_argument("-o", "--output", help="save the result instead of printing")
    within.set_defaults(func=cmd_within)

    stats = with_input(subparsers.add_parser("stats", help="summarise one column"))
    stats.add_argument("column")
    stats.add_argument("--json", action="store_true")
    stats.set_defaults(func=cmd_stats)

    geocode = subparsers.add_parser("geocode", help="look up an address (needs network)")
    geocode.add_argument("address")
    geocode.add_argument(
        "--user-agent", required=True,
        help="identify your app, with contact details. The service requires it",
    )
    geocode.add_argument("--country", help="ISO country code, e.g. in")
    geocode.add_argument("--json", action="store_true")
    geocode.set_defaults(func=cmd_geocode)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """
    Entry point. Returns a process exit code rather than calling ``sys.exit``,
    so it stays testable.
    """
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    try:
        return int(args.func(args) or 0)
    except QOREgeoError as exc:
        # QOREgeo errors already explain themselves; a traceback adds nothing.
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":       # pragma: no cover
    sys.exit(main())
