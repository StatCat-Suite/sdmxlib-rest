"""A second consumer can stand up an SDMX-REST service through the library alone (#35).

This was the definition-of-done for the extraction, and the only test that
would catch it being fake: a suite driving the first consumer's app would keep
passing even if ``sdmxlib_rest.create_app`` still secretly needed something
shaped like it.

So the provider below is deliberately minimal — an in-memory DuckDB and an
empty catalog, no bundle, no statcatlib, no consumer import — and the assertions are
about *assembly*, not data: does the app boot, is the middleware stack in the
invariant order, do mounted surfaces share the one catalog, does the docs page
list every spec, do the startup/shutdown hooks fire.

The middleware-order assertion is the one to keep. It fails loudly if someone
reorders the stack; `tests/test_middleware_invariants.py` (#1) now covers what
the order is *for*, and is where a reorder gets diagnosed rather than merely
detected.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
import pytest
import sdmxlib as sl
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from sdmxlib_rest.app import MiddlewareSpec, Mount, RestSettings, create_app, library_routers
from sdmxlib_rest.middleware.compression import SelectiveGZipMiddleware
from sdmxlib_rest.middleware.etag import ConditionalGetMiddleware
from sdmxlib_rest.middleware.head_method import HeadMethodMiddleware
from sdmxlib_rest.middleware.lang_override import LangOverrideMiddleware
from sdmxlib_rest.middleware.perf_log import PerfLogMiddleware
from sdmxlib_rest.middleware.version_token import SdmxVersionAdvertiseMiddleware
from sdmxlib_rest.provider import OpenedCatalog
from sdmxlib_rest.runtime import CatalogRuntime

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.unit


class ToyProvider:
    """The smallest thing satisfying ``CatalogProvider`` — no bundle, no statcatlib.

    ``opens`` counts calls so the mount-sharing test can assert one open serves
    every surface rather than each mount opening its own.
    """

    def __init__(self) -> None:
        self.opens = 0
        self.closes = 0
        # A path that never gets written — the catalog is only constructed, not
        # populated. These tests assert on assembly, not on data.
        self._uri = Path(tempfile.mkdtemp()) / "meta.duckdb"

    def open(self) -> OpenedCatalog:
        self.opens += 1
        conn = duckdb.connect()
        return OpenedCatalog(
            registry=sl.LocalRegistry(self._uri).open(),
            meta_conn=conn,
            manifest=None,
            build_id="toybuild",
            built_at="2026-01-01T00:00:00Z",
            on_close=self._closed,
        )

    def _closed(self) -> None:
        self.closes += 1


def _settings(**over: Any) -> RestSettings:
    base: dict[str, Any] = {"title": "toy-sdmx-api", "version": "0.1.0"}
    base.update(over)
    return RestSettings(**base)


def _spec_paths(app: FastAPI) -> list[str]:
    """Published OpenAPI paths, in document order.

    Asserted on instead of ``app.routes`` because route storage is a framework
    internal that has already shifted once — starlette 1.4 nests included
    routers under an ``_IncludedRouter`` rather than flattening them onto
    ``app.routes`` — whereas the spec is the contract consumers actually see.
    """
    return list(app.openapi()["paths"])


def _build(**kwargs: Any) -> tuple[FastAPI, ToyProvider]:
    provider = ToyProvider()
    runtime = CatalogRuntime(provider)
    kwargs.setdefault("routers", list(library_routers()))
    return create_app(runtime, _settings(), **kwargs), provider


@pytest.fixture
def toy() -> Iterator[tuple[TestClient, ToyProvider]]:
    app, provider = _build()
    with TestClient(app) as client:
        yield client, provider


def test_a_toy_provider_boots_a_working_service(toy: tuple[TestClient, ToyProvider]) -> None:
    """The app starts, opens the catalog once, and serves its identity."""
    client, provider = toy
    assert provider.opens == 1

    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"service": "toy-sdmx-api", "version": "0.1.0", "docs": "/docs"}


def test_library_routes_are_published(toy: tuple[TestClient, ToyProvider]) -> None:
    """Structure, metadata and availability all reach the published spec."""
    app = toy[0].app
    assert isinstance(app, FastAPI)
    paths = _spec_paths(app)
    assert any(p.startswith("/structure/") for p in paths)
    assert any(p.startswith("/metadata/") for p in paths)
    assert any(p.startswith("/availability/") for p in paths)


def test_middleware_stack_is_in_the_invariant_order() -> None:
    """The documented chain, outermost first. Reordering this is a silent bug."""
    app, _ = _build()
    # `user_middleware` is stored outermost-first (reverse of add order).
    assert [m.cls for m in app.user_middleware] == [
        CORSMiddleware,
        PerfLogMiddleware,
        HeadMethodMiddleware,
        SelectiveGZipMiddleware,
        SdmxVersionAdvertiseMiddleware,
        LangOverrideMiddleware,
        ConditionalGetMiddleware,
    ]


def test_outer_middleware_lands_just_inside_cors() -> None:
    """A consumer tap observes the original request but never intercepts preflight."""

    class _Tap(BaseHTTPMiddleware):
        pass

    app, _ = _build(outer_middleware=[MiddlewareSpec(_Tap)])
    classes = [m.cls for m in app.user_middleware]
    assert classes[0] is CORSMiddleware, "CORS must stay outermost"
    assert classes[1] is _Tap, "consumer middleware sits directly inside CORS"


def test_mounts_share_one_catalog_open() -> None:
    """Every surface resolves to the parent's runtime — one open, not one per mount."""
    provider = ToyProvider()
    runtime = CatalogRuntime(provider)
    sub = FastAPI()
    app = create_app(
        runtime,
        _settings(),
        routers=list(library_routers()),
        mounts=[Mount("/v1", sub, "Legacy")],
    )
    with TestClient(app):
        assert provider.opens == 1
        assert sub.state.rest_runtime is app.state.rest_runtime


def test_state_is_mirrored_onto_mounts() -> None:
    """Request deps resolve against ``request.app`` — the sub-app for a mounted path."""
    sub = FastAPI()
    provider = ToyProvider()
    app = create_app(
        CatalogRuntime(provider),
        _settings(),
        state={"settings": "SENTINEL"},
        mounts=[Mount("/v1", sub)],
    )
    assert app.state.settings == "SENTINEL"
    assert sub.state.settings == "SENTINEL"


def test_docs_page_lists_every_spec() -> None:
    """The dropdown needs the canonical spec plus one entry per named mount."""
    provider = ToyProvider()
    app = create_app(
        CatalogRuntime(provider),
        _settings(canonical_spec_name="Canonical"),
        routers=list(library_routers()),
        mounts=[Mount("/v1", FastAPI(), "Legacy"), Mount("/hidden", FastAPI())],
    )
    with TestClient(app) as client:
        html = client.get("/docs").text
    assert '{name: "Canonical", url: "/openapi.json"}' in html
    assert '{name: "Legacy", url: "/v1/openapi.json"}' in html
    assert '"urls.primaryName": "Canonical"' in html
    # A mount with no spec_name stays out of the dropdown.
    assert "/hidden/openapi.json" not in html
    # Load-bearing for the dropdown to render at all.
    assert "StandaloneLayout" in html
    assert "SwaggerUIStandalonePreset" in html


def test_startup_and_shutdown_hooks_fire_around_the_catalog() -> None:
    """``on_startup`` runs with the catalog already open; ``on_shutdown`` replaces close."""
    provider = ToyProvider()
    runtime = CatalogRuntime(provider)
    events: list[str] = []

    async def _startup(app_: FastAPI) -> None:
        events.append(f"startup:open={runtime.is_open}")

    def _shutdown() -> None:
        events.append("shutdown")
        runtime.close()

    app = create_app(runtime, _settings(), on_startup=_startup, on_shutdown=_shutdown)
    with TestClient(app):
        pass

    assert events == ["startup:open=True", "shutdown"]
    assert provider.closes == 1


def test_default_shutdown_closes_the_runtime() -> None:
    """With no hook supplied the library closes the catalog itself."""
    app, provider = _build()
    with TestClient(app):
        pass
    assert provider.closes == 1


def test_router_order_is_preserved() -> None:
    """Inclusion order drives OpenAPI path order, so it must not be reshuffled."""
    first = APIRouter()
    second = APIRouter()

    @first.get("/aaa")
    async def _a() -> dict[str, str]:
        return {}

    @second.get("/zzz")
    async def _z() -> dict[str, str]:
        return {}

    # Deliberately reverse-alphabetical to prove order comes from the caller.
    app, _ = _build(routers=[second, first])
    paths = _spec_paths(app)
    assert paths.index("/zzz") < paths.index("/aaa")


def test_unknown_query_params_are_rejected(toy: tuple[TestClient, ToyProvider]) -> None:
    """The app-wide guard is wired by the factory, not by each consumer."""
    resp = toy[0].get("/structure/dataflow/all/all/all", params={"bogusParam": "1"})
    assert resp.status_code == 400
