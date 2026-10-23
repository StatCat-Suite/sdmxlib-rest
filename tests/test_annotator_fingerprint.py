"""An annotator's fingerprint feeds the structure cache and the ETag (GH-2).

A consumer can change annotations on a running deployment, an overlay edited
without rebuilding the bundle, by having its `MessageAnnotator` implement
`SupportsFingerprint`. The token alone does not move then, so without the
fingerprint a cached response kept the old annotations and a revalidating
client got a 304 for them.

An annotator without a fingerprint must key exactly as before: the same cache
keys, and byte-identical ETags.
"""

from __future__ import annotations

from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from sdmxlib_rest.app import RestSettings, create_app, library_routers
from sdmxlib_rest.availability_values import NullAvailabilityValues
from sdmxlib_rest.message_annotator import (
    CompositeMessageAnnotator,
    MessageAnnotator,
    NullMessageAnnotator,
    SupportsFingerprint,
    annotator_fingerprint,
)
from sdmxlib_rest.runtime import CatalogRuntime
from sdmxlib_rest.structure_cache import NullStructureCache
from tests.conftest import BUILD_ID, _WorldProvider

if TYPE_CHECKING:
    from collections.abc import Iterator

    import duckdb
    import sdmxlib as sl
    from fastapi.responses import Response
    from sdmxlib.model.message import StructureMessage

    from sdmxlib_rest.availability_values import AvailabilityKey, AvailabilityValues, ResolvedAvailability
    from sdmxlib_rest.structure_cache import StructureCache, StructureQuery
    from tests.conftest import World

pytestmark = pytest.mark.unit

_FLOW = "/structure/dataflow/AG/DF_POP/1.0"
_AVAILABILITY = "/availability/dataflow/AG/DF_POP/1.0/all/all"


class _Overlay:
    """An annotator whose output follows a version a consumer bumps at runtime."""

    def __init__(self, version: str = "v1") -> None:
        self.version = version
        self.applied: list[str] = []

    def annotate(
        self,
        message: StructureMessage,
        *,
        registry: sl.LocalRegistry,
        conn: duckdb.DuckDBPyConnection,
        token: str,
    ) -> StructureMessage:
        self.applied.append(token)
        return message

    def fingerprint(self) -> str:
        return self.version


class _Plain:
    """An annotator with no fingerprint: its output depends on the bundle alone."""

    def __init__(self, name: str = "", log: list[str] | None = None) -> None:
        self.name = name
        self.log = log if log is not None else []

    def annotate(
        self,
        message: StructureMessage,
        *,
        registry: sl.LocalRegistry,
        conn: duckdb.DuckDBPyConnection,
        token: str,
    ) -> StructureMessage:
        self.log.append(self.name)
        return message


class _Cache:
    """A structure cache that never hits and records every key."""

    def __init__(self) -> None:
        self.tokens: list[str] = []

    def lookup(self, query: StructureQuery) -> Response | None:
        self.tokens.append(query.token)
        return None

    def store(self, query: StructureQuery, response: Response) -> None:
        pass


class _Values:
    """An `AvailabilityValues` that never answers and records every key."""

    def __init__(self) -> None:
        self.tokens: list[str] = []

    def lookup(self, key: AvailabilityKey) -> ResolvedAvailability | None:
        self.tokens.append(key.token)
        return None

    def store(self, key: AvailabilityKey, resolved: ResolvedAvailability) -> None:
        pass


def _runtime(
    world: World,
    annotator: MessageAnnotator,
    *,
    structure_cache: StructureCache | None = None,
    availability_values: AvailabilityValues | None = None,
) -> CatalogRuntime:
    return CatalogRuntime(
        _WorldProvider(world),
        message_annotator=annotator,
        structure_cache=structure_cache or NullStructureCache(),
        availability_values=availability_values or NullAvailabilityValues(),
    )


def _client(runtime: CatalogRuntime) -> TestClient:
    app = create_app(runtime, RestSettings(title="t", version="0.1.0"), routers=list(library_routers()))
    return TestClient(app)


@pytest.fixture
def overlay() -> _Overlay:
    return _Overlay()


@pytest.fixture
def cache() -> _Cache:
    return _Cache()


@pytest.fixture
def client(world: World, overlay: _Overlay, cache: _Cache) -> Iterator[TestClient]:
    with _client(_runtime(world, overlay, structure_cache=cache)) as test_client:
        yield test_client


def test_a_fingerprint_change_changes_the_etag_and_ends_the_304(client: TestClient, overlay: _Overlay) -> None:
    before = client.get(_FLOW)
    overlay.version = "v2"
    after = client.get(_FLOW, headers={"If-None-Match": before.headers["etag"]})

    assert after.status_code == 200
    assert after.headers["etag"] != before.headers["etag"]


def test_an_unchanged_fingerprint_still_revalidates(client: TestClient) -> None:
    first = client.get(_FLOW)
    again = client.get(_FLOW, headers={"If-None-Match": first.headers["etag"]})

    assert again.status_code == 304


def test_a_fingerprint_change_misses_the_structure_cache(client: TestClient, overlay: _Overlay, cache: _Cache) -> None:
    client.get(_FLOW)
    client.get(_FLOW)
    overlay.version = "v2"
    client.get(_FLOW)

    first, second, third = cache.tokens
    assert first == second
    assert third != first


def test_the_annotator_is_handed_the_bare_token(client: TestClient, overlay: _Overlay) -> None:
    """It memoises per publication; folding its own fingerprint in would defeat that."""
    client.get(_FLOW)
    overlay.version = "v2"
    client.get(_FLOW)

    assert overlay.applied == [BUILD_ID, BUILD_ID]


def test_availability_keys_ignore_the_fingerprint(world: World, overlay: _Overlay) -> None:
    """Annotations cannot change which values are available, so no entry is invalidated."""
    values = _Values()
    with _client(_runtime(world, overlay, availability_values=values)) as client:
        client.get(_AVAILABILITY)
        overlay.version = "v2"
        client.get(_AVAILABILITY)

    assert values.tokens == [BUILD_ID, BUILD_ID]


def test_last_modified_moves_with_the_fingerprint(client: TestClient, overlay: _Overlay) -> None:
    """``If-Modified-Since`` must not 304 a body whose annotations changed.

    The change happens within the same second as the first request, the case
    HTTP's whole-second dates would otherwise hide.
    """
    before = client.get(_FLOW)
    overlay.version = "v2"
    after = client.get(_FLOW, headers={"If-Modified-Since": before.headers["last-modified"]})

    assert after.status_code == 200
    assert parsedate_to_datetime(after.headers["last-modified"]) > parsedate_to_datetime(
        before.headers["last-modified"]
    )


def test_no_fingerprint_keys_exactly_as_before(world: World) -> None:
    """A fingerprint-free annotator gets the ETag and Last-Modified of the default one."""
    with _client(_runtime(world, NullMessageAnnotator())) as client:
        default = client.get(_FLOW)
    with _client(_runtime(world, _Plain())) as client:
        plain = client.get(_FLOW)

    assert plain.headers["etag"] == default.headers["etag"]
    assert plain.headers["last-modified"] == default.headers["last-modified"]
    assert plain.headers["last-modified"] == "Thu, 01 Jan 2026 00:00:00 GMT"


def test_the_effective_token_is_the_token_until_there_is_a_fingerprint(world: World, overlay: _Overlay) -> None:
    plain = _runtime(world, _Plain())
    plain.ensure_open()
    fingerprinted = _runtime(world, overlay)
    fingerprinted.ensure_open()

    assert plain.effective_token == plain.token == BUILD_ID
    assert fingerprinted.effective_token not in {"", BUILD_ID}


def test_a_composite_joins_its_members_fingerprints(overlay: _Overlay) -> None:
    plain = CompositeMessageAnnotator((_Plain("a"), _Plain("b")))
    mixed = CompositeMessageAnnotator((_Plain("a"), overlay))

    assert isinstance(plain, SupportsFingerprint)
    assert annotator_fingerprint(plain) == ""
    assert annotator_fingerprint(mixed) == "0:\n1:v1"
    overlay.version = "v2"
    assert annotator_fingerprint(mixed) == "0:\n1:v2"


def test_a_composite_applies_its_members_in_order(world: World) -> None:
    log: list[str] = []
    composite = CompositeMessageAnnotator((_Plain("a", log), _Plain("b", log)))
    with _client(_runtime(world, composite)) as client:
        client.get(_FLOW)

    assert log == ["a", "b"]
