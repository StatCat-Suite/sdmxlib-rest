"""SDMX structural artefact endpoints (DSD, Dataflow, Codelist, ConceptScheme, Hierarchy)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

import duckdb
import sdmxlib as sl
from attrs import evolve
from attrs import fields as attrs_fields
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from fastapi.openapi.models import Example
from fastapi.responses import Response
from sdmxlib.formats import to_json, to_xml
from sdmxlib.model.message import StructureMessage

from sdmxlib_rest.localization import localized_json_response
from sdmxlib_rest.message_annotator import MessageAnnotator
from sdmxlib_rest.negotiation import ensure_acceptable, is_browser_request
from sdmxlib_rest.responses.asof import AsOf, reject_asof
from sdmxlib_rest.responses.empty import to_204_if_empty
from sdmxlib_rest.runtime import (
    get_build_id,
    get_message_annotator,
    get_meta_conn,
    get_registry,
    get_splices,
    get_structure_cache,
)
from sdmxlib_rest.splice import SpliceContext, SpliceRegistry, run_structure_splices
from sdmxlib_rest.structure_cache import StructureCache, StructureQuery

_SUPPORTED_STRUCTURE_MEDIA = [
    "application/vnd.sdmx.structure+json;version=2.0.0",
    "application/vnd.sdmx.structure+xml;version=2.1",
    "application/vnd.sdmx.structure+xml;version=3.0",
]

router = APIRouter(tags=["structure"])

# SDMX 2.1 / 3.0 IDType + NestedIDType + version patterns from
# SDMXCommonReferences.xsd. Applied to Path() so malformed refs fail with
# 422 at the framework boundary instead of bubbling through to a ValueError
# in sdmxlib's URN parser (which would surface as 500).
#
# Two wildcard tokens are accepted: ``all`` (SDMX-REST 1.5 legacy that .Stat /
# DLM emit; e.g. ``/structure/dataflow/all/all/all`` lists every dataflow)
# and ``*`` (the canonical 2.2.2 form). Both normalise to the same internal
# sentinel (``_WILDCARD``) at handler entry via :func:`_normalize_wildcard`,
# so downstream dispatch only ever sees ``"all"``.
_AGENCY_PATTERN = r"^(all|\*|[A-Za-z0-9_@$-]+(\.[A-Za-z0-9_@$-]+)*)$"
_ID_PATTERN = r"^(all|\*|[A-Za-z0-9_@$-]+)$"
_VERSION_PATTERN = r"^(all|latest|\*|[0-9]+(\.[0-9]+)*)$"
_WILDCARD = "all"
_LATEST = "latest"


def _normalize_wildcard(value: str) -> str:
    """Map the SDMX-REST 2.2.2 ``*`` wildcard token to our internal ``all`` sentinel.

    Spec regex for agency/id/version slots admits ``*`` as the canonical
    wildcard in 2.2.2; ``all`` is 1.5 legacy. We accept both at the
    boundary and converge on ``all`` internally so dispatch logic
    (``Registry.get_all`` vs single-artefact fetch) stays single-branched.
    """
    return _WILDCARD if value == "*" else value

    # Shared error-response declarations so the OpenAPI spec matches the actual
    # HTTPException(400 / 404) raises in the route. Serialised as {"detail": ...}.


_ERROR_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"detail": {"type": "string"}},
    "required": ["detail"],
}
# 413 / 414 are enforced by the ingress in front of this app, not by it — we
# declare them in OpenAPI so clients can distinguish "URL too long" from
# "key parse error" without server-source-diving.
_INGRESS_LIMIT_RESPONSES = {
    413: {
        "description": "Payload Too Large — enforced by the ingress; surface returned as-is.",
        "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
    },
    414: {
        "description": "URI Too Long — enforced by the ingress (~8 KiB by default). Use `c[…]=` filters for OR sets instead of long `+`-joined keys.",  # noqa: E501
        "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
    },
}
_ERROR_RESPONSES = {
    400: {
        "description": "Unknown structure type.",
        "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
    },
    404: {
        "description": "Artefact not found.",
        "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
    },
    406: {
        "description": "Accept header doesn't intersect the route's supported media types.",
        "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
    },
    **_INGRESS_LIMIT_RESPONSES,
}
_ITEM_ERROR_RESPONSES = {
    400: {
        "description": "Unknown item-scheme type.",
        "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
    },
    404: {
        "description": "Scheme or item not found.",
        "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
    },
    406: {
        "description": "Accept header doesn't intersect the route's supported media types.",
        "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
    },
    **_INGRESS_LIMIT_RESPONSES,
    501: {
        "description": "Item-scheme type is valid per spec but not implemented by this server.",
        "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
    },
}
# Declare the actual content types the route emits so the spec matches
# runtime (FastAPI's default `application/json` would understate it).
_STRUCTURE_200_CONTENT: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "SDMX structure message.",
        "content": {
            "application/vnd.sdmx.structure+json;version=2.0.0": {},
            "application/vnd.sdmx.structure+xml;version=2.1": {},
            "application/vnd.sdmx.structure+xml;version=3.0": {},
        },
    },
}


_TYPES: dict[str, type[sl.MaintainableArtefact]] = {
    "datastructure": sl.DataStructure,
    "dataflow": sl.Dataflow,
    "codelist": sl.Codelist,
    "conceptscheme": sl.ConceptScheme,
    "hierarchy": sl.Hierarchy,
    # SDMX 3 binding artefact for hierarchies (one per Hierarchy bound to a
    # dimension). Strict SDMX 3 clients read these to discover hierarchical
    # filters without needing the legacy ``HIER_CONTEXT`` annotation shim.
    "hierarchyassociation": sl.HierarchyAssociation,
    # Additional spec-listed structureType values for clients that probe /
    # list these artefacts (AgencySchemes, and optionally CategoryScheme +
    # Categorisations for catalog browsing). Empty lists are the correct
    # response when the catalogue has none of an artefact type, so
    # exposing each new type is non-breaking.
    "categorisation": sl.Categorisation,
    "categoryscheme": sl.CategoryScheme,
    "agencyscheme": sl.AgencyScheme,
    "dataproviderscheme": sl.DataProviderScheme,
    # Reference-metadata artefacts, so clients probing
    # ``/structure/metadatastructure/all/all/latest`` for discovery don't 400
    # even though the artefacts are reachable via ``/metadata/...`` too.
    "metadatastructure": sl.MetadataStructure,
    "metadataflow": sl.Metadataflow,
}

# Item-bearing artefacts: maps the artefact class to the attrs field that
# holds its items. Used by ``detail=allstubs`` to strip the heavy item tree
# without losing the artefact's identification + name + description.
#
# Keyed `type[object]`, not the concrete three-class union: `_stub` looks up
# `type(artefact)` on an artefact it only knows as `object`, and a narrower key
# type makes that membership test a type error rather than the miss it is.
_ITEM_FIELD: dict[type[object], str] = {
    sl.Codelist: "codes",
    sl.ConceptScheme: "concepts",
    sl.Hierarchy: "tree",
}

# SDMX-REST 2.2.2 single-item fetch: `/structure/{itemSchemeType}/.../{itemID}`.
# Maps the spec's itemSchemeType keywords to (scheme class, scheme's item field).
# Only Codelist + ConceptScheme item schemes are implemented; the spec's
# other values (agencyscheme, categoryscheme, transformationscheme, …) 501.
_ITEM_SCHEME_TYPES: dict[str, tuple[type[sl.MaintainableArtefact], str]] = {
    "codelist": (sl.Codelist, "codes"),
    "conceptscheme": (sl.ConceptScheme, "concepts"),
}

# Spec-defined itemSchemeType values we recognise as valid but don't implement.
# Distinguishing them lets the route return 501 (valid scheme, not implemented)
# rather than 400 (unknown scheme).
_SPEC_ITEM_SCHEME_TYPES_UNIMPLEMENTED: frozenset[str] = frozenset(
    {
        "agencyscheme",
        "categoryscheme",
        "dataproviderscheme",
        "dataconsumerscheme",
        "organisationunitscheme",
        "transformationscheme",
        "rulesetscheme",
        "userdefinedoperatorscheme",
        "customtypescheme",
        "namepersonalisationscheme",
        "vtlmappingscheme",
        "valuelist",
    },
)

# Per-parameter Swagger examples. Each dropdown teaches its own parameter with
# neutral placeholder values — a catalog-specific deployment can override these
# in its own OpenAPI post-processing if it wants to advertise real artefact IDs.
_STRUCTURE_TYPE_EXAMPLES: dict[str, Example] = {
    "dataflow": Example(summary="A Dataflow (typically the entrypoint for /data)", value="dataflow"),
    "datastructure": Example(summary="A DataStructure Definition (DSD)", value="datastructure"),
    "codelist": Example(summary="A Codelist (one column's allowed values)", value="codelist"),
    "conceptscheme": Example(summary="A ConceptScheme", value="conceptscheme"),
    "hierarchy": Example(summary="A Hierarchy (parent/child code relationships)", value="hierarchy"),
}

_AGENCY_EXAMPLES: dict[str, Example] = {
    "agency": Example(summary="A maintenance agency ID", value="SDMX"),
    "all": Example(summary="Wildcard: every agency", value="all"),
}

_RESOURCE_ID_EXAMPLES: dict[str, Example] = {
    "resource": Example(summary="An artefact ID matching the structureType", value="CL_UNIT_MEASURE"),
    "all": Example(summary="Wildcard: every resource of this type/agency", value="all"),
}

_VERSION_EXAMPLES: dict[str, Example] = {
    "1.0": Example(summary="An explicit version", value="1.0"),
    "all": Example(summary="Wildcard: every version of this resource", value="all"),
    "latest": Example(summary="Highest published version of each matching artefact", value="latest"),
}

# SDMX-REST 2.2.2 detail vocabulary. Implementation status:
#   full                       implemented (default)
#   allstubs                   implemented — strips item lists, keeps name + description
#   allcompletestubs           implemented — alias of allstubs in our serialiser; both
#                                produce header + name + description (no items)
#   referencestubs             implemented — queried artefact full + referenced artefacts
#                                stubbed (walks dataflow → DSD → all codelists)
#   referencecompletestubs     implemented — same walk, stubs keep name + description
#   referencepartial           approximated as referencestubs — strict version needs a
#                                per-component "items actually used in data" join (#11);
#                                .Stat Explorer sends this on every dataflow preview, so
#                                returning 501 broke compatibility. The approximation gives
#                                clients the DSD + codelist headers they need to render.
#   raw                        501 — implementation-defined per spec; not exposed here.
_Detail = Literal[
    "full",
    "allstubs",
    "allcompletestubs",
    "referencestubs",
    "referencecompletestubs",
    "referencepartial",
    "raw",
]
_DETAIL_STUB_BUCKET: frozenset[str] = frozenset({"allstubs", "allcompletestubs"})
_DETAIL_REFERENCE_BUCKET: frozenset[str] = frozenset({"referencestubs", "referencecompletestubs", "referencepartial"})
_DETAIL_NOT_IMPLEMENTED: frozenset[str] = frozenset({"raw"})

_DETAIL_EXAMPLES: dict[str, Example] = {
    "full": Example(summary="Full materialisation including items", value="full"),
    "allstubs": Example(summary="Header only — items stripped (use for large codelists/hierarchies)", value="allstubs"),
    "allcompletestubs": Example(
        summary="Like allstubs — both keep name + description; we don't model a name-only stub",
        value="allcompletestubs",
    ),
    "referencestubs": Example(
        summary="Queried artefact full; referenced artefacts as stubs (DSD + codelists from a Dataflow)",
        value="referencestubs",
    ),
    "referencecompletestubs": Example(
        summary="Same walk as referencestubs; identical here since stubs keep labels in our serialiser",
        value="referencecompletestubs",
    ),
}

# SDMX-REST 2.2.2 references vocabulary. The spec allows BOTH the canonical
# keywords (none / parents / parentsandsiblings / children / descendants /
# all) AND any specific artefact-type token (e.g. `contentconstraint`,
# `dataflow`, `codelist`, …) to filter the walk to that type. .Stat
# Explorer probes a lot of these — accept any string and dispatch:
#   children                   implemented (Dataflow → DSD)
#   descendants / all          approximated as the referencestubs walk
#                                (DSD + every codelist the DSD references)
#   none / parents / parentsandsiblings  return only the queried artefact;
#                                this single-layer bundle has no real parent
#                                links to walk back up.
#   any artefact-type name     accepted but currently returns only the
#                                queried artefact — filtering the walk to one
#                                referenced type isn't wired yet, but rejecting
#                                with 422 broke .Stat's preview which probes
#                                `references=contentconstraint` etc.
_REFERENCES_CHILDREN_BUCKET: frozenset[str] = frozenset({"children"})
_REFERENCES_DESCENDANT_BUCKET: frozenset[str] = frozenset({"descendants", "all"})
_REFERENCES_PARENTS_BUCKET: frozenset[str] = frozenset({"parents", "parentsandsiblings"})

_REFERENCES_EXAMPLES: dict[str, Example] = {
    "none": Example(summary="The artefact alone", value="none"),
    "children": Example(summary="Inline immediate references (e.g. a Dataflow's DSD)", value="children"),
    "descendants": Example(summary="Walk down: DSD + every codelist the DSD references", value="descendants"),
    "all": Example(summary="Walk down (equivalent to descendants for a flat catalogue)", value="all"),
    "parents": Example(summary="Walk up to referencing artefacts (where the catalogue models them)", value="parents"),
    "parentsandsiblings": Example(
        summary="Parents plus artefacts sharing a parent",
        value="parentsandsiblings",
    ),
}


@router.get(
    "/structure/{structureType}/{agencyID}/{resourceID}/{version}",
    summary="Retrieve a structural artefact",
    description=(
        "Retrieve a structural artefact by `(structureType, agencyID, resourceID, version)`. "
        "`resourceID` must correspond to the `structureType` (a Dataflow ID for "
        "`structureType=dataflow`, a DSD ID for `datastructure`, and so on).\n\n"
        "Any path segment may be the literal `all` to enumerate "
        "(e.g. `/structure/dataflow/all/all/all` lists every dataflow). Pair "
        "`detail=allstubs` with large codelists / hierarchies to strip their item trees."
    ),
    response_description="SDMX-JSON 2.0 (default) or SDMX-ML 2.1 / 3.0 via Accept header.",
    responses={**_STRUCTURE_200_CONTENT, **_ERROR_RESPONSES},
)
async def get_structure(
    structureType: Annotated[  # noqa: N803 — SDMX-REST 2.2.2 spec param name
        str,
        Path(
            description="Artefact type: `dataflow` | `datastructure` | `codelist` | `conceptscheme` | `hierarchy`.",
            openapi_examples=_STRUCTURE_TYPE_EXAMPLES,
        ),
    ],
    agencyID: Annotated[  # noqa: N803
        str,
        Path(
            description="Maintainer agency ID, or `all` to wildcard.",
            pattern=_AGENCY_PATTERN,
            openapi_examples=_AGENCY_EXAMPLES,
        ),
    ],
    resourceID: Annotated[  # noqa: N803
        str,
        Path(
            description=(
                "Artefact ID — must correspond to the `structureType` "
                "(see the table in the route description). `all` wildcards every resource."
            ),
            pattern=_ID_PATTERN,
            openapi_examples=_RESOURCE_ID_EXAMPLES,
        ),
    ],
    version: Annotated[
        str,
        Path(
            description="Artefact version, or `all` to wildcard.",
            pattern=_VERSION_PATTERN,
            openapi_examples=_VERSION_EXAMPLES,
        ),
    ],
    registry: Annotated[sl.LocalRegistry, Depends(get_registry)],
    conn: Annotated[duckdb.DuckDBPyConnection, Depends(get_meta_conn)],
    build_id: Annotated[str, Depends(get_build_id)],
    splices: Annotated[SpliceRegistry, Depends(get_splices)],
    cache: Annotated[StructureCache, Depends(get_structure_cache)],
    annotator: Annotated[MessageAnnotator, Depends(get_message_annotator)],
    detail: Annotated[
        _Detail,
        Query(
            description=(
                "Per SDMX-REST 2.2.2. `full` (default) returns items; "
                "`allstubs` / `allcompletestubs` strip items; "
                "`referencestubs` / `referencecompletestubs` keep the queried artefact full and "
                "add its referenced artefacts as stubs. "
                "`referencepartial` and `raw` return 501."
            ),
            openapi_examples=_DETAIL_EXAMPLES,
        ),
    ] = "full",
    references: Annotated[
        str,
        Query(
            description=(
                "Per SDMX-REST 2.2.2. `none` (default), `children`, `descendants`, `all` are honoured; "
                "`parents` / `parentsandsiblings` walk up where the catalogue models them. Any "
                "artefact-type token (e.g. `contentconstraint`, `dataflow`) is accepted for spec compat "
                "but currently behaves as `none`."
            ),
            openapi_examples=_REFERENCES_EXAMPLES,
        ),
    ] = "none",
    asof: AsOf = None,
    accept: Annotated[str | None, Header()] = None,
    accept_language: Annotated[str | None, Header(alias="Accept-Language")] = None,
) -> Response:
    """Resolve a structural artefact and serialise it as SDMX-JSON 2.0 (default) or SDMX-ML (Accept)."""
    reject_asof(asof)
    # Normalise the SDMX-REST 2.2.2 ``*`` wildcard to our internal ``all``
    # sentinel before any dispatch. Path patterns accept either token; the
    # rest of this handler only ever needs to compare against ``all``.
    agencyID = _normalize_wildcard(agencyID)  # noqa: N806
    resourceID = _normalize_wildcard(resourceID)  # noqa: N806
    version = _normalize_wildcard(version)
    ensure_acceptable(accept, _SUPPORTED_STRUCTURE_MEDIA)
    if detail in _DETAIL_NOT_IMPLEMENTED:
        raise HTTPException(
            status_code=501,
            detail=(
                f"detail={detail!r} is a SDMX-REST 2.2.2 value but not implemented "
                "on this service (see docs/sdmx-rest-conformance.md)"
            ),
        )
    cls = _TYPES.get(structureType.lower())
    if cls is None:
        raise HTTPException(status_code=400, detail=f"unknown structureType: {structureType}")

        # `latest` with concrete (agency, id) resolves to a single version — fall
        # through to the single-artefact 404 path. Mixed wildcards + latest stay
        # in the enumerator branch where we reduce per (agency, id) group.
    if version == _LATEST and _WILDCARD not in (agencyID, resourceID):
        resolved = _resolve_latest(registry, cls, agency=agencyID, id_=resourceID)
        if resolved is None:
            raise HTTPException(
                status_code=404,
                detail=f"not found: {cls.__name__}(agency={agencyID}, id={resourceID}, version=latest)",
            )
        version = resolved

        # Consumer-owned response cache (a pod-lifetime speedup for expensive
        # shapes — large codelists, slow-to-materialise hierarchies). The bundle is
        # immutable per ``build_id`` so cached bytes are stable for the pod
        # lifetime; a bare catalog gets the no-op cache and always renders.
    cache_query = StructureQuery(
        structure_type=structureType,
        agency_id=agencyID,
        resource_id=resourceID,
        version=version,
        detail=detail,
        references=references,
        accept=accept,
        accept_language=accept_language,
        build_id=build_id,
    )
    cached = cache.lookup(cache_query)
    if cached is not None:
        return cached

    if _WILDCARD in (agencyID, resourceID, version) or version == _LATEST:
        artefacts = _enumerate_with_wildcards(
            registry,
            cls,
            agency=agencyID,
            id_=resourceID,
            version=version,
            detail=detail,
            references=references,
        )
        msg = _build_message_many(artefacts)
    else:
        urn = cls.urn_for(agency=agencyID, id=resourceID, version=version)
        artefact = registry.get(urn)
        if artefact is None:
            raise HTTPException(status_code=404, detail=f"not found: {urn}")

            # `detail` controls the SHAPE of the queried artefact; `references`
            # controls which RELATED artefacts to inline. The two are independent
            # — the old code conflated them and dropped `references` whenever
            # `detail` was in the stub bucket, which broke
            # `?references=children&detail=allcompletestubs` (the shape every
            # .Stat client sends for structure listings).
        # Load the items only when the response carries them. A stub comes from
        # the wrapper, and `_resolve_extras` is handed the artefact as stored:
        # none of its walkers acts on a codelist or a hierarchy — the only lazy
        # types — so the wrapper and the loaded object take the same path and give
        # the same answer. Loading first, as this used to, cost ~5.6 ms per
        # thousand codes for nothing whenever `detail` was a stub.
        shaped_artefact = _stub_of(artefact) if detail in _DETAIL_STUB_BUCKET else _materialize(artefact)
        extras = _resolve_extras(artefact, references, detail, registry)

        msg = _build_message(shaped_artefact, extras)

        # Pre-serialise annotation seam: a consumer may project extra annotations
        # onto the model before it is written, so they reach EVERY wire format
        # (SDMX-JSON 2.0 + SDMX-ML 2.1/3.0), not just the JSON body a post-serialise
        # splice can touch. A consumer can use this for the .Stat-compat METADATA annotation
        # that drives Explorer's Overview metadata discovery; the default annotator
        # is a no-op. Runs before both the XML and JSON branches below.
    msg = annotator.annotate(msg, registry=registry, conn=conn, build_id=build_id)

    accept_lower = (accept or "").lower()
    # Browsers send `text/html,application/xhtml+xml,application/xml;q=0.9,…`.
    # The bare `"xml" in accept` check would dispatch them to SDMX-ML — what
    # they really want is the JSON body so they can pretty-print it inline.
    # Skip the XML branch when the request comes from a browser; the JSON
    # path further down downgrades Content-Type to `application/json` too.
    if "xml" in accept_lower and not is_browser_request(accept):
        sdmx_version = "3.0" if "3.0" in accept_lower else "2.1"
        body = to_xml(msg, version=sdmx_version)
        media = f"application/vnd.sdmx.structure+xml;version={sdmx_version}"
        return Response(content=body, media_type=media)

    response = localized_json_response(
        to_json(msg),
        media_type="application/vnd.sdmx.structure+json;version=2.0.0",
        accept_language=accept_language,
        accept=accept,
    )
    # Run every post-serialise splice in a SINGLE orjson parse/encode pass
    # (previously each splice round-tripped the full 8 MB body through
    # ``json.loads`` / ``json.dumps`` independently). The pipeline:
    #   1. HIER_CONTEXT annotation on every DSD (closes #41)
    #      (the METADATA annotation moved UP to the pre-serialise annotator
    #      seam above, so it reaches XML + JSON, not just this JSON pass.)
    #   2. Codelist hydration per ``detail`` (closes #25) — only on
    #      dataflow queries with referencepartial / full.
    #   3. ConceptScheme concept hydration — same trigger as (2); pulls
    #      the DSD-referenced subset so .Stat's dim-label lookup resolves.
    # 204 No Content runs at the tail, after all splices have had their
    # chance — preserves the #32 "empty wildcard returns 204" behaviour.
    hydrate = structureType == "dataflow" and detail in {"referencepartial", "full"}
    ctx = SpliceContext(
        registry=registry,
        conn=conn,
        build_id=build_id,
        agency_id=agencyID,
        resource_id=resourceID,
        version=version,
        detail=detail,
        references=references,
        structure_type=structureType,
        hydrate=hydrate,
    )
    response = await run_structure_splices(response, splices, ctx)
    response = to_204_if_empty(response)
    cache.store(cache_query, response)
    return response


@router.get(
    "/structure/{itemSchemeType}/{agencyID}/{resourceID}/{version}/{itemID}",
    summary="Retrieve one item from an item scheme",
    description=(
        "Returns one Code from a Codelist, or one Concept from a ConceptScheme. "
        "The response is a StructureMessage containing the parent scheme with "
        "only the requested item populated — clients can use the surrounding "
        "scheme metadata (id, agency, version, name) without paying for the "
        "full item tree.\n\n"
        "**Implemented item schemes:** `codelist`, `conceptscheme`.\n\n"
        "**Spec-defined but not implemented (returns `501`):** `agencyscheme`, "
        "`categoryscheme`, `dataproviderscheme`, `dataconsumerscheme`, "
        "`organisationunitscheme`, `transformationscheme`, `rulesetscheme`, "
        "`userdefinedoperatorscheme`, `customtypescheme`, "
        "`namepersonalisationscheme`, `vtlmappingscheme`, `valuelist`."
    ),
    response_description="SDMX-JSON 2.0 (default) or SDMX-ML 2.1 / 3.0 via Accept header.",
    responses={**_STRUCTURE_200_CONTENT, **_ITEM_ERROR_RESPONSES},
)
async def get_structure_item(
    itemSchemeType: Annotated[  # noqa: N803 — SDMX-REST 2.2.2 spec param name
        str,
        Path(
            description="Item scheme type: `codelist` or `conceptscheme` (others 501).",
            openapi_examples={
                "codelist": Example(summary="A Code inside a Codelist", value="codelist"),
                "conceptscheme": Example(summary="A Concept inside a ConceptScheme", value="conceptscheme"),
            },
        ),
    ],
    agencyID: Annotated[  # noqa: N803
        str,
        Path(
            description="Maintainer agency ID of the parent scheme.",
            pattern=_AGENCY_PATTERN,
            openapi_examples=_AGENCY_EXAMPLES,
        ),
    ],
    resourceID: Annotated[  # noqa: N803
        str,
        Path(
            description="Parent scheme ID (a Codelist or ConceptScheme ID).",
            pattern=_ID_PATTERN,
            openapi_examples={
                "codelist": Example(summary="A Codelist", value="CL_UNIT_MEASURE"),
                "conceptscheme": Example(summary="A ConceptScheme", value="CROSS_DOMAIN_CONCEPTS"),
            },
        ),
    ],
    version: Annotated[
        str,
        Path(
            description="Parent scheme version.",
            pattern=_VERSION_PATTERN,
            openapi_examples=_VERSION_EXAMPLES,
        ),
    ],
    itemID: Annotated[  # noqa: N803
        str,
        Path(
            description="Item ID within the parent scheme (a Code id or a Concept id).",
            pattern=_ID_PATTERN,
            openapi_examples={
                "code": Example(summary="A Code id", value="_T"),
                "concept": Example(summary="A Concept id", value="REF_AREA"),
            },
        ),
    ],
    registry: Annotated[sl.LocalRegistry, Depends(get_registry)],
    asof: AsOf = None,
    accept: Annotated[str | None, Header()] = None,
    accept_language: Annotated[str | None, Header(alias="Accept-Language")] = None,
) -> Response:
    """Resolve a single item from its scheme and serialise it inside a StructureMessage."""
    reject_asof(asof)
    # See get_structure for the rationale — both spec wildcard tokens accepted at the boundary.
    agencyID = _normalize_wildcard(agencyID)  # noqa: N806
    resourceID = _normalize_wildcard(resourceID)  # noqa: N806
    version = _normalize_wildcard(version)
    itemID = _normalize_wildcard(itemID)  # noqa: N806
    ensure_acceptable(accept, _SUPPORTED_STRUCTURE_MEDIA)
    scheme_type_lower = itemSchemeType.lower()
    if scheme_type_lower in _SPEC_ITEM_SCHEME_TYPES_UNIMPLEMENTED:
        raise HTTPException(
            status_code=501,
            detail=f"itemSchemeType {itemSchemeType!r} is defined by SDMX-REST 2.2.2 but not implemented",
        )
    binding = _ITEM_SCHEME_TYPES.get(scheme_type_lower)
    if binding is None:
        raise HTTPException(status_code=400, detail=f"unknown itemSchemeType: {itemSchemeType}")
    cls, item_field = binding

    if version == _LATEST:
        resolved = _resolve_latest(registry, cls, agency=agencyID, id_=resourceID)
        if resolved is None:
            raise HTTPException(
                status_code=404,
                detail=f"not found: {cls.__name__}(agency={agencyID}, id={resourceID}, version=latest)",
            )
        version = resolved

    scheme_urn = cls.urn_for(agency=agencyID, id=resourceID, version=version)
    scheme = registry.get(scheme_urn)
    if scheme is None:
        raise HTTPException(status_code=404, detail=f"not found: {scheme_urn}")

    materialised = _materialize(scheme)
    items = list(getattr(materialised, item_field, []))
    matching = [it for it in items if getattr(it, "id", None) == itemID]
    if not matching:
        raise HTTPException(
            status_code=404,
            detail=f"item {itemID!r} not found in {scheme_urn}",
        )

    one_item_scheme = _evolve(materialised, **{item_field: matching})
    msg = _build_message(one_item_scheme, [])

    accept_lower = (accept or "").lower()
    # Browsers send `text/html,application/xhtml+xml,application/xml;q=0.9,…`.
    # The bare `"xml" in accept` check would dispatch them to SDMX-ML — what
    # they really want is the JSON body so they can pretty-print it inline.
    # Skip the XML branch when the request comes from a browser; the JSON
    # path further down downgrades Content-Type to `application/json` too.
    if "xml" in accept_lower and not is_browser_request(accept):
        sdmx_version = "3.0" if "3.0" in accept_lower else "2.1"
        body = to_xml(msg, version=sdmx_version)
        media = f"application/vnd.sdmx.structure+xml;version={sdmx_version}"
        return Response(content=body, media_type=media)

    return to_204_if_empty(
        localized_json_response(
            to_json(msg),
            media_type="application/vnd.sdmx.structure+json;version=2.0.0",
            accept_language=accept_language,
            accept=accept,
        )
    )


def _evolve(artefact: object, **changes: Any) -> object:
    """``attrs.evolve`` for an artefact whose static type is only ``object``.

    The stub/materialise helpers below deal in heterogeneous SDMX artefacts,
    so they are typed ``object`` — but pyrefly's attrs plugin resolves
    ``evolve``'s first argument against a *concrete* attrs class and has no
    spelling for "some attrs instance" (``AttrsInstance`` and ``cast`` both
    fail it). Every caller reaches here off an ``_ITEM_FIELD`` or
    ``_LAZY_STUB_SPECS`` lookup that only matches attrs classes, so the
    precondition is discharged by the guard, not by the type. Funnelled
    through one function so that costs one suppression instead of two.
    """
    # pyrefly: ignore[bad-argument-type]
    return evolve(artefact, **changes)


def _materialize(artefact: object) -> object:
    """Hydrate a Lazy* wrapper into its concrete attrs class; passthrough otherwise."""
    materialize = getattr(artefact, "materialize", None)
    return materialize() if callable(materialize) else artefact


def _stub(artefact: object) -> object:
    """Return a copy of an already-materialised artefact with its item field cleared."""
    field_name = _ITEM_FIELD.get(type(artefact))
    if field_name is None:
        return artefact
    return _evolve(artefact, **{field_name: []})


#: Lazy* wrapper class name -> (concrete class, the field holding its items).
#: The items are what make a lazy artefact expensive; everything else is read
#: off the wrapper.
_LAZY_STUB_SPECS: dict[str, tuple[type, str]] = {
    "LazyCodelist": (sl.Codelist, "codes"),
    "LazyHierarchy": (sl.Hierarchy, "tree"),
}


def _stub_lazy(artefact: object) -> object | None:
    """Build a stub directly from a Lazy* wrapper, or return None for non-lazy artefacts.

    The result must equal ``_stub(_materialize(artefact))`` — the same stub,
    without reading the items. So the fields are **derived from the concrete
    class**, not listed by hand: every constructor field except the items, read
    off the wrapper. A hand-kept list is how this used to go wrong. It named six
    fields, so annotations, validity dates, ``uri`` and extensions were dropped
    from every lazy stub — while artefacts with no lazy wrapper, stubbed through
    `_stub`, kept them all, so a stub's shape depended on whether storage happened
    to hand back a lazy object. Measured on a 100k-code codelist, reading the
    extra fields costs ~4 ms against ~565 ms to materialise.

    ``init=False`` fields are skipped: they are caches the class derives itself
    (``Hierarchy._codes``, ``_parent_of``), not constructor arguments, and the
    wrappers do not expose them.
    """
    spec = _LAZY_STUB_SPECS.get(type(artefact).__name__)
    if spec is None:
        return None
    cls, items_field = spec
    kwargs: dict[str, object] = {
        f.alias: getattr(artefact, f.name) for f in attrs_fields(cls) if f.init and f.name != items_field
    }
    kwargs[items_field] = []
    stub: object = cls(**kwargs)
    return stub


def _stub_of(artefact: object) -> object:
    """Stub ``artefact``, building it from its lazy wrapper when it has one.

    Test the lazy stub against ``None``, never for truthiness. `_stub_lazy`
    returns ``None`` to mean "not a lazy wrapper", but a stub it *does* build has
    its items emptied — and an empty ``Codelist`` is falsy, because sdmxlib 0.62
    gave it ``__len__`` (sdmxlib #398). Written as ``_stub_lazy(x) or ...``, the
    eleven call sites this replaces discarded every lazy codelist stub and
    materialised the whole codelist only to stub it again: ~5.6 ms per thousand
    codes, so ~3 s per stub on a codelist of half a million codes, on every request (#9).

    Only ``Codelist`` is sized today, but the other sites were one sdmxlib
    release away from the same bug, which is why every stub goes through here
    rather than through a copy of the expression.
    """
    stub = _stub_lazy(artefact)
    return stub if stub is not None else _stub(_materialize(artefact))


def _resolve_extras(
    artefact: object,
    references: str,
    detail: _Detail | str,
    registry: sl.LocalRegistry,
) -> list[object]:
    """Dispatch `references` to the right walker, then shape extras per `detail`.

    SDMX-REST 2.2.2 makes `detail` and `references` orthogonal — `detail`
    shapes the queried artefact, `references` shapes the bag of related
    artefacts returned alongside it. The reference-detail buckets
    (`referencestubs` etc) imply `references=descendants` by default if the
    caller didn't pass one, which is the spec contract.

    When `detail` is in the stub bucket, every related artefact is also
    stubbed — that's what `detail=allcompletestubs&references=children`
    means: the whole response is stubs.
    """
    extras: list[object]
    effective = references or "none"
    if effective in (None, "", "none") and detail in _DETAIL_REFERENCE_BUCKET:
        effective = "descendants"

    if effective in _REFERENCES_CHILDREN_BUCKET:
        extras = _resolve_children(artefact, registry)
    elif effective in _REFERENCES_DESCENDANT_BUCKET:
        extras = _walk_references_as_stubs(artefact, registry)
    elif effective in _REFERENCES_PARENTS_BUCKET:
        extras = _resolve_parents_and_siblings(artefact, registry)
    elif effective == "dataflow" and isinstance(artefact, sl.CategoryScheme):
        # SDMX-REST 2.2.2: ``references=<artefact-type>`` returns every
        # artefact of that type that references (or is referenced by) the
        # queried one. For CategoryScheme + `dataflow` this yields every
        # Dataflow tied to any Category in the scheme via a Categorisation.
        # sfs (the .Stat Solr indexer) crawls exactly this shape to build
        # its worklist.
        extras = _resolve_dataflows_referencing_scheme(artefact, registry)
    else:
        extras = []

    if detail in _DETAIL_STUB_BUCKET and extras:
        extras = [_stub_of(e) for e in extras]
    return extras


def _resolve_dataflows_referencing_scheme(
    scheme: sl.CategoryScheme,
    registry: sl.LocalRegistry,
) -> list[object]:
    """Return every Dataflow the scheme's categorisations point to.

    Walks the registry for Categorisations whose target URN sits under this
    scheme; deduplicates the source Dataflow URNs; fetches each Dataflow
    (stubbed) from the registry. Empty result when the scheme has no
    categorisations — the caller emits an empty ``dataflows[]`` array which
    is what sfs and other crawlers expect on that shape.
    """
    scheme_urn = str(sl.CategoryScheme.urn_for(agency=scheme.maintainer.id, id=scheme.id, version=scheme.version))
    scheme_prefix = scheme_urn.replace(
        "urn:sdmx:org.sdmx.infomodel.categoryscheme.CategoryScheme=",
        "urn:sdmx:org.sdmx.infomodel.categoryscheme.Category=",
    )
    df_urns: list[str] = []
    seen: set[str] = set()
    for cat_raw in registry.get_all(sl.Categorisation):
        materialize = getattr(cat_raw, "materialize", None)
        cat = materialize() if callable(materialize) else cat_raw
        if not isinstance(cat, sl.Categorisation) or cat.target is None or cat.source is None:
            continue
        target_urn = str(cat.target.urn)
        if not target_urn.startswith(scheme_prefix + "."):
            continue
        src_urn = str(cat.source.urn)
        if src_urn in seen:
            continue
        seen.add(src_urn)
        df_urns.append(src_urn)
    out: list[object] = []
    for urn_str in df_urns:
        df = registry.get(sl.SdmxUrn.parse(urn_str))
        if df is not None:
            out.append(df)
    return out


def _resolve_children(artefact: object, registry: sl.LocalRegistry) -> list[object]:
    """Resolve the artefacts that ``references=children`` should inline.

    For a Dataflow we return the full transitive set the consumer needs to
    render a UI: the DSD at full detail, plus every codelist and concept
    scheme referenced by the DSD's components as stubs. Strict SDMX-REST
    "children" is one hop (Dataflow → DSD), but every major SDMX server
    (Fusion Registry, OECD .Stat NSI WS) treats Dataflow children this way
    in practice — without the codelists + concept schemes, .Stat's
    Explorer can't render dimension labels or value pickers from a single
    request. Keeping strict semantics meant the DLM "related structures"
    listing came back empty.

    Other artefact classes have no canonical "child" reference in this
    bundle (a Codelist references nothing outbound), so they return [].
    """
    if isinstance(artefact, sl.Dataflow) and artefact.structure is not None:
        refs: list[object] = []
        dsd = registry.get(artefact.structure.urn)
        if dsd is None:
            return refs
        materialize = getattr(dsd, "materialize", None)
        if callable(materialize):
            dsd = materialize()
        refs.append(dsd)
        if isinstance(dsd, sl.DataStructure):
            refs.extend(_walk_dsd_outbound_refs(dsd, registry))
        return refs
    return []


def _resolve_parents_and_siblings(artefact: object, registry: sl.LocalRegistry) -> list[object]:
    """Resolve `references=parents` / `parentsandsiblings`.

    Strict SDMX-REST defines "parents" of a Dataflow as the Categorisations
    + CategoryScheme it sits under. This bundle has none. To keep the DLM
    "related structures" panel populated we widen the response to:

    - every other Dataflow in the registry (as siblings — loose, since
      they're not strictly in the same category, but it's what DLM shows)
    - the linked DSD (stubbed)

    Other artefact classes return [] — siblings of a Codelist or
    ConceptScheme aren't meaningful in this flat catalogue.
    """
    if not isinstance(artefact, sl.Dataflow):
        return []
    refs: list[object] = []
    self_agency = getattr(artefact.maintainer, "id", None) or ""
    self_urn = sl.Dataflow.urn_for(agency=self_agency, id=artefact.id, version=artefact.version)
    for other in registry.get_all(sl.Dataflow):
        other_agency = getattr(other.maintainer, "id", None) or ""
        other_urn = sl.Dataflow.urn_for(agency=other_agency, id=other.id, version=other.version)
        if str(other_urn) == str(self_urn):
            continue
        refs.append(_stub_of(other))
    if artefact.structure is not None:
        dsd = registry.get(artefact.structure.urn)
        if dsd is not None:
            refs.append(_stub_of(dsd))
    return refs


def _walk_references_as_stubs(
    artefact: object,
    registry: sl.LocalRegistry,
) -> list[object]:
    """Walk the artefact's reference graph and return each ref stubbed.

    For ``detail=referencestubs`` / ``referencecompletestubs``: the queried
    artefact is returned at full detail (the caller), and every referenced
    artefact is returned alongside it as a stub. We walk:

    - Dataflow → DataStructure (stubbed) → every codelist + concept scheme
      referenced by its dimensions and attributes.
    - DataStructure → same outbound walk (codelists + concept schemes).
    - Other artefact classes have no outgoing references to walk.

    Codelists and concept schemes are looked up in the registry and
    stubbed via the lazy/concrete path so heavy item trees (large
    geography codelists, etc.) don't materialise.
    """
    refs: list[object] = []
    dsd = _resolve_dsd_for_ref_walk(artefact, registry, refs)
    if dsd is not None:
        refs.extend(_walk_dsd_outbound_refs(dsd, registry))
        # SDMX-REST 2.2.2: a Dataflow's ``references=all`` walk includes every
        # Categorisation whose source is this dataflow, plus the pruned
        # CategoryScheme the target category lives under. sfs's crawl reads this
        # to build category-chain facet fields — without it every dataflow doc
        # loses its topic/product classification.
    if isinstance(artefact, sl.Dataflow):
        refs.extend(_walk_dataflow_categorisations(artefact, registry))
    return refs


def _walk_dataflow_categorisations(dataflow: sl.Dataflow, registry: sl.LocalRegistry) -> list[object]:
    """Return every Categorisation for this dataflow plus each target CategoryScheme (pruned).

    Walks the registry for Categorisations whose source URN is this
    dataflow's URN; for every matched Categorisation, includes the
    Categorisation itself plus the CategoryScheme its target belongs to.
    The scheme is emitted only once per (agency, id, version) triple; no
    tree-pruning is done — sfs walks the full scheme and only surfaces
    categories that have matching categorisations in the response.
    """
    df_urn = str(sl.Dataflow.urn_for(agency=dataflow.maintainer.id, id=dataflow.id, version=dataflow.version))
    out: list[object] = []
    seen_schemes: set[str] = set()
    for cat_raw in registry.get_all(sl.Categorisation):
        materialize = getattr(cat_raw, "materialize", None)
        cat = materialize() if callable(materialize) else cat_raw
        if not isinstance(cat, sl.Categorisation) or cat.source is None or cat.target is None:
            continue
        if str(cat.source.urn) != df_urn:
            continue
        out.append(cat)
        # Extract the scheme URN from the category URN.
        target_urn = str(cat.target.urn)
        scheme_prefix = target_urn.replace(
            "urn:sdmx:org.sdmx.infomodel.categoryscheme.Category=",
            "urn:sdmx:org.sdmx.infomodel.categoryscheme.CategoryScheme=",
        )
        # Strip the trailing ".{category_id}" from the scheme URN.
        scheme_urn = scheme_prefix.rsplit(".", 1)[0]
        if scheme_urn in seen_schemes:
            continue
        seen_schemes.add(scheme_urn)
        scheme = registry.get(sl.SdmxUrn.parse(scheme_urn))
        if scheme is not None:
            out.append(scheme)
    return out


def _walk_dsd_outbound_refs(dsd: sl.DataStructure, registry: sl.LocalRegistry) -> list[object]:
    """Stubbed Codelists + ConceptSchemes + HierarchyAssociations + Hierarchies referenced by ``dsd``.

    ``DSD.all_codelists`` gives every codelist referenced by a component's
    representation. Concept schemes have no equivalent accessor on the DSD
    in sdmxlib, so we walk dimensions / attributes / measures, extract
    each component's `concept` Ref URN, derive the scheme URN by stripping
    the item id, and dedupe.

    HierarchyAssociations bind hierarchies to dimensions on this DSD. We
    enumerate every HA in the registry, filter to those whose
    ``linked_object`` resolves to one of this DSD's dimensions, and
    include both the HA and the Hierarchy it points at. This is the
    SDMX 3-native discovery channel that .Stat 3.x and Fusion Registry
    consume directly (vs the SDMX 2.1 HCL shim + HIER_CONTEXT
    annotation .Stat 1.5 needs).
    """
    refs: list[object] = []
    seen: set[str] = set()
    # all_codelists values are LazyCodelist instances; rebuild the URN from
    # their (agency_id, id, version) since the lazy wrapper doesn't carry a
    # precomputed urn attribute.
    for codelist in dsd.all_codelists.values():
        cl_urn = sl.Codelist.urn_for(
            agency=codelist.agency_id,
            id=codelist.id,
            version=codelist.version,
        )
        _append_stub_by_urn(cl_urn, registry, refs, seen)
    for cs_urn in _concept_scheme_urns_for_dsd(dsd):
        _append_stub_by_urn(cs_urn, registry, refs, seen)
    _append_hierarchy_associations_for_dsd(dsd, registry, refs, seen)
    return refs


def _append_hierarchy_associations_for_dsd(
    dsd: sl.DataStructure,
    registry: sl.LocalRegistry,
    refs: list[object],
    seen: set[str],
) -> None:
    """Append each HierarchyAssociation bound to this DSD's dimensions + its target Hierarchy."""
    dim_ids = {dim.id for dim in dsd.dimensions if dim.id}
    # Encode the DSD coordinates once so we can quickly accept-or-reject each
    # HA's ``linked_object`` URN. The URN shape is
    # ``urn:sdmx:org.sdmx.infomodel.datastructure.Dimension=<ag>:<dsd-id>(<ver>).<dim-id>``.
    dim_urn_prefix = f"urn:sdmx:org.sdmx.infomodel.datastructure.Dimension={dsd.agency_id}:{dsd.id}({dsd.version})."
    for ha_raw in registry.get_all(sl.HierarchyAssociation):
        materialize = getattr(ha_raw, "materialize", None)
        ha = materialize() if callable(materialize) else ha_raw
        if not isinstance(ha, sl.HierarchyAssociation):
            continue
        linked_object = ha.linked_object
        linked_hierarchy = ha.linked_hierarchy
        if linked_object is None or linked_hierarchy is None:
            continue
        linked_obj_urn = str(getattr(linked_object, "urn", linked_object))
        if not linked_obj_urn.startswith(dim_urn_prefix):
            continue
        if linked_obj_urn[len(dim_urn_prefix) :] not in dim_ids:
            continue
            # The HA itself — referenced by stub through the same lazy/concrete
            # path the other walkers use, so its identity (URN/links) makes it
            # through URN injection.
        ha_key = str(sl.HierarchyAssociation.urn_for(agency=ha.agency_id, id=ha.id, version=ha.version))
        if ha_key not in seen:
            seen.add(ha_key)
            refs.append(_stub_of(ha))
            # The Hierarchy it points at, via the registry — lazy stub keeps the
            # tree out of the structure response (.Stat reads the SDMX 2.1
            # /hierarchicalcodelist endpoint for the actual codes).
        hier_urn = getattr(linked_hierarchy, "urn", None)
        if hier_urn is not None:
            _append_stub_by_urn(hier_urn, registry, refs, seen)


def _concept_scheme_urns_for_dsd(dsd: sl.DataStructure) -> list[sl.SdmxUrn[sl.ConceptScheme]]:
    """Unique ConceptScheme URNs referenced by ``dsd``'s components."""
    out: list[sl.SdmxUrn[sl.ConceptScheme]] = []
    seen: set[str] = set()
    for comp in [*dsd.dimensions, *dsd.attributes, *dsd.measures]:
        concept_ref = getattr(comp, "concept", None)
        urn = getattr(concept_ref, "urn", None) if concept_ref is not None else None
        if urn is None:
            continue
            # Concept URN: …Concept=AGENCY:SCHEME(version).item_id
            # Scheme URN: …ConceptScheme=AGENCY:SCHEME(version)
        scheme_urn = sl.ConceptScheme.urn_for(agency=urn.agency, id=urn.id, version=urn.version)
        key = str(scheme_urn)
        if key in seen:
            continue
        seen.add(key)
        out.append(scheme_urn)
    return out


def _resolve_dsd_for_ref_walk(
    artefact: object,
    registry: sl.LocalRegistry,
    refs: list[object],
) -> sl.DataStructure | None:
    """Pick the DSD whose codelists/concepts we should walk; append it to ``refs`` if reached via Dataflow."""
    if isinstance(artefact, sl.DataStructure):
        return artefact
    if isinstance(artefact, sl.Dataflow) and artefact.structure is not None:
        dsd = registry.get(artefact.structure.urn)
        if dsd is None:
            return None
        mat = getattr(dsd, "materialize", None)
        if callable(mat):
            dsd = mat()
        if isinstance(dsd, sl.DataStructure):
            refs.append(_stub(dsd))
            return dsd
    return None


def _append_stub_by_urn(
    urn: sl.SdmxUrn[object],
    registry: sl.LocalRegistry,
    refs: list[object],
    seen: set[str],
) -> None:
    """Look up ``urn`` in the registry, stub it via the lazy fast path, append once."""
    key = str(urn)
    if key in seen:
        return
    seen.add(key)
    art = registry.get(urn)
    if art is None:
        return
    refs.append(_stub_of(art))


def _build_message(primary: object, extras: list[object]) -> StructureMessage:
    if not extras:
        return StructureMessage.containing(primary)
    return _build_message_many([primary, *extras])


def _build_message_many(artefacts: list[object]) -> StructureMessage:
    """Group ``artefacts`` by their ``msg_field`` slot and wrap in one StructureMessage."""
    slots: dict[str, list[object]] = {}
    for art in artefacts:
        slot = getattr(type(art), "msg_field", None)
        if not isinstance(slot, str):
            continue
        slots.setdefault(slot, []).append(art)
    return StructureMessage(**slots)  # type: ignore[arg-type]


def _version_key(version: str) -> tuple[int, ...]:
    """Sort key for dotted-numeric SDMX versions (`"2.10"` > `"2.9"` > `"1.0"`)."""
    return tuple(int(p) for p in version.split("."))


def _resolve_latest(
    registry: sl.LocalRegistry,
    cls: type[sl.MaintainableArtefact],
    *,
    agency: str,
    id_: str,
) -> str | None:
    """Return the highest version of ``cls`` for the concrete (agency, id), or None.

    Used when the caller specifies `version=latest` against a single artefact
    (no wildcards in agency / id). Sorts by :func:`_version_key`, so `"2.10"`
    correctly outranks `"2.9"`.
    """
    versions: list[str] = []
    for art in registry.get_all(cls):
        a_urn = cls.urn_for(agency=art.agency_id, id=art.id, version=art.version)
        if a_urn.agency == agency and a_urn.id == id_:
            versions.append(a_urn.version)
    if not versions:
        return None
    return max(versions, key=_version_key)


def _enumerate_with_wildcards(  # noqa: C901
    registry: sl.LocalRegistry,
    cls: type[sl.MaintainableArtefact],
    *,
    agency: str,
    id_: str,
    version: str,
    detail: _Detail,
    references: str = "none",
) -> list[object]:
    """Enumerate every artefact of ``cls`` matching the partial (agency, id, version).

    Each segment may be ``all`` (the SDMX-REST 2.2.2 wildcard). Non-wildcard
    segments narrow the result by exact match against the artefact's URN. The
    catalog has no native multi-segment query, so we list-then-filter via
    ``Registry.get_all`` — fine at this dataset's scale (single-digit dataflows,
    sub-second across every other class).

    `version="latest"` keeps every match in the first pass, then reduces per
    (agency, id) group to the highest version (so `/structure/dataflow/all/all/latest`
    returns each dataflow at its highest version).
    """
    all_arts = registry.get_all(cls)
    version_is_open = version in (_WILDCARD, _LATEST)
    out: list[Any] = []
    for art in all_arts:
        a_urn = cls.urn_for(agency=art.agency_id, id=art.id, version=art.version)
        if agency not in (_WILDCARD, a_urn.agency):
            continue
        if id_ not in (_WILDCARD, a_urn.id):
            continue
        if not version_is_open and version != a_urn.version:
            continue
        out.append(art)

    if version == _LATEST:
        # Reduce to one-per-(agency, id) at the highest version.
        latest_per_group: dict[tuple[str, str], Any] = {}
        for art in out:
            a_urn = cls.urn_for(agency=art.agency_id, id=art.id, version=art.version)
            key = (a_urn.agency, a_urn.id)
            prev = latest_per_group.get(key)
            if prev is None:
                latest_per_group[key] = art
            else:
                prev_urn = cls.urn_for(agency=prev.agency_id, id=prev.id, version=prev.version)
                if _version_key(a_urn.version) > _version_key(prev_urn.version):
                    latest_per_group[key] = art
        out = list(latest_per_group.values())

        # Shape each enumerated artefact per `detail`, then walk + append the
        # references each one implies. The references walk is per-artefact, then
        # deduped across the whole result (no point inlining the same DSD twice
        # when two dataflows point at it).
    shaped: list[object] = []
    extras_seen: set[str] = set()
    for a in out:
        # As for one artefact: items only when the response carries them. In a
        # listing this used to be every codelist in the catalogue, loaded in full
        # to answer a request for their names.
        primary = _stub_of(a) if detail in _DETAIL_STUB_BUCKET else _materialize(a)
        shaped.append(primary)
        for extra in _resolve_extras(a, references, detail, registry):
            urn = _urn_str(extra)
            if urn is None or urn in extras_seen:
                continue
            extras_seen.add(urn)
            shaped.append(extra)
    return shaped


def _urn_str(artefact: object) -> str | None:
    """Best-effort URN string for an artefact (for dedup across an enumerated walk)."""
    urn_factory = getattr(type(artefact), "urn", None)
    if not callable(urn_factory):
        return None
    try:
        agency = getattr(getattr(artefact, "maintainer", None), "id", None) or getattr(artefact, "agency_id", "")
        return str(
            urn_factory(
                agency=agency,
                id=getattr(artefact, "id", ""),
                version=getattr(artefact, "version", "1.0"),
            )
        )
    except (TypeError, AttributeError):
        return None
