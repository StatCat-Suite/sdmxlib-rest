# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.3] - 2026-10-22

Makes a fresh install work again, and moves the project to GitHub. Upgrade if
you resolve dependencies without a lockfile: 0.6.2 installed today pulls in
DuckDB 2.0 and fails every `/availability` request.

### Fixed

- **Caps `duckdb<2`.** DuckDB 2.0 removed `DuckDBPyConnection.pl`, which the
  sdmxlib releases this package allows call. The cap lifts when we move to an
  sdmxlib release that supports DuckDB 2.
- The `version=latest` and wildcard structure lookups read an artefact's agency
  as `agency_id`, not `getattr(maintainer, "id", None)`, so they can no longer
  build a URN with no agency. Found by pyrefly 1.4; the dev floor is now 1.4.0.

### Changed

- **The project lives at
  [github.com/StatCat-Suite/sdmxlib-rest](https://github.com/StatCat-Suite/sdmxlib-rest).**
  Its history there starts at 0.6.2; earlier history and the issue tracker
  stay readable on GitLab. A bare `#N` in the tree means the GitLab issue;
  GitHub issues are written `GH-N`.
- `asOf`'s OpenAPI description and 501 detail say the service runs one bundle
  "per process", not "per pod".
- The flag marking the zipfile isa-l patch is renamed
  `zipfile._sdmxlib_rest_isal_patched`. The patch is idempotent, so a consumer
  still setting the old flag only patches twice to the same effect.

## [0.6.2] - 2026-10-08

Lets consumers take sdmxlib 0.67, which fixes three bugs filed from here and
makes the per-request `/availability` overhead ~22% smaller. No code changes.

### Changed

- **Allows sdmxlib 0.67** (`>=0.65,<0.68`, was `<0.67`). The floor stays 0.65,
  which `/availability` needs for `Query.agg` and `sl.len()`. Of note in 0.67.0:
  - an organisation-scheme version is `1.0` in every format (sdmxlib#416), and
    the SDMX-JSON writer refuses a version its schema cannot carry (#417);
  - every `Ref` field wraps an `SdmxUrn` and **refuses URN text** (#418). A
    consumer that builds artefacts with a URN string in a reference field now
    gets an error at construction; wrap it with `SdmxUrn.parse` or `urn_for`.
    Nothing in this library did;
  - each artefact's spine is read once (part of #426), taking
    `registry.observations()`, which `/availability` pays once per request, from
    15.7 to 12.3 ms on a large census bundle.

  0.66.1, tagged upstream but never published, is included in 0.67.0.

## [0.6.1] - 2026-10-07

Lets consumers take sdmxlib 0.66. No code changes.

### Changed

- **Allows sdmxlib 0.66** (`>=0.65,<0.67`, was `<0.66`). 0.66.0 is typing-only:
  a return-widening audit, `SdmxUrn.parse(text)` typed `SdmxUrn[object]`
  (sdmxlib#399), and pyrefly as sdmxlib's sole checker. The floor stays 0.65,
  which `/availability` needs for `Query.agg` and `sl.len()`.

## [0.6.0] - 2026-10-07

`/availability` now runs on sdmxlib's query DSL instead of SQL it built itself,
which finishes the milestone that started with measuring it. Answers are
identical to 0.5.x on every request shape tested against a large census bundle.
What changes for a consumer is where the rows are read from: the registry's own
data plane, not `meta_conn`. Breaking for a provider whose lake is attached only
to some other connection; the migration is below.

Also: content sdmxlib refuses to serialise now produces a 500 that names the
artefact and the rule, and error responses are no longer stamped cacheable.

**Needs sdmxlib `>=0.65,<0.66`** (was `>=0.62.2`). The route calls `Query.agg` and
`sl.len()`, which 0.65 introduced (sdmxlib#414).

### Breaking

- **`/availability` reads observations through `registry.observations(flow)`**
  (#6). The c[] operators, key filters, `startPeriod`/`endPeriod`, the
  per-component value lists, the filtered `componentID=all` single scan and the
  sidecar join are all sdmxlib's now (sdmxlib#412, #413, #414). The hand-built
  SQL, its `LIKE` escaping, the sidecar-join derivation and the WHERE-text search
  that decided when to join are gone.

  **Migrating.** The registry a `CatalogProvider` returns must reach its lake on
  its own connection. `LocalRegistry.bundle(path)` does, attaching lazily on the
  first observation query, and so does pinax's `Catalog`. A lake attached only to
  `meta_conn` (or any other connection) is invisible to the DSL, and every
  `/availability` request will fail. `meta_conn` is still required: it is
  handed to `ComponentFilterShaper`, `MessageAnnotator` and `MetadataStore`.
  A shaper's `SqlRelation` values must be built on the registry's connection,
  which they are when they come from `registry.query(...)`.

  **A series-attributes sidecar with duplicate join keys is refused** with a
  500 that names the flow and the table (#8). Joining it would multiply every
  observation it matched, and the "(N observations)" badge came back inflated
  with nothing to say so. sdmxlib refuses when the flow's query root is built,
  so every query on such a flow is refused, not only the ones that join. The
  defect is the producer's, and belongs in a publish-time check there.

  **Cost.** Warm requests on small flows carry ~10-20 ms more than 0.5.x, the
  time `registry.observations()` spends re-reading the flow, DSD and binding on
  every call (sdmxlib#426). On large flows the query dominates and it is noise.
  Unfiltered requests served by `AvailabilityValues` do not pay it.

### Added

- **A 500 for unwritable content says what failed** (#10). sdmxlib refuses to
  serialise content no format can carry: a component with no
  `ConceptIdentity`, a metadataflow with no targets, a `MetadataSet` version
  SDMX-JSON rejects. That used to reach clients as a bare
  `Internal Server Error`. Now a JSON body lists each issue's `path` (artefact
  and field) and `message` (the rule), plus the request `target`, and logs the
  same. Installed on the app and on every mount. There is deliberately no switch
  to turn validation off.

### Fixed

- **Error responses were stamped cacheable.** `ConditionalGetMiddleware` added
  `ETag` and `Cache-Control` to every response whatever its status, so a 500 was
  publicly cacheable for up to an hour stale. Worse, a client revalidating with
  its ETag got a 304, telling it to keep the error. Only 2xx responses are
  stamped now.
- **`mode=available` dropped `startPeriod`/`endPeriod` for every component**,
  not only the time dimension's own query. With `startPeriod=2021`, SEX listed
  values that exist only in 2020.
- **`key_dimension_ids` survived a bundle reopen.** Its memo was module-level and
  keyed on the flow URN alone, so a runtime closed and reopened in-process at a
  new build kept parsing dotted keys against the previous build's dimension
  order. It is now per registry, and the signature is unchanged.

### Internal

- The test fixture is a real bundle (`meta.duckdb` + `lake.ducklake` +
  `parquet/`, opened with `LocalRegistry.bundle`), because the DSL reads through
  the registry's data plane. `mode=available` gained the filtered-request
  coverage it never had, and the case the old sidecar-join search existed for (a
  shaper constraining a sidecar column the request never named) is now pinned as
  route behaviour.
- `CatalogRuntime.schema_inspector`, added on main for #8 and never released, is
  gone again: the DSL owns the inspector.

## [0.5.1] - 2026-10-05

Two fixes for large codelists, both found porting the first consumer and both inside the
route where no consumer cache can reach them. Responses are unchanged — a new
round-trip test proves it field for field in all three formats.

### Fixed

- A stub request for a codelist or hierarchy no longer loads its items.
  `/structure/{type}/...` with `detail=allstubs` or `allcompletestubs` loaded the
  queried artefact in full before looking at `detail`, only to stub it — and in a
  listing that was every codelist in the catalogue. The load existed to hand the
  references walk a materialised object, but no walker acts on a codelist or a
  hierarchy, so it was spent for nothing whatever `references` said, including
  the `references=children&detail=allcompletestubs` shape .Stat sends for
  structure listings. Measured against 0.5.0 with a 100k-code codelist: one
  codelist 653 ms → 8 ms, a stub listing 610 ms → 15 ms, the .Stat shape
  598 ms → 14 ms; responses identical. `detail=full` still loads, unchanged.
  Distinct from the #9 fix, which covered codelists *referenced by* the queried
  artefact; this is the queried artefact itself.

- `references=` materialised every referenced codelist in full instead of
  stubbing it from its lazy wrapper (#9). Eleven call sites stubbed with
  `_stub_lazy(x) or _stub(_materialize(x))`; sdmxlib 0.62 gave `Codelist` a
  `__len__`, so the stub — its codes emptied by design — became falsy and every
  site threw it away and materialised the codelist to stub it again. That costs
  ~5.6 ms per thousand codes: 12 ms for 619, 565 ms for 100k, about 3 s per stub on
  a codelist of half a million codes, on every request and inside the route where no consumer
  cache can reach it. Present since 0.2.0, the release that moved to sdmxlib 0.62.
  `/structure` and `/availability`'s `references=` paths were both affected.

  All eleven now go through one `_stub_of` that tests against `None`. Only
  `Codelist` is sized today, but the other ten sites were one sdmxlib release
  away from the same bug, and an AST guard test now fails if the `or` form
  reappears anywhere in the package.

  **Responses are unchanged** — byte-identical apart from the `prepared`
  timestamp, measured on `/structure?references=children`,
  `/availability?references=codelist` and `/structure/codelist?detail=allstubs`.
  That took a second fix: the lazy stub copied a hand-kept list of six fields and
  so dropped annotations, validity dates, `uri` and extensions, while artefacts
  with no lazy wrapper kept them. Since 0.2.0 the bug had been routing codelists
  through the materialising path, so clients have been receiving those fields;
  the lazy stub now derives its fields from the concrete class, and a test
  asserts it equals the materialised stub for both `Codelist` and `Hierarchy`.
  Measured with a 100k-code codelist: `references=children` 614 ms → 32 ms,
  `references=codelist` 636 ms → 31 ms.

  Not changed: requesting a large codelist *itself* with a stub `detail` still
  materialises it once, because the references walk is handed the full object.

### Internal

- A round-trip test (`tests/test_round_trip.py`): request a response, parse it
  back with sdmxlib's reader, and compare every codelist field against the
  registry, in SDMX-JSON 2.0, SDMX-ML 2.1 and SDMX-ML 3.0. Nothing is listed field
  by field, so a field dropped anywhere — stub builder, serialiser, splice — fails
  whichever field it is. Run against the first version of the #9 fix, which
  dropped annotations, `uri` and validity dates from lazy stubs, it fails every
  stub route in every format; the speed tests that version shipped with did not.
  A companion test fails when sdmxlib adds a `Codelist` field the rich fixture
  leaves at its default, since a round trip only checks what the fixture sets.
  `isFinal` is excluded for SDMX-JSON 2.0 and SDMX-ML 3.0 only — it exists solely
  in the SDMX-ML 2.1 schemas — and each exclusion is asserted to be a real loss,
  so it fails rather than hides if a format starts carrying the field.
- The shared test fixture built concept references by appending the id to the
  scheme URN, giving `ConceptScheme=AG:CS_POP(1.0).REF_AREA` — an item URN with
  its scheme's class name. JSON readers accept it; the SDMX-ML 2.1 reader rejects
  it as schema-invalid. No test parsed an XML response back until the round trip,
  so every route test had been running on an invalid DSD. Built with
  `Concept.urn_for` now.

## [0.5.0] - 2026-10-05

Availability precompute moves from rendered responses to value lists. A consumer
that baked responses now bakes the per-component values and a count, and the
library renders them — so one precomputed entry serves every format, language and
request spelling, where the old seam needed a slot for each and still missed
`?lang=fr`. Breaking for any consumer implementing `AvailabilityCache`; the
migration is below.

Also: the middleware chain gets behavioural coverage rather than just an order
assertion, and the bench tier can measure a cold cache repeatably — which
corrected a cold-cache figure that had been informing a design decision.

### Breaking

- **`AvailabilityCache` is replaced by `AvailabilityValues`** — a seam over value
  lists rather than rendered responses (#7). `availability_cache.py`,
  `AvailabilityQuery`, `NullAvailabilityCache`, `CatalogRuntime.availability_cache`
  and `get_availability_cache` are gone; their replacements are
  `availability_values.py`, `AvailabilityKey`, `ResolvedAvailability`,
  `NullAvailabilityValues`, `CatalogRuntime.availability_values` and
  `get_availability_values`.

  The old key was the raw request path and query string plus both negotiation
  headers, so every client spelling was its own slot. A consumer baking files had to plant
  each under every accept variant and query suffix, and still missed `?lang=fr`
  and the whole 2.2.2 path. The new key is `(build_id, flow_urn, component_id)`: mode,
  format, language, `references=` and `dimensionAtObservation` change the
  rendering and not the values, so one entry serves every one of them.

  **The seam is consulted only for unfiltered requests**, judged after the
  `ComponentFilterShaper` runs — a shaper can add a constraint the URL never
  named, and a precomputed unfiltered answer would then be wrong. Precompute can
  only cover that case anyway, since nobody can bake every filter combination;
  restricting the seam to it is what keeps the key free of any filter
  normalisation. Mode drops out for the same reason: with no constraints,
  `mode=available` relaxes nothing and returns what `mode=exact` does.

  **Migrating.** A consumer that baked rendered responses now bakes value lists
  and a count — far smaller, and format-free — and returns them from `lookup`.
  `store` is offered the scanned values on a miss, so a consumer with no bake can
  still scan once per build. Repeat-caching *filtered* answers is memoisation
  rather than precompute, and belongs in an HTTP response cache installed through
  `create_app(outer_middleware=...)`; it sits outside compression there, so it
  must key on `Accept-Encoding` as well as `Accept` and `Accept-Language`.

### Added

- `sdmxlib_rest.responses.availability.render_availability()` renders an
  availability response from value lists and an observation count, with no scan.
  Everything after the computation — `DataConstraint` synthesis, the
  `references=` walk, 204-on-empty, XML/JSON negotiation, the `obs_count`
  annotation .Stat reads, localisation — now lives behind one call, which the
  route itself uses. A consumer holding precomputed values gets a conformant
  response without reimplementing the format, and there is one renderer rather
  than a live one and a baked one that can drift (#7). `registry` stays required
  because the `references=` walk resolves artefacts out of the flow's DSD; that is
  the one part a consumer cannot serve from baked values alone.

### Fixed

- `_install_middleware`'s docstring claimed `ConditionalGet` must be innermost
  because its ETag "hashes the uncompressed body", so moving it outward would
  make ETags vary with compression. It does not hash the body: the signature is
  `version|build_id|path|query|accept|resolved_lang`, computed before the app
  runs. The ETag is compression-independent by construction, and the only
  adjacency a reorder provably breaks is `LangOverride` sitting outside it. The
  docstring now says that, with the test that defends it named.

### Internal

- The bundle tier can measure a **cold** cache repeatably.
  `Bundle.evict_page_cache()` drops the lake's parquet files with
  `POSIX_FADV_DONTNEED`, which evicts clean pages and needs no privileges, and
  `test_eviction_actually_cools_the_cache` fails if that ever stops biting —
  without it every cold assertion would silently measure the warm regime.

  This corrected a conclusion. A 43.91 s figure taken as `mode=available`'s cold
  cost was a first-ever-run in a fresh process, also paying extension load,
  DuckLake metadata and first file opens. Re-measured with eviction, the
  per-dimension loop beats the single-scan `FILTER` form in **every** regime
  (cold 1.03x-1.62x, warm 1.03x-5.49x), so `mode=available` keeps its N+1 loop
  as a measured choice — and sdmxlib is not asked for the column-level
  `unique(where=…)` the earlier reading had made look necessary.
- Fan-out sidecar coverage for #8: a 1:1 sidecar leaves `COUNT(*)` alone, a 3x
  fan-out turns 6 observations into 18, and the `EXISTS` form is immune at fan
  1/3/7. Synthetic and unit-level, so they run in `just ci` — no real bundle has
  a fan-out sidecar, which is why the defect has never shown up in practice.
- The middleware chain has behavioural coverage, not just an order assertion
  (#1). Thirteen tests drive real requests through the stack and pin what each
  adjacency protects: an ETag that survives compression, a `?lang=` override
  that hashes identically to the equivalent header, the two-part SDMX version
  token on the wire, a perf tap that records wire size and the client's original
  method, and a CORS preflight a consumer's own middleware cannot reject. Each
  was watched to fail against a real reorder of `_install_middleware`.

## [0.4.0] - 2026-10-02

Two defects in `/availability`'s hand-built SQL, found by measuring it against a
real bundle for the first time, plus the benchmark tier that found them. One is a
correctness fix a consumer should take deliberately: the observation count this
service reports could be silently inflated.

The minor bump rather than a patch is the `duckdb` floor — raising it can change
what a consumer resolves, which is more than a patch should do.

### Changed

- `duckdb` is now `>=1.5.5`, up from `>=1.1`. Nothing was tested below 1.5.5 and
  nothing could resolve below it either — sdmxlib requires `>=1.5.5` and is a
  hard dependency — so the old floor advertised support that was never exercised,
  and would have become real if sdmxlib ever relaxed. No upper bound: sdmxlib's
  runtime scan-integrity guard refuses a store whose scans lose rows whatever
  version is installed, so a DuckDB upgrade is a performance decision rather than
  a correctness risk. 1.5.6 measured as indistinguishable from 1.5.5 on every
  availability query shape, so there is no urgency either way.
- `/availability`'s `componentID=all` combined aggregate is now used **only when
  a filter narrows the scan**. Unfiltered it loses to the per-dimension loop by
  2.8x — each unfiltered `DISTINCT` is cheap while the combined form pays to
  materialise a large sorted array — and unfiltered is the dominant
  .Stat request. Filtered it wins by 1.1x-4.8x, because any predicate makes every
  `DISTINCT` pay a scan of its own and one scan then beats six. Measured
  2026-10-01 on a large census bundle; the comment carries the table and the date, since
  the number it replaces was true when written and then silently expired.

### Fixed

- `/availability` appended the series-attributes `LEFT JOIN` to every query it
  built, `COUNT(*)` included. DuckDB elides it for a single-column `DISTINCT`
  but cannot for `COUNT(*)` — a LEFT JOIN whose right side has duplicate keys
  legitimately changes the count — so the join was paid in full on the one shape
  that can never need it: **126x-1301x** on a ~500M-row bundle,
  ~2.2x on a whole availability request. The join is now emitted only when the
  query references a column the sidecar alone carries.

  This was also a latent correctness bug. On a bundle whose sidecar is 1:1 the
  joined count is right by luck; on one whose sidecar fans out, the `obs_count`
  shipped to .Stat for its "(N observations)" badge was silently inflated.

- The `componentID=all` combined aggregate carried no join at all, which is
  wrong whenever a `c[]` filter reaches a sidecar-only column — `mode=exact`
  admits such a filter, and the aggregate was then computed over the unjoined
  table. It now takes the same conditional join as every other shape.

### Internal

- A bundle-tier benchmark (`tests/bench/`, `just bench <dir>`) that asserts
  **ratios, not wall times**. The same query drifted 2x between identical runs
  on page-cache state alone while every ratio held, so each assertion is a ratio
  against the same query without the clause under test. Opt-in via
  `SDMXLIB_REST_BENCH_BUNDLE`; skipped without a bundle, and deselected from
  `just ci`. It prices the two defects in milestone 1 — the unconditional
  sidecar join on `COUNT(*)` and the `componentID=all` combined aggregate — and
  records whether the bundle's sidecar is 1:1, since that is what decides
  whether the shipped `obs_count` is correct.

## [0.3.1] - 2026-10-01

### Changed

- `sdmxlib` is now `>=0.62.2,<0.64`. 0.63.0 carries no breaking change — three
  additive features (`ref_to`, `coded_as="string"`, `scan_csv` resolving the
  structure a file names) and a variance fix — and none of it touches a path
  this package uses, so excluding it bought nothing. The range spans two minors
  rather than moving to one: the full gate is green on 0.62.2 and 0.63.0 both,
  and a consumer already pinned to 0.62.2 should not have to move to take this.
  The upper bound stays real, because sdmxlib ships no deprecation shims.

## [0.3.0] - 2026-10-01

sdmxlib 0.62.2 closed both seams this package was patching around
([sdmxlib#406](https://gitlab.com/pinax-suite/sdmxlib/-/work_items/406)), so the
patches go.

### Breaking

- `responses/urn_injection` is deleted, with `inject_urns` and
  `inject_urns_in_place`. sdmxlib's SDMX-JSON writer now emits
  `links[rel="self"]` on every maintainable, from the single `_maint_fields`
  path every artefact DTO goes through.
- `run_structure_splices` no longer takes `inject_urns`. There is no
  library-owned built-in splice left to order around, so a consumer's splices
  are simply run in the order it registered them.
- Requires `sdmxlib>=0.62.2,<0.63`, which is where both of the above land.

A consumer that registered its own URN splice should delete it rather than keep
it: the top-level `urn` property that splice also wrote is **not** valid
SDMX-JSON 2.1, whose schema closes every object with `unevaluatedProperties`.
sdmxlib emits only the `links` form for that reason, and the .Stat Suite client
reads only that form anyway.

### Changed

- The two `/metadata` routes ask `registry.get(..., expect=sl.MetadataSet)`
  instead of filtering an `isinstance` over a union. The runtime check and the
  static narrowing were always one operation there — a stored row may name
  something that is not a MetadataSet, and before the filter existed such a row
  reached the writer as one and failed there.

### Added

- `test_a_maintainable_states_its_own_urn_in_links` pins the self-URN now that
  this package no longer writes it, including the deliberate absence of the
  top-level property.

### Internal

- `just release-4-push` publishes the curated `CHANGELOG.md` section via
  `--notes-file` instead of regenerating notes with `git-cliff --latest`. The
  generated form contradicts the entry curated two steps earlier — v0.2.1 is
  the worked example, where cliff listed a `uv_build` range that lost a merge
  and never shipped. Extraction runs before the push, so a missing or empty
  section aborts while it is still local rather than leaving a pushed tag with
  no release attached.

## [0.2.1] - 2026-09-23

Maintenance. No route, seam or runtime behaviour changes — the only thing a
consumer can observe is the build-backend range and some annotations that were
always implied.

### Changed

- `build-system.requires` is now `uv_build>=0.11.6,<0.13.0`. The old
  `<0.12.0` ceiling excluded the uv most installs now carry, so `uv build`
  warned and anyone building the sdist from source resolved a backend the
  metadata declared unsupported. The upper bound stays real rather than being
  dropped: uv_build is pre-1.0 and its minors may change build behaviour.
- Annotations on the response splices and the structure/metadata route
  helpers, tightened to satisfy pyrefly's strict preset. The only one on an
  exported name: `inject_urns_in_place` and `inject_urns` now say
  `dict[str, Any]` where they said a bare `dict`. The rest are internal, and
  no signature changed shape.

### Internal

- `pyrefly` joined `just check`. The findings backlog inherited from the
  extraction is burned down to 0 (one documented suppression remains, for a
  gap in pyrefly's attrs plugin), and a green gate that only runs on request
  stops being a gate.

## [0.2.0] - 2026-09-23

Ported to sdmxlib 0.62. sdmxlib 0.50 deleted `FederatedCatalog` — the type this
package was built on — so the provider seam now hands back a `LocalRegistry`.
sdmxlib breaks cleanly between minors and ships no deprecation shims, so this
release pins a single minor.

### Breaking

- `OpenedCatalog.catalog` is now `OpenedCatalog.registry`, typed
  `sl.LocalRegistry`. A provider's `open()` returns `registry=` instead of
  `catalog=`.
- `CatalogRuntime.catalog` is now `CatalogRuntime.registry`.
- The FastAPI dependency `get_catalog` is now `get_registry`.
- `MessageAnnotator.annotate` and `FilterContext` take `registry=`, not
  `catalog=`.
- Requires `sdmxlib>=0.62,<0.63`.

Named `registry` rather than kept as `catalog` because a field called `catalog`
holding a registry is the drift sdmxlib spent 0.50 removing. Typed concretely
rather than behind a Protocol because `binding_for` — which every data-plane
helper needs — exists only on `LocalRegistry`; 0.48's `FederatedCatalog` was
equally concrete, so nothing previously open is narrowed.

### Fixed

- Every gzip-eligible response over `gzip_minimum_size` crashed mid-send
  against Starlette 1.4. `_IsalGZipResponder` skipped `GZipResponder.__init__`
  to install its own `IGzipFile`, but Starlette had moved to a
  `zlib.compressobj` behind a `compressor` property and added
  `thread_minimum_size`; the subclass set neither, so the responder raised
  `AttributeError` after the response had started and clients received a
  truncated body.
- `/availability` responses could not be serialised at all under sdmxlib 0.62,
  which validates on serialise: the synthesised `DataConstraint` carried no
  name, and every `NameableArtefact` requires one.

### Changed

- `_shared_join_columns` asks the DuckDB connection for a table's columns
  instead of the data store, which 0.62 exposes only through a private
  `LocalRegistry` member. The connection is also the one the route runs the
  JOIN on, so the two can no longer disagree.
- Dotted keys are parsed against `DataStructure.key_slot_ids("3.0")`. Every
  path this package publishes is SDMX-REST 2.2.2 grammar, where the time
  dimension is a key slot; a 1.5 surface is a consumer's forwarder.

### Added

- Route-level test coverage for the structure, metadata and availability
  planes, which previously had none — 44 tests to 94.

## [0.1.0] - 2026-08-05

Initial release. Extracted from its first consumer, where the package was
developed in place against a production service and its test suite before being split out.

### Added

- Structure, metadata and availability route planes.
- `create_app` — middleware stack, runtime sharing across mounts, multi-spec docs page.
- Seam Protocols on `CatalogRuntime`, each with an inert default:
  `CatalogProvider`, `SpliceRegistry`, `StructureCache`, `AvailabilityCache`,
  `MetadataStore`, `ComponentFilterShaper`, `MessageAnnotator`.
- `sdmxlib_rest.data` — data-plane helpers (parameter parsing, projection,
  paging headers, binding-driven SQL fragments, arrow→CSV/parquet encoders).
  The `/data` route itself stays with the consumer, by design.
- Middleware: conditional GET/ETag, selective gzip, HEAD→GET, language
  override, SDMX version advertisement, unknown-query-param guard, perf log,
  OpenAPI vendor markers.
