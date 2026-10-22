"""Content sdmxlib refuses to serialise is a 500 that names the artefact and the rule (#10).

A bundle can carry MetadataStructure attributes with no
``ConceptIdentity``. Every SDMX format rejects that, so sdmxlib refuses to
write it, and the route used to fail with Starlette's bare ``Internal Server
Error``: a consumer could see that the route failed, but not which artefact
or which rule. The fix is the producer's; this response is what makes it
quick to find.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sdmxlib as sl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sdmxlib_rest.app import Mount, RestSettings, create_app, library_routers
from sdmxlib_rest.runtime import CatalogRuntime
from tests.conftest import AGENCY, World, _WorldProvider, build_world, istr

pytestmark = pytest.mark.unit

BROKEN = "/structure/metadatastructure/AG/MSD_BROKEN/1.0"


@pytest.fixture
def broken_world() -> Iterator[World]:
    """The shared world plus one MSD whose attribute has no ConceptIdentity."""
    world = build_world()
    world.registry.add(
        sl.MetadataStructure(
            id="MSD_BROKEN",
            maintainer=AGENCY,
            version="1.0",
            name=istr("An MSD no format can write"),
            attributes=[sl.MetadataAttribute(id="NOTE")],
        )
    )
    yield world
    world.registry.close()


def _client(world: World, **kwargs: Any) -> TestClient:
    return TestClient(
        create_app(
            CatalogRuntime(_WorldProvider(world)),
            RestSettings(title="test-sdmx-api", version="0.1.0"),
            routers=list(library_routers()),
            **kwargs,
        )
    )


@pytest.mark.parametrize(
    "accept",
    [
        "application/vnd.sdmx.structure+json;version=2.0.0",
        "application/vnd.sdmx.structure+xml;version=3.0",
        "application/vnd.sdmx.structure+xml;version=2.1",
    ],
)
def test_unwritable_content_names_the_artefact_and_the_rule(broken_world: World, accept: str) -> None:
    with _client(broken_world) as client:
        resp = client.get(BROKEN, headers={"Accept": accept})

    assert resp.status_code == 500
    body = resp.json()
    assert body["target"] == BROKEN
    paths = [issue["path"] for issue in body["issues"]]
    assert any(path.startswith("MetadataStructure:MSD_BROKEN") for path in paths), paths
    assert any("ConceptIdentity" in issue["message"] for issue in body["issues"]), body["issues"]


def test_the_500_is_not_cacheable(broken_world: World) -> None:
    with _client(broken_world) as client:
        resp = client.get(BROKEN)

    assert resp.status_code == 500
    assert "etag" not in resp.headers
    assert "cache-control" not in resp.headers


def test_a_listing_names_the_one_artefact_that_failed(broken_world: World) -> None:
    """In a listing the request target names no artefact, so the issue path must."""
    with _client(broken_world) as client:
        resp = client.get("/structure/metadatastructure/AG/all/latest")

    assert resp.status_code == 500
    assert any(issue["path"].startswith("MetadataStructure:MSD_BROKEN") for issue in resp.json()["issues"])


def test_a_mounted_app_answers_the_same_way(broken_world: World) -> None:
    """A mounted FastAPI app answers its own unhandled errors, so it needs the handler too."""
    sub = FastAPI()
    for router in library_routers():
        sub.include_router(router)
    with _client(broken_world, mounts=[Mount("/v1", sub)]) as client:
        resp = client.get(f"/v1{BROKEN}")

    assert resp.status_code == 500
    assert any(issue["path"].startswith("MetadataStructure:MSD_BROKEN") for issue in resp.json()["issues"])


def test_writable_content_is_unaffected(broken_world: World) -> None:
    """The discrimination check: the handler fires on refusal, not on every metadata structure."""
    with _client(broken_world) as client:
        resp = client.get("/structure/metadatastructure/AG/MSD_INFO/1.0")

    assert resp.status_code == 200, resp.text
