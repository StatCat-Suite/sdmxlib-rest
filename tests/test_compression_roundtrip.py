"""Regression cover for the isa-l gzip responder.

``_IsalGZipResponder`` used to skip ``GZipResponder.__init__`` and install its
own ``IGzipFile``. That worked against the Starlette of the day, whose
responder wrote body chunks into a ``GzipFile``. Starlette 1.4 rewrote it to
hold a ``zlib.compressobj`` and added ``thread_minimum_size`` — neither of
which the subclass set — so from that release on, every gzip-eligible response
over ``minimum_size`` raised ``AttributeError`` *after* the response had
started, and the client saw a truncated body.

Nothing caught it: the pre-existing suite only ever asserted on bodies under
the compression threshold, where the responder short-circuits. The threshold is
the whole point of these tests, so they assert on both sides of it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pytest
import sdmxlib as sl
from fastapi import APIRouter
from fastapi.testclient import TestClient

from sdmxlib_rest.app import RestSettings, create_app
from sdmxlib_rest.provider import OpenedCatalog
from sdmxlib_rest.runtime import CatalogRuntime

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.unit

# Comfortably over Starlette's 500-byte default, and over any minimum_size the
# app factory is likely to pick.
BIG = "a" * 5000
SMALL = "a" * 10


class _BareProvider:
    """An empty catalog — these tests are about the middleware, not the data."""

    def open(self) -> OpenedCatalog:
        return OpenedCatalog(
            registry=sl.LocalRegistry(Path(tempfile.mkdtemp()) / "meta.duckdb").open(),
            meta_conn=duckdb.connect(),
            build_id="b",
            built_at="2026-01-01T00:00:00Z",
        )


@pytest.fixture
def gzip_client() -> Iterator[TestClient]:
    """The library app plus two routes straddling the compression threshold."""
    router = APIRouter()

    @router.get("/big")
    async def _big() -> dict[str, str]:
        return {"x": BIG}

    @router.get("/small")
    async def _small() -> dict[str, str]:
        return {"x": SMALL}

    app = create_app(
        CatalogRuntime(_BareProvider()),
        RestSettings(title="gzip-test", version="0.1.0"),
        routers=[router],
    )
    with TestClient(app) as client:
        yield client


def test_a_large_response_is_compressed_and_decodes(gzip_client: TestClient) -> None:
    """The failure this guards was a crash mid-send, so reaching 200 is half the assertion."""
    resp = gzip_client.get("/big", headers={"Accept-Encoding": "gzip"})

    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.json() == {"x": BIG}


def test_compression_actually_shrinks_the_body(gzip_client: TestClient) -> None:
    """A responder that emitted the body uncompressed would still round-trip."""
    compressed = gzip_client.get("/big", headers={"Accept-Encoding": "gzip"})
    identity = gzip_client.get("/big", headers={"Accept-Encoding": "identity"})

    assert int(compressed.headers["content-length"]) < int(identity.headers["content-length"])
    assert compressed.json() == identity.json()


def test_a_small_response_is_left_alone(gzip_client: TestClient) -> None:
    """Below the threshold the responder must not add an encoding header."""
    resp = gzip_client.get("/small", headers={"Accept-Encoding": "gzip"})

    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers
    assert resp.json() == {"x": SMALL}


def test_a_client_that_declines_gzip_gets_plain_bytes(gzip_client: TestClient) -> None:
    """The identity path is the one every non-negotiating client takes."""
    resp = gzip_client.get("/big", headers={"Accept-Encoding": "identity"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") != "gzip"
    assert resp.json() == {"x": BIG}


def test_a_real_structure_response_survives_compression(client: TestClient) -> None:
    """The end-to-end shape: a real route body over the configured threshold.

    ``references=children`` is used to push the message past
    ``gzip_minimum_size`` (1024). A smaller structure response stays under it
    and is passed through uncompressed, which would make this a test of
    nothing — hence the explicit assertion that the body really was encoded.
    """
    resp = client.get(
        "/structure/dataflow/AG/DF_POP/1.0",
        params={"references": "children"},
        headers={"Accept-Encoding": "gzip"},
    )

    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.json()["data"]["dataflows"][0]["id"] == "DF_POP"
