# sdmxlib-rest

[![PyPI](https://img.shields.io/pypi/v/sdmxlib-rest)](https://pypi.org/project/sdmxlib-rest/)
[![Python](https://img.shields.io/pypi/pyversions/sdmxlib-rest)](https://pypi.org/project/sdmxlib-rest/)
[![License](https://img.shields.io/pypi/l/sdmxlib-rest)](LICENSE)
[![Docs](https://github.com/StatCat-Suite/sdmxlib-rest/actions/workflows/docs.yml/badge.svg)](https://statcat-suite.github.io/sdmxlib-rest/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

> **Note:** This library has been written extensively with AI assistance
> (Claude Code). Users who are not comfortable with AI-generated code should
> take that into account before adopting it.

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

That serves the structure, metadata and availability planes. The consumer
writes the `/data` route; see *Data-plane helpers* below.

## What you supply

Each catalog-specific behaviour is a Protocol on `CatalogRuntime`. A consumer
must implement `CatalogProvider`; every other seam has an inert default.

| Seam | Purpose | Default |
|---|---|---|
| `CatalogProvider` | how the registry is opened | **required** |
| `SpliceRegistry` | post-serialise JSON transforms | empty |
| `StructureCache` | cache rendered structure responses | never caches |
| `AvailabilityValues` | precomputed availability values, for unfiltered requests | always scans |
| `MetadataStore` | reference-metadata lookups | returns nothing |
| `ComponentFilterShaper` | rewrite a parsed filter before it is queried | identity |
| `MessageAnnotator` | pre-serialise message annotation; with `SupportsFingerprint`, annotations that change at runtime re-key the structure cache and ETags. `OverlayAnnotator` applies sdmxlib annotation overlays | no-op |

A consumer that installs none of the optional seams gets a correct service
that recomputes more than it needs to.

**The registry has to reach its own observations.** `/availability` reads rows
through sdmxlib's query DSL (`registry.obs(flow)`), which runs on the
registry's own connection and attaches the lake there. Open a bundle with
`LocalRegistry.bundle(path)`, or any opener that does the same (statcatlib's
`Catalog` does). The DSL sees only lakes attached to the registry's own
connection. This holds since 0.6.0; earlier versions ran `/availability` SQL on
`meta_conn`.

## What `create_app` gives you

Besides the routes, `create_app` assembles:

- **The middleware stack in a fixed order.** The chain has real
  adjacency constraints — `?lang=` has to rewrite the header before the ETag
  layer reads it; the version rewrite has to land before compression decides;
  CORS has to be outermost or preflights fall through to a 404 that browsers
  report as a CORS failure. Each constraint is documented at
  `_install_middleware`. Only successful responses carry an ETag and
  `Cache-Control`, so clients cache and revalidate successes only.
- **A 500 that names unwritable catalogue content.** sdmxlib
  refuses to serialise content no format can carry (a component with no
  concept, say). The response lists each artefact path and the rule it
  breaks, on the app and every mount.
- **Runtime sharing across mounts**, so a service exposing several surfaces
  (a canonical one plus compatibility variants) opens the registry once.
- **A multi-spec docs page**, built by hand because FastAPI's
  helper emits a single-spec page and Swagger UI renders nothing when given
  both `url` and `urls`.

## Data-plane helpers

The library owns data-plane *helpers* — key and query-parameter parsing,
projection, paging headers, binding-driven SQL fragments, and the
arrow→CSV/parquet encoders, all under `sdmxlib_rest.data`. Each consumer writes
its own `/data` handler around them.

The handler orchestrates response caching, partition pushdown and vendor
extensions, and every catalog shapes those differently: one catalog's hierarchy
descent and partition pruning fit that catalog alone.

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
