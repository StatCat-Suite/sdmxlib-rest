"""HEAD → GET rewriter so every GET route also serves HEAD.

FastAPI's ``@router.get(...)`` registers the route with ``methods=["GET"]``.
Starlette then answers HEAD requests to the same path with **405 Method
Not Allowed**, which breaks any client that HEADs before GETing —
``curl -I``, RFC 7232 validators, spec-conformance runners, and CDN
prefetchers all do this.

Rather than plumb ``methods=["GET", "HEAD"]`` through every route
declaration (dozens of decorators across ``routes/`` and ``v1/``), this
middleware sits above the routing layer and:

1. When ``scope["method"] == "HEAD"``, rewrites it to ``GET`` before
   dispatching. The middleware inside it (compression, version rewrite,
   ``?lang=`` override, ConditionalGet) sees GET and behaves as if HEAD
   support was native — including ``ConditionalGetMiddleware``'s 304
   short-circuit on ``If-None-Match`` hits. The perf log, any
   ``outer_middleware`` and CORS sit outside it and see the original HEAD.
2. Taps the ASGI ``send`` stream to drop every ``http.response.body``
   chunk, so the response reaches the wire as a valid HEAD (status +
   headers + empty body) per RFC 7231 §4.3.2.

The chunk-drop preserves ``more_body`` semantics — Starlette streams
multiple chunks with only the last carrying ``more_body=False``, and
we forward exactly one empty terminal frame at the end so the client
sees a clean end-of-stream. Content-Length, ETag, Last-Modified, all
other headers pass through untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send


class HeadMethodMiddleware:
    """Rewrite HEAD → GET on the way in, drop response body on the way out."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: D102 — ASGI entrypoint
        if scope["type"] != "http" or scope.get("method") != "HEAD":
            await self.app(scope, receive, send)
            return

        # Rewrite the method so downstream matchers dispatch to the
        # GET route. Copying scope is cheap (dict of ~10 keys) and
        # avoids mutating the object the outer runtime handed us.
        get_scope = dict(scope)
        get_scope["method"] = "GET"

        body_sent = False

        async def _send_head(message: Message) -> None:
            nonlocal body_sent
            if message["type"] == "http.response.start":
                await send(message)
                return
            if message["type"] == "http.response.body":
                # Coalesce multi-chunk bodies into one empty terminal frame.
                # Sending an empty body{more_body:False} once matches HEAD
                # semantics (no body, clean end-of-stream). Any subsequent
                # body chunks from downstream are dropped silently — they
                # cannot arrive after ``more_body=False`` in a well-formed
                # ASGI stream, but the guard is cheap.
                if body_sent:
                    return
                if not message.get("more_body", False):
                    body_sent = True
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                return
            await send(message)

        await self.app(get_scope, receive, _send_head)
