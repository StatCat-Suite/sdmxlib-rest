"""`OverlayAnnotator`: sdmxlib annotation overlays applied at serve time (GH-10).

What matters to a consumer is that an overlay changes what every wire format
serves, that swapping overlays on a running deployment changes the response
and its ETag, and that the key the runtime computes is the one sdmxlib defines
for a bundle with overlays, so a cache shared with other sdmxlib tooling agrees.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import msgspec
import pytest
import sdmxlib as sl
from fastapi.testclient import TestClient
from sdmxlib.bundle import OVERLAY_FORMAT, Overlay, OverlayAnnotation, OverlayEntry, OverlayError, effective_token
from sdmxlib.testing import Dim, Shape, synthetic_bundle

from sdmxlib_rest.app import RestSettings, create_app, library_routers
from sdmxlib_rest.message_annotator import NullMessageAnnotator
from sdmxlib_rest.overlays import OverlayAnnotator
from sdmxlib_rest.provider import OpenedCatalog
from sdmxlib_rest.runtime import CatalogRuntime
from tests.conftest import _WorldProvider

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sdmxlib_rest.message_annotator import MessageAnnotator
    from tests.conftest import World

pytestmark = pytest.mark.unit

_GEO = "/structure/codelist/AG/CL_GEO/1.0"
_XML = {"Accept": "application/vnd.sdmx.structure+xml;version=3.0"}


def _overlay(title: str, *, selector: str = "AG:CL_GEO", overlay_id: str = "display") -> Overlay:
    return Overlay(
        format_version=OVERLAY_FORMAT,
        id=overlay_id,
        entries=(
            OverlayEntry(selector=selector, op="set", type="LAYOUT_ROW", annotation=OverlayAnnotation(title=title)),
        ),
    )


def _titles(annotated: dict[str, Any], annotation_type: str) -> list[str]:
    return [a["title"] for a in annotated.get("annotations", []) if a.get("type") == annotation_type]


def _app(world: World, annotator: MessageAnnotator) -> TestClient:
    runtime = CatalogRuntime(_WorldProvider(world), message_annotator=annotator)
    return TestClient(create_app(runtime, RestSettings(title="t", version="0.1.0"), routers=list(library_routers())))


@pytest.fixture
def annotator() -> OverlayAnnotator:
    return OverlayAnnotator(overlays=(_overlay("REF_AREA"),))


@pytest.fixture
def client(world: World, annotator: OverlayAnnotator) -> Iterator[TestClient]:
    with _app(world, annotator) as test_client:
        yield test_client


def test_an_overlay_annotates_the_served_codelist(client: TestClient) -> None:
    codelist = client.get(_GEO).json()["data"]["codelists"][0]

    assert _titles(codelist, "LAYOUT_ROW") == ["REF_AREA"]


def test_the_overlay_reaches_sdmx_ml_too(client: TestClient) -> None:
    """It runs on the model before serialisation, so every format carries it."""
    body = client.get(_GEO, headers=_XML).text

    assert "LAYOUT_ROW" in body
    assert "REF_AREA" in body


def test_an_item_selector_annotates_one_code(world: World) -> None:
    code = "urn:sdmx:org.sdmx.infomodel.codelist.Code=AG:CL_GEO(1.0).CA"
    with _app(world, OverlayAnnotator(overlays=(_overlay("first", selector=code),))) as client:
        codes = {c["id"]: c for c in client.get(_GEO).json()["data"]["codelists"][0]["codes"]}

    assert _titles(codes["CA"], "LAYOUT_ROW") == ["first"]
    assert _titles(codes["US"], "LAYOUT_ROW") == []


def test_replacing_the_overlays_changes_the_response_and_its_etag(
    client: TestClient, annotator: OverlayAnnotator
) -> None:
    before = client.get(_GEO)
    annotator.replace([_overlay("TIME_PERIOD")])
    after = client.get(_GEO, headers={"If-None-Match": before.headers["etag"]})

    assert after.status_code == 200
    assert after.headers["etag"] != before.headers["etag"]
    assert _titles(after.json()["data"]["codelists"][0], "LAYOUT_ROW") == ["TIME_PERIOD"]


def test_no_overlays_serve_and_key_as_the_default_annotator(world: World) -> None:
    with _app(world, NullMessageAnnotator()) as client:
        default = client.get(_GEO)
    with _app(world, OverlayAnnotator()) as client:
        empty = client.get(_GEO)

    assert empty.headers["etag"] == default.headers["etag"]
    assert empty.content == default.content


def test_from_files_reads_overlay_documents_in_order(tmp_path: Path) -> None:
    paths = []
    for n, title in enumerate(["first", "second"]):
        path = tmp_path / f"{n}.json"
        path.write_bytes(msgspec.json.encode(_overlay(title, overlay_id=f"o{n}")))
        paths.append(path)

    annotator = OverlayAnnotator.from_files(paths)

    assert [o.id for o in annotator.overlays] == ["o0", "o1"]


def test_from_files_refuses_a_document_that_is_not_an_overlay(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"format_version": OVERLAY_FORMAT, "id": "x"}), encoding="utf-8")

    with pytest.raises(OverlayError):
        OverlayAnnotator.from_files([path])


class _Published:
    def __init__(self, root: Path) -> None:
        self._root = root

    def open(self) -> OpenedCatalog:
        registry = sl.LocalRegistry.bundle(self._root, require_manifest=True)
        return OpenedCatalog(registry=registry, meta_conn=registry.connection, on_close=registry.close)


def test_the_runtime_keys_on_sdmxlibs_effective_token(tmp_path: Path) -> None:
    """The same key sdmxlib defines for a bundle with overlays, to the character."""
    root = tmp_path / "bundle"
    manifest = synthetic_bundle(root, Shape(dimensions=(Dim(codes=4),), observations=8, periods=2))
    overlays = (_overlay("a", selector="*", overlay_id="one"), _overlay("b", selector="Codelist", overlay_id="two"))
    runtime = CatalogRuntime(_Published(root), message_annotator=OverlayAnnotator(overlays=overlays))
    runtime.ensure_open()
    try:
        assert runtime.token == manifest.token
        assert runtime.effective_token == effective_token(manifest, overlays)
    finally:
        runtime.close()
