# sdmxlib_rest.responses

## Overview

`sdmxlib_rest.responses` holds the response shaping that every SDMX-REST
catalog needs and that no single route owns. It answers a non-null `asOf` with
501, turns an empty SDMX-JSON 200 into a 204, answers every sdmxlib
`ValidationError` with a 500 that names each artefact and rule, and renders an
`/availability` response from per-component value lists and an observation
count. The package holds no catalog-specific logic; the routes in
`sdmxlib_rest.routes` and `create_app` in `app.py` call it.

## Interface

- `AsOf` and `reject_asof` (`asof.py`): `AsOf` is an `Annotated[str | None,
  Query(alias="asOf", ...)]` parameter type that declares `asOf` in the
  OpenAPI schema with one shared description. `reject_asof(value)` raises
  `HTTPException(501)` when `value` is non-null and does nothing otherwise.
  `get_structure` and `get_structure_item` (`routes/structure.py`) and
  `get_metadataset`, `get_metadataflow_sets` and
  `get_structure_attached_sets` (`routes/metadata.py`) declare
  `asof: AsOf = None` and call `reject_asof(asof)` first.
- `to_204_if_empty` (`empty.py`): takes a `Response` and returns a
  `Response(status_code=204)` when the input is a 200 with a JSON media type,
  a non-empty `bytes` body that parses as JSON, and a `data` object in which
  no key of `_DATA_COLLECTION_KEYS` holds a non-empty list. It returns the
  input unchanged in every other case. `routes/structure.py` applies it to
  the `/structure/{structureType}/...` response after the splices run and
  before `StructureCache.store`, and to the JSON branch of
  `/structure/{itemSchemeType}/.../{itemID}`. `routes/metadata.py:_serialise`
  applies it to the JSON branch of all three `/metadata` routes.
- `unwritable_content_response` (`unwritable.py`): an async exception handler
  `(request, exc: ValidationError) -> JSONResponse`.
  `app.py:_install_unwritable_handler` registers it for
  `sdmxlib.model.errors.ValidationError` through
  `app.add_exception_handler`, on the app and on every mounted sub-app
  (`create_app`), since a mounted FastAPI app answers its own unhandled
  exceptions. sdmxlib raises `ValidationError` when a writer finds an issue
  and also when an artefact breaks a rule as it is built, for example in the
  `DataConstraint(...)` call in `render_availability`. Both get the 500, and
  the body calls the content unwritable in the requested format in both
  cases.
- `render_availability` and `JSON_MEDIA` (`availability.py`):
  `render_availability(*, registry, flow_urn, component_id, key_values,
  obs_count, accept, accept_language, references="none") -> Response` builds a
  `DataConstraint` from the value lists and serialises it in a
  `StructureMessage`. `JSON_MEDIA` is
  `application/vnd.sdmx.structure+json;version=2.0.0`. `routes/availability.py`
  calls `render_availability` twice in its `/availability/{context}/...` route,
  once with values from `AvailabilityValues.lookup` and once with scanned
  values, and imports `JSON_MEDIA` for its OpenAPI content map and
  `ensure_acceptable` list. A consumer with precomputed value lists can call
  it directly (`availability_values.py` points there).

## Invariants

- `asOf` is never served. Any non-null value on a route that declares `AsOf`
  answers 501 with a fixed `detail` (`reject_asof`). The `/availability` route
  does not declare `AsOf`, so `reject_unknown_query_params` answers `asOf`
  there with its unknown-parameter 400.
- `to_204_if_empty` is idempotent and changes only a JSON 200. A non-200
  status, a non-JSON media type, a non-`bytes` body (such as a stream), an
  empty body or a body that fails `json.loads` passes through unchanged. XML
  responses therefore keep their 200. A body that parses to a non-object (such
  as `[]`), or whose `data` is truthy and not an object, raises
  `AttributeError` (GH-72).
- A collection key absent from `_DATA_COLLECTION_KEYS` never makes a response
  count as non-empty. A message whose only content sits under such a key
  becomes a 204.
- The 204 from `to_204_if_empty` keeps the original headers except
  `content-length` and `etag`, which go stale with the dropped body.
- `unwritable_content_response` always answers 500. Its JSON body carries
  `detail` with the issue count, `target` (the request path plus `?query`
  when a query string is present) and `issues`, one `{"path", "message"}`
  per entry in `exc.issues`. `tests/test_unwritable_content.py` checks that
  the 500 carries no `ETag` and no `Cache-Control`.
- `render_availability` answers 204 with no body whenever `obs_count == 0`,
  for every `Accept`, XML included.
- `render_availability` parses `references` and resolves the related
  artefacts before the `obs_count` check. An unknown `references` token
  raises `HTTPException(400)` listing the valid tokens, even for an empty
  cube. Valid tokens are `none`, `all`, `datastructure`, `dataflow`,
  `codelist`, `conceptscheme`, `dataproviderscheme` and `valuelist`; `all`
  expands to `datastructure`, `dataflow`, `codelist` and `conceptscheme`.
  `dataproviderscheme` and `valuelist` are accepted and add nothing.
- The constraint id is `AVAIL_<component_id>`, its maintainer is
  `flow_urn.agency`, its version is `1.0`, its type is
  `ConstraintType.ACTUAL`, and its name is
  `Availability for <flow id> (<component_id>)`. `key_values` enter one
  `CubeRegion(include=True)` in the order given.
- An `Accept` containing `xml` that `is_browser_request` does not flag gets
  SDMX-ML, version 3.0 when `Accept` contains `3.0` and 2.1 otherwise. Every
  other request gets JSON through `localized_json_response` with
  `JSON_MEDIA`.
- The JSON body of every `dataConstraints` entry carries exactly one
  annotation `{"id": "obs_count", "title": "<obs_count>", "type":
  "sdmx_metrics"}` and `"type": "Actual"`
  (`_inject_constraint_annotations`). `name` and `names` fall back to
  `Autogenerated content constraint` only when the writer omitted them
  (`setdefault`); `tests/test_render_availability.py` pins the derived name.
- Related artefacts added by `references=` are stubs built with
  `routes/structure.py:_stub_of`. Codelists are deduplicated by URN; an
  artefact `registry.get` cannot find is skipped. A flow with no structure
  adds nothing.

## Side Effects

- `reject_asof` raises `HTTPException`. `_parse_acreferences` raises
  `HTTPException(400)`.
- `unwritable_content_response` logs one `logger.error` line through loguru
  naming the target, the issue count and the issue paths.
- `render_availability` reads the registry (`registry.get`, and
  `materialize()` on a lazy DSD) when `references` names any artefact type.
  It reads nothing when `references` is `none` or empty.
- `availability.py` imports `_stub_of` and `_concept_scheme_urns_for_dsd`
  from `routes/structure.py` inside functions, to break the import cycle
  between the two modules.
- `to_204_if_empty` and `_inject_constraint_annotations` parse the serialised
  JSON body once each with `json.loads`.

## Verification

- Run `just test` (unit tests) and `just check` (ruff, docstrings, prose,
  `Any` ratchet, index guard, pyrefly). `just ci` is the full gate.
- `tests/test_render_availability.py` covers `render_availability`: the
  constraint, the `obs_count` annotation, `type` and `name`, the 204 on an
  empty cube, XML and browser negotiation, and the `AVAIL_<component_id>` id.
- `tests/test_availability_values.py` covers an empty precomputed answer
  rendering as a 204.
- `tests/test_metadata_routes.py` covers the 204 for a metadataflow with no
  sets.
- `tests/test_unwritable_content.py` covers the 500 body, the target, the
  missing `ETag` and `Cache-Control`, the handler on a mounted app, and
  writable content passing through.
