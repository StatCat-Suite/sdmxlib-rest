"""A real bundle to measure against, discovered rather than hard-coded.

Opt-in: set ``SDMXLIB_REST_BENCH_BUNDLE`` to a bundle directory containing
``lake.ducklake``. Without it every test here skips, so ``just test-all`` stays
green on a machine that has no bundle.

**Read-only ATTACH rather than a copy.** sdmxlib's equivalent tier copies the
bundle first because ``LocalRegistry.bundle()`` opens ``meta.duckdb``
read-write and DuckDB's lock is exclusive — pointing it at a live bundle would
lock what it measures. Nothing here needs the structural side: these tests
measure the SQL ``/availability`` runs against the *lake*, which attaches
``READ_ONLY`` and takes no lock. Measured at 20 ms and +8 MB for a multi-GB
DuckLake, so there is nothing to amortise by copying.

**Discovered, not hard-coded.** The fixture finds the widest table in the lake
and treats it as the observations table, then any other table sharing columns
with it as the sidecar. That keeps the tier runnable against any bundle instead
of only the one it was written on, which is what made the numbers in
#2 unreproducible.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pytest
from attrs import define

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Dimension-ish columns are low-cardinality text; measures are not. Chosen by
#: name because the lake carries no DSD — the point is to exercise the shapes
#: `/availability` emits, not to resolve real components.
_MEASURE_PREFIXES = ("OBS_", "VALUE", "MEASURE_")

_ENV = "SDMXLIB_REST_BENCH_BUNDLE"


@define
class Bundle:
    """The lake a benchmark runs against.

    Attributes:
        conn: Connection with the lake attached read-only.
        main: Fully-qualified observations table.
        sidecar: Fully-qualified sidecar table, or None when the bundle has one
            table only.
        join_keys: Columns shared by main and sidecar — what
            ``_shared_join_columns`` would return.
        dims: Candidate dimension columns on the main table.
        rows: Row count of the main table.
        attach: The ATTACH statement that produced ``conn``'s lake, so a test
            needing its own connection (a memory cap, a different pragma) can
            reproduce it rather than guess at it.
        data_files: The lake's parquet files, for `evict_page_cache`.
    """

    conn: duckdb.DuckDBPyConnection
    main: str
    sidecar: str | None
    join_keys: tuple[str, ...]
    dims: tuple[str, ...]
    rows: int
    attach: str
    data_files: tuple[Path, ...]

    def evict_page_cache(self) -> int:
        """Drop this lake's parquet files from the OS page cache. Returns the count.

        ``POSIX_FADV_DONTNEED`` evicts *clean* pages, which read-only parquet
        always is, and needs no privileges — so a cold measurement becomes
        repeatable instead of being whatever the first run of the day happened
        to see. Verified on a large census bundle: a query at 1.5 s warm takes 5.0 s after
        eviction and returns to 1.7 s on the next run.

        This matters more than it sounds. The first "cold" number taken for #6
        was 43.91 s, measured as the first query in a fresh process — which also
        paid extension load, DuckLake metadata and first file opens. Treating
        that as the cold cost inverted the conclusion it was used for.

        Best effort: a file that cannot be opened is skipped rather than failing
        the test, since a partial eviction makes a measurement conservative
        (warmer, so it understates the cold cost) rather than wrong.
        """
        evicted = 0
        for path in self.data_files:
            with contextlib.suppress(OSError):
                fd = os.open(path, os.O_RDONLY)
                try:
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                    evicted += 1
                finally:
                    os.close(fd)
        return evicted

    def connect(self, **config: object) -> duckdb.DuckDBPyConnection:
        """A fresh connection with the same lake attached, for pragma tests."""
        conn = duckdb.connect(config=config or None)
        conn.execute("INSTALL ducklake; LOAD ducklake;")
        conn.execute(self.attach)
        return conn

    def join_clause(self) -> str:
        """The ``LEFT JOIN`` our route appends to every shape, or ``""``."""
        if self.sidecar is None or not self.join_keys:
            return ""
        keys = ", ".join(f'"{k}"' for k in self.join_keys)
        return f" LEFT JOIN {self.sidecar} USING ({keys})"


def _columns(conn: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    return [str(r[0]) for r in conn.execute(f"DESCRIBE {table}").fetchall()]


@pytest.fixture(scope="session")
def bundle() -> Iterator[Bundle]:
    path = os.environ.get(_ENV)
    if not path:
        pytest.skip(f"set {_ENV} to a bundle directory to run the bundle tier")
    directory = Path(path)
    lake = directory / "lake.ducklake"
    if not lake.is_file():
        pytest.skip(f"{lake} is not a file — {_ENV} should name a bundle directory")

    # OVERRIDE_DATA_PATH, as sdmxlib's `LocalRegistry.bundle()` does: a catalog
    # records the data path it was written with, often relative to the builder's
    # working directory, and DuckDB 2.0 refuses a DATA_PATH that differs from it.
    attach = (
        f"ATTACH 'ducklake:{lake}' AS lake (READ_ONLY, DATA_PATH '{directory / 'parquet'}/', OVERRIDE_DATA_PATH true)"
    )
    conn = duckdb.connect()
    conn.execute("INSTALL ducklake; LOAD ducklake;")
    conn.execute(attach)

    tables = [
        f"lake.{schema}.{name}"
        for schema, name in conn.execute(
            "SELECT table_schema, table_name FROM information_schema.tables WHERE table_catalog = 'lake'"
        ).fetchall()
    ]
    if not tables:
        pytest.skip("the attached lake has no tables")

    # Widest table is the fact table on every layout we have seen; row count
    # comes from DuckLake metadata, so this costs nothing.
    by_width = sorted(tables, key=lambda t: len(_columns(conn, t)), reverse=True)
    main = by_width[0]
    main_cols = _columns(conn, main)

    sidecar: str | None = None
    join_keys: tuple[str, ...] = ()
    for candidate in by_width[1:]:
        shared = tuple(c for c in main_cols if c in set(_columns(conn, candidate)))
        if shared:
            sidecar, join_keys = candidate, shared
            break

    sidecar_only = set() if sidecar is None else set(_columns(conn, sidecar)) - set(main_cols)
    dims = tuple(c for c in main_cols if not c.startswith(_MEASURE_PREFIXES) and c not in sidecar_only)
    rows = int(conn.execute(f"SELECT count(*) FROM {main}").fetchone()[0])  # pyright: ignore[reportOptionalSubscript]

    yield Bundle(
        conn=conn,
        main=main,
        sidecar=sidecar,
        join_keys=join_keys,
        dims=dims,
        rows=rows,
        attach=attach,
        data_files=tuple(sorted((directory / "parquet").glob("**/*.parquet"))),
    )
    conn.close()
