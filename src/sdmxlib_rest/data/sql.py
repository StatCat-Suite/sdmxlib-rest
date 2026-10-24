"""SQL fragment builders driven by the dataflow binding.

These shape the generated query. Each one is
binding-driven and degrades to a no-op when the columns it needs are absent,
so a catalog that doesn't model them pays nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from sdmxlib_rest.data.params import sql_ident

if TYPE_CHECKING:
    import sdmxlib as sl


def decimals_format_replace_clause(
    binding: sl.DataflowBinding,
    *,
    targets: Iterable[str],
    decimals_id: str = "DECIMALS",
) -> str:
    """Build ``REPLACE`` arguments formatting numeric columns to per-row precision.

    Wraps each target in ``printf('%.*f', DECIMALS, value)`` so the row's own
    ``DECIMALS`` attribute drives the decimal places. Returns an empty string
    when the binding declares no ``decimals_id`` attribute or none of the
    targets resolve — in either case a plain ``SELECT *`` is correct.

    The problem this solves is generic to SDMX: a single ``OBS_VALUE`` column
    typed ``DOUBLE`` has to carry counts, percentages, rates and medians at
    once, so the wire format needs per-row precision. ``DECIMALS`` is the
    standard attribute that carries it. NULL inputs (missing DECIMALS,
    suppressed value) propagate to NULL — ``printf`` short-circuits.

    Each target is looked up in the measure map **and** the attribute map: a
    producer may model confidence bounds either way, and the underlying column
    is numeric regardless.

    Args:
        binding: Supplies both column maps.
        targets: Component IDs to format, e.g. ``("OBS_VALUE",)``.
        decimals_id: Attribute ID carrying the per-row precision.
    """
    decimals_col = binding.attribute_column_map.get(decimals_id)
    if not decimals_col:
        return ""
    fragments = []
    dec_ident = sql_ident(decimals_col)
    for target_id in targets:
        col = binding.measure_column_map.get(target_id) or binding.attribute_column_map.get(target_id)
        if not col:
            continue
        col_ident = sql_ident(col)
        fragments.append(f"printf('%.*f', CAST({dec_ident} AS INTEGER), {col_ident}) AS {col_ident}")
    return ", ".join(fragments)


def qualify_is_active(cardinality: int, n_cap: int) -> bool:
    """Return False when ``firstNObservations`` / ``lastNObservations`` is a logical no-op.

    The ``QUALIFY row_number() OVER (PARTITION BY <series> ORDER BY <time>) <= N``
    pattern is expensive: the Window operator blocks partition pruning from
    being pushed through to the parquet read (measured ~85x slower than the
    bare scan on a filtered census query). When the time dimension's
    cardinality is already ``<= N`` the cap would keep every row anyway, so
    the QUALIFY can be skipped entirely for the same result.

    A cardinality of 0 means "unknown" and takes the safe path — apply the
    QUALIFY.

    Args:
        cardinality: Distinct values of the time dimension for this flow.
            Callers typically supply a cached probe.
        n_cap: The requested first/last N.
    """
    if n_cap <= 0:
        return True
    if cardinality == 0:
        return True
    return cardinality > n_cap
