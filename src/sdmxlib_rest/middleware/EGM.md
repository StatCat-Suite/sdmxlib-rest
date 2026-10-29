# sdmxlib_rest.middleware

## Overview

`sdmxlib_rest.middleware` holds the HTTP layers of the SDMX-REST surface:
conditional GET, gzip compression, HEAD support, the
`?lang=` override, the media-type version rewrite, a per-request performance
log, the unknown-query-parameter guard and the OpenAPI vendor markers. Seven
classes here are raw ASGI middleware: `ConditionalGetMiddleware`,
`SelectiveGZipMiddleware`, `IsalGZipMiddleware`, `HeadMethodMiddleware`,
`LangOverrideMiddleware`, `SdmxVersionAdvertiseMiddleware` and
`PerfLogMiddleware`. `_install_middleware` in `app.py` installs six of them in
a fixed order; `IsalGZipMiddleware` runs inside `SelectiveGZipMiddleware`.
`reject_unknown_query_params` is a FastAPI dependency and
`apply_vendor_markers` an OpenAPI post-processor. `create_app` in `app.py`
wires the dependency, the OpenAPI hook and the isa-l zip backend.

## Interface

- `ConditionalGetMiddleware` (`etag.py`): takes `runtime: CatalogRuntime`,
  `cache_control: str = ""` and `version: str = ""`. On GET and HEAD it
  computes the ETag as the first 16 hex characters of SHA-256 over
  `version|effective_token|path|query_string|Accept|resolved_lang`, quoted.
  `resolved_lang` is the first tag from `parse_accept_language`, lowercased,
  or `en` when the header is absent. `Last-Modified` comes from
  `runtime.last_modified` (ISO 8601, naive values read as UTC, via
  `_parse_built_at`). It answers 304 when `If-None-Match` matches (`*`, or a
  comma-separated list with `W/` stripped) or `If-Modified-Since` is at or
  after `Last-Modified` at whole-second precision.
- `SelectiveGZipMiddleware`, `IsalGZipMiddleware` and `SKIP_GZIP_CONTENT_TYPES`
  (`compression.py`): gzip through isa-l with `minimum_size=1024` and
  `compresslevel=1` defaults. `_IsalGZipResponder` overrides only the
  `compressor` property of Starlette's `GZipResponder` and clamps the level to
  0-3. `SelectiveGZipMiddleware` wraps the app in `_TagSkippableApp`, which sets
  `Content-Encoding: identity` on responses whose base `Content-Type` (the
  part before `;`) is in
  `SKIP_GZIP_CONTENT_TYPES` (`application/vnd.apache.parquet`,
  `application/x-parquet`, `application/octet-stream`, `application/zstd`,
  `application/zip`), so Starlette's responder skips them. A request without
  `gzip` in `Accept-Encoding` goes through `IdentityResponder`.
- `install_isal_zip_backend` (`compression.py`): replaces
  `zipfile._get_compressor` and `zipstream.ng._get_compressor` with an isa-l
  raw-DEFLATE compressor (wbits -15, level clamped to 0-3, default 1). Other
  compression types fall through to the original. `create_app` calls it.
- `HeadMethodMiddleware` (`head_method.py`): rewrites a HEAD request's
  `scope["method"]` to `GET` on a copied scope, forwards
  `http.response.start` unchanged, and replaces the body with one empty
  `http.response.body` frame (`more_body=False`) sent when the downstream
  final chunk arrives.
- `LangOverrideMiddleware` (`lang_override.py`): when the query string holds a
  non-empty `lang`, replaces the `accept-language` request header with the
  last `lang` value (stripped), drops duplicate `accept-language` headers, or
  appends one if none existed.
- `SdmxVersionAdvertiseMiddleware` and `canonicalise_version_token`
  (`version_token.py`): rewrite a response `Content-Type` segment
  `;version=X.Y.0` to `;version=X.Y` with `_VERSION_PATCH_RE`. Two-part
  versions and other parameters pass through.
- `PerfLogMiddleware` (`perf_log.py`): takes `sink: _Sink | None`. Per HTTP
  request it calls `sink("perf", ...)` with `method`, `path`, `query_len`,
  `status`, `wall_ms` (from `time.perf_counter`, rounded to 2 places), `bytes`
  (sum of body chunk lengths), `content_type` (truncated to 64 characters),
  `content_encoding` (truncated to 16, default `identity`), `has_range` and
  `has_if_none_match`.
- `reject_unknown_query_params` (`query_param_guard.py`): a FastAPI dependency
  that raises `HTTPException(400)` naming every query parameter outside the
  route's declared params (`route.dependant.query_params`, by alias and by
  name), `_GLOBAL_VENDOR_PARAMS` (`_chunk`), `_GLOBAL_NOOP_PARAMS`
  (`dimensionAtObservation`, `format`, `lang`) and the bracketed forms `c[...]`
  and `within[...]`. `create_app` installs it in the app's `dependencies`.
- `apply_vendor_markers` (`openapi_extensions.py`): sets `x-vendor: true` on
  every parameter named in `_VENDOR_PARAM_NAMES` and on every operation under a
  path in `_VENDOR_PATH_TEMPLATES`, mutating the spec in place and returning
  it. `_install_vendor_marker_hook` in `app.py` wraps `app.openapi` with it.

## Invariants

- `_install_middleware` (`app.py`) builds the stack in this order, outermost
  first: `CORSMiddleware`, each consumer `MiddlewareSpec` from
  `outer_middleware`, `PerfLogMiddleware`, `HeadMethodMiddleware`,
  `SelectiveGZipMiddleware`, `SdmxVersionAdvertiseMiddleware`,
  `LangOverrideMiddleware`, `ConditionalGetMiddleware`.
  `test_middleware_stack_is_in_the_invariant_order` and
  `test_outer_middleware_lands_just_inside_cors`
  (`tests/test_library_create_app.py`) pin it. The `_install_middleware`
  docstring gives each adjacency's reason, and a change to the order updates
  that docstring and the test together.
- `LangOverrideMiddleware` sits outside `ConditionalGetMiddleware`, so
  `?lang=fr` produces the same ETag as `Accept-Language: fr`.
  `test_a_lang_override_hashes_the_same_as_the_equivalent_header`
  (`tests/test_middleware_invariants.py`) fails if they are swapped.
- The ETag never reads the response body. `ConditionalGetMiddleware` computes it
  before calling the app, so it is the same with or without compression and at
  any compression level.
- `SelectiveGZipMiddleware` sits outside `SdmxVersionAdvertiseMiddleware`.
  Both orders compress the same responses, because the skip-list and
  Starlette's exclusion list compare the base type before `;` and the rewrite
  changes only the `;version=` parameter. The rewrite reads only the
  `http.response.start` message and never the body.
  `tests/test_library_create_app.py` pins the order, and
  `test_the_rewritten_type_is_still_compressed`
  (`tests/test_middleware_invariants.py`) passes in either order.
- `HeadMethodMiddleware` sits inside `PerfLogMiddleware` and the consumer
  taps, so they record the client's HEAD method, and outside the ETag, gzip and
  version layers, so those see GET and a HEAD gets the same 304 as a GET.
- `CORSMiddleware` is outermost, so it answers an OPTIONS preflight for a path
  the app does not route instead of letting it reach a 404.
- `ConditionalGetMiddleware` stamps `ETag`, `Vary: Accept, Accept-Language`,
  `Last-Modified` and a non-empty `Cache-Control` only on 2xx responses and on
  its own 304s. Error responses carry none of them.
- `ConditionalGetMiddleware` passes requests through untouched while
  `runtime.is_open` is false, and for methods other than GET and HEAD.
- Every ASGI class here passes non-`http` scopes (lifespan, websocket) straight
  to the wrapped app.
- `PerfLogMiddleware` emits nothing when `sink` is `None`, for OPTIONS
  requests, and for `/health`. It emits the record in a `finally`, so a
  request whose app raises is still logged.
- `install_isal_zip_backend` and `apply_vendor_markers` are idempotent:
  `zipfile._sdmxlib_rest_isal_patched` guards the first, and the second
  reassigns `True` to the same keys. `_install_vendor_marker_hook` also skips a
  spec already flagged `x-vendor-markers-applied`.
- `reject_unknown_query_params` checks nothing for a route without a
  `dependant` attribute or for a path containing `/openapi.json`, `/docs`,
  `/redoc` or `/explorer`.
- `query_param_guard.py` and `openapi_extensions.py` name one consumer's
  routes and parameters. `reject_unknown_query_params` admits `within[...]`
  (a filter shaper's parameter) and `_chunk` on every route and exempts
  `/explorer`, and
  `apply_vendor_markers` hardcodes that consumer's `/data` parameters
  (`format`, `pivot`, `debug`, `_chunk`) in `_VENDOR_PARAM_NAMES` and its
  `/contentconstraint`, `/hierarchicalcodelist`, `/hierarchyassociation`,
  `/health` and `/explorer` paths in `_VENDOR_PATH_TEMPLATES`
  (GH-73).

## Side Effects

- `install_isal_zip_backend` patches module attributes process-wide:
  `zipfile._get_compressor`, `zipstream.ng._get_compressor` and the flag
  `zipfile._sdmxlib_rest_isal_patched`.
- `ConditionalGetMiddleware` appends `etag`, `vary`, `last-modified` and
  `cache-control` to 2xx `http.response.start` headers, and sends a complete
  304 response with an empty body without calling the app.
- `SelectiveGZipMiddleware` sets `content-encoding: identity` on skip-listed
  responses. For other responses, Starlette's `GZipResponder` holds the
  `http.response.start` message until the first body chunk arrives. It sends a
  body under `minimum_size` uncompressed, and passes 206 responses through.
  It also passes through the types in Starlette's default
  `exclude_content_types`, which `_IsalGZipResponder` inherits:
  `application/gzip`, `application/x-gzip`, `application/zip`,
  `application/grpc`, `audio/*`, `video/*`, the common image types, `font/woff`,
  `font/woff2` and `text/event-stream`.
  Otherwise it compresses, sets `Content-Encoding: gzip`, adds
  `Accept-Encoding` to `Vary` and updates `Content-Length` (removed for a
  streamed body).
- `HeadMethodMiddleware` discards every response body chunk.
- `SdmxVersionAdvertiseMiddleware` rewrites the response `content-type` header
  when it carries a three-part `version=`.
- `LangOverrideMiddleware` replaces the request's `accept-language` header in a
  copied scope; every layer and route inside it reads the override.
- `PerfLogMiddleware` calls the injected sink once per logged request. The
  record omits the query string, cookies, `Authorization` and the body; the
  path may contain SDMX keys.
- `reject_unknown_query_params` raises `HTTPException(400)`; FastAPI turns it
  into a 400 response.
- `apply_vendor_markers` mutates the OpenAPI dict it receives, which is
  FastAPI's cached `app.openapi_schema`.

## Verification

- Run `just test` (unit tests) and `just check` (ruff, docstrings, prose, the
  `Any` ratchet, the index guard and pyrefly). `just ci` is the full gate.
- `tests/test_library_create_app.py` pins the middleware order and the
  `outer_middleware` slot.
- `tests/test_middleware_invariants.py` checks each adjacency's behaviour:
  ETag stability under compression, the `?lang=` hash, the two-part version on
  the wire, the gzip minimum size, perf-log wire size and method, HEAD headers,
  and CORS preflights.
- `tests/test_compression_roundtrip.py`, `tests/test_head_method.py`,
  `tests/test_perf_log.py`, `tests/test_cache_control_header.py` and
  `tests/test_openapi_vendor_markers.py` cover the individual layers.
  `tests/test_annotator_fingerprint.py` and `tests/test_publication_token.py`
  cover the ETag's token input.
