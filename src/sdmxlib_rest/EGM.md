# sdmxlib_rest

## Overview

`sdmxlib_rest` is a generic SDMX-REST 2.2.2 HTTP surface over an sdmxlib
`BundleRegistry`. It owns what every catalog serves the same way: URL
grammar, content negotiation, `Accept-Language` localisation, streaming and
response assembly. Everything catalog-specific sits behind a seam, a
Protocol carried on `CatalogRuntime` with an inert default, which the
consumer replaces. `create_app` assembles a FastAPI app from a runtime, the
library's routers, the fixed middleware stack and any consumer mounts.

The library serves `/structure`, `/metadata` and `/availability`. It provides
helpers for `/data`, and the consumer writes that route. `__init__.py`
exports nothing (`__all__ = []`); consumers import from the modules. The
modules directly in this directory hold the app factory, the runtime and the
seams.

## Interface

- `create_app`, `RestSettings`, `Mount`, `MiddlewareSpec` and
  `library_routers` (`app.py`): `create_app(runtime, settings, *, routers,
  outer_middleware, mounts, state, perf_sink, on_startup, on_shutdown,
  root_payload)` returns a FastAPI app whose lifespan calls
  `runtime.ensure_open()`, then `on_startup`, and on shutdown calls
  `on_shutdown` or else `runtime.close()`. It includes `routers` in the order
  given, installs `reject_unknown_query_params` as an app-wide dependency,
  installs a handler turning sdmxlib's `ValidationError` into
  `unwritable_content_response` on the app and on every mount, wraps each
  app's `openapi` with `apply_vendor_markers`, serves a multi-spec Swagger UI
  page at `/docs` and a JSON payload at `GET /`. `library_routers()` returns
  `(structure.router, metadata.router, availability.router)`. `RestSettings`
  defaults: `cache_control="public, max-age=300,
  stale-while-revalidate=3600"`, CORS origins `("*",)`, `cors_max_age=3600`,
  `gzip_minimum_size=1024`, `gzip_compress_level=1`,
  `canonical_spec_name="SDMX-REST 2.2.2 (canonical)"` (the app's own entry in
  the `/docs` dropdown). `Mount` carries `path`, `app` and `spec_name`; a
  mount with `spec_name=None` serves its own `/openapi.json` and is left out
  of the `/docs` dropdown.
- `CatalogProvider` and `OpenedCatalog` (`provider.py`): the one required
  seam. `open()` returns an `OpenedCatalog` holding `registry`
  (`sl.BundleRegistry`), `meta_conn` (a DuckDB connection), an opaque
  `manifest`, `build_id`, `built_at`, an optional `token` override and an
  `on_close` callback that `close()` runs.
- `CatalogRuntime` (`runtime.py`): holds the provider, the opened catalog
  and the seams `splices`, `structure_cache`, `metadata_store`,
  `availability_values`, `filter_shaper` and `message_annotator`, each
  defaulting to its null implementation. `ensure_open`, `close`, `is_open`,
  and the forcing properties `registry`, `meta_conn`, `manifest`. The
  non-forcing properties `build_id`, `built_at`, `token`, `effective_token`
  and `last_modified` return `""` before open.
- Request dependencies (`runtime.py`): `attach_runtime` stores the runtime on
  `app.state.rest_runtime`; `share_runtime` points a mounted sub-app at its
  parent's. `get_runtime`, `get_registry`, `get_meta_conn`, `get_splices`,
  `get_build_id`, `get_token`, `get_effective_token`, `get_structure_cache`,
  `get_metadata_store`, `get_availability_values`, `get_filter_shaper` and
  `get_message_annotator` read it from `request.app.state`.
- `SpliceRegistry`, `SpliceContext` and `run_structure_splices`
  (`splice.py`): ordered post-serialise JSON splices, each
  `(parsed_json, ctx) -> bool`, sync or async. `run_structure_splices` skips
  non-JSON responses, parses once with orjson, runs every splice, and
  rebuilds the response only if one returned true, dropping `Content-Length`
  and `ETag` (and `Content-Type` when a splice set `ctx.media_override`).
- `StructureCache`, `StructureQuery` and `NullStructureCache`
  (`structure_cache.py`): `lookup(query)` before a structure render and
  `store(query, response)` after it. Only the JSON path of `get_structure`
  calls `store`: SDMX-ML responses are looked up in `StructureCache` and never
  stored, and `store` runs after `to_204_if_empty`, so an empty JSON result is
  stored as a bodiless 204 (GH-74). The `StructureCache`
  docstring says only 2xx responses with a body reach `store`.
  `StructureQuery.token` is the runtime's `effective_token`.
- `AvailabilityValues`, `AvailabilityKey`, `ResolvedAvailability` and
  `NullAvailabilityValues` (`availability_values.py`): precomputed
  `/availability` value lists. `AvailabilityKey` is `(token, flow_urn,
  component_id)`, with the bare `token` and the resolved flow URN.
  `ResolvedAvailability` is `key_values` plus `obs_count`.
- `MetadataStore` and `NullMetadataStore` (`metadata_store.py`):
  `sets_for_structure` and `sets_for_target` return MetadataSet URNs for the
  `/metadata` routes, read through the `conn` the route passes.
- `ComponentFilterShaper`, `FilterContext` and `NullComponentFilterShaper`
  (`filter_shaper.py`): `shape(component_filter, ctx)` returns the filter to
  generate SQL from. `FilterContext` carries the registry, connection, flow
  URN, `DataflowBinding` and every `(name, value)` query item, repeats kept.
- `MessageAnnotator`, `SupportsFingerprint`, `annotator_fingerprint`,
  `CompositeMessageAnnotator` and `NullMessageAnnotator`
  (`message_annotator.py`): a projection applied to a `StructureMessage`
  before serialisation, so it reaches every wire format. An annotator with
  `fingerprint()` has it folded into `effective_token`.
  `CompositeMessageAnnotator` runs several in order and joins their
  fingerprints by position.
- `OverlayAnnotator` (`overlays.py`): the library's `MessageAnnotator`
  implementation applying sdmxlib annotation overlays (`apply_overlays`) with
  an optional `merge` hook map and `audience`. `from_files` reads overlay
  documents; `overlays` returns the current overlays; `replace` swaps them on
  a running deployment. `fingerprint` is the hooks line and each overlay's
  digest of `applied(overlay, audience)`, so a change of `audience` changes
  it (GH-10). With no overlays it is `""`, and `effective_token` then equals
  `token`.
- `ensure_acceptable` and `is_browser_request` (`negotiation.py`):
  `ensure_acceptable(accept, supported)` raises `HTTPException(406)` when no
  `Accept` token matches a supported media type. Versions compare after
  `_normalize_version` (`2.0.0` equals `2.0`), an unversioned token matches
  any version, `*/*` and `application/*` always pass, and an empty or missing
  `Accept` always passes. A shorthand token `xml`, `json`, `csv` or `parquet`
  passes when the header names no `vnd.sdmx` type and one of the route's
  supported media types contains the same token, so `Accept: text/csv` on a
  route serving only JSON and XML is a 406. `is_browser_request` is true when
  `Accept` contains `text/html` and no `application/vnd.sdmx`.
- `localized_json_response`, `localize_sdmx_json`, `parse_accept_language`
  and `best_match` (`localization.py`): rewrite `name` and `description` on
  every JSON node from its `names` / `descriptions` map to the best
  `Accept-Language` match (exact tag, then base language). `Content-Language`
  is set only when some node matched; with no `Accept-Language`, a bare `*`, a
  body that fails to parse as JSON, or no match, `localize_sdmx_json` returns
  the body unchanged and no language. A browser request gets
  `application/json` as its media type.
- `configure_logging` (`logging.py`): replaces loguru's sinks with one stderr
  sink at `level`. No library module calls it.
- Subpackages:
  - [`routes/`](./routes/EGM.md): the `/structure`, `/metadata` and
    `/availability` routers.
  - [`middleware/`](./middleware/EGM.md): ETag and conditional GET,
    `?lang=` override, media-type version rewrite, selective gzip, HEAD
    handling, the perf-log tap, the unknown-query-parameter guard and the
    OpenAPI vendor markers.
  - [`responses/`](./responses/EGM.md): `asOf`, empty (204), availability
    rendering from value lists, and the 500 for unwritable content.
  - [`data/`](./data/EGM.md): data-plane helpers for a consumer's `/data`
    route: `c[]` and sort parsing, projection, Range paging headers, SQL
    fragments and the CSV, zip and parquet encoders.

## Invariants

- The package imports no consumer and no catalog library:
  `tests/test_library_import_boundary.py` imports every module in a fresh
  interpreter and fails if `statcatlib`, `pinax` or `pinaxlib` loads.
- Every seam default is inert: `NullStructureCache` never hits,
  `NullAvailabilityValues` always scans, `NullMetadataStore` returns `[]`,
  `NullComponentFilterShaper` returns its input, `NullMessageAnnotator`
  returns its input, and an empty `SpliceRegistry` runs no splice. A runtime
  given only a provider serves every route.
- The middleware order is fixed in `_install_middleware` (`app.py`), outermost
  first: `CORSMiddleware`, consumer `outer_middleware`, `PerfLogMiddleware`,
  `HeadMethodMiddleware`, `SelectiveGZipMiddleware`,
  `SdmxVersionAdvertiseMiddleware`, `LangOverrideMiddleware`,
  `ConditionalGetMiddleware`. The docstring gives each adjacency's reason;
  `test_middleware_stack_is_in_the_invariant_order` in
  `tests/test_library_create_app.py` pins the sequence and
  `tests/test_middleware_invariants.py` tests what it protects. Change the
  assertion only together with the docstring.
- One `CatalogRuntime` serves an app and all its mounts: `create_app` calls
  `share_runtime` and copies `state` onto every mount, so the provider opens
  once.
- `CatalogRuntime.token` is the provider's `token` if set, else the sdmxlib
  manifest's `token`, else `build_id`. `effective_token` equals `token` while
  the annotator has no fingerprint, and is otherwise the sha256 of
  `v2\n{token}\n{fingerprint}`, the same lines sdmxlib's `effective_token`
  hashes for overlays. The two tokens match only when an `OverlayAnnotator` is
  the annotator itself; inside a `CompositeMessageAnnotator` each fingerprint
  is prefixed with its position. The structure cache, the splices and the ETag
  key on `effective_token`; `AvailabilityKey` keys on the bare `token`, since
  annotations cannot change availability.
- `last_modified` follows `token`'s source (the manifest's `created_at` or
  the provider's `built_at`). With a fingerprint it is the later of that and
  the moment this process first saw the current fingerprint, moved forward at
  least one second per change.
- `ensure_open` is idempotent but unlocked on the first call; two concurrent
  cold requests may both open the provider, last wins (`runtime.py`
  docstring). `create_app`'s lifespan opens it at startup.
- No library route queries `meta_conn`. It is passed to consumer seams
  (`FilterContext.conn`, `SpliceContext.conn`, `MessageAnnotator.annotate`,
  `MetadataStore`). `/availability` reads observations through
  `registry.obs(flow)` on the registry's own connection (`provider.py`).
- `OverlayAnnotator` refuses an overlay with `texts` with `OverlayError`, at
  construction and in `replace`, and `replace` assigns the overlays before
  the fingerprint so a racing request can pair a new body only with the old
  key.
- `run_structure_splices` re-encodes only when a splice reports a change.

## Side Effects

- The provider's `open` and `OpenedCatalog.close` run at app startup and
  shutdown; what they touch (DuckDB files, a lake, object storage) is the
  consumer's.
- `CatalogRuntime.last_modified` writes `_fingerprint_seen` and
  `_fingerprint_since` on the runtime when the fingerprint changes.
- `create_app` calls `install_isal_zip_backend()`, which routes gzip DEFLATE
  through isa-l process-wide, and replaces `app.openapi` on the app and each
  mount.
- The `/docs` page loads Swagger UI from `cdn.jsdelivr.net` in the client's
  browser.
- `OverlayAnnotator.from_files` reads overlay files. `replace` mutates the
  annotator in place.
- `configure_logging` removes every loguru sink in the process.

## Verification

- Run: `just test` (unit tests) and `just check` (ruff with warnings failing,
  format, docstrings, prose, the `Any` ratchet, the index guard, pyrefly).
- Full gate: `just ci` (`check`, `test`, `docs-build`).
- Package-level tests: `tests/test_library_create_app.py`,
  `tests/test_library_import_boundary.py`,
  `tests/test_middleware_invariants.py`, `tests/test_publication_token.py`,
  `tests/test_annotator_fingerprint.py`, `tests/test_overlays.py`,
  `tests/test_availability_values.py`.
- Performance of `/structure` and `/availability` against a real or
  synthetic bundle: `just bench <bundle-dir>` or `just bench-synthetic
  [large|census]`.
