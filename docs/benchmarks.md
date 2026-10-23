# Benchmarks

The benchmarks assert ratios, never wall times. The same query on a large
census bundle took 3.1 s on one run and 5.3 s on the next, from machine load
and page-cache state alone, while every ratio held. So each test compares a
query with the same query without the clause under test.

They run outside `just ci`, against a bundle directory.

## Running them

```bash
just bench path/to/bundle          # a real bundle's lake
just bench-synthetic               # a synthetic bundle, shape `large`
just bench-synthetic census        # shape `census`
```

`just bench-synthetic` builds the bundle with sdmxlib's `synthetic_bundle`,
caches it under `.bench/` by shape and seed, and runs the same tests on it.

| Shape | Models | Builds in |
| --- | --- | --- |
| `large` | A 20,000-code geography, 20M rows | about a minute |
| `census` | The public 2021 Census Profile's codelists: a ~580,000-code six-level geography, a ~2,600-code nine-level characteristic list, English and French names, 20M rows | about 2 minutes |

`just bench-profile <bundle> <flow> <name>` writes a real bundle's shape to
`tests/bench/shapes/local/`, which git ignores, so a private bundle's
cardinalities stay out of the repository.

## What they measure

**Availability** (`tests/bench/test_availability_sql.py`) compares the query
shapes `/availability` chooses between:

- one combined aggregate against one `DISTINCT` per dimension, unfiltered and
  filtered;
- `mode=available`'s per-dimension queries against a single scan, with the
  page cache evicted;
- an answer under a 256 MB memory cap.

**Structure routes** (`tests/bench/test_structure_routes.py`) serve the
bundle's largest codelist through the app. They skip below 50,000 codes.
Measured on the `census` shape:

| Request | Time |
| --- | --- |
| Codelist, `detail=allstubs` | 3 ms |
| Codelist, full, SDMX-JSON (101 MB) | 5.4 s |
| Codelist, full, French names | 5.4 s |
| Codelist, full, SDMX-ML 3.0 | 5.9 s |
| Dataflow, `references=children&detail=referencestubs` | 32 ms |
| Dataflow, `references=children&detail=full` (95 MB) | 3.8 s |

## Cold measurements

The cold tests evict the bundle's Parquet files from the page cache with
`os.posix_fadvise`, which Linux has and macOS lacks. On a virtual machine the
host may cache the disk itself, so eviction can leave reads warm; the tests
check that a query slowed by at least 1.5× after eviction, and skip with both
times when it did not.

## In CI

`.github/workflows/bench.yml` runs `just bench-synthetic` on Linux for pull
requests that change the benchmarks, and on demand with a choice of shape. It
reports without blocking a merge.
