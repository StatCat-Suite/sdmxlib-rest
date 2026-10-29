"""Response-splice registry — the post-serialise extension seam.

sdmxlib's JSON writer emits a spec-clean structure body. Consumers routinely
need to *splice* extra fields into it after serialisation — client-specific
annotations, detail-aware codelist/concept hydration, .Stat compatibility
trims. The library must not know about any of those; it only knows how to run
an ordered list of splices in one parse/encode pass.

A **splice** is ``(parsed_json: dict, ctx: SpliceContext) -> bool`` (sync or
async) that mutates ``parsed_json`` in place and returns whether it changed
anything. Consumers register their splices in a `SpliceRegistry`; the
generic route calls `run_structure_splices`, which runs them in order.

Instead of a hardcoded import-and-call list, the order is a registry the consumer owns,
and the single ``orjson.loads → mutate-each → orjson.dumps`` cycle lives here.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

import orjson
from attrs import define, field

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import duckdb
    import sdmxlib as sl
    from fastapi.responses import Response

    # A splice mutates parsed JSON in place and reports whether it changed it.
    Splice = Callable[[dict[str, Any], "SpliceContext"], "bool | Awaitable[bool]"]


@define
class SpliceContext:
    """Everything a structure splice may need about the request being served.

    The generic route builds this and hands it to every splice; a splice reads
    only the fields it cares about. ``extra`` carries surface-specific flags
    (e.g. ``inline_contentconstraint``, ``strip_hierarchy_managed``) so
    consumer sub-apps can vary splice behaviour without new context fields.
    """

    registry: sl.BundleRegistry
    conn: duckdb.DuckDBPyConnection
    # The runtime's `effective_token`, so a splice that caches keys on
    # annotation changes as well as publications.
    token: str
    agency_id: str = ""
    resource_id: str = ""
    version: str = ""
    detail: str = "full"
    references: str = "none"
    structure_type: str = ""
    hydrate: bool = False
    extra: dict[str, Any] = field(factory=dict)
    # Media-type channel. ``media_type`` is the response's current content type
    # (set by the runner before splices run); a splice that reshapes the body
    # into a different format (e.g. the /v1 SDMX-JSON 2.0 -> 1.0 rewrite) sets
    # ``media_override`` and the runner swaps the response's Content-Type on
    # rebuild. ``None`` leaves the content type untouched.
    media_type: str = ""
    media_override: str | None = None


@define
class SpliceRegistry:
    """Ordered splices a consumer installs for a given app surface.

    ``structure`` runs on structure-message JSON bodies. Each app instance
    (canonical, ``/v1``, ``/de``) carries its own registry, so the same generic
    route serves different post-processing per surface without the library
    knowing which splices exist.
    """

    structure: list[Splice] = field(factory=list)


async def run_structure_splices(
    response: Response,
    registry: SpliceRegistry,
    ctx: SpliceContext,
) -> Response:
    """Run every registered structure splice in one parse/encode pass.

    Skips non-JSON responses untouched. Splices may be sync or async; each is
    awaited if it returns an awaitable. Rebuilds the response only when
    something actually changed (dropping the stale ETag / Content-Length so the
    outer middleware recomputes them).
    """
    if not _is_json_response(response):
        return response
    parsed = orjson.loads(response.body)
    ctx.media_type = response.media_type or ""

    changed = False
    for splice in registry.structure:
        result = splice(parsed, ctx)
        if inspect.isawaitable(result):
            result = await result
        if result:
            changed = True

    if not changed:
        return response
    return _rebuild_response(response, orjson.dumps(parsed), ctx.media_override)


def _is_json_response(response: Response) -> bool:
    # Starlette declares `media_type = None` unannotated, so its type is unknown to pyrefly.
    # pyrefly: ignore[unknown-attribute-access]
    return bool(response.media_type and "json" in response.media_type and isinstance(response.body, bytes))


def _rebuild_response(response: Response, new_body: bytes, new_media_type: str | None = None) -> Response:
    """Replace a response's body, dropping stale ETag / Content-Length headers.

    When ``new_media_type`` is given, also drops the stale Content-Type so the
    new one takes effect (a splice reshaped the body into a different format).
    """
    from fastapi.responses import Response as _Response  # noqa: PLC0415 — break import cycle

    skip = {"content-length", "etag"}
    if new_media_type is not None:
        skip.add("content-type")
    new_headers = {k: v for k, v in response.headers.items() if k.lower() not in skip}
    return _Response(
        content=new_body,
        media_type=new_media_type or response.media_type,
        headers=new_headers,
        status_code=response.status_code,
    )


__all__ = ["SpliceContext", "SpliceRegistry", "run_structure_splices"]
