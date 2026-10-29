# Scope and seams

sdmxlib-rest was extracted in place from its first consumer service over four
phases and split into its own repository in a fifth. Every decision on this
page came from deciding, one piece at a time, whether that service's code was
the same for every catalog.

## The rule

The library holds no catalog-specific logic. Code that needs to know about a
particular bundle, lake layout, agency, codelist or vendor parameter belongs
in the consumer. The library defines a Protocol for it and ships an inert
default.

A default is inert when a consumer that installs no seam gets a correct
service that recomputes more than it needs to. `NullStructureCache` renders
every structure response; `NullAvailabilityValues` scans for every
availability answer. Neither changes a response body.

`tests/test_library_import_boundary.py` fails the build if the package
imports statcatlib, under its current name or its former names `pinax` and
`pinaxlib`.

The middleware breaks the rule today. `openapi_extensions.py` hardcodes one
consumer's paths and `/data` parameters, and `query_param_guard.py` admits
that consumer's `within[` and `_chunk` parameters on every route and exempts
its `/explorer` path. [GH-73](https://github.com/StatCat-Suite/sdmxlib-rest/issues/73) moves them into the consumer's
configuration.

## When a seam is added

A seam is added when a running service needs one, and never in anticipation.
Each seam on the [Seams](../seams.md) page names the case that created it.
A proposal for a new seam names the service that needs it and the code that
service would delete.

`CatalogProvider` is the one seam that names a concrete type. It returns an
`sl.BundleRegistry`, the read-only registry `LocalRegistry.bundle` opens,
because the binding-driven data-plane helpers need `binding_for`, and
`binding_for` exists only on that class. No sdmxlib Protocol covers it.

## The data route

The library serves `/structure`, `/metadata` and `/availability`. Consumers
write `/data`.

A `/data` handler decides how responses are cached, which partitions a query
prunes, and which vendor parameters exist. When the library was extracted, an
inventory of what each consumer's handler did found those three parts
different in every service, and a library route was rejected. The library
ships the parts every handler writes the same way, in `sdmxlib_rest.data`:
the key's slot order, filter and sort parsing, projection, paging headers, SQL
fragments and the streaming encoders. The handler splits the key itself. [Writing a data route](../data-plane.md) lists them.

A request to add a data route reopens this decision. It needs a new
inventory showing the orchestration has converged across consumers.

## `/availability` reads observations

`/availability` is the one library route that reads observations. It reads
them through sdmxlib's query DSL, `registry.obs(flow)`, so a provider's
registry reaches its lake on its own connection. `OpenedCatalog.meta_conn`
exists for consumers' seams. [Availability](availability.md) has the
reasons.
