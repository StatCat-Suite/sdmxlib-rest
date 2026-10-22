"""Behavioural coverage for ``/metadata``.

The smallest of the three route modules — every handler resolves a MetadataSet
by URN and little else — but it has two behaviours worth pinning: an empty
result is a 204 rather than an empty message, and MetadataMessage is SDMX-ML
3.0 only.

The reported-attribute half of this surface lives behind the ``MetadataStore``
seam, whose default is inert by design, so ``detail=full`` is exercised here
only as far as the default store takes it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


def test_a_metadataset_resolves_by_its_urn(client: TestClient) -> None:
    """The single-set route addresses one MetadataSet by provider/id/version."""
    resp = client.get("/metadata/metadataset/AG/MDS_1/1.0.0")

    assert resp.status_code == 200
    sets = resp.json()["data"]["metadataSets"]
    assert [s["id"] for s in sets] == ["MDS_1"]


def test_an_absent_metadataset_is_404(client: TestClient) -> None:
    """A miss is an error, not an empty message."""
    resp = client.get("/metadata/metadataset/AG/NO_SUCH_SET/1.0")
    assert resp.status_code == 404


def test_a_metadataflow_with_no_sets_is_204(client: TestClient) -> None:
    """An empty list is No Content — a body with zero sets would be misleading.

    The fixture's sets are not attached to any flow, so the flow listing is
    legitimately empty and exercises the 204 path rather than an error one.
    """
    resp = client.get("/metadata/metadataflow/AG/MDF_INFO/1.0/all")

    assert resp.status_code == 204
    assert resp.content == b""


def test_an_absent_metadataflow_is_not_a_500(client: TestClient) -> None:
    """An unknown flow resolves to nothing; it must not raise."""
    resp = client.get("/metadata/metadataflow/AG/NO_SUCH_FLOW/1.0/all")
    assert resp.status_code in {204, 404}


def test_metadata_is_served_as_sdmx_ml_3_0(client: TestClient) -> None:
    """MetadataMessage has no SDMX-ML 2.1 encoding, so the writer pins 3.0."""
    resp = client.get(
        "/metadata/metadataset/AG/MDS_1/1.0.0",
        headers={"Accept": "application/vnd.sdmx.metadata+xml;version=3.0"},
    )

    assert resp.status_code == 200
    assert resp.content.startswith(b"<?xml")


def test_a_browser_accept_header_still_gets_json(client: TestClient) -> None:
    """Same browser carve-out as the structure route, and for the same reason."""
    resp = client.get(
        "/metadata/metadataset/AG/MDS_1/1.0.0",
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
    )

    assert resp.status_code == 200
    assert "json" in resp.headers["content-type"]


def test_an_unacceptable_media_type_is_406(client: TestClient) -> None:
    """Negotiation refuses rather than guessing."""
    resp = client.get(
        "/metadata/metadataset/AG/MDS_1/1.0.0",
        headers={"Accept": "application/pdf"},
    )
    assert resp.status_code == 406
