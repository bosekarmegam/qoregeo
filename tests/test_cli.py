"""
Command-line interface.

``main()`` returns an exit code rather than calling ``sys.exit``, so every
command can be driven in-process and its output captured.
"""

from __future__ import annotations

import argparse
import json

import pytest

from qoregeo.cli import build_parser, main, parse_point


class TestParsePoint:
    def test_parses_a_coordinate(self):
        assert parse_point("28.6139,77.2090") == (28.6139, 77.2090)

    def test_tolerates_spaces(self):
        assert parse_point(" 28.6139 , 77.2090 ") == (28.6139, 77.2090)

    def test_negative_values(self):
        assert parse_point("-33.87,151.21") == (-33.87, 151.21)

    @pytest.mark.parametrize("bad", ["28.6139", "a,b", "1,2,3", ""])
    def test_rejects_malformed_input(self, bad):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_point(bad)

    def test_error_shows_the_expected_shape(self):
        with pytest.raises(argparse.ArgumentTypeError) as exc:
            parse_point("nope")
        assert "lat,lng" in str(exc.value)


class TestParser:
    def test_no_command_prints_help(self, capsys):
        assert main([]) == 0
        assert "usage" in capsys.readouterr().out

    def test_version_flag(self, capsys):
        import qoregeo

        with pytest.raises(SystemExit):
            build_parser().parse_args(["--version"])
        assert qoregeo.__version__ in capsys.readouterr().out

    @pytest.mark.parametrize(
        "command",
        ["info", "convert", "map", "heatmap", "image", "distance",
         "nearest", "within", "stats", "geocode"],
    )
    def test_every_command_is_registered(self, command):
        actions = build_parser()._subparsers._group_actions[0].choices
        assert command in actions


class TestInfo:
    def test_human_output(self, cities_csv, capsys):
        assert main(["info", cities_csv]) == 0
        out = capsys.readouterr().out
        assert "Features    7" in out
        assert "Point × 7" in out
        assert "all valid" in out

    def test_json_output(self, cities_csv, capsys):
        assert main(["info", cities_csv, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["features"] == 7
        assert payload["columns"] == ["name", "state", "population"]

    def test_missing_file_exits_nonzero(self, capsys):
        assert main(["info", "/no/such/file.csv"]) == 1
        assert "File Not Found" in capsys.readouterr().err


class TestDistance:
    def test_default_units(self, capsys):
        assert main(["distance", "28.6139,77.2090", "19.0760,72.8777"]) == 0
        out = capsys.readouterr().out
        assert "km" in out
        assert "South-Southwest" in out

    def test_json_output(self, capsys):
        assert main(["distance", "0,0", "0,1", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["bearing_degrees"] == pytest.approx(90, abs=0.01)
        assert payload["direction"] == "East"

    def test_unit_selection(self, capsys):
        main(["distance", "0,0", "0,1", "-u", "miles", "--json"])
        miles = json.loads(capsys.readouterr().out)["distance"]
        main(["distance", "0,0", "0,1", "--json"])
        km = json.loads(capsys.readouterr().out)["distance"]
        assert miles == pytest.approx(km * 0.621371, rel=1e-4)

    def test_vincenty_method(self, capsys):
        main(["distance", "28.6139,77.2090", "19.0760,72.8777",
              "--method", "vincenty", "--json"])
        assert json.loads(capsys.readouterr().out)["method"] == "vincenty"


class TestNearest:
    def test_lists_closest_first(self, cities_csv, capsys):
        assert main(["nearest", cities_csv, "19.0,73.0", "-k", "3"]) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 3
        assert "Mumbai" in lines[0]

    def test_json_output(self, cities_csv, capsys):
        main(["nearest", cities_csv, "19.0,73.0", "-k", "2", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert len(payload) == 2
        assert payload[0]["distance"] <= payload[1]["distance"]


class TestWithin:
    def test_prints_matches(self, cities_csv, capsys):
        assert main(["within", cities_csv, "19.07,72.87", "200"]) == 0
        out = capsys.readouterr().out
        assert "Mumbai" in out
        assert "Pune" in out

    def test_saves_to_a_file(self, cities_csv, tmp_path, capsys):
        out_path = tmp_path / "near.geojson"
        assert main(["within", cities_csv, "19.07,72.87", "200", "-o", str(out_path)]) == 0
        assert json.loads(out_path.read_text())["type"] == "FeatureCollection"


class TestStats:
    def test_numeric_column(self, cities_csv, capsys):
        assert main(["stats", cities_csv, "population"]) == 0
        assert "median" in capsys.readouterr().out

    def test_text_column_falls_back_to_counts(self, cities_csv, capsys):
        assert main(["stats", cities_csv, "state"]) == 0
        assert "Maharashtra" in capsys.readouterr().out

    def test_unknown_column_exits_nonzero(self, cities_csv, capsys):
        assert main(["stats", cities_csv, "nope"]) == 1
        assert "Column Not Found" in capsys.readouterr().err


class TestConvert:
    @pytest.mark.parametrize("ext", ["geojson", "csv", "ndjson", "wkt", "gpx", "kml"])
    def test_converts_between_formats(self, cities_csv, tmp_path, ext, capsys):
        out_path = tmp_path / f"out.{ext}"
        assert main(["convert", cities_csv, str(out_path)]) == 0
        assert out_path.stat().st_size > 0

    def test_query_narrows_the_output(self, cities_csv, tmp_path, capsys):
        out_path = tmp_path / "out.geojson"
        assert main(["convert", cities_csv, str(out_path), "-q", "population > 15000000"]) == 0
        assert len(json.loads(out_path.read_text())["features"]) == 2

    def test_bad_query_exits_nonzero(self, cities_csv, tmp_path, capsys):
        out_path = tmp_path / "out.geojson"
        assert main(["convert", cities_csv, str(out_path), "-q", "pop @@ 5"]) == 1
        assert "Invalid Query" in capsys.readouterr().err


class TestRendering:
    def test_map(self, cities_csv, tmp_path):
        out_path = tmp_path / "m.html"
        assert main(["map", cities_csv, "-o", str(out_path)]) == 0
        assert "leaflet" in out_path.read_text().lower()

    def test_map_with_options(self, cities_csv, tmp_path):
        out_path = tmp_path / "m.html"
        assert main(["map", cities_csv, "-o", str(out_path), "--colour-by", "state",
                     "--basemap", "light", "--tooltip", "name"]) == 0
        assert 'var COLOUR_BY = "state";' in out_path.read_text()

    def test_us_spelling_flag(self, cities_csv, tmp_path):
        out_path = tmp_path / "m.html"
        assert main(["map", cities_csv, "-o", str(out_path), "--color-by", "state"]) == 0
        assert 'var COLOUR_BY = "state";' in out_path.read_text()

    def test_heatmap(self, cities_csv, tmp_path):
        out_path = tmp_path / "h.html"
        assert main(["heatmap", cities_csv, "-o", str(out_path), "--intensity", "population"]) == 0
        assert "heatLayer" in out_path.read_text()

    def test_svg_image(self, cities_csv, tmp_path):
        out_path = tmp_path / "m.svg"
        assert main(["image", cities_csv, "-o", str(out_path)]) == 0
        assert out_path.read_text().startswith("<svg")

    def test_png_image(self, cities_csv, tmp_path):
        out_path = tmp_path / "m.png"
        assert main(["image", cities_csv, "-o", str(out_path), "--theme", "light"]) == 0
        assert out_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_image_size_flags(self, cities_csv, tmp_path):
        out_path = tmp_path / "m.svg"
        main(["image", cities_csv, "-o", str(out_path), "--width", "640", "--height", "480"])
        assert 'viewBox="0 0 640 480"' in out_path.read_text()


class TestColumnOverrides:
    def test_custom_lat_lng_columns(self, tmp_path, capsys):
        path = tmp_path / "odd.csv"
        path.write_text("place,y_coord,x_coord\nDelhi,28.6139,77.2090\n", encoding="utf-8")
        assert main(["info", str(path), "--lat-col", "y_coord", "--lng-col", "x_coord"]) == 0
        assert "Features    1" in capsys.readouterr().out


class TestGeocodeCommand:
    def test_requires_a_user_agent(self, capsys):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["geocode", "India Gate"])

    def test_network_failure_exits_nonzero(self, capsys, monkeypatch):
        from qoregeo import geocode as geocode_module
        from qoregeo.exceptions import GeocodingError

        def explode(self, endpoint, params, label):
            raise GeocodingError(label, "no network in tests")

        monkeypatch.setattr(geocode_module.Geocoder, "_get", explode)
        assert main(["geocode", "India Gate", "--user-agent", "test/1.0 (a@b.c)"]) == 1
        assert "Geocoding Failed" in capsys.readouterr().err

    def test_reports_a_result(self, capsys, monkeypatch):
        from qoregeo import geocode as geocode_module

        def fake(self, endpoint, params, label):
            return [{"lat": "28.6129", "lon": "77.2295",
                     "display_name": "India Gate, New Delhi", "type": "monument",
                     "address": {"city": "New Delhi", "country_code": "in"}}]

        monkeypatch.setattr(geocode_module.Geocoder, "_get", fake)
        assert main(["geocode", "India Gate", "--user-agent", "test/1.0 (a@b.c)"]) == 0
        assert "India Gate" in capsys.readouterr().out

    def test_no_match_exits_nonzero(self, capsys, monkeypatch):
        from qoregeo import geocode as geocode_module

        monkeypatch.setattr(geocode_module.Geocoder, "_get", lambda self, endpoint, params, label: [])
        assert main(["geocode", "Nowhere", "--user-agent", "test/1.0 (a@b.c)"]) == 1
        assert "No match" in capsys.readouterr().err
