# AGENTS.md: working on qoregeo

Instructions for AI coding agents (Claude Code, Codex, Copilot, Cursor, Gemini, …) and humans
contributing to this repository. To *use* qoregeo from an agent, see
[skills/qoregeo/SKILL.md](skills/qoregeo/SKILL.md) and the MCP section of the README.

## Setup and checks

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e . --group dev                      # pip >= 25.1 (PEP 735 groups)
pytest                                            # 3,000+ tests, well under a minute, offline
ruff check qoregeo tests
mypy                                              # strict, configured in pyproject.toml
python -m build && twine check dist/*            # packaging
```

All four must pass before a commit. CI (`.github/workflows/tests.yml`) runs the tests on
Linux/macOS/Windows × Python 3.10 to 3.15, and ruff, mypy and the build + `twine check --strict`
once, on Ubuntu with Python 3.14.

## Hard rules

- **Zero runtime dependencies.** Standard library only in `qoregeo/`. Test-only and dev tools go
  in `[dependency-groups]`.
- **Python 3.10+.** `from __future__ import annotations`, PEP 604/585 types, strict mypy.
- **Tests never touch the network.** Fake HTTP by monkeypatching `Geocoder._fetch`; servers in
  tests bind 127.0.0.1 only. Use `tmp_path`. No cross-imports between test modules: each file
  must run on its own (`pytest tests/test_tools.py`), including against the installed wheel,
  which only the release workflow tests (with `--import-mode=importlib`).
- **Coordinate conventions.** Python API: `(lat, lng)`. GeoJSON: `[lng, lat]`. Tool/MCP
  arguments: `{"lat": …, "lng": …}` objects. Bounding boxes: `[west, south, east, north]`,
  where west > east crosses the antimeridian. Keep that working.
- **Errors teach.** Raise the classes in `qoregeo/exceptions.py` (they subclass built-ins and
  pickle) with the value, what was expected and a runnable example.
- **Never weaken the HTML safety model.** Map data is embedded with `JSON.parse("…")` and
  `<`, `>`, `&`, U+2028/9 escaped; popups use `textContent`; Leaflet loads with SRI hashes.
- **Respect the tile and geocoding policies.** OpenStreetMap tiles need a Referer (pages served
  over http, `referrerPolicy` set) and attribution; Nominatim allows 1 request/second with an
  identifying User-Agent. Don't add keyed providers as defaults.
- **Writes are atomic** (`utils._atomic_write`) and never target the loaded source file.

## Layout

| Path | What |
|---|---|
| `qoregeo/engine.py` | `GeoEngine`, the public API |
| `qoregeo/utils.py` | geometry, parsing and I/O helpers |
| `qoregeo/map_builder.py` | Leaflet HTML pages, basemap presets |
| `qoregeo/render.py` | static PNG/SVG renderer (pure Python) |
| `qoregeo/geocode.py` | Nominatim / Photon client |
| `qoregeo/serve.py` | localhost server used by `show()` / `qoregeo serve` |
| `qoregeo/tools.py` | AI tool registry (MCP, Claude, OpenAI schemas + handlers) |
| `qoregeo/mcp_server.py` | zero-dependency MCP stdio server (2024-11-05 … 2026-07-28) |
| `qoregeo/cli.py` | the `qoregeo` command |
| `skills/qoregeo/` | Agent Skill (also packaged into the wheel) |
| `.claude-plugin/`, `.mcp.json` | Claude Code / Codex plugin + marketplace |
| `server.json`, `gemini-extension.json` | MCP Registry and Gemini CLI manifests |

## Adding an AI tool

1. Write a handler `(ctx: ToolContext, args: dict) -> ToolResult` in `qoregeo/tools.py`; raise
   `ToolError` for problems the model can fix.
2. Register a `Tool(...)` with a JSON Schema: `type: object`, `additionalProperties: false`, no
   root-level `anyOf`/`oneOf`, property names `[A-Za-z0-9_-]`, points as `{lat, lng}` objects.
   Set `read_only=False` for writers and `open_world=True` for network access.
3. Add tests to `tests/test_tools.py` (and `tests/test_mcp_server.py` if visibility changes).
4. Document it in the README tool table and `skills/qoregeo/SKILL.md`.

## Writing docs

These rules cover every doc, docstring, comment, help text, error message and tool or JSON
description. `tests/test_docs.py` checks the Markdown files (README, AGENTS.md, CHANGELOG,
llms.txt, `docs/`, `skills/`) against the dash and word rules, so run
`pytest tests/test_docs.py` after editing them.

- **No dashes as punctuation.** No em dash (U+2014) or en dash (U+2013), and no spaced
  hyphen or double hyphen between clauses. Use a period (often best: split the sentence),
  a comma, a colon or parentheses. CLI flags such as `--near` and the `--` separator in
  shell commands are code and stay, as do hyphens inside words (`zero-dependency`).
- **Ranges.** Write "Python 3.10 to 3.15" in prose. A plain hyphen (`0-360`) is fine in
  tables and code.
- **Plain words.** No marketing or filler words (`seamless`, `robust`, `powerful`,
  `leverage`, `utilize`, `comprehensive`, `simply`, `various`, …), no `just` or `easily`
  to play down effort, no exclamation marks, rhetorical questions or decorative emoji
  (status ticks in tables are fine). The full list is `BANNED` in `tests/test_docs.py`.
- **Voice.** Say what the thing does, then show it. Short sentences, one idea each. Use
  "you" in guides and the imperative for steps ("Load the file"). Give numbers instead of
  adjectives; claims must be obvious or measured (the scripts in `experiments/`). Headings
  are in sentence case. Keep the spelling a file already uses ("neighbours").
- **Examples run.** Every code example must run as written against the current code, or
  start with `# needs network`. `tests/test_docs.py` runs the `python` blocks and the
  `qoregeo` command lines on the `docs/*.md` pages offline (its conventions are in a comment
  above those tests), runs the README and Agent Skill quick starts, and checks the API
  reference against the real signatures.

## Releasing

Bump the version in `qoregeo/_version.py`, `.claude-plugin/plugin.json`,
`gemini-extension.json` and `server.json` (both `version` fields), add a CHANGELOG entry, then
publish a GitHub Release tagged `vX.Y.Z`. Or, once the version bump is merged, run the
*Publish to PyPI* workflow on `main` by hand with the version; it creates the tag and
Release itself. Either way the workflow tests, builds, uploads to PyPI (trusted publishing)
and lists the new version in the MCP Registry.
