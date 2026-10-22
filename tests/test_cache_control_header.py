"""``Cache-Control`` emission on GET responses.

Browser heuristic-freshness was caching every ``/data`` / ``/structure``
response for hours-to-days after a deploy, so server-side fixes weren't
visible until a manual cache bust. The ``ConditionalGetMiddleware`` now
appends ``Cache-Control`` (default
``public, max-age=300, stale-while-revalidate=3600``) to both the 200
and 304 paths; the existing ETag plumbing turns every revalidation into
a cheap 304 round-trip, and the ``public, max-age`` policy lets any
HTTP-cache-aware client (browser HTTP cache, future CDN) reuse
responses within the freshness window without a server round-trip at all.

Tests cover the three settings the env knob exposes:
- default → emitted on 200 + 304
- empty string → header absent (pre-fix behaviour)
- custom value → passed through verbatim
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from sdmxlib_rest.middleware.etag import ConditionalGetMiddleware
from sdmxlib_rest.provider import OpenedCatalog
from sdmxlib_rest.runtime import CatalogRuntime

pytestmark = pytest.mark.unit


class _FakeProvider:
    """Opens a catalog with only the build identity the ETag middleware reads."""

    def open(self) -> OpenedCatalog:
        return OpenedCatalog(
            registry=None,  # type: ignore[arg-type] — middleware only reads build_id/built_at
            meta_conn=None,  # type: ignore[arg-type]
            build_id="testbuild",
            built_at="2026-06-16T00:00:00+00:00",
        )


def _open_runtime() -> CatalogRuntime:
    """A runtime pre-opened with a fake build identity (no real bundle)."""
    rt = CatalogRuntime(_FakeProvider())
    rt.ensure_open()
    return rt


def _app(cache_control: str) -> FastAPI:
    """Build a tiny FastAPI app wrapped in the middleware under test."""
    app = FastAPI()

    @app.get("/echo")
    def echo() -> PlainTextResponse:
        return PlainTextResponse("hello")

    @app.get("/broken")
    def broken() -> PlainTextResponse:
        return PlainTextResponse("catalogue content cannot be written", status_code=500)

    @app.get("/missing")
    def missing() -> PlainTextResponse:
        return PlainTextResponse("no such artefact", status_code=404)

    # Inject a pre-opened runtime so the middleware's build_id is non-empty and
    # the wrapped_send / ETag path actually runs (no real bundle needed).
    app.add_middleware(ConditionalGetMiddleware, runtime=_open_runtime(), cache_control=cache_control, version="test")
    return app


def test_default_emits_public_max_age_swr() -> None:
    """Default settings → both 200 and 304 carry the shared-cache-friendly header."""
    default = "public, max-age=300, stale-while-revalidate=3600"
    app = _app(default)
    client = TestClient(app)

    r = client.get("/echo")
    assert r.status_code == 200
    assert r.headers["cache-control"] == default
    etag = r.headers["etag"]

    # Revalidation request → 304 + same header
    r2 = client.get("/echo", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.headers["cache-control"] == default


def test_empty_value_omits_header() -> None:
    """An empty ``cache_control`` setting → header absent on both paths."""
    app = _app("")
    client = TestClient(app)

    r = client.get("/echo")
    assert r.status_code == 200
    assert "cache-control" not in r.headers, r.headers
    etag = r.headers["etag"]

    r2 = client.get("/echo", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert "cache-control" not in r2.headers, r2.headers


def test_custom_value_passes_through_verbatim() -> None:
    """Production-friendly ``public, max-age=86400`` flows untouched."""
    app = _app("public, max-age=86400")
    client = TestClient(app)

    r = client.get("/echo")
    assert r.headers["cache-control"] == "public, max-age=86400"

    r2 = client.get("/echo", headers={"If-None-Match": r.headers["etag"]})
    assert r2.status_code == 304
    assert r2.headers["cache-control"] == "public, max-age=86400"


def test_non_get_unaffected() -> None:
    """Middleware short-circuits non-GET requests; no header injected."""
    app = _app("public, max-age=300, stale-while-revalidate=3600")

    @app.post("/post")
    def post() -> PlainTextResponse:
        return PlainTextResponse("ok")

    client = TestClient(app)
    r = client.post("/post")
    assert r.status_code == 200
    assert "cache-control" not in r.headers, r.headers


@pytest.mark.parametrize("path", ["/broken", "/missing"])
def test_an_error_is_never_stamped_cacheable(path: str) -> None:
    """Only a 2xx is the build's answer; an error must not be cached or revalidated.

    Stamped, a 500 was publicly cacheable for minutes (an hour stale), and a
    client revalidating with its ETag got a 304, which tells it to keep
    serving the error.
    """
    client = TestClient(_app("public, max-age=300, stale-while-revalidate=3600"))

    r = client.get(path)

    assert r.status_code in {404, 500}
    assert "cache-control" not in r.headers, r.headers
    assert "etag" not in r.headers, r.headers
