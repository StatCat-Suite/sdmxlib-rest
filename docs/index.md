# sdmxlib-rest

sdmxlib-rest serves an [sdmxlib](https://statcat-suite.github.io/sdmxlib/)
`LocalRegistry` over [SDMX-REST 2.2.2](https://github.com/sdmx-twg/sdmx-rest).
It owns the parts of an SDMX-REST service that every catalog shares: the URL
grammar, content negotiation, localisation, conditional GET, compression and
response assembly. A consumer supplies how the registry is opened and,
optionally, catalog-specific behaviour through a set of seams.

```python
from sdmxlib_rest.app import RestSettings, create_app, library_routers
from sdmxlib_rest.runtime import CatalogRuntime

runtime = CatalogRuntime(BundleProvider("path/to/bundle"))
app = create_app(
    runtime,
    RestSettings(title="my-sdmx-api", version="1.0.0"),
    routers=list(library_routers()),
)
```

That app serves the structure, metadata and availability routes.
[Serving a bundle](serving.md) shows `BundleProvider` and how to run it.

## What it serves

| Route | Answers |
| --- | --- |
| `/structure/{structureType}/{agencyID}/{resourceID}/{version}` | Structural artefacts, with `detail` and `references` |
| `/structure/{itemSchemeType}/{agencyID}/{resourceID}/{version}/{itemID}` | Items of an item scheme |
| `/metadata/structure/…`, `/metadata/metadataflow/…`, `/metadata/metadataset/…` | Reference metadata |
| `/availability/{context}/{agencyID}/{resourceID}/{version}/{key}/{componentID}` | The values present in the data, with `mode=exact` or `mode=available` |

Structures are written as SDMX-JSON 2.0, SDMX-ML 2.1 or SDMX-ML 3.0, chosen by
the `Accept` header. The `/data` route belongs to the consumer, which builds
it from the library's [data-plane helpers](data-plane.md).

## What a consumer supplies

Everything catalog-specific is a Protocol carried on `CatalogRuntime`, and
only the provider is required. Every other seam has a default that gives a
correct service which recomputes more than it needs to.

| Seam | Purpose | Default |
| --- | --- | --- |
| `CatalogProvider` | Opens the registry | required |
| `SpliceRegistry` | Transforms structure JSON after it is written | empty |
| `StructureCache` | Caches rendered structure responses | caches nothing |
| `AvailabilityValues` | Supplies precomputed availability values | always scans |
| `MetadataStore` | Finds the metadata sets attached to an artefact | finds none |
| `ComponentFilterShaper` | Rewrites a parsed filter before it is queried | identity |
| `MessageAnnotator` | Adds annotations to a structure message before it is written | none |

[Seams](seams.md) describes each one.

## Where it sits in the suite

sdmxlib-rest depends on sdmxlib and on nothing else in the suite. It serves a
bundle that statcatlib builds and sdmxlib defines; the
[suite architecture](https://statcat-suite.github.io/architecture/) describes
the bundle and its lifecycle. A test fails the build if the library imports
statcatlib or a consumer.

## Install

```bash
pip install sdmxlib-rest
```

It needs Python 3.14 or later. Each release pins an sdmxlib minor version;
the [changelog](changelog.md) lists the range and what changed.
