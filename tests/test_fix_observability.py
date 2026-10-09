"""Observability, request-id propagation and payment-callback throttling.

Covers the P1-7 (remaining) / P2-15 / P2-18 work:
  * ``X-Request-ID`` middleware generates, echoes, and forwards a request id.
  * Structured access logging is emitted (asserted with ``caplog``).
  * The proxy path logs upstream status/latency (non-streaming and streaming).
  * Payment callback / notification endpoints are throttled and size-capped.
The upstream HTTP client is mocked with ``httpx.MockTransport`` so no network
or real credentials are involved.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.payments import MockPaymentProvider
from app.service import create_device, create_order, save_payment_intent
import app.main as main


@pytest.fixture(autouse=True)
def _enable_log_propagation():
    """Let ``caplog`` see the gateway logger.

    The app attaches its own handler to ``btf_device_gateway``; propagation
    stays on, but we pin the level to DEBUG and restore it afterwards so caplog
    captures every record regardless of the ambient ``BTF_LOG_LEVEL``.
    """
    old_level = main.logger.level
    old_propagate = main.logger.propagate
    main.logger.setLevel(logging.DEBUG)
    main.logger.propagate = True
    yield
    main.logger.setLevel(old_level)
    main.logger.propagate = old_propagate


def _gateway_records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == "btf_device_gateway"]


def _use_test_database(monkeypatch, db, **overrides):
    overrides.setdefault("new_api_token", "test-upstream-token")
    monkeypatch.setattr(main, "settings", replace(main.settings, database_path=db, **overrides))


# --- 1. request id middleware ---------------------------------------------


def test_request_id_header_generated_when_absent():
    client = TestClient(main.app)
    response = client.get("/health")
    assert response.status_code == 200
    request_id = response.headers.get("X-Request-ID")
    assert request_id and request_id.startswith("req-")


def test_request_id_header_echoes_client_value_and_is_forwarded(monkeypatch, tmp_path):
    db = tmp_path / "reqid.sqlite3"
    init_db(db)
    _use_test_database(monkeypatch, db)
    monkeypatch.setattr(main, "_upstream_client", None)
    key, _ = create_device(db, "BTF-REQID:1", 1)

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-request-id", ""))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        main, "upstream_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    client = TestClient(main.app)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "btf-chat", "messages": []},
        headers={"authorization": f"Bearer {key}", "X-Request-ID": "trace-abc123"},
    )
    # Echoed back to the client ...
    assert response.headers["X-Request-ID"] == "trace-abc123"
    # ... and propagated to the upstream new-api call for correlation.
    assert seen == ["trace-abc123"]


def test_request_id_rejects_unsafe_client_value():
    client = TestClient(main.app)
    response = client.get("/health", headers={"X-Request-ID": "bad id\r\nX-Evil: 1"})
    generated = response.headers.get("X-Request-ID", "")
    # The unsafe value is dropped and replaced with a server-generated one.
    assert generated.startswith("req-")
    assert "evil" not in generated.lower()


def test_access_log_records_method_path_status(caplog, monkeypatch, tmp_path):
    db = tmp_path / "log.sqlite3"
    init_db(db)
    _use_test_database(monkeypatch, db)
    monkeypatch.setattr(main, "_upstream_client", None)
    key, _ = create_device(db, "BTF-LOG:1", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        main, "upstream_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    with caplog.at_level(logging.DEBUG, logger="btf_device_gateway"):
        client = TestClient(main.app)
        client.post(
            "/v1/chat/completions",
            json={"model": "btf-chat", "messages": []},
            headers={"authorization": f"Bearer {key}", "X-Request-ID": "logged-req"},
        )

    joined = "\n".join(record.getMessage() for record in _gateway_records(caplog))
    assert "logged-req" in joined
    assert "status=200" in joined
    assert "device=BTF-LOG:1" in joined
    assert "latency_ms" in joined


def test_upstream_error_is_logged_as_warning(caplog, monkeypatch, tmp_path):
    db = tmp_path / "warn.sqlite3"
    init_db(db)
    _use_test_database(monkeypatch, db)
    monkeypatch.setattr(main, "_upstream_client", None)
    key, _ = create_device(db, "BTF-WARN:1", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    monkeypatch.setattr(
        main, "upstream_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    with caplog.at_level(logging.DEBUG, logger="btf_device_gateway"):
        client = TestClient(main.app)
        response = client.post(
            "/v1/chat/completions",
            json={"model": "btf-chat", "messages": []},
            headers={"authorization": f"Bearer {key}"},
        )

    assert response.status_code == 500
    upstream_warnings = [
        record.getMessage()
        for record in _gateway_records(caplog)
        if record.levelno >= logging.WARNING and record.getMessage().startswith("upstream ")
    ]
    assert any("status=500" in message for message in upstream_warnings), caplog.records


def test_streaming_response_logs_upstream_and_has_request_id(caplog, monkeypatch, tmp_path):
    db = tmp_path / "stream.sqlite3"
    init_db(db)
    _use_test_database(monkeypatch, db)
    monkeypatch.setattr(main, "_stream_client", None)
    key, _ = create_device(db, "BTF-STREAMLOG:1", 1)

    sse = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    monkeypatch.setattr(
        main, "stream_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    with caplog.at_level(logging.DEBUG, logger="btf_device_gateway"):
        client = TestClient(main.app)
        response = client.post(
            "/v1/chat/completions",
            json={"model": "btf-chat", "messages": [], "stream": True},
            headers={"authorization": f"Bearer {key}", "X-Request-ID": "stream-req"},
        )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "stream-req"
    joined = "\n".join(record.getMessage() for record in _gateway_records(caplog))
    assert "streamed=True" in joined
    assert "stream-req" in joined


# --- 2. payment callback throttling + body cap ----------------------------


def _mock_setup(monkeypatch, tmp_path, device_id, **overrides):
    db = tmp_path / "pay.sqlite3"
    init_db(db)
    key, device = create_device(db, device_id, 1)
    order = create_order(db, key, "day_1")
    provider = MockPaymentProvider("test-secret")
    intent = asyncio.run(provider.create_payment(order, "https://example.test/notify"))
    transaction = save_payment_intent(db, order["id"], intent)
    overrides.setdefault("demo_payment_enabled", True)
    overrides.setdefault("mock_payment_secret", "test-secret")
    _use_test_database(monkeypatch, db, **overrides)
    return db, key, order, provider, transaction


def test_mock_notify_rate_limited(monkeypatch, tmp_path):
    db, key, order, provider, transaction = _mock_setup(
        monkeypatch, tmp_path, "BTF-OBS-RL", payment_notify_rate_limit=3
    )
    # Fresh limiter so other tests' bucket state does not leak in.
    monkeypatch.setattr(
        main, "payment_notify_limiter", main.TokenBucketLimiter(3, main.PAYMENT_NOTIFY_RATE_WINDOW)
    )
    payload = {
        "order_id": order["id"],
        "provider_order_id": transaction["provider_order_id"],
        "status": "paid",
        "amount": order["amount"],
        "currency": order["currency"],
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    signed = {
        "x-btf-signature": provider.sign_callback("test-secret", payload),
        "X-Device-Key": key,
    }

    client = TestClient(main.app)
    statuses = [
        client.post("/api/device/payments/mock/notify", content=body, headers=signed).status_code
        for _ in range(5)
    ]
    # First three within budget answer normally; the rest are throttled.
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3:] == [429, 429]

    limited = client.post("/api/device/payments/mock/notify", content=body, headers=signed)
    # Generic error, no internal detail echoed back.
    assert limited.json() == {"success": False, "message": "too many requests"}
    assert "Retry-After" in limited.headers


def test_unconfigured_notify_rate_limited_before_provider_lookup(monkeypatch, tmp_path):
    # An unconfigured provider still gets throttled first (cheap rejection).
    _use_test_database(monkeypatch, tmp_path / "rl2.sqlite3")
    monkeypatch.setattr(
        main, "payment_notify_limiter", main.TokenBucketLimiter(1, main.PAYMENT_NOTIFY_RATE_WINDOW)
    )
    client = TestClient(main.app)
    assert client.post("/api/device/payments/wechat/notify", content=b"{}").status_code == 503
    assert client.post("/api/device/payments/wechat/notify", content=b"{}").status_code == 429


def test_notify_body_over_cap_rejected(monkeypatch, tmp_path):
    _mock_setup(monkeypatch, tmp_path, "BTF-OBS-CAP", payment_notify_max_body_bytes=64)
    client = TestClient(main.app)
    oversized = b"x" * 128
    response = client.post(
        "/api/device/payments/mock/notify",
        content=io.BytesIO(oversized),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json() == {"success": False, "message": "request entity too large"}


def test_mock_complete_body_over_cap_rejected(monkeypatch, tmp_path):
    db, key, order, provider, transaction = _mock_setup(
        monkeypatch, tmp_path, "BTF-OBS-CAP2", payment_notify_max_body_bytes=16
    )
    monkeypatch.setattr(
        main, "payment_notify_limiter", main.TokenBucketLimiter(60, main.PAYMENT_NOTIFY_RATE_WINDOW)
    )
    client = TestClient(main.app)
    response = client.post(
        f"/api/device/payments/mock/complete/{transaction['provider_order_id']}",
        content=io.BytesIO(b"y" * 64),
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 413
    assert response.json() == {"success": False, "message": "request entity too large"}


# --- 3. config: log level parsing -----------------------------------------


def test_log_level_env_falls_back_on_invalid(monkeypatch, tmp_path):
    from app import config

    monkeypatch.setenv("BTF_LOG_LEVEL", "not-a-level")
    assert config.env_log_level() == "INFO"
    monkeypatch.setenv("BTF_LOG_LEVEL", "debug")
    assert config.env_log_level() == "DEBUG"
    monkeypatch.delenv("BTF_LOG_LEVEL", raising=False)
    assert config.env_log_level() == "INFO"
