# tools

## Overview

Standalone Python scripts that guard the repository and build the synthetic
bundles the bench tier runs on. None is imported by the `sdmxlib_rest`
package. Four are gates run through `justfile` recipes with `--check`, and
`just check` runs all four. Three were ported from sdmxlib as part of the
StatCat-Suite baseline (`check_docstrings.py`, `check_prose.py`,
`count_any.py`). Each script's module docstring records why it exists and the
measurement behind its design; read it before changing the script.

## Interface

| Script | Entry | What it does |
| ------ | ----- | ------------ |
| `check_docstrings.py` | `just check-docstrings` (`--check`) | Fails on four arms under `src` (`TARGET`): Sphinx/RST roles and `Example::` literal blocks (`rst_markup`), NumPy section headers, a section name followed by a rule (`numpy_sections`), `Parameters:` / `Arguments:` / `Params:` headings (`argument_section_names`), and every pydoclint finding under `pyproject.toml`'s `[tool.pydoclint]` (`pydoclint_findings`). `ARMS` lists them; `main` runs them. |
| `check_prose.py` | `just check-prose` (`--check`) | Fails on a Markdown heading ending in `?`, a phrase in `PHRASES`, and a bare `#N` or `GH-N` in `docs/` (`USER_DOCS`). Headings and issue references are read from the Markdown in `MARKDOWN` (`docs`, `README.md`, `CLAUDE.md`) outside code fences; phrases also from every Python and toml file and the justfile under `CODE` (`src`, `tests`, `tools`, `justfile`, `pyproject.toml`). Functions: `find`, `main`. |
| `count_any.py` | `just check-any` (`--check`, `--write`) | Counts `Any` used as a type in `src/`, following module-level aliases discovered across `src/`, and compares the total to `any_baseline.txt`. Functions: `alias_names`, `count_file`, `survey`, `main`. |
| `any_baseline.txt` | read by `count_any.py` | The recorded `Any` count (currently 30). |
| `check_indexes.py` | `just check-indexes` (`--check`) | Fails when a tracked file names a package index outside `PUBLIC_INDEX_HOSTS`, a credential-bearing file in `CREDENTIAL_BEARING_FILES` (`uv.lock`, `.netrc`, `.pypirc`, `pip.conf`, `pip.ini`) is tracked, or `uv.lock` is not gitignored. Symbols: `Finding`, `PUBLIC_INDEX_HOSTS`, `is_public`, `hosts_in`, `directive_value`, `scan_text`, `tracked_files`, `structural_findings`, `survey`, `main`. |
| `synthetic_bench.py` | `just bench-synthetic [shape] [seed]` (`build`); `just bench-profile BUNDLE FLOW NAME` (`profile`) | `build` turns a shape under `tests/bench/shapes/` (`SHAPES`), `shapes/local/` (`LOCAL_SHAPES`) or a `.json` path into a published bundle with `sdmxlib.testing.synthetic_bundle` and prints the publication directory `just bench` opens. `profile` reads a real bundle's shape with `profile_bundle` into `tests/bench/shapes/local/NAME.json`. Symbols: `shape_to_json`, `shape_from_json`, `ShapeFileError`, `resolve_shape`, `build`, `profile`, `main`. |

## Invariants

- Each gate exits 1 under `--check` when it has a finding and only reports
  otherwise. Each has a test in `tests/` that feeds it a violation:
  `test_check_docstrings.py`, `test_check_prose.py`, `test_count_any.py`,
  `test_check_indexes.py`. `test_synthetic_bench.py` covers
  `synthetic_bench.py`.
- `count_any.py --check` fails when the count moves off the baseline in
  either direction. A drop is recorded with `--write`, so the lowered
  baseline shows in the diff; raising it needs a reason in the commit
  message (`any_baseline.txt` header).
- `check_indexes.py` is an allow-list of public index hosts. It never names a
  private host, because naming it in the gate would leak it. The script and
  its test are exempt from its own scan (`SELF_EXEMPT`).
- `check_prose.py` exempts the lines of CLAUDE.md's "Writing style" section
  (`RULES_HEADING`) and itself, since both quote the banned phrases.
  CHANGELOG.md is not read. A Markdown link is removed from a line before the
  issue-reference check reads it.
- `check_docstrings.py` reads pydoclint's findings from stderr and stdout,
  and treats pydoclint exit codes 0 and 1 as a run; any other exit code, or
  `uv` missing from `PATH`, exits 2 (`pydoclint_findings`).
- `synthetic_bench.py build` reuses an output directory whose
  `synthetic-bench.json` stamp matches the shape and seed, carries a `token`,
  and names a publication directory that still exists. It deletes and
  rebuilds a directory whose stamp fails any of those tests, and raises `FileExistsError` for a non-empty directory
  with no stamp.
- A profiled shape goes only to `tests/bench/shapes/local/`, which is
  gitignored: a real bundle's codelist sizes and observation count identify
  it.

## Side Effects

- `count_any.py --write` rewrites `tools/any_baseline.txt`.
- `check_docstrings.py` runs `uv run pydoclint` as a subprocess;
  `check_indexes.py` runs `git ls-files` and `git check-ignore`.
- `synthetic_bench.py build` writes a bundle (DuckDB metadata store, DuckLake
  data and `bundle.manifest.json`) and its `synthetic-bench.json` stamp into
  the output directory, under `.bench/` when run from `just bench-synthetic`,
  and may delete that directory first. `profile` reads a bundle and writes
  one JSON file.

## Verification

- Run: `just check-docstrings`, `just check-prose`, `just check-any`,
  `just check-indexes` (all four run inside `just check`)
- Run: `uv run pytest tests/test_check_docstrings.py tests/test_check_prose.py tests/test_count_any.py tests/test_check_indexes.py tests/test_synthetic_bench.py`
- Full gate: `just ci`
