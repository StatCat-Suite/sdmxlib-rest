"""The generic SDMX-REST application factory.

What a consumer actually has to get right to stand up an SDMX-REST service is
not the routes — those are already library-owned — but the *assembly*: the
middleware stack has an ordering invariant that is easy to violate and whose
violations are subtle (an ETag computed over compressed bytes still looks like
an ETag), the runtime has to be shared rather than rebuilt across mounted
surfaces or each one opens its own catalog, and the multi-spec docs page has to
be hand-rolled because FastAPI's helper can't emit one.

So this module owns the assembly and leaves every catalog-shaped decision to
the caller: which routers to include and in what order, what extra middleware
to layer, which sub-apps to mount, and what to warm at startup.

See :func:`create_app`. The ordering invariant is documented at its call to
``_install_middleware`` and is the single most important thing here — #1
tracks giving it regression coverage.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from attrs import field, frozen
from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger
from sdmxlib.model.validation import ValidationError

from sdmxlib_rest.middleware.compression import SelectiveGZipMiddleware, install_isal_zip_backend
from sdmxlib_rest.middleware.etag import ConditionalGetMiddleware
from sdmxlib_rest.middleware.head_method import HeadMethodMiddleware
from sdmxlib_rest.middleware.lang_override import LangOverrideMiddleware
from sdmxlib_rest.middleware.openapi_extensions import apply_vendor_markers
from sdmxlib_rest.middleware.perf_log import PerfLogMiddleware
from sdmxlib_rest.middleware.query_param_guard import reject_unknown_query_params
from sdmxlib_rest.middleware.version_token import SdmxVersionAdvertiseMiddleware
from sdmxlib_rest.responses.unwritable import unwritable_content_response
from sdmxlib_rest.routes import availability, metadata, structure
from sdmxlib_rest.runtime import CatalogRuntime, attach_runtime, share_runtime

# CORS response headers a browser-based SDMX client actually reads from script.
# Anything not listed here is hidden from JS on a cross-origin fetch.
_DEFAULT_EXPOSE_HEADERS: tuple[str, ...] = (
    "ETag",
    "Last-Modified",
    "Content-Disposition",
    "Vary",
    "Content-Encoding",
    # Range pagination — clients read `Content-Range` off the response;
    # `Accept-Ranges` rides along for feature detection.
    "Content-Range",
    "Accept-Ranges",
)

# POST is here for the `/body` form-fallback routes: clients POST an
# `application/x-www-form-urlencoded` body carrying the SDMX key when the GET
# URL would exceed an ingress limit. Without it the browser preflight rejects
# the fallback.
_DEFAULT_ALLOW_METHODS: tuple[str, ...] = ("GET", "HEAD", "OPTIONS", "POST")


@frozen
class RestSettings:
    """Configuration the generic app assembly needs — nothing catalog-specific.

    A consumer's own settings object will carry far more (bundle paths, log
    destinations, feature flags); map the relevant slice onto this.
    """

    title: str
    version: str
    description: str = ""
    cache_control: str = "public, max-age=300, stale-while-revalidate=3600"
    cors_allowed_origins: tuple[str, ...] = ("*",)
    cors_max_age: int = 3600
    # Level 1: SDMX-CSV is highly compressible (long format with repeated
    # dimension values — ~37x measured on census data), and a low level keeps
    # the CPU cost from eating the streaming throughput.
    gzip_minimum_size: int = 1024
    gzip_compress_level: int = 1
    canonical_spec_name: str = "SDMX-REST 2.2.2 (canonical)"


@frozen
class Mount:
    """A sub-app mounted under the main app.

    Mounted as a sub-app rather than a prefixed router so it publishes its own
    ``/openapi.json``, which is what lets the docs page offer a spec dropdown.
    The mount shares the parent's runtime — one catalog open serves every
    surface — and receives a copy of the parent's ``state``, because request
    dependencies resolve against ``request.app``, which is the *sub*-app for a
    request that lands inside a mount.

    ``spec_name`` labels it in the docs dropdown; ``None`` keeps it out.
    """

    path: str
    app: FastAPI
    spec_name: str | None = None


@frozen
class MiddlewareSpec:
    """A middleware class plus its keyword arguments, applied at the outer slot."""

    cls: type
    options: Mapping[str, Any] = field(factory=dict)


def library_routers() -> tuple[APIRouter, ...]:
    """The routers this library owns: structure, metadata, availability.

    Returned in a sensible default order. A consumer that interleaves its own
    routers should pass an explicit list to :func:`create_app` instead — route
    inclusion order is preserved verbatim and determines OpenAPI path order.
    """
    return (structure.router, metadata.router, availability.router)


def _install_unwritable_handler(app: object) -> None:
    """Answer sdmxlib's serialise-time ``ValidationError`` with a 500 naming artefact and rule (#10)."""
    add = getattr(app, "add_exception_handler", None)
    if add is not None:
        add(ValidationError, unwritable_content_response)


def create_app(
    runtime: CatalogRuntime,
    settings: RestSettings,
    *,
    routers: Sequence[APIRouter] = (),
    outer_middleware: Sequence[MiddlewareSpec] = (),
    mounts: Sequence[Mount] = (),
    state: Mapping[str, Any] | None = None,
    perf_sink: Callable[..., None] | None = None,
    on_startup: Callable[[FastAPI], Awaitable[None]] | None = None,
    on_shutdown: Callable[[], None] | None = None,
    root_payload: Mapping[str, Any] | None = None,
) -> FastAPI:
    """Assemble a FastAPI app serving SDMX-REST over ``runtime``'s catalog.

    The catalog is opened once, on startup, and shared with every mount. The
    returned app is ordinary FastAPI — a consumer is free to keep mounting
    static files or including further routers afterwards.

    Args:
        runtime: Carries the provider and every seam (splices, caches, filter
            shaper, annotator). Install those before calling; they are read per
            request, so later mutation works but is rarely what you want.
        settings: The generic slice of configuration — see :class:`RestSettings`.
        routers: Routers to include, **in order**. Order is preserved because it
            determines the order paths appear in the OpenAPI document. Use
            :func:`library_routers` for the standard trio, or interleave your own.
        outer_middleware: Extra middleware layered just inside CORS — i.e.
            outside everything else, so it observes the client's original
            request and the fully-encoded response. Logging taps belong here.
        mounts: Sub-apps to mount; each shares this app's runtime and state.
        state: Attributes to stamp on ``app.state`` and mirror onto every mount.
        perf_sink: Sink for the perf-log middleware; ``None`` makes it a
            zero-overhead no-op.
        on_startup: Awaited after the catalog opens — the place for cache
            warmers. Exceptions propagate and will fail startup, so a
            best-effort warmer must swallow its own errors.
        on_shutdown: Called after the app stops, instead of the default
            ``runtime.close()``. Supply one only if you need to close something
            more than the runtime; it takes over responsibility for doing so.
        root_payload: JSON body for ``GET /``. Defaults to a service identity.
    """
    # Routes DEFLATE through isa-l instead of stdlib zlib — same wire format,
    # ~2-3x throughput at the low compression levels we use. Idempotent.
    install_isal_zip_backend()

    @asynccontextmanager
    async def _lifespan(app_: FastAPI) -> AsyncGenerator[None]:
        logger.info("opening catalog")
        runtime.ensure_open()
        if on_startup is not None:
            await on_startup(app_)
        yield
        if on_shutdown is not None:
            on_shutdown()
        else:
            runtime.close()
        logger.info("catalog closed")

    app = FastAPI(
        title=settings.title,
        version=settings.version,
        description=settings.description,
        # Disabled so the multi-spec `/docs` page below can be served instead —
        # FastAPI's helper only emits a single-spec page.
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
        # Reject unknown query params with 400 rather than silently ignoring
        # them. App-wide; the dependency is one declared-set build per request.
        dependencies=[Depends(reject_unknown_query_params)],
    )
    _apply_state(app, state)
    attach_runtime(app, runtime)

    _install_middleware(app, runtime, settings, perf_sink=perf_sink, outer=outer_middleware)

    for router in routers:
        app.include_router(router)

    # Every app, mounts included: a mounted FastAPI app answers its own
    # unhandled exceptions with a bare 500 before the parent could see them.
    _install_unwritable_handler(app)
    for mount in mounts:
        _apply_state(mount.app, state)
        share_runtime(app, mount.app)
        _install_unwritable_handler(mount.app)
        app.mount(mount.path, mount.app)

    # Mark vendor params/paths with `x-vendor: true` so SDK generators can
    # filter them out for a spec-conformant client. Wraps each app's `openapi`
    # method; the marker pass mutates FastAPI's cached dict in place.
    _install_vendor_marker_hook(app)
    for mount in mounts:
        _install_vendor_marker_hook(mount.app)

    _install_docs(app, settings, mounts)

    payload = (
        dict(root_payload)
        if root_payload is not None
        else {
            "service": settings.title,
            "version": settings.version,
            "docs": "/docs",
        }
    )

    @app.get("/", include_in_schema=False)
    async def _root() -> JSONResponse:
        return JSONResponse(payload)

    return app


def _apply_state(app: FastAPI, state: Mapping[str, Any] | None) -> None:
    for key, value in (state or {}).items():
        setattr(app.state, key, value)


def _install_middleware(
    app: FastAPI,
    runtime: CatalogRuntime,
    settings: RestSettings,
    *,
    perf_sink: Callable[..., None] | None,
    outer: Sequence[MiddlewareSpec],
) -> None:
    """Build the middleware stack — the order here is an invariant.

    Starlette wraps inside-out: the LAST middleware added is the OUTERMOST and
    runs first on the way in, last on the way out. Reading the calls below
    top-to-bottom therefore walks from the routes outward. Each adjacency
    exists for a reason:

    - **LangOverride outside ConditionalGet** — ``?lang=`` rewrites the
      Accept-Language header, and that rewrite must land before the ETag layer
      reads the header, so ``?lang=fr`` hashes the same as the equivalent
      ``Accept-Language: fr`` request. This is the one adjacency here that a
      reorder provably breaks; ``test_a_lang_override_hashes_the_same_as_the``
      ``_equivalent_header`` is watched to fail on it.
    - **ConditionalGet innermost** — kept innermost, but *not* for the reason
      this docstring gave until #1 measured it. The claim was that the ETag
      "hashes the uncompressed body", so moving it outward would make ETags vary
      with compression. It does not hash the body at all: the signature is
      ``version|build_id|path|query|accept|resolved_lang``, computed before the
      app runs. So the ETag is compression-independent by construction, and no
      behavioural test fails when it moves outward *except* via the
      LangOverride relation above. If you need to move it, that is the only
      constraint the suite can currently defend.
    - **GZip outside the version-advertise rewrite** — the rewrite edits
      Content-Type, which the compression decision reads.
    - **HeadMethod inside the logging taps** — it rewrites HEAD to GET so the
      ETag / GZip / version layers see a normal GET and the 304 short-circuit
      behaves identically. The taps stay outside it so they still observe the
      client's *original* method.
    - **CORS outermost** — so it answers every OPTIONS preflight directly, even
      for paths the app doesn't route. A preflight that falls through to a 404
      reads to the browser as a CORS failure.
    """
    app.add_middleware(
        ConditionalGetMiddleware,
        runtime=runtime,
        cache_control=settings.cache_control,
        version=settings.version,
    )
    app.add_middleware(LangOverrideMiddleware)
    # sdmxlib's writers emit three-part media-type versions (`;version=2.0.0`);
    # SDMX-REST 2.2.2 §6.1 advertises the two-part form (`;version=2.0`), and
    # strict clients compare Content-Type literally. Rewrites only the version
    # segment on the way out; `ensure_acceptable` already treats both as
    # equivalent on the way in, so three-part clients keep working.
    app.add_middleware(SdmxVersionAdvertiseMiddleware)
    app.add_middleware(
        SelectiveGZipMiddleware,
        minimum_size=settings.gzip_minimum_size,
        compresslevel=settings.gzip_compress_level,
    )
    app.add_middleware(HeadMethodMiddleware)
    # Outermost of the taps: `wall_ms` then covers CORS, compression, ETag,
    # routing and body render. A `None` sink short-circuits at zero cost.
    app.add_middleware(PerfLogMiddleware, sink=perf_sink)
    for spec in outer:
        app.add_middleware(spec.cls, **dict(spec.options))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allowed_origins),
        # Required by CORS to coexist with `allow_origins=["*"]`. Revisit if
        # the service ever grows real auth.
        allow_credentials=False,
        allow_methods=list(_DEFAULT_ALLOW_METHODS),
        # Wildcard rather than a curated list: real clients send Cache-Control,
        # Pragma and custom X-* headers on top of the SDMX basics, and a
        # curated list turns any extra header into a preflight 400 that
        # browsers surface as "preflight doesn't pass access control check".
        # Safe because `allow_credentials=False`.
        allow_headers=["*"],
        expose_headers=list(_DEFAULT_EXPOSE_HEADERS),
        max_age=settings.cors_max_age,
    )


def _install_vendor_marker_hook(app: FastAPI) -> None:
    """Wrap ``app.openapi`` so vendor params/paths get ``x-vendor: true``.

    FastAPI memoises the schema on ``app.openapi_schema``; the dict is mutated
    in place once, and later calls return the already-marked cache.
    """
    original = app.openapi

    def _openapi_with_vendor() -> dict[str, Any]:
        spec = original()
        if not isinstance(spec, dict):
            return spec
        if not spec.get("x-vendor-markers-applied"):
            apply_vendor_markers(spec)
            spec["x-vendor-markers-applied"] = True
        return spec

    app.openapi = _openapi_with_vendor  # type: ignore[method-assign]


def _install_docs(app: FastAPI, settings: RestSettings, mounts: Sequence[Mount]) -> None:
    """Serve a hand-rolled Swagger UI page with a spec dropdown.

    FastAPI's ``get_swagger_ui_html`` always emits a single-spec ``url:`` line;
    Swagger UI given both ``url`` and ``urls`` renders "No API definition
    provided", so the page is built here instead. Three details are load-bearing
    for the dropdown to appear at all: the standalone-preset script, the
    ``StandaloneLayout`` layout, and ``SwaggerUIStandalonePreset`` in ``presets``.
    """
    specs = [(settings.canonical_spec_name, "/openapi.json")]
    specs.extend((m.spec_name, f"{m.path}/openapi.json") for m in mounts if m.spec_name)
    urls = ",\n".join(f'        {{name: "{name}", url: "{url}"}}' for name, url in specs)

    html = (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "<head>\n"
        f"  <title>{app.title} — API docs</title>\n"
        '  <link rel="stylesheet" type="text/css" '
        'href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">\n'
        "</head>\n"
        "<body>\n"
        '  <div id="swagger-ui"></div>\n'
        '  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>\n'
        '  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-standalone-preset.js"></script>\n'
        "  <script>\n"
        "    window.ui = SwaggerUIBundle({\n"
        '      dom_id: "#swagger-ui",\n'
        '      layout: "StandaloneLayout",\n'
        "      deepLinking: true,\n"
        "      showExtensions: true,\n"
        "      showCommonExtensions: true,\n"
        "      urls: [\n"
        f"{urls}\n"
        "      ],\n"
        f'      "urls.primaryName": "{settings.canonical_spec_name}",\n'
        # Drop the bottom-right validator badge — it pings validator.swagger.io,
        # which can't reach a private deployment, so it always renders a spurious
        # warning.
        "      validatorUrl: null,\n"
        "      presets: [\n"
        "        SwaggerUIBundle.presets.apis,\n"
        "        SwaggerUIStandalonePreset\n"
        "      ]\n"
        "    });\n"
        "  </script>\n"
        "</body>\n"
        "</html>\n"
    )

    @app.get("/docs", include_in_schema=False)
    async def _swagger_ui() -> HTMLResponse:
        return HTMLResponse(html)


__all__ = ["MiddlewareSpec", "Mount", "RestSettings", "create_app", "library_routers"]
