"""
tests/test_docs.py
==================
The documentation must match the code: README.md (which is also the PyPI
project page), the Agent Skill and its references, AGENTS.md, llms.txt, the
CHANGELOG and the guide in docs/. Examples are executed where that is cheap
and offline; everything else is checked against the real signatures and
presets. The last two sections run every example on the docs/*.md pages
and check the writing style of all Markdown files.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import inspect
import io
import json
import re
import shlex
import shutil
import sys
import urllib.request
import webbrowser
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

import qoregeo
from qoregeo import GeoEngine
from qoregeo.geocode import Geocoder
from qoregeo.map_builder import _TILES

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
SKILL = ROOT / "skills" / "qoregeo" / "SKILL.md"
API_REF = ROOT / "skills" / "qoregeo" / "references" / "python-api.md"
RECIPES = ROOT / "skills" / "qoregeo" / "references" / "recipes.md"
CHANGELOG = ROOT / "CHANGELOG.md"
AGENTS = ROOT / "AGENTS.md"
LLMS = ROOT / "llms.txt"
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"

GITHUB_BLOB = "https://github.com/bosekarmegam/qoregeo/blob/main/"
# A fenced block, also one indented inside a list item.
FENCE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<lang>[\w+-]*)[^\n]*\n"
    r"(?P<body>.*?)^[ \t]*(?P=fence)[ \t]*$",
    re.DOTALL | re.MULTILINE,
)


def read(path: Path) -> str:
    """The file's text, or skip: an sdist ships only some of these files."""
    if not path.is_file():
        pytest.skip(f"{path.relative_to(ROOT)} is not part of this checkout")
    return path.read_text(encoding="utf-8")


def code_blocks(text: str, lang: str) -> list[tuple[int, str]]:
    """(first line number, source) of every fenced block in language ``lang``."""
    blocks = []
    for m in FENCE_RE.finditer(text):
        if m.group("lang") != lang:
            continue
        body, indent = m.group("body"), m.group("indent")
        if indent:   # a block inside a list item: take off the item's indentation
            body = "".join(line[len(indent):] if line.startswith(indent) else line.lstrip(" \t")
                           for line in body.splitlines(keepends=True))
        blocks.append((text.count("\n", 0, m.start()) + 2, body))
    return blocks


def prose(text: str) -> str:
    """The text without fenced blocks and inline code (neither renders links)."""
    text = FENCE_RE.sub("", text)
    return re.sub(r"`[^`\n]*`", "", text)


def paragraphs(text: str) -> list[str]:
    """Prose paragraphs; each table row counts as a paragraph of its own."""
    out: list[str] = []
    for block in re.split(r"\n\s*\n", FENCE_RE.sub("", text)):
        rows = [line for line in block.splitlines() if line.lstrip().startswith("|")]
        out.extend(rows)
        rest = "\n".join(line for line in block.splitlines() if line not in rows)
        if rest.strip():
            out.append(rest)
    return out


def section(text: str, heading: str) -> str:
    """The body of the markdown section titled ``heading`` (up to the next ##)."""
    match = re.search(rf"^##+ {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", text,
                      re.MULTILINE | re.DOTALL)
    assert match, f"no '{heading}' section"
    return match.group(1)


# ─── README is also the PyPI page ────────────────────────────────────────────

class TestReadmeLinks:
    def test_links_work_outside_github(self) -> None:
        # PyPI resolves relative links against pypi.org/project/qoregeo/, where they 404.
        text = prose(read(README))
        targets = re.findall(r"\]\(([^)\s]+)", text) + re.findall(r'(?:href|src)="([^"]+)"', text)
        assert targets
        relative = [t for t in targets if not t.startswith(("https://", "http://", "mailto:", "#"))]
        assert relative == []

    def test_repository_links_point_at_files_that_exist(self) -> None:
        text = prose(read(README))
        paths = re.findall(re.escape(GITHUB_BLOB) + r"([^)\s#]+)", text)
        assert "CHANGELOG.md" in paths and "skills/qoregeo/SKILL.md" in paths
        if not (ROOT / "examples").is_dir():
            pytest.skip("not a full repository checkout")
        assert [p for p in paths if not (ROOT / p).exists()] == []


# ─── Examples that call show() ────────────────────────────────────────────────

def _blocking_show_calls(source: str) -> list[int]:
    """Line numbers of show() calls that block in a script but aren't the last statement."""
    try:
        body = ast.parse(source).body
    except SyntaxError:
        return []
    bad = []
    for index, stmt in enumerate(body):
        for node in ast.walk(stmt):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "show"):
                continue
            background = any(k.arg == "block" and isinstance(k.value, ast.Constant)
                             and k.value.value is False for k in node.keywords)
            if not background and index != len(body) - 1:
                bad.append(node.lineno)
    return bad


@pytest.mark.parametrize("path", [README, SKILL, API_REF, RECIPES], ids=lambda p: p.name)
def test_show_comes_last_in_python_examples(path: Path) -> None:
    # show() serves until Ctrl+C in a script, so code after it never runs.
    text = read(path)
    blocked = [
        f"{path.name}:{start + line - 1}"
        for start, source in code_blocks(text, "python")
        for line in _blocking_show_calls(source)
    ]
    assert blocked == []


def test_blocking_show_detector() -> None:
    assert _blocking_show_calls("geo.show()\ngeo.save('x.csv')\n") == [1]
    assert _blocking_show_calls("geo.save('x.csv')\ngeo.show()\n") == []
    assert _blocking_show_calls("url = geo.show(block=False)\ngeo.save('x.csv')\n") == []
    assert _blocking_show_calls("geo.show(block=True)\nx = 1\n") == [1]


def record_show(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace GeoEngine.show (which would serve until Ctrl+C); return its calls."""
    calls: list[dict[str, Any]] = []

    def show(self: GeoEngine, *args: Any, **kwargs: Any) -> str:
        assert self.count() > 0
        calls.append(kwargs)
        return "http://127.0.0.1:0/map.html"

    monkeypatch.setattr(GeoEngine, "show", show)
    return calls


def _with_print_checks(source: str) -> tuple[str, list[tuple[int, str]]]:
    """
    Rewrite ``print(expr)  # expected`` lines into checks; return the new source
    and the (line, expected) pairs. The comment's first column is the output.
    """
    expectations: list[tuple[int, str]] = []
    lines = source.splitlines()
    for number, line in enumerate(lines, 1):
        match = re.match(r"^print\((.*?)\)\s*#\s*(.+)$", line)
        if match:
            expected = re.split(r"\s{2,}", match.group(2).strip())[0]
            expectations.append((number, expected))
            lines[number - 1] = f"_check({number}, {match.group(1)})"
    return "\n".join(lines) + "\n", expectations


def test_readme_quick_start_runs_to_the_end_and_prints_what_it_says(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = read(README)
    quick_start = section(text, "Quick Start")
    sample = re.search(r"```csv\n(name,state,population,latitude,longitude\n.*?)```",
                       quick_start, re.DOTALL)
    assert sample, "the cities.csv sample is missing"
    (tmp_path / "cities.csv").write_text(sample.group(1), encoding="utf-8")
    source = code_blocks(quick_start, "python")[0][1]
    shows = record_show(monkeypatch)
    monkeypatch.chdir(tmp_path)

    checked, expectations = _with_print_checks(source)
    assert len(expectations) >= 10
    printed: dict[int, str] = {}
    exec(compile(checked, "README Quick Start", "exec"),
         {"_check": lambda line, value: printed.__setitem__(line, str(value))})

    assert {line: printed.get(line) for line, _ in expectations} == dict(expectations)
    assert len(shows) == 1
    for name in ("output.html", "heat.html", "cities.png", "output.geojson", "output.csv"):
        assert (tmp_path / name).is_file(), name


def test_skill_python_quick_reference_runs_to_the_end(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = code_blocks(section(read(SKILL), "Python quick reference"), "python")[0][1]
    (tmp_path / "stores.csv").write_text(
        "name,city,revenue,latitude,longitude\n"
        "CP,Delhi,250000,28.6315,77.2167\n"
        "Lodhi,Delhi,90000,28.5933,77.2190\n"
        "Bandra,Mumbai,180000,19.0596,72.8295\n",
        encoding="utf-8",
    )
    shows = record_show(monkeypatch)
    monkeypatch.chdir(tmp_path)
    exec(compile(source, "SKILL.md", "exec"), {})
    assert len(shows) == 1
    for name in ("near.geojson", "near.png", "near.html"):
        assert (tmp_path / name).is_file(), name


# ─── python-api.md is "every GeoEngine method and parameter" ──────────────────

def _documented_calls(text: str) -> list[tuple[str, ast.Call]]:
    """The ``method(args)`` signatures in the first column of the reference tables."""
    calls = []
    for row in re.findall(r"^\|([^|\n]+)\|", text, re.MULTILINE):
        for snippet in re.findall(r"`([A-Za-z_]\w*\(.*?\))`", row):
            name = snippet.split("(", 1)[0]
            if not callable(getattr(GeoEngine, name, None)):
                continue
            call = ast.parse(snippet, mode="eval").body
            assert isinstance(call, ast.Call), snippet
            calls.append((name, call))
    return calls


def test_api_reference_documents_every_public_method() -> None:
    text = read(API_REF)
    documented = {name for name, _ in _documented_calls(text)}
    public = {name for name, value in vars(GeoEngine).items()
              if not name.startswith("_") and callable(value)}
    assert public - documented == set()


def test_api_reference_signatures_match_the_code() -> None:
    problems = []
    for name, call in _documented_calls(read(API_REF)):
        if any(isinstance(a, ast.Constant) and a.value is Ellipsis for a in call.args):
            continue   # `map_html(...)` means "same options as"
        params = list(inspect.signature(getattr(GeoEngine, name)).parameters.values())[1:]
        named = [p for p in params if p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)]
        covered = {p.name for p in named[:len(call.args)]}
        if len(call.args) > len(named):
            problems.append(f"{name}: too many positional parameters")
        for kw in call.keywords:
            if kw.arg is None:   # **options
                if not any(p.kind is p.VAR_KEYWORD for p in params):
                    problems.append(f"{name}: no **kwargs")
                continue
            real = next((p for p in named if p.name == kw.arg), None)
            if real is None:
                problems.append(f"{name}: no parameter {kw.arg!r}")
                continue
            covered.add(kw.arg)
            if real.default is not real.empty and ast.literal_eval(kw.value) != real.default:
                problems.append(f"{name}: {kw.arg} defaults to {real.default!r}")
        missing = [p.name for p in named if p.name not in covered]
        if missing:
            problems.append(f"{name}: undocumented {missing}")
    assert problems == []


def test_api_reference_nearest_returns_what_it_says() -> None:
    row = next(line for line in read(API_REF).splitlines() if line.startswith("| `nearest("))
    result = GeoEngine(verbose=False).add_point(28.6139, 77.2090, name="Delhi").nearest(
        (28.61, 77.20))
    for key in result:
        assert f'"{key}"' in row
    assert "_distance" not in row


def test_api_reference_where_example_works_on_a_default_csv_load(tmp_path: Path) -> None:
    row = next(line for line in read(API_REF).splitlines() if line.startswith("| `where("))
    example = re.search(r"`(geo\.where\(.+?\))`", row)
    assert example
    data = tmp_path / "places.csv"
    data.write_text("name,type,rating,latitude,longitude\n"
                    "A,cafe,4.5,28.6,77.2\nB,cafe,3.9,28.7,77.1\nC,bar,4.8,28.5,77.3\n",
                    encoding="utf-8")
    geo = GeoEngine(verbose=False).load(data)   # values stay strings
    result = eval(example.group(1), {"geo": geo})
    assert [f["properties"]["name"] for f in result] == ["A"]


@pytest.mark.parametrize("path", [README, SKILL, API_REF, RECIPES], ids=lambda p: p.name)
def test_api_references_use_real_keyword_arguments(path: Path) -> None:
    # e.g. `geo.map(path=...)`: the parameter is output_path.
    problems = []
    for _, source in code_blocks(read(path), "python"):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            method = getattr(GeoEngine, node.func.attr, None)
            if not callable(method) or node.func.attr.startswith("_"):
                continue
            params = inspect.signature(method).parameters
            if any(p.kind is p.VAR_KEYWORD for p in params.values()):
                continue
            problems += [f"{node.func.attr}({k.arg}=…)" for k in node.keywords
                         if k.arg is not None and k.arg not in params]
    assert problems == []


# ─── Facts the docs state ─────────────────────────────────────────────────────

def test_documented_exception_bases_are_real() -> None:
    errors = section(read(API_REF), "Errors")
    claimed = {name for name in re.findall(r"`(\w+)`", errors)
               if isinstance(getattr(builtins, name, None), type)
               and issubclass(getattr(builtins, name), BaseException)}
    assert claimed
    classes = [value for value in vars(qoregeo.exceptions).values()
               if isinstance(value, type) and issubclass(value, qoregeo.QOREgeoError)]
    bases = {base for cls in classes for base in cls.__mro__}
    not_a_base = {name for name in claimed if getattr(builtins, name) not in bases}
    # "a LookupError, not a KeyError" names KeyError only to rule it out.
    assert not_a_base <= {"KeyError"}
    assert "not a `KeyError`" in errors or "KeyError" not in errors
    assert not issubclass(qoregeo.ColumnNotFoundError, KeyError)


def test_readme_exception_table_matches_the_classes() -> None:
    rows = re.findall(r"^\| `(\w+)` \| `(\w+)` \|", read(README), re.MULTILINE)
    assert len(rows) >= 10
    for name, also in rows:
        cls = getattr(qoregeo.exceptions, name)
        base = getattr(builtins, also, None) or getattr(qoregeo.exceptions, also)
        assert issubclass(cls, base), (name, also)


@pytest.mark.parametrize("path", [SKILL, API_REF], ids=lambda p: p.name)
def test_area_is_described_as_spherical(path: Path) -> None:
    # area() uses a sphere of mean Earth radius, not the WGS84 ellipsoid.
    lines = [line for line in read(path).splitlines() if re.search(r"\b[Aa]reas?\b", line)
             and ("area(" in line or "Areas" in line)]
    assert lines
    for line in lines:
        assert "spherical" in line, line
        assert "geodesic" not in line and "use the WGS84" not in line, line


def test_readme_basemap_count() -> None:
    match = re.search(r"(\d+) open basemaps", read(README))
    assert match
    presets = qoregeo.tile_presets()
    assert "none" in presets
    assert int(match.group(1)) == len(presets) - 1   # 'none' is not a basemap


DISK_SAFE = {name for name, preset in _TILES.items() if not preset["needsReferer"]}


@pytest.mark.parametrize("path", [README, SKILL, API_REF, RECIPES, LLMS], ids=lambda p: p.name)
def test_docs_agree_on_basemaps_that_work_from_disk(path: Path) -> None:
    text = read(path)
    named: set[str] = set()
    for para in paragraphs(text):
        if "from disk" not in para or "work" not in para:
            continue
        names = set(re.findall(r'["`]([a-z-]+)["`]', para)) & set(_TILES)
        assert names <= DISK_SAFE, para
        named |= names
    assert named == DISK_SAFE


def test_readme_wheel_size_claim() -> None:
    # The wheel holds the package, the skill and README.md (as METADATA), deflated.
    if not (ROOT / "qoregeo" / "engine.py").is_file():
        pytest.skip("package sources not available")
    text = read(README)
    match = re.search(r"~(\d+) KB pure-Python wheel", text)
    assert match
    files = [p for p in (ROOT / "qoregeo").glob("*.py")]
    files += [p for p in (ROOT / "skills" / "qoregeo").rglob("*.md")]
    files += [README, ROOT / "LICENSE"]
    estimate = sum(len(zlib.compress(p.read_bytes(), 6)) + 150 for p in files if p.is_file())
    claimed = int(match.group(1)) * 1000
    assert abs(estimate - claimed) <= 0.15 * claimed, f"wheel ≈ {estimate / 1000:.0f} KB"


# ─── CHANGELOG, AGENTS.md ─────────────────────────────────────────────────────

def test_changelog_unreleased_is_on_top_and_lists_only_unshipped_work() -> None:
    text = read(CHANGELOG)
    headings = re.findall(r"^## \[([^\]]+)\]", text, re.MULTILINE)
    assert headings[0] == "Unreleased"
    unreleased = section(text, "[Unreleased]")
    for name in re.findall(r"`(\w+)\(\)`", unreleased):
        assert not hasattr(GeoEngine, name), f"{name}() has shipped"
    for shipped in ("Street address geocoding", "PNG / SVG static map export"):
        assert shipped not in unreleased


def test_agents_md_describes_ci_accurately() -> None:
    agents, workflow = read(AGENTS), read(TESTS_WORKFLOW)
    versions = re.search(r'python-version: \[([^\]]+)\]', workflow)
    assert versions
    matrix = [v.strip().strip('"') for v in versions.group(1).split(",")]
    # "Python 3.10 to 3.15"; older text used an en dash, which the style rules now ban.
    assert (f"Python {matrix[0]} to {matrix[-1]}" in agents
            or f"Python {matrix[0]}\N{EN DASH}{matrix[-1]}" in agents)
    lint_job = workflow.split("\n  test:")[0]
    assert "ruff" in lint_job and "mypy" in lint_job and "ubuntu-latest" in lint_job
    lint_python = re.search(r'python-version: "([\d.]+)"', lint_job)
    assert lint_python
    sentence = re.search(r"CI \(.*?\)(.*?)\.\n", agents.replace("\n", " ") + ".\n")
    assert sentence and "Ubuntu" in sentence.group(1) and lint_python.group(1) in sentence.group(1)
    if "--import-mode=importlib" in agents:
        assert "--import-mode=importlib" in read(PUBLISH_WORKFLOW)
        assert "--import-mode=importlib" not in workflow
        assert "release workflow" in agents


# ─── The guide in docs/: every example runs ──────────────────────────────────
#
# Conventions for docs/*.md (they are also how a page shows its output):
#
# * ```python blocks of one page run top to bottom in ONE namespace, in a
#   temporary directory holding a copy of every file in docs/data/, so
#   examples load the samples by bare name ("stores.csv").
# * A top-level line ``print(expr)  # expected`` is a check: str(expr) must
#   equal the comment up to its first run of two spaces. Any top-level print
#   with a trailing comment is checked, so put notes on their own line.
# * A python block whose first line is ``# needs network`` is compiled, not run.
# * GeoEngine.show() is a stub; a blocking show() must end its block.
# * In ```bash (also sh, shell, console) blocks, each line that starts with
#   ``qoregeo `` (optionally after ``$ ``) runs through qoregeo.cli.main() in
#   the same directory and must exit 0. Not run: serve, mcp, geocode, reverse,
#   lines with --serve and lines ending in ``# needs network``. ``> file``,
#   ``>> file`` and ``< file`` work; after ``|`` the rest of the line is ignored.
# * ```json blocks must parse as JSON, ```toml blocks with tomllib (3.11+).
# * Relative links must point at files that exist, #anchors at real headings.
#
# Nothing may touch the network: Geocoder._fetch and urllib's urlopen fail the
# test, the browser never opens and HOME points into the temporary directory.

DOCS = ROOT / "docs"
DOCS_DATA = DOCS / "data"
DOC_PAGES = sorted(DOCS.glob("*.md"))
NETWORK_MARK = "# needs network"
NOT_RUN_COMMANDS = frozenset({"serve", "mcp", "geocode", "reverse"})
SHELL_LANGS = ("bash", "sh", "shell", "console")
_SHELL_OPERATORS = frozenset("();<>|&")


def _page_id(path: Path) -> str:
    return path.name


@dataclass
class DocSandbox:
    """Where a docs page runs, and what its examples tried to do."""

    work: Path
    shows: list[dict[str, Any]]
    network: list[str]


@pytest.fixture
def doc_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DocSandbox:
    work = tmp_path / "work"
    if DOCS_DATA.is_dir():
        shutil.copytree(DOCS_DATA, work)
    else:
        work.mkdir()
    home = tmp_path / "home"   # `qoregeo skill install` writes to ~
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    # `qoregeo call` / ToolContext read these; the root must be the work directory.
    for name in ("QOREGEO_ROOT", "CLAUDE_PROJECT_DIR", "QOREGEO_ALLOW_NETWORK",
                 "QOREGEO_GEOCODER_URL", "QOREGEO_GEOCODER_EMAIL"):
        monkeypatch.delenv(name, raising=False)

    network: list[str] = []

    def refuse(url: Any) -> None:
        target = getattr(url, "full_url", url)
        network.append(str(target))
        raise AssertionError(f"a docs example tried to reach the network ({target}); "
                             f"start its block with '{NETWORK_MARK}'")

    monkeypatch.setattr(Geocoder, "_fetch", lambda self, url: refuse(url))
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, *a, **k: refuse(url))
    monkeypatch.setattr(webbrowser, "open", lambda *args, **kwargs: True)

    def no_serve(server: Any, url: str, *args: Any, **kwargs: Any) -> str:
        server.server_close()   # serve_file() / serve_html() would block until Ctrl+C
        return url

    monkeypatch.setattr(importlib.import_module("qoregeo.serve"), "_run", no_serve)
    shows = record_show(monkeypatch)
    monkeypatch.chdir(work)
    return DocSandbox(work, shows, network)


def _doc_name(page: Path) -> str:
    return page.relative_to(ROOT).as_posix()


def _needs_network(source: str) -> bool:
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    return bool(lines) and lines[0].lower().startswith(NETWORK_MARK)


@pytest.mark.parametrize("page", DOC_PAGES, ids=_page_id)
def test_doc_python_examples_run(page: Path, doc_sandbox: DocSandbox) -> None:
    name, filename = _doc_name(page), str(page)
    expected: dict[int, str] = {}
    wrong: list[str] = []

    def check(line: int, *values: Any, sep: str | None = " ", **_: Any) -> None:
        printed = (" " if sep is None else sep).join(str(v) for v in values)
        print(printed)
        if printed != expected[line]:
            wrong.append(f"{name}:{line}: printed {printed!r}, the comment says {expected[line]!r}")

    namespace: dict[str, Any] = {"__name__": "__main__", "_check": check}
    for start, source in code_blocks(read(page), "python"):
        # Padding with blank lines gives tracebacks and checks the page's line numbers.
        padded = "\n" * (start - 1) + source
        blocking = [f"{name}:{line}" for line in _blocking_show_calls(padded)]
        assert not blocking, (f"{', '.join(blocking)}: show() serves until Ctrl+C in a script; "
                              f"make it the block's last statement or pass block=False")
        if _needs_network(source):
            compile(padded, filename, "exec")
            continue
        checked, expectations = _with_print_checks(padded)
        expected.update(expectations)
        try:
            exec(compile(checked, filename, "exec"), namespace)
        except BaseException:
            if wrong:   # shown under "Captured stdout call"
                print("\nprint checks that failed before the error:\n" + "\n".join(wrong))
            raise
    assert doc_sandbox.network == []
    assert not wrong, "\n".join(wrong)


def doc_commands(text: str) -> list[tuple[int, str]]:
    """(page line, command) of every shell-block line that starts with ``qoregeo ``."""
    found = []
    for lang in SHELL_LANGS:
        for start, source in code_blocks(text, lang):
            for offset, raw in enumerate(source.splitlines()):
                line = raw.strip()
                if line.startswith("$ "):
                    line = line[2:].lstrip()
                if line.startswith("qoregeo "):
                    found.append((start + offset, line))
    return sorted(found)


def _shell_words(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _is_run(command: str, argv: list[str]) -> bool:
    if re.search(r"#\s*needs network\s*$", command):
        return False
    subcommand = next((word for word in argv[1:] if not word.startswith("-")), None)
    return subcommand not in NOT_RUN_COMMANDS and "--serve" not in argv


@pytest.mark.parametrize("page", DOC_PAGES, ids=_page_id)
def test_doc_cli_examples_run(page: Path, doc_sandbox: DocSandbox,
                              capsys: pytest.CaptureFixture[str],
                              monkeypatch: pytest.MonkeyPatch) -> None:
    from qoregeo.cli import main

    name = _doc_name(page)
    stdin = sys.stdin
    for line, command in doc_commands(read(page)):
        where = f"{name}:{line}: {command}"
        assert not command.endswith("\\"), f"{where}\n  write the command on one line"
        argv: list[str] = []
        redirects: dict[str, str] = {}
        words = iter(_shell_words(command))
        for word in words:
            if word == "|":
                break   # a pipeline: only the qoregeo part runs
            if word in (">", ">>", "<"):
                target = next(words, "")
                assert target and not Path(target).is_absolute() and ".." not in Path(target).parts, \
                    f"{where}\n  redirect to a file in the current directory"
                redirects[word] = target
            elif set(word) <= _SHELL_OPERATORS:
                pytest.fail(f"{where}\n  {word!r} is not supported; write one command per line")
            else:
                argv.append(word)
        if not _is_run(command, argv):
            continue

        if "<" in redirects:
            data = (doc_sandbox.work / redirects["<"]).read_bytes()
            monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))
        else:
            monkeypatch.setattr(sys, "stdin", stdin)
        capsys.readouterr()
        try:
            code = main(argv[1:])
        except SystemExit as exc:   # argparse errors, --help and --version
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        out, err = capsys.readouterr()
        assert code == 0, f"{where}\n  exited with {code}:\n{err or out}"
        for op in (">", ">>"):
            if op in redirects:
                with open(doc_sandbox.work / redirects[op], "a" if op == ">>" else "w",
                          encoding="utf-8") as fh:
                    fh.write(out)
    assert doc_sandbox.network == []


@pytest.mark.parametrize("page", DOC_PAGES, ids=_page_id)
def test_doc_json_and_toml_blocks_parse(page: Path) -> None:
    text, name = read(page), _doc_name(page)
    problems = []
    for start, source in code_blocks(text, "json"):
        try:
            json.loads(source)
        except ValueError as exc:
            line = start + getattr(exc, "lineno", 1) - 1
            problems.append(f"{name}:{line}: not valid JSON: {getattr(exc, 'msg', exc)}")

    toml_blocks = code_blocks(text, "toml")
    try:
        tomllib: Any = importlib.import_module("tomllib")
    except ModuleNotFoundError:   # Python 3.10
        tomllib = None
    for start, source in toml_blocks if tomllib else []:
        try:
            tomllib.loads(source)
        except tomllib.TOMLDecodeError as exc:
            problems.append(f"{name}:{start}: not valid TOML (block line numbers): {exc}")
    assert not problems, "\n".join(problems)
    if toml_blocks and tomllib is None:
        pytest.skip("JSON checked; checking TOML needs tomllib (Python 3.11+)")


def github_slug(heading: str) -> str:
    """The #anchor GitHub gives a heading."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", heading)        # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)       # links keep their text
    text = re.sub(r"<[^>]+>", "", text)                         # HTML tags
    text = re.sub(r"[^\w\- ]", "", text.strip().lower())       # \w keeps letters, digits, _
    return text.replace(" ", "-")


def heading_anchors(text: str) -> set[str]:
    """Every #anchor a Markdown file has: headings (GitHub rules) and id=/name= attributes."""
    text = FENCE_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    anchors: set[str] = set()
    seen: Counter[str] = Counter()
    for m in re.finditer(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", text, re.MULTILINE):
        slug = github_slug(m.group(1))
        anchors.add(f"{slug}-{seen[slug]}" if seen[slug] else slug)
        seen[slug] += 1
    anchors.update(re.findall(r"""\b(?:id|name)\s*=\s*["']([^"']+)["']""", text))
    return anchors


def relative_links(text: str) -> list[tuple[int, str]]:
    """(line, target) of every link and image in the prose that is not a URL."""
    text = FENCE_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    text = re.sub(r"`[^`\n]*`", "", text)
    found = []
    patterns = (r"\]\(\s*<?([^)\s>]+)", r"^\s{0,3}\[[^\]]+\]:\s*<?([^\s>]+)",
                r"""\b(?:href|src)\s*=\s*["']([^"']+)["']""")
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.MULTILINE):
            target = m.group(1)
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:|^//", target):
                continue   # https:, mailto:, data: ...
            found.append((text.count("\n", 0, m.start()) + 1, target))
    return sorted(found)


@pytest.mark.parametrize("page", DOC_PAGES, ids=_page_id)
def test_doc_relative_links_resolve(page: Path) -> None:
    text, name = read(page), _doc_name(page)
    problems = []
    for line, target in relative_links(text):
        path_part, _, anchor = target.partition("#")
        dest = (page.parent / unquote(path_part)).resolve() if path_part else page
        if not dest.exists():
            problems.append(f"{name}:{line}: {target}: no such file")
        elif anchor and dest.suffix == ".md" and dest.is_file():
            if unquote(anchor) not in heading_anchors(dest.read_text(encoding="utf-8")):
                problems.append(f"{name}:{line}: {target}: no heading with that anchor")
    assert not problems, "\n".join(problems)


# ─── Writing style (see the style guide: no dashes, no filler words) ─────────

def _style_files() -> list[Path]:
    files = [README, AGENTS, CHANGELOG, LLMS, ROOT / "experiments" / "README.md"]
    for folder in (DOCS, ROOT / "skills"):
        files += sorted(folder.rglob("*.md")) if folder.is_dir() else []
    return [path for path in files if path.is_file()]


# Banned words and phrases: (what to report, regex). Case-insensitive, any inflection.
BANNED: tuple[tuple[str, str], ...] = (
    ("delve", r"\bdelv(?:e|es|ed|ing)\b"),
    ("seamless", r"\bseamless(?:ly)?\b"),
    ("robust", r"\brobust(?:ly|ness)?\b"),
    ("leverage", r"\bleverag(?:e|es|ed|ing)\b"),
    ("utilize", r"\butili[sz](?:e|es|ed|ing|ation)\b"),
    ("comprehensive", r"\bcomprehensive(?:ly)?\b"),
    ("cutting-edge", r"\bcutting[- ]edge\b"),
    ("state-of-the-art", r"\bstate[- ]of[- ]the[- ]art\b"),
    ("empower", r"\bempower(?:s|ed|ing|ment)?\b"),
    ("unlock", r"\bunlock(?:s|ed|ing)?\b"),
    ("unleash", r"\bunleash(?:es|ed|ing)?\b"),
    ("elevate", r"\belevat(?:e|es|ed|ing)\b"),   # "elevation" is a real term
    ("harness", r"\bharness(?:es|ed|ing)?\b"),
    ("streamline", r"\bstreamlin(?:e|es|ed|ing)\b"),
    ("supercharge", r"\bsupercharg(?:e|es|ed|ing)\b"),
    ("game-changer", r"\bgame[- ]chang(?:er|ers|ing)\b"),
    ("revolutionize", r"\brevolution(?:i[sz](?:e|es|ed|ing)|ary)\b"),
    ("effortless", r"\beffortless(?:ly)?\b"),
    ("blazing", r"\bblazing(?:ly)?\b"),
    ("lightning-fast", r"\blightning[- ]fast\b"),
    ("powerful", r"\bpowerful(?:ly)?\b"),
    ("journey", r"\bjourneys?\b"),
    ("landscape", r"\blandscapes?\b"),
    ("tapestry", r"\btapestr(?:y|ies)\b"),
    ("testament", r"\btestaments?\b"),
    ("crucial", r"\bcrucial(?:ly)?\b"),
    ("pivotal", r"\bpivotal\b"),
    ("vibrant", r"\bvibrant(?:ly)?\b"),
    ("intricate", r"\bintrica(?:te|tely|cy|cies)\b"),
    ("meticulous", r"\bmeticulous(?:ly)?\b"),
    ("realm", r"\brealms?\b"),
    ("foster", r"\bfoster(?:s|ed|ing)?\b"),
    ("embark", r"\bembark(?:s|ed|ing)?\b"),
    ("boast", r"\bboast(?:s|ed|ing|ful)?\b"),
    ("showcase", r"\bshowcas(?:e|es|ed|ing)\b"),
    ("plethora", r"\bplethora\b"),
    ("myriad", r"\bmyriad\b"),
    ("paramount", r"\bparamount\b"),
    ("furthermore", r"\bfurthermore\b"),
    ("moreover", r"\bmoreover\b"),
    ("holistic", r"\bholistic(?:ally)?\b"),
    ("synergy", r"\bsynerg(?:y|ies|istic)\b"),
    ("elegant", r"\belegan(?:t|tly|ce)\b"),
    ("stunning", r"\bstunning(?:ly)?\b"),
    ("breeze", r"\bbreezes?\b"),
    ("hassle-free", r"\bhassle[- ]free\b"),
    ("a wide range of", r"\ba wide range of\b"),
    ("in today's", r"\bin today['\N{RIGHT SINGLE QUOTATION MARK}]s\b"),
    ("whether you're", r"\bwhether you(?:['\N{RIGHT SINGLE QUOTATION MARK}]re| are)\b"),
    ("look no further", r"\blook no further\b"),
    ("at its core", r"\bat its core\b"),
    ("dive into", r"\b(?:dive|dives|diving|dove)\s+into\b"),
    ("deep dive", r"\bdeep[- ]dives?\b"),
    ("it's worth noting", r"\bit(?:['\N{RIGHT SINGLE QUOTATION MARK}]s| is) worth not(?:ing|e)\b"),
    ("it is important to note", r"\bit(?:['\N{RIGHT SINGLE QUOTATION MARK}]s| is) important to note\b"),
    ("in conclusion", r"\bin conclusion\b"),
    ("rest assured", r"\brest assured\b"),
    ("best-in-class", r"\bbest[- ]in[- ]class\b"),
    ("world-class", r"\bworld[- ]class\b"),
    ("next-level", r"\bnext[- ]level\b"),   # also "take it to the next level"
    ("first-class", r"\bfirst[- ]class\b"),
    ("out of the box", r"\bout[- ]of[- ]the[- ]box\b"),
    ("under the hood", r"\bunder the hood\b"),
    ("the power of", r"\bthe power of\b"),
    ("simply", r"\bsimply\b"),
    ("magic", r"\bmagic(?:al|ally)?\b"),
    ("easily", r"\beasily\b"),
    ("various", r"\bvarious\b"),
    ("not just X, but Y", r"\bnot just\b[^.]*?\bbut\b"),
)
_BANNED_RE = [(label, re.compile(pattern, re.IGNORECASE)) for label, pattern in BANNED]

_DASH_RULES = (
    ("em dash", re.compile("\N{EM DASH}|&mdash;")),
    ("en dash", re.compile("\N{EN DASH}|&ndash;")),
    ("spaced hyphen used as a dash",
     re.compile(r"(?<=[\w)\].,;:!?'\"*_\N{RIGHT SINGLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK}])"
                r" - "
                r"(?=[\w(\[\"'*_\N{LEFT SINGLE QUOTATION MARK}\N{LEFT DOUBLE QUOTATION MARK}])")),
    ("double hyphen used as a dash", re.compile(r"(?<=\S) -- (?=\S)|(?<=[A-Za-z])--(?=[A-Za-z])")),
)
# Pictographs, except status ticks and warning signs.
_EMOJI = "".join(chr(c) for c in (0x1F000, 0x2D, 0x1FAFF, 0x2600, 0x2D, 0x27BF, 0x2B50, 0x2B55))
_TICKS = "".join(chr(c) for c in (0x2705, 0x274C, 0x2714, 0x2713, 0x2717, 0x2716, 0x26A0))
_SENTENCE_RULES = (
    ("'Additionally' starting a sentence", re.compile(r"(?:^|(?<=[.!?:])\s+)additionally\b",
                                                      re.IGNORECASE)),
    ("exclamation mark", re.compile(r"(?<=[\w)\]\"'*_])!(?=[\s*_)\"']|$)")),
    ("decorative emoji", re.compile(f"[{_EMOJI}](?<![{_TICKS}])")),
)
_BULLET_RE = re.compile(r"^\s*(?:>\s*)*(?:[-*+]|\d+[.)])\s+")


def style_prose(text: str) -> str:
    """
    The text a reader sees as prose, with line numbers kept: fenced blocks become
    blank lines, inline code a placeholder word, link targets and URLs disappear.
    """
    text = FENCE_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    text = re.sub(r"``[^`\n]+``|`[^`\n]+`", "CODE", text)
    text = re.sub(r"\]\([^)\s]*(?:\s+\"[^\"]*\")?\)", "]", text)            # [text](target)
    text = re.sub(r"""\b(?:href|src)\s*=\s*["'][^"']*["']""", "", text)
    return re.sub(r"<?\b(?:https?|mailto):[^\s>)]+>?", "URL", text)


def style_problems(text: str, name: str) -> list[str]:
    problems = []
    for number, line in enumerate(style_prose(text).splitlines(), 1):
        body = _BULLET_RE.sub("", line, count=1)
        hits = [(label, m) for label, rule in _DASH_RULES for m in rule.finditer(body)]
        hits += [(label, m) for label, rule in _SENTENCE_RULES for m in rule.finditer(body)]
        hits += [(f"banned word '{label}'", m) for label, rule in _BANNED_RE
                 for m in rule.finditer(line)]
        for label, m in hits:
            context = m.string[max(0, m.start() - 25):m.end() + 25].strip()
            problems.append(f"{name}:{number}: {label}: ...{context}...")
    return problems


def test_style_checker_catches_what_it_should() -> None:
    bad = ("The engine \N{EM DASH} fast. Use 3.10\N{EN DASH}3.15.\nGeocoding - and maps.\n"
           "A -- B. It's a robust, seamless tool that lets you delve into maps!\n"
           "Simply put, it utilizes the power of maps. \U0001F680\nAdditionally, this.\n")
    labels = {problem.split(": ", 2)[1] for problem in style_problems(bad, "x.md")}
    assert labels >= {"em dash", "en dash", "spaced hyphen used as a dash",
                      "double hyphen used as a dash", "banned word 'robust'",
                      "banned word 'seamless'", "banned word 'delve'", "exclamation mark",
                      "banned word 'simply'", "banned word 'utilize'",
                      "banned word 'the power of'", "decorative emoji",
                      "'Additionally' starting a sentence"}
    good = ("# Load a CSV file\n\n- Load the file: list bullets are fine.\n"
            "Use `--near 28.61,77.21,25` and `qoregeo mcp add -- uvx`.\n"
            "```python\ngeo.load('x.csv')  # robust \N{EM DASH} code is not prose\n```\n"
            "| Option | Default |\n|---|---|\n| `tiles` | - |\n"
            "Elevation, zero-dependency, [maps](maps.md), ![map](a-landscape.svg), \N{WHITE HEAVY CHECK MARK}.\n"
            "<!-- a comment --> x != y\n")
    assert style_problems(good, "x.md") == []


@pytest.mark.parametrize("path", _style_files(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_writing_style(path: Path) -> None:
    # No em or en dashes, no spaced hyphens or double hyphens as dashes, no filler words.
    # Fenced code and inline code are not checked.
    problems = style_problems(read(path), path.relative_to(ROOT).as_posix())
    assert not problems, f"{len(problems)} style problem(s):\n" + "\n".join(problems)
