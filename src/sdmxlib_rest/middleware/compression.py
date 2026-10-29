"""Compression: isa-l gzip middleware + isa-l deflate for csv.zip output.

Two behaviours stacked into one ASGI component:

1. **isa-l gzip backend.** `IsalGZipMiddleware` is a drop-in for
   Starlette's `starlette.middleware.gzip.GZipMiddleware` that
   swaps stdlib ``zlib`` for isa-l's ``compressobj``. Wire
   format is unchanged (clients still see ``Content-Encoding: gzip``)
   but the deflate step is 3-5x faster at low levels. Diagnostic on the
   medium census slice showed stdlib gzip at level 1 was ~30% of total
   request time; isa-l makes it nearly free, lifting the next bottleneck
   (CSV serialization in sdmxlib) into view.

2. **Skip-list for already-compressed bodies.** `SelectiveGZipMiddleware`
   wraps the isa-l middleware and tags responses whose base media type
   is in `SKIP_GZIP_CONTENT_TYPES` (``application/vnd.apache.parquet``,
   ``application/x-parquet``, ``application/octet-stream``,
   ``application/zstd``, ``application/zip``) with
   ``Content-Encoding: identity`` *before* the gzip layer sees them. The
   gzip responder short-circuits on ``content-encoding in headers``, so
   the tagged responses go through untouched.

The responders are Starlette's, so their built-in exclusions apply too.
They pass through uncompressed any response that already carries
``Content-Encoding``, any 206 partial response, any body under
``minimum_size``, and any type in Starlette's
``DEFAULT_EXCLUDED_CONTENT_TYPES``: gzip, zip, grpc, avif, gif, jpeg,
png and webp images, ``audio/*``, ``video/*``, woff fonts and
``text/event-stream``.
"""

from __future__ import annotations

import zipfile
import zlib
from typing import Any, override

from isal import isal_zlib
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.gzip import GZipResponder, IdentityResponder
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def install_isal_zip_backend() -> None:
    """Make `zipfile` (and via re-export, `zipstream-ng`) use isa-l for DEFLATE.

    zipstream-ng imports ``_get_compressor`` from stdlib `zipfile`,
    which in turn calls ``zlib.compressobj(...)``. We replace that with a
    drop-in that returns an isa-l compressor: same API, raw-deflate wire
    output (stdlib ``zlib.decompressobj(-15)`` reads it back), ~2-3x
    faster at low compression levels.

    Idempotent. Call once at app startup; module-level patch.
    """
    if getattr(zipfile, "_sdmxlib_rest_isal_patched", False):
        return
    original = zipfile._get_compressor  # pyrefly: ignore[missing-attribute]

    # The return type is `Any` because the DEFLATED branch yields
    # an `isal_zlib.Compress` (a C extension type with no stub), and the
    # fallback returns whatever the untyped stdlib hook returns for bz2/lzma.
    # No narrower annotation is true of both.
    def _isal_get_compressor(compress_type: int, compresslevel: int | None = None) -> Any:  # noqa: ANN401
        if compress_type == zipfile.ZIP_DEFLATED:
            # isa-l's level range is 0-3 (vs zlib's 0-9). Clamp without
            # silently changing meaning — level 1 stays as the "fast" mode.
            lvl = compresslevel if compresslevel is not None else 1
            lvl = max(0, min(3, lvl))
            return isal_zlib.compressobj(lvl, isal_zlib.DEFLATED, -15)
        return original(compress_type, compresslevel)

    zipfile._get_compressor = _isal_get_compressor  # pyrefly: ignore[missing-attribute]
    # Refresh the import zipstream-ng captured at its own import time.
    import zipstream.ng  # noqa: PLC0415

    zipstream.ng._get_compressor = _isal_get_compressor
    zipfile._sdmxlib_rest_isal_patched = True  # pyrefly: ignore[missing-attribute]


SKIP_GZIP_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.apache.parquet",
        "application/x-parquet",
        "application/octet-stream",
        "application/zstd",
        "application/zip",
    }
)


class _IsalGZipResponder(GZipResponder):
    """`GZipResponder` that compresses with isa-l instead of zlib.

    Starlette's responder funnels every body chunk through
    ``self.compressor``. isa-l exposes the same ``compress``/``flush`` API
    and the same gzip wire format at ``16 + MAX_WBITS``, so replacing that
    one property is the entire substitution — buffering, threading and
    header handling stay Starlette's.

    Overriding the *property* rather than ``__init__`` is the load-bearing
    choice. The original skipped ``GZipResponder.__init__`` to install its
    own ``IGzipFile``, which silently stopped working when Starlette moved
    to a ``compressobj`` and added ``thread_minimum_size``: from then on
    every response over ``minimum_size`` raised ``AttributeError``
    mid-send, after the response had already started. Inheriting the
    constructor means a future field lands here for free.
    """

    # isa-l's levels are 0-3; zlib's are 0-9. Clamp so isa-l never receives
    # a level it rejects — level 1 keeps its "fast" meaning either way.
    _MAX_ISAL_LEVEL = 3

    @property
    @override
    def compressor(self) -> zlib._Compress:
        """An isa-l compressobj, built once, in stdlib-gzip wire format."""
        compressor = self._compressor
        if compressor is None:
            level = max(0, min(self._MAX_ISAL_LEVEL, self.compresslevel))
            # isa-l's compressobj is structurally what Starlette wants — same
            # compress/flush, same gzip wire format at `16 + MAX_WBITS` — but
            # the two classes are nominally unrelated, so a cast between them
            # is a claim the type system can disprove. `Any` is the honest
            # bridge: it says "checked by hand", which a disjoint cast does not.
            built: Any = isal_zlib.compressobj(level, isal_zlib.DEFLATED, 16 + isal_zlib.MAX_WBITS)
            compressor = self._compressor = built
        return compressor


class IsalGZipMiddleware:
    """Starlette-compatible gzip middleware backed by isa-l."""

    def __init__(self, app: ASGIApp, *, minimum_size: int = 1024, compresslevel: int = 1) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: D102
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if "gzip" in headers.get("Accept-Encoding", ""):
            responder: ASGIApp = _IsalGZipResponder(self.app, self.minimum_size, compresslevel=self.compresslevel)
        else:
            responder = IdentityResponder(self.app, self.minimum_size)
        await responder(scope, receive, send)


class SelectiveGZipMiddleware:
    """GZip middleware (isa-l backend) that skips `SKIP_GZIP_CONTENT_TYPES`."""

    def __init__(self, app: ASGIApp, *, minimum_size: int = 1024, compresslevel: int = 1) -> None:
        self._gzip = IsalGZipMiddleware(_TagSkippableApp(app), minimum_size=minimum_size, compresslevel=compresslevel)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: D102
        await self._gzip(scope, receive, send)


class _TagSkippableApp:
    """Inner ASGI wrapper marking skip-list responses with ``Content-Encoding: identity``.

    The outer `GZipMiddleware` already short-circuits when any
    ``content-encoding`` header is present on the response; we exploit
    that to opt parquet (and similar) responses out of compression.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def tagging_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type in SKIP_GZIP_CONTENT_TYPES:
                    headers["content-encoding"] = "identity"
            await send(message)

        await self.app(scope, receive, tagging_send)
