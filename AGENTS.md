# AGENTS.md — working on qoregeo

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
Linux/macOS/Windows × Python 3.10–3.15, and ruff, mypy and the build + `twine check --strict`
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
  arguments: `{"lat": …, "lng": …}` objects. Bounding boxes: `[west, south, east, north]`
  (west > east crosses the antimeridian — keep that working).
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
| `qoregeo/engine.py` | `GeoEngine` — the public API |
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

## Releasing

Bump the version in `qoregeo/_version.py`, `.claude-plugin/plugin.json`,
`gemini-extension.json` and `server.json` (both `version` fields), add a CHANGELOG entry, then
publish a GitHub Release tagged `vX.Y.Z` — or, once that's merged, run the *Publish to PyPI*
workflow on `main` by hand with the version, which creates the tag and Release itself. Either
way the workflow tests, builds, uploads to PyPI (trusted publishing) and lists the new version
in the MCP Registry.
