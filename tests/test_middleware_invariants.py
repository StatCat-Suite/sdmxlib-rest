"""The invariants the middleware order protects, not just the order (#1).

`test_middleware_stack_is_in_the_invariant_order` pins the sequence, so a
reorder fails loudly. That is the floor: it asserts the order and none of the
behaviours the order exists for. A misordered chain returns correct responses
for the common cases and fails at the edges with no signal — an ETag computed
over the wrong bytes still looks like a perfectly good ETag.

So each test here drives a real request through the whole stack and asserts one
invariant an adjacency protects. Where a test can distinguish "the order is
right" from "the order happens not to matter", it does: the `?lang=` pair below
asserts the override and the equivalent header produce the *same* ETag, which is
only true if the rewrite lands before the ETag layer reads the header.

**One correction this file encodes.** `_install_middleware`'s docstring said the
ETag "hashes the *uncompressed* body", so moving it outward would make ETags vary
with compression. It does not hash the body at all — the signature is
`version|token|path|query|accept|resolved_lang`, computed before the app runs.
The invariant (an ETag that does not move when compression changes) holds; it
holds by construction rather than by ordering, and the docstring now says so.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, override

import pytest
from fastapi import APIRouter
from fastapi.responses import Response
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from sdmxlib_rest.app import MiddlewareSpec, RestSettings, create_app
from sdmxlib_rest.runtime import CatalogRuntime
from tests.test_library_create_app import ToyProvider

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.unit

#: Routes emit the three-part media-type version sdmxlib's writers produce, so
#: the advertise rewrite has something to rewrite.
WRITER_MEDIA = "application/vnd.sdmx.structure+json;version=2.0.0"
#: Two-part is what the wire must carry (SDMX-REST 2.2.2 section 6.1).
WIRE_MEDIA = "application/vnd.sdmx.structure+json;version=2.0"

#: Comfortably over any sane gzip_minimum_size, and compressible.
BIG = json.dumps({"data": {"codelists": [{"id": f"CL_{n}", "name": "a name"} for n in range(400)]}})
SMALL = json.dumps({"ok": True})


def _probe_router(calls: list[str]) -> APIRouter:
    """Routes that record being reached, so we can prove when they are not."""
    router = APIRouter()

    @router.get("/probe/big")
    def big() -> Response:
        calls.append("big")
        return Response(content=BIG, media_type=WRITER_MEDIA)

    @router.get("/probe/small")
    def small() -> Response:
        calls.append("small")
        return Response(content=SMALL, media_type=WRITER_MEDIA)

    return router


def _app(calls: list[str], sink: Any = None, **settings: Any) -> Any:
    base: dict[str, Any] = {"title": "toy", "version": "0.1.0", "gzip_minimum_size": 500}
    base.update(settings)
    return create_app(
        CatalogRuntime(ToyProvider()),
        RestSettings(**base),
        routers=[_probe_router(calls)],
        perf_sink=sink,
    )


@pytest.fixture
def probe() -> Iterator[tuple[TestClient, list[str], list[dict[str, Any]]]]:
    calls: list[str] = []
    records: list[dict[str, Any]] = []

    def sink(_kind: str, **fields: Any) -> None:
        records.append(fields)

    with TestClient(_app(calls, sink)) as client:
        yield client, calls, records


def _etag(client: TestClient, url: str, **kwargs: Any) -> str:
    resp = client.get(url, **kwargs)
    assert resp.status_code == 200, resp.text
    etag = resp.headers.get("etag")
    assert etag, f"no ETag on {url}"
    return etag


# ── ConditionalGet innermost ──────────────────────────────────────────────────


def test_the_etag_does_not_move_when_the_body_is_compressed(
    probe: tuple[TestClient, list[str], list[dict[str, Any]]],
) -> None:
    """A client that accepts gzip must get the same validator as one that does not.

    Otherwise a cache keyed on the ETag fragments by transfer encoding, and a
    304 for the identity client is a miss for the gzip client on the same bytes.
    """
    client, _, _ = probe
    plain = client.get("/probe/big", headers={"Accept-Encoding": "identity"})
    gzipped = client.get("/probe/big", headers={"Accept-Encoding": "gzip"})

    assert gzipped.headers.get("content-encoding") == "gzip"
    assert "content-encoding" not in plain.headers
    assert plain.headers["etag"] == gzipped.headers["etag"]


def test_the_etag_does_not_move_when_the_compression_level_changes() -> None:
    """Two deployments differing only in gzip level must agree on validators.

    They do because the ETag is built from request coordinates and the build id
    rather than from the body — which is *not* what `_install_middleware`'s
    docstring used to claim, and the reason this test exists.
    """
    # Levels 0 and 1, not 1 and 9: the backend is isa-l, whose range is 0-3, so
    # 1 through 9 all emit byte-identical output on this body and the pair would
    # prove nothing. 0 vs 1 is 1933 vs 1062 wire bytes.
    with TestClient(_app([], gzip_compress_level=0)) as loose, TestClient(_app([], gzip_compress_level=1)) as tight:
        cheap = loose.get("/probe/big", headers={"Accept-Encoding": "gzip"})
        dense = tight.get("/probe/big", headers={"Accept-Encoding": "gzip"})

    # The discrimination check: the levels really did produce different wire bytes.
    assert cheap.headers["content-length"] != dense.headers["content-length"]
    assert cheap.content == dense.content, "same entity either way"
    assert cheap.headers["etag"] == dense.headers["etag"]


def test_a_matching_if_none_match_short_circuits_to_304(
    probe: tuple[TestClient, list[str], list[dict[str, Any]]],
) -> None:
    client, calls, _ = probe
    etag = _etag(client, "/probe/big")
    calls.clear()

    resp = client.get("/probe/big", headers={"If-None-Match": etag})

    assert resp.status_code == 304
    assert not resp.content
    assert calls == [], "a 304 must not re-render the body"


# ── LangOverride outside ConditionalGet ───────────────────────────────────────


def test_the_etag_changes_with_a_lang_override(
    probe: tuple[TestClient, list[str], list[dict[str, Any]]],
) -> None:
    """``?lang=fr`` must not be served a cached French-less entity."""
    client, _, _ = probe
    assert _etag(client, "/probe/big?lang=fr") != _etag(client, "/probe/big?lang=en")


def test_a_lang_override_hashes_the_same_as_the_equivalent_header(
    probe: tuple[TestClient, list[str], list[dict[str, Any]]],
) -> None:
    """The adjacency itself: the rewrite lands *before* the ETag layer reads it.

    The discrimination check for the test above. If `LangOverride` ran inside
    `ConditionalGet`, `?lang=fr` would hash as a language-less request and
    collide with the default — so a differing ETag alone would not tell us the
    override reached the hash, only that the query string did.
    """
    client, _, _ = probe
    via_query = _etag(client, "/probe/small?lang=fr")
    via_header = _etag(client, "/probe/small?lang=fr", headers={"Accept-Language": "fr"})
    assert via_query == via_header


# ── GZip outside the version rewrite ─────────────────────────────────────────


def test_the_version_token_on_the_wire_is_the_two_part_form(
    probe: tuple[TestClient, list[str], list[dict[str, Any]]],
) -> None:
    """Routes emit ``;version=2.0.0``; strict clients compare Content-Type literally."""
    client, _, _ = probe
    resp = client.get("/probe/small")
    assert resp.headers["content-type"].startswith(WIRE_MEDIA)
    assert "2.0.0" not in resp.headers["content-type"]


def test_the_rewritten_type_is_still_compressed(
    probe: tuple[TestClient, list[str], list[dict[str, Any]]],
) -> None:
    """The compression decision reads Content-Type, and must read the rewritten one.

    A body carrying the rewritten two-part SDMX type still compresses, so the
    rewrite has not moved the type outside whatever set the compressor accepts.
    """
    client, _, _ = probe
    resp = client.get("/probe/big", headers={"Accept-Encoding": "gzip"})
    assert resp.headers.get("content-encoding") == "gzip"
    assert resp.headers["content-type"].startswith(WIRE_MEDIA)


def test_gzip_is_skipped_below_the_minimum_size(
    probe: tuple[TestClient, list[str], list[dict[str, Any]]],
) -> None:
    client, _, _ = probe
    resp = client.get("/probe/small", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in resp.headers
    assert resp.json() == {"ok": True}


# ── Perf tap outermost of the taps, HeadMethod inside it ─────────────────────


def test_the_perf_tap_records_the_wire_size_not_the_body_size(
    probe: tuple[TestClient, list[str], list[dict[str, Any]]],
) -> None:
    """``bytes`` is what crossed the wire, which is the number ops reason about."""
    client, _, records = probe
    records.clear()
    resp = client.get("/probe/big", headers={"Accept-Encoding": "gzip"})

    assert resp.headers.get("content-encoding") == "gzip"
    assert len(records) == 1
    recorded = records[0]["bytes"]
    assert recorded < len(resp.content), (
        f"perf recorded {recorded} bytes against an uncompressed body of {len(resp.content)}; "
        f"the tap is outside compression, so it must see the smaller wire size"
    )
    assert records[0]["content_encoding"] == "gzip"


def test_the_perf_tap_sees_the_client_method_for_a_head_request(
    probe: tuple[TestClient, list[str], list[dict[str, Any]]],
) -> None:
    """HeadMethod rewrites HEAD to GET; the taps sit outside it, so logs stay honest."""
    client, _, records = probe
    records.clear()
    client.head("/probe/big")

    assert len(records) == 1
    assert records[0]["method"] == "HEAD"


def test_a_head_request_gets_the_get_headers_and_no_body(
    probe: tuple[TestClient, list[str], list[dict[str, Any]]],
) -> None:
    client, _, _ = probe
    head = client.head("/probe/big")
    get = client.get("/probe/big", headers={"Accept-Encoding": "identity"})

    assert head.status_code == 200
    assert not head.content
    assert head.headers["etag"] == get.headers["etag"]
    assert head.headers["content-type"] == get.headers["content-type"]


# ── CORS outermost ───────────────────────────────────────────────────────────


def test_a_cors_preflight_on_an_unrouted_path_is_answered_not_404(
    probe: tuple[TestClient, list[str], list[dict[str, Any]]],
) -> None:
    """A preflight falling through to a 404 reads to the browser as a CORS failure."""
    client, calls, _ = probe
    calls.clear()

    resp = client.options(
        "/no/such/path",
        headers={"Origin": "https://example.test", "Access-Control-Request-Method": "GET"},
    )

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin")
    assert calls == [], "the preflight must be answered above the routes"


def test_a_cors_preflight_is_not_perf_logged(
    probe: tuple[TestClient, list[str], list[dict[str, Any]]],
) -> None:
    """Preflight chatter would swamp the perf log.

    Not an ordering test, and worth saying so: ``PerfLogMiddleware`` skips
    OPTIONS by construction, so this holds wherever CORS sits in the chain. It
    is here because the behaviour is worth pinning, not because it discriminates.
    """
    client, _, records = probe
    records.clear()
    client.options(
        "/probe/big",
        headers={"Origin": "https://example.test", "Access-Control-Request-Method": "GET"},
    )
    assert records == []


def test_a_consumer_middleware_cannot_reject_a_cors_preflight() -> None:
    """The actual content of "CORS outermost": it is above the consumer's own stack.

    `create_app`'s ``outer`` specs are where a consumer installs auth. If CORS
    sat inside them, a preflight — which carries no credentials by design —
    would be rejected before CORS ever saw it, and the browser would report a
    CORS failure for an app that is configured correctly.

    This is the test that fails when CORS stops being outermost; the preflight
    tests above do not, because CORS is still above the *routes* wherever else
    it lands.
    """
    rejected: list[str] = []

    class RejectEverything(BaseHTTPMiddleware):
        @override
        async def dispatch(self, request: Any, call_next: Any) -> Response:
            rejected.append(request.method)
            return Response(status_code=401, content=b"nope")

    app = create_app(
        CatalogRuntime(ToyProvider()),
        RestSettings(title="toy", version="0.1.0"),
        routers=[_probe_router([])],
        outer_middleware=[MiddlewareSpec(RejectEverything)],
    )
    with TestClient(app) as client:
        preflight = client.options(
            "/probe/big",
            headers={"Origin": "https://example.test", "Access-Control-Request-Method": "GET"},
        )
        guarded = client.get("/probe/big")

    assert preflight.status_code == 200, "the preflight must be answered above the consumer's stack"
    assert preflight.headers.get("access-control-allow-origin")
    assert "OPTIONS" not in rejected, "the consumer middleware must never see the preflight"
    # The discrimination check: that middleware really is installed and really does reject.
    assert guarded.status_code == 401
    assert "GET" in rejected
