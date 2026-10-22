"""Behavioural coverage for ``/structure`` — the route the migration touches most.

``routes/structure.py`` is the biggest of the three route modules and took the
brunt of the sdmxlib 0.62 port — the registry seam, every URN builder call, and
six now-deleted registry-type guards. So this file pins what the route
*returns* rather than how it gets there: URN spelling, artefact shape under
``detail``, which related artefacts ``references`` inlines, and the JSON/XML
negotiation split.

Asserting on internals would mean a test rewritten by the same change it is
supposed to guard — which is why the port touched none of these.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

# The route writes ``version=2.0.0``; SdmxVersionAdvertiseMiddleware rewrites the
# token to ``2.0`` on the way out. The wire value is the contract, so assert on it.
JSON_MEDIA = "application/vnd.sdmx.structure+json;version=2.0"


def _artefacts(payload: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    """The ``kind`` collection out of an SDMX-JSON 2.0 structure message."""
    found: list[dict[str, Any]] = payload.get("data", {}).get(kind, [])
    return found


def test_a_dataflow_resolves_by_its_coordinates(client: TestClient) -> None:
    """The happy path: agency/id/version addresses exactly one artefact."""
    resp = client.get("/structure/dataflow/AG/DF_POP/1.0")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(JSON_MEDIA)
    flows = _artefacts(resp.json(), "dataflows")
    assert [f["id"] for f in flows] == ["DF_POP"]
    assert flows[0]["name"] == "Population"


def test_a_maintainable_states_its_own_urn_in_links(client: TestClient) -> None:
    """``links[rel=self].urn`` is what makes a response's cross-references resolvable.

    This used to be spliced in post-serialise by a module here; sdmxlib 0.62.2
    writes it (pinax-suite/sdmxlib#406) and the splice is gone. Pinned because
    deleting our own guarantee is exactly when the library's needs a guard —
    and because the property form is deliberately *not* emitted: SDMX-JSON 2.1
    closes its objects, so a top-level ``urn`` makes the document invalid.
    """
    resp = client.get("/structure/dataflow/AG/DF_POP/1.0")

    flow = _artefacts(resp.json(), "dataflows")[0]
    self_links = [link for link in flow.get("links", []) if link.get("rel") == "self"]
    assert [link["urn"] for link in self_links] == ["urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow=AG:DF_POP(1.0)"]
    assert "urn" not in flow


def test_an_absent_artefact_is_404_not_an_empty_message(client: TestClient) -> None:
    """A miss must not render as a valid message with nothing in it."""
    resp = client.get("/structure/dataflow/AG/NO_SUCH_FLOW/1.0")
    assert resp.status_code == 404


def test_a_codelist_carries_its_codes(client: TestClient) -> None:
    """``detail=full`` (the default) materialises items off the lazy wrapper."""
    resp = client.get("/structure/codelist/AG/CL_GEO/1.0")

    assert resp.status_code == 200
    codes = _artefacts(resp.json(), "codelists")[0]["codes"]
    assert [c["id"] for c in codes] == ["CA", "US"]


def test_allstubs_strips_the_items_but_keeps_the_artefact(client: TestClient) -> None:
    """The stub bucket answers "which codelists exist", not "what is in them"."""
    resp = client.get("/structure/codelist/AG/CL_GEO/1.0", params={"detail": "allstubs"})

    assert resp.status_code == 200
    codelist = _artefacts(resp.json(), "codelists")[0]
    assert codelist["id"] == "CL_GEO"
    assert codelist.get("codes", []) == []


def test_references_children_inlines_the_dsd_the_flow_points_at(client: TestClient) -> None:
    """``references`` controls related artefacts; a flow's child is its DSD."""
    resp = client.get("/structure/dataflow/AG/DF_POP/1.0", params={"references": "children"})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [d["id"] for d in data["dataflows"]] == ["DF_POP"]
    assert "DSD_POP" in [d["id"] for d in data.get("dataStructures", [])]


def test_detail_and_references_are_independent(client: TestClient) -> None:
    """The queried artefact is stubbed while its references still resolve.

    These two knobs were conflated once already: ``references`` was dropped
    whenever ``detail`` fell in the stub bucket, which broke the
    ``?references=children&detail=allcompletestubs`` shape .Stat clients send
    for structure listings.
    """
    resp = client.get(
        "/structure/dataflow/AG/DF_POP/1.0",
        params={"detail": "allcompletestubs", "references": "children"},
    )

    assert resp.status_code == 200
    assert "DSD_POP" in [d["id"] for d in resp.json()["data"].get("dataStructures", [])]


def test_a_wildcard_enumerates_every_artefact_of_the_type(client: TestClient) -> None:
    """``all`` goes down the enumerator branch, which needs a registry that can list."""
    resp = client.get("/structure/codelist/all/all/all")

    assert resp.status_code == 200
    assert sorted(c["id"] for c in _artefacts(resp.json(), "codelists")) == ["CL_GEO", "CL_SEX"]


@pytest.mark.parametrize("listing", ["AG/all/latest", "all/all/all"])
def test_a_listing_inlines_the_references_a_single_artefact_does(client: TestClient, listing: str) -> None:
    """A listing's references are the single artefact's, each once.

    They were dropped whole: the enumerator dedups references by URN, built by
    calling ``type(artefact).urn``, and once sdmxlib made ``urn`` a property
    nothing could be built, so every reference was skipped as unidentifiable.
    """
    one = client.get("/structure/dataflow/AG/DF_POP/1.0", params={"references": "children"}).json()
    many = client.get(f"/structure/dataflow/{listing}", params={"references": "children"}).json()

    for kind in ("dataStructures", "conceptSchemes", "codelists"):
        expected = sorted(a["id"] for a in _artefacts(one, kind))
        assert expected, f"the discrimination check: a single dataflow inlines {kind}"
        assert sorted(a["id"] for a in _artefacts(many, kind)) == expected, kind


def test_latest_resolves_to_the_one_concrete_version(client: TestClient) -> None:
    """``latest`` with a concrete agency+id narrows to a single artefact."""
    resp = client.get("/structure/dataflow/AG/DF_POP/latest")

    assert resp.status_code == 200
    assert [f["id"] for f in _artefacts(resp.json(), "dataflows")] == ["DF_POP"]


def test_an_unknown_structure_type_is_rejected(client: TestClient) -> None:
    """A typo in the resource segment is a client error, not a 404 or a 500."""
    resp = client.get("/structure/dataflowz/AG/DF_POP/1.0")
    assert resp.status_code in {400, 404}


@pytest.mark.parametrize(
    ("accept", "expected_version"),
    [
        ("application/vnd.sdmx.structure+xml;version=2.1", "2.1"),
        ("application/vnd.sdmx.structure+xml;version=3.0", "3.0"),
    ],
)
def test_xml_is_served_at_the_negotiated_version(client: TestClient, accept: str, expected_version: str) -> None:
    """Both SDMX-ML generations are reachable, and the type echoes which one."""
    resp = client.get("/structure/codelist/AG/CL_GEO/1.0", headers={"Accept": accept})

    assert resp.status_code == 200
    assert resp.headers["content-type"] == f"application/vnd.sdmx.structure+xml;version={expected_version}"
    assert resp.content.startswith(b"<?xml")


def test_a_browser_accept_header_still_gets_json(client: TestClient) -> None:
    """A browser sends ``application/xml;q=0.9``; it wants the inline-viewable body.

    The naive ``"xml" in accept`` dispatch sent browsers to SDMX-ML, which they
    download instead of rendering.
    """
    resp = client.get(
        "/structure/codelist/AG/CL_GEO/1.0",
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
    )

    assert resp.status_code == 200
    assert "json" in resp.headers["content-type"]


def test_an_unacceptable_media_type_is_406(client: TestClient) -> None:
    """Content negotiation refuses rather than guessing a format."""
    resp = client.get("/structure/codelist/AG/CL_GEO/1.0", headers={"Accept": "application/pdf"})
    assert resp.status_code == 406
