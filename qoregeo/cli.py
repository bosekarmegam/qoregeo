"""
qoregeo.cli
===========
The ``qoregeo`` command.

    qoregeo info cities.csv
    qoregeo distance 28.6139,77.2090 19.0760,72.8777 --unit miles
    qoregeo map cities.csv -o cities.html --near 28.61,77.21,500 --serve
    qoregeo image cities.csv -o cities.png
    qoregeo export cities.csv -o big.geojson --where "population>=5000000"
    qoregeo serve cities.html
    qoregeo geocode "Gateway of India, Mumbai"
    qoregeo mcp --root ./data                     # MCP server for AI assistants
    qoregeo tools --format anthropic              # tool schemas for the Claude API
    qoregeo call distance '{"from": {"lat": 0, "lng": 0}, "to": {"lat": 1, "lng": 1}}'
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from typing import Any

from ._version import __version__
from .engine import GeoEngine
from .exceptions import GeoFileNotFoundError, QOREgeoError
from .map_builder import tile_presets

_WHERE_RE = re.compile(
    r"^\s*(?P<col>.+?)\s*(?P<op>==|!=|>=|<=|>|<|=|\snot in\s|\sin\s|\scontains\s)\s*(?P<val>.*?)\s*$"
)


class _UsageError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Argument parsing helpers
# ─────────────────────────────────────────────────────────────────────────────

class _Parser(argparse.ArgumentParser):
    """
    argparse that reads ``-33.9,18.4`` or ``-10,-5,10,5`` as a value, not an
    unknown option, on every Python version (3.14 does this already; 3.10-3.13
    only accept plain negative numbers such as ``-33.9``).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._negative_number_matcher = re.compile(r"^-\.?\d")


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number >= 1, e.g. 200 (got {text!r})")
    return value


def _floats(text: str, n: int, what: str, example: str) -> list[float]:
    parts = [p for p in re.split(r"[,\s]+", text.strip()) if p]
    try:
        values = [float(p) for p in parts]
    except ValueError:
        values = []
    if len(values) != n:
        raise _UsageError(f"{what} must be {n} comma-separated numbers, e.g. {example} (got {text!r})")
    return values


def _point(text: str) -> tuple[float, float]:
    lat, lng = _floats(text, 2, "a point", "28.6139,77.2090")
    return lat, lng


def _value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text.strip("'\"")


def _where(text: str) -> dict[str, Any]:
    m = _WHERE_RE.match(text)
    if not m or not m.group("col"):
        raise _UsageError(f"--where must look like 'column>=value', 'name==\"Delhi\"' or "
                          f"'city in [\"Pune\",\"Delhi\"]' (got {text!r})")
    op = m.group("op").strip()
    return {"column": m.group("col").strip(), "op": "==" if op == "=" else op,
            "value": _value(m.group("val"))}


def _filter_args(ns: argparse.Namespace) -> dict[str, Any]:
    args: dict[str, Any] = {}
    if ns.near:
        lat, lng, radius = _floats(ns.near, 3, "--near", "28.61,77.21,25")
        args["near"] = {"lat": lat, "lng": lng, "radius": radius, "unit": ns.unit}
    if ns.bbox:
        args["bbox"] = _floats(ns.bbox, 4, "--bbox (west,south,east,north)", "68,6,98,36")
    if ns.within:
        try:
            with open(ns.within, encoding="utf-8-sig") as fh:
                args["within"] = json.load(fh)
        except ValueError as exc:   # not JSON (a missing file is reported like any other)
            raise _UsageError(f"--within {ns.within}: not valid GeoJSON ({exc})") from None
    if ns.where:
        args["where"] = [_where(w) for w in ns.where]
    if ns.sort:
        args["sort_by"], args["descending"] = ns.sort, ns.desc
    if ns.limit is not None:
        args["limit"] = ns.limit
    return args


def _load(ns: argparse.Namespace) -> GeoEngine:
    from .tools import _apply_filters

    geo = GeoEngine(verbose=False).load(ns.file, lat_col=ns.lat_col, lng_col=ns.lng_col,
                                        infer_types=True)
    return _apply_filters(geo, _filter_args(ns))


def _utf8(stream: Any) -> bool:
    try:
        return codecs.lookup(getattr(stream, "encoding", None) or "ascii").name == "utf-8"
    except LookupError:
        return False


def _print_json(obj: Any) -> None:
    """
    Print strict JSON: NaN / Infinity become null, and the text is pure ASCII
    (\\uXXXX escapes) unless stdout is UTF-8, so any reader can decode it.
    """
    from .tools import _text_safe
    from .utils import _json_safe

    # Lone surrogates (undecodable file names, bad data) become U+FFFD so the
    # output is valid UTF-8 JSON for every parser.
    text = json.dumps(_text_safe(_json_safe(obj)), indent=2, ensure_ascii=not _utf8(sys.stdout),
                      allow_nan=False)
    print(text)


# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────

def _cmd_info(ns: argparse.Namespace) -> int:
    from .tools import ToolContext, call_tool

    path = os.path.realpath(ns.file)   # a symlinked file is read from its real folder
    if not os.path.isfile(path):
        raise GeoFileNotFoundError(ns.file)
    args = {"path": os.path.basename(path), "lat_column": ns.lat_col, "lng_column": ns.lng_col}
    result = call_tool("describe_dataset", {k: v for k, v in args.items() if v is not None},
                       ToolContext(root=os.path.dirname(path), max_file_mb=float("inf")))
    if result.is_error:
        print(f"qoregeo info: {result.data['error']}", file=sys.stderr)
        return 1
    if ns.json:
        _print_json(result.data)
        return 0
    d = result.data
    print(f"{ns.file}: {d['count']} features")
    print("  geometry : " + ", ".join(f"{k} × {v}" for k, v in d["geometry_types"].items()))
    if d.get("bbox"):
        w, s, e, n = d["bbox"]
        print(f"  bbox     : west {w}, south {s}, east {e}, north {n}")
        print(f"  centre   : {d['centroid']['lat']}, {d['centroid']['lng']}")
    print("  columns  : " + ", ".join(f"{k} ({v})" for k, v in d["columns"].items()))
    return 0


def _cmd_distance(ns: argparse.Namespace) -> int:
    geo = GeoEngine(verbose=False)
    a, b = _point(ns.a), _point(ns.b)
    d = geo.distance(a, b, unit=ns.unit, method=ns.method)
    if ns.json:
        _print_json({"distance": d, "unit": ns.unit, "method": ns.method,
                     "bearing": geo.bearing(a, b, as_degrees=True), "compass": geo.bearing(a, b)})
    else:
        print(f"{d} {ns.unit}  (bearing {geo.bearing(a, b, as_degrees=True)}°, {geo.bearing(a, b)})")
    return 0


def _cmd_map(ns: argparse.Namespace) -> int:
    geo = _load(ns)
    geo.verbose = not ns.quiet
    out = ns.output or (os.path.splitext(os.path.basename(ns.file))[0]
                        + ("_heatmap.html" if ns.heatmap else "_map.html"))
    title = ns.title or os.path.basename(ns.file)
    if ns.heatmap:
        geo.heatmap(out, title=title, intensity_col=ns.intensity, tiles=ns.tiles,
                    attribution=ns.attribution)
    else:
        geo.map(out, title=title, tiles=ns.tiles, attribution=ns.attribution)
    if ns.serve:
        from .serve import serve_file
        serve_file(out, port=ns.port, block=True, verbose=not ns.quiet)
    return 0


def _cmd_image(ns: argparse.Namespace) -> int:
    geo = _load(ns)
    geo.verbose = not ns.quiet
    out = ns.output or os.path.splitext(os.path.basename(ns.file))[0] + ".png"
    geo.image(out, width=ns.width, height=ns.height, title=ns.title, labels=ns.labels,
              grid=not ns.no_grid, scale_bar=not ns.no_scale_bar)
    return 0


def _cmd_export(ns: argparse.Namespace) -> int:
    from .engine import _same_file

    if _same_file(ns.output, ns.file):
        raise _UsageError("--output is the input file; write to a different file")
    geo = _load(ns)
    geo.verbose = not ns.quiet
    geo.save(ns.output, csv_safe=not ns.raw_csv)
    return 0


def _cmd_serve(ns: argparse.Namespace) -> int:
    from .serve import serve_file

    serve_file(ns.file, port=ns.port, block=True, open_browser=not ns.no_browser)
    return 0


def _geocoder(ns: argparse.Namespace) -> Any:
    from .geocode import Geocoder

    return Geocoder(provider=ns.provider, url=ns.url, language=ns.language, email=ns.email)


def _cmd_geocode(ns: argparse.Namespace) -> int:
    results = _geocoder(ns).search(ns.query, limit=ns.limit, country_codes=ns.country)
    if ns.json:
        _print_json(results)
    elif not results:
        print("No match.")
        return 1
    else:
        for r in results:
            print(f"{r['lat']:.6f},{r['lng']:.6f}  {r['name']}")
        print(results[0]["attribution"], file=sys.stderr)
    return 0


def _cmd_reverse(ns: argparse.Namespace) -> int:
    lat, lng = _point(ns.point)
    result = _geocoder(ns).reverse(lat, lng)
    if ns.json:
        _print_json(result)
    elif result is None:
        print("No address found.")
        return 1
    else:
        print(result["name"])
    return 0


def _cmd_tiles(ns: argparse.Namespace) -> int:
    for name, label in tile_presets().items():
        print(f"{name:<13} {label}")
    return 0


def _cmd_tools(ns: argparse.Namespace) -> int:
    from .tools import tool_definitions

    _print_json(tool_definitions(ns.format, include_network=not ns.no_network))
    return 0


def _usable_env(name: str) -> str | None:
    """An environment variable's value, unless empty or an unexpanded ``${…}`` placeholder."""
    value = os.environ.get(name, "").strip()
    return value if value and "${" not in value else None


def _default_root(cli_root: str | None) -> str:
    """--root, else $QOREGEO_ROOT, else $CLAUDE_PROJECT_DIR, else the current directory."""
    if cli_root is not None:
        if not cli_root.strip():   # e.g. --root "$DATA_DIR" with the variable unset
            raise _UsageError("--root is empty; pass the folder the tools may use, "
                              "e.g. --root ./data (or leave --root out to use the current folder)")
        return cli_root
    return _usable_env("QOREGEO_ROOT") or _usable_env("CLAUDE_PROJECT_DIR") or os.getcwd()


def _network_allowed(flag: bool) -> bool:
    return flag or (_usable_env("QOREGEO_ALLOW_NETWORK") or "").lower() in ("1", "true", "yes", "on")


def _tool_context(ns: argparse.Namespace) -> Any:
    """The sandbox for `qoregeo call` / `qoregeo mcp`: flags first, then environment variables."""
    from .tools import ToolContext

    url = ns.geocoder_url or _usable_env("QOREGEO_GEOCODER_URL")
    email = ns.geocoder_email or _usable_env("QOREGEO_GEOCODER_EMAIL")
    geocoder = None
    if url or email:
        from .geocode import Geocoder
        geocoder = Geocoder(provider=ns.geocoder, url=url, email=email)
    return ToolContext(root=_default_root(ns.root), allow_write=not ns.read_only,
                       allow_network=bool(url) or _network_allowed(ns.allow_network),
                       max_features=ns.max_features, geocoder=geocoder)


def _read_stdin() -> str:
    """Standard input as UTF-8 (JSON's encoding), whatever the console's code page is."""
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:   # a text-only stream, e.g. in an IDE
        return sys.stdin.read()
    data: bytes = buffer.read()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode(getattr(sys.stdin, "encoding", None) or "utf-8", "replace")


def _cmd_call(ns: argparse.Namespace) -> int:
    from .tools import call_tool

    raw = _read_stdin() if ns.arguments == "-" else ns.arguments
    try:
        arguments = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise _UsageError(f"arguments must be a JSON object: {exc}") from None
    ctx = _tool_context(ns)
    result = call_tool(ns.tool, arguments, ctx)
    _print_json(result.data)
    for i, (mime, blob) in enumerate(result.images):
        if ns.save_images:
            ext = ".png" if mime == "image/png" else ".svg"
            path = f"{ns.tool}_{i}{ext}" if len(result.images) > 1 else f"{ns.tool}{ext}"
            with open(path, "wb") as fh:
                fh.write(blob)
            print(f"image saved -> {path}", file=sys.stderr)
    return 1 if result.is_error else 0


def _cmd_mcp(ns: argparse.Namespace) -> int:
    from .mcp_server import MCPServer

    MCPServer(_tool_context(ns)).serve_stdio()
    return 0


def _skill_source() -> str:
    """The bundled Agent Skill: inside the installed package, or skills/ in a source checkout."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, "_skill", "qoregeo"),
                      os.path.join(os.path.dirname(here), "skills", "qoregeo")):
        if os.path.isfile(os.path.join(candidate, "SKILL.md")):
            return candidate
    raise _UsageError("the bundled skill is missing from this installation")


def _zip_skill(source: str, output: str) -> int:
    """
    The skill as a zip for claude.ai / the Skills API, with the qoregeo package
    itself in scripts/ — so it works in sandboxes without pip or internet.
    """
    import zipfile

    package = os.path.dirname(os.path.abspath(__file__))
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder, prefix in ((source, "qoregeo"), (package, "qoregeo/scripts/qoregeo")):
            for dirpath, dirnames, filenames in os.walk(folder):
                dirnames[:] = sorted(d for d in dirnames if d not in ("__pycache__", "_skill"))
                for name in sorted(filenames):
                    if name.endswith((".pyc", ".pyo")):
                        continue
                    full = os.path.join(dirpath, name)
                    zf.write(full, prefix + "/" + os.path.relpath(full, folder).replace(os.sep, "/"))
    print(f"Skill bundle written -> {output}  "
          "(upload it in Claude: Settings > Capabilities > Skills)")
    return 0


def _is_link(path: str) -> bool:
    """A symlink, or a Windows directory junction (what `npx skills` makes there)."""
    isjunction: Callable[[str], bool] = getattr(os.path, "isjunction", lambda p: False)
    return os.path.islink(path) or isjunction(path)


def _install_skill(source: str, target: str) -> None:
    """
    Copy the skill to ``target``, replacing whatever is there. The copy is made
    next to it first, so a failed copy never leaves a half-written skill behind.
    """
    import shutil
    import tempfile

    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".qoregeo-skill-", dir=parent)
    try:
        copy = os.path.join(staging, "qoregeo")
        shutil.copytree(source, copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if _is_link(target) or os.path.isfile(target):
            os.unlink(target)   # remove the link itself, never the folder it points to
        elif os.path.isdir(target):
            shutil.rmtree(target)
        os.replace(copy, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _cmd_skill(ns: argparse.Namespace) -> int:
    source = _skill_source()
    if ns.action == "path":
        print(source)
        return 0
    if ns.action == "zip":
        return _zip_skill(source, ns.output or "qoregeo-skill.zip")
    base = os.path.expanduser("~") if ns.scope == "user" else os.getcwd()
    folders = {"claude": ".claude", "agents": ".agents"}
    targets = [os.path.join(base, folders[a], "skills", "qoregeo")
               for a in (["claude", "agents"] if ns.agent == "all" else [ns.agent])]
    if not ns.force:   # check every target before installing into any of them
        for target in targets:
            if os.path.lexists(target):
                raise _UsageError(f"{target} already exists (use --force to replace it)")
    for target in targets:
        _install_skill(source, target)
        print(f"Installed the qoregeo skill -> {target}")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────────────────

def _add_source(p: argparse.ArgumentParser) -> None:
    p.add_argument("file", help="CSV/TSV/GeoJSON file")
    p.add_argument("--lat-col", help="latitude column (auto-detected)")
    p.add_argument("--lng-col", help="longitude column (auto-detected)")


def _add_filters(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("filters")
    g.add_argument("--near", metavar="LAT,LNG,RADIUS", help="keep features within RADIUS of a point")
    g.add_argument("--unit", default="km", help="distance unit for --near (km, m, miles, ft, nmi)")
    g.add_argument("--bbox", metavar="W,S,E,N", help="keep features inside a bounding box")
    g.add_argument("--within", metavar="GEOJSON", help="keep features intersecting the shape in this file")
    g.add_argument("--where", action="append", metavar="EXPR",
                   help="attribute condition, e.g. 'population>=1000000' (repeatable)")
    g.add_argument("--sort", metavar="COLUMN", help="sort by a column")
    g.add_argument("--desc", action="store_true", help="sort descending")
    g.add_argument("--limit", type=int, help="keep the first N features")


def _add_geocoder(p: argparse.ArgumentParser) -> None:
    p.add_argument("--provider", default="nominatim", choices=["nominatim", "photon"])
    p.add_argument("--url", help="your own Nominatim/Photon server (recommended for bulk use)")
    p.add_argument("--language", help="preferred result language, e.g. 'en' or 'hi,en'")
    p.add_argument("--email", help="contact e-mail sent to Nominatim (recommended by its policy)")
    p.add_argument("--json", action="store_true", help="print full JSON results")


def _add_sandbox(p: argparse.ArgumentParser) -> None:
    p.add_argument("--root", help="directory tools may read and write (default: $QOREGEO_ROOT, "
                                  "$CLAUDE_PROJECT_DIR or the current directory)")
    p.add_argument("--read-only", action="store_true", help="disable tools that write files")
    p.add_argument("--allow-network", action="store_true",
                   help="enable geocoding with the public OpenStreetMap Nominatim service (1 request/s, "
                        "no bulk use: operations.osmfoundation.org/policies/nominatim); "
                        "also QOREGEO_ALLOW_NETWORK=1")
    p.add_argument("--geocoder-url", help="your own Nominatim/Photon server; enables geocoding "
                                          "(also QOREGEO_GEOCODER_URL)")
    p.add_argument("--geocoder", default="nominatim", choices=["nominatim", "photon"],
                   help="API of --geocoder-url (default: nominatim)")
    p.add_argument("--geocoder-email", help="contact e-mail sent with Nominatim requests "
                                            "(also QOREGEO_GEOCODER_EMAIL)")
    p.add_argument("--max-features", type=_positive_int, default=200,
                   help="most features returned per call (at least 1)")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="qoregeo",
        description="Zero-dependency geospatial toolkit: distances, filters, maps, geocoding - "
                    "and an MCP server for AI assistants.",
    )
    parser.add_argument("--version", action="version", version=f"qoregeo {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("info", help="summarise a dataset")
    _add_source(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_info)

    p = sub.add_parser("distance", help="distance and bearing between two points")
    p.add_argument("a", metavar="LAT,LNG")
    p.add_argument("b", metavar="LAT,LNG")
    p.add_argument("--unit", default="km")
    p.add_argument("--method", default="haversine", choices=["haversine", "vincenty"])
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_distance)

    p = sub.add_parser("map", help="interactive HTML map or heatmap")
    _add_source(p)
    _add_filters(p)
    p.add_argument("-o", "--output", help="HTML file (default: <file>_map.html)")
    p.add_argument("--title")
    p.add_argument("--tiles", default="light", help=f"basemap: {', '.join(tile_presets())} or a URL")
    p.add_argument("--attribution", metavar="HTML",
                   help="credit for a custom --tiles URL, e.g. '&copy; Example Maps'")
    p.add_argument("--heatmap", action="store_true", help="draw a heatmap")
    p.add_argument("--intensity", metavar="COLUMN", help="heatmap weight column")
    p.add_argument("--serve", action="store_true", help="open it via a local web server")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=_cmd_map)

    p = sub.add_parser("image", help="static PNG/SVG map image (no browser needed)")
    _add_source(p)
    _add_filters(p)
    p.add_argument("-o", "--output", help="output .png or .svg (default: <file>.png)")
    p.add_argument("--title")
    p.add_argument("--width", type=int, default=1200)
    p.add_argument("--height", type=int, default=800)
    p.add_argument("--labels", metavar="COLUMN", help="label each feature with this column")
    p.add_argument("--no-grid", action="store_true", help="leave out the lat/lng grid")
    p.add_argument("--no-scale-bar", action="store_true", help="leave out the scale bar")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=_cmd_image)

    p = sub.add_parser("export", help="filter and/or convert to CSV / GeoJSON")
    _add_source(p)
    _add_filters(p)
    p.add_argument("-o", "--output", required=True, help="output .csv, .tsv or .geojson")
    p.add_argument("--raw-csv", action="store_true",
                   help="don't escape cells that spreadsheets would run as formulas")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=_cmd_export)

    p = sub.add_parser("serve", help="view a saved map in the browser (needed for OpenStreetMap tiles)")
    p.add_argument("file")
    p.add_argument("--port", type=int, default=0, help="TCP port (default: any free port)")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser")
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser("geocode", help="address or place -> coordinates (OpenStreetMap)")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=1)
    p.add_argument("--country", help="ISO country code(s), e.g. 'in' or 'de,fr'")
    _add_geocoder(p)
    p.set_defaults(func=_cmd_geocode)

    p = sub.add_parser("reverse", help="coordinates -> address (OpenStreetMap)")
    p.add_argument("point", metavar="LAT,LNG")
    _add_geocoder(p)
    p.set_defaults(func=_cmd_reverse)

    p = sub.add_parser("tiles", help="list the built-in basemaps")
    p.set_defaults(func=_cmd_tiles)

    p = sub.add_parser("tools", help="print AI tool definitions (JSON)")
    p.add_argument("--format", default="mcp", choices=["mcp", "anthropic", "openai", "openai-responses"])
    p.add_argument("--no-network", action="store_true", help="leave out the geocoding tools")
    p.set_defaults(func=_cmd_tools)

    p = sub.add_parser("call", help="run one AI tool with JSON arguments (for agents with a shell)")
    p.add_argument("tool")
    p.add_argument("arguments", nargs="?", default="{}", help="JSON object, or '-' to read stdin")
    p.add_argument("--save-images", action="store_true", help="write returned images to files")
    _add_sandbox(p)
    p.set_defaults(func=_cmd_call)

    p = sub.add_parser("mcp", help="run the MCP server on stdio (for Claude, Codex, Cursor, ...)")
    _add_sandbox(p)
    p.set_defaults(func=_cmd_mcp)

    p = sub.add_parser("skill",
                       help="install the qoregeo Agent Skill for Claude Code, Codex, Gemini, ...")
    p.add_argument("action", choices=["install", "zip", "path"],
                   help="install: copy into agent skill folders; zip: build an uploadable bundle "
                        "(includes the package, for sandboxes without pip); path: print its location")
    p.add_argument("-o", "--output", help="zip file to write (default: qoregeo-skill.zip)")
    p.add_argument("--agent", choices=["claude", "agents", "all"], default="all",
                   help="claude: ~/.claude/skills (Claude Code); agents: ~/.agents/skills "
                        "(Codex, Gemini CLI, GitHub Copilot, VS Code); default: both")
    p.add_argument("--scope", choices=["user", "project"], default="user",
                   help="user: your home folder (default); project: the current directory")
    p.add_argument("--force", action="store_true", help="replace an existing copy")
    p.set_defaults(func=_cmd_skill)
    return parser


# What a console shows for symbols its code page lacks (Windows pipes use the
# ANSI code page, e.g. cp1252); anything else becomes a \uXXXX escape.
_CONSOLE_FALLBACKS = {"✅": "[OK]", "❌": "[X]", "→": "->", "—": "-", "─": "-",
                      "…": "...", "×": "x", "•": "*"}


def _console_fallback(exc: UnicodeError) -> tuple[str, int]:
    """A codecs error handler: ASCII stand-ins for common symbols, escapes for the rest."""
    if not isinstance(exc, UnicodeEncodeError):
        raise exc
    text = exc.object[exc.start:exc.end]
    out = "".join(_CONSOLE_FALLBACKS.get(ch) or ch.encode("ascii", "backslashreplace").decode()
                  for ch in text)
    return out, exc.end


def _tolerant_output() -> Callable[[], None]:
    """
    Make stdout / stderr show characters the console can't encode (see
    _console_fallback) instead of raising UnicodeEncodeError. Returns a
    function that restores the previous setting.
    """
    codecs.register_error("qoregeo.console", _console_fallback)
    changed: list[tuple[Any, str]] = []
    for stream in (sys.stdout, sys.stderr):
        errors = getattr(stream, "errors", None)
        if errors in ("strict", "backslashreplace") and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="qoregeo.console")
            except (OSError, ValueError):   # closed or detached
                continue
            changed.append((stream, errors))

    def restore() -> None:
        for stream, errors in changed:
            try:
                stream.reconfigure(errors=errors)
            except (OSError, ValueError):
                pass

    return restore


def _discard_stdout() -> None:
    """After a broken pipe, send stdout to os.devnull so the flush at exit stays quiet."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass


def main(argv: Sequence[str] | None = None) -> int:
    restore = _tolerant_output()
    try:
        return _main(argv)
    finally:
        restore()


def _main(argv: Sequence[str] | None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    if not getattr(ns, "func", None):
        parser.print_help()
        return 2
    try:
        code: int = ns.func(ns)
        return code
    except _UsageError as exc:
        parser.exit(2, f"qoregeo {ns.command}: error: {exc}\n")
    except QOREgeoError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except BrokenPipeError:   # the reader stopped early, e.g. `qoregeo tools | head`
        _discard_stdout()
        return 1
    except OSError as exc:   # permission denied, a folder where a file should be, disk full, …
        print(f"qoregeo {ns.command}: {exc}", file=sys.stderr)
        return 1
    except UnicodeEncodeError as exc:
        print(f"qoregeo {ns.command}: {exc} (set PYTHONIOENCODING=utf-8 and try again)",
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
