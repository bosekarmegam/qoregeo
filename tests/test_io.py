"""
tests/test_io.py
================
Loading and saving tests for QOREgeo: ``GeoEngine.load()`` (CSV / TSV / TXT
and GeoJSON), ``load_data()``, ``add_point()`` and ``save()``.

Every file is written into pytest's ``tmp_path`` with an explicit encoding
(and ``newline=""`` for delimited text) so the suite behaves the same on
Linux, macOS and Windows. No network, browser or optional package is used.

Run with:
    pytest tests/test_io.py -v
"""

from __future__ import annotations

import contextlib
import csv
import datetime
import decimal
import json
import math
import os
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import qoregeo
from qoregeo import GeoEngine
from qoregeo.exceptions import (
    ColumnNotFoundError,
    EmptyDatasetError,
    GeoFileNotFoundError,
    InvalidArgumentError,
    InvalidCoordinateError,
    InvalidGeoJSONError,
    NoDataError,
    QOREgeoError,
    UnsupportedFormatError,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers & fixtures
# ─────────────────────────────────────────────────────────────────────────────

CITIES_CSV = (
    "name,state,zip,population,latitude,longitude\n"
    "Delhi,Delhi,110001,32900000,28.6139,77.2090\n"
    "Mumbai,Maharashtra,400001,20700000,19.0760,72.8777\n"
    '"Boston, MA",Massachusetts,02134,675647,42.3601,-71.0589\n'
    "São Paulo,SP,01000-000,12300000,-23.5505,-46.6333\n"
)

SQUARE = {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]}
LINE = {"type": "LineString", "coordinates": [[0, 0], [2, 2], [4, 0]]}


def _write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    """Write ``text`` verbatim (no newline translation) and return the path."""
    with open(path, "w", encoding=encoding, newline="") as f:
        f.write(text)
    return path


def _write_json(path: Path, obj: Any) -> Path:
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def _read_csv(path: Path, delimiter: str = ",", encoding: str = "utf-8") -> list[dict[str, str]]:
    with open(path, newline="", encoding=encoding) as f:
        return list(csv.DictReader(f, delimiter=delimiter))


def _csv_header(path: Path, delimiter: str = ",") -> list[str]:
    with open(path, newline="", encoding="utf-8") as f:
        return next(csv.reader(f, delimiter=delimiter))


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name!r}")


def _strict_json(path: Path) -> Any:
    """Parse a file as RFC 8259 JSON (NaN / Infinity are rejected)."""
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)


def _point(lng: float, lat: float, **props: Any) -> dict[str, Any]:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "properties": props}


def _same_file(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _qoregeo_warnings(records: Any) -> list[warnings.WarningMessage]:
    return [r for r in records if issubclass(r.category, UserWarning)
            and str(r.message).startswith("QOREgeo")]


@contextlib.contextmanager
def _no_warnings() -> Iterator[None]:
    """Turn any warning raised inside the block into a test failure."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        yield


@pytest.fixture
def cities_csv(tmp_path: Path) -> Path:
    return _write(tmp_path / "cities.csv", CITIES_CSV)


@pytest.fixture
def cities_geojson(tmp_path: Path) -> Path:
    fc = {
        "type": "FeatureCollection",
        "features": [
            _point(77.209, 28.6139, name="Delhi", pop=32900000),
            _point(72.8777, 19.076, name="Mumbai", pop=20700000),
            {"type": "Feature", "geometry": SQUARE, "properties": {"name": "Square"}},
        ],
    }
    return _write_json(tmp_path / "cities.geojson", fc)


# ─────────────────────────────────────────────────────────────────────────────
# CSV loading: basics
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadCsvBasics:
    def test_loads_every_row(self, cities_csv):
        assert GeoEngine().load(cities_csv).count() == 4

    def test_returns_self_for_chaining(self, cities_csv):
        geo = GeoEngine()
        assert geo.load(cities_csv) is geo

    def test_point_coordinates_are_lng_lat_floats(self, cities_csv):
        first = GeoEngine().load(cities_csv).get_features()[0]
        assert first["type"] == "Feature"
        assert first["geometry"] == {"type": "Point", "coordinates": [77.209, 28.6139]}

    def test_properties_are_strings_without_coordinate_columns(self, cities_csv):
        props = GeoEngine().load(cities_csv).get_features()[0]["properties"]
        assert props == {"name": "Delhi", "state": "Delhi", "zip": "110001",
                         "population": "32900000"}

    def test_row_order_is_preserved(self, cities_csv):
        names = [f["properties"]["name"] for f in GeoEngine().load(cities_csv)]
        assert names == ["Delhi", "Mumbai", "Boston, MA", "São Paulo"]

    def test_quoted_field_with_delimiter_and_unicode(self, cities_csv):
        feats = GeoEngine().load(cities_csv).get_features()
        assert feats[2]["properties"]["name"] == "Boston, MA"
        assert feats[3]["properties"]["name"] == "São Paulo"
        assert feats[3]["geometry"]["coordinates"] == [-46.6333, -23.5505]

    def test_records_source_path(self, cities_csv):
        geo = GeoEngine().load(cities_csv)
        assert geo._source_path == str(cities_csv)

    def test_reload_replaces_previous_data(self, cities_csv, cities_geojson):
        geo = GeoEngine().load(cities_csv).load(cities_geojson)
        assert geo.count() == 3
        assert geo._source_path == str(cities_geojson)

    @pytest.mark.parametrize("name", ["CITIES.CSV", "cities.Csv", "cities.backup.csv"])
    def test_extension_is_case_insensitive_and_last_suffix_wins(self, tmp_path, name):
        path = _write(tmp_path / name, CITIES_CSV)
        assert GeoEngine().load(path).count() == 4

    def test_boundary_coordinates_are_accepted(self, tmp_path):
        path = _write(tmp_path / "edges.csv", "lat,lng\n90,180\n-90,-180\n0,0\n")
        with _no_warnings():
            geo = GeoEngine().load(path)
        coords = [f["geometry"]["coordinates"] for f in geo]
        assert coords == [[180.0, 90.0], [-180.0, -90.0], [0.0, 0.0]]

    def test_whitespace_padded_coordinates_are_parsed(self, tmp_path):
        path = _write(tmp_path / "pad.csv", "lat,lng\n 28.6139 , 77.209 \n")
        assert GeoEngine().load(path).get_features()[0]["geometry"]["coordinates"] == [
            77.209, 28.6139,
        ]


# ─────────────────────────────────────────────────────────────────────────────
# CSV loading: delimiter detection
# ─────────────────────────────────────────────────────────────────────────────

class TestDelimiterDetection:
    @pytest.mark.parametrize(
        ("delimiter", "ext"),
        [(",", ".csv"), (";", ".csv"), ("\t", ".csv"), ("|", ".csv"),
         ("\t", ".txt"), ("|", ".txt"), (";", ".txt"), ("\t", ".tsv")],
        ids=["comma", "semicolon", "tab", "pipe", "txt-tab", "txt-pipe", "txt-semicolon", "tsv"],
    )
    def test_auto_detects_delimiter(self, tmp_path, delimiter, ext):
        text = delimiter.join(["name", "latitude", "longitude"]) + "\n" \
            + delimiter.join(["Delhi", "28.6139", "77.209"]) + "\n" \
            + delimiter.join(["Mumbai", "19.076", "72.8777"]) + "\n"
        geo = GeoEngine().load(_write(tmp_path / f"data{ext}", text))
        assert geo.count() == 2
        assert geo.get_features()[1]["properties"] == {"name": "Mumbai"}
        assert geo.get_features()[1]["geometry"]["coordinates"] == [72.8777, 19.076]

    def test_semicolon_file_keeps_commas_inside_values(self, tmp_path):
        text = "name;latitude;longitude\nBoston, MA;42.3601;-71.0589\n"
        props = GeoEngine().load(_write(tmp_path / "eu.csv", text)).get_features()[0]["properties"]
        assert props == {"name": "Boston, MA"}

    def test_tsv_extension_always_means_tab(self, tmp_path):
        # The header holds more commas than tabs, but .tsv is tab-separated by definition.
        text = "Location (city, state, country)\tlatitude\tlongitude\nDelhi, DL, IN\t28.6\t77.2\n"
        geo = GeoEngine().load(_write(tmp_path / "data.TSV", text))
        assert geo.get_features()[0]["properties"] == {
            "Location (city, state, country)": "Delhi, DL, IN"
        }

    def test_explicit_delimiter_overrides_detection(self, tmp_path):
        # Header has more commas than semicolons; the explicit delimiter wins.
        text = "full name, title;latitude;longitude\nA, B;10;20\n"
        geo = GeoEngine().load(_write(tmp_path / "x.csv", text), delimiter=";")
        assert geo.get_features()[0]["properties"] == {"full name, title": "A, B"}

    def test_explicit_delimiter_overrides_tsv_default(self, tmp_path):
        text = "name,latitude,longitude\nA,10,20\n"
        geo = GeoEngine().load(_write(tmp_path / "comma.tsv", text), delimiter=",")
        assert geo.get_features()[0]["geometry"]["coordinates"] == [20.0, 10.0]

    def test_wrong_explicit_delimiter_reports_missing_column(self, tmp_path):
        path = _write(tmp_path / "x.csv", "name,latitude,longitude\nA,10,20\n")
        with pytest.raises(ColumnNotFoundError):
            GeoEngine().load(path, delimiter=";")

    def test_tab_txt_with_commas_in_header(self, tmp_path):
        text = "Location (city, state, country)\tlatitude\tlongitude\nDelhi\t28.6\t77.2\n"
        geo = GeoEngine().load(_write(tmp_path / "export.txt", text))
        assert geo.get_features()[0]["properties"] == {"Location (city, state, country)": "Delhi"}

    def test_quoted_header_containing_other_delimiters(self, tmp_path):
        text = '"Notes; remarks; misc; other",latitude,longitude\nx,10,20\n'
        geo = GeoEngine().load(_write(tmp_path / "quoted.csv", text))
        assert geo.get_features()[0]["properties"] == {"Notes; remarks; misc; other": "x"}


# ─────────────────────────────────────────────────────────────────────────────
# CSV loading: headers and lat/lng auto-detection
# ─────────────────────────────────────────────────────────────────────────────

class TestCsvHeaders:
    def test_utf8_bom_is_ignored(self, tmp_path):
        path = tmp_path / "bom.csv"
        path.write_bytes("﻿latitude,longitude,name\n28.6,77.2,Delhi\n".encode())
        geo = GeoEngine().load(path)
        assert geo.get_features()[0]["properties"] == {"name": "Delhi"}

    def test_utf8_bom_with_semicolon_delimiter(self, tmp_path):
        path = tmp_path / "bom_eu.csv"
        path.write_bytes("﻿name;lat;lng\nDelhi;28.6;77.2\n".encode())
        assert GeoEngine().load(path).get_features()[0]["properties"] == {"name": "Delhi"}

    def test_header_whitespace_is_stripped(self, tmp_path):
        path = _write(tmp_path / "ws.csv", " name , Latitude ,  LONGITUDE \nDelhi,28.6,77.2\n")
        feat = GeoEngine().load(path).get_features()[0]
        assert feat["properties"] == {"name": "Delhi"}
        assert feat["geometry"]["coordinates"] == [77.2, 28.6]

    @pytest.mark.parametrize(
        ("lat_name", "lng_name"),
        [
            ("latitude", "longitude"), ("LATITUDE", "LONGITUDE"), ("Lat", "Lng"),
            ("lat", "lon"), ("LAT", "LONG"), ("y", "x"), ("Y", "X"),
            ("geo_lat", "geo_lng"), ("Geo_Lat", "Geo_Long"), ("point_lat", "point_lng"),
            ("latitude_deg", "longitude_deg"), ("lat_deg", "lng_deg"), ("ylat", "xlong"),
        ],
    )
    def test_auto_detects_coordinate_column_names(self, tmp_path, lat_name, lng_name):
        path = _write(tmp_path / "d.csv", f"id,{lng_name},{lat_name}\n1,77.2,28.6\n")
        feat = GeoEngine().load(path).get_features()[0]
        assert feat["geometry"]["coordinates"] == [77.2, 28.6]
        assert feat["properties"] == {"id": "1"}

    def test_named_columns_take_precedence_over_x_y(self, tmp_path):
        # x / y hold projected metres here; 'latitude' / 'longitude' must win.
        text = "x,y,latitude,longitude\n500000,4649776,42.0,-75.0\n"
        with _no_warnings():
            feat = GeoEngine().load(_write(tmp_path / "proj.csv", text)).get_features()[0]
        assert feat["geometry"]["coordinates"] == [-75.0, 42.0]
        assert feat["properties"] == {"x": "500000", "y": "4649776"}

    def test_missing_latitude_column(self, tmp_path):
        path = _write(tmp_path / "nolat.csv", "name,longitude\nA,10\n")
        with pytest.raises(ColumnNotFoundError) as exc:
            GeoEngine().load(path)
        err = exc.value
        assert err.column == "latitude"
        assert err.available == ["name", "longitude"]
        assert err.filepath == str(path)
        assert "lat_col=" in str(err)

    def test_missing_longitude_column(self, tmp_path):
        path = _write(tmp_path / "nolng.csv", "name,latitude\nA,10\n")
        with pytest.raises(ColumnNotFoundError) as exc:
            GeoEngine().load(path)
        assert exc.value.column == "longitude"

    def test_column_not_found_is_a_lookup_error(self, tmp_path):
        path = _write(tmp_path / "none.csv", "a,b\n1,2\n")
        with pytest.raises(LookupError):
            GeoEngine().load(path)


# ─────────────────────────────────────────────────────────────────────────────
# CSV loading: lat_col / lng_col overrides
# ─────────────────────────────────────────────────────────────────────────────

class TestColumnOverrides:
    TEXT = "name,lat_wgs84,lng_wgs84,lat,lng\nA,10,20,-1,-2\n"

    def _path(self, tmp_path: Path) -> Path:
        return _write(tmp_path / "ovr.csv", self.TEXT)

    def test_both_overrides(self, tmp_path):
        feat = GeoEngine().load(self._path(tmp_path), lat_col="lat_wgs84",
                                lng_col="lng_wgs84").get_features()[0]
        assert feat["geometry"]["coordinates"] == [20.0, 10.0]
        assert feat["properties"] == {"name": "A", "lat": "-1", "lng": "-2"}

    def test_lat_col_only_auto_detects_longitude(self, tmp_path):
        feat = GeoEngine().load(self._path(tmp_path), lat_col="lat_wgs84").get_features()[0]
        assert feat["geometry"]["coordinates"] == [-2.0, 10.0]
        assert "lat" in feat["properties"]
        assert "lng" not in feat["properties"]

    def test_lng_col_only_auto_detects_latitude(self, tmp_path):
        feat = GeoEngine().load(self._path(tmp_path), lng_col="lng_wgs84").get_features()[0]
        assert feat["geometry"]["coordinates"] == [20.0, -1.0]
        assert "lng" in feat["properties"]
        assert "lat" not in feat["properties"]

    def test_override_is_case_and_whitespace_insensitive(self, tmp_path):
        feat = GeoEngine().load(self._path(tmp_path), lat_col=" LAT_WGS84 ",
                                lng_col="Lng_Wgs84").get_features()[0]
        assert feat["geometry"]["coordinates"] == [20.0, 10.0]

    def test_override_matches_stripped_header(self, tmp_path):
        path = _write(tmp_path / "sp.csv", "name, my lat , my lng \nA,10,20\n")
        feat = GeoEngine().load(path, lat_col="my lat", lng_col="my lng").get_features()[0]
        assert feat["geometry"]["coordinates"] == [20.0, 10.0]

    @pytest.mark.parametrize("kwarg", ["lat_col", "lng_col"])
    def test_override_naming_missing_column(self, tmp_path, kwarg):
        path = self._path(tmp_path)
        with pytest.raises(ColumnNotFoundError) as exc:
            GeoEngine().load(path, **{kwarg: "does_not_exist"})
        err = exc.value
        assert err.column == "does_not_exist"
        assert err.available == ["name", "lat_wgs84", "lng_wgs84", "lat", "lng"]
        message = str(err)
        assert "does_not_exist" in message
        assert "geo.load(" in message
        assert "lat_col=" in message

    def test_override_pointing_at_text_column_skips_everything(self, tmp_path):
        with pytest.warns(UserWarning, match="skipped 1 row"), pytest.raises(EmptyDatasetError):
            GeoEngine().load(self._path(tmp_path), lat_col="name")


# ─────────────────────────────────────────────────────────────────────────────
# CSV loading: row validation, warnings and empty datasets
# ─────────────────────────────────────────────────────────────────────────────

class TestCsvRowValidation:
    def test_blank_coordinates_are_skipped_silently(self, tmp_path):
        text = "name,lat,lng\nA,10,20\nB,,20\nC,10,\nD,  ,\t\nE,,abc\n"
        with _no_warnings():
            geo = GeoEngine().load(_write(tmp_path / "blank.csv", text))
        assert [f["properties"]["name"] for f in geo] == ["A"]

    def test_invalid_rows_skipped_with_one_summary_warning(self, tmp_path):
        text = "name,lat,lng\nA,10,20\nB,abc,20\nC,95,20\nD,10,200\nE,11,21\n"
        with pytest.warns(UserWarning) as rec:
            geo = GeoEngine().load(_write(tmp_path / "bad.csv", text))
        assert [f["properties"]["name"] for f in geo] == ["A", "E"]
        ours = _qoregeo_warnings(rec)
        assert len(ours) == 1
        message = str(ours[0].message)
        assert "skipped 3 row(s)" in message
        assert "line 3" in message and "line 4" in message and "line 5" in message
        assert "swapped" in message

    @pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "1,5", "N/A", "28.6N", "true"])
    def test_non_numeric_or_non_finite_values_warn(self, tmp_path, bad):
        text = f"name;lat;lng\nA;10;20\nB;{bad};20\n"
        with pytest.warns(UserWarning, match="skipped 1 row"):
            geo = GeoEngine().load(_write(tmp_path / "nf.csv", text))
        assert geo.count() == 1

    def test_summary_truncates_after_five_rows(self, tmp_path):
        rows = "".join(f"P{i},999,0\n" for i in range(8))
        text = "name,lat,lng\nok,1,1\n" + rows
        with pytest.warns(UserWarning, match=r"skipped 8 row\(s\)") as rec:
            GeoEngine().load(_write(tmp_path / "many.csv", text))
        ours = _qoregeo_warnings(rec)
        assert len(ours) == 1
        assert "(+3 more)" in str(ours[0].message)

    def test_warning_points_at_the_caller(self, tmp_path):
        text = "lat,lng\n1,1\nabc,1\n"
        with pytest.warns(UserWarning) as rec:
            GeoEngine().load(_write(tmp_path / "w.csv", text))
        assert _same_file(_qoregeo_warnings(rec)[0].filename, __file__)

    def test_ragged_rows_warn_and_extra_values_dropped(self, tmp_path):
        text = "name,lat,lng\nA,10,20,EXTRA,MORE\nB,11,21\n"
        with pytest.warns(UserWarning, match="more values than header") as rec:
            geo = GeoEngine().load(_write(tmp_path / "ragged.csv", text))
        assert geo.count() == 2
        assert geo.get_features()[0]["properties"] == {"name": "A"}
        assert all(None not in f["properties"] for f in geo)
        ours = _qoregeo_warnings(rec)
        assert len(ours) == 1
        assert "1 row(s)" in str(ours[0].message)

    def test_invalid_and_ragged_rows_give_two_warnings(self, tmp_path):
        text = "lat,lng\nabc,1\n1,2,3\n"
        with pytest.warns(UserWarning) as rec:
            GeoEngine().load(_write(tmp_path / "both.csv", text))
        messages = [str(r.message) for r in _qoregeo_warnings(rec)]
        assert len(messages) == 2
        assert any("skipped 1 row" in m for m in messages)
        assert any("more values than header" in m for m in messages)

    def test_short_row_missing_extra_column_is_kept(self, tmp_path):
        text = "name,lat,lng,extra\nA,10,20\n"
        feat = GeoEngine().load(_write(tmp_path / "short.csv", text)).get_features()[0]
        assert feat["geometry"]["coordinates"] == [20.0, 10.0]
        assert feat["properties"]["name"] == "A"
        assert not feat["properties"]["extra"]

    def test_all_rows_invalid_raises_empty_dataset(self, tmp_path):
        path = _write(tmp_path / "allbad.csv", "lat,lng\nabc,1\n95,1\n")
        with pytest.warns(UserWarning, match="skipped 2 row"), \
                pytest.raises(EmptyDatasetError) as exc:
            GeoEngine().load(path)
        assert exc.value.path == str(path)

    def test_all_rows_blank_raises_empty_dataset(self, tmp_path):
        path = _write(tmp_path / "allblank.csv", "name,lat,lng\nA,,\nB,,\n")
        with pytest.raises(EmptyDatasetError):
            GeoEngine().load(path)

    @pytest.mark.parametrize("text", ["lat,lng\n", "lat,lng", "name;latitude;longitude\r\n"])
    def test_header_only_raises_empty_dataset(self, tmp_path, text):
        with pytest.raises(EmptyDatasetError):
            GeoEngine().load(_write(tmp_path / "header.csv", text))

    def test_zero_byte_file_raises_empty_dataset(self, tmp_path):
        with pytest.raises(EmptyDatasetError):
            GeoEngine().load(_write(tmp_path / "zero.csv", ""))

    def test_empty_dataset_error_is_a_value_error(self, tmp_path):
        with pytest.raises(ValueError):
            GeoEngine().load(_write(tmp_path / "header.csv", "lat,lng\n"))

    def test_crlf_line_endings(self, tmp_path):
        text = "name,lat,lng\r\nA,10,20\r\nB,11,21\r\n"
        geo = GeoEngine().load(_write(tmp_path / "crlf.csv", text))
        assert [f["properties"]["name"] for f in geo] == ["A", "B"]


# ─────────────────────────────────────────────────────────────────────────────
# CSV loading: infer_types
# ─────────────────────────────────────────────────────────────────────────────

# Known answers from the research vectors (csv_inference) plus extra edge cases.
INFER_CASES = [
    ("1000", 1000),
    ("-7", -7),
    ("+5", "+5"),        # leading + (phone numbers) stays a string
    ("0", 0),
    (" 42 ", 42),
    ("3.14", 3.14),
    ("-0.5", -0.5),
    (".5", 0.5),
    ("5.", 5.0),
    ("1e3", 1000.0),
    ("2.5E-3", 0.0025),
    ("02134", "02134"),     # ZIP code: leading zero preserved
    ("007", "007"),
    ("-007", "-007"),
    ("00.5", "00.5"),
    ("1_000", "1_000"),     # float() accepts it, but it is not a plain number
    ("1,000", "1,000"),
    ("nan", "nan"),
    ("inf", "inf"),
    ("true", True),
    ("TRUE", True),
    ("False", False),
    ("yes", "yes"),
    ("Delhi", "Delhi"),
    ("", None),
]


class TestInferTypes:
    @pytest.mark.parametrize(("raw", "expected"), INFER_CASES, ids=[repr(c[0]) for c in INFER_CASES])
    def test_value_conversion(self, tmp_path, raw, expected):
        path = tmp_path / "infer.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([["v", "lat", "lng"], [raw, "10", "20"]])
        value = GeoEngine().load(path, infer_types=True).get_features()[0]["properties"]["v"]
        assert value == expected
        assert type(value) is type(expected)

    def test_default_keeps_raw_strings(self, tmp_path):
        text = "a,b,c,d,lat,lng\n1,2.5,true,,10,20\n"
        props = GeoEngine().load(_write(tmp_path / "raw.csv", text)).get_features()[0]["properties"]
        assert props == {"a": "1", "b": "2.5", "c": "true", "d": ""}

    def test_coordinates_are_floats_and_not_in_properties(self, tmp_path):
        text = "lat,lng,count\n10,20,3\n"
        feat = GeoEngine().load(_write(tmp_path / "t.csv", text), infer_types=True).get_features()[0]
        assert feat["geometry"]["coordinates"] == [20.0, 10.0]
        assert all(isinstance(c, float) for c in feat["geometry"]["coordinates"])
        assert feat["properties"] == {"count": 3}

    def test_inferred_numbers_work_with_numeric_filters(self, cities_csv):
        geo = GeoEngine().load(cities_csv, infer_types=True)
        assert geo.get_features()[0]["properties"]["population"] == 32900000
        assert geo.get_features()[2]["properties"]["zip"] == "02134"
        assert geo.filter("population", 15_000_000, op=">").count() == 2


# ─────────────────────────────────────────────────────────────────────────────
# load(): paths, missing files and formats
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadPaths:
    def test_accepts_str_and_pathlib(self, cities_csv):
        assert GeoEngine().load(str(cities_csv)).count() == 4
        assert GeoEngine().load(Path(cities_csv)).count() == 4

    def test_accepts_any_os_pathlike(self, cities_csv):
        class _Custom:
            def __fspath__(self) -> str:
                return str(cities_csv)

        assert GeoEngine().load(_Custom()).count() == 4

    def test_missing_file(self, tmp_path):
        missing = tmp_path / "nope.csv"
        with pytest.raises(GeoFileNotFoundError) as exc:
            GeoEngine().load(missing)
        assert exc.value.path == str(missing)
        assert exc.value.filename == str(missing)
        assert "nope.csv" in str(exc.value)

    def test_missing_file_is_builtin_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            GeoEngine().load(tmp_path / "nope.geojson")

    def test_backward_compatible_alias(self, tmp_path):
        with pytest.deprecated_call():
            alias = qoregeo.FileNotFoundError
        assert alias is GeoFileNotFoundError
        with pytest.raises(alias):
            GeoEngine().load(tmp_path / "nope.csv")

    def test_directory_path_is_not_a_file(self, tmp_path):
        folder = tmp_path / "folder.csv"
        folder.mkdir()
        with pytest.raises(GeoFileNotFoundError):
            GeoEngine().load(folder)

    @pytest.mark.parametrize("ext", [".xlsx", ".kml", ".shp", ".parquet", ".XLS"])
    def test_unsupported_extension(self, tmp_path, ext):
        path = _write(tmp_path / f"data{ext}", "lat,lng\n1,2\n")
        with pytest.raises(UnsupportedFormatError) as exc:
            GeoEngine().load(path)
        assert exc.value.ext == ext.lower()
        assert isinstance(exc.value, ValueError)

    def test_no_extension(self, tmp_path):
        path = _write(tmp_path / "data", "lat,lng\n1,2\n")
        with pytest.raises(UnsupportedFormatError) as exc:
            GeoEngine().load(path)
        assert exc.value.ext == "(no extension)"

    @pytest.mark.parametrize("bad", [123, None, 4.5, ["a.csv"]])
    def test_non_path_argument(self, bad):
        with pytest.raises(InvalidArgumentError) as exc:
            GeoEngine().load(bad)
        assert exc.value.name == "path"

    def test_failed_load_keeps_previous_dataset(self, tmp_path, cities_csv):
        geo = GeoEngine().load(cities_csv)
        with pytest.raises(GeoFileNotFoundError):
            geo.load(tmp_path / "missing.csv")
        with pytest.raises(EmptyDatasetError):
            geo.load(_write(tmp_path / "empty.csv", "lat,lng\n"))
        with pytest.raises(InvalidGeoJSONError):
            geo.load(_write(tmp_path / "broken.geojson", "{not json"))
        assert geo.count() == 4
        assert geo._source_path == str(cities_csv)

    def test_explicit_encoding(self, tmp_path):
        path = tmp_path / "latin.csv"
        path.write_bytes("name,lat,lng\nSão Paulo,-23.55,-46.63\n".encode("cp1252"))
        geo = GeoEngine().load(path, encoding="cp1252")
        assert geo.get_features()[0]["properties"]["name"] == "São Paulo"

    def test_wrong_encoding_raises_value_error(self, tmp_path):
        path = tmp_path / "latin.csv"
        path.write_bytes("name,lat,lng\nSão Paulo,-23.55,-46.63\n".encode("cp1252"))
        with pytest.raises(ValueError):
            GeoEngine().load(path)


# ─────────────────────────────────────────────────────────────────────────────
# GeoJSON loading
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadGeoJson:
    def test_feature_collection(self, cities_geojson):
        geo = GeoEngine().load(cities_geojson)
        assert geo.count() == 3
        assert geo.get_features()[0]["properties"] == {"name": "Delhi", "pop": 32900000}
        assert geo.get_features()[2]["geometry"] == SQUARE

    def test_json_extension(self, tmp_path):
        path = _write_json(tmp_path / "data.JSON", {"type": "FeatureCollection",
                                                   "features": [_point(1, 2)]})
        assert GeoEngine().load(path).count() == 1

    def test_single_feature(self, tmp_path):
        path = _write_json(tmp_path / "one.geojson", _point(77.2, 28.6, name="Delhi"))
        geo = GeoEngine().load(path)
        assert geo.count() == 1
        assert geo.get_geojson()["type"] == "FeatureCollection"
        assert geo.get_features()[0]["properties"] == {"name": "Delhi"}

    @pytest.mark.parametrize("geometry", [
        {"type": "Point", "coordinates": [77.2, 28.6]},
        {"type": "MultiPoint", "coordinates": [[77.2, 28.6], [72.8, 19.0]]},
        LINE,
        {"type": "MultiLineString", "coordinates": [[[0, 0], [1, 1]], [[2, 2], [3, 3]]]},
        SQUARE,
        {"type": "MultiPolygon", "coordinates": [SQUARE["coordinates"]]},
        {"type": "GeometryCollection", "geometries": [{"type": "Point", "coordinates": [1, 2]}]},
    ], ids=lambda g: g["type"])
    def test_bare_geometry_is_wrapped(self, tmp_path, geometry):
        geo = GeoEngine().load(_write_json(tmp_path / "geom.geojson", geometry))
        assert geo.get_features() == [{"type": "Feature", "geometry": geometry, "properties": {}}]

    def test_top_level_list_of_features_and_geometries(self, tmp_path):
        data = [_point(1, 2, n=1), {"type": "Point", "coordinates": [3, 4]}]
        geo = GeoEngine().load(_write_json(tmp_path / "list.json", data))
        assert geo.count() == 2
        assert geo.get_features()[1]["geometry"]["coordinates"] == [3, 4]
        assert geo.get_features()[1]["properties"] == {}

    def test_null_and_missing_properties_become_empty_dict(self, tmp_path):
        data = [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1, 2]},
             "properties": None},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [3, 4]}},
        ]
        geo = GeoEngine().load(_write_json(tmp_path / "props.geojson", data))
        assert [f["properties"] for f in geo] == [{}, {}]

    def test_null_geometry_is_kept_and_counted(self, tmp_path):
        data = {"type": "FeatureCollection", "features": [
            _point(1, 2, name="located"),
            {"type": "Feature", "geometry": None, "properties": {"name": "unlocated"}},
            {"type": "Feature", "properties": {"name": "no geometry key"}},
        ]}
        with _no_warnings():
            geo = GeoEngine().load(_write_json(tmp_path / "nullgeom.geojson", data))
        assert geo.count() == 3
        assert geo.get_features()[1]["geometry"] is None
        assert geo.get_features()[2]["geometry"] is None

    def test_point_with_altitude_is_kept(self, tmp_path):
        data = _point(77.2, 28.6)
        data["geometry"]["coordinates"].append(216.0)
        geo = GeoEngine().load(_write_json(tmp_path / "alt.geojson", data))
        assert geo.get_features()[0]["geometry"]["coordinates"] == [77.2, 28.6, 216.0]

    def test_unsupported_and_out_of_range_features_skipped_with_one_warning(self, tmp_path):
        data = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Circle", "coordinates": [1, 2]},
             "properties": {"name": "circle"}},
            _point(77.2, 28.6, name="ok"),
            _point(10, 95, name="lat out of range"),
            _point(200, 10, name="lng out of range"),
            {"type": "Feature", "geometry": "POINT (1 2)", "properties": {}},
            {"type": "Feature", "geometry": {"type": "Point"}, "properties": {}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1]},
             "properties": {}},
        ]}
        with pytest.warns(UserWarning) as rec:
            geo = GeoEngine().load(_write_json(tmp_path / "mixed.geojson", data))
        assert [f["properties"]["name"] for f in geo] == ["ok"]
        ours = _qoregeo_warnings(rec)
        assert len(ours) == 1
        message = str(ours[0].message)
        assert "skipped 6 feature(s)" in message
        assert "#0 (unsupported geometry type 'Circle')" in message
        assert "#2 (latitude 95 is outside -90..90" in message
        assert "#3 (longitude 200 is outside" in message
        assert "(+1 more)" in message

    def test_nan_point_coordinate_is_skipped(self, tmp_path):
        path = _write(tmp_path / "nan.geojson",
                      '[{"type": "Point", "coordinates": [NaN, 10]},'
                      ' {"type": "Point", "coordinates": [1, 2]}]')
        with pytest.warns(UserWarning, match="skipped 1 feature"):
            geo = GeoEngine().load(path)
        assert geo.count() == 1

    def test_utf8_bom_geojson(self, tmp_path):
        path = tmp_path / "bom.geojson"
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps(_point(1, 2, name="Zürich"),
                                                     ensure_ascii=False).encode("utf-8"))
        assert GeoEngine().load(path).get_features()[0]["properties"] == {"name": "Zürich"}

    @pytest.mark.parametrize("text", ["{not json", "", '{"type": "Feature",}'])
    def test_invalid_json(self, tmp_path, text):
        path = _write(tmp_path / "broken.geojson", text)
        with pytest.raises(InvalidGeoJSONError) as exc:
            GeoEngine().load(path)
        assert exc.value.source == str(path)
        assert "not valid JSON" in exc.value.reason
        assert isinstance(exc.value, UnsupportedFormatError)
        assert isinstance(exc.value, ValueError)
        assert isinstance(exc.value, QOREgeoError)

    def test_invalid_geojson_caught_as_unsupported_format(self, tmp_path):
        path = _write(tmp_path / "broken.json", "[1, 2")
        with pytest.raises(UnsupportedFormatError):
            GeoEngine().load(path)

    @pytest.mark.parametrize("payload", [
        {"type": "Topology", "objects": {}},
        {"features": []},
        {"type": "FeatureCollection"},
        {"type": "FeatureCollection", "features": {"0": {}}},
        42,
        "just a string",
        None,
        [1, 2],
        [{"type": "FeatureCollection", "features": []}],
    ], ids=["topology", "no-type", "no-features", "features-dict", "number", "string",
            "null", "list-of-numbers", "nested-collection"])
    def test_wrong_structure_raises_invalid_geojson(self, tmp_path, payload):
        path = _write_json(tmp_path / "wrong.geojson", payload)
        with pytest.raises(InvalidGeoJSONError):
            GeoEngine().load(path)

    @pytest.mark.parametrize("payload", [
        {"type": "FeatureCollection", "features": []},
        [],
    ], ids=["empty-collection", "empty-list"])
    def test_no_features_raises_empty_dataset(self, tmp_path, payload):
        path = _write_json(tmp_path / "empty.geojson", payload)
        with pytest.raises(EmptyDatasetError) as exc:
            GeoEngine().load(path)
        assert exc.value.path == str(path)

    def test_all_features_skipped_raises_empty_dataset(self, tmp_path):
        path = _write_json(tmp_path / "bad.geojson", [_point(10, 95), _point(500, 0)])
        with pytest.warns(UserWarning, match="skipped 2 feature"), \
                pytest.raises(EmptyDatasetError):
            GeoEngine().load(path)

    def test_foreign_members_are_preserved(self, tmp_path):
        data = {
            "type": "FeatureCollection",
            "name": "cities",
            "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
            "features": [{
                "type": "Feature", "id": "delhi-1", "title": "Capital",
                "geometry": {"type": "Point", "coordinates": [77.2, 28.6]},
                "properties": {"name": "Delhi"},
            }],
        }
        gj = GeoEngine().load(_write_json(tmp_path / "fm.geojson", data)).get_geojson()
        assert gj["type"] == "FeatureCollection"
        assert gj["name"] == "cities"
        assert gj["crs"] == data["crs"]
        assert gj["features"][0]["id"] == "delhi-1"
        assert gj["features"][0]["title"] == "Capital"

    def test_skip_warning_points_at_the_caller(self, tmp_path):
        path = _write_json(tmp_path / "w.geojson", [_point(10, 95), _point(1, 2)])
        with pytest.warns(UserWarning) as rec:
            GeoEngine().load(path)
        assert _same_file(_qoregeo_warnings(rec)[0].filename, __file__)


# ─────────────────────────────────────────────────────────────────────────────
# load_data()
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadData:
    FEATURES = [_point(77.209, 28.6139, name="Delhi"), _point(72.8777, 19.076, name="Mumbai")]

    def test_list(self):
        assert GeoEngine().load_data(self.FEATURES).count() == 2

    def test_tuple(self):
        assert GeoEngine().load_data(tuple(self.FEATURES)).count() == 2

    def test_generator(self):
        geo = GeoEngine().load_data(f for f in self.FEATURES)
        assert [f["properties"]["name"] for f in geo] == ["Delhi", "Mumbai"]

    def test_feature_collection_dict(self):
        fc = {"type": "FeatureCollection", "name": "india", "features": self.FEATURES}
        geo = GeoEngine().load_data(fc)
        assert geo.count() == 2
        assert geo.get_geojson()["name"] == "india"

    def test_single_feature(self):
        geo = GeoEngine().load_data(self.FEATURES[0])
        assert geo.count() == 1
        assert geo.get_features()[0]["properties"] == {"name": "Delhi"}

    def test_single_geometry(self):
        geo = GeoEngine().load_data(SQUARE)
        assert geo.get_features() == [{"type": "Feature", "geometry": SQUARE, "properties": {}}]

    def test_list_of_geometries(self):
        geo = GeoEngine().load_data([SQUARE, LINE])
        assert [f["geometry"]["type"] for f in geo] == ["Polygon", "LineString"]

    def test_returns_self(self):
        geo = GeoEngine()
        assert geo.load_data(self.FEATURES) is geo

    @pytest.mark.parametrize("empty", [
        None, [], (), iter([]), {"type": "FeatureCollection", "features": []},
    ], ids=["none", "list", "tuple", "iterator", "collection"])
    def test_empty_input_raises_empty_dataset(self, empty):
        with pytest.raises(EmptyDatasetError) as exc:
            GeoEngine().load_data(empty)
        assert exc.value.path == "<in-memory data>"

    @pytest.mark.parametrize("bad", [
        [1, 2], ["Feature"], [[77.2, 28.6]], [_point(1, 2), None], "abc", 42, {},
        {"type": "Sphere"}, [{"type": "FeatureCollection", "features": []}],
    ], ids=["ints", "strings", "pairs", "none-item", "str", "int", "empty-dict",
            "bad-type", "nested-collection"])
    def test_invalid_input_raises_invalid_geojson(self, bad):
        with pytest.raises(InvalidGeoJSONError) as exc:
            GeoEngine().load_data(bad)
        assert exc.value.source == "<in-memory data>"

    def test_non_dict_item_reason_names_the_item(self):
        with pytest.raises(InvalidGeoJSONError) as exc:
            GeoEngine().load_data([_point(1, 2), 7])
        assert "item #1" in exc.value.reason
        assert "int" in exc.value.reason

    def test_resets_source_path(self, cities_csv):
        geo = GeoEngine().load(cities_csv)
        geo.load_data(self.FEATURES)
        assert geo._source_path is None
        with pytest.raises(ColumnNotFoundError) as exc:
            geo.filter("missing", 1)
        assert exc.value.filepath == "dataset"
        assert str(cities_csv) not in str(exc.value)

    def test_does_not_alias_callers_properties(self):
        feature = _point(1, 2, name="original")
        geo = GeoEngine().load_data([feature])
        geo.get_features()[0]["properties"]["name"] = "changed by engine"
        assert feature["properties"]["name"] == "original"
        feature["properties"]["name"] = "changed by caller"
        feature["properties"]["new"] = True
        assert geo.get_features()[0]["properties"] == {"name": "changed by engine"}

    def test_does_not_mutate_callers_objects(self):
        features = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [1, 2]},
                     "properties": None}, {"type": "Point", "coordinates": [3, 4]}]
        snapshot = json.loads(json.dumps(features))
        geo = GeoEngine().load_data(features)
        geo.add_point(5, 6)
        assert features == snapshot
        assert len(features) == 2

    def test_failed_load_data_keeps_previous_dataset(self):
        geo = GeoEngine().load_data(self.FEATURES)
        with pytest.raises(InvalidGeoJSONError):
            geo.load_data([1])
        with pytest.raises(EmptyDatasetError):
            geo.load_data([])
        assert geo.count() == 2

    def test_round_trip_through_get_geojson(self, cities_geojson):
        original = GeoEngine().load(cities_geojson)
        copy = GeoEngine().load_data(original.get_geojson())
        assert copy.get_features() == original.get_features()

    def test_skip_warning_points_at_the_caller(self):
        with pytest.warns(UserWarning, match="skipped 1 feature") as rec:
            GeoEngine().load_data([_point(1, 95), _point(1, 2)])
        assert _same_file(_qoregeo_warnings(rec)[0].filename, __file__)

    def test_loaded_points_always_have_numeric_coordinates(self, tmp_path):
        # Positions must be numbers (RFC 7946); string coordinates are skipped
        # with a warning rather than kept as a feature without a location.
        with pytest.warns(UserWarning, match="must contain numbers"):
            geo = GeoEngine().load_data([
                {"type": "Point", "coordinates": ["77.2", "28.6"]},
                {"type": "Point", "coordinates": [72.8, 19.0]},
            ])
        assert geo.count() == 1
        for feat in geo:
            coords = feat["geometry"]["coordinates"]
            assert all(isinstance(c, (int, float)) for c in coords), coords


# ─────────────────────────────────────────────────────────────────────────────
# add_point()
# ─────────────────────────────────────────────────────────────────────────────

class TestAddPoint:
    def test_creates_dataset(self):
        geo = GeoEngine().add_point(28.6139, 77.209, name="Delhi")
        assert geo.count() == 1
        assert geo.get_features()[0] == _point(77.209, 28.6139, name="Delhi")
        assert "1 features" in repr(geo)

    def test_returns_self_and_chains(self):
        geo = GeoEngine()
        assert geo.add_point(1, 2) is geo
        assert geo.add_point(3, 4).add_point(5, 6).count() == 3

    def test_appends_to_loaded_data(self, cities_csv):
        geo = GeoEngine().load(cities_csv).add_point(12.97, 77.59, name="Bangalore")
        assert geo.count() == 5
        assert geo.get_features()[-1]["properties"] == {"name": "Bangalore"}

    def test_numeric_strings_become_floats(self):
        coords = GeoEngine().add_point("28.6", " 77.2 ").get_features()[0]["geometry"]["coordinates"]
        assert coords == [77.2, 28.6]
        assert all(type(c) is float for c in coords)

    @pytest.mark.parametrize(("lat", "lng"), [
        (95, 0), (-90.5, 0), (0, 181), (0, -180.01), ("abc", 0), (None, 0),
        (float("nan"), 0), (0, float("inf")), (True, 0), (0, False),
    ])
    def test_invalid_coordinates(self, lat, lng):
        geo = GeoEngine()
        with pytest.raises(InvalidCoordinateError):
            geo.add_point(lat, lng)
        with pytest.raises(NoDataError):
            geo.get_features()   # a failed add_point must not create a dataset

    def test_does_not_affect_filtered_copies(self, cities_csv):
        geo = GeoEngine().load(cities_csv)
        subset = geo.filter("state", "Maharashtra")
        subset.add_point(18.52, 73.86, name="Pune")
        assert subset.count() == 2
        assert geo.count() == 4

    def test_properties_round_trip_through_save(self, tmp_path):
        out = tmp_path / "pt.geojson"
        GeoEngine().add_point(1.5, 2.5, name="x", tags=["a", "b"]).save(out)
        assert _strict_json(out)["features"][0]["properties"] == {"name": "x", "tags": ["a", "b"]}


# ─────────────────────────────────────────────────────────────────────────────
# save(): GeoJSON
# ─────────────────────────────────────────────────────────────────────────────

class TestSaveGeoJson:
    @pytest.mark.parametrize("name", ["out.geojson", "out.json", "OUT.GeoJSON"])
    def test_round_trip(self, tmp_path, cities_geojson, name):
        geo = GeoEngine().load(cities_geojson)
        out = tmp_path / name
        geo.save(out)
        assert GeoEngine().load(out).get_features() == geo.get_features()

    def test_round_trip_with_every_geometry_type(self, tmp_path):
        data = [
            _point(1, 2, kind="point"),
            {"type": "Feature", "geometry": LINE, "properties": {"kind": "line"}},
            {"type": "Feature", "geometry": SQUARE, "properties": {"kind": "polygon"}},
            {"type": "Feature", "geometry": None, "properties": {"kind": "none"}},
            {"type": "Feature", "properties": {"kind": "gc"}, "geometry": {
                "type": "GeometryCollection",
                "geometries": [{"type": "Point", "coordinates": [3, 4]}, LINE]}},
        ]
        out = tmp_path / "all.geojson"
        GeoEngine().load_data(data).save(out)
        assert GeoEngine().load(out).get_features() == GeoEngine().load_data(data).get_features()

    def test_output_is_a_feature_collection(self, tmp_path, cities_csv):
        out = tmp_path / "fc.geojson"
        GeoEngine().load(cities_csv).save(out)
        doc = _strict_json(out)
        assert doc["type"] == "FeatureCollection"
        assert len(doc["features"]) == 4

    def test_returns_self(self, tmp_path):
        geo = GeoEngine().add_point(1, 2)
        assert geo.save(tmp_path / "x.geojson") is geo

    def test_accepts_str_path(self, tmp_path):
        out = os.path.join(str(tmp_path), "s.geojson")
        GeoEngine().add_point(1, 2).save(out)
        assert os.path.isfile(out)

    def test_nan_and_infinity_become_null(self, tmp_path):
        geo = GeoEngine().add_point(10, 20, score=float("nan"), hi=float("inf"),
                                    lo=float("-inf"), nested={"v": [1.5, float("nan")]})
        out = tmp_path / "nan.geojson"
        geo.save(out)
        text = out.read_text(encoding="utf-8")
        assert "NaN" not in text and "Infinity" not in text
        props = _strict_json(out)["features"][0]["properties"]
        assert props == {"score": None, "hi": None, "lo": None, "nested": {"v": [1.5, None]}}

    def test_save_does_not_mutate_in_memory_values(self, tmp_path):
        geo = GeoEngine().add_point(10, 20, score=float("nan"), pair=(1, 2))
        geo.save(tmp_path / "keep.geojson")
        props = geo.get_features()[0]["properties"]
        assert math.isnan(props["score"])
        assert props["pair"] == (1, 2)

    def test_non_json_values_converted(self, tmp_path):
        class Thing:
            def __str__(self) -> str:
                return "a thing"

        geo = GeoEngine().add_point(
            10, 20, day=datetime.date(2024, 1, 2), price=decimal.Decimal("1.50"),
            thing=Thing(), where=Path("a") / "b",
        )
        out = tmp_path / "objs.geojson"
        geo.save(out)
        props = _strict_json(out)["features"][0]["properties"]
        assert props == {"day": "2024-01-02", "price": 1.5, "thing": "a thing",
                         "where": str(Path("a") / "b")}

    def test_tuples_are_written_as_arrays(self, tmp_path):
        geo = GeoEngine().load_data([{"type": "Point", "coordinates": (1.0, 2.0)}])
        out = tmp_path / "tuple.geojson"
        geo.save(out)
        assert _strict_json(out)["features"][0]["geometry"]["coordinates"] == [1.0, 2.0]

    def test_unicode_is_written_unescaped(self, tmp_path):
        out = tmp_path / "u.geojson"
        GeoEngine().add_point(28.6, 77.2, name="दिल्ली", city="São Paulo").save(out)
        text = out.read_text(encoding="utf-8")
        assert "दिल्ली" in text and "São Paulo" in text
        assert GeoEngine().load(out).get_features()[0]["properties"]["name"] == "दिल्ली"

    def test_foreign_members_are_written(self, tmp_path):
        data = {"type": "FeatureCollection", "name": "layer",
                "features": [dict(_point(1, 2), id=99)]}
        out = tmp_path / "fm.geojson"
        GeoEngine().load_data(data).save(out)
        doc = _strict_json(out)
        assert doc["name"] == "layer"
        assert doc["features"][0]["id"] == 99


# ─────────────────────────────────────────────────────────────────────────────
# save(): CSV / TSV
# ─────────────────────────────────────────────────────────────────────────────

class TestSaveCsv:
    def test_latitude_longitude_come_first(self, tmp_path, cities_csv):
        out = tmp_path / "out.csv"
        GeoEngine().load(cities_csv).save(out)
        assert _csv_header(out) == ["latitude", "longitude", "name", "state", "zip", "population"]
        first = _read_csv(out)[0]
        assert float(first["latitude"]) == 28.6139
        assert float(first["longitude"]) == 77.209

    def test_property_columns_are_union_in_first_seen_order(self, tmp_path):
        geo = GeoEngine().add_point(1, 2, a=1).add_point(3, 4, b=2, a=3).add_point(5, 6, c=4)
        out = tmp_path / "union.csv"
        geo.save(out)
        assert _csv_header(out) == ["latitude", "longitude", "a", "b", "c"]
        rows = _read_csv(out)
        assert [(r["a"], r["b"], r["c"]) for r in rows] == [("1", "", ""), ("3", "2", ""),
                                                           ("", "", "4")]

    def test_nested_values_are_json(self, tmp_path):
        nested = {"tags": ["x", "y"], "meta": {"k": 1, "name": "São"}}
        out = tmp_path / "nested.csv"
        GeoEngine().add_point(1, 2, **nested, pair=(1, 2)).save(out)
        row = _read_csv(out)[0]
        assert json.loads(row["tags"]) == ["x", "y"]
        assert json.loads(row["meta"]) == {"k": 1, "name": "São"}
        assert json.loads(row["pair"]) == [1, 2]

    def test_none_is_blank_and_scalars_use_str(self, tmp_path):
        out = tmp_path / "scalars.csv"
        GeoEngine().add_point(1, 2, none=None, flag=True, n=5, f=2.5).save(out)
        row = _read_csv(out)[0]
        assert (row["none"], row["flag"], row["n"], row["f"]) == ("", "True", "5", "2.5")

    def test_values_needing_quotes_survive(self, tmp_path):
        text = 'he said "hi", then\nleft; | \t'
        out = tmp_path / "quotes.csv"
        GeoEngine().add_point(1, 2, text=text).save(out)
        assert _read_csv(out)[0]["text"] == text
        assert GeoEngine().load(out).get_features()[0]["properties"]["text"] == text

    def test_coordinate_named_properties_dropped_with_warning(self, tmp_path):
        geo = GeoEngine().add_point(10, 20, latitude=99, longitude=-99, name="x")
        out = tmp_path / "clash.csv"
        with pytest.warns(UserWarning, match="clash") as rec:
            geo.save(out)
        ours = _qoregeo_warnings(rec)
        assert len(ours) == 1
        assert "latitude" in str(ours[0].message) and "longitude" in str(ours[0].message)
        assert _same_file(ours[0].filename, __file__)
        assert _csv_header(out) == ["latitude", "longitude", "name"]
        row = _read_csv(out)[0]
        assert (float(row["latitude"]), float(row["longitude"])) == (10.0, 20.0)
        # the in-memory properties are untouched
        assert geo.get_features()[0]["properties"]["latitude"] == 99

    def test_point_only_data_has_no_geometry_column(self, tmp_path):
        out = tmp_path / "pts.csv"
        with _no_warnings():
            GeoEngine().add_point(1, 2, geometry="kept as a normal column").save(out)
        assert _csv_header(out) == ["latitude", "longitude", "geometry"]
        assert _read_csv(out)[0]["geometry"] == "kept as a normal column"

    def test_non_point_features_get_representative_point_and_geometry(self, tmp_path):
        data = [
            {"type": "Feature", "geometry": SQUARE, "properties": {"name": "square"}},
            {"type": "Feature", "geometry": LINE, "properties": {"name": "line"}},
            _point(5, 6, name="point"),
        ]
        out = tmp_path / "shapes.csv"
        GeoEngine().load_data(data).save(out)
        assert _csv_header(out) == ["latitude", "longitude", "geometry", "name"]
        square, line, point = _read_csv(out)
        assert float(square["latitude"]) == pytest.approx(1.0)    # polygon area centroid
        assert float(square["longitude"]) == pytest.approx(1.0)
        assert json.loads(square["geometry"]) == SQUARE
        assert float(line["latitude"]) == pytest.approx(2 / 3, abs=1e-3)   # spherical vertex mean
        assert float(line["longitude"]) == pytest.approx(2.0, abs=1e-3)
        assert json.loads(line["geometry"]) == LINE
        assert (float(point["latitude"]), float(point["longitude"])) == (6.0, 5.0)
        assert point["geometry"] == ""

    def test_geometry_property_dropped_when_geometry_column_needed(self, tmp_path):
        data = [{"type": "Feature", "geometry": SQUARE,
                 "properties": {"geometry": "wkt here", "name": "sq"}}]
        out = tmp_path / "geomprop.csv"
        with pytest.warns(UserWarning, match="geometry"):
            GeoEngine().load_data(data).save(out)
        assert json.loads(_read_csv(out)[0]["geometry"]) == SQUARE

    def test_null_geometry_writes_blank_coordinates(self, tmp_path):
        data = [_point(1, 2, name="a"),
                {"type": "Feature", "geometry": None, "properties": {"name": "b"}}]
        out = tmp_path / "null.csv"
        GeoEngine().load_data(data).save(out)
        rows = _read_csv(out)
        assert (rows[1]["latitude"], rows[1]["longitude"], rows[1]["name"]) == ("", "", "b")

    def test_tsv_output_uses_tabs(self, tmp_path, cities_csv):
        out = tmp_path / "out.tsv"
        GeoEngine().load(cities_csv).save(out)
        first_line = out.read_text(encoding="utf-8").splitlines()[0]
        assert first_line == "latitude\tlongitude\tname\tstate\tzip\tpopulation"
        assert _read_csv(out, delimiter="\t")[2]["name"] == "Boston, MA"

    def test_unicode_output_encoding(self, tmp_path):
        out = tmp_path / "u16.csv"
        GeoEngine().add_point(1, 2, name="São Paulo").save(out, encoding="utf-16")
        assert _read_csv(out, encoding="utf-16")[0]["name"] == "São Paulo"
        geo = GeoEngine().load(out, encoding="utf-16")
        assert geo.get_features()[0]["properties"]["name"] == "São Paulo"


# ─────────────────────────────────────────────────────────────────────────────
# save(): empty data, errors and paths
# ─────────────────────────────────────────────────────────────────────────────

class TestSaveGeneral:
    def test_empty_filtered_result_to_geojson(self, tmp_path, cities_csv):
        empty = GeoEngine().load(cities_csv).filter("name", "Atlantis")
        assert empty.count() == 0
        out = tmp_path / "empty.geojson"
        assert empty.save(out) is empty
        assert _strict_json(out) == {"type": "FeatureCollection", "features": []}

    def test_empty_filtered_result_to_csv(self, tmp_path, cities_csv):
        empty = GeoEngine().load(cities_csv).head(0)
        out = tmp_path / "empty.csv"
        empty.save(out)
        assert _csv_header(out) == ["latitude", "longitude"]
        assert _read_csv(out) == []
        with pytest.raises(EmptyDatasetError):
            GeoEngine().load(out)

    @pytest.mark.parametrize("name", ["x.geojson", "x.csv", "x.xlsx", "x"])
    def test_save_before_load_raises_no_data(self, tmp_path, name):
        with pytest.raises(NoDataError) as exc:
            GeoEngine().save(tmp_path / name)
        assert exc.value.method_name == "save"
        assert not (tmp_path / name).exists()

    @pytest.mark.parametrize("ext", [".xlsx", ".kml", ".txt", ".shp", ".html"])
    def test_unsupported_extension(self, tmp_path, ext):
        target = tmp_path / "newdir" / f"out{ext}"
        with pytest.raises(UnsupportedFormatError) as exc:
            GeoEngine().add_point(1, 2).save(target)
        assert exc.value.ext == ext
        assert not target.parent.exists()   # nothing is created on failure

    def test_no_extension(self, tmp_path):
        with pytest.raises(UnsupportedFormatError) as exc:
            GeoEngine().add_point(1, 2).save(tmp_path / "out")
        assert exc.value.ext == "(no extension)"

    @pytest.mark.parametrize("name", ["out.geojson", "out.csv", "out.tsv"])
    def test_creates_missing_parent_directories(self, tmp_path, name):
        target = tmp_path / "a" / "b" / "c" / name
        GeoEngine().add_point(1, 2).save(target)
        assert target.is_file()

    def test_relative_path_in_current_directory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        GeoEngine().add_point(1, 2).save("here.geojson")
        assert (tmp_path / "here.geojson").is_file()

    def test_overwrites_existing_file(self, tmp_path):
        out = tmp_path / "over.geojson"
        GeoEngine().add_point(1, 2).add_point(3, 4).save(out)
        GeoEngine().add_point(5, 6).save(out)
        assert len(_strict_json(out)["features"]) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Round trips
# ─────────────────────────────────────────────────────────────────────────────

class TestRoundTrips:
    def test_csv_save_load_preserves_values(self, tmp_path, cities_csv):
        original = GeoEngine().load(cities_csv)
        out = tmp_path / "copy.csv"
        original.save(out)
        assert GeoEngine().load(out).get_features() == original.get_features()

    def test_csv_round_trip_with_infer_types(self, tmp_path, cities_csv):
        original = GeoEngine().load(cities_csv, infer_types=True)
        out = tmp_path / "typed.csv"
        original.save(out)
        again = GeoEngine().load(out, infer_types=True)
        assert again.get_features() == original.get_features()
        assert again.get_features()[2]["properties"]["zip"] == "02134"
        assert again.get_features()[0]["properties"]["population"] == 32900000

    def test_semicolon_csv_to_comma_csv(self, tmp_path):
        src = _write(tmp_path / "eu.csv", "name;lat;lng\nBerlin;52.52;13.405\n")
        out = tmp_path / "out.csv"
        GeoEngine().load(src).save(out)
        assert out.read_text(encoding="utf-8").splitlines()[0] == "latitude,longitude,name"
        assert GeoEngine().load(out).get_features() == GeoEngine().load(src).get_features()

    def test_tsv_round_trip(self, tmp_path, cities_csv):
        original = GeoEngine().load(cities_csv)
        out = tmp_path / "copy.tsv"
        original.save(out)
        assert GeoEngine().load(out).get_features() == original.get_features()

    def test_csv_to_geojson_and_back(self, tmp_path, cities_csv):
        original = GeoEngine().load(cities_csv)
        gj = tmp_path / "cities.geojson"
        original.save(gj)
        assert GeoEngine().load(gj).get_features() == original.get_features()
        back = tmp_path / "back.csv"
        GeoEngine().load(gj).save(back)
        assert GeoEngine().load(back).get_features() == original.get_features()

    def test_geojson_points_to_csv_keep_coordinates(self, tmp_path, cities_geojson):
        out = tmp_path / "pts.csv"
        GeoEngine().load(cities_geojson).save(out)
        reloaded = GeoEngine().load(out, infer_types=True)
        assert reloaded.count() == 3
        assert reloaded.get_features()[0]["geometry"]["coordinates"] == [77.209, 28.6139]
        assert reloaded.get_features()[0]["properties"] == {"name": "Delhi", "pop": 32900000}
        square = reloaded.get_features()[2]
        # The geometry column written by save() restores the polygon on load.
        assert square["geometry"] == SQUARE
        assert "geometry" not in square["properties"]

    def test_filtered_subset_round_trip(self, tmp_path, cities_csv):
        subset = GeoEngine().load(cities_csv).filter("state", "Maharashtra")
        out = tmp_path / "mh.geojson"
        subset.save(out)
        assert [f["properties"]["name"] for f in GeoEngine().load(out)] == ["Mumbai"]
