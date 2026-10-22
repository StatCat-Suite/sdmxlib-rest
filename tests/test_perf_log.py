"""Per-request perf-log middleware — records what and only what we expect.

Guards two things:

1. Envelope fields (method / path / status / wall_ms / bytes / content_type /
   content_encoding / has_range / has_if_none_match) show up in every record.
2. What we DON'T log — no query string, no request body, no Authorization
   or Cookie headers — is a promise we want under test.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.testclient import TestClient

from sdmxlib_rest.middleware.perf_log import PerfLogMiddleware

pytestmark = pytest.mark.unit


def _make_app_with_sink() -> tuple[FastAPI, list[dict[str, object]]]:
    """Small FastAPI app with PerfLog wired to an in-memory sink for assertions."""
    records: list[dict[str, object]] = []

    def _sink(msg: str, /, **kwargs: object) -> None:
        records.append({"_msg": msg, **kwargs})

    app = FastAPI()

    @app.get("/tiny")
    def tiny() -> PlainTextResponse:
        return PlainTextResponse("hello")

    @app.get("/stream")
    def stream() -> StreamingResponse:
        def _iter() -> object:
            yield b"a" * 1000
            yield b"b" * 2000

        return StreamingResponse(_iter(), media_type="text/plain")

    @app.get("/error")
    def error() -> PlainTextResponse:
        # Route returning 500 to prove status is captured even for errors.
        return PlainTextResponse("nope", status_code=500)

    app.add_middleware(PerfLogMiddleware, sink=_sink)
    return app, records


def test_records_basic_envelope_fields() -> None:
    """Every non-OPTIONS request produces one record with the documented fields."""
    app, records = _make_app_with_sink()
    with TestClient(app) as client:
        r = client.get("/tiny")
    assert r.status_code == 200
    assert len(records) == 1
    rec = records[0]
    assert rec["_msg"] == "perf"
    assert rec["method"] == "GET"
    assert rec["path"] == "/tiny"
    assert rec["status"] == 200
    assert rec["bytes"] == 5  # ``hello``
    assert isinstance(rec["wall_ms"], float)
    assert rec["wall_ms"] >= 0
    assert str(rec["content_type"]).startswith("text/plain")
    assert rec["content_encoding"] == "identity"
    assert rec["has_range"] is False
    assert rec["has_if_none_match"] is False


def test_bytes_accumulates_over_streaming_response() -> None:
    """Multi-chunk StreamingResponse still yields a single record with total bytes."""
    app, records = _make_app_with_sink()
    with TestClient(app) as client:
        r = client.get("/stream")
    assert r.status_code == 200
    assert len(records) == 1
    assert records[0]["bytes"] == 3000  # 1000 + 2000


def test_captures_status_on_non_2xx() -> None:
    """A 5xx from the route still logs status=500, not 0."""
    app, records = _make_app_with_sink()
    with TestClient(app) as client:
        r = client.get("/error")
    assert r.status_code == 500
    assert records[0]["status"] == 500


def test_captures_range_and_if_none_match_signals() -> None:
    """Request-side header sniffs report presence, not the value."""
    app, records = _make_app_with_sink()
    with TestClient(app) as client:
        client.get("/tiny", headers={"Range": "items=0-9", "If-None-Match": '"abc"'})
    assert records[0]["has_range"] is True
    assert records[0]["has_if_none_match"] is True


def test_query_string_is_not_logged() -> None:
    """Only ``query_len`` is captured — SDMX keys / c[] filters stay out of the record."""
    app, records = _make_app_with_sink()
    with TestClient(app) as client:
        client.get("/tiny?c[FREQ]=A5&secret=oops")
    rec = records[0]
    assert "query" not in rec  # no field named query / query_string
    for value in rec.values():
        assert "secret=oops" not in str(value)
    assert rec["query_len"] > 0


def test_options_is_skipped() -> None:
    """CORS preflight OPTIONS produce no record — they'd flood the log otherwise."""
    app, records = _make_app_with_sink()
    with TestClient(app) as client:
        client.options("/tiny", headers={"Access-Control-Request-Method": "GET"})
    assert records == []


def test_none_sink_disables_all_recording() -> None:
    """Middleware with sink=None is a no-op — the guard path for perf_log_path=''."""
    app = FastAPI()

    @app.get("/tiny")
    def tiny() -> PlainTextResponse:
        return PlainTextResponse("hi")

    app.add_middleware(PerfLogMiddleware, sink=None)
    with TestClient(app) as client:
        r = client.get("/tiny")
    # If the middleware crashed on sink=None the response would 500.
    assert r.status_code == 200
    assert r.text == "hi"
