"""What `/availability`'s three query shapes cost, as ratios.

**Ratios, never wall times.** On a large census bundle the same query drifted 2x
between otherwise identical runs — 3.1 s to 5.3 s for one availability answer —
purely on machine load and page-cache state, while every ratio held. So
``assert elapsed < 4`` is a flaky test on a quiet laptop and a false pass on a
fast one. Each assertion here is a ratio against the same query without the
clause under test, which says "this costs what it should" rather than "this
machine is fast". The bounds are set well inside the observed effect so they
fail when the effect disappears, not when the CI runner is busy.

Observed on a ~500M-row, 6-dimension census bundle, duckdb 1.5.5 and 1.5.6 alike,
except where a row names duckdb 2.0.0:

=================================================  ==================
measurement                                        observed
=================================================  ==================
``COUNT(*)`` with the sidecar join                 126x - 882x slower
combined aggregate vs the loop, unfiltered         ~2.6x slower
the same, duckdb 2.0.0 (GH-36)                     0.85x
availability answer under a 256 MB cap             completes, 7.5 s
=================================================  ==================

The first two are the evidence for issues #3 and #4 respectively.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import duckdb
import pytest

if TYPE_CHECKING:
    from tests.bench.conftest import Bundle

pytestmark = pytest.mark.bundle

#: The unfiltered comparison was measured on a ~500M-row bundle. Below this the
#: per-dimension arrays can outweigh the scans saved, and a loss is expected.
_LARGE_LAKE_ROWS = 100_000_000

#: Timing is best-of-N: the minimum is the least noisy estimator here, because
#: interference only ever makes a run slower.
_REPEATS = 2


def _best(conn: duckdb.DuckDBPyConnection, sql: str) -> float:
    best = float("inf")
    for _ in range(_REPEATS):
        start = time.perf_counter()
        _ = conn.execute(sql).fetchall()
        best = min(best, time.perf_counter() - start)
    return best


def _best_params(conn: duckdb.DuckDBPyConnection, sql: str, params: list[object]) -> float:
    best = float("inf")
    for _ in range(_REPEATS):
        start = time.perf_counter()
        _ = conn.execute(sql, params).fetchall()
        best = min(best, time.perf_counter() - start)
    return best


def _distinct(bundle: Bundle, column: str, *, join: str) -> str:
    quoted = f'"{column}"'
    return f"SELECT DISTINCT {quoted} FROM {bundle.main}{join} WHERE {quoted} IS NOT NULL ORDER BY {quoted}"


def _combined(bundle: Bundle, columns: tuple[str, ...]) -> str:
    parts = [f'array_agg(DISTINCT "{c}" ORDER BY "{c}") FILTER (WHERE "{c}" IS NOT NULL)' for c in columns]
    return f"SELECT {', '.join(parts)}, COUNT(*) FROM {bundle.main}"


def test_the_sidecar_join_is_what_count_cannot_elide(bundle: Bundle) -> None:
    """#3: the join our route appends unconditionally, priced on ``COUNT(*)``.

    DuckDB elides the join for a single-column ``DISTINCT`` but cannot for
    ``COUNT(*)``: a LEFT JOIN whose right side has duplicate keys legitimately
    changes the count. That is both why it is expensive and why leaving it on
    is a correctness risk.
    """
    if bundle.sidecar is None or not bundle.join_keys:
        pytest.skip("bundle has no sidecar, so there is no join to price")

    plain = _best(bundle.conn, f"SELECT COUNT(*) FROM {bundle.main}")
    joined = _best(bundle.conn, f"SELECT COUNT(*) FROM {bundle.main}{bundle.join_clause()}")

    assert joined > plain * 10, (
        f"expected the unconditional sidecar join to dominate COUNT(*); "
        f"plain={plain * 1000:.1f}ms joined={joined * 1000:.1f}ms "
        f"({joined / plain:.1f}x). If this ratio has collapsed, DuckDB learned to "
        f"elide the join and #3's urgency changed — re-measure before acting on it."
    )


def test_the_join_does_not_change_the_count_on_this_bundle(bundle: Bundle) -> None:
    """The correctness half of #3, which is a property of the data, not the code.

    A 1:1 sidecar makes the joined count right. This records which kind of
    bundle we are measuring, so a failure here is the interesting news: a bundle
    whose sidecar fans out. Since #8 the route refuses such a bundle with a 500
    on every query that needs the join, rather than shipping an inflated
    ``obs_count``, so a failure here also predicts those 500s.
    """
    if bundle.sidecar is None or not bundle.join_keys:
        pytest.skip("bundle has no sidecar")

    keys = ", ".join(f'"{k}"' for k in bundle.join_keys)
    counted = bundle.conn.execute(f"SELECT count(*), count(DISTINCT ({keys})) FROM {bundle.sidecar}").fetchone()
    assert counted is not None, "an aggregate always returns a row"
    rows, distinct = counted

    if rows != distinct:
        pytest.fail(
            f"{bundle.sidecar} has {rows:,} rows for {distinct:,} distinct join keys, so a "
            f"COUNT(*) over the join would be INFLATED on this bundle; /availability refuses "
            f"(500) every query that needs the join until the producer makes it unique (#8)."
        )
    joined = bundle.conn.execute(f"SELECT COUNT(*) FROM {bundle.main}{bundle.join_clause()}").fetchone()
    assert joined is not None
    assert int(joined[0]) == bundle.rows


def test_unfiltered_the_combined_aggregate_beats_the_loop(bundle: Bundle) -> None:
    """#4, half one: with no filter the combined aggregate wins on DuckDB 2 (GH-36).

    This is the dominant .Stat request (the bare-flow URL). DuckDB 1.5 lost it
    by 2.8x, building a sorted array per dimension; DuckDB 2 wins it by 1.2x on
    a ~500M-row census bundle. A bundle with few rows and a very large
    geography can still lose: on the synthetic ``census`` shape (20M rows) the
    loop usually wins, by 1.2-2x. Below `_LARGE_LAKE_ROWS` a loss is reported as
    an expected failure with its ratio; above it, a loss fails.
    """
    columns = bundle.dims[:6]
    if len(columns) < 2:
        pytest.skip("need at least two dimension-ish columns to compare the shapes")

    loop = sum(_best(bundle.conn, _distinct(bundle, c, join="")) for c in columns)
    loop += _best(bundle.conn, f"SELECT COUNT(*) FROM {bundle.main}")
    combined = _best(bundle.conn, _combined(bundle, columns))

    if combined >= loop and bundle.rows < _LARGE_LAKE_ROWS:
        pytest.xfail(
            f"the loop wins on {bundle.rows:,} rows: loop={loop:.2f}s combined={combined:.2f}s "
            f"({combined / loop:.2f}x); the route is tuned for {_LARGE_LAKE_ROWS:,}+"
        )
    assert combined < loop, (
        f"expected the combined aggregate to win unfiltered; "
        f"loop={loop:.2f}s combined={combined:.2f}s ({combined / loop:.2f}x). "
        f"`fast_path_eligible` sends unfiltered requests through it; if the loop wins "
        f"on a large bundle too, exclude the unfiltered case again."
    )


def test_filtered_the_combined_aggregate_beats_the_loop(bundle: Bundle) -> None:
    """#4, half two: with a filter the combined form wins, which is why it survives.

    Any predicate makes every ``DISTINCT`` pay a scan of its own, and then one
    scan beats six. On DuckDB 1.5 this half alone kept the combined shape in
    the route.
    """
    columns = bundle.dims[:6]
    if len(columns) < 2:
        pytest.skip("need at least two dimension-ish columns to compare the shapes")

    # Narrow on a column that is not one of the aggregated ones where possible,
    # so the filter is not trivially answering the query it restricts.
    pivot = columns[1]
    value = bundle.conn.execute(f'SELECT "{pivot}" FROM {bundle.main} GROUP BY 1 ORDER BY count(*) LIMIT 1').fetchone()
    if value is None:
        pytest.skip(f"no values to filter {pivot} on")
    where = f' WHERE "{pivot}" = ?'
    quoted = [f'"{c}"' for c in columns]

    loop = 0.0
    for column in quoted:
        loop += _best_params(
            bundle.conn,
            f"SELECT DISTINCT {column} FROM {bundle.main}{where} AND {column} IS NOT NULL ORDER BY {column}",
            [value[0]],
        )
    loop += _best_params(bundle.conn, f"SELECT COUNT(*) FROM {bundle.main}{where}", [value[0]])
    combined = _best_params(bundle.conn, f"{_combined(bundle, columns)}{where}", [value[0]])

    assert combined < loop, (
        f"expected the combined aggregate to win once a filter narrows the scan; "
        f"loop={loop:.2f}s combined={combined:.2f}s ({combined / loop:.2f}x). "
        f"If it is losing here too, `fast_path_eligible` should drop the shape entirely."
    )


def test_an_availability_answer_completes_under_a_hard_memory_cap(bundle: Bundle) -> None:
    """Bounded memory is a requirement, not a nice-to-have.

    A half-billion-row cube answers under 256 MB by spilling. Asserted as
    completion rather than as a time, because the point is the absence of an
    OOM and of a cliff — the cost of the cap is latency, which the issue
    records as a smooth trade (7.5 s at 256 MB, 4.4 s at 4 GB).
    """
    columns = bundle.dims[:6]
    capped = bundle.connect(memory_limit="256MB", threads=4)
    try:
        for column in columns:
            _ = capped.execute(_distinct(bundle, column, join="")).fetchall()
        count = capped.execute(f"SELECT COUNT(*) FROM {bundle.main}").fetchone()
        assert count is not None
        assert int(count[0]) == bundle.rows
    finally:
        capped.close()


def _relaxed(constraints: dict[str, str], drop: str) -> tuple[str, list[str]]:
    """``mode=available``'s WHERE for one dimension: every constraint but its own."""
    kept = {k: v for k, v in constraints.items() if k != drop}
    if not kept:
        return "", []
    return " AND ".join(f'"{k}" = ?' for k in kept), list(kept.values())


@pytest.fixture(scope="module")
def cold(bundle: Bundle) -> Bundle:
    """The bundle, once eviction is shown to cool its page cache; skips otherwise.

    `Bundle.evict_page_cache` needs `os.posix_fadvise`, which Linux has and macOS
    and Windows lack. On a VM, which includes GitHub's runners and Colima, the
    host may cache the guest's disk, so evicting the guest's cache can leave
    reads warm: one runner measured 1.28x, the next cooled. Either way a cold
    test would measure the warm regime, which is the mistake it exists to
    prevent, so it skips with the reason instead of failing.
    """
    if not hasattr(os, "posix_fadvise"):
        pytest.skip("page-cache eviction needs os.posix_fadvise (Linux)")
    sql = f'SELECT COUNT(DISTINCT "{bundle.dims[0]}") FROM {bundle.main} WHERE "{bundle.dims[0]}" IS NOT NULL'
    _ = _best(bundle.conn, sql)  # warm it
    warm = _best(bundle.conn, sql)
    if bundle.evict_page_cache() == 0:
        pytest.skip("nothing was evicted, so no measurement here would be cold")
    start = time.perf_counter()
    _ = bundle.conn.execute(sql).fetchall()
    cooled = time.perf_counter() - start
    if cooled < warm * 1.5:
        pytest.skip(
            f"eviction did not cool the cache (warm={warm * 1000:.0f}ms after={cooled * 1000:.0f}ms); "
            f"a VM's host caches its disk, so cold measurements here would be warm"
        )
    return bundle


def test_mode_available_does_not_want_a_single_scan_even_cold(cold: Bundle) -> None:
    """#6: the per-dimension loop beats the one-scan form in both cache regimes.

    ``mode=available`` relaxes a different constraint per dimension, so collapsing
    it to one scan needs a per-aggregate ``FILTER`` — which is what a column-level
    ``unique(where=…)`` would compile to, and the capability #6 was ready to ask
    sdmxlib for.

    It is not worth asking for. Measured cold (page cache evicted) and warm, the
    loop wins every round: cold 1.03x-1.62x, warm 1.03x-5.49x. The single scan
    cannot be pruned by any one predicate — each ``FILTER`` belongs to a different
    aggregate — so it reads everything every time, while the relaxed queries are
    selective enough to stay cheaper even when nothing is cached.

    This corrects an earlier reading. A first-ever-run figure of 43.91 s was taken
    for the loop's cold cost and made the single scan look 7x better; that run also
    paid extension load, DuckLake metadata and first file opens. With
    `evict_page_cache` the loop's genuine cold cost is ~5-8 s, and the conclusion
    inverts. Asserted cold, because that is the regime a growing catalog lives in.
    """
    columns = cold.dims[:6]
    if len(columns) < 3:
        pytest.skip("need at least three dimension-ish columns to relax one at a time")

    # Constrain two dimensions, chosen for low cardinality so the filter is real.
    constraints: dict[str, str] = {}
    for column in columns:
        row = cold.conn.execute(f'SELECT "{column}" FROM {cold.main} GROUP BY 1 ORDER BY count(*) LIMIT 1').fetchone()
        if row is not None and row[0] is not None:
            constraints[column] = str(row[0])
        if len(constraints) == 2:
            break
    if len(constraints) < 2:
        pytest.skip("could not pick two constrainable dimensions")

    full_where, full_params = _relaxed(constraints, drop="")

    def loop() -> float:
        start = time.perf_counter()
        for column in columns:
            where, params = _relaxed(constraints, drop=column)
            clause = f" WHERE {where} AND" if where else " WHERE"
            _ = cold.conn.execute(
                f'SELECT DISTINCT "{column}" FROM {cold.main}{clause} "{column}" IS NOT NULL ORDER BY "{column}"',
                params,
            ).fetchall()
        _ = cold.conn.execute(f"SELECT COUNT(*) FROM {cold.main} WHERE {full_where}", full_params).fetchall()
        return time.perf_counter() - start

    def single_scan() -> float:
        parts: list[str] = []
        params: list[str] = []
        for column in columns:
            where, bound = _relaxed(constraints, drop=column)
            cond = f'{where} AND "{column}" IS NOT NULL' if where else f'"{column}" IS NOT NULL'
            parts.append(f'array_agg(DISTINCT "{column}" ORDER BY "{column}") FILTER (WHERE {cond})')
            params += bound
        parts.append(f"COUNT(*) FILTER (WHERE {full_where})")
        params += full_params
        start = time.perf_counter()
        _ = cold.conn.execute(f"SELECT {', '.join(parts)} FROM {cold.main}", params).fetchall()
        return time.perf_counter() - start

    assert cold.evict_page_cache() > 0, "nothing was evicted, so this is not a cold measurement"
    cold_loop = loop()
    assert cold.evict_page_cache() > 0
    cold_scan = single_scan()

    assert cold_loop < cold_scan, (
        f"cold, the per-dimension loop should still win: loop={cold_loop:.2f}s "
        f"scan={cold_scan:.2f}s ({cold_scan / cold_loop:.2f}x). If the single scan wins "
        f"cold, #6's case for asking sdmxlib for a column-level `unique(where=…)` is back on."
    )
