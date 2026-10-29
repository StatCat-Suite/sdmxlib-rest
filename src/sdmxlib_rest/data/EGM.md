# sdmxlib_rest.data

## Overview

`sdmxlib_rest.data` holds the reusable parts of an SDMX-REST `/data` handler.
The library serves no `/data` route; a consumer writes its own handler and
calls these helpers. They parse query parameters, build SQL fragments, resolve
`attributes=`/`measures=` projection, build `Range` paging headers and encode
Arrow batches as SDMX-CSV, zip or Parquet. No helper carries catalog-specific
logic. The helpers that map SDMX component ids to physical columns take an
`sdmxlib.DataflowBinding`. These take none: `parse_bracketed`,
`parse_c_params`, `like_pattern`, `sql_ident` and
`reject_unsupported_standard_params` (`params.py`), `resolve_projection_set`
(`projection.py`), `qualify_is_active` (`sql.py`), everything in
`paging.py`, and `prepend_utf8_bom`, `stream_csv_zip` and `encode_parquet`
(`encoding.py`). The library's own
`/availability` route (`routes/availability.py`) imports the id patterns,
`parse_c_params`, `reject_unsupported_standard_params` and
`key_dimension_ids` from here.

## Interface

- `AGENCY_PATTERN`, `ID_PATTERN` and `VERSION_PATTERN` (`params.py`): SDMX
  identifier regexes from `SDMXCommonReferences.xsd`, applied to FastAPI
  `Path()` parameters so a malformed reference fails 422 before it reaches
  sdmxlib's URN parser.
- `C_PARAM`, `parse_bracketed` and `parse_c_params` (`params.py`): parse
  bracketed parameters such as `c[<id>]=<expr>` from
  `request.query_params.multi_items()`. `parse_bracketed` merges repeated keys
  and `+`-joined values into one list per id. `parse_c_params` splits each
  `+`-joined term on an optional `<op>:` prefix and returns
  `(eq_values, non_eq_terms)`.
- `C_OPERATORS`, `NON_EQ_OPERATOR_SQL`, `LIKE_OPS`, `like_pattern` and
  `build_non_eq_where` (`params.py`): the ten `c[...]` operators, one SQL
  template per non-`eq` operator, and the renderer that turns
  `non_eq_terms` into `(fragments, params)` against the binding's dimension
  and attribute column maps.
- `sql_ident` (`params.py`): double-quotes a SQL identifier.
- `parse_sort` (`params.py`): parses `sort=` into
  `[(physical_column, "ASC" | "DESC"), ...]`. Accepts component ids, `*`
  (every key dimension in binding order) and `series_key` (every key
  dimension except `TIME_PERIOD`).
- `validate_dimension_at_observation` (`params.py`): accepts `None`,
  `AllDimensions` or a key dimension id in the binding.
- `reject_unsupported_standard_params` (`params.py`): raises 501 when any of
  `includeHistory`, `updatedAfter` or `reportingYearStartDay` is set.
- `KEEP_ALL_ATTRIBUTE_KEYWORDS`, `KEEP_ALL_MEASURE_KEYWORDS` and
  `DROP_ALL_ATTRIBUTE_KEYWORDS` (`params.py`): projection keywords.
  `attributes=` keeps everything on `all`, `dsd`, `dataset`, `series`, `obs`
  or `observations`, and drops everything on `msd`; `measures=` keeps
  everything on `all`.
- `resolve_projection_set`, `project_binding` and `project_arrow_batches`
  (`projection.py`): resolve `attributes=`/`measures=` into a set of ids to
  keep, return a binding whose attribute and measure column maps are trimmed
  with `attrs.evolve`, and select the same columns from each Arrow batch for
  the Parquet path.
- `key_dimension_ids` (`projection.py`): the flow's key-dimension ids in the
  order a dotted key is read in.
- `materialise_dsd_for_flow` (`projection.py`): resolves a flow URN to its
  materialised `sl.DataStructure`, or `None`.
- `label_maps_for_dims` (`projection.py`): `{column_name: {code_id: label}}`
  for each key dimension whose codelist the registry resolves, built from
  `CodelistLike.code_map(lang=...)`.
- `decimals_format_replace_clause(binding, *, targets, decimals_id="DECIMALS")`
  (`sql.py`): `REPLACE` arguments that wrap each component id in `targets` in
  `printf('%.*f', CAST(<decimals column> AS INTEGER), <col>)`, where the
  decimals column is the attribute `decimals_id` maps to. Targets are looked
  up in the measure map, then the attribute map.
- `qualify_is_active` (`sql.py`): whether a `firstNObservations` /
  `lastNObservations` cap needs a `QUALIFY row_number()` clause.
- `RANGE_RE`, `parse_items_range`, `build_range_headers`, `with_range` and
  `range_status` (`paging.py`): parse `Range: items=<start>-<end>` or
  `Range: values=<start>-<end>` into `(start, end, unit)`, and build the
  `Content-Range`, `Accept-Ranges` and optional `X-Sort-Skipped: true`
  headers and the 206/200 status.
- `emit_csv_with_labels` (`encoding.py`): SDMX-CSV with columns `STRUCTURE`,
  `STRUCTURE_ID`, `ACTION`, then dimensions (each followed by `<col>_label`
  when `label_maps` has it), measures and attributes.
- `prepend_utf8_bom`, `stream_csv_zip` and `encode_parquet` (`encoding.py`):
  prefix a UTF-8 BOM, wrap CSV chunks in a single-entry streaming zip
  (`zipstream-ng`, DEFLATE level 1), and stream Parquet (zstd,
  `write_statistics=False`, `use_dictionary=False`).

`__init__.py` re-exports all of the above in `__all__`.

## Invariants

- Request values never enter SQL text. `build_non_eq_where` emits `?`
  placeholders and returns the values in `params`. For `co`, `nc`, `sw` and
  `ew`, `like_pattern` escapes `\`, `%` and `_`, and the templates declare
  `ESCAPE '\'`.
- Every identifier interpolated into SQL passes through `sql_ident`, which
  raises `ValueError` unless the name is alphanumeric after removing `_`
  (`build_non_eq_where`, `decimals_format_replace_clause`). `parse_sort`
  returns unquoted physical column names; the caller quotes them.
- A component id the binding does not declare raises `HTTPException(400)`
  naming the valid ids: in `build_non_eq_where`, `parse_sort`,
  `validate_dimension_at_observation` and `resolve_projection_set`.
- In `parse_c_params`, a term whose prefix before `:` is not in
  `C_OPERATORS` is an `eq` value for the whole term, so values containing `:`
  (timestamps) survive.
- `key_dimension_ids` reads the slot order from
  `dsd.key_slot_ids("3.0")`, the SDMX-REST 2.2.2 grammar in which the time
  dimension is a key slot. No helper in this package takes an API version.
  When the flow's DSD is missing or the registry returns a lazy,
  unmaterialised DSD, it falls back to the order of
  `binding.dimension_column_map`; it never forces materialisation.
- `key_dimension_ids` memoises per registry and flow URN in
  `_KEY_DIM_IDS_CACHE`, a `weakref.WeakKeyDictionary` keyed on the registry,
  so a reopened bundle starts with an empty cache.
- `project_binding` never trims `dimension_column_map`, and returns the input
  binding unchanged when both parameters resolve to keep-all.
- `emit_csv_with_labels` writes the header line exactly once, also for an
  empty batch iterable, and raises `KeyError` when a batch lacks a column the
  binding declares. A code missing from a label map yields an empty label.
- The encoders are generators that consume one batch or chunk at a time;
  memory is bounded by batch size. `encode_parquet` yields nothing for an
  empty batch iterable, since the writer is created on the first batch.
- `decimals_format_replace_clause` returns `""` when the binding has no
  `decimals_id` attribute or no target resolves, and a plain `SELECT *` is then
  correct.
- `qualify_is_active` returns `True` when `n_cap <= 0` or the cardinality is
  0 (unknown), and otherwise `cardinality > n_cap`.
- `parse_items_range` returns `None` for an absent header and for any header
  `RANGE_RE` does not match: a unit other than `items` or `values`, an open
  range such as `items=0-`, a suffix range, or several ranges. It does not
  validate bounds.

## Side Effects

- No module opens files, sockets or database connections. The SQL helpers
  return strings and parameter lists; the caller runs the query.
- `key_dimension_ids`, `materialise_dsd_for_flow` and `label_maps_for_dims`
  read through `registry.get`. `materialise_dsd_for_flow` calls the DSD's
  `materialize()` when it has one, and `label_maps_for_dims` calls
  `code_map`. sdmxlib's `LazyCodelist.code_map` memoises per instance and
  language; the eager `Codelist.code_map` rebuilds the dict on each call.
- `key_dimension_ids` writes to the module-level `_KEY_DIM_IDS_CACHE`.
- `emit_csv_with_labels`, `stream_csv_zip` and `encode_parquet` encode into
  in-memory buffers (`io.BytesIO`, `zipstream.ZipStream`) and yield the bytes.
- `params.py` and `projection.py` raise `fastapi.HTTPException` (400, or 501
  from `reject_unsupported_standard_params`).

## Verification

- Run `just test` (unit tests) and `just check` (ruff, docstrings, prose,
  `Any` ratchet, index guard, pyrefly). `just ci` is the full gate.
- `tests/test_bracketed_params.py` covers `C_PARAM` and `parse_bracketed`.
  `tests/test_key_slots.py` covers `key_dimension_ids` slot order.
  `tests/test_availability_routes.py` exercises `parse_c_params`, the id
  patterns and `key_dimension_ids` through `/availability`.
- `just bench <bundle-dir>` and `just bench-synthetic` measure `/availability`
  query cost (`tests/bench/`), which calls `key_dimension_ids` per request.
  No benchmark covers the encoders.
