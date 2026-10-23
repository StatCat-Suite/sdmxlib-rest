"""Map empty SDMX-JSON structure responses to ``204 No Content``.

The SDMX-REST spec lists 204 in the common response set on every read
path: a query that succeeded but matched zero artefacts/observations.
The spec is clear that 200 with an empty payload and 204 carry the
same semantic ("you asked, nothing matched"), but strict clients
differentiate the two so that a "no match" doesn't have to be inferred
from a non-error body's shape.

We apply this to JSON structure / metadata / availability responses
post-serialise — same splice pattern as the URN / HIER_CONTEXT
injections. The check is "every artefact collection in
``response.data`` is absent or empty" and (for availability) the
constraint envelope's ``keyValues`` are all empty.

XML responses, CSV / parquet streams, and the dedicated debug-output
shapes are left at 200 — they have their own emptiness conventions
and aren't covered by the spec text (or by clients that want 204).
"""

from __future__ import annotations

import json
from typing import Any, Final

from fastapi.responses import Response

# Names of every JSON-encoded collection key the server can emit. Anything
# in here that's non-empty counts the response as "non-empty" for 204
# purposes. The keys are the camelCase forms sdmxlib's writer uses.
_DATA_COLLECTION_KEYS: Final[frozenset[str]] = frozenset(
    {
        "codelists",
        "conceptSchemes",
        "categorySchemes",
        "categorisations",
        "dataStructures",
        "dataflows",
        "agencySchemes",
        "dataProviderSchemes",
        "dataConsumerSchemes",
        "organisationUnitSchemes",
        "hierarchies",
        "hierarchyAssociations",
        "hierarchicalCodelists",
        "provisionAgreements",
        "metadataflows",
        "metadataStructures",
        # Constraint collections — emptiness for these is determined by their
        # cube-region keyValues, not by their presence; treat them as "non-empty"
        # collections and handle the availability case in the route itself.
        "dataConstraints",
        "contentConstraints",
        # MetadataSets — when a structure walk surfaces them via /metadata.
        "metadataSets",
    },
)


def _data_is_empty(data: dict[str, Any]) -> bool:
    """``True`` when ``data`` contains no non-empty maintainable collections."""
    if not data:
        return True
    for key in _DATA_COLLECTION_KEYS:
        value = data.get(key)
        if isinstance(value, list) and value:
            return False
    return True


def to_204_if_empty(response: Response) -> Response:
    """Replace a JSON ``200`` with an empty body by a ``204`` (no body).

    Idempotent — if the response is already 204 or carries a non-JSON
    body or has a populated ``data`` block, returns ``response``
    unchanged.
    """
    if response.status_code != 200:
        return response
    # Starlette declares `media_type = None` unannotated, so its type is unknown to pyrefly.
    # pyrefly: ignore[unknown-attribute-access]
    if not (response.media_type and "json" in response.media_type and isinstance(response.body, bytes)):
        return response
    if not response.body:
        return response
    try:
        parsed = json.loads(response.body)
    except json.JSONDecodeError:
        return response
    if not _data_is_empty(parsed.get("data") or {}):
        return response

    # Reuse the original response's headers (modulo content-length / etag —
    # both go stale with the body change). ETag middleware will repopulate
    # on the next request.
    skip = {"content-length", "etag"}
    new_headers = {k: v for k, v in response.headers.items() if k.lower() not in skip}
    return Response(status_code=204, headers=new_headers)


__all__ = ["to_204_if_empty"]
