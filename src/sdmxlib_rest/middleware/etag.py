"""Conditional GET (ETag / Last-Modified / 304) middleware.

A publication of the bundle is immutable — every (URL + query string +
Accept header) deterministically maps to one byte-stream until the
bundle is re-published, which mints a new ``token``, or an annotator's
fingerprint changes (GH-2); ``effective_token`` folds in both. That makes conditional
GET trivial:

- ETag is a hash of ``(effective_token, path, query string, Accept)`` — different
  publications produce different ETags; different responses from the same
  one do too (because path/query/Accept differ).
- Last-Modified is the publication's timestamp (``runtime.last_modified``).

When a client sends ``If-None-Match`` matching the ETag, or
``If-Modified-Since`` >= that timestamp, we return ``304 Not Modified``
with no body. Otherwise the response carries ``ETag``, ``Last-Modified``,
and ``Vary: Accept`` so caches behave correctly.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from typing import TYPE_CHECKING

from sdmxlib_rest.localization import parse_accept_language

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from sdmxlib_rest.runtime import CatalogRuntime


class ConditionalGetMiddleware:
    """ASGI middleware adding ETag / Last-Modified + handling If-None-Match / If-Modified-Since.

    Reads ``effective_token`` (the publication's token with any annotator
    fingerprint folded in, GH-2) and ``last_modified`` off the injected
    `CatalogRuntime`; before the catalog is open the middleware passes
    through untouched. ``version`` is the consumer app's version string: it
    busts every ETag on release so a response-shape change ships new ETags
    even though the bundle's ``token`` is stable across code deploys.
    """

    def __init__(self, app: ASGIApp, *, runtime: CatalogRuntime, cache_control: str = "", version: str = "") -> None:
        self.app = app
        self.runtime = runtime
        # Empty string disables emission. See ``RestSettings.cache_control``
        # for the rationale on defaults vs operator overrides.
        self.cache_control = cache_control
        self.version = version

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entrypoint — short-circuits to 304 on a cache-hit, else delegates and decorates."""
        # HEAD is defined by RFC 7231 to be identical to GET except no body,
        # so it MUST honour ETag / If-None-Match / If-Modified-Since — else
        # validators that HEAD-then-GET (curl -I, spec conformance runners)
        # see an inconsistent view where GET returns 304 but HEAD returns 200.
        if scope["type"] != "http" or scope.get("method") not in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return

        if not self.runtime.is_open:
            # Catalog not open yet — nothing stable to fingerprint. (Guard on
            # is_open, not token: an opened-but-empty token still gets a
            # deterministic ETag, matching the old ``manifest is None`` check.)
            await self.app(scope, receive, send)
            return
        token = self.runtime.effective_token

        headers: dict[bytes, str] = {k.lower(): v.decode("latin-1") for k, v in scope["headers"]}
        accept = headers.get(b"accept", "")
        accept_language = headers.get(b"accept-language", "")
        # Fingerprint the RESOLVED primary language tag rather than the raw
        # ``Accept-Language`` header. Two clients sending ``en-US;q=0.9,en``
        # vs ``en`` both resolve to ``en`` — hashing the raw header would
        # spuriously miss the cache for what is the same language choice.
        # Falls back to ``en`` when no header (mirrors `_lang_from_accept_language`).
        preferred = parse_accept_language(accept_language)
        resolved_lang = preferred[0].lower() if preferred else "en"

        # `__version__` busts caches on every release. Without it, server-side
        # response shape changes (e.g. JSON post-processing in the v1 forwarders
        # injecting `obs_count` / `DEFAULT` annotations) ride on the old ETag
        # because the bundle's token is stable across code deploys —
        # clients keep getting 304 with their pre-change cached body.
        signature = f"{self.version}|{token}|{scope['path']}|{scope['query_string'].decode()}|{accept}|{resolved_lang}"
        etag_value = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]
        etag = f'"{etag_value}"'

        last_modified_dt = _parse_built_at(self.runtime.last_modified)
        last_modified = format_datetime(last_modified_dt, usegmt=True) if last_modified_dt else None

        if_none_match = headers.get(b"if-none-match")
        if_modified_since = headers.get(b"if-modified-since")

        if _if_none_match_satisfied(if_none_match, etag):
            await _send_304(send, etag, last_modified, self.cache_control)
            return
        if last_modified_dt is not None and _if_modified_since_satisfied(if_modified_since, last_modified_dt):
            await _send_304(send, etag, last_modified, self.cache_control)
            return

        cache_control = self.cache_control

        async def wrapped_send(msg: Message) -> None:
            # Only a success is the build's answer to this request. Stamping an
            # error made it cacheable for minutes publicly, and a revalidation
            # with its ETag then 304'd, telling the client to keep the error.
            if msg["type"] == "http.response.start" and 200 <= msg["status"] < 300:
                msg = dict(msg)
                extra: list[tuple[bytes, bytes]] = [
                    (b"etag", etag.encode("latin-1")),
                    (b"vary", b"Accept, Accept-Language"),
                ]
                if last_modified is not None:
                    extra.append((b"last-modified", last_modified.encode("latin-1")))
                if cache_control:
                    extra.append((b"cache-control", cache_control.encode("latin-1")))
                msg["headers"] = [*msg.get("headers", []), *extra]
            await send(msg)

        await self.app(scope, receive, wrapped_send)


def _parse_built_at(raw: str) -> datetime | None:
    """Parse the manifest's ISO 8601 ``built_at`` to an aware datetime."""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _if_none_match_satisfied(header: str | None, etag: str) -> bool:
    """``True`` if the client's ``If-None-Match`` matches ``etag``."""
    if not header:
        return False
    header = header.strip()
    if header == "*":
        return True
    candidates = [c.strip() for c in header.split(",")]
    # Strip weak-validator marker (W/...) per RFC 7232; we don't issue weak etags.
    candidates = [c.removeprefix("W/") for c in candidates]
    return etag in candidates


def _if_modified_since_satisfied(header: str | None, built_at: datetime) -> bool:
    """``True`` if the client's ``If-Modified-Since`` is >= the bundle's ``built_at``."""
    if not header:
        return False
    try:
        client_dt = parsedate_to_datetime(header)
    except TypeError, ValueError:
        return False
    if client_dt.tzinfo is None:
        client_dt = client_dt.replace(tzinfo=UTC)
    # The resource hasn't been modified since the client's timestamp if their
    # timestamp is >= the bundle's built_at (down to whole-second precision —
    # HTTP-date is second-resolution).
    return client_dt.replace(microsecond=0) >= built_at.replace(microsecond=0)


async def _send_304(send: Send, etag: str, last_modified: str | None, cache_control: str = "") -> None:
    headers: list[tuple[bytes, bytes]] = [
        (b"etag", etag.encode("latin-1")),
        (b"vary", b"Accept, Accept-Language"),
    ]
    if last_modified is not None:
        headers.append((b"last-modified", last_modified.encode("latin-1")))
    if cache_control:
        headers.append((b"cache-control", cache_control.encode("latin-1")))
    await send({"type": "http.response.start", "status": 304, "headers": headers})
    await send({"type": "http.response.body", "body": b""})
