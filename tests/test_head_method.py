"""HEAD → GET rewriter (routes registered as GET must also serve HEAD).

Guards two invariants:

1. Every FastAPI ``@router.get(...)`` route answers HEAD identically to GET
   — same status, same headers — but with an empty body.
2. Downstream middleware (ETag) sees the rewritten GET and applies its
   normal 304 short-circuit on ``If-None-Match`` matches. HEAD callers
   must get the same 304 GET would produce.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from sdmxlib_rest.middleware.head_method import HeadMethodMiddleware

pytestmark = pytest.mark.unit


def _make_app_with_head_support() -> FastAPI:
    app = FastAPI()

    @app.get("/thing")
    def thing() -> PlainTextResponse:
        return PlainTextResponse(
            "hello world",
            headers={"X-Custom": "yes", "ETag": '"deadbeef"'},
        )

    app.add_middleware(HeadMethodMiddleware)
    return app


def test_head_returns_200_and_headers_but_no_body() -> None:
    """Without the middleware Starlette would 405 HEAD. With it, HEAD == GET-headers, empty body."""
    app = _make_app_with_head_support()
    with TestClient(app) as client:
        get_resp = client.get("/thing")
        head_resp = client.head("/thing")
    assert get_resp.status_code == 200
    assert head_resp.status_code == 200
    # Headers identical to GET (down to the custom + ETag we set).
    assert head_resp.headers.get("x-custom") == "yes"
    assert head_resp.headers.get("etag") == '"deadbeef"'
    # No body on HEAD.
    assert head_resp.content == b""
    assert get_resp.content == b"hello world"


def test_head_without_middleware_gets_405() -> None:
    """Guard the premise: FastAPI's default behaviour for HEAD on a GET-only route is 405."""
    app = FastAPI()

    @app.get("/thing")
    def thing() -> PlainTextResponse:
        return PlainTextResponse("hi")

    with TestClient(app) as client:
        r = client.head("/thing")
    # If Starlette starts auto-adding HEAD later, this test breaks
    # loudly — that's fine, it means the middleware is redundant and
    # can be removed. Until then it's what motivates the middleware.
    assert r.status_code == 405


def test_head_still_405s_on_actually_unsupported_paths() -> None:
    """Routes registered as POST-only (no GET) are still 405 for HEAD — we haven't hidden real errors."""
    app = FastAPI()

    @app.post("/write")
    def write() -> PlainTextResponse:
        return PlainTextResponse("ok")

    app.add_middleware(HeadMethodMiddleware)
    with TestClient(app) as client:
        r = client.head("/write")
    assert r.status_code == 405


def test_non_head_requests_pass_through_untouched() -> None:
    """GET/POST/etc reach the app unmodified — the middleware only intercepts HEAD."""
    app = FastAPI()

    @app.get("/thing")
    def thing() -> PlainTextResponse:
        return PlainTextResponse("body")

    @app.post("/thing")
    def thing_post() -> PlainTextResponse:
        return PlainTextResponse("posted")

    app.add_middleware(HeadMethodMiddleware)
    with TestClient(app) as client:
        assert client.get("/thing").text == "body"
        assert client.post("/thing").text == "posted"


def test_head_streaming_response_body_is_dropped_cleanly() -> None:
    """Multi-chunk streaming responses still emit an empty terminal HEAD body."""
    from fastapi.responses import StreamingResponse

    app = FastAPI()

    @app.get("/stream")
    def stream() -> StreamingResponse:
        def _iter() -> object:
            yield b"chunk1"
            yield b"chunk2"
            yield b"chunk3"

        return StreamingResponse(_iter(), media_type="text/plain")

    app.add_middleware(HeadMethodMiddleware)
    with TestClient(app) as client:
        r = client.head("/stream")
    assert r.status_code == 200
    assert r.content == b""
