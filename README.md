# sdmxlib-rest

A generic [SDMX-REST](https://github.com/sdmx-twg/sdmx-rest) HTTP surface over a
[sdmxlib](https://github.com/StatCat-Suite/sdmxlib) `LocalRegistry`.

It owns the parts of an SDMX-REST service that are the same everywhere — URL
grammar, content negotiation, localisation, streaming, response assembly — and
leaves everything catalog-specific to the consumer, behind explicit seams.

Documentation: https://statcat-suite.github.io/sdmxlib-rest/

```python
from sdmxlib_rest.app import RestSettings, create_app, library_routers
from sdmxlib_rest.runtime import CatalogRuntime

runtime = CatalogRuntime(MyCatalogProvider())
app = create_app(
    runtime,
    RestSettings(title="my-sdmx-api", version="1.0.0"),
    routers=list(library_routers()),
)
```

That serves the structure, metadata and availability planes. Serving data is the
consumer's job — see *Why there is no data route* below.

## What you supply

Everything catalog-shaped is a Protocol on `CatalogRuntime`, each with a no-op
default. A bare consumer implements only the first one.

| Seam | Purpose | Default |
|---|---|---|
| `CatalogProvider` | how the registry is opened | **required** |
| `SpliceRegistry` | post-serialise JSON transforms | empty |
| `StructureCache` | cache rendered structure responses | never caches |
| `AvailabilityValues` | precomputed availability values, for unfiltered requests | always scans |
| `MetadataStore` | reference-metadata lookups | returns nothing |
| `ComponentFilterShaper` | rewrite a parsed filter before it is queried | identity |
| `MessageAnnotator` | pre-serialise message annotation; with `SupportsFingerprint`, annotations that change at runtime re-key the structure cache and ETags. `OverlayAnnotator` applies sdmxlib annotation overlays | no-op |

The defaults are all *inert*, not *broken*: a consumer that installs none of
them gets a correct service, just one that recomputes more than it needs to.

**The registry has to reach its own observations.** `/availability` reads rows
through sdmxlib's query DSL (`registry.obs(flow)`), which runs on the
registry's own connection and attaches the lake there. Open a bundle with
`LocalRegistry.bundle(path)`, or any opener that does the same (statcatlib's
`Catalog` does). A lake attached only to some other connection is invisible to
it. Since 0.6.0; before, `/availability` ran its own SQL on `meta_conn`.

## What `create_app` gives you

Routes are the easy part. The assembly is what's worth not rewriting:

- **The middleware stack, in the one order that works.** The chain has real
  adjacency constraints — `?lang=` has to rewrite the header before the ETag
  layer reads it; the version rewrite has to land before compression decides;
  CORS has to be outermost or preflights fall through to a 404 that browsers
  report as a CORS failure. Each constraint is documented at
  `_install_middleware`. Only successful responses are stamped with an ETag
  and `Cache-Control`, so an error is never cached or revalidated.
- **Unwritable catalogue content as a 500 that says what failed.** sdmxlib
  refuses to serialise content no format can carry (a component with no
  concept, say). The response lists each artefact path and the rule it
  breaks, on the app and every mount, instead of a bare server error.
- **Runtime sharing across mounts**, so a service exposing several surfaces
  (a canonical one plus compatibility variants) opens the registry once.
- **A multi-spec docs page**, which has to be hand-rolled because FastAPI's
  helper emits a single-spec page and Swagger UI renders nothing when given
  both `url` and `urls`.

## Why there is no data route

The library owns data-plane *helpers* — key and query-parameter parsing,
projection, paging headers, binding-driven SQL fragments, and the
arrow→CSV/parquet encoders, all under `sdmxlib_rest.data`. It does not own the
`/data` route itself.

That is deliberate. Orchestration is where response caching, partition pushdown
and vendor extensions live, and every catalog shapes those differently — one
catalog's hierarchy descent and partition-pruning tricks are noise to the next.
A second consumer gets the hard, fiddly parts and writes its own handler around
them.

## Status

Extracted from a production SDMX-REST service and versioned independently from
`0.1.0`. The seam set is settled; the `create_app` signature may still move
before `1.0`.

Issues are tracked on [GitHub](https://github.com/StatCat-Suite/sdmxlib-rest/issues).
The project started on GitLab, so a bare `#7` in code, comments or docs means
an issue in the [GitLab tracker](https://gitlab.com/pinax-suite/sdmxlib-rest/-/issues),
which stays readable as an archive. GitHub issues are written `GH-12`.

## Licence

Apache-2.0.
