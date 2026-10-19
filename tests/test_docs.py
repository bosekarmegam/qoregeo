"""
tests/test_docs.py
==================
The documentation must match the code: README.md (which is also the PyPI
project page), the Agent Skill and its references, AGENTS.md, llms.txt and
the CHANGELOG. Examples are executed where that is cheap and offline;
everything else is checked against the real signatures and presets.
"""

from __future__ import annotations

import ast
import builtins
import inspect
import re
import zlib
from pathlib import Path
from typing import Any

import pytest

import qoregeo
from qoregeo import GeoEngine
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
FENCE_RE = re.compile(r"^```(\w*)[^\n]*\n(.*?)^```", re.DOTALL | re.MULTILINE)


def read(path: Path) -> str:
    """The file's text, or skip — an sdist ships only some of these files."""
    if not path.is_file():
        pytest.skip(f"{path.relative_to(ROOT)} is not part of this checkout")
    return path.read_text(encoding="utf-8")


def code_blocks(text: str, lang: str) -> list[tuple[int, str]]:
    """(first line number, source) of every fenced block in language ``lang``."""
    return [
        (text.count("\n", 0, m.start()) + 2, m.group(2))
        for m in FENCE_RE.finditer(text) if m.group(1) == lang
    ]


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
            continue   # `map_html(...)` — "same options as"
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
    assert f"Python {matrix[0]}–{matrix[-1]}" in agents
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
