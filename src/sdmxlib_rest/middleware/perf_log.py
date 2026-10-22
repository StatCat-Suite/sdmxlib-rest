r"""Per-request performance log (one JSONL line per HTTP request).

Wraps every non-OPTIONS request with a wall-clock timer, taps the ASGI
``send`` stream to count response bytes (works for streaming responses,
which ``BaseHTTPMiddleware`` would collapse), and emits a single record
covering path + status + duration + wire-size + a few caching-relevant
request headers.

Positioned OUTERMOST so it measures the whole middleware stack — CORS,
compression, ETag, negotiation, and the route body all fold into
``wall_ms``. OPTIONS preflights are skipped so the CORS chatter doesn't
flood the log.

Route via the sink argument to ``PerfLogMiddleware(app, sink=<callable>)``.
The default wiring in ``app.py`` binds it to a loguru sink pointed at
``settings.perf_log_path`` when that setting is non-empty. Point it at
``/tmp/perf.jsonl`` and inspect with ``jq``:

.. code-block:: shell

   jq -r 'select(.kind=="perf") | [.wall_ms, .bytes, .path] | @tsv' /tmp/perf.jsonl \\
       | sort -rn | head -20

Fields captured — deliberately narrow:

- ``method``, ``path`` — request identity (no query string, so URLs with
  keys don't blow up cardinality on grep-and-count histograms).
- ``query_len`` — just the byte length; useful for spotting outsized
  filter expressions without leaking their contents.
- ``status`` — HTTP status code (200/206/304/404/etc.).
- ``wall_ms`` — total time from request headers received to response
  body fully sent, measured with ``time.perf_counter``.
- ``bytes`` — sum of body chunks; matches the wire size AFTER
  compression / chunking / whatever the middleware stack applied.
- ``content_type`` / ``content_encoding`` — response envelope class
  (json vs csv vs parquet; gzip / br / identity). Answers "was this
  streamed compressed."
- ``has_range`` / ``has_if_none_match`` — did the client engage the two
  cache-friendly paths. Lets us track "how much traffic actually
  ranges" and "conditional-GET hit ratio".

Nothing sensitive in the record — no cookies, no Authorization, no
query string, no body. The path itself may contain SDMX keys (which
are catalog identifiers, not personal data).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _Sink(Protocol):
    """Minimal shape for the perf log sink — a loguru-style structured info call.

    Callables passed as ``sink`` receive one positional string (a fixed
    ``"perf"`` message tag so a downstream JSON sink filter can pick these
    lines out) and structured kwargs — the payload keys documented in the
    module docstring.
    """

    def __call__(self, msg: str, /, **kwargs: Any) -> None: ...


class PerfLogMiddleware:
    """Emit one structured record per HTTP request (skipped for OPTIONS)."""

    def __init__(self, app: ASGIApp, sink: _Sink | None = None) -> None:
        self.app = app
        self.sink = sink

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: C901, D102 — ASGI entrypoint; complexity is inherent to tapping http.response.start/body
        # HTTP-only + sink-guarded — no-op for lifespan / websocket / when disabled.
        if scope["type"] != "http" or self.sink is None:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        # CORS preflight OPTIONS: high volume, no route work, uninteresting for perf.
        # /health: health checks hit this every few seconds. In a first
        # session's worth of records they drown out real traffic 10:1 — skip.
        if method == "OPTIONS" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return

        t0 = time.perf_counter()
        status = 0
        content_type = "-"
        content_encoding = "identity"
        bytes_out = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status, content_type, content_encoding, bytes_out
            if message["type"] == "http.response.start":
                status = int(message.get("status", 0))
                for k, v in message.get("headers", []):
                    key = k.lower()
                    if key == b"content-type":
                        content_type = v.decode("latin-1", errors="replace")[:64]
                    elif key == b"content-encoding":
                        content_encoding = v.decode("latin-1", errors="replace")[:16]
            elif message["type"] == "http.response.body":
                body = message.get("body")
                if body:
                    bytes_out += len(body)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            wall_ms = (time.perf_counter() - t0) * 1000.0
            # Scope's ``headers`` is a list[(bytes, bytes)] — cheap sniffs
            # for the two request-header signals we care about. Full name
            # match; case-insensitive on the key side (ASGI lowercases).
            has_range = False
            has_if_none_match = False
            for k, _ in scope["headers"]:
                if k == b"range":
                    has_range = True
                elif k == b"if-none-match":
                    has_if_none_match = True
                if has_range and has_if_none_match:
                    break
            self.sink(
                "perf",
                method=method,
                path=scope.get("path", ""),
                query_len=len(scope.get("query_string", b"")),
                status=status,
                wall_ms=round(wall_ms, 2),
                bytes=bytes_out,
                content_type=content_type,
                content_encoding=content_encoding,
                has_range=has_range,
                has_if_none_match=has_if_none_match,
            )
