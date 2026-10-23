"""Rewrite outgoing ``Content-Type: ...;version=X.Y.0`` to the spec-canonical ``;version=X.Y``.

SDMX-REST 2.2.2 §6.1 and the SDMX-JSON 2.0 file-format spec advertise
media types with two-part versions (``application/vnd.sdmx.structure+json;version=2.0``).
sdmxlib's writers emit the off-spec three-part form (``;version=2.0.0``).
A strict SDMX-2-native client that compares the Content-Type token
literally against the spec's two-part form sees a mismatch and either
refuses the body or downgrades to a generic JSON parser.

The middleware does one transform on the way out: any
``Content-Type: <base>;version=X.Y.0[;<other params>]`` becomes
``Content-Type: <base>;version=X.Y[;<other params>]``. ``2.0.0 → 2.0``,
``1.0.0 → 1.0``, ``2.1 → 2.1`` (unchanged), ``3.0 → 3.0`` (unchanged).
Other matrix params (``;urn=true``, ``;charset=utf-8``) survive untouched
since we only touch the ``version=`` segment.

Symmetric to the input-side normalisation in `negotiation`: that
matcher already treats ``version=X.Y`` ≡ ``version=X.Y.0``, so any client
matching on three-part still works against our two-part response.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Captures the version-segment of any media type and detects a trailing
# ``.0`` patch component. Anchored at the start of a version=... param so
# we don't accidentally rewrite an unrelated `.0` inside another field.
_VERSION_PATCH_RE = re.compile(r"(;\s*version=\d+\.\d+)\.0(?=\b|;|$)", re.IGNORECASE)


def canonicalise_version_token(content_type: str) -> str:
    """Strip trailing ``.0`` from any ``version=X.Y.0`` segment in a Content-Type.

    Returns the input unchanged when the version is already two-part or
    when the header has no ``version=`` matrix param at all.
    """
    return _VERSION_PATCH_RE.sub(r"\1", content_type)


class SdmxVersionAdvertiseMiddleware:
    """ASGI middleware rewriting outgoing ``Content-Type`` version tokens to the SDMX two-part form."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entrypoint — rewrites the response's Content-Type version on the way out."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped_send(msg: Message) -> None:
            if msg["type"] == "http.response.start":
                headers = msg.get("headers") or []
                rewritten: list[tuple[bytes, bytes]] = []
                changed = False
                for k, v in headers:
                    if k.lower() == b"content-type":
                        original = v.decode("latin-1")
                        new = canonicalise_version_token(original)
                        if new != original:
                            changed = True
                            rewritten.append((k, new.encode("latin-1")))
                            continue
                    rewritten.append((k, v))
                if changed:
                    msg = dict(msg)
                    msg["headers"] = rewritten
            await send(msg)

        await self.app(scope, receive, wrapped_send)
