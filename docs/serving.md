# Serving a bundle

A service needs three things: a provider that opens the registry, a
`CatalogRuntime` that holds it with the seams, and the app `create_app`
assembles.

## The provider

`CatalogProvider` has one method, `open`, which returns an `OpenedCatalog`.
This provider opens a publication of a bundle with sdmxlib:

```python
import sdmxlib as sl
from sdmxlib_rest.provider import OpenedCatalog


class BundleProvider:
    def __init__(self, publication: str) -> None:
        self._publication = publication

    def open(self) -> OpenedCatalog:
        registry = sl.LocalRegistry.bundle(self._publication)
        return OpenedCatalog(
            registry=registry,
            meta_conn=registry.connection,
            on_close=registry.close,
        )
```

A bundle is a directory of publications, `<bundle>/<token>/`, over the
`data/` they share, and the provider opens one publication.
`LocalRegistry.bundle` opens it read-only and returns an `sl.BundleRegistry`,
so any number of processes can serve the same publication. It refuses a
directory without `bundle.manifest.json` with `ManifestError`, and a store
schema it does not read with `StoreSchemaError`. The manifest's `token`, the
publication's directory name, is what every cache and the ETag key on.

**The registry must reach its own observations.** `/availability` reads rows
through sdmxlib's query DSL (`registry.obs(flow)`), which runs on the
registry's own connection and attaches the lake there. `LocalRegistry.bundle`
does this, and so does statcatlib's `Catalog`. A lake attached only to some
other connection is invisible to `/availability`.

`meta_conn` is the DuckDB connection the seams receive, for consumers whose
seams read their own tables. `OpenedCatalog` also takes `build_id` and
`built_at`, which identify the build in logs and `Last-Modified`, and `token`,
which overrides the manifest's token when a provider keys on something else.

## The app

```python
from sdmxlib_rest.app import RestSettings, create_app, library_routers
from sdmxlib_rest.runtime import CatalogRuntime

runtime = CatalogRuntime(BundleProvider("path/to/bundle/<token>"))
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
| `title`, `version`, `description` | required, required, `""` | The OpenAPI document and the `GET /` payload |
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
  `GET /`.

The middleware between those slots is fixed, in the order
[HTTP behaviour](http.md#middleware-order) describes.
