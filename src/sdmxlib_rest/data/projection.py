"""Column projection for the data plane — SDMX-REST ``attributes=`` / ``measures=``.

Projection is applied by trimming the binding's column maps before they reach
the serialiser: the underlying query still ``SELECT *``s, and the writer emits
only the columns the trimmed binding lists. The arrow/parquet path needs the
projection applied to the batches themselves, which :func:`project_arrow_batches`
does.
"""

from __future__ import annotations

import weakref
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING

import sdmxlib as sl
from attrs import evolve
from fastapi import HTTPException

from sdmxlib_rest.data.params import (
    DROP_ALL_ATTRIBUTE_KEYWORDS,
    KEEP_ALL_ATTRIBUTE_KEYWORDS,
    KEEP_ALL_MEASURE_KEYWORDS,
)

if TYPE_CHECKING:
    import pyarrow as pa


# registry -> flow URN -> key-dimension ids, in the order dotted keys are parsed
# against. Per registry, not per process: a runtime can be closed and reopened
# on a new build in the same process (a consumer's catalog may be), and a flow URN alone
# would then keep answering with the previous build's dimension order. Weak, so
# the entries go with the registry they describe.
_KEY_DIM_IDS_CACHE: weakref.WeakKeyDictionary[sl.LocalRegistry, dict[str, list[str]]] = weakref.WeakKeyDictionary()


def resolve_projection_set(
    available: dict[str, str],
    raw: str | None,
    *,
    keep_all_keywords: frozenset[str],
    drop_all_keywords: frozenset[str],
    kind: str,
) -> set[str] | None:
    """Parse a SDMX-REST 2.2.2 projection param.

    Returns ``None`` (no filter — keep every column) when the param is missing
    or matches a "keep all" keyword, the empty set on ``none`` or any
    ``drop_all_keywords`` token, or the set of IDs to retain on a
    comma-separated list. Raises ``HTTPException(400)`` if any listed ID
    isn't in ``available``.
    """
    if raw is None:
        return None
    lowered = raw.strip().lower()
    if not lowered:
        return None
    if lowered in keep_all_keywords:
        return None
    if lowered == "none" or lowered in drop_all_keywords:
        return set()
    requested = [tok.strip() for tok in raw.split(",") if tok.strip()]
    unknown = [tok for tok in requested if tok not in available]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{kind}={raw!r}: unknown id(s) {unknown!r}. "
                f"Available: {sorted(available)}; or use the keyword(s) "
                f"{sorted(keep_all_keywords)} (keep all) / `none` (drop all)"
                + (f" / {sorted(drop_all_keywords)} (drop all)" if drop_all_keywords else "")
                + "."
            ),
        )
    return set(requested)


def project_binding(
    binding: sl.DataflowBinding,
    *,
    attributes_param: str | None,
    measures_param: str | None,
) -> sl.DataflowBinding:
    """Return a copy of ``binding`` with attribute / measure column maps projected.

    Parses the SDMX-REST 2.2.2 ``attributes=`` / ``measures=`` query strings — a
    comma-separated list of IDs, the keyword ``all`` / ``dsd`` (keep everything,
    the default), or ``none`` (drop everything). Unknown IDs raise so callers
    surface them as 400. Dimension columns are never projected (they always
    appear in the response).
    """
    attr_keep = resolve_projection_set(
        binding.attribute_column_map,
        attributes_param,
        keep_all_keywords=KEEP_ALL_ATTRIBUTE_KEYWORDS,
        drop_all_keywords=DROP_ALL_ATTRIBUTE_KEYWORDS,
        kind="attributes",
    )
    measure_keep = resolve_projection_set(
        binding.measure_column_map,
        measures_param,
        keep_all_keywords=KEEP_ALL_MEASURE_KEYWORDS,
        drop_all_keywords=frozenset(),
        kind="measures",
    )
    if attr_keep is None and measure_keep is None:
        return binding
    return evolve(
        binding,
        attribute_column_map=(
            binding.attribute_column_map
            if attr_keep is None
            else {k: v for k, v in binding.attribute_column_map.items() if k in attr_keep}
        ),
        measure_column_map=(
            binding.measure_column_map
            if measure_keep is None
            else {k: v for k, v in binding.measure_column_map.items() if k in measure_keep}
        ),
    )


def project_arrow_batches(
    batches: Iterable[pa.RecordBatch],
    binding: sl.DataflowBinding,
) -> Iterator[pa.RecordBatch]:
    """Yield each arrow batch projected to the columns the binding declares.

    The CSV writer reads from the trimmed binding to pick columns, but a
    parquet writer fed the raw ``SELECT *`` batches would emit everything —
    so the arrow path has to project explicitly to honour ``attributes=`` /
    ``measures=``.
    """
    keep_cols = [
        *binding.dimension_column_map.values(),
        *binding.measure_column_map.values(),
        *binding.attribute_column_map.values(),
    ]
    for batch in batches:
        present = [c for c in keep_cols if c in batch.schema.names]
        yield batch.select(present)


def materialise_dsd_for_flow(registry: sl.LocalRegistry, flow_urn: sl.SdmxUrn[sl.Dataflow]) -> sl.DataStructure | None:
    """Resolve ``flow_urn`` to its bound DSD, materialised.

    sdmxlib's SDMX-JSON writers need the DSD object (not just a URN) to pull
    localised dimension / attribute / code labels.
    """
    df = registry.get(flow_urn)
    if not isinstance(df, sl.Dataflow) or df.structure is None:
        return None
    dsd_raw = registry.get(df.structure.urn)
    if dsd_raw is None:
        return None
    materialize = getattr(dsd_raw, "materialize", None)
    dsd = materialize() if callable(materialize) else dsd_raw
    return dsd if isinstance(dsd, sl.DataStructure) else None


def key_dimension_ids(
    registry: sl.LocalRegistry,
    flow_urn: sl.SdmxUrn[sl.Dataflow],
    binding: sl.DataflowBinding,
) -> list[str]:
    """Resolve the flow's key-dimension ids in DSD order.

    Dotted-key parsing is positional, so the *order* is the contract — segment
    ``i`` of ``A5.2021.._T`` means whatever dimension sits at index ``i`` here.
    The DSD is authoritative; the binding's column map is the fallback for
    flows whose DSD is missing or not materialisable, which is lossy only if
    the producer wrote that map in a different order.

    Memoised per registry and flow URN: the answer is fixed for a bundle build,
    and the lookup is on the hot path of every ``/data`` and ``/availability``
    request. A reopened bundle is a new registry, so it starts empty.

    Deliberately does *not* materialise a lazy DSD — an already-materialised
    one is used, anything else takes the binding fallback. Forcing the
    materialise here would change the dimension order this returns for lazy
    flows, which would silently re-interpret every dotted key against them.

    Args:
        registry: Registry used to resolve the flow and its DSD.
        flow_urn: URN of the dataflow.
        binding: Supplies the fallback dimension order.
    """
    cache_key = str(flow_urn)
    per_registry = _KEY_DIM_IDS_CACHE.setdefault(registry, {})
    cached = per_registry.get(cache_key)
    if cached is not None:
        return cached
    flow = registry.get(flow_urn)
    result: list[str] | None = None
    if isinstance(flow, sl.Dataflow) and isinstance(flow.structure, sl.Ref):
        dsd = registry.get(flow.structure.urn)
        if isinstance(dsd, sl.DataStructure):
            # sdmxlib splits the slot list by REST generation: 1.5/2.1 keeps
            # the time dimension out of the dotted key, 2.2.2 makes it a slot.
            # Hardcoded rather than threaded through as a parameter because
            # every path this library publishes is 2.2.2 grammar — there is no
            # request here that could be the other generation. A service that
            # also answers 1.5 URLs translates them in a forwarder, which is
            # the layer that knows which generation it is serving.
            result = dsd.key_slot_ids("3.0")
    if result is None:
        result = list(binding.dimension_column_map.keys())
    per_registry[cache_key] = result
    return result


def label_maps_for_dims(
    registry: sl.LocalRegistry,
    dim_codelist_urns: dict[str, sl.SdmxUrn[sl.Codelist]],
    binding: sl.DataflowBinding,
    lang: str,
) -> dict[str, dict[str, str]]:
    """Return ``{column_name: {code_id: label}}`` for every key dim bound to a codelist.

    Takes the ``dim_id -> codelist URN`` mapping rather than deriving it, so
    consumers can supply a cached one. Uses ``LazyCodelist.code_map``, which is
    itself cached per (codelist_urn, lang). Dims without an enumeration, or
    whose codelist the registry cannot resolve, yield no entry — the CSV emit
    path passes their ids through unchanged.

    Args:
        registry: Registry used to resolve each codelist URN.
        dim_codelist_urns: ``dim_id -> codelist URN`` for the flow's key dims.
        binding: Supplies the ``dim_id -> physical column`` mapping.
        lang: Language tag passed to ``code_map``.
    """
    out: dict[str, dict[str, str]] = {}
    for dim_id, cl_urn in dim_codelist_urns.items():
        col = binding.dimension_column_map.get(dim_id)
        if not col:
            continue
        cl = registry.get(cl_urn)
        if isinstance(cl, sl.CodelistLike):
            out[col] = cl.code_map(lang=lang)
    return out


__all__ = [
    "key_dimension_ids",
    "label_maps_for_dims",
    "materialise_dsd_for_flow",
    "project_arrow_batches",
    "project_binding",
    "resolve_projection_set",
]
