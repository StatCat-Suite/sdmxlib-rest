# sdmxlib-rest Blueprint

## System Overview

sdmxlib-rest serves an sdmxlib `BundleRegistry` over SDMX-REST 2.2.2 as a
FastAPI application. It owns the parts of an SDMX-REST service that are the
same for every catalog: URL grammar, content negotiation, `Accept-Language`
localisation, conditional GET and ETags, compression, streaming and response
assembly. Everything that knows a particular bundle, lake layout, agency,
codelist or vendor parameter belongs to the consumer, behind a seam: a
Protocol carried on `CatalogRuntime` with an inert default. The library
serves `/structure`, `/metadata` and `/availability`; a consumer writes its
own `/data` route from the helpers in `sdmxlib_rest.data`.

The repository holds one distribution, `sdmxlib-rest`, built from
`pyproject.toml` and pinned to `sdmxlib>=0.74,<0.75`. `README.md` is the user
guide and PyPI description; `CLAUDE.md` holds the project's working rules,
the seams table and the writing style; `docs/` and `mkdocs.yml` build the
docs site, whose guide pages restate the behaviour of the seams, the routes
and `create_app`. The design pages under `docs/design/` record what was
decided and why: scope and seams, the URL grammar, availability, tokens and
cache keys, and unwritable content. `CHANGELOG.md` is the release record.

## Subdirectories & System Boundaries

- src: [`./src/EGM.md`](./src/EGM.md) - the `sdmxlib_rest` package. Its
  index is [`./src/sdmxlib_rest/EGM.md`](./src/sdmxlib_rest/EGM.md): the app
  factory, `CatalogRuntime` and the seams, and the subpackages
  [`routes/`](./src/sdmxlib_rest/routes/EGM.md),
  [`middleware/`](./src/sdmxlib_rest/middleware/EGM.md),
  [`responses/`](./src/sdmxlib_rest/responses/EGM.md) and
  [`data/`](./src/sdmxlib_rest/data/EGM.md).
- tools: [`./tools/EGM.md`](./tools/EGM.md) - repository gates run from the
  justfile (docstring style, prose style, the `Any` ratchet, the
  package-index allow-list) and the synthetic-bundle builder for the bench
  tier. Nothing here is imported by the package.

`tests/` (including the bench tier in `tests/bench/`) and `docs/` carry no
contract: EGM's own rules exclude test directories, and `.egmignore` lists
`docs/`, which is prose, the design pages included. `site/`, `htmlcov/`, `dist/` and `.bench/` are build output and gitignored.

## Global Invariants

- The library holds no catalog-specific logic and imports no consumer or
  catalog library. `tests/test_library_import_boundary.py` imports every
  module in a fresh interpreter and fails if `statcatlib`, `pinax` or
  `pinaxlib` loads.
- Every seam except `CatalogProvider` has an inert default
  (`NullStructureCache`, `NullAvailabilityValues`, `NullMetadataStore`,
  `NullComponentFilterShaper`, `NullMessageAnnotator`, an empty
  `SpliceRegistry`), so a runtime given only a provider serves every route
  correctly.
- The library owns data-plane helpers and no `/data` route. `/availability`
  is the one library route that reads observations, through
  `registry.obs(flow)` on the registry's own connection, building one query
  root per request and never caching it across requests. No library route
  queries `meta_conn`; it goes to consumer seams.
- Routes serve the SDMX-REST 2.2.2 grammar only. A key is parsed against
  the slot order from `data/projection.py:key_dimension_ids`, which is
  `dsd.key_slot_ids("3.0")` when the registry returns a materialised
  `sl.DataStructure`, and otherwise the order of
  `binding.dimension_column_map`, which the producer decides. A key of the
  wrong length is a 400. A 1.5 surface is a consumer's forwarder; the data
  plane takes no API version.
- The middleware order in `app.py:_install_middleware` is fixed, outermost
  first: CORS, consumer `outer_middleware`, perf log, HEAD, selective gzip,
  media-type version rewrite, `?lang=` override, conditional GET.
  `tests/test_library_create_app.py` pins it; its docstring gives each
  adjacency's reason.
- Caches, splices and the ETag key on `CatalogRuntime.effective_token` (the
  publication token with the annotator fingerprint folded in);
  `AvailabilityKey` keys on the bare `token`.
- Each suite dependency is capped below its next minor while it is pre-1.0
  (`pyproject.toml`). Breaking a seam Protocol is a minor bump before 1.0.
- pyrefly is the only type checker, ruff warnings fail the gate, and the
  count of `Any` in `src/` may only move with `tools/any_baseline.txt`.

## Global Side Effects

- Reads of the consumer's catalog through the `CatalogProvider` it supplies:
  DuckDB metadata stores and DuckLake observation files, opened once at app
  startup and closed at shutdown.
- HTTP responses with ETag, `Last-Modified`, `Cache-Control`, `Vary`,
  `Content-Language` and CORS headers; gzip through isa-l, installed
  process-wide by `install_isal_zip_backend`.
- One perf record per request to the consumer's `perf_sink`, and loguru log
  lines.
- `just bench-synthetic` writes synthetic bundles under `.bench/`;
  `just bench-profile` writes shapes under `tests/bench/shapes/local/`.

## System Verification

- Inner loop: `just check` (ruff with warnings failing, format, docstrings,
  prose, the `Any` ratchet, the index guard, pyrefly) and `just test` (unit
  tests).
- The gate: `just ci` (`check`, `test`, `docs-build`). The hosted workflows
  are `ci.yml` (`just check` and `just docs-build`, `just test`, and the unit
  tests at every direct dependency's floor), `docs.yml` (deploys the site)
  and `bench.yml` (on demand and on pull requests that touch the bench).
- Performance: `just bench <bundle-dir>` against a real publication, or
  `just bench-synthetic [large|census]`.
