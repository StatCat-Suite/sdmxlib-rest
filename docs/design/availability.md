# Availability

`/availability` returns the distinct values each component takes under a
partial key, and the observation count. It is the slowest route a
picker-style client calls on every step, and the unfiltered request for a
whole flow is both the most common and the most expensive.

## Reading through the query DSL

The route reads observations through sdmxlib's query DSL,
`registry.obs(flow)`. It used to build SQL by hand, with a sidecar join placed
above every query shape. The DSL compiler decides per query whether a join is
needed, which removed a class of bugs that made a `COUNT(*)` 126 to 882 times
slower ([GitLab #3](https://gitlab.com/pinax-suite/sdmxlib-rest/-/issues/3),
[#6](https://gitlab.com/pinax-suite/sdmxlib-rest/-/issues/6)).

The cost is a contract on providers: the registry must reach its lake on its
own connection. `LocalRegistry.bundle` attaches it there, and attaching a
DuckLake is a metadata read, measured at 20 ms and 8 MB for a 520-million-row
lake ([GitLab #6](https://gitlab.com/pinax-suite/sdmxlib-rest/-/issues/6)). The DSL builds one query root per request, 13 to 19 ms on a large
census bundle; roots are not cached across requests, since a root binds the
constructing thread's cursor.

## Query shapes

The route picks one of three shapes. Each choice was measured on a
500-million-row, six-dimension census bundle. The comments above the choice in
`routes/availability.py` carry the `componentID=all` tables, and the docstrings
in `tests/bench/test_availability_sql.py` carry the `mode=available` ones. The
bench asserts the ratios; it records times only in its docstrings.

| Request | Shape | Why |
| --- | --- | --- |
| `componentID=all`, `mode=exact` | One aggregate: every `DISTINCT` and the count in a single scan | It beats the per-dimension loop by 1.2× unfiltered on DuckDB 2, and by up to 4.8× filtered, measured on DuckDB 1.5 |
| `mode=available` | One query per component, each relaxing that component's own constraint, plus one count under the full filter | Each relaxed query needs its own `WHERE`, so no single scan can prune. The loop won every round, by 1.03× to 1.62× with the page cache evicted and 1.03× to 5.49× warm |
| One `componentID` | One `DISTINCT` and one count | There is no loop to collapse |

The `mode=available` measurement settled whether to ask sdmxlib for
`unique(where=...)`: the route would not use it, so it was not filed. A
benchmark in which the single scan wins cold reopens that request.

## Precompute

A flow's value lists change only when its publication changes, so they can be
computed once. Comparable services keep availability as content constraints
built at load time.

Precompute stays in the consumer
([GitLab #7](https://gitlab.com/pinax-suite/sdmxlib-rest/-/issues/7)). Which
flows to precompute, where the values live and when to warm them are catalog
decisions. The library supplies two parts:

- **`AvailabilityValues`**, a seam consulted before the scan. It is keyed on
  the bare token, the flow URN and the component id, and holds value lists
  and a count, so one entry serves every format, language and URL path.
- **`render_availability`** (`responses/availability.py`), which builds the
  response from value lists. A precomputed answer and a scanned one go through
  the same renderer and negotiate format and language alike.

The route consults the seam only when the filter the shaper produced is
empty: no key, no `c[]`, no period bound. No producer can precompute every
filter, and a shaper may add a constraint the URL never named, so an
unfiltered entry would be wrong for it.

An earlier response-level cache keyed on the raw query string missed on
`?lang=`, on the 2.2.2 path, and on any query spelling the producer had not
planted. The value-level seam replaced it.

## The default

With no seam installed every request scans. On the 500-million-row bundle an
unfiltered answer took about 3 to 5 s warm and 5 to 8 s cold
([GitLab #7](https://gitlab.com/pinax-suite/sdmxlib-rest/-/issues/7)), uses 0.5 to
1 GB of memory, and completes under a 256 MB cap in 7.5 s by spilling. That is
a correct default and too slow for a large flow in production, so a service
over large flows installs `AvailabilityValues`.
