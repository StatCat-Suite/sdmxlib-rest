"""Caches and the ETag key on the publication, not on the build id.

A producer may keep ``build_id`` across a metadata-only rebuild, so keying on
it served the old body after a republish, and a revalidating client got a 304
for it. sdmxlib's bundle manifest mints a fresh ``token`` at every publication;
the runtime keys on that whenever the registry has one, and on ``build_id``
otherwise. Every published bundle has a manifest, so the ``build_id`` case is a
plain store a provider opens itself.
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from email.utils import format_datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sdmxlib as sl
from fastapi.testclient import TestClient
from sdmxlib.bundle import Manifest, Producer, read_manifest, write_manifest

from sdmxlib_rest.app import RestSettings, create_app, library_routers
from sdmxlib_rest.provider import OpenedCatalog
from sdmxlib_rest.runtime import CatalogRuntime
from tests.conftest import BUILD_ID, BUILT_AT, build_world

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from fastapi.responses import Response

    from sdmxlib_rest.structure_cache import StructureQuery

pytestmark = pytest.mark.unit

_FLOW = "/structure/dataflow/AG/DF_POP/1.0"
_CHOSEN = "provider-chosen"


class _Provider:
    """Opens a publication with a fixed ``build_id``, as a producer would keep it."""

    def __init__(
        self, publication: Path, token: str = "", opener: Callable[[Path], sl.BundleRegistry] | None = None
    ) -> None:
        self._publication = publication
        self._token = token
        self._opener = opener or sl.LocalRegistry.bundle

    def open(self) -> OpenedCatalog:
        registry = self._opener(self._publication)
        return OpenedCatalog(
            registry=registry,
            meta_conn=registry.connection,
            build_id=BUILD_ID,
            built_at=BUILT_AT,
            token=self._token,
            on_close=registry.close,
        )


class _RecordingCache:
    """A structure cache that never hits and remembers every key it was asked for."""

    def __init__(self) -> None:
        self.queries: list[StructureQuery] = []

    def lookup(self, query: StructureQuery) -> Response | None:
        self.queries.append(query)
        return None

    def store(self, query: StructureQuery, response: Response) -> None:
        pass


@pytest.fixture
def bundle() -> Path:
    """The conftest world's publication, closed."""
    world = build_world()
    world.registry.close()
    return world.registry.uri.parent


def _republish(publication: Path) -> Path:
    """A new publication extending `publication`, with nothing changed: the same content, a new token."""
    with sl.LocalRegistry.build(publication.parent, previous=publication.name) as builder:
        new = builder.publication
    write_manifest(new, bundle_id="test", producer=Producer(name="tests", version="0"))
    return new


def _manifest(publication: Path) -> Manifest:
    return read_manifest(publication)


@contextmanager
def _client(runtime: CatalogRuntime) -> Generator[TestClient]:
    app = create_app(runtime, RestSettings(title="t", version="0.1.0"), routers=list(library_routers()))
    try:
        with TestClient(app) as client:
            yield client
    finally:
        runtime.close()


def _get(root: Path, headers: dict[str, str] | None = None) -> tuple[dict[str, str], int]:
    with _client(CatalogRuntime(_Provider(root))) as client:
        response = client.get(_FLOW, headers=headers or {})
    return dict(response.headers), response.status_code


def test_a_republish_with_the_same_build_id_changes_the_etag(bundle: Path) -> None:
    first, _ = _get(bundle)
    second, status = _get(_republish(bundle), {"If-None-Match": first["etag"]})

    assert second["etag"] != first["etag"]
    assert status == 200


def test_an_unchanged_publication_still_revalidates(bundle: Path) -> None:
    first, _ = _get(bundle)
    _, status = _get(bundle, {"If-None-Match": first["etag"]})

    assert status == 304


def test_last_modified_is_the_publication_time_not_built_at(bundle: Path) -> None:
    manifest = _manifest(bundle)
    headers, _ = _get(bundle)

    assert headers["last-modified"] == format_datetime(manifest.created_at.replace(microsecond=0), usegmt=True)


def test_the_structure_cache_keys_on_the_manifest_token(bundle: Path) -> None:
    manifest = _manifest(bundle)
    cache = _RecordingCache()
    runtime = CatalogRuntime(_Provider(bundle), structure_cache=cache)
    with _client(runtime) as client:
        client.get(_FLOW)

    assert [q.token for q in cache.queries] == [manifest.token]


def test_a_provider_token_wins_over_the_manifest(bundle: Path) -> None:
    runtime = CatalogRuntime(_Provider(bundle, token=_CHOSEN))
    runtime.ensure_open()

    assert runtime.token == _CHOSEN
    assert runtime.last_modified == BUILT_AT
    runtime.close()


def test_without_a_manifest_the_token_is_the_build_id() -> None:
    store = Path(tempfile.mkdtemp()) / "meta.duckdb"
    runtime = CatalogRuntime(_Provider(store, opener=lambda path: sl.LocalRegistry(path).open()))
    runtime.ensure_open()

    assert runtime.registry.manifest is None
    assert runtime.token == BUILD_ID
    assert runtime.last_modified == BUILT_AT
    runtime.close()
