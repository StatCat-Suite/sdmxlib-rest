# Drift report, 2026-10-28

The EGM drift report over the eight `EGM.md` contracts the EGM bootstrap wrote
and the five `docs/design/` pages, before their first commit. Five reviewers
each read a set of documents against the source. The last section lists the
drift the bootstrap found between the code and `CLAUDE.md`, the guide pages
and the docstrings.

Each finding is tagged with where the fix belongs:

- **[doc]**: the document is wrong; correct it.
- **[code]**: the code breaks a rule the project holds; filed as an issue.
- **[both]**: a docstring or guide page and the contract disagree with the
  code in the same way; correct the prose.

## EGM.md

- [doc] Says a key is parsed against `dsd.key_slot_ids("3.0")`.
  `data/projection.py:key_dimension_ids` returns that only when the registry
  returns a materialised `sl.DataStructure`. For a missing or lazy DSD it falls
  back to `binding.dimension_column_map` order (`data/projection.py:197-213`),
  which the producer decides.

## src/EGM.md

No drift detected.

## src/sdmxlib_rest/EGM.md

- [code] `StructureCache.store` is said to receive 2xx responses with a body.
  The XML branch of `get_structure` returns before `cache.store`
  (`routes/structure.py:503-507`), so SDMX-ML responses are looked up and
  never stored. On the JSON path `cache.store` runs after `to_204_if_empty`
  (`routes/structure.py:540-542`), so an empty result is stored as a bodiless
  204. The `StructureCache` docstring (`structure_cache.py:52-54`) and
  `docs/seams.md` make the same claim.
- [doc] `ensure_acceptable`: the `xml`/`json`/`csv`/`parquet` shorthand passes
  only when one of the route's supported types contains the token
  (`negotiation.py:139-141`); `Accept: text/csv` on a JSON/XML route is a 406.
  An empty or missing `Accept` always passes (`negotiation.py:115-116`).
- [doc] Localisation sets `Content-Language` only when a node matched. With no
  `Accept-Language`, a bare `*`, unparseable JSON or no match,
  `localize_sdmx_json` returns `(body, None)` (`localization.py:50-53`,
  `122-134`).
- [doc] `OverlayAnnotator`'s fingerprint is `""` with no overlays, leaving
  `effective_token == token` (`overlays.py:58-59`, `157-159`), and each digest
  is over `applied(o, audience)`, so changing the audience changes it
  (`overlays.py:61`).
- [doc] Coverage: `OverlayAnnotator.overlays` (`overlays.py:123-126`),
  `RestSettings.canonical_spec_name` (`app.py:84`) and `Mount.spec_name = None`
  keeping a mount out of the `/docs` dropdown (`app.py:98`, `366`) are
  missing.

## src/sdmxlib_rest/data/EGM.md

- [doc] "Every helper takes ... an `sdmxlib.DataflowBinding`" is false for
  `parse_bracketed`, `parse_c_params`, `like_pattern`, `sql_ident`,
  `reject_unsupported_standard_params` (`params.py`), `resolve_projection_set`
  (`projection.py:38`), `qualify_is_active` (`sql.py:64`), all of `paging.py`,
  and `prepend_utf8_bom`, `stream_csv_zip`, `encode_parquet` (`encoding.py`).
- [doc] `code_map` caching: only `LazyCodelist.code_map` memoises, per
  instance and language; the eager `Codelist.code_map` recomputes on each call
  (sdmxlib `storage/lazy.py:582-600`, `model/codelist.py:560-572`).
- [doc] `decimals_format_replace_clause` takes the attribute id as
  `decimals_id` (default `"DECIMALS"`) and a `targets` keyword; neither is
  mentioned (`sql.py:23`, `50-52`).
- [doc] `parse_items_range` also returns `None` for a header `RANGE_RE` does not
  match: an open range such as `items=0-`, a suffix range, or several ranges
  (`paging.py:17`, `32-34`).

## src/sdmxlib_rest/middleware/EGM.md

- [doc] Counts six ASGI classes; there are seven. `IsalGZipMiddleware`
  (`compression.py:120`) is the one left out of the count.
- [both] Says `SelectiveGZipMiddleware` sits outside
  `SdmxVersionAdvertiseMiddleware` because the rewrite edits the
  `Content-Type` the skip-list reads. The skip-list compares the base type
  only (`compression.py:170`), and the rewrite changes only `;version=`
  (`version_token.py:33`), so either order compresses the same responses.
  `tests/test_middleware_invariants.py:196` passes in either order. The
  `_install_middleware` docstring (`app.py:284-285`) gives the same reason;
  `CLAUDE.md` requires the docstring and the order test to change together.
- [doc] Side effects omit Starlette's built-in `exclude_content_types`, which
  `_IsalGZipResponder` inherits (`compression.py:135`): gzip, grpc, image,
  audio, video, woff fonts and `text/event-stream` pass uncompressed.

## src/sdmxlib_rest/responses/EGM.md

- [both] `unwritable_content_response` is registered for every sdmxlib
  `ValidationError` (`app.py:124-128`), which sdmxlib also raises when an
  artefact is built (sdmxlib `model/errors.py:169`), for example by
  `DataConstraint(...)` in `render_availability`
  (`responses/availability.py:106-120`). Such a failure also gets the 500, and
  its body calls the content unwritable in the requested format
  (`unwritable.py:48-51`).
- [code] `to_204_if_empty` is said to pass any unparseable body through. A body
  that parses to a non-object (`[]`), or whose `data` is truthy and not a dict,
  raises `AttributeError` (`empty.py:65`, `90`).
- [doc] The handler is also installed on every mounted sub-app
  (`app.py:216-220`, pinned by
  `tests/test_unwritable_content.py:111`).

## src/sdmxlib_rest/routes/EGM.md

- [doc] Four type segments carry no `pattern`: `structureType`
  (`structure.py:329-335`), `itemSchemeType` (`structure.py:566-570`),
  availability `context` (`availability.py:269-275`) and metadata
  `structureType` (`metadata.py:344-350`). A bad value there is a 400 or 501,
  never a 422.
- [both] An empty `/availability` answer is a 204 for every format:
  `render_availability` returns it on `obs_count == 0` before negotiation
  (`responses/availability.py:137-138`). `docs/http.md` says XML answers 200.
- [doc] An empty `/metadata/metadataflow` answer is a 204 for JSON
  (`metadata.py:139-141`, `311`; `tests/test_metadata_routes.py:40-48`), not a
  200 with an empty message.
- [code] "`detail=full` materialises related artefacts; other details stub
  them" holds only for some walkers. `_resolve_children` appends the DSD as
  returned (`structure.py:889`), `_resolve_dataflows_referencing_scheme`
  returns raw Dataflows (`structure.py:863`), and
  `_walk_dataflow_categorisations` returns materialised Categorisations and
  raw CategorySchemes (`structure.py:983`, `997`). Under the stub details those
  come back unstubbed; under `full` the `children` DSD is not materialised.
- [doc] The Categorisation walk (`_walk_dataflow_categorisations`, called from
  `_walk_descendants`, `structure.py:955-999`) is missing from the Interface.
- [code] Same as above: XML responses never reach `StructureCache.store`.
- [doc] Annotator, splice and cache invariants hold for `get_structure` only;
  `get_structure_item` (`structure.py:565-676`) calls none of them.
- [doc] The synthesis log line is written only when the route scans; a
  precomputed hit returns at `availability.py:492`, before it.
- [doc] Same `key_dimension_ids` fallback as in `EGM.md`.
- [doc] `/availability` validates `references` against eight values and
  answers 400 otherwise (`responses/availability.py:55-57`, `167-172`).
- [doc] Verification omits `tests/test_render_availability.py`.

## tools/EGM.md

- [doc] `synthetic_bench.py build` also rebuilds when the stamp matches but has
  no `token`, or when the publication directory is gone
  (`tools/synthetic_bench.py:150-155`).

## docs/design/scope.md

- [doc] `tests/test_library_import_boundary.py` forbids only `statcatlib`,
  `pinax` and `pinaxlib` (line 23); it names no consumer.
- [doc] "Every data-plane helper needs `binding_for`" is false for the helpers
  listed under `data/EGM.md` above. `CLAUDE.md` makes the same claim.
- [both] `sdmxlib_rest.data` ships no dotted-key parser; `key_dimension_ids`
  returns the slot order and the consumer splits the key. `CLAUDE.md` lists
  "key parsing" among the helpers.
- [code] The library holds consumer-specific logic, against the page's rule:
  - `middleware/openapi_extensions.py:42-52` marks a consumer's paths as
    vendor paths: `/v1/contentconstraint/...`, `/contentconstraint/...`,
    `/hierarchicalcodelist/...`, `/v1/hierarchicalcodelist/...`,
    `/v1/hierarchyassociation/...`, `/health`, `/explorer`. Line 10 names
    `/v1/openapi.json`.
  - `middleware/openapi_extensions.py:21-31` hardcodes a consumer `/data`
    route's vendor parameters: `format`, `pivot`, `debug`, `_chunk`.
  - `middleware/query_param_guard.py:38` admits `within[` on every route, and
    advertises it (lines 17, 120); `within[` is a filter shaper's parameter
    (`filter_shaper.py:26`).
  - `middleware/query_param_guard.py:44` admits `_chunk` on every route.
  - `middleware/query_param_guard.py:79` exempts `/explorer` from the guard.
  - `splice.py:43` names a consumer's splice ids as examples.

## docs/design/index.md

- [doc] `docs/design/egm/` did not exist when the page named it. This report
  creates it.
- [doc] `history/design/` does not exist here; the page should say it is
  created when the first page moves.
- [both] The `_install_middleware` docstring does not give a failure for every
  adjacency. Its conditional-GET entry withdraws the ETag-over-compressed-bytes
  reason (`app.py:275-283`), and the perf-log and `outer` slots have no reason
  in it (`app.py:313-317`).

## docs/design/tokens.md

- [doc] `OpenedCatalog.token` defaults to `""` (`provider.py:64`).
  `CatalogRuntime.token` falls back to the provider's token, then
  `registry.manifest.token`, then `OpenedCatalog.build_id`
  (`runtime.py:118-129`).
- [doc] The ETag also hashes the app version (`middleware/etag.py:88`), so a
  release changes every ETag, and `Last-Modified` moves on a restart while the
  ETag holds (`runtime.py:149-183`). The opening sentence says the token alone
  decides.
- [doc] The ETag is predictable from sdmxlib only when the provider sets no
  token and an `OverlayAnnotator` is the only annotator. Inside a
  `CompositeMessageAnnotator` the fingerprint is index-prefixed
  (`message_annotator.py:107`) and no longer matches sdmxlib's
  `effective_token`.

## docs/design/url-grammar.md

- [doc] Same `key_dimension_ids` fallback as in `EGM.md`.
- [doc] Says a 1.5 key read against the 2.2.2 slots returns a wrong answer
  without an error. On these routes `sl.parse_components` rejects a key of the
  wrong length (sdmxlib `rest.py:56-61`), and the route answers 400
  (`routes/availability.py:468-473`, `tests/test_key_slots.py:94-106`). The
  silent shift happens in code that pads or does not check length, such as a
  forwarder.

## docs/design/availability.md

- [doc] The sidecar-join cost is 126× to 882× (`tests/bench/test_availability_sql.py:18`);
  the page says 126 to 202.
- [doc] The route comments and the bench say "~500M-row"; the page says 520
  million.
- [doc] The route comments carry tables for `componentID=all` only; the
  `mode=available` figures are in the bench docstring.
- [doc] The `mode=available` range mixes regimes: cold 1.03× to 1.62×, warm
  1.03× to 5.49× (`tests/bench/test_availability_sql.py:262`).
- [doc] The bench asserts ratios and does not repeat the route comment's
  tables; its unfiltered figures differ (bench lines 19-20).
- [doc] "5 to 8 s cold" for an unfiltered answer comes from
  [GitLab #7](https://gitlab.com/pinax-suite/sdmxlib-rest/-/issues/7), and the
  page does not cite it; the bench's cold figure is for `mode=available`.

## docs/design/content-validity.md

- [doc] Same `ValidationError` scope as under `responses/EGM.md`: the 500
  also covers construction-time rule failures.

## Outside the checked documents

The bootstrap found this drift between the code and `CLAUDE.md`, the guide
pages and the docstrings.

- [code] The availability path without `componentID` is not routed
  (`routes/availability.py:247`); `CLAUDE.md` and `docs/http.md` list it as a
  served spelling.
- [both] `asOf` is a 501 on structure and metadata only; on `/availability` it
  is the unknown-parameter 400. `docs/http.md` and `responses/asof.py` imply
  every route.
- [code] Four client-facing messages cite `docs/sdmx-rest-conformance.md`,
  which does not exist: `query_param_guard.py:121`, `metadata.py:191`,
  `metadata.py:272`, `structure.py:415`.
- [both] The `detail` description says `referencepartial` is 501; it is served
  as `referencestubs`
  ([GH-69](https://github.com/StatCat-Suite/sdmxlib-rest/issues/69)).
- [both] The `references` description for `parents` and `parentsandsiblings`
  overstates `_resolve_parents_and_siblings`, which returns every other
  Dataflow and the linked DSD, and nothing for non-Dataflows.
- [both] `docs/data-plane.md` lists `key_dimension_ids` under `data.params`;
  it is in `data/projection.py`.
- [both] `CLAUDE.md` and the `app.py` module docstring cite "an ETag over
  compressed bytes", which the ETag design rules out.
- [both] The `etag.py` docstring omits the version and language hash inputs and
  `Vary: Accept-Language`.
- [both] The `head_method.py` docstring says the perf log and CORS see GET;
  both sit outside it.
- [both] `perf_log.py` and an `app.py` comment call it outermost and say
  `wall_ms` includes CORS; CORS and `outer_middleware` sit outside it. It also
  skips `/health`, undocumented.
- [both] `docs/http.md` and the `compression.py` docstring list fewer skipped
  types than `SKIP_GZIP_CONTENT_TYPES` (which adds `application/x-parquet` and
  `application/zip`).
- [both] The `responses/empty.py` docstring says `to_204_if_empty` handles
  availability; it does not.
- [both] The `responses/__init__.py` docstring omits `availability.py` and
  `unwritable.py`.
- [both] The `_inject_constraint_annotations` docstring names a constraint name
  that `setdefault` lets the derived name override.
- [code] `responses/availability.py` imports the private `_stub_of` and
  `_concept_scheme_urns_for_dsd` from `routes/structure.py` inside functions to
  break an import cycle.
- [doc] `CLAUDE.md`'s Layout omits `logging.py`; its `just check` comment omits
  `checkfmt`.
- [doc] `pyproject.toml` says there is no docs site and points `Documentation`
  at the README; the site is https://statcat-suite.github.io/sdmxlib-rest/.
