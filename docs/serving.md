# Serving an edition

A service needs three things: a provider that opens the registry, a
`CatalogRuntime` that holds it with the seams, and the app `create_app`
assembles.

## The provider

`CatalogProvider` has one method, `open`, which returns an `OpenedCatalog`.
This provider opens one edition of a product with sdmxlib:

```python
import sdmxlib as sl
from sdmxlib_rest.provider import OpenedCatalog


class EditionProvider:
    def __init__(self, edition: str) -> None:
        self._edition = edition

    def open(self) -> OpenedCatalog:
        registry = sl.LocalRegistry.edition(self._edition)
        return OpenedCatalog(
            registry=registry,
            meta_conn=registry.connection,
            on_close=registry.close,
        )
```

A product is a directory of editions, `<product>/<edition_id>/`, over the
`data/` they share, and the provider opens one edition. An edition holds
`manifest.json`, `metadata.duckdb` and `tables.ducklake`.
`LocalRegistry.edition` opens it read-only and returns an `sl.EditionRegistry`,
so any number of processes can serve the same edition. It raises
`ManifestError` for a directory without `manifest.json` or with a manifest
format other than 3.x, so every directory sdmxlib 0.79 or earlier wrote is
refused, and `StoreSchemaError` for a store schema it does not read. The manifest's `edition_id`, the edition's directory name,
is what every cache and the ETag key on.

Which edition of each product is live is a deployment's decision. sdmxlib
records it in the Inventory (`sdmxlib.inventory`); a provider that reads it
passes the live edition's directory to `LocalRegistry.edition`, with the live
entry's `manifest_sha256` (`LocalRegistry.edition(path,
manifest_sha256=live.manifest_sha256)`), so that a directory holding another
manifest than the one the Inventory records raises `ManifestError`.
sdmxlib-rest requires sdmxlib 0.85 or later. sdmxlib 0.85 falls back to
`catalog.json` and `inventory/HISTORY` when `CURRENT` is missing, malformed or
names a root that fails to verify. sdmxlib 0.82 and later read a
root of any event kind within format 1.x, including `kind: describe` and kinds
added in later 1.x releases. sdmxlib 0.81 and earlier skip a root whose kind
they do not know and stay on the version before it.

**The registry must reach its own observations.** `/availability` reads rows
through sdmxlib's query DSL (`registry.obs(flow)`), which runs on the
registry's own connection and attaches the lake there. `LocalRegistry.edition`
does this, and so does statcatlib's `Catalog`. A lake attached only to some
other connection is invisible to `/availability`.

`meta_conn` is the DuckDB connection the seams receive, for consumers whose
seams read their own tables. `OpenedCatalog` also takes `build_id` and
`built_at`, which identify the build in logs and `Last-Modified`, and `token`,
which overrides the manifest's edition id when a provider keys on something else.

## The app

```python
from sdmxlib_rest.app import RestSettings, create_app, library_routers
from sdmxlib_rest.runtime import CatalogRuntime

runtime = CatalogRuntime(EditionProvider("path/to/<product>/<edition_id>"))
app = create_app(
    runtime,
    RestSettings(title="my-sdmx-api", version="1.0.0"),
    routers=list(library_routers()),
)
```

Run it with any ASGI server:

```bash
uvicorn myservice:app
```

The registry opens once, at startup, and closes at shutdown. Then:

```bash
curl -H 'Accept: application/vnd.sdmx.structure+json;version=2.0' \
  'http://localhost:8000/structure/dataflow/all/all/latest?detail=allstubs'
```

lists every dataflow, and `GET /docs` serves the OpenAPI page.

## Settings

`RestSettings` holds the configuration the assembly needs:

| Field | Default | Sets |
| --- | --- | --- |
| `title`, `version`, `description` | required, required, `""` | The OpenAPI document and the default `GET /` payload |
| `cache_control` | `public, max-age=300, stale-while-revalidate=3600` | `Cache-Control` on successful responses |
| `cors_allowed_origins` | `("*",)` | CORS origins |
| `cors_max_age` | `3600` | How long a browser keeps a preflight answer |
| `gzip_minimum_size`, `gzip_compress_level` | `1024`, `1` | When and how hard to gzip |

## What else `create_app` takes

- **`routers`**, included in the order given, which is also the order of the
  OpenAPI paths. `library_routers()` returns structure, metadata and
  availability; a consumer adds its own `/data` router to the list.
- **`mounts`**: sub-apps, such as a compatibility surface, that share the
  parent's runtime, so one registry serves every surface. Each publishes its
  own `/openapi.json`, and `/docs` offers a dropdown across them.
- **`outer_middleware`**: middleware placed just inside CORS, where it sees
  the client's request and the encoded response. Logging taps go here.
- **`on_startup`**: awaited after the registry opens; cache warmers go here.
- **`perf_sink`**: receives one record per request from the performance log.
- **`state`** and **`root_payload`**: values for `app.state` and the body of
  `GET /`. The body defaults to `service`, `version` and `docs`. `create_app`
  adds `sdmxlib_version`, the installed sdmxlib version, to either body, and
  overwrites a `sdmxlib_version` key in `root_payload`. A release job reads
  this field from each pod before it expires old Inventory roots.

The middleware between those slots is fixed, in the order
[HTTP behaviour](http.md#middleware-order) describes.
