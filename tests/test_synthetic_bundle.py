"""The library routes, served from a published bundle the way a consumer opens one.

`conftest.py`'s world is a bundle assembled by hand. This one is built and
published by `sdmxlib.testing.synthetic_bundle` and opened with
`LocalRegistry.bundle`: the path a consumer's provider takes. It has what the
hand-built world lacks — a
hierarchical codelist, names in two languages, an observation attribute,
annotated codes — and it needs no real bundle, so it runs everywhere.

Expected answers come from sdmxlib's own query DSL over the same bundle, not
from constants, so a reseeded or reshaped bundle still checks the route
against an independent reading of the data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
import sdmxlib as sl
from fastapi.testclient import TestClient
from sdmxlib.testing import Dim, Shape, synthetic_bundle

from sdmxlib_rest.app import RestSettings, create_app, library_routers
from sdmxlib_rest.provider import OpenedCatalog
from sdmxlib_rest.runtime import CatalogRuntime

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sdmxlib.bundle import Manifest

pytestmark = pytest.mark.unit

_FLOW = sl.Dataflow.urn_for(agency="SYNTHETIC", id="DF_SYNTHETIC", version="1.0")
_FLOW_PATH = "SYNTHETIC/DF_SYNTHETIC/1.0"
_DIMENSIONS = ("DIM_1", "DIM_2")
#: `DIM_2`'s codelist: the hierarchical one.
_HIERARCHICAL = "/structure/codelist/SYNTHETIC/CL_DIM_2/1.0"
#: Small enough to build in well under a second; shaped so every feature the
#: hand-built world lacks is present: a two-level ragged hierarchy, two
#: languages, an attribute, and annotated codes.
_SHAPE = Shape(
    dimensions=(Dim(codes=6), Dim(codes=12, depth=2, fanout=(2, 4))),
    observations=80,
    periods=4,
    languages=("en", "fr"),
    attributes=1,
    annotated=0.5,
)


class _PublishedBundleProvider:
    """Opens a publication the way a consumer's provider does."""

    def __init__(self, publication: Path) -> None:
        self._publication = publication

    def open(self) -> OpenedCatalog:
        registry = sl.LocalRegistry.bundle(self._publication)
        manifest = registry.manifest
        assert manifest is not None
        return OpenedCatalog(
            registry=registry,
            meta_conn=registry.connection,
            build_id=manifest.bundle_id,
            built_at=manifest.created_at.isoformat(),
            on_close=registry.close,
        )


@pytest.fixture(scope="module")
def published(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Manifest]:
    root = tmp_path_factory.mktemp("synthetic") / "bundle"
    manifest = synthetic_bundle(root, _SHAPE, seed=7)
    return root / manifest.token, manifest


@pytest.fixture(scope="module")
def observations(published: tuple[Path, Manifest]) -> pl.DataFrame:
    """Every observation, read through sdmxlib's DSL: the oracle for /availability."""
    publication, _ = published
    with sl.LocalRegistry.bundle(publication) as registry:
        return registry.obs(_FLOW).frame()


@pytest.fixture(scope="module")
def runtime(published: tuple[Path, Manifest], observations: pl.DataFrame) -> Iterator[CatalogRuntime]:
    # `observations` first, so the oracle reads the bundle before the app does.
    del observations
    publication, _ = published
    runtime = CatalogRuntime(_PublishedBundleProvider(publication))
    yield runtime
    runtime.close()


@pytest.fixture(scope="module")
def client(runtime: CatalogRuntime) -> Iterator[TestClient]:
    app = create_app(runtime, RestSettings(title="synthetic", version="0.1.0"), routers=list(library_routers()))
    with TestClient(app) as test_client:
        yield test_client


def _available(payload: dict[str, Any]) -> dict[str, list[str]]:
    """Available values per component, out of the single CubeRegion."""
    region = payload["data"]["dataConstraints"][0]["cubeRegions"][0]
    return {kv["id"]: sorted(kv["values"]) for kv in region["keyValues"]}


def _distinct(frame: pl.DataFrame, column: str) -> list[str]:
    return sorted(str(v) for v in frame[column].unique().to_list())


def test_the_runtime_keys_on_the_published_manifest(
    client: TestClient, runtime: CatalogRuntime, published: tuple[Path, Manifest]
) -> None:
    _, manifest = published
    client.get(f"/structure/dataflow/{_FLOW_PATH}")

    assert runtime.token == manifest.token
    assert runtime.build_id == "synthetic-7"


@pytest.mark.parametrize("references", ["children", "descendants", "all"])
def test_detail_full_returns_the_referenced_codelists_in_full(client: TestClient, references: str) -> None:
    """GH-32: `detail` and `references` are orthogonal, so `full` means full references too."""
    resp = client.get(f"/structure/dataflow/{_FLOW_PATH}", params={"references": references})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [df["id"] for df in data["dataflows"]] == ["DF_SYNTHETIC"]
    assert len(data["dataStructures"]) == 1
    codes = {cl["id"]: len(cl.get("codes", [])) for cl in data["codelists"]}
    # A codelist per dimension and per attribute, each with every code.
    assert codes["CL_DIM_1"] == _SHAPE.dimensions[0].codes
    assert codes["CL_DIM_2"] == _SHAPE.dimensions[1].codes
    assert codes["CL_ATTR_1"] > 0
    assert len(codes) == len(_DIMENSIONS) + _SHAPE.attributes


@pytest.mark.parametrize("detail", ["referencestubs", "referencecompletestubs", "referencepartial", "allstubs"])
def test_a_stub_detail_returns_the_referenced_codelists_as_stubs(client: TestClient, detail: str) -> None:
    resp = client.get(f"/structure/dataflow/{_FLOW_PATH}", params={"references": "all", "detail": detail})

    assert resp.status_code == 200
    codelists = resp.json()["data"]["codelists"]
    assert sorted(cl["id"] for cl in codelists) == ["CL_ATTR_1", "CL_DIM_1", "CL_DIM_2"]
    assert not any(cl.get("codes") for cl in codelists)


def test_a_hierarchical_codelist_keeps_its_parents(client: TestClient) -> None:
    hierarchical = client.get(_HIERARCHICAL).json()["data"]["codelists"][0]

    assert len(hierarchical["codes"]) == _SHAPE.dimensions[1].codes
    parented = [c for c in hierarchical["codes"] if c.get("parent")]
    ids = {c["id"] for c in hierarchical["codes"]}
    assert parented, "a depth-2 codelist must have child codes"
    assert all(c["parent"] in ids for c in parented)


def test_names_follow_accept_language(client: TestClient) -> None:
    def names(lang: str) -> list[str]:
        codelist = client.get(_HIERARCHICAL, headers={"Accept-Language": lang}).json()["data"]["codelists"][0]
        return [codelist["name"], *(code["name"] for code in codelist["codes"])]

    english, french = names("en"), names("fr")

    assert all(english)
    assert all(french)
    assert all(en != fr for en, fr in zip(english, french, strict=True))


def test_unfiltered_availability_matches_the_observations(client: TestClient, observations: pl.DataFrame) -> None:
    resp = client.get(f"/availability/dataflow/{_FLOW_PATH}/all/all")

    assert resp.status_code == 200
    available = _available(resp.json())
    for column in (*_DIMENSIONS, "TIME_PERIOD"):
        assert available[column] == _distinct(observations, column), column


def test_the_constraint_counts_every_observation(client: TestClient) -> None:
    resp = client.get(f"/availability/dataflow/{_FLOW_PATH}/all/all")

    annotations = resp.json()["data"]["dataConstraints"][0]["annotations"]
    assert [a["title"] for a in annotations if a.get("id") == "obs_count"] == [str(_SHAPE.observations)]


def test_a_key_narrows_availability_as_the_data_does(client: TestClient, observations: pl.DataFrame) -> None:
    code = _distinct(observations, "DIM_1")[0]
    narrowed = observations.filter(pl.col("DIM_1").cast(pl.String) == code)

    resp = client.get(f"/availability/dataflow/{_FLOW_PATH}/{code}../all")

    assert resp.status_code == 200
    available = _available(resp.json())
    assert available["DIM_1"] == [code]
    assert available["DIM_2"] == _distinct(narrowed, "DIM_2")
    assert available["TIME_PERIOD"] == _distinct(narrowed, "TIME_PERIOD")


def test_availability_serialises_as_sdmx_ml(client: TestClient) -> None:
    resp = client.get(
        f"/availability/dataflow/{_FLOW_PATH}/all/all",
        headers={"Accept": "application/vnd.sdmx.structure+xml;version=3.0.0"},
    )

    assert resp.status_code == 200
    assert b"DataConstraint" in resp.content


def test_a_revalidation_with_the_published_etag_is_304(client: TestClient) -> None:
    first = client.get(f"/structure/dataflow/{_FLOW_PATH}")
    again = client.get(f"/structure/dataflow/{_FLOW_PATH}", headers={"If-None-Match": first.headers["etag"]})

    assert again.status_code == 304
