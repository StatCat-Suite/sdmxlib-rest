"""Content sdmxlib refuses to serialise is a 500 that names the artefact and the rule (#10).

What every format rejects, sdmxlib now refuses as an artefact is built, so it
never reaches a catalogue. What remains is content one format cannot carry: a
DataStructure with no measures is valid SDMX 3.0, but SDMX-ML 2.1 requires a
measure, so sdmxlib refuses to write it there. The route used to fail on such
a refusal with Starlette's bare ``Internal Server Error``: a consumer could see
that the route failed, but not which artefact or which rule. The fix is the
producer's; this response is what makes it quick to find.
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

BROKEN = "/structure/datastructure/AG/DSD_NOMEAS/1.0"
SDMX_ML_21 = "application/vnd.sdmx.structure+xml;version=2.1"


@pytest.fixture
def broken_world() -> Iterator[World]:
    """The shared world plus one DSD with no measures, which only SDMX-ML 2.1 cannot carry."""
    world = build_world()
    pop = world.registry.get(sl.DataStructure.urn_for(agency="AG", id="DSD_POP", version="1.0"))
    assert isinstance(pop, sl.DataStructure)
    world.registry.add(
        sl.DataStructure(
            id="DSD_NOMEAS",
            maintainer=AGENCY,
            version="1.0",
            name=istr("A DSD SDMX-ML 2.1 cannot write"),
            dimensions=pop.dimensions,
            time_dimension=pop.time_dimension,
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


def test_unwritable_content_names_the_artefact_and_the_rule(broken_world: World) -> None:
    with _client(broken_world) as client:
        resp = client.get(BROKEN, headers={"Accept": SDMX_ML_21})

    assert resp.status_code == 500
    body = resp.json()
    assert body["target"] == BROKEN
    paths = [issue["path"] for issue in body["issues"]]
    assert any(path.startswith("DataStructure:DSD_NOMEAS") for path in paths), paths
    assert any("Measure" in issue["message"] for issue in body["issues"]), body["issues"]


@pytest.mark.parametrize(
    "accept",
    [
        "application/vnd.sdmx.structure+json;version=2.0.0",
        "application/vnd.sdmx.structure+xml;version=3.0",
    ],
)
def test_a_format_that_can_carry_it_still_serves_it(broken_world: World, accept: str) -> None:
    """The refusal is the format's, not the artefact's: the same DSD is fine elsewhere."""
    with _client(broken_world) as client:
        resp = client.get(BROKEN, headers={"Accept": accept})

    assert resp.status_code == 200, resp.text


def test_the_500_is_not_cacheable(broken_world: World) -> None:
    with _client(broken_world) as client:
        resp = client.get(BROKEN, headers={"Accept": SDMX_ML_21})

    assert resp.status_code == 500
    assert "etag" not in resp.headers
    assert "cache-control" not in resp.headers


def test_a_listing_names_the_one_artefact_that_failed(broken_world: World) -> None:
    """In a listing the request target names no artefact, so the issue path must."""
    with _client(broken_world) as client:
        resp = client.get("/structure/datastructure/AG/all/latest", headers={"Accept": SDMX_ML_21})

    assert resp.status_code == 500
    assert any(issue["path"].startswith("DataStructure:DSD_NOMEAS") for issue in resp.json()["issues"])


def test_a_mounted_app_answers_the_same_way(broken_world: World) -> None:
    """A mounted FastAPI app answers its own unhandled errors, so it needs the handler too."""
    sub = FastAPI()
    for router in library_routers():
        sub.include_router(router)
    with _client(broken_world, mounts=[Mount("/v1", sub)]) as client:
        resp = client.get(f"/v1{BROKEN}", headers={"Accept": SDMX_ML_21})

    assert resp.status_code == 500
    assert any(issue["path"].startswith("DataStructure:DSD_NOMEAS") for issue in resp.json()["issues"])


def test_writable_content_is_unaffected(broken_world: World) -> None:
    """The discrimination check: the handler fires on refusal, not on every data structure."""
    with _client(broken_world) as client:
        resp = client.get("/structure/datastructure/AG/DSD_POP/1.0", headers={"Accept": SDMX_ML_21})

    assert resp.status_code == 200, resp.text
