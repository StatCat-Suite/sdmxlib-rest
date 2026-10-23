"""Generic building blocks for an SDMX-REST data plane.

The library owns the reusable middle of a ``/data`` handler — query-parameter
parsing, WHERE construction, projection resolution, paging headers and the
arrow→CSV/parquet streaming path. The consumer owns the route, because
orchestration is where catalog-specific caching, partition pushdown and vendor
extensions live, and every catalog shapes those differently. A consumer writes
its own handler and calls into these.
"""

from __future__ import annotations

from sdmxlib_rest.data.encoding import (
    emit_csv_with_labels,
    encode_parquet,
    prepend_utf8_bom,
    stream_csv_zip,
)
from sdmxlib_rest.data.paging import (
    RANGE_RE,
    build_range_headers,
    parse_items_range,
    range_status,
    with_range,
)
from sdmxlib_rest.data.params import (
    AGENCY_PATTERN,
    C_OPERATORS,
    C_PARAM,
    DROP_ALL_ATTRIBUTE_KEYWORDS,
    ID_PATTERN,
    KEEP_ALL_ATTRIBUTE_KEYWORDS,
    KEEP_ALL_MEASURE_KEYWORDS,
    LIKE_OPS,
    NON_EQ_OPERATOR_SQL,
    VERSION_PATTERN,
    build_non_eq_where,
    like_pattern,
    parse_bracketed,
    parse_c_params,
    parse_sort,
    reject_unsupported_standard_params,
    sql_ident,
    validate_dimension_at_observation,
)
from sdmxlib_rest.data.projection import (
    key_dimension_ids,
    label_maps_for_dims,
    materialise_dsd_for_flow,
    project_arrow_batches,
    project_binding,
    resolve_projection_set,
)
from sdmxlib_rest.data.sql import decimals_format_replace_clause, qualify_is_active

__all__ = [
    "AGENCY_PATTERN",
    "C_OPERATORS",
    "C_PARAM",
    "DROP_ALL_ATTRIBUTE_KEYWORDS",
    "ID_PATTERN",
    "KEEP_ALL_ATTRIBUTE_KEYWORDS",
    "KEEP_ALL_MEASURE_KEYWORDS",
    "LIKE_OPS",
    "NON_EQ_OPERATOR_SQL",
    "RANGE_RE",
    "VERSION_PATTERN",
    "build_non_eq_where",
    "build_range_headers",
    "decimals_format_replace_clause",
    "emit_csv_with_labels",
    "encode_parquet",
    "key_dimension_ids",
    "label_maps_for_dims",
    "like_pattern",
    "materialise_dsd_for_flow",
    "parse_bracketed",
    "parse_c_params",
    "parse_items_range",
    "parse_sort",
    "prepend_utf8_bom",
    "project_arrow_batches",
    "project_binding",
    "qualify_is_active",
    "range_status",
    "reject_unsupported_standard_params",
    "resolve_projection_set",
    "sql_ident",
    "stream_csv_zip",
    "validate_dimension_at_observation",
    "with_range",
]
