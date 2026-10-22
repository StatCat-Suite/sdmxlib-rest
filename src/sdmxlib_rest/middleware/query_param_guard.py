"""Reject unknown query params with 400 (closes #34).

SDMX-REST 2.2.2 §"Bad syntax" says the server SHOULD reject malformed or
unrecognised input. FastAPI's default is to silently ignore unknown query
params — a typo like ``?detial=full`` instead of ``?detail=full`` quietly
gets dropped. That's what hid bug #45 (1.5 ``detail`` was undeclared on
/v1/data so strict spec clients got silent ignore).

This dependency runs per route:

1. Reads the matched route's declared query params via
   ``route.dependant.query_params`` (FastAPI's own model). Aliases
   (e.g. ``Range`` for ``range_header``) are used — they're the names
   that appear on the wire.
2. Adds the per-route vendor allowlist (currently shared across all
   routes; per-route is a future refinement).
3. Allows bracketed prefixes — ``c[<DIM>]=`` and ``within[<DIM>]=``
   appear in the query string but aren't modelled by FastAPI's
   parameter dependancy graph (deepObject style). They're parsed
   manually from ``request.query_params.multi_items()`` inside the
   handler, so they're legitimate.
4. 400 on anything else, naming the offender.

Wired in via the FastAPI ``app.router.dependencies`` list so every
route picks it up without per-router opt-in.
"""

from __future__ import annotations

import re
from typing import Final

from fastapi import HTTPException, Request

# Bracketed query params our routes accept via raw ``multi_items()``
# extraction (FastAPI doesn't model deepObject). These prefixes are
# allowed regardless of which route handles the request.
_BRACKETED_PREFIXES: Final[tuple[str, ...]] = ("c[", "within[")

# Cross-route vendor query params declared in our OpenAPI but not in
# the FastAPI route signature (because they're documented via the
# vendor extensions hook, not as Python kwargs).
_GLOBAL_VENDOR_PARAMS: Final[frozenset[str]] = frozenset(
    {
        # Internal-only benchmarking knob — accepted but ``include_in_schema=False``.
        "_chunk",
    },
)

# Spec-defined SDMX-REST 2.2.2 query parameters that some clients append
# unconditionally to routes where they have no behavioural meaning. .Stat /
# DLM, for instance, attach ``dimensionAtObservation=AllDimensions`` to
# every request including structure + availability fetches; ``format``
# similarly stands in for ``Accept`` on some legacy clients. We accept
# them as global no-ops rather than 400 (the spec-compliance goal of #34
# is catching *typos* like ``detial`` — a known spec keyword on the
# "wrong" endpoint is a permissible client habit, not a bug).
#
# Handler-level declaration still wins where the param has actual
# semantics (e.g. ``format`` on ``/data``, where we validate the value);
# this set only matters when the route DOESN'T declare them.
_GLOBAL_NOOP_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "dimensionAtObservation",
        "format",
        # ``?lang=<tag>`` overrides Accept-Language via LangOverrideMiddleware
        # (issue #72). The middleware consumes it before routing, so from a
        # route's POV it's a no-op; declare here so the query-param guard
        # doesn't 400 on it.
        "lang",
    },
)

# Path components that aren't real routes (FastAPI auto-mounted).
_NOT_A_REGULAR_ROUTE: Final[tuple[str, ...]] = (
    "/openapi.json",
    "/docs",
    "/redoc",
    "/explorer",
)

_BRACKETED_MATCHER: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*\[.*\]$")


def _is_bracketed(name: str) -> bool:
    """``c[GEO_LEVEL]`` / ``within[REF_AREA]`` style — allowed unconditionally."""
    return any(name.startswith(prefix) for prefix in _BRACKETED_PREFIXES) and _BRACKETED_MATCHER.match(name) is not None


def reject_unknown_query_params(request: Request) -> None:
    """FastAPI dependency: 400 when the request has query params the route doesn't declare.

    Idempotent — a request with only declared / vendor / bracketed params passes through.
    """
    route = request.scope.get("route")
    if route is None or not hasattr(route, "dependant"):
        return  # Auto-routed paths (openapi.json, docs, etc.); nothing to check.
    if any(part in request.url.path for part in _NOT_A_REGULAR_ROUTE):
        return

    declared: set[str] = set()
    for param in route.dependant.query_params:
        # ``alias`` is the wire-facing name (e.g. ``startPeriod``); ``name`` is the
        # Python kwarg (e.g. ``start_period``). Both forms are accepted by FastAPI
        # but the wire name is what we get back from request.query_params.
        if param.alias:
            declared.add(param.alias)
        if param.name:
            declared.add(param.name)

    allowed = declared | _GLOBAL_VENDOR_PARAMS | _GLOBAL_NOOP_PARAMS

    unknown = [key for key in request.query_params if key not in allowed and not _is_bracketed(key)]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown query parameter(s): {sorted(set(unknown))}. "
                f"Allowed for this operation: {sorted(declared) or '[none]'}. "
                "Bracketed forms `c[<dim>]=` and `within[<dim>]=` are also accepted. "
                "If you meant a vendor extension, see docs/sdmx-rest-conformance.md."
            ),
        )


__all__ = ["reject_unknown_query_params"]
