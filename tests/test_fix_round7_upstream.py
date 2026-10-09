"""Round-7 family closure: upstream / client-controlled integer & structural
hardening for the usage and session parsing paths.

Independent verification found a residual "unhandled exception -> 500" family
where non-finite / oversized numbers arrive from an upstream JSON body:

  G2. Upstream ``usage`` numbers such as ``1e999`` (-> ``int(inf)``) or
      ``2**63`` (finite but outside signed 64-bit) raised ``OverflowError`` in
      ``_safe_int`` or later when bound to SQLite by ``record_usage``.
  G3. ``/api/user/self`` returning a huge / non-finite ``id`` raised
      ``OverflowError`` in ``resolve_dashboard_user`` (500 instead of 401) and
      in ``augment_customer_self_response`` (500 while proxying the response).

The upstream HTTP client is mocked with ``httpx.MockTransport``; no network or
credentials are involved.
"""
from __future__ import annotations

import asyncio
import json
import math
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import app.main as main
from app.db import connect, init_db
from app.service import create_device, record_usage

INT64_MAX = 2**63 - 1


# --- helpers --------------------------------------------------------------


def _use_test_database(monkeypatch: pytest.MonkeyPatch, db, **overrides):
    overrides.setdefault("new_api_token", "test-upstream-token")
    monkeypatch.setattr(main, "settings", replace(main.settings, database_path=db, **overrides))
    main.DEVICE_AUTH_CACHE.clear()


def _reset_clients(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main, "_upstream_client", None)
    monkeypatch.setattr(main, "_stream_client", None)


def _sse(*events: dict) -> bytes:
    return b"".join(
        f"data: {json.dumps(event)}\n\n".encode("utf-8") for event in events
    ) + b"data: [DONE]\n\n"


def _last_usage(db):
    with connect(db) as conn:
        return conn.execute(
            """
            select status_code, prompt_tokens, completion_tokens, total_tokens,
                   estimated_cost
            from usage_events order by id desc limit 1
            """
        ).fetchone()


def _make_request(method: str, headers: dict[str, str] | None = None) -> Request:
    raw = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": method,
        "headers": raw,
        "path": "/",
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


class _FakeUpstreamResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stands in for the httpx client ``resolve_dashboard_user`` uses."""

    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        return _FakeUpstreamResponse(200, self._payload)

    async def post(self, url, headers=None):
        return _FakeUpstreamResponse(200, {})


def _patch_dashboard_upstream(monkeypatch, payload):
    client = _FakeAsyncClient(payload)
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: client)


# --- G2a: _safe_int hardening ---------------------------------------------


@pytest.mark.parametrize(
    "value",
    [float("inf"), float("-inf"), 2**63, -(2**63) - 1, 10**30, "1e999", "2e308"],
)
def test_safe_int_neutralises_non_finite_and_oversized(value):
    assert main._safe_int(value) == 0


@pytest.mark.parametrize("value,expected", [(0, 0), (5, 5), (-7, -7), (INT64_MAX, INT64_MAX), ("42", 42)])
def test_safe_int_keeps_valid_values(value, expected):
    assert main._safe_int(value) == expected


def test_safe_int_never_raises_on_garbage():
    for value in (None, "abc", [], {}, b"\x00", object()):
        assert main._safe_int(value) == 0


def test_normalize_usage_clamps_non_finite_and_oversized():
    payload = {
        "usage": {
            "prompt_tokens": 1e999,          # non-finite -> int(inf) OverflowError
            "completion_tokens": 2**63,       # finite but out of int64
            "total_tokens": 10**30,
        }
    }
    assert main.normalize_usage(payload) == (0, 0, 0)


# --- G2b: record_usage clamps before SQLite -------------------------------


def test_record_usage_clamps_oversized_and_non_finite(tmp_path):
    db = tmp_path / "r7-usage.sqlite3"
    init_db(db)
    create_device(db, "BTF:R7:USAGE", 30)
    # Must not raise OverflowError when binding to SQLite.
    event_id = record_usage(
        db,
        "BTF:R7:USAGE",
        "/v1/chat/completions",
        200,
        prompt_tokens=2**63,
        completion_tokens=10**30,
        total_tokens=2**63 + 1,
        estimated_cost=float("inf"),
    )
    assert event_id > 0
    row = _last_usage(db)
    assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (0, 0, 0)
    assert math.isfinite(row["estimated_cost"]) and row["estimated_cost"] == 0.0


def test_record_usage_keeps_in_range_values(tmp_path):
    db = tmp_path / "r7-usage-ok.sqlite3"
    init_db(db)
    create_device(db, "BTF:R7:USAGE:OK", 30)
    record_usage(db, "BTF:R7:USAGE:OK", "/v1/chat/completions", 200, 11, 7, 18)
    row = _last_usage(db)
    assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (11, 7, 18)


# --- G2c: non-streaming upstream usage -> no 500 --------------------------


def test_non_streaming_oversized_upstream_usage_does_not_500(monkeypatch, tmp_path):
    db = tmp_path / "r7-nonstream.sqlite3"
    init_db(db)
    _use_test_database(monkeypatch, db)
    _reset_clients(monkeypatch)
    key, _ = create_device(db, "BTF:R7:NS", 30)

    body = json.dumps(
        {
            "id": "chatcmpl-1",
            "choices": [{"message": {"content": "hi"}}],
            "usage": {
                "prompt_tokens": 2**63,
                "completion_tokens": 1e999,
                "total_tokens": 10**30,
            },
        }
    ).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, content=body)

    monkeypatch.setattr(
        main, "upstream_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "btf-chat", "messages": []},
        headers={"authorization": f"Bearer {key}"},
    )
    assert response.status_code == 200
    row = _last_usage(db)
    assert row is not None
    assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (0, 0, 0)
    assert row["estimated_cost"] == 0.0


# --- G2d: streamed upstream usage -> no 500 -------------------------------


def test_streaming_oversized_upstream_usage_does_not_500(monkeypatch, tmp_path):
    db = tmp_path / "r7-stream.sqlite3"
    init_db(db)
    _use_test_database(monkeypatch, db)
    _reset_clients(monkeypatch)
    key, _ = create_device(db, "BTF:R7:STREAM", 30)

    chunk = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "choices": []}
    final = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "choices": [],
        "usage": {"prompt_tokens": 1e999, "completion_tokens": 2**63, "total_tokens": 10**30},
    }
    body = _sse(chunk, final)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    monkeypatch.setattr(
        main, "stream_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "btf-chat", "messages": [], "stream": True},
        headers={"authorization": f"Bearer {key}"},
    )
    assert response.status_code == 200
    row = _last_usage(db)
    assert row is not None
    assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (0, 0, 0)


# --- G3a: resolve_dashboard_user oversized id -> 401 ----------------------


@pytest.mark.parametrize("bad_id", [float("inf"), 2**63, 10**30])
def test_resolve_dashboard_user_oversized_id_is_401(monkeypatch, bad_id):
    _patch_dashboard_upstream(
        monkeypatch,
        {"success": True, "data": {"id": bad_id, "role": 1, "username": "u"}},
    )
    request = _make_request("GET", {"Cookie": "session=abc"})
    with pytest.raises(main.HTTPException) as excinfo:
        asyncio.run(main.resolve_dashboard_user(request, None))
    assert excinfo.value.status_code == 401


def test_resolve_dashboard_user_valid_id_still_works(monkeypatch):
    _patch_dashboard_upstream(
        monkeypatch,
        {"success": True, "data": {"id": 7, "role": 1, "username": "u"}},
    )
    request = _make_request("GET", {"Cookie": "session=abc"})
    user = asyncio.run(main.resolve_dashboard_user(request, None))
    assert user.id == 7


# --- G3b: augment_customer_self_response oversized id -> no 500 -----------


@pytest.mark.parametrize("bad_id", [10**30, 2**63, float("inf")])
def test_augment_customer_self_response_oversized_id_does_not_500(bad_id):
    content = json.dumps(
        {"success": True, "data": {"id": bad_id, "role": 1, "username": "u"}}
    ).encode("utf-8")
    # Must not raise OverflowError; returns the body untouched when the id is
    # unusable (<= 0 after clamping).
    result = main.augment_customer_self_response(content, "application/json")
    assert result == content


def test_augment_customer_self_response_valid_id_untouched_when_not_configured(monkeypatch, tmp_path):
    db = tmp_path / "r7-aug.sqlite3"
    init_db(db)
    _use_test_database(monkeypatch, db)
    content = json.dumps(
        {"success": True, "data": {"id": 3, "role": 1, "username": "u"}}
    ).encode("utf-8")
    # A non-admin id takes the augmentation path; it must still return valid
    # JSON (or the original bytes) without raising.
    result = main.augment_customer_self_response(content, "application/json")
    assert json.loads(result)["success"] is True
