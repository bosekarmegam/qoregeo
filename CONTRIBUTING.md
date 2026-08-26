# Contributing to QOREgeo

Thanks for considering it. This guide covers what the project needs, how to set
up, and the standards a change has to meet.

---

## The one rule that shapes everything

**QOREgeo has zero runtime dependencies, and that is not negotiable.**

It is the whole reason the project exists: `pip install qoregeo` works on a
Raspberry Pi, in a Lambda layer, on Alpine, and on a corporate laptop without
admin rights, because there is nothing to compile. A pull request that adds a
runtime dependency will not be merged, however useful the feature.

In practice that means:

- Standard library only, in `qoregeo/`.
- No C extensions, no Cython, no build step.
- Optional accelerators are fine **if** the pure-Python path stays complete and
  the import is guarded. Nothing may ever be required.
- Test-only and lint-only dependencies are fine. They live in
  `[project.optional-dependencies]`.

If a feature genuinely cannot be done in pure Python, say so in an issue. The
right answer may be a companion package rather than a compromise here.

---

## What is most useful

In rough order:

1. **A concrete case QOREgeo handles badly.** Open an issue describing what you
   were trying to do, what you had to install instead, and roughly what the
   data looked like. This is worth more than a feature list.
2. **Bug reports with a reproducer.** Ten lines that fail beat a paragraph
   that describes failing.
3. **Roadmap items**, see [ROADMAP.md](ROADMAP.md). Comment on the issue
   before starting something large, so nobody duplicates work.
4. **Documentation and examples**, particularly worked examples on real data.

---

## Setup

```bash
git clone https://github.com/bosekarmegam/qoregeo
cd qoregeo
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Then check everything passes before you change anything:

```bash
pytest                      # the full suite
ruff check qoregeo tests    # lint
mypy qoregeo                # types
```

---

## Making a change

```bash
git checkout -b feature/short-description
```

Work in small commits with messages that say what changed and why.

### Before opening a pull request

All three must pass:

```bash
pytest                            # every test, including yours
ruff check qoregeo tests
mypy qoregeo
pytest --cov=qoregeo --cov-report=term-missing    # coverage should not drop
```

---

## Standards

### Tests

Every behaviour change needs a test. New modules need a test module.

The important part is **what** you test against. Numerical code is verified
against something independent, never against its own output:

- Spatial index results are compared feature-for-feature with a brute-force
  linear scan, across randomised datasets including polar and antimeridian
  cases.
- Route optimisation is compared against a brute-force permutation search on
  problems small enough to enumerate.
- Areas and buffers are compared against closed-form analytic values.
- The PNG encoder is verified by decoding its own output and checking every
  chunk CRC.
- Geohashes and projections are checked against published reference values.

If you cannot think of an independent check, that is a signal the test is
asserting the implementation rather than the behaviour.

Also test the edges. The bugs found while building v1.1 were all at edges:
searches crossing a pole, corridors on long great circles, polygon boundaries,
degenerate bounding boxes, and tied values in quantile classes.

### Code style

- `ruff` enforces formatting and imports; run it rather than guessing.
- Line length 100.
- Type annotations on all public functions. The package ships `py.typed`.
- Python 3.8 compatibility: `from __future__ import annotations` at the top of
  every module, and `List`/`Optional`/`Union` from `typing` rather than builtin
  generics. Type *aliases* are evaluated eagerly, so `tuple[float, float]`
  breaks at runtime on 3.8. This lifts in v2.0.

### Comments and docstrings

Comment the **why**, not the what. `# increment i` is noise; `# Sweeping the
other way would cut back across the segment, leaving the endpoint outside its
own buffer` is what stops the next person reintroducing the bug.

Docstrings are NumPy style: a one-line summary, a paragraph on why the function
exists or when to reach for it, then `Parameters` / `Returns` / `Examples`.

### Errors

QOREgeo exceptions teach. Every one names the problem and shows the fix:

```
❌  QOREgeo. Column Not Found
────────────────────────────────────────────────
File: 'stores.csv'
Could not find a 'lat' column.

Columns in your file:
    'store_id', 'latitude', 'longitude'

Fix it:
    geo.load('stores.csv', lat_col='latitude', lng_col='longitude')
```

New failure modes get new exception classes in `qoregeo/exceptions.py`,
following that shape. Never raise a bare `ValueError` from public API.

### Spelling

The codebase uses British spelling (`optimise`, `colour`, `centre`, `neighbour`)
with US aliases exported alongside (`optimize_route`, `color_by`,
`center_of_mass`, `neighbors`). Keep both working when you add API in that
area.

---

## Project layout

```
qoregeo/
  engine.py       GeoEngine, the public entry point
  geometry.py     spherical and planar primitives
  index.py        uniform-grid spatial index
  analysis.py     clustering, joins, point-pattern statistics
  routing.py      stop ordering (nearest neighbour + 2-opt)
  query.py        the query expression parser
  formats.py      WKT, GPX, KML, NDJSON, shapefile
  geohash.py      geohashes, slippy tiles, quadkeys
  crs.py          Web Mercator and UTM
  map_builder.py  interactive HTML maps
  static_map.py   SVG and PNG rendering
  geocode.py      opt-in address lookup
  cli.py          the command line
  exceptions.py   every error type
  utils.py        shared low-level helpers

tests/
  conftest.py           shared fixtures
  test_qoregeo.py       v1.0 regression suite. Do not weaken
  test_<module>.py      one per module
```

`tests/test_qoregeo.py` pins the original v1.0 behaviour. If a change makes one
of those fail, that is a compatibility break: either fix the change, or make
the case for the break in your pull request.

---

## Pull requests

Describe what changed and why. Include:

- The problem, ideally with the failing snippet.
- What you changed, and any approach you rejected.
- Test results, and anything you could not cover.

Small, focused pull requests get reviewed faster than large ones.

---

## Reporting bugs

Open an issue with:

- What you ran (a minimal snippet).
- What happened, including the full traceback.
- What you expected.
- Python version, operating system, and QOREgeo version
  (`python -c "import qoregeo; print(qoregeo.__version__)"`).

Security issues: email <suneelbosekarmegam@gmail.com> rather than opening a
public issue.

---

## License

Contributions are licensed under the MIT License, the same as the project.
