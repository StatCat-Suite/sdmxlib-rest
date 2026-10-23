"""SDMX REST 2.x reference-metadata endpoints (`/metadata/...`)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal

import duckdb
import sdmxlib as sl
from attrs import evolve
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from fastapi.openapi.models import Example
from fastapi.responses import Response
from sdmxlib.formats import to_json, to_xml
from sdmxlib.model.message import MetadataMessage

from sdmxlib_rest.localization import localized_json_response
from sdmxlib_rest.metadata_store import MetadataStore
from sdmxlib_rest.negotiation import ensure_acceptable, is_browser_request
from sdmxlib_rest.responses.asof import AsOf, reject_asof
from sdmxlib_rest.responses.empty import to_204_if_empty
from sdmxlib_rest.runtime import get_meta_conn, get_metadata_store, get_registry

router = APIRouter(tags=["metadata"])


_JSON_MEDIA = "application/vnd.sdmx.metadata+json;version=2.0.0"
_XML_MEDIA_3 = "application/vnd.sdmx.metadata+xml;version=3.0"
_SUPPORTED_METADATA_MEDIA = [_JSON_MEDIA, _XML_MEDIA_3]

# SDMX 2.1 / 3.0 IDType + NestedIDType + version patterns from
# SDMXCommonReferences.xsd. Applied to Path() so malformed refs fail with
# 422 at the framework boundary instead of bubbling through to a ValueError
# in sdmxlib's URN parser (which would surface as 500).
_AGENCY_PATTERN = r"^[A-Za-z0-9_@$-]+(\.[A-Za-z0-9_@$-]+)*$"
_ID_PATTERN = r"^[A-Za-z0-9_@$-]+$"
_VERSION_PATTERN = r"^[0-9]+(\.[0-9]+)*$"

# Shared error-response declarations so the OpenAPI spec matches the actual
# HTTPException(400 / 404) raises in the route bodies. FastAPI's HTTPException
# always serialises as {"detail": "<message>"}.
_ERROR_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"detail": {"type": "string"}},
    "required": ["detail"],
}
_NOT_FOUND_RESPONSE = {
    "description": "Artefact not found.",
    "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
}
_BAD_REQUEST_RESPONSE = {
    "description": "Malformed reference (e.g. unknown artefact type).",
    "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
}
_NOT_ACCEPTABLE_RESPONSE = {
    "description": "Accept header doesn't intersect the route's supported media types.",
    "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
}
# 413 / 414 are ingress-enforced; declared so clients can distinguish them.
_PAYLOAD_TOO_LARGE_RESPONSE = {
    "description": "Payload Too Large — enforced by the ingress.",
    "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
}
_URI_TOO_LONG_RESPONSE = {
    "description": "URI Too Long — enforced by the ingress (~8 KiB default).",
    "content": {"application/json": {"schema": _ERROR_RESPONSE_SCHEMA}},
}
# Declare the actual content types the routes emit so schemathesis +
# Schemathesis-style clients don't see drift between spec and runtime.
# FastAPI's default 200 model documents `application/json`, but these
# routes use the SDMX-versioned media types instead.
_METADATA_200_CONTENT: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "SDMX metadata message.",
        "content": {
            _JSON_MEDIA: {},
            _XML_MEDIA_3: {},
        },
    },
}


# Per-parameter Swagger examples. We deliberately don't share example names
# across parameters: the Swagger UI doesn't link dropdowns, so users who
# picked the same scenario name in two different params got incoherent
# requests in practice. Each route below carries a "useful pairings" table
# in its description for cross-param discovery.
_PROVIDER_ID_EXAMPLES: dict[str, Example] = {
    "provider": Example(summary="A metadata provider ID", value="SDMX"),
    "all": Example(summary="Wildcard: disable the provider filter (on /metadataflow only)", value="all"),
}

_METADATASET_RESOURCE_ID_EXAMPLES: dict[str, Example] = {
    "metadataset": Example(summary="A MetadataSet ID", value="MS_INFO"),
}

_METADATAFLOW_AGENCY_EXAMPLES: dict[str, Example] = {
    "agency": Example(summary="A maintenance agency ID", value="SDMX"),
}

_METADATAFLOW_RESOURCE_ID_EXAMPLES: dict[str, Example] = {
    "metadataflow": Example(summary="A Metadataflow ID", value="MDF_INFO"),
}

_STRUCTURE_TYPE_EXAMPLES: dict[str, Example] = {
    "dataflow": Example(summary="A Dataflow", value="dataflow"),
    "datastructure": Example(summary="A DataStructure (DSD)", value="datastructure"),
    "conceptscheme": Example(summary="A ConceptScheme", value="conceptscheme"),
    "codelist": Example(summary="A Codelist", value="codelist"),
    "hierarchy": Example(summary="A Hierarchy", value="hierarchy"),
}

_STRUCTURE_ATTACHED_AGENCY_EXAMPLES: dict[str, Example] = {
    "agency": Example(summary="A maintenance agency ID", value="SDMX"),
}

_STRUCTURE_ATTACHED_RESOURCE_ID_EXAMPLES: dict[str, Example] = {
    "resource": Example(summary="An artefact ID matching the structureType", value="DF_EXAMPLE"),
}

_VERSION_EXAMPLES: dict[str, Example] = {
    "1.0": Example(summary="An explicit version", value="1.0"),
}

_DETAIL_EXAMPLES: dict[str, Example] = {
    "full": Example(summary="Full materialisation including reported attributes", value="full"),
    "allstubs": Example(summary="Header only — attribute tree stripped (cheap listings)", value="allstubs"),
}


def _serialise(sets: Sequence[sl.MetadataSet], accept: str | None, accept_language: str | None = None) -> Response:
    """Wrap MetadataSets in a MetadataMessage and serialise per Accept (+ Accept-Language for JSON)."""
    msg = MetadataMessage(metadata_sets=list(sets))
    # Browser default Accept includes `application/xml` — that's not a real
    # SDMX-ML request; fall through to JSON so the browser renders inline.
    if accept and "xml" in accept.lower() and not is_browser_request(accept):
        return Response(content=to_xml(msg, version="3.0"), media_type=_XML_MEDIA_3)
        # 204 No Content when the MetadataSet list is empty.
    return to_204_if_empty(
        localized_json_response(to_json(msg), media_type=_JSON_MEDIA, accept_language=accept_language, accept=accept)
    )


def _metadataset_urn(agency: str, id_: str, version: str) -> sl.SdmxUrn[sl.MetadataSet]:
    return sl.MetadataSet.urn_for(agency=agency, id=id_, version=version)


def _metadataflow_urn(agency: str, id_: str, version: str) -> sl.SdmxUrn[sl.Metadataflow]:
    return sl.Metadataflow.urn_for(agency=agency, id=id_, version=version)


def _structure_urn_for(structure_type: str, agency: str, id_: str, version: str) -> str:
    cls: type[sl.MaintainableArtefact] | None = {
        "dataflow": sl.Dataflow,
        "datastructure": sl.DataStructure,
        "conceptscheme": sl.ConceptScheme,
        "codelist": sl.Codelist,
        "hierarchy": sl.Hierarchy,
    }.get(structure_type.lower())
    if cls is None:
        raise HTTPException(status_code=400, detail=f"unknown structureType: {structure_type}")
    return str(cls.urn_for(agency=agency, id=id_, version=version))

    # SDMX-REST 2.2.2 keys MetadataSets by `{providerID}`. How that maps to the
    # stored linkage is the consumer's concern — see the injected MetadataStore.


@router.get(
    "/metadata/metadataset/{providerID}/{resourceID}/{version}",
    summary="Retrieve one MetadataSet by URN",
    description=(
        "Retrieve one MetadataSet by its `(providerID, resourceID, version)`. "
        "`providerID` maps to the MetadataSet URN's maintaining-agency segment."
    ),
    response_description="SDMX-JSON 2.0 metadata message (default) or SDMX-ML 3.0 GenericMetadata via Accept.",
    responses={
        **_METADATA_200_CONTENT,
        404: _NOT_FOUND_RESPONSE,
        406: _NOT_ACCEPTABLE_RESPONSE,
        413: _PAYLOAD_TOO_LARGE_RESPONSE,
        414: _URI_TOO_LONG_RESPONSE,
    },
)
async def get_metadataset(
    providerID: Annotated[  # noqa: N803 — SDMX-REST 2.2.2 spec param name
        str,
        Path(
            description=(
                "Data provider that published this MetadataSet. In this server's bundle, "
                "providerID is the maintaining agency from the MetadataSet's URN — "
                "see docs/sdmx-rest-conformance.md."
            ),
            pattern=_AGENCY_PATTERN,
            openapi_examples=_PROVIDER_ID_EXAMPLES,
        ),
    ],
    resourceID: Annotated[  # noqa: N803
        str,
        Path(
            description="MetadataSet ID — pair with the matching providerID (see the route description).",
            pattern=_ID_PATTERN,
            openapi_examples=_METADATASET_RESOURCE_ID_EXAMPLES,
        ),
    ],
    version: Annotated[
        str,
        Path(description="Version.", pattern=_VERSION_PATTERN, openapi_examples=_VERSION_EXAMPLES),
    ],
    registry: Annotated[sl.LocalRegistry, Depends(get_registry)],
    asof: AsOf = None,
    accept: Annotated[str | None, Header()] = None,
    accept_language: Annotated[str | None, Header(alias="Accept-Language")] = None,
) -> Response:
    """Resolve one populated MetadataSet by URN."""
    reject_asof(asof)
    ensure_acceptable(accept, _SUPPORTED_METADATA_MEDIA)
    urn = _metadataset_urn(providerID, resourceID, version)
    ms = registry.get(urn)
    if ms is None:
        raise HTTPException(status_code=404, detail=f"not found: {urn}")
    return _serialise([ms], accept, accept_language)


@router.get(
    "/metadata/metadataflow/{agencyID}/{resourceID}/{version}/{providerID}",
    summary="List MetadataSets under a Metadataflow",
    description=(
        "List every MetadataSet attached to the given Metadataflow. Set "
        "`providerID=all` to list every set under the flow; use a specific "
        "provider ID to narrow."
    ),
    response_description="Metadata message containing every set the flow holds.",
    responses={
        **_METADATA_200_CONTENT,
        404: _NOT_FOUND_RESPONSE,
        406: _NOT_ACCEPTABLE_RESPONSE,
        413: _PAYLOAD_TOO_LARGE_RESPONSE,
        414: _URI_TOO_LONG_RESPONSE,
    },
)
async def get_metadataflow_sets(
    agencyID: Annotated[  # noqa: N803
        str,
        Path(
            description="Metadataflow agency.",
            pattern=_AGENCY_PATTERN,
            openapi_examples=_METADATAFLOW_AGENCY_EXAMPLES,
        ),
    ],
    resourceID: Annotated[  # noqa: N803
        str,
        Path(
            description="Metadataflow ID — pair with the matching agencyID (see the route description).",
            pattern=_ID_PATTERN,
            openapi_examples=_METADATAFLOW_RESOURCE_ID_EXAMPLES,
        ),
    ],
    version: Annotated[
        str,
        Path(
            description="Metadataflow version.",
            pattern=_VERSION_PATTERN,
            openapi_examples=_VERSION_EXAMPLES,
        ),
    ],
    providerID: Annotated[  # noqa: N803
        str,
        Path(
            description=(
                "Restrict to sets published by this provider. Use `all` to disable the filter. "
                "Mapped to the MetadataSet URN's maintaining-agency segment — see "
                "docs/sdmx-rest-conformance.md for the spec-vs-implementation alignment."
            ),
            # NB: allow the literal `all` keyword in addition to the SDMX ID pattern.
            pattern=rf"^(all|{_AGENCY_PATTERN[1:-1]})$",
            openapi_examples=_PROVIDER_ID_EXAMPLES,
        ),
    ],
    registry: Annotated[sl.LocalRegistry, Depends(get_registry)],
    conn: Annotated[duckdb.DuckDBPyConnection, Depends(get_meta_conn)],
    store: Annotated[MetadataStore, Depends(get_metadata_store)],
    detail: Annotated[
        Literal["full", "allstubs"],
        Query(
            description="`full` returns reported attributes; `allstubs` returns headers only.",
            openapi_examples=_DETAIL_EXAMPLES,
        ),
    ] = "allstubs",
    limit: Annotated[
        int,
        Query(ge=1, le=1000, description="Max sets to materialise (full detail can be heavy)."),
    ] = 100,
    asof: AsOf = None,
    accept: Annotated[str | None, Header()] = None,
    accept_language: Annotated[str | None, Header(alias="Accept-Language")] = None,
) -> Response:
    """List every MetadataSet attached to the given Metadataflow, optionally narrowed to one provider."""
    reject_asof(asof)
    ensure_acceptable(accept, _SUPPORTED_METADATA_MEDIA)
    flow_urn = _metadataflow_urn(agencyID, resourceID, version)
    set_urns = store.sets_for_structure(
        conn,
        structure_urn=str(flow_urn),
        provider_id=None if providerID == "all" else providerID,
        limit=limit,
    )
    if not set_urns:
        # Distinguish "flow has zero sets" (200, empty) from "flow doesn't exist" (404).
        if registry.get(flow_urn) is None:
            raise HTTPException(status_code=404, detail=f"not found: {flow_urn}")
        return _serialise([], accept, accept_language)

    # `expect=` is the isinstance this site would write by hand (sdmxlib 0.62.2):
    # a store row can legitimately name something that is not a MetadataSet, and
    # before the check existed such a row reached the writer as one and failed
    # there. `is not None` rather than truthiness — an empty set is still a hit.
    sets = [
        ms for urn_str in set_urns if (ms := registry.get(sl.SdmxUrn.parse(urn_str), expect=sl.MetadataSet)) is not None
    ]
    if detail == "allstubs":
        sets = [_strip_attributes(ms) for ms in sets]
    return _serialise(sets, accept, accept_language)


@router.get(
    "/metadata/structure/{structureType}/{agencyID}/{resourceID}/{version}",
    summary="List MetadataSets attached to a structural artefact",
    description=(
        "List every MetadataSet whose target is the given structural artefact — "
        "matching both exact targets and item-level targets under it (e.g. "
        "metadata attached to individual Concepts of a ConceptScheme). Cap heavy "
        "item-scheme queries with `limit` + `detail=allstubs`."
    ),
    response_description="Metadata message: every set whose target_urn is this artefact, or an item under it.",
    responses={
        **_METADATA_200_CONTENT,
        400: _BAD_REQUEST_RESPONSE,
        406: _NOT_ACCEPTABLE_RESPONSE,
        413: _PAYLOAD_TOO_LARGE_RESPONSE,
        414: _URI_TOO_LONG_RESPONSE,
    },
)
async def get_structure_attached_sets(
    structureType: Annotated[  # noqa: N803
        str,
        Path(
            description="Structural artefact type to query targets for.",
            openapi_examples=_STRUCTURE_TYPE_EXAMPLES,
        ),
    ],
    agencyID: Annotated[  # noqa: N803
        str,
        Path(pattern=_AGENCY_PATTERN, openapi_examples=_STRUCTURE_ATTACHED_AGENCY_EXAMPLES),
    ],
    resourceID: Annotated[  # noqa: N803
        str,
        Path(
            description="Pair with the matching structureType+agencyID (see the route description).",
            pattern=_ID_PATTERN,
            openapi_examples=_STRUCTURE_ATTACHED_RESOURCE_ID_EXAMPLES,
        ),
    ],
    version: Annotated[
        str,
        Path(pattern=_VERSION_PATTERN, openapi_examples=_VERSION_EXAMPLES),
    ],
    registry: Annotated[sl.LocalRegistry, Depends(get_registry)],
    conn: Annotated[duckdb.DuckDBPyConnection, Depends(get_meta_conn)],
    store: Annotated[MetadataStore, Depends(get_metadata_store)],
    detail: Annotated[
        Literal["full", "allstubs"],
        Query(
            description="`full` materialises each set; `allstubs` returns headers only.",
            openapi_examples=_DETAIL_EXAMPLES,
        ),
    ] = "full",
    limit: Annotated[
        int,
        Query(ge=1, le=1000, description="Max sets to materialise."),
    ] = 500,
    asof: AsOf = None,
    accept: Annotated[str | None, Header()] = None,
    accept_language: Annotated[str | None, Header(alias="Accept-Language")] = None,
) -> Response:
    """Return every MetadataSet attached (via ``metadata_target``) to the given structural artefact.

    Matches both exact targets and item-level targets under the artefact —
    e.g. dictionary entries attached to individual Concepts of a
    ConceptScheme — so a single call surfaces the full picture of metadata
    attached to an artefact and its items.
    """
    reject_asof(asof)
    ensure_acceptable(accept, _SUPPORTED_METADATA_MEDIA)
    target_urn = _structure_urn_for(structureType, agencyID, resourceID, version)
    # Match exact targets and item-level targets. Items live under a sibling
    # artefact-class in the same package (ConceptScheme → Concept,
    # Codelist → Code, Hierarchy → HierarchicalCode), so the item URN looks
    # like `<package>.<ItemClass>=<AGENCY>:<ID>(<VERSION>).<item_id>`. We
    # derive that LIKE pattern when an item class exists for this artefact.
    item_pattern = _item_urn_prefix(target_urn)
    set_urns = store.sets_for_target(conn, target_urn=target_urn, item_pattern=item_pattern, limit=limit)
    # `expect=` is the isinstance this site would write by hand (sdmxlib 0.62.2):
    # a store row can legitimately name something that is not a MetadataSet, and
    # before the check existed such a row reached the writer as one and failed
    # there. `is not None` rather than truthiness — an empty set is still a hit.
    sets = [
        ms for urn_str in set_urns if (ms := registry.get(sl.SdmxUrn.parse(urn_str), expect=sl.MetadataSet)) is not None
    ]
    if detail == "allstubs":
        sets = [_strip_attributes(ms) for ms in sets]
    return _serialise(sets, accept, accept_language)


_ITEM_CLASS_FOR = {
    "ConceptScheme": "Concept",
    "Codelist": "Code",
    "Hierarchy": "HierarchicalCode",
    "CategoryScheme": "Category",
    "AgencyScheme": "Agency",
}


def _item_urn_prefix(parent_urn: str) -> str | None:
    """Build a SQL LIKE pattern that matches item URNs under ``parent_urn``.

    Returns ``None`` when the parent's artefact class doesn't carry items
    (e.g. Dataflow, DataStructure) — caller should fall back to exact-match.
    """
    # URN shape: urn:sdmx:org.sdmx.infomodel.<pkg>.<Class>=<AGENCY>:<ID>(<VER>)
    head, _, payload = parent_urn.partition("=")
    prefix, _, parent_class = head.rpartition(".")
    item_class = _ITEM_CLASS_FOR.get(parent_class)
    if item_class is None:
        return None
    return f"{prefix}.{item_class}={payload}.%"


def _strip_attributes(ms: sl.MetadataSet) -> sl.MetadataSet:
    """Return a copy of ``ms`` with the (heavy) reported-attribute tree cleared."""
    return evolve(ms, attributes=[])
