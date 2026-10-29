"""SDMX-REST 2.2.2 `/availability` — the values queryable for one component.

Given a flow and a partial key (any segment may be wildcard), return the
distinct set of values that actually appear for the requested component
under those constraints. Powers next-step picker UIs — "the user picked a
region, now show only the topics that have data for it".

The response is a SDMX StructureMessage carrying a single
`DataConstraint` with one ``CubeRegion`` whose
``KeyValue`` lists the available codes for ``componentID``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Annotated, Any, Literal

import duckdb
import sdmxlib as sl
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.openapi.models import Example
from fastapi.responses import Response
from loguru import logger
from sdmxlib.model.constraint import (
    KeyValue,
)
from sdmxlib.model.dataflow import Dataflow

# Runtime imports, not TYPE_CHECKING: FastAPI resolves the handler's annotations
# at import time to build the dependency graph, and a name it can't evaluate is
# silently demoted to a request-body field (every call then 422s).
from sdmxlib_rest.availability_values import AvailabilityKey, AvailabilityValues, ResolvedAvailability
from sdmxlib_rest.data.params import (
    AGENCY_PATTERN,
    ID_PATTERN,
    VERSION_PATTERN,
    parse_c_params,
)
from sdmxlib_rest.data.params import (
    reject_unsupported_standard_params as _reject_standard_params,
)
from sdmxlib_rest.data.projection import key_dimension_ids
from sdmxlib_rest.filter_shaper import ComponentFilterShaper, FilterContext
from sdmxlib_rest.negotiation import ensure_acceptable
from sdmxlib_rest.responses.availability import JSON_MEDIA as _JSON_MEDIA
from sdmxlib_rest.responses.availability import render_availability
from sdmxlib_rest.runtime import (
    get_availability_values,
    get_filter_shaper,
    get_meta_conn,
    get_registry,
    get_token,
)

# componentID accepts the literal ``all`` plus the SDMX-REST 2.2.2 wildcard
# ``*``; bare ID_PATTERN doesn't include ``*`` so we widen it just for this
# slot. ``*`` is normalised to ``all`` at handler entry.
_COMPONENT_PATTERN = r"^(all|\*|[A-Za-z0-9_@$-]+)$"


def _strip_dim_from_key(key: str, drop_dim_id: str, dsd_key_dims: list[str]) -> str:
    """Replace ``drop_dim_id``'s dotted-key segment with an empty wildcard.

    ``key='all'`` is the universal wildcard — no stripping needed. Otherwise
    we split on ``.`` and replace the segment positioned at the dim's index
    in the DSD's dimension order with an empty string (which sdmxlib's
    `parse_components` treats as the wildcard for that position).
    """
    if key == "all":
        return "all"
    segments = key.split(".")
    # Pad to the DSD's dimension count so we have a slot per dim even if the
    # caller sent a truncated key (e.g. `A5.2021A...`).
    if len(segments) < len(dsd_key_dims):
        segments.extend([""] * (len(dsd_key_dims) - len(segments)))
    try:
        idx = dsd_key_dims.index(drop_dim_id)
    except ValueError:
        # drop_dim_id isn't a key dimension (it's an attribute or a typo).
        # No key segment to modify — the c[] filter strip below still applies.
        return key
    segments[idx] = ""
    return ".".join(segments)


#: SDMX-REST 2.2.2's non-equality ``c[]`` operators, as query-DSL predicates.
#: The string operators are sdmxlib's own (sdmxlib#413), which escape ``%``,
#: ``_`` and ``\\`` in the value themselves. A missing value matches neither
#: ``co`` nor ``nc``, as SQL's ``LIKE`` and ``!=`` read it.
_NON_EQ: dict[str, Callable[[sl.Untyped[Any], str], sl.Pred[Any]]] = {
    "ne": lambda col, v: col != v,
    "lt": lambda col, v: col < v,
    "le": lambda col, v: col <= v,
    "gt": lambda col, v: col > v,
    "ge": lambda col, v: col >= v,
    "co": lambda col, v: col.str.contains(v),
    "nc": lambda col, v: ~col.str.contains(v),
    "sw": lambda col, v: col.str.starts_with(v),
    "ew": lambda col, v: col.str.ends_with(v),
}


def _shaped_filter(
    *,
    key: str,
    eq_filters: dict[str, list[str]],
    dsd_key_dims: list[str],
    shaper: ComponentFilterShaper,
    ctx: FilterContext,
) -> sl.ComponentFilter:
    """Parse the key and equality ``c[]`` filters, then let the consumer's shaper rewrite them."""
    wide_filters: dict[str, list[str] | sl.SqlRelation] = dict(eq_filters)
    return shaper.shape(sl.parse_components(key, wide_filters, dsd_key_dims), ctx)


def _constrains(component_filter: sl.ComponentFilter) -> bool:
    """Whether the filter narrows anything. ``None`` is a wildcard; ``[]`` is not (it matches nothing)."""
    return any(v is not None for v in component_filter.dimensions) or any(
        v is not None for v in component_filter.attributes.values()
    )


def _narrow(
    obs: sl.ObsQuery,
    component_filter: sl.ComponentFilter,
    non_eq_filters: dict[str, list[tuple[str, str]]],
    periods: tuple[str, str | None, str | None],
) -> sl.ObsQuery:
    """Apply every constraint a request carries to the flow's observations.

    Args:
        obs: The flow's unfiltered observations.
        component_filter: The shaped key plus equality ``c[]`` filters.
        non_eq_filters: ``{component_id: [(op, value), ...]}`` for the other operators.
        periods: ``(time_dimension_id, startPeriod, endPeriod)``, the 1.5 backport.

    Returns:
        The narrowed query. A filter naming a component this dataflow has not
        got raises `KeyError` from sdmxlib, listing the ones it has; the caller
        turns that into a 400.
    """
    predicates = [_NON_EQ[op](obs.col(cid), value) for cid, terms in non_eq_filters.items() for op, value in terms]
    time_id, start, end = periods
    if start is not None:
        predicates.append(obs.col(time_id) >= start)
    if end is not None:
        predicates.append(obs.col(time_id) <= end)
    narrowed = obs.filter_components(component_filter)
    return narrowed.filter(*predicates) if predicates else narrowed


def _distinct(query: sl.ObsQuery, component_id: str) -> frozenset[str]:
    """The values ``component_id`` takes under ``query``, missing values dropped."""
    frame = query.select(query.col(component_id)).unique().collect()
    return frozenset(str(v) for v in frame.to_series() if v is not None)


def _observations(registry: sl.BundleRegistry, flow_urn: sl.SdmxUrn[sl.Dataflow]) -> sl.ObsQuery:
    """The flow's observations, or a 500 naming why they cannot be read.

    sdmxlib refuses a series-attributes sidecar with more than one row per join
    key when the query root is built (sdmxlib#412), because joining it would
    multiply every count. That is a producer defect no request can avoid, so it
    is a 500 that names the flow and the table to fix (#8).
    """
    try:
        return registry.obs(flow_urn)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"cannot answer availability for {flow_urn}: {exc}") from exc


def _time_dimension_id(registry: sl.BundleRegistry, flow_urn: sl.SdmxUrn[sl.Dataflow]) -> str:
    """The DSD's time dimension, which ``startPeriod`` / ``endPeriod`` bound."""
    dsd = registry.dsd(flow_urn)
    time_dimension = dsd.time_dimension if dsd is not None else None
    return time_dimension.id if time_dimension is not None else "TIME_PERIOD"


# `all` expands to the artefact-type tokens we can actually source from the bundle.
# `dataproviderscheme` + `valuelist` are accepted at the boundary but produce empty
# collections — the bundle ships none.


def reject_unsupported_standard_params(
    *,
    updatedAfter: str | None,  # noqa: N803
    reportingYearStartDay: str | None,  # noqa: N803
) -> None:
    """Wrap the /data helper for /availability — neither permanent-501 param is supported here."""
    _reject_standard_params(
        includeHistory=None,
        updatedAfter=updatedAfter,
        reportingYearStartDay=reportingYearStartDay,
    )


router = APIRouter(tags=["availability"])

_XML_MEDIA_3 = "application/vnd.sdmx.structure+xml;version=3.0"
_XML_MEDIA_21 = "application/vnd.sdmx.structure+xml;version=2.1"

_ERROR_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"detail": {"type": "string"}},
    "required": ["detail"],
}
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "description": "Malformed key, query parameter, or unknown component.",
        "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
    },
    404: {
        "description": "Dataflow not found / no binding.",
        "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
    },
    406: {
        "description": "Accept header doesn't intersect the route's supported media types.",
        "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
    },
    413: {
        "description": "Payload Too Large — ingress-enforced.",
        "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
    },
    414: {
        "description": "URI Too Long — ingress-enforced (~8 KiB default).",
        "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
    },
    501: {
        "description": (
            "Context not supported (only `dataflow` is implemented), or an unsupported standard param was set."
        ),
        "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
    },
}
_STRUCTURE_200_CONTENT: dict[int | str, dict[str, Any]] = {
    200: {
        "description": (
            "SDMX structure message carrying a single DataConstraint whose one CubeRegion "
            "enumerates the available codes for `componentID`."
        ),
        "content": {_JSON_MEDIA: {}, _XML_MEDIA_21: {}, _XML_MEDIA_3: {}},
    },
}


@router.get(
    "/availability/{context}/{agencyID}/{resourceID}/{version}/{key}/{componentID}",
    summary="Distinct values for one component under a partial key",
    description=(
        "Given a flow and a partial SDMX dotted key, return the set of values that actually "
        "appear for `componentID` under those constraints. Useful for building next-step "
        "picker UIs.\n\n"
        "The response is a SDMX-JSON / SDMX-ML StructureMessage carrying one "
        "`DataConstraint` with one `CubeRegion` whose single `KeyValue` lists the "
        "available codes. `constraint_type=Actual` (the values exist in the data, not "
        "merely the allowed set from the codelist).\n\n"
        "Only `context=dataflow` is implemented (others 501)."
    ),
    responses={**_STRUCTURE_200_CONTENT, **_ERROR_RESPONSES},
)
async def get_availability(  # noqa: C901, PLR0912, PLR0915
    request: Request,
    registry: Annotated[sl.BundleRegistry, Depends(get_registry)],
    conn: Annotated[duckdb.DuckDBPyConnection, Depends(get_meta_conn)],
    token: Annotated[str, Depends(get_token)],
    availability_values: Annotated[AvailabilityValues, Depends(get_availability_values)],
    filter_shaper: Annotated[ComponentFilterShaper, Depends(get_filter_shaper)],
    context: Annotated[
        str,
        Path(
            description="Structural context. Only `dataflow` is implemented; others return 501.",
            openapi_examples={"dataflow": Example(summary="Query via dataflow", value="dataflow")},
        ),
    ],
    agencyID: Annotated[  # noqa: N803
        str,
        Path(
            description="Maintainer agency ID.",
            pattern=AGENCY_PATTERN,
            openapi_examples={"agency": Example(summary="Maintainer agency", value="AGENCY")},
        ),
    ],
    resourceID: Annotated[  # noqa: N803
        str,
        Path(
            description="Dataflow ID.",
            pattern=ID_PATTERN,
            openapi_examples={"flow": Example(summary="A dataflow in this catalog", value="DF_EXAMPLE")},
        ),
    ],
    version: Annotated[
        str,
        Path(
            description="Dataflow version.",
            pattern=VERSION_PATTERN,
            openapi_examples={"v2": Example(summary="v2.0", value="2.0")},
        ),
    ],
    key: Annotated[
        str,
        Path(
            description=(
                "SDMX dotted key (empty segments wildcard, `+` is OR within a dimension, "
                "`all` is the whole-wildcard shorthand)."
            ),
            openapi_examples={
                "partial": Example(
                    summary="One dimension pinned, the rest wildcarded — availability for the "
                    "requested component is computed under that constraint.",
                    value="A..CODE_1+CODE_2..",
                ),
                "wildcard": Example(summary="Everything — components present in the dataflow", value="all"),
            },
        ),
    ],
    componentID: Annotated[  # noqa: N803
        str,
        Path(
            description=(
                "The component (key dimension or attribute) whose available values you want. "
                "`all` or `*` (SDMX-REST 2.2.2 wildcard) enumerates every key dimension at once."
            ),
            pattern=_COMPONENT_PATTERN,
            openapi_examples={
                "dimension": Example(summary="A key dimension", value="FREQ"),
                "all": Example(summary="Every key dimension at once", value="all"),
                "asterisk": Example(summary="Spec wildcard — every dimension at once", value="*"),
            },
        ),
    ],
    mode: Annotated[
        Literal["exact", "available"],
        Query(
            description=(
                "SDMX-REST 2.2.2 `/availability` `mode`. `exact` (default) enumerates the "
                "distinct values present under the supplied key + `c[]` constraints. "
                "`available` enumerates the values that *would* leave the cube non-empty if "
                "added to the key — i.e. per-component, the constraints on THAT component "
                "are dropped before counting. Equivalent for components that have no "
                "constraint in the key."
            ),
        ),
    ] = "exact",
    references: Annotated[
        str,
        Query(
            description=(
                "SDMX-REST 2.2.2 `acreferences` — comma-separated list of artefact types to "
                "include alongside the synthesised constraint. Honoured values: "
                "`none` (default), `all`, `datastructure`, `dataflow`, `codelist`, "
                "`conceptscheme`. `dataproviderscheme` / `valuelist` are accepted but yield "
                "empty collections (this bundle has none). For non-`none` values we walk the "
                "dataflow's DSD and stub each referenced artefact — the constraint stays "
                "the canonical payload; the extras let clients render dimension labels + "
                "concept names without a second round-trip."
            ),
        ),
    ] = "none",
    start_period: Annotated[
        str | None,
        Query(
            alias="startPeriod",
            description=(
                "SDMX-REST 1.5 inclusive lower bound on TIME_PERIOD. 1.5 backport on the "
                "2.2.2 surface; strict 2.2.2 clients should use "
                "`c[TIME_PERIOD]=ge:<value>` instead."
            ),
        ),
    ] = None,
    end_period: Annotated[
        str | None,
        Query(
            alias="endPeriod",
            description=(
                "SDMX-REST 1.5 inclusive upper bound on TIME_PERIOD. 1.5 backport — see "
                "`startPeriod` for the 2.2.2 canonical form."
            ),
        ),
    ] = None,
    updatedAfter: Annotated[  # noqa: N803
        str | None,
        Query(
            description=(
                "SDMX-REST: return only values whose data changed since this timestamp. "
                "Permanently not implemented — our bundle is static; presence returns `501`."
            ),
        ),
    ] = None,
    reportingYearStartDay: Annotated[  # noqa: N803
        str | None,
        Query(
            description=(
                "SDMX-REST: explicit reporting-year start day for fiscal-period queries. "
                "Permanently not implemented — presence returns `501`."
            ),
        ),
    ] = None,
    dimensionAtObservation: Annotated[  # noqa: N803, ARG001
        str | None,
        Query(
            description=(
                "Per SDMX-REST 2.2.2 this controls observation packing for data messages. "
                "Availability returns a structural ContentConstraint (`DataConstraint`/`CubeRegion`), "
                "not observations — so the parameter has no effect here. Accepted as a no-op for "
                "client compatibility (.Stat appends it unconditionally on availability lookups)."
            ),
        ),
    ] = None,
    accept: Annotated[str | None, Header()] = None,
    accept_language: Annotated[str | None, Header(alias="Accept-Language")] = None,
) -> Response:
    """Resolve the available-values DataConstraint and serialise it."""
    # Normalise SDMX-REST 2.2.2 wildcard `*` to our internal `all` sentinel so the
    # `componentID == "all"` dispatch lower down doesn't need to know about both.
    if componentID == "*":
        componentID = "all"  # noqa: N806
    ensure_acceptable(accept, [_JSON_MEDIA, _XML_MEDIA_21, _XML_MEDIA_3])
    if context != "dataflow":
        raise HTTPException(
            status_code=501,
            detail=f"context {context!r} not implemented (only `dataflow` is supported)",
        )

    reject_unsupported_standard_params(
        updatedAfter=updatedAfter,
        reportingYearStartDay=reportingYearStartDay,
    )

    flow_urn = Dataflow.urn_for(agency=agencyID, id=resourceID, version=version)
    binding = registry.binding_for(flow_urn)
    if binding is None:
        raise HTTPException(status_code=404, detail=f"no binding for {flow_urn}")

        # Resolve componentID → list of (component, column) to enumerate. The
        # SDMX-REST `componentID=all` form (and the 1.5 no-componentID form,
        # routed to "all") asks for availability across every key dimension —
        # used by .Stat / DLM to enumerate cube cardinality. Singular component
        # lookup falls through to the existing single-column path.
    components_to_query: list[tuple[str, str]]
    if componentID == "all":
        components_to_query = list(binding.dimension_column_map.items())
    else:
        column = binding.dimension_column_map.get(componentID) or binding.attribute_column_map.get(componentID)
        if column is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown component {componentID!r} on flow {flow_urn} — "
                    f"valid dimensions: {sorted(binding.dimension_column_map)}; "
                    f"valid attributes: {sorted(binding.attribute_column_map)}; "
                    "or use `all` for every key dimension."
                ),
            )
        components_to_query = [(componentID, column)]

        # Parse the dotted key + bracketed filters, same shape as /data.
    dsd_key_dims = key_dimension_ids(registry, flow_urn, binding)
    items = list(request.query_params.multi_items())
    eq_filters, non_eq_filters = parse_c_params(items)
    filter_ctx = FilterContext(
        registry=registry,
        conn=conn,
        flow_urn=flow_urn,
        binding=binding,
        query_items=tuple(items),
    )
    try:
        component_filter = _shaped_filter(
            key=key, eq_filters=eq_filters, dsd_key_dims=dsd_key_dims, shaper=filter_shaper, ctx=filter_ctx
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 1.5 backport: startPeriod / endPeriod were SDMX-REST 1.5's mechanism for
    # inclusive TIME_PERIOD bounds. 2.2.2 dropped them; the canonical
    # equivalent is `c[TIME_PERIOD]=ge:<v>+le:<v>`. We accept both forms on
    # /availability to match /data — strict 2.2.2 clients should
    # prefer the c[] form.
    # Resolved only when a bound is given: a DSD read is ~15 ms on a large census bundle
    # and most requests carry no period bound.
    has_bounds = start_period is not None or end_period is not None
    periods = (_time_dimension_id(registry, flow_urn) if has_bounds else "", start_period, end_period)

    # Precomputed values, for the one case precompute can cover: no filter at
    # all. Judged on the filter the shaper produced, not on the URL — a shaper
    # may add a constraint the request never named, and an unfiltered answer
    # would then be wrong for it. See `availability_values` for the full case.
    values_key = AvailabilityKey(token=token, flow_urn=flow_urn, component_id=componentID)
    unfiltered = not (_constrains(component_filter) or non_eq_filters or has_bounds)
    if unfiltered and (resolved := availability_values.lookup(values_key)) is not None:
        return render_availability(
            registry=registry,
            flow_urn=flow_urn,
            component_id=componentID,
            key_values=resolved.key_values,
            obs_count=resolved.obs_count,
            accept=accept,
            accept_language=accept_language,
            references=references,
        )

    obs = _observations(registry, flow_urn)
    try:
        narrowed = _narrow(obs, component_filter, non_eq_filters, periods)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc.args[0] if exc.args else exc)) from exc

    # componentID=all can compute every per-dimension DISTINCT plus the total
    # COUNT as one aggregate in a single scan (`agg` + `sl.len()`, sdmxlib#414),
    # and on DuckDB 2 that beats the per-dimension loop filtered or not.
    #
    # Unfiltered, measured on a ~500M-row, 6-dimension census bundle, warm,
    # loop vs combined, best-of-2 (tests/bench/test_availability_sql.py, GH-36):
    #
    #   DuckDB 1.5.6, macOS           4.54 s vs 12.56 s   combined loses 2.8x
    #   DuckDB 2.0.0, macOS                               combined wins  1.2-1.3x
    #   DuckDB 2.0.0, Linux           3.22 s vs  2.72 s   combined wins  1.2x
    #
    # Filtered, every DISTINCT pays a scan of its own and one scan beats six;
    # measured on DuckDB 1.5:
    #
    #   c[SEX]=M        (1/3 of rows) 9.70 s vs  8.98 s   combined wins  1.1x
    #   c[GEO_LEVEL]=Country          0.08 s vs  0.02 s   combined wins  4.8x
    #   c[REF_AREA]=<one code>        0.11 s vs  0.04 s   combined wins  2.4x
    #
    # The unfiltered margin is thin and depends on row count: on a synthetic
    # bundle with the same 580k-code geography and 20M rows the loop usually
    # wins, by 1.2-2x, because building the per-dimension arrays outweighs the
    # scans saved. Both take under half a second there.
    # Re-measure on a large bundle before reverting.
    #
    # ``mode=available`` relaxes one constraint per per-dimension query, so each
    # aggregate would need its own WHERE and cannot share the combined scan —
    # and measured cold and warm, the loop wins there anyway (#6).
    #
    # componentID=<specific> stays on the single-query path: one DISTINCT is
    # not a loop to collapse.
    component_ids = [comp_id for comp_id, _ in components_to_query]
    fast_path_eligible = componentID == "all" and mode == "exact"
    profile = componentID == "all"
    key_values: list[KeyValue] = []
    t0 = time.perf_counter()

    try:
        if mode == "available":
            # mode=available: enumerate values that would still leave the cube
            # non-empty if added to the key. Equivalent to mode=exact with the
            # constraint on the enumerated component dropped (key segment +
            # c[] filter on that component). For dimensions that have no
            # constraint in the key, the two modes return identical results
            # — the strip is a no-op.
            for comp_id in component_ids:
                relaxed_filter = _shaped_filter(
                    key=_strip_dim_from_key(key, comp_id, dsd_key_dims),
                    eq_filters={k: v for k, v in eq_filters.items() if k != comp_id},
                    dsd_key_dims=dsd_key_dims,
                    shaper=filter_shaper,
                    ctx=filter_ctx,
                )
                # startPeriod / endPeriod bound the time dimension, so only its
                # own query drops them; every other component keeps them.
                relaxed = _narrow(
                    obs,
                    relaxed_filter,
                    {k: v for k, v in non_eq_filters.items() if k != comp_id},
                    (periods[0], None, None) if comp_id == periods[0] else periods,
                )
                key_values.append(KeyValue(dimension_id=comp_id, values=_distinct(relaxed, comp_id)))
            # obs_count for mode=available reports the cube cardinality under
            # the FULL filter (all constraints applied) — the same number .Stat
            # / DLM show in the "(N observations)" badge. The per-component
            # value lists are wider; the badge count covers the user's actual
            # query.
            obs_count_value = narrowed.count()
        elif fast_path_eligible:
            aliases = {f"c{i}": comp_id for i, comp_id in enumerate(component_ids)}
            row = (
                narrowed.agg(**{alias: obs.col(comp_id).unique() for alias, comp_id in aliases.items()}, n=sl.len())
                .collect()
                .row(0, named=True)
            )
            for alias, comp_id in aliases.items():
                values = frozenset(str(v) for v in row[alias] or () if v is not None)
                key_values.append(KeyValue(dimension_id=comp_id, values=values))
            obs_count_value = int(row["n"] or 0)
        else:
            key_values.extend(
                KeyValue(dimension_id=comp_id, values=_distinct(narrowed, comp_id)) for comp_id in component_ids
            )
            obs_count_value = narrowed.count()
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc.args[0] if exc.args else exc)) from exc

    synthesis_dt = time.perf_counter() - t0

    # .Stat DLM reads the obs count off an `obs_count` annotation on the
    # constraint (id="obs_count", type="sdmx_metrics", title="<count>"). The
    # shape comes from the OECD .NET writer (JsonAvailableConstraintWriterBase
    # .WriteObsCountAnnotations).
    if profile:
        logger.info(
            "availability synthesis flow={flow} total={total:.2f}s count={count} dims={ndims} (fast-path)",
            flow=str(flow_urn),
            total=synthesis_dt,
            count=obs_count_value,
            ndims=len(components_to_query),
        )

    if unfiltered:
        # Offered whatever the outcome, an empty cube included: a zero count is
        # as expensive to derive as a full answer, and renders as the 204.
        availability_values.store(
            values_key, ResolvedAvailability(key_values=tuple(key_values), obs_count=obs_count_value)
        )
    return render_availability(
        registry=registry,
        flow_urn=flow_urn,
        component_id=componentID,
        key_values=key_values,
        obs_count=obs_count_value,
        accept=accept,
        accept_language=accept_language,
        references=references,
    )
