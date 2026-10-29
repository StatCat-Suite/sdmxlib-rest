"""Shared helper for the SDMX-REST 2.2.2 ``asOf`` query parameter.

The spec lists ``asOf`` on every read path (``/data``, ``/structure``,
``/metadata/*``, ``/schema``). Semantics: "retrieve the resource as it
was at the supplied point in time (aka time travel)". Implementations
that don't ship a temporal history layer should return ``501`` to make
the gap visible to clients (vs FastAPI's silent 422 on an unknown
query param).

This service runs from a single immutable bundle per process, identified
by ``build_id``. There's no history layer — point-in-time queries
aren't meaningful. The structure and metadata routes declare `AsOf` and
call `reject_asof`, which surfaces the 501 to a strict client and
registers the param in the OpenAPI schema so it's discoverable.
`/availability` does not declare ``asOf``, so there it is an unknown
query parameter and `reject_unknown_query_params` answers 400.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import HTTPException, Query

# Re-usable Annotated type — keeps every route's signature uniform and
# the OpenAPI parameter description identical across the surface.
AsOf = Annotated[
    str | None,
    Query(
        alias="asOf",
        description=(
            "SDMX-REST 2.2.2 — time-travel query. Not implemented: this service runs from a "
            "single immutable bundle (`build_id`) per process and has no history layer, so any "
            "non-null `asOf` returns `501`. The parameter is declared so clients can "
            "learn from OpenAPI that time-travel queries are unsupported."
        ),
    ),
]


def reject_asof(value: str | None) -> None:
    """Raise ``HTTPException(501)`` if ``value`` is non-null. No-op otherwise."""
    if value is not None:
        raise HTTPException(
            status_code=501,
            detail=(
                "asOf is a SDMX-REST 2.2.2 parameter but not implemented: this service runs "
                "from a single immutable bundle per process and has no history layer. Point-in-time "
                "queries are out of scope."
            ),
        )
