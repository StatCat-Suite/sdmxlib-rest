# sdmxlib_rest.routes

## Overview

`sdmxlib_rest.routes` holds the three SDMX-REST 2.2.2 routers the library owns:
structure, reference metadata and availability. Each module is a FastAPI
`APIRouter` plus its handlers. A handler reads the registry and seams through
the request dependencies in `runtime.py` and never names a catalog.
`app.library_routers()` returns `(structure.router, metadata.router,
availability.router)`. `create_app` includes only the routers passed in its
`routers` argument (default empty), in the order given. The library ships no
`/data` route; consumers own it and build it from `sdmxlib_rest.data`
helpers.

## Interface

- `router` and `get_structure` (`structure.py`): serve
  `GET /structure/{structureType}/{agencyID}/{resourceID}/{version}`.
  `structureType` is one of the 12 keys of `_TYPES` (`datastructure`,
  `dataflow`, `codelist`, `conceptscheme`, `hierarchy`,
  `hierarchyassociation`, `categorisation`, `categoryscheme`, `agencyscheme`,
  `dataproviderscheme`, `metadatastructure`, `metadataflow`), matched
  case-insensitively. Query parameters are `detail` (`_Detail`, default
  `full`), `references` (any string, default `none`) and `asof`. Responses are
  SDMX-JSON 2.0, SDMX-ML 2.1 or SDMX-ML 3.0 (`_SUPPORTED_STRUCTURE_MEDIA`).
- `get_structure_item` (`structure.py`): serves
  `GET /structure/{itemSchemeType}/{agencyID}/{resourceID}/{version}/{itemID}`.
  It returns the parent scheme with only the matching item. `codelist` and
  `conceptscheme` are implemented (`_ITEM_SCHEME_TYPES`); the 12 names in
  `_SPEC_ITEM_SCHEME_TYPES_UNIMPLEMENTED` answer 501.
- Reference walkers (`structure.py`): `_resolve_extras` dispatches
  `references` to `_resolve_children` (`children`), `_walk_descendants`
  (`descendants`, `all`), `_resolve_parents_and_siblings` (`parents`,
  `parentsandsiblings`) and `_resolve_dataflows_referencing_scheme`
  (`dataflow` on a `CategoryScheme`). Any other token adds nothing.
  `_walk_dsd_outbound_refs` collects a DSD's codelists, concept schemes,
  hierarchy associations and their hierarchies. For a Dataflow,
  `_walk_descendants` also calls `_walk_dataflow_categorisations`, which adds
  every Categorisation whose source is the flow and, once each, the
  CategoryScheme its target category belongs to.
  `_resolve_parents_and_siblings` returns every other Dataflow in the
  registry and the flow's DSD, and nothing for an artefact that is not a
  Dataflow.
- Shaping helpers (`structure.py`): `_materialize`, `_stub`, `_stub_lazy`,
  `_stub_of`, `_evolve`, `_build_message`, `_build_message_many`,
  `_resolve_latest`, `_enumerate_with_wildcards`, `_version_key` and
  `_normalize_wildcard`.
- `router`, `get_metadataset`, `get_metadataflow_sets` and
  `get_structure_attached_sets` (`metadata.py`): serve
  `GET /metadata/metadataset/{providerID}/{resourceID}/{version}`,
  `GET /metadata/metadataflow/{agencyID}/{resourceID}/{version}/{providerID}`
  and `GET /metadata/structure/{structureType}/{agencyID}/{resourceID}/{version}`.
  The last two take `detail` (`full` or `allstubs`) and `limit` (1 to 1000;
  defaults 100 and 500). Responses are SDMX-JSON 2.0 metadata or SDMX-ML 3.0
  (`_SUPPORTED_METADATA_MEDIA`). `_item_urn_prefix` builds the SQL `LIKE`
  pattern for item-level targets, and `_strip_attributes` clears a set's
  reported attributes.
- `router` and `get_availability` (`availability.py`): serve
  `GET /availability/{context}/{agencyID}/{resourceID}/{version}/{key}/{componentID}`.
  Query parameters are `mode` (`exact` or `available`, default `exact`),
  `references`, `startPeriod`, `endPeriod`, `c[<id>]` filters,
  `updatedAfter`, `reportingYearStartDay` and `dimensionAtObservation`.
  `render_availability` (`responses/availability.py`) writes the response.
- Availability helpers (`availability.py`): `_shaped_filter`, `_constrains`,
  `_narrow`, `_NON_EQ` (the `ne`, `lt`, `le`, `gt`, `ge`, `co`, `nc`, `sw` and
  `ew` operators), `_distinct`, `_observations`, `_time_dimension_id`,
  `_strip_dim_from_key` and the module-level
  `reject_unsupported_standard_params`.

## Invariants

- Every handler receives the registry, `meta_conn`, tokens and seams through
  `Depends` on `runtime.py` getters (`get_registry`, `get_meta_conn`,
  `get_token`, `get_effective_token`, `get_splices`, `get_structure_cache`,
  `get_message_annotator`, `get_metadata_store`, `get_availability_values`,
  `get_filter_shaper`). No module imports a consumer or names a bundle.
- Path segments are validated by regex in `Path(pattern=...)`, so a malformed
  reference is a 422 from FastAPI. Four type segments carry no `pattern`:
  `structureType` and `itemSchemeType` on `/structure`, `context` on
  `/availability` and `structureType` on `/metadata/structure`. A bad value
  there is a 400 or a 501. Structure routes accept `all`, `*` and
  `latest`; `_normalize_wildcard` maps `*` to `all` at handler entry.
  Metadata routes accept no wildcard, except `providerID=all` on
  `/metadata/metadataflow`. `/availability` takes `VERSION_PATTERN` from
  `data/params.py`, which is numeric only, so `latest` is a 422.
- `ensure_acceptable` runs before any registry read, so an `Accept` matching
  no supported media type is a 406 on every route.
- `reject_asof` makes `asof` a 501 on the structure and metadata routes.
  `reject_unsupported_standard_params` makes `updatedAfter` and
  `reportingYearStartDay` a 501 on `/availability`.
- On `/structure`, `detail=raw` is a 501 and an unknown `structureType` is a
  400. A concrete `version=latest` resolves through `_resolve_latest`, which
  orders versions numerically by `_version_key` (`2.10` above `2.9`), and
  answers 404 when no version exists. A missing artefact is a 404.
- `detail` shapes the queried artefact and `references` selects the related
  artefacts; `_resolve_extras` computes them independently. A `reference*`
  detail with `references=none` walks `descendants`. The walkers that shape
  through `_walk_dsd_outbound_refs`, `_resolve_parents_and_siblings` and the
  DSD step of `_walk_descendants` return materialised artefacts under
  `detail=full` and stubs under every other detail (GH-32). Three walkers
  return artefacts as the registry gives them under every detail:
  `_resolve_children` appends the DSD unshaped,
  `_resolve_dataflows_referencing_scheme` returns raw Dataflows, and
  `_walk_dataflow_categorisations` returns materialised Categorisations and
  raw CategorySchemes (GH-75). `referencepartial` takes
  the `referencestubs` path.
- A stub never reads an item tree. `_stub_of` builds stubs of `LazyCodelist`
  and `LazyHierarchy` from the wrapper (`_stub_lazy`), deriving the fields
  from the concrete attrs class, and tests the result against `None` because
  an empty `Codelist` is falsy (sdmxlib#398). `get_structure` and
  `_enumerate_with_wildcards` materialise an artefact only when `detail` is
  outside the stub bucket.
- `_enumerate_with_wildcards` deduplicates related artefacts across the
  listing by URN string (`_urn_str`). With `version=latest` it keeps one
  artefact per (agency, id), at the highest version.
- `get_structure` consults `StructureCache.lookup` after resolving `latest`
  and before any rendering, and keys `StructureQuery` on `effective_token`,
  `Accept` and `Accept-Language` as well as the path and query. It calls
  `StructureCache.store` on the JSON path only, after `to_204_if_empty`:
  SDMX-ML responses are looked up in `StructureCache` and never stored, and
  an empty JSON result is stored as a bodiless 204 (GH-74).
- In `get_structure`, `MessageAnnotator.annotate` runs on the
  `StructureMessage` before either writer, so its annotations reach SDMX-JSON
  and SDMX-ML. Post-serialise `SpliceRegistry` splices
  (`run_structure_splices`) run on the JSON body only. `get_structure_item`
  calls no annotator, no splice and no `StructureCache`.
- An `Accept` containing `xml` selects SDMX-ML unless `is_browser_request`
  reports a browser; on the structure routes `3.0` in `Accept` selects
  SDMX-ML 3.0, otherwise 2.1. Metadata routes write SDMX-ML 3.0 only.
- On `/structure` and `/metadata`, an empty SDMX-JSON result answers 204
  through `to_204_if_empty`, and an empty SDMX-ML result answers 200. On
  `/availability` an empty answer (`obs_count == 0`) is a 204 for every
  format, XML included, because `render_availability` returns it before
  negotiating the format.
- `/metadata/metadataflow` with no sets answers 404 when the metadataflow is
  absent from the registry. When it exists, the answer is a 204 for JSON and
  a 200 with an empty message for SDMX-ML. The
  metadata routes resolve each store URN with `registry.get(...,
  expect=sl.MetadataSet)` and drop rows that are not metadata sets. An
  unknown `structureType` on `/metadata/structure` is a 400.
- `/availability` answers only `context=dataflow`; any other context is a
  501. A flow with no `registry.binding_for` result is a 404. An unknown
  `componentID` is a 400 naming the valid dimensions and attributes.
  `componentID` of `all` or `*` covers every key dimension in
  `binding.dimension_column_map`. `references` takes `none`, `all`,
  `datastructure`, `dataflow`, `codelist`, `conceptscheme`,
  `dataproviderscheme` and `valuelist`; any other token is a 400.
- `/availability` parses the key against `key_dimension_ids`
  (`data/projection.py`). When the registry returns a materialised
  `sl.DataStructure`, that is `dsd.key_slot_ids("3.0")`, in which time is a
  key slot. For a missing or lazy DSD it is the order of
  `binding.dimension_column_map`, which the producer decides. A short or long
  key is a 400 (`_shaped_filter` raises `ValueError` or `KeyError`). The data
  plane takes no `ApiVersion`.
- `ComponentFilterShaper.shape` rewrites every parsed filter, including each
  relaxed filter under `mode=available`.
- `AvailabilityValues` is consulted only when the shaped filter, the non-equality
  `c[]` filters and the period bounds are all empty (`unfiltered`). The key is
  `AvailabilityKey(token, flow_urn, component_id)`, keyed on the resolved
  flow URN. An unfiltered scan offers its result to `store`, a zero count
  included.
- `/availability` builds one query root per request with `registry.obs`
  (`_observations`) and reuses it for every component; no root is cached
  across requests. A `ValueError` from `registry.obs` (a sidecar with more
  than one row per join key, sdmxlib#412) is a 500 naming the flow.
- `startPeriod` and `endPeriod` bound the DSD time dimension inclusively.
  `_time_dimension_id` reads the DSD only when a bound is given and falls
  back to `TIME_PERIOD`.
- `mode=exact` with `componentID=all` computes every distinct set and the
  count in one aggregate (`agg` plus `sl.len()`). `mode=available` drops each
  component's own key segment, `c[]` filters and, for the time dimension, the
  period bounds; its `obs_count` uses the full filter.
- `dimensionAtObservation` is accepted and ignored on `/availability`.

## Side Effects

- Structure handlers read the registry (`registry.get`, `registry.get_all`)
  and materialise lazy artefacts. `get_structure` writes a rendered JSON
  response to the consumer's `StructureCache.store`.
- `MessageAnnotator.annotate` and the splices receive `meta_conn` and may run
  DuckDB queries on it.
- Metadata handlers call `MetadataStore.sets_for_structure` or
  `sets_for_target` with `meta_conn`.
- `get_availability` runs DuckDB queries through the registry's own
  connection (`registry.obs`), calls `ComponentFilterShaper.shape` with
  `meta_conn` in its `FilterContext`, and calls `AvailabilityValues.store` on
  an unfiltered scan.
- `get_availability` logs one `loguru` `info` line with the flow, synthesis
  time, count and dimension count when `componentID` is `all` and the route
  scans. A precomputed `AvailabilityValues` hit returns before the line.
- `key_dimension_ids` memoises its result per registry and flow URN.

## Verification

- Run `just test` (unit tests) and `just check` (ruff, docstrings, prose,
  `Any` ratchet, index guard, pyrefly). `just ci` is the full gate.
- Route tests: `tests/test_structure_routes.py`,
  `tests/test_metadata_routes.py`, `tests/test_availability_routes.py`,
  `tests/test_availability_sidecar.py`, `tests/test_availability_values.py`,
  `tests/test_stub_never_materialises.py`, `tests/test_round_trip.py`,
  `tests/test_render_availability.py`,
  `tests/test_annotator_fingerprint.py`, `tests/test_synthetic_bundle.py` and
  `tests/test_library_create_app.py` (router mounting).
- Measure `/availability` and structure-route performance with
  `just bench <bundle-dir>` against a real lake or
  `just bench-synthetic [shape]` (`large` or `census`). The bench files are
  `tests/bench/test_availability_sql.py` (loop versus combined aggregate,
  GH-36) and `tests/bench/test_structure_routes.py`. Building a query root
  costs about 13 to 19 ms on a large census bundle (sdmxlib#426).
