# URL grammar

Every route serves the SDMX-REST 2.2.2 URL grammar, and the data plane takes
no API version.

## Why one generation

SDMX-REST 2.2.2 makes the time dimension a key slot. SDMX-REST 1.5 and 2.1
keep time out of the key and filter it with `startPeriod` and `endPeriod`, so
a 1.5 key has one segment fewer for the same data structure.

The two generations' keys cannot be told apart by looking at them. Code that
reads a 1.5 key against the 2.2.2 slot list, and pads it or skips the length
check, shifts every value one dimension and returns a well-formed wrong
answer. The routes read a key against the slot list
`data/projection.py:key_dimension_ids` returns, and `sl.parse_components`
refuses a key of the wrong length, so a 1.5 key sent to these routes is a 400
that names the dimensions. `tests/test_key_slots.py` pins the slot order and
the refusal.

`key_dimension_ids` returns `dsd.key_slot_ids("3.0")` when the registry
returns a materialised `DataStructure`. For a missing or lazy DSD it returns
the order of the binding's `dimension_column_map`, which the producer writes;
time is then a key slot only if the producer's map includes it.

## Older generations

A consumer that serves SDMX-REST 1.5 runs a forwarder. The forwarder knows
which generation its URL belongs to, translates the request, and passes a
2.2.2 request to these routes. The routes never guess a key's generation.

`AvailabilityKey` keys on the resolved dataflow URN, so a 1.5 path and a
2.2.2 path for the same flow share one precomputed entry, and two flows stay
distinct under any forwarder path shape.

## Backported spellings

Two 1.5 conveniences are accepted on the 2.2.2 routes because they cannot be
misread: the `all` key, and `startPeriod` and `endPeriod`. A further spelling
is added on the same terms: it must have one reading under both grammars.
The availability path without a `componentID` is a forwarder's to route to
`all`; `routes/availability.py` registers only the six-segment path
([GH-76](https://github.com/StatCat-Suite/sdmxlib-rest/issues/76)).
