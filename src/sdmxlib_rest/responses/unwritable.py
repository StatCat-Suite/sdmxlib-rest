"""Turn sdmxlib's serialise-time ``ValidationError`` into a 500 that says what failed.

sdmxlib validates every message it writes and refuses content the target
format cannot carry: a component with no ``ConceptIdentity``, a metadataflow
with no targets, a ``MetadataSet`` version SDMX-JSON's semver pattern rejects.
Unhandled, that surfaced as Starlette's bare ``Internal Server Error``, so a
consumer could see *that* a route failed but not which artefact or which rule
(#10). The defect is in the catalogue, so the fix is the producer's, and this
response exists to make it fast to find.

Deliberately a 500 and nothing more lenient. No request can avoid it, and
writing the content anyway would hand strict clients a document they reject,
moving the failure somewhere harder to diagnose. A switch to turn validation
off was considered on #10 and not added.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse
from loguru import logger

if TYPE_CHECKING:
    from fastapi import Request
    from sdmxlib.model.validation import ValidationError


async def unwritable_content_response(request: Request, exc: ValidationError) -> JSONResponse:
    """The 500 for content sdmxlib refused to serialise, naming each artefact and rule.

    Each issue's ``path`` names the artefact (``MetadataStructure:MSD_X``) and
    the field within it; ``message`` names the rule and why it fails. The
    request target says which query hit it, which a listing needs: there the
    path is the only thing naming the artefact.
    """
    issues = [{"path": i.path, "message": i.message, "severity": i.severity} for i in exc.issues]
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    logger.error(
        "unwritable content: {target} — {n} validation issue(s): {paths}",
        target=target,
        n=len(issues),
        paths=", ".join(i["path"] for i in issues),
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": (
                f"the catalogue holds content that cannot be written in the requested format "
                f"({len(issues)} validation issue(s)); the artefact needs fixing at its source"
            ),
            "target": target,
            "issues": issues,
        },
    )
