"""``?lang=`` query-param override for ``Accept-Language`` (issue #72).

Upstream ``dotstatsuite-core-getdata`` accepts ``?lang=fr`` as a
convenience override so Explorer / dev tooling can switch language
without header rewriting (``DataV10Controller.cs:122``,
``AvailableConstraintV10Controller.cs:98``).

We implement this as an ASGI middleware that mutates the request's
``accept-language`` header when ``?lang=<tag>`` is present, so every
downstream consumer — ETag fingerprint, JSON localiser, SDMX-JSON
writer language selection — picks the override automatically without
each route needing to know about ``lang``.

Precedence: ``?lang=<tag>`` > incoming ``Accept-Language`` > "en"
(the localiser's own fallback).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qs

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


class LangOverrideMiddleware:
    """Rewrite ``Accept-Language`` from a ``?lang=`` query param.

    Runs OUTSIDE ``ConditionalGetMiddleware`` so the ETag hash sees the
    resolved language (a request with ``?lang=fr`` must not collide with
    a header-driven ``en`` request that shares the same path + query).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entrypoint — rewrite ``accept-language`` from ``?lang=`` before dispatch."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        qs = scope.get("query_string", b"") or b""
        if b"lang=" not in qs:
            await self.app(scope, receive, send)
            return
        parsed = parse_qs(qs.decode("latin-1"), keep_blank_values=False)
        lang_values = parsed.get("lang") or []
        if not lang_values or not lang_values[-1]:
            await self.app(scope, receive, send)
            return
        override = lang_values[-1].strip()
        if not override:
            await self.app(scope, receive, send)
            return
        # Replace (or append) accept-language while preserving every other
        # header. ASGI headers are (bytes, bytes) tuples; case is
        # lowercase by contract but we lower-compare defensively.
        new_value = override.encode("latin-1")
        rebuilt: list[tuple[bytes, bytes]] = []
        replaced = False
        for name, value in scope["headers"]:
            if name.lower() == b"accept-language":
                if not replaced:
                    rebuilt.append((name, new_value))
                    replaced = True
                # Drop duplicate accept-language headers to avoid
                # confusing downstream parsers.
                continue
            rebuilt.append((name, value))
        if not replaced:
            rebuilt.append((b"accept-language", new_value))
        new_scope = dict(scope)
        new_scope["headers"] = rebuilt
        await self.app(new_scope, receive, send)
