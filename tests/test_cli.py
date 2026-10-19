"""
tests/test_cli.py
=================
The ``qoregeo`` command line (qoregeo.cli.main).

Every command runs in-process with ``capsys`` inside ``tmp_path``: info,
distance, map (filters, tiles, heatmap, --serve), image, export (formula
escaping, refusing to overwrite the input), serve, tiles, tools, call (stdin
arguments, saved images, read-only mode, sandbox root from flags and
environment variables), mcp, skill (install / path / zip into a fake home
directory) and geocode / reverse with a fake geocoder. Nothing opens a
browser, starts a blocking server or touches the network. One test runs
``python -m qoregeo --version`` as a subprocess.

Run with:
    pytest tests/test_cli.py -v
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import pytest

import qoregeo.geocode
import qoregeo.serve
from qoregeo import __version__, cli
from qoregeo.cli import main
from qoregeo.map_builder import tile_presets
from qoregeo.tools import TOOLS

REPO_ROOT = Path(__file__).resolve().parent.parent
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CONFIG_RE = re.compile(r"^[ \t]*var CONFIG = JSON\.parse\((.*)\);[ \t]*$", re.MULTILINE)

CITIES_CSV = (
    "name,lat,lng,population,state,note\n"
    "Delhi,28.6139,77.2090,32000000,Delhi,=1+1\n"
    "Mumbai,19.0760,72.8777,21000000,Maharashtra,ok\n"
    "Pune,18.5204,73.8567,7000000,Maharashtra,@cmd\n"
    "Jaipur,26.9124,75.7873,4000000,Rajasthan,ok\n"
    "Chennai,13.0827,80.2707,11000000,Tamil Nadu,ok\n"
    "Kolkata,22.5726,88.3639,15000000,West Bengal,ok\n"
    "Ahmedabad,23.0225,72.5714,8000000,Gujarat,ok\n"
)
_REAL_SERVE_FILE = qoregeo.serve.serve_file
ENV_VARS = ("QOREGEO_ROOT", "CLAUDE_PROJECT_DIR", "QOREGEO_ALLOW_NETWORK",
            "QOREGEO_GEOCODER_URL", "QOREGEO_GEOCODER_EMAIL")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers + fixtures
# ─────────────────────────────────────────────────────────────────────────────

def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    """Run ``qoregeo argv…``; return (exit code, stdout, stderr)."""
    try:
        code: Any = main(list(argv))
    except SystemExit as exc:
        code = exc.code
    out, err = capsys.readouterr()
    return (0 if code is None else code), out, err


def page_config(path: Path) -> dict[str, Any]:
    found = _CONFIG_RE.findall(path.read_text(encoding="utf-8"))
    assert len(found) == 1
    return json.loads(json.loads(found[0]))


def mapped_names(path: Path) -> list[str]:
    return [f["properties"]["name"] for f in page_config(path)["data"]["features"]]


def read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class FakeGeocoder:
    """Replaces qoregeo.geocode.Geocoder; records how it was built and called."""

    instances: list[FakeGeocoder] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls: list[tuple[str, Any]] = []
        FakeGeocoder.instances.append(self)

    def search(self, query: str, limit: int = 5, country_codes: Any = None) -> list[dict[str, Any]]:
        self.calls.append(("search", (query, limit, country_codes)))
        if query == "nowhere":
            return []
        return [{"lat": 18.921984, "lng": 72.834654, "name": "Gateway of India, Mumbai",
                 "attribution": "Data © OpenStreetMap contributors"},
                {"lat": 18.9, "lng": 72.8, "name": "Second match",
                 "attribution": "Data © OpenStreetMap contributors"}][:limit]

    def reverse(self, lat: float, lng: float) -> dict[str, Any] | None:
        self.calls.append(("reverse", (lat, lng)))
        if lat == 0 and lng == 0:
            return None
        return {"lat": lat, "lng": lng, "name": "Colaba, Mumbai",
                "attribution": "Data © OpenStreetMap contributors"}


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Run in tmp_path with a clean environment; no browser, server or network."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    calls: dict[str, list[Any]] = {"browser": [], "serve_file": [], "serve_html": []}
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: calls["browser"].append(url))

    def fake_serve_file(path: Any, **kwargs: Any) -> str:
        calls["serve_file"].append((path, kwargs))
        return "http://127.0.0.1:1/" + os.path.basename(str(path))

    def fake_serve_html(page: str, name: str = "map.html", **kwargs: Any) -> str:
        calls["serve_html"].append((page, name, kwargs))
        return "http://127.0.0.1:1/" + name

    monkeypatch.setattr(qoregeo.serve, "serve_file", fake_serve_file)
    monkeypatch.setattr(qoregeo.serve, "serve_html", fake_serve_html)
    FakeGeocoder.instances = []
    monkeypatch.setattr(qoregeo.geocode, "Geocoder", FakeGeocoder)
    return calls


@pytest.fixture
def cities(tmp_path: Path) -> Path:
    path = tmp_path / "cities.csv"
    path.write_text(CITIES_CSV, encoding="utf-8")
    return path


@pytest.fixture
def west_zone(tmp_path: Path) -> Path:
    """Polygon around Mumbai, Pune, Jaipur and Ahmedabad (not Delhi)."""
    ring = [[72, 15], [76, 15], [76, 27], [72, 27], [72, 15]]
    path = tmp_path / "west.geojson"
    path.write_text(json.dumps({"type": "Feature", "properties": {},
                                "geometry": {"type": "Polygon", "coordinates": [ring]}}),
                    encoding="utf-8")
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Top level
# ─────────────────────────────────────────────────────────────────────────────

class TestTopLevel:
    def test_version(self, capsys):
        code, out, _ = run(capsys, "--version")
        assert code == 0
        assert out.strip() == f"qoregeo {__version__}"

    def test_no_command_prints_help(self, capsys):
        code, out, _ = run(capsys)
        assert code == 2
        assert "usage: qoregeo" in out
        for command in ("info", "distance", "map", "image", "export", "serve", "geocode",
                        "reverse", "tiles", "tools", "call", "mcp", "skill"):
            assert command in out

    def test_unknown_command(self, capsys):
        code, _, err = run(capsys, "teleport")
        assert code == 2 and "invalid choice" in err

    def test_python_dash_m(self):
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT) + os.pathsep
               + os.environ.get("PYTHONPATH", "")}
        proc = subprocess.run([sys.executable, "-m", "qoregeo", "--version"],
                              capture_output=True, text=True, env=env, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == f"qoregeo {__version__}"


# ─────────────────────────────────────────────────────────────────────────────
# info / distance
# ─────────────────────────────────────────────────────────────────────────────

class TestInfo:
    def test_text(self, capsys, cities):
        code, out, _ = run(capsys, "info", "cities.csv")
        assert code == 0
        assert out.startswith("cities.csv: 7 features")
        assert "Point × 7" in out
        assert "west 72.5714, south 13.0827, east 88.3639, north 28.6139" in out
        assert "population (int)" in out and "name (str)" in out

    def test_json(self, capsys, cities):
        code, out, _ = run(capsys, "info", str(cities), "--json")
        assert code == 0
        data = json.loads(out)
        assert data["count"] == 7
        assert data["bbox"] == [72.5714, 13.0827, 88.3639, 28.6139]
        assert data["geometry_types"] == {"Point": 7}

    def test_explicit_columns(self, capsys, tmp_path):
        (tmp_path / "odd.csv").write_text("place,north,east\nA,10,20\n", encoding="utf-8")
        code, out, _ = run(capsys, "info", "odd.csv", "--lat-col", "north", "--lng-col", "east",
                           "--json")
        assert code == 0 and json.loads(out)["bbox"] == [20.0, 10.0, 20.0, 10.0]

    def test_file_in_other_directory(self, capsys, tmp_path, cities):
        sub = tmp_path / "elsewhere"
        sub.mkdir()
        os.chdir(sub)
        code, out, _ = run(capsys, "info", os.path.join("..", "cities.csv"), "--json")
        assert code == 0 and json.loads(out)["count"] == 7

    def test_missing_file_has_message(self, capsys):
        code, out, err = run(capsys, "info", "missing.csv")
        assert code != 0
        assert "missing.csv" in err and out == ""

    def test_missing_file_exit_code(self, capsys):
        code, _, err = run(capsys, "info", "missing.csv")
        assert code == 1
        assert "list_files" not in err


class TestDistance:
    def test_text(self, capsys):
        code, out, _ = run(capsys, "distance", "28.6139,77.2090", "19.0760,72.8777")
        assert code == 0
        assert out.startswith("1148.0965 km")
        assert "South-Southwest" in out and "°" in out

    def test_json(self, capsys):
        code, out, _ = run(capsys, "distance", "28.6139,77.2090", "19.0760,72.8777", "--json")
        data = json.loads(out)
        assert code == 0
        assert set(data) == {"distance", "unit", "method", "bearing", "compass"}
        assert data["distance"] == pytest.approx(1148.0965)
        assert data["unit"] == "km" and data["method"] == "haversine"
        assert data["compass"] == "South-Southwest"
        assert 180 < data["bearing"] < 225

    def test_miles(self, capsys):
        code, out, _ = run(capsys, "distance", "28.6139,77.2090", "19.0760,72.8777", "--unit",
                           "miles", "--json")
        data = json.loads(out)
        assert data["unit"] == "miles"
        assert data["distance"] == pytest.approx(1148.0965 / 1.609344, rel=1e-4)

    def test_vincenty(self, capsys):
        _, out, _ = run(capsys, "distance", "28.6139,77.2090", "19.0760,72.8777", "--method",
                        "vincenty", "--json")
        data = json.loads(out)
        assert data["method"] == "vincenty"
        assert data["distance"] == pytest.approx(1148.1, rel=0.01)
        assert data["distance"] != pytest.approx(1148.0965, abs=1e-3)

    def test_space_separated_point(self, capsys):
        code, out, _ = run(capsys, "distance", "0 0", "0, 1", "--json")
        assert code == 0 and json.loads(out)["distance"] == pytest.approx(111.1949, abs=1e-3)

    @pytest.mark.parametrize("a", ["28.6", "abc,def", "1,2,3", ""])
    def test_bad_point(self, capsys, a):
        code, out, err = run(capsys, "distance", a, "19.0760,72.8777")
        assert code == 2
        assert "a point must be 2 comma-separated numbers" in err
        assert out == ""

    def test_bad_unit(self, capsys):
        code, _, err = run(capsys, "distance", "0,0", "1,1", "--unit", "parsec")
        assert code == 1 and "parsec" in err

    def test_bad_method(self, capsys):
        code, _, err = run(capsys, "distance", "0,0", "1,1", "--method", "euclid")
        assert code == 2 and "invalid choice" in err

    def test_out_of_range(self, capsys):
        code, _, err = run(capsys, "distance", "100,0", "1,1")
        assert code == 1 and "Coordinate" in err


# ─────────────────────────────────────────────────────────────────────────────
# map / image / export
# ─────────────────────────────────────────────────────────────────────────────

class TestMap:
    def test_default_output(self, capsys, tmp_path, cities):
        code, out, _ = run(capsys, "map", "cities.csv")
        assert code == 0
        page = tmp_path / "cities_map.html"
        assert page.is_file()
        assert "cities_map.html" in out and "7 features" in out
        assert "<title>cities.csv</title>" in page.read_text(encoding="utf-8")
        assert mapped_names(page)[0] == "Delhi"

    def test_default_output_goes_to_cwd(self, capsys, tmp_path, cities):
        sub = tmp_path / "work"
        sub.mkdir()
        os.chdir(sub)
        code, _, _ = run(capsys, "map", str(cities), "-q")
        assert code == 0
        assert (sub / "cities_map.html").is_file()

    def test_output_title_tiles_quiet(self, capsys, tmp_path, cities):
        code, out, _ = run(capsys, "map", "cities.csv", "-o", "india.html", "--title", "India",
                           "--tiles", "topo", "-q")
        assert code == 0 and out == ""
        page = tmp_path / "india.html"
        assert "<title>India</title>" in page.read_text(encoding="utf-8")
        assert page_config(page)["tiles"]["name"] == "topo"

    @pytest.mark.parametrize("tiles", sorted(tile_presets()))
    def test_every_tile_preset(self, capsys, tmp_path, cities, tiles):
        code, _, _ = run(capsys, "map", "cities.csv", "-o", "t.html", "--tiles", tiles, "-q")
        assert code == 0
        assert page_config(tmp_path / "t.html")["tiles"]["name"] == tiles

    def test_bad_tiles(self, capsys, cities):
        code, _, err = run(capsys, "map", "cities.csv", "--tiles", "google", "-q")
        assert code == 1 and "tiles" in err

    def test_heatmap(self, capsys, tmp_path, cities):
        code, out, _ = run(capsys, "map", "cities.csv", "--heatmap", "--intensity", "population")
        assert code == 0
        page = tmp_path / "cities_heatmap.html"
        assert page.is_file() and "7 points" in out
        config = page_config(page)
        weights = sorted(p[2] for p in config["points"])
        assert weights[-1] == 1.0
        assert weights[0] == pytest.approx(4 / 32)

    def test_heatmap_bad_intensity(self, capsys, cities):
        code, _, err = run(capsys, "map", "cities.csv", "--heatmap", "--intensity", "nope")
        assert code == 1 and "nope" in err

    def test_filters(self, capsys, tmp_path, cities):
        code, _, _ = run(capsys, "map", "cities.csv", "-o", "f.html", "-q",
                         "--near", "19.0760,72.8777,1200",
                         "--bbox", "68,15,80,30",
                         "--where", "population>=5000000",
                         "--sort", "population", "--desc", "--limit", "3")
        assert code == 0
        assert mapped_names(tmp_path / "f.html") == ["Delhi", "Mumbai", "Ahmedabad"]

    def test_near_unit(self, capsys, tmp_path, cities):
        run(capsys, "map", "cities.csv", "-o", "n.html", "-q", "--near", "19.0760,72.8777,125",
            "--unit", "miles")
        assert mapped_names(tmp_path / "n.html") == ["Mumbai", "Pune"]

    @pytest.mark.parametrize("expr, expected", [
        ("population>=15000000", ["Delhi", "Mumbai", "Kolkata"]),
        ("population<5000000", ["Jaipur"]),
        ("state==Maharashtra", ["Mumbai", "Pune"]),
        ('state=="maharashtra"', ["Mumbai", "Pune"]),
        ("state=Gujarat", ["Ahmedabad"]),
        ("state != Maharashtra", ["Delhi", "Jaipur", "Chennai", "Kolkata", "Ahmedabad"]),
        ('name in ["Delhi","Pune"]', ["Delhi", "Pune"]),
        ('name not in ["Delhi","Pune","Mumbai"]', ["Jaipur", "Chennai", "Kolkata", "Ahmedabad"]),
        ("name contains ai", ["Mumbai", "Jaipur", "Chennai"]),
        ("state contains Nadu", ["Chennai"]),
    ])
    def test_where_operators(self, capsys, tmp_path, cities, expr, expected):
        code, _, err = run(capsys, "map", "cities.csv", "-o", "w.html", "-q", "--where", expr)
        assert code == 0, err
        assert mapped_names(tmp_path / "w.html") == expected

    def test_where_repeated(self, capsys, tmp_path, cities):
        run(capsys, "map", "cities.csv", "-o", "w.html", "-q", "--where", "state==Maharashtra",
            "--where", "population>10000000")
        assert mapped_names(tmp_path / "w.html") == ["Mumbai"]

    def test_sort_ascending(self, capsys, tmp_path, cities):
        run(capsys, "map", "cities.csv", "-o", "s.html", "-q", "--sort", "name", "--limit", "2")
        assert mapped_names(tmp_path / "s.html") == ["Ahmedabad", "Chennai"]

    def test_within(self, capsys, tmp_path, cities, west_zone):
        code, _, _ = run(capsys, "map", "cities.csv", "-o", "z.html", "-q", "--within",
                         str(west_zone))
        assert code == 0
        assert mapped_names(tmp_path / "z.html") == ["Mumbai", "Pune", "Jaipur", "Ahmedabad"]

    def test_within_missing_file(self, capsys, cities):
        code, _, err = run(capsys, "map", "cities.csv", "--within", "nope.geojson", "-q")
        assert code == 1 and "nope.geojson" in err

    def test_within_malformed_file(self, capsys, tmp_path, cities):
        (tmp_path / "bad.geojson").write_text("{not json", encoding="utf-8")
        code, _, err = run(capsys, "map", "cities.csv", "--within", "bad.geojson", "-q")
        assert code in (1, 2) and "bad.geojson" in err

    def test_within_not_a_shape(self, capsys, tmp_path, cities):
        (tmp_path / "list.json").write_text("[1, 2]", encoding="utf-8")
        code, _, err = run(capsys, "map", "cities.csv", "--within", "list.json", "-q")
        assert code == 1 and err

    @pytest.mark.parametrize("args, phrase", [
        (["--near", "19.07,72.87"], "--near must be 3 comma-separated numbers"),
        (["--near", "a,b,c"], "--near must be 3 comma-separated numbers"),
        (["--bbox", "68,6,98"], "--bbox (west,south,east,north) must be 4"),
        (["--where", "population"], "--where must look like"),
        (["--where", ""], "--where must look like"),
    ])
    def test_bad_filter_syntax(self, capsys, tmp_path, cities, args, phrase):
        code, out, err = run(capsys, "map", "cities.csv", "-o", "bad.html", *args)
        assert code == 2
        assert phrase in err
        assert out == ""
        assert not (tmp_path / "bad.html").exists()

    def test_bad_filter_values(self, capsys, cities):
        assert run(capsys, "map", "cities.csv", "--where", "popularity>1", "-q")[0] == 1
        assert run(capsys, "map", "cities.csv", "--near", "0,0,-5", "-q")[0] == 1
        assert run(capsys, "map", "cities.csv", "--limit", "-1", "-q")[0] == 1

    def test_missing_file(self, capsys):
        code, out, err = run(capsys, "map", "missing.csv")
        assert code == 1
        assert "missing.csv" in err and out == ""

    def test_refuses_to_overwrite_input(self, capsys, cities):
        code, _, err = run(capsys, "map", "cities.csv", "-o", "cities.csv")
        assert code == 1 and "output_path" in err
        assert cities.read_text(encoding="utf-8") == CITIES_CSV

    def test_serve(self, capsys, cities, sandbox):
        code, _, _ = run(capsys, "map", "cities.csv", "--serve", "--port", "8765", "-q")
        assert code == 0
        (path, kwargs), = sandbox["serve_file"]
        assert path == "cities_map.html"
        assert kwargs["port"] == 8765 and kwargs["block"] is True
        assert kwargs["verbose"] is False


class TestImage:
    def test_default_png(self, capsys, tmp_path, cities):
        code, out, _ = run(capsys, "image", "cities.csv", "--width", "120", "--height", "90")
        assert code == 0
        assert (tmp_path / "cities.png").read_bytes()[:8] == PNG_SIGNATURE
        assert "cities.png" in out

    def test_svg(self, capsys, tmp_path, cities):
        code, out, _ = run(capsys, "image", "cities.csv", "-o", "img.svg", "--title", "Cities",
                           "--width", "300", "--height", "200", "-q",
                           "--where", "state==Maharashtra")
        assert code == 0 and out == ""
        svg = ET.parse(tmp_path / "img.svg").getroot()
        assert svg.get("width") == "300" and svg.get("height") == "200"

    def test_bad_extension(self, capsys, tmp_path, cities):
        code, _, err = run(capsys, "image", "cities.csv", "-o", "img.jpg", "-q")
        assert code == 1 and "png" in err
        assert not (tmp_path / "img.jpg").exists()

    def test_bad_size(self, capsys, cities):
        assert run(capsys, "image", "cities.csv", "--width", "5", "-q")[0] == 1

    def test_refuses_to_overwrite_input(self, capsys, tmp_path):
        source = tmp_path / "shapes.svg.geojson"
        source.write_text('{"type": "Point", "coordinates": [1, 2]}', encoding="utf-8")
        code, _, _ = run(capsys, "image", str(source), "-o", str(source), "-q")
        assert code == 1
        assert source.read_text(encoding="utf-8").startswith('{"type": "Point"')


class TestExport:
    def test_csv_escapes_formulas(self, capsys, tmp_path, cities):
        code, out, _ = run(capsys, "export", "cities.csv", "-o", "out.csv")
        assert code == 0 and out == ""
        rows = read_csv(tmp_path / "out.csv")
        assert [r["name"] for r in rows] == ["Delhi", "Mumbai", "Pune", "Jaipur", "Chennai",
                                             "Kolkata", "Ahmedabad"]
        assert rows[0]["note"] == "'=1+1"
        assert rows[2]["note"] == "'@cmd"
        assert rows[1]["note"] == "ok"

    def test_raw_csv(self, capsys, tmp_path, cities):
        run(capsys, "export", "cities.csv", "-o", "raw.csv", "--raw-csv")
        rows = read_csv(tmp_path / "raw.csv")
        assert rows[0]["note"] == "=1+1" and rows[2]["note"] == "@cmd"

    def test_geojson_filtered(self, capsys, tmp_path, cities):
        code, _, _ = run(capsys, "export", "cities.csv", "-o", "big.geojson",
                         "--where", "population>=15000000", "--sort", "population")
        assert code == 0
        data = json.loads((tmp_path / "big.geojson").read_text(encoding="utf-8"))
        assert [f["properties"]["name"] for f in data["features"]] == \
            ["Kolkata", "Mumbai", "Delhi"]
        assert data["features"][2]["properties"]["note"] == "=1+1"   # JSON is never escaped

    def test_tsv(self, capsys, tmp_path, cities):
        run(capsys, "export", "cities.csv", "-o", "out.tsv", "--limit", "1")
        lines = (tmp_path / "out.tsv").read_text(encoding="utf-8").splitlines()
        assert lines[0].split("\t")[:3] == ["latitude", "longitude", "name"]
        assert lines[1].split("\t")[-1] == "'=1+1"

    @pytest.mark.parametrize("spelling", ["cities.csv", os.path.join(".", "cities.csv"),
                                          os.path.join("sub", "..", "cities.csv")])
    def test_refuses_input_as_output(self, capsys, tmp_path, cities, spelling):
        (tmp_path / "sub").mkdir(exist_ok=True)
        code, _, err = run(capsys, "export", "cities.csv", "-o", spelling)
        assert code == 2
        assert "input file" in err
        assert cities.read_text(encoding="utf-8") == CITIES_CSV

    def test_refuses_input_as_output_absolute(self, capsys, cities):
        code, _, _ = run(capsys, "export", str(cities), "-o", "cities.csv", "--raw-csv")
        assert code == 2
        assert cities.read_text(encoding="utf-8") == CITIES_CSV

    def test_output_required(self, capsys, cities):
        code, _, err = run(capsys, "export", "cities.csv")
        assert code == 2 and "--output" in err

    def test_bad_extension(self, capsys, tmp_path, cities):
        code, _, err = run(capsys, "export", "cities.csv", "-o", "out.xlsx")
        assert code == 1 and ".xlsx" in err
        assert not (tmp_path / "out.xlsx").exists()

    def test_missing_file(self, capsys):
        code, _, err = run(capsys, "export", "missing.csv", "-o", "x.csv")
        assert code == 1 and "missing.csv" in err


# ─────────────────────────────────────────────────────────────────────────────
# serve / tiles / tools
# ─────────────────────────────────────────────────────────────────────────────

class TestServeCommand:
    def test_serve(self, capsys, tmp_path, sandbox):
        (tmp_path / "m.html").write_text("<p>x</p>", encoding="utf-8")
        assert run(capsys, "serve", "m.html", "--port", "9999")[0] == 0
        (path, kwargs), = sandbox["serve_file"]
        assert path == "m.html"
        assert kwargs == {"port": 9999, "block": True, "open_browser": True}

    def test_no_browser(self, capsys, tmp_path, sandbox):
        (tmp_path / "m.html").write_text("<p>x</p>", encoding="utf-8")
        run(capsys, "serve", "m.html", "--no-browser")
        assert sandbox["serve_file"][0][1]["open_browser"] is False
        assert sandbox["browser"] == []

    def test_missing_file(self, capsys, monkeypatch):
        # The real serve_file checks the file before it starts a server.
        monkeypatch.setattr(qoregeo.serve, "serve_file", _REAL_SERVE_FILE)
        code, _, err = run(capsys, "serve", "nope.html", "--no-browser")
        assert code == 1 and "nope.html" in err


class TestTilesAndTools:
    def test_tiles_lists_every_preset(self, capsys):
        code, out, _ = run(capsys, "tiles")
        assert code == 0
        names = [line.split()[0] for line in out.splitlines() if line.strip()]
        assert names == list(tile_presets())
        for name, label in tile_presets().items():
            assert label in out, name

    @pytest.mark.parametrize("fmt", ["mcp", "anthropic", "openai", "openai-responses"])
    def test_tools_formats(self, capsys, fmt):
        code, out, _ = run(capsys, "tools", "--format", fmt)
        assert code == 0
        defs = json.loads(out)
        names = [d["function"]["name"] if fmt == "openai" else d["name"] for d in defs]
        assert names == list(TOOLS)

    def test_tools_default_is_mcp(self, capsys):
        defs = json.loads(run(capsys, "tools")[1])
        assert "inputSchema" in defs[0] and "annotations" in defs[0]

    def test_tools_no_network(self, capsys):
        defs = json.loads(run(capsys, "tools", "--format", "anthropic", "--no-network")[1])
        names = {d["name"] for d in defs}
        assert names == set(TOOLS) - {"geocode", "reverse_geocode"}

    def test_tools_bad_format(self, capsys):
        code, _, err = run(capsys, "tools", "--format", "gemini")
        assert code == 2 and "invalid choice" in err


# ─────────────────────────────────────────────────────────────────────────────
# call
# ─────────────────────────────────────────────────────────────────────────────

DISTANCE_JSON = '{"from": {"lat": 0, "lng": 0}, "to": {"lat": 0, "lng": 1}}'


class TestCall:
    def test_success(self, capsys):
        code, out, _ = run(capsys, "call", "distance", DISTANCE_JSON)
        assert code == 0
        assert json.loads(out) == {"distance": pytest.approx(111.1949, abs=1e-3), "unit": "km",
                                   "method": "haversine"}

    def test_default_arguments(self, capsys, cities):
        code, out, _ = run(capsys, "call", "list_files")
        assert code == 0
        assert [f["path"] for f in json.loads(out)["files"]] == ["cities.csv"]

    def test_tool_error_exit_1(self, capsys):
        code, out, _ = run(capsys, "call", "distance", "{}")
        assert code == 1
        assert "Missing" in json.loads(out)["error"]

    def test_unknown_tool(self, capsys):
        code, out, _ = run(capsys, "call", "teleport")
        assert code == 1 and "teleport" in json.loads(out)["error"]

    @pytest.mark.parametrize("raw", ["{not json", "{'a': 1}"])
    def test_invalid_json(self, capsys, raw):
        code, out, err = run(capsys, "call", "distance", raw)
        assert code == 2 and "JSON" in err and out == ""

    def test_non_object_json(self, capsys):
        code, out, _ = run(capsys, "call", "distance", "[1, 2]")
        assert code == 1 and "object" in json.loads(out)["error"]

    def test_stdin(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(DISTANCE_JSON))
        code, out, _ = run(capsys, "call", "distance", "-")
        assert code == 0 and json.loads(out)["distance"] == pytest.approx(111.1949, abs=1e-3)

    def test_empty_stdin(self, capsys, monkeypatch, cities):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        code, out, _ = run(capsys, "call", "list_files", "-")
        assert code == 0 and json.loads(out)["files"]

    def test_save_images(self, capsys, tmp_path, cities):
        code, out, err = run(capsys, "call", "preview_map",
                             '{"path": "cities.csv", "width": 64, "height": 64}', "--save-images")
        assert code == 0
        data = json.loads(out)
        png = (tmp_path / "preview_map.png").read_bytes()
        assert png[:8] == PNG_SIGNATURE and len(png) == data["bytes"]
        assert "preview_map.png" in err

    def test_images_not_saved_by_default(self, capsys, tmp_path, cities):
        run(capsys, "call", "preview_map", '{"path": "cities.csv", "width": 64, "height": 64}')
        assert not (tmp_path / "preview_map.png").exists()

    def test_read_only_blocks_writers(self, capsys, tmp_path, cities):
        code, out, _ = run(capsys, "call", "make_map",
                           '{"path": "cities.csv", "output": "m.html"}', "--read-only")
        assert code == 1
        assert "disabled" in json.loads(out)["error"].lower()
        assert not (tmp_path / "m.html").exists()
        code, out, _ = run(capsys, "call", "make_map", '{"path": "cities.csv", "output": "m.html"}')
        assert code == 0 and (tmp_path / "m.html").is_file()

    def test_max_features(self, capsys, cities):
        code, out, _ = run(capsys, "call", "query_features", '{"path": "cities.csv"}',
                           "--max-features", "2")
        data = json.loads(out)
        assert code == 0 and data["returned"] == 2 and data["truncated"] is True

    def test_root_flag(self, capsys, tmp_path, cities):
        other = tmp_path / "other"
        other.mkdir()
        os.chdir(other)
        code, out, _ = run(capsys, "call", "describe_dataset", '{"path": "cities.csv"}',
                           "--root", str(tmp_path))
        assert code == 0 and json.loads(out)["count"] == 7
        code, out, _ = run(capsys, "call", "describe_dataset", '{"path": "cities.csv"}')
        assert code == 1   # the sandbox is the current directory by default

    def test_bad_root(self, capsys, tmp_path):
        code, _, err = run(capsys, "call", "list_files", "--root", str(tmp_path / "missing"))
        assert code == 1 and "root" in err

    def test_geocoding_disabled_by_default(self, capsys):
        code, out, _ = run(capsys, "call", "geocode", '{"query": "Pune"}')
        assert code == 1 and "disabled" in json.loads(out)["error"].lower()
        assert FakeGeocoder.instances == []

    def test_allow_network_flag(self, capsys):
        code, out, _ = run(capsys, "call", "geocode", '{"query": "Gateway", "limit": 1}',
                           "--allow-network")
        assert code == 0
        assert json.loads(out)["results"][0]["name"] == "Gateway of India, Mumbai"
        fake, = FakeGeocoder.instances
        assert fake.calls == [("search", ("Gateway", 1, None))]

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_allow_network_env(self, capsys, monkeypatch, value):
        monkeypatch.setenv("QOREGEO_ALLOW_NETWORK", value)
        code, out, _ = run(capsys, "call", "reverse_geocode",
                           '{"point": {"lat": 18.9, "lng": 72.8}}')
        assert code == 0 and json.loads(out)["result"]["name"] == "Colaba, Mumbai"

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "${ALLOW}"])
    def test_allow_network_env_off(self, capsys, monkeypatch, value):
        monkeypatch.setenv("QOREGEO_ALLOW_NETWORK", value)
        code, _, _ = run(capsys, "call", "geocode", '{"query": "Pune"}')
        assert code == 1

    def test_geocoder_url_enables_geocoding(self, capsys):
        code, _, _ = run(capsys, "call", "geocode", '{"query": "Pune"}',
                         "--geocoder-url", "http://localhost:8080", "--geocoder", "photon",
                         "--geocoder-email", "me@example.com")
        assert code == 0
        fake, = FakeGeocoder.instances
        assert fake.kwargs == {"provider": "photon", "url": "http://localhost:8080",
                               "email": "me@example.com"}

    def test_geocoder_url_env(self, capsys, monkeypatch):
        monkeypatch.setenv("QOREGEO_GEOCODER_URL", "http://geo.internal")
        monkeypatch.setenv("QOREGEO_GEOCODER_EMAIL", "ops@example.com")
        assert run(capsys, "call", "geocode", '{"query": "Pune"}')[0] == 0
        fake, = FakeGeocoder.instances
        assert fake.kwargs == {"provider": "nominatim", "url": "http://geo.internal",
                               "email": "ops@example.com"}


class TestDefaultRoot:
    def test_flag_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QOREGEO_ROOT", "/env/root")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/claude/project")
        assert cli._default_root("given") == "given"

    def test_qoregeo_root(self, monkeypatch):
        monkeypatch.setenv("QOREGEO_ROOT", "/env/root")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/claude/project")
        assert cli._default_root(None) == "/env/root"

    def test_claude_project_dir(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/claude/project")
        assert cli._default_root(None) == "/claude/project"

    @pytest.mark.parametrize("unusable", ["", "   ", "${CLAUDE_PROJECT_DIR}", "${workspaceFolder}"])
    def test_unusable_values_skipped(self, monkeypatch, tmp_path, unusable):
        monkeypatch.setenv("QOREGEO_ROOT", unusable)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/claude/project")
        assert cli._default_root(None) == "/claude/project"
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", unusable)
        assert cli._default_root(None) == os.getcwd()

    def test_cwd_fallback(self, tmp_path):
        assert cli._default_root(None) == os.getcwd()
        assert os.path.samefile(cli._default_root(None), tmp_path)

    def test_call_uses_env_root(self, capsys, monkeypatch, tmp_path, cities):
        other = tmp_path / "other"
        other.mkdir()
        os.chdir(other)
        monkeypatch.setenv("QOREGEO_ROOT", str(tmp_path))
        code, out, _ = run(capsys, "call", "describe_dataset", '{"path": "cities.csv"}')
        assert code == 0 and json.loads(out)["count"] == 7

    def test_call_uses_claude_project_dir(self, capsys, monkeypatch, tmp_path, cities):
        other = tmp_path / "other"
        other.mkdir()
        os.chdir(other)
        monkeypatch.setenv("QOREGEO_ROOT", "${QOREGEO_ROOT}")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        code, out, _ = run(capsys, "call", "describe_dataset", '{"path": "cities.csv"}')
        assert code == 0 and json.loads(out)["count"] == 7


# ─────────────────────────────────────────────────────────────────────────────
# mcp
# ─────────────────────────────────────────────────────────────────────────────

def mcp_session(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
                *argv: str) -> tuple[int, list[dict[str, Any]], str]:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "describe_dataset", "arguments": {"path": "cities.csv"}}},
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(json.dumps(m) + "\n"
                                                          for m in messages)))
    code, out, err = run(capsys, "mcp", *argv)
    return code, [json.loads(line) for line in out.splitlines() if line.strip()], err


class TestMcpCommand:
    def test_session(self, capsys, monkeypatch, tmp_path, cities):
        code, replies, err = mcp_session(capsys, monkeypatch, "--root", str(tmp_path))
        assert code == 0
        assert [r["id"] for r in replies] == [1, 2, 3]
        names = {t["name"] for t in replies[1]["result"]["tools"]}
        assert {"make_map", "export_features"} <= names
        assert not names & {"geocode", "reverse_geocode"}
        assert replies[2]["result"]["structuredContent"]["count"] == 7
        assert "ready" in err and "write: on" in err and "network: off" in err

    def test_read_only_and_network(self, capsys, monkeypatch, tmp_path, cities):
        code, replies, err = mcp_session(capsys, monkeypatch, "--root", str(tmp_path),
                                         "--read-only", "--allow-network")
        names = {t["name"] for t in replies[1]["result"]["tools"]}
        assert code == 0
        assert not names & {"make_map", "export_features"}
        assert {"geocode", "reverse_geocode"} <= names
        assert "write: off" in err and "network: on" in err

    def test_env_root(self, capsys, monkeypatch, tmp_path, cities):
        os.chdir(tmp_path.parent)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        code, replies, _ = mcp_session(capsys, monkeypatch)
        assert code == 0 and replies[2]["result"]["structuredContent"]["count"] == 7

    def test_bad_root(self, capsys, monkeypatch, tmp_path):
        code, replies, err = mcp_session(capsys, monkeypatch, "--root", str(tmp_path / "nope"))
        assert code == 1 and replies == [] and "root" in err


# ─────────────────────────────────────────────────────────────────────────────
# skill
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def skill_source() -> Path:
    try:
        return Path(cli._skill_source())
    except cli._UsageError:
        pytest.skip("the bundled skill is not available in this checkout")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
    assert os.path.expanduser("~") == str(path)
    return path


def assert_skill_copy(target: Path, source: Path) -> None:
    assert (target / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()
    expected = sorted(p.relative_to(source).as_posix() for p in source.rglob("*")
                      if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    copied = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
    assert copied == expected
    assert any(name.startswith("references/") for name in copied)


class TestSkill:
    def test_path(self, capsys, skill_source):
        code, out, _ = run(capsys, "skill", "path")
        assert code == 0
        assert Path(out.strip()) == skill_source
        assert (skill_source / "SKILL.md").is_file()
        assert (skill_source / "references").is_dir()

    def test_install_user_scope_all_agents(self, capsys, home, skill_source):
        code, out, _ = run(capsys, "skill", "install")
        assert code == 0
        claude = home / ".claude" / "skills" / "qoregeo"
        agents = home / ".agents" / "skills" / "qoregeo"
        for target in (claude, agents):
            assert_skill_copy(target, skill_source)
            assert str(target) in out

    @pytest.mark.parametrize("agent, folder, other", [("claude", ".claude", ".agents"),
                                                      ("agents", ".agents", ".claude")])
    def test_install_one_agent(self, capsys, home, skill_source, agent, folder, other):
        code, out, _ = run(capsys, "skill", "install", "--agent", agent)
        assert code == 0
        assert_skill_copy(home / folder / "skills" / "qoregeo", skill_source)
        assert not (home / other).exists()
        assert out.count("→") == 1

    def test_install_project_scope(self, capsys, tmp_path, home, skill_source):
        project = tmp_path / "project"
        project.mkdir()
        os.chdir(project)
        code, out, _ = run(capsys, "skill", "install", "--scope", "project", "--agent", "claude")
        assert code == 0
        assert_skill_copy(project / ".claude" / "skills" / "qoregeo", skill_source)
        assert not (home / ".claude").exists()

    def test_existing_copy_needs_force(self, capsys, home, skill_source):
        assert run(capsys, "skill", "install", "--agent", "claude")[0] == 0
        target = home / ".claude" / "skills" / "qoregeo"
        (target / "stale.txt").write_text("old", encoding="utf-8")
        code, _, err = run(capsys, "skill", "install", "--agent", "claude")
        assert code == 2 and "--force" in err
        assert (target / "stale.txt").is_file()

        code, out, _ = run(capsys, "skill", "install", "--agent", "claude", "--force")
        assert code == 0 and str(target) in out
        assert not (target / "stale.txt").exists()
        assert_skill_copy(target, skill_source)

    def test_zip(self, capsys, tmp_path, skill_source):
        code, out, _ = run(capsys, "skill", "zip", "-o", "bundle.zip")
        assert code == 0 and "bundle.zip" in out
        with zipfile.ZipFile(tmp_path / "bundle.zip") as zf:
            names = zf.namelist()
            assert "qoregeo/SKILL.md" in names
            assert "qoregeo/scripts/qoregeo/__init__.py" in names
            assert "qoregeo/scripts/qoregeo/tools.py" in names
            assert not any(n.endswith(".pyc") or "__pycache__" in n for n in names)
            assert zf.read("qoregeo/SKILL.md") == (skill_source / "SKILL.md").read_bytes()

    def test_bad_action(self, capsys):
        assert run(capsys, "skill", "uninstall")[0] == 2


# ─────────────────────────────────────────────────────────────────────────────
# geocode / reverse (fake geocoder)
# ─────────────────────────────────────────────────────────────────────────────

class TestGeocodeCommands:
    def test_geocode_text(self, capsys):
        code, out, err = run(capsys, "geocode", "Gateway of India")
        assert code == 0
        assert out == "18.921984,72.834654  Gateway of India, Mumbai\n"
        assert "OpenStreetMap" in err
        fake, = FakeGeocoder.instances
        assert fake.kwargs == {"provider": "nominatim", "url": None, "language": None,
                               "email": None}
        assert fake.calls == [("search", ("Gateway of India", 1, None))]

    def test_geocode_options_and_json(self, capsys):
        code, out, _ = run(capsys, "geocode", "Gateway", "--limit", "2", "--country", "in",
                           "--provider", "photon", "--url", "http://localhost:2322",
                           "--language", "en", "--email", "me@example.com", "--json")
        assert code == 0
        assert [r["name"] for r in json.loads(out)] == ["Gateway of India, Mumbai",
                                                       "Second match"]
        fake, = FakeGeocoder.instances
        assert fake.kwargs == {"provider": "photon", "url": "http://localhost:2322",
                               "language": "en", "email": "me@example.com"}
        assert fake.calls == [("search", ("Gateway", 2, "in"))]

    def test_geocode_no_match(self, capsys):
        code, out, _ = run(capsys, "geocode", "nowhere")
        assert code == 1 and out.strip() == "No match."
        code, out, _ = run(capsys, "geocode", "nowhere", "--json")
        assert code == 0 and json.loads(out) == []

    def test_reverse_text(self, capsys):
        code, out, _ = run(capsys, "reverse", "18.92,72.83")
        assert code == 0 and out.strip() == "Colaba, Mumbai"
        assert FakeGeocoder.instances[0].calls == [("reverse", (18.92, 72.83))]

    def test_reverse_json(self, capsys):
        code, out, _ = run(capsys, "reverse", "18.92,72.83", "--json")
        assert code == 0 and json.loads(out)["name"] == "Colaba, Mumbai"

    def test_reverse_nothing(self, capsys):
        code, out, _ = run(capsys, "reverse", "0,0")
        assert code == 1 and out.strip() == "No address found."
        code, out, _ = run(capsys, "reverse", "0,0", "--json")
        assert code == 0 and json.loads(out) is None

    def test_reverse_bad_point(self, capsys):
        code, _, err = run(capsys, "reverse", "north,east")
        assert code == 2 and "a point" in err
        assert FakeGeocoder.instances == []

    def test_bad_provider(self, capsys):
        assert run(capsys, "geocode", "x", "--provider", "google")[0] == 2


class TestImageLabels:
    def test_labels_and_switches(self, capsys, cities, tmp_path):
        code, _, _ = run(capsys, "image", "cities.csv", "-o", "l.svg", "--labels", "name",
                         "--no-grid", "--no-scale-bar", "-q")
        text = (tmp_path / "l.svg").read_text(encoding="utf-8")
        assert code == 0 and ">Delhi<" in text
        assert 'id="qg-grid"' not in text and 'id="qg-scale-bar"' not in text
