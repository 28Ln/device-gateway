"""Streaming proxy + billing regressions for the /v1 device gateway.

Covers the streaming-proxy requirements:
  * SSE responses are forwarded chunk-by-chunk (not buffered whole).
  * Usage is extracted from the last SSE ``data:`` usage block.
  * Device auth / plan limits run before any byte is streamed.
  * Failed upstream responses are recorded but billed at cost 0.
The upstream HTTP client is mocked with a custom ``httpx`` transport, so no
real network or credentials are involved.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.service import create_device
import app.main as main


def _use_test_database(monkeypatch: pytest.MonkeyPatch, db, **overrides):
    overrides.setdefault("new_api_token", "test-upstream-token")
    monkeypatch.setattr(main, "settings", replace(main.settings, database_path=db, **overrides))


def _reset_clients(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main, "_upstream_client", None)
    monkeypatch.setattr(main, "_stream_client", None)


def _sse(*events: dict) -> bytes:
    return b"".join(
        f"data: {json.dumps(event)}\n\n".encode("utf-8") for event in events
    ) + b"data: [DONE]\n\n"


CHUNK_USAGE = {
    "id": "chatcmpl-1",
    "object": "chat.completion.chunk",
    "choices": [{"delta": {"content": "hi"}}],
}

FINAL_USAGE = {
    "id": "chatcmpl-1",
    "object": "chat.completion.chunk",
    "choices": [],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
}


def test_stream_forwards_each_chunk_and_extracts_usage(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    _use_test_database(monkeypatch, db)
    _reset_clients(monkeypatch)
    key, _ = create_device(db, "BTF:STREAM:OK", 1)

    calls: list[dict] = []
    body = _sse(CHUNK_USAGE, FINAL_USAGE)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"method": request.method, "path": request.url.path})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    monkeypatch.setattr(main, "stream_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    client = TestClient(main.app)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "btf-chat", "messages": [], "stream": True},
        headers={"authorization": f"Bearer {key}"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == body  # every SSE event forwarded verbatim
    assert "chat.completion.chunk" in response.text
    assert calls == [{"method": "POST", "path": "/v1/chat/completions"}]
    # The concurrency slot is released once the stream finishes.
    assert main.ACTIVE_REQUESTS.get("BTF:STREAM:OK") is None

    with connect(db) as conn:
        row = conn.execute(
            """
            select status_code, prompt_tokens, completion_tokens, total_tokens, estimated_cost
            from usage_events where device_id = ?
            """,
            ("BTF:STREAM:OK",),
        ).fetchone()
    assert row is not None
    assert row["status_code"] == 200
    assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (11, 7, 18)


def test_stream_without_usage_still_records_request(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    _use_test_database(monkeypatch, db)
    _reset_clients(monkeypatch)
    key, _ = create_device(db, "BTF:STREAM:NOUSAGE", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(CHUNK_USAGE),
        )

    monkeypatch.setattr(main, "stream_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    client = TestClient(main.app)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "btf-chat", "messages": [], "stream": True},
        headers={"authorization": f"Bearer {key}"},
    )

    assert response.status_code == 200
    assert "chat.completion.chunk" in response.text

    with connect(db) as conn:
        row = conn.execute(
            """
            select status_code, total_tokens, estimated_cost
            from usage_events where device_id = ?
            """,
            ("BTF:STREAM:NOUSAGE",),
        ).fetchone()
    assert row is not None  # request is never silently dropped
    assert row["status_code"] == 200
    assert row["total_tokens"] == 0
    assert row["estimated_cost"] == 0


def test_stream_authenticates_before_forwarding(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    _use_test_database(monkeypatch, db)
    _reset_clients(monkeypatch)

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(FINAL_USAGE),
        )

    monkeypatch.setattr(main, "stream_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    client = TestClient(main.app)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "btf-chat", "messages": [], "stream": True},
        headers={"authorization": "Bearer not-a-real-device"},
    )

    assert response.status_code == 403
    assert calls == []  # rejected before any upstream call / streaming
    with connect(db) as conn:
        count = conn.execute("select count(*) as n from usage_events").fetchone()["n"]
    assert count == 0


def test_stream_plan_limit_blocks_before_upstream(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    _use_test_database(monkeypatch, db)
    _reset_clients(monkeypatch)
    key, _ = create_device(db, "BTF:STREAM:LIMIT", 1)
    with connect(db) as conn:
        conn.execute("update plans set daily_request_limit = 1 where id = 'day_1'")
        conn.execute(
            """
            insert into usage_events (device_id, path, status_code, created_at)
            values (?, '/v1/chat/completions', 200, ?)
            """,
            ("BTF:STREAM:LIMIT", main.now_ts()),
        )

    calls: list[int] = []
    monkeypatch.setattr(
        main,
        "stream_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: calls.append(1) or httpx.Response(200))
        ),
    )

    client = TestClient(main.app)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "btf-chat", "messages": [], "stream": True},
        headers={"authorization": f"Bearer {key}"},
    )

    assert response.status_code == 403
    assert calls == []


def test_non_stream_query_uses_shared_client(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    _use_test_database(monkeypatch, db)
    _reset_clients(monkeypatch)
    key, _ = create_device(db, "BTF:PLAIN:SHARED", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps({"model": "deepseek-v4.1-flash", "usage": {"total_tokens": 3}}).encode(),
        )

    monkeypatch.setattr(main, "upstream_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    client = TestClient(main.app)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "btf-chat", "messages": []},
        headers={"authorization": f"Bearer {key}"},
    )

    assert response.status_code == 200
    assert response.json()["model"] == "btf-chat"  # alias mapping preserved
    with connect(db) as conn:
        row = conn.execute(
            "select total_tokens from usage_events where device_id = ?",
            ("BTF:PLAIN:SHARED",),
        ).fetchone()
    assert row["total_tokens"] == 3


def test_non_stream_error_is_recorded_without_cost(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    _use_test_database(monkeypatch, db, chat_prompt_cost_per_million=1000.0, chat_completion_cost_per_million=1000.0)
    _reset_clients(monkeypatch)
    key, _ = create_device(db, "BTF:PLAIN:ERR", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            headers={"content-type": "application/json"},
            content=json.dumps({"error": "upstream boom", "usage": {"total_tokens": 99}}).encode(),
        )

    monkeypatch.setattr(main, "upstream_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    client = TestClient(main.app)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "btf-chat", "messages": []},
        headers={"authorization": f"Bearer {key}"},
    )

    assert response.status_code == 500
    with connect(db) as conn:
        row = conn.execute(
            "select status_code, estimated_cost from usage_events where device_id = ?",
            ("BTF:PLAIN:ERR",),
        ).fetchone()
    assert row is not None  # audited
    assert row["status_code"] == 500
    assert row["estimated_cost"] == 0  # failures are not billed


def test_failed_stream_request_is_recorded_without_cost(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    _use_test_database(monkeypatch, db, chat_prompt_cost_per_million=1000.0, chat_completion_cost_per_million=1000.0)
    _reset_clients(monkeypatch)
    key, _ = create_device(db, "BTF:STREAM:ERR", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"content-type": "application/json"},
            content=json.dumps({"error": "rate limited"}).encode(),
        )

    monkeypatch.setattr(main, "stream_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    client = TestClient(main.app)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "btf-chat", "messages": [], "stream": True},
        headers={"authorization": f"Bearer {key}"},
    )

    assert response.status_code == 429
    with connect(db) as conn:
        row = conn.execute(
            "select status_code, estimated_cost from usage_events where device_id = ?",
            ("BTF:STREAM:ERR",),
        ).fetchone()
    assert row["status_code"] == 429
    assert row["estimated_cost"] == 0


def test_extract_stream_usage_returns_last_usage_block():
    body = _sse(
        CHUNK_USAGE,
        {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        {},
        FINAL_USAGE,
    )
    assert main.extract_stream_usage(body.decode()) == (11, 7, 18)


def test_extract_stream_usage_handles_missing_and_malformed():
    assert main.extract_stream_usage("") == (0, 0, 0)
    assert main.extract_stream_usage("data: [DONE]\n\n") == (0, 0, 0)
    assert main.extract_stream_usage("data: {not json}\n\n") == (0, 0, 0)
    assert main.extract_stream_usage('data: {"choices": []}\n\n') == (0, 0, 0)


def test_upstream_sse_without_stream_flag_is_streamed(monkeypatch, tmp_path):
    """An upstream that returns text/event-stream is relayed chunk-by-chunk
    even when the client did not send ``stream: true``."""
    db = tmp_path / "test.sqlite3"
    init_db(db)
    _use_test_database(monkeypatch, db)
    _reset_clients(monkeypatch)
    key, _ = create_device(db, "BTF:STREAM:IMPLICIT", 1)

    body = _sse(CHUNK_USAGE, FINAL_USAGE)
    monkeypatch.setattr(
        main,
        "upstream_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, content=body
                )
            )
        ),
    )

    client = TestClient(main.app)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "btf-chat", "messages": []},
        headers={"authorization": f"Bearer {key}"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == body
    with connect(db) as conn:
        row = conn.execute(
            "select total_tokens from usage_events where device_id = ?",
            ("BTF:STREAM:IMPLICIT",),
        ).fetchone()
    assert row["total_tokens"] == 18


def test_stream_flag_with_json_upstream_falls_back_to_buffering(monkeypatch, tmp_path):
    """stream=true but a JSON upstream (e.g. an error object) is buffered."""
    db = tmp_path / "test.sqlite3"
    init_db(db)
    _use_test_database(monkeypatch, db)
    _reset_clients(monkeypatch)
    key, _ = create_device(db, "BTF:STREAM:JSON", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps({"model": "deepseek-v4.1-flash", "usage": {"total_tokens": 5}}).encode(),
        )

    monkeypatch.setattr(main, "stream_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    client = TestClient(main.app)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "btf-chat", "messages": [], "stream": True},
        headers={"authorization": f"Bearer {key}"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["model"] == "btf-chat"
    with connect(db) as conn:
        row = conn.execute(
            "select total_tokens from usage_events where device_id = ?",
            ("BTF:STREAM:JSON",),
        ).fetchone()
    assert row["total_tokens"] == 5


def test_non_stream_image_generation_still_persists(monkeypatch, tmp_path):
    """The refactor must not drop image persistence from the proxy path."""
    db = tmp_path / "test.sqlite3"
    image_dir = tmp_path / "generated-images"
    init_db(db)
    _use_test_database(
        monkeypatch,
        db,
        generated_images_dir=image_dir,
        public_base_url="http://testserver",
    )
    _reset_clients(monkeypatch)
    key, _ = create_device(db, "BTF:IMG:SHARED", 30)

    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"test-image").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps({"model": "gpt-image-2", "data": [{"b64_json": png}]}).encode(),
        )

    monkeypatch.setattr(main, "upstream_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    client = TestClient(main.app)
    response = client.post(
        "/v1/images/generations",
        json={"model": "btf-image", "prompt": "red square"},
        headers={"authorization": f"Bearer {key}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "btf-image"  # alias mapping still applied
    assert body["data"][0]["url"].startswith("http://testserver/device-images/")
    with connect(db) as conn:
        row = conn.execute(
            "select device_id, content_type, byte_size from generated_images where device_id = ?",
            ("BTF:IMG:SHARED",),
        ).fetchone()
    assert row is not None
    assert row["byte_size"] > 0


def test_request_wants_stream_is_conservative():
    assert main.request_wants_stream(b'{"stream": true}', "application/json") is True
    assert main.request_wants_stream(b'{"stream": false}', "application/json") is False
    assert main.request_wants_stream(b'{"model": "x"}', "application/json") is False
    assert main.request_wants_stream(b"not-json", "application/json") is False
    assert main.request_wants_stream(b'{"stream": true}', "text/plain") is False


class _FakeChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


def test_stream_proxy_body_yields_upstream_chunks_in_order():
    chunks = [b'data: {"i": 1}\n\n', b'data: {"i": 2}\n\n', b"data: [DONE]\n\n"]
    upstream = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=_FakeChunkStream(chunks),
    )
    buffer: list[bytearray] = [bytearray()]

    async def collect() -> list[bytes]:
        return [chunk async for chunk in main.stream_proxy_body(upstream, buffer)]

    forwarded = asyncio.run(collect())

    # Each upstream chunk is forwarded as its own chunk (no whole-body buffering).
    assert forwarded == chunks
    assert len(forwarded) == 3
    assert bytes(buffer[0]).startswith(b'data: {"i": 1}')

