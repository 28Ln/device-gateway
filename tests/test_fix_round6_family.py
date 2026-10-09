"""Round-6 family closure: two same-family residues found by the 3rd
independent verification pass.

  B1. Creating (or batch-creating) a device with a duplicate ``device_id``
      echoed the raw SQLite error (``UNIQUE constraint failed: devices.key_hash``)
      back to the client. Those paths now answer with a friendly, schema-free
      message and log the detail server-side.
  B2. Non-object JSON bodies (``[]``/``null``/``123``/``"str"``/``true``/``1.5``)
      on ``/v1/*`` and the mock payment callback hit ``payload.get`` on a
      non-dict -> ``AttributeError`` -> HTTP 500. They now answer 4xx.
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.db import connect, init_db
from app.service import create_device

# JSON bodies that parse to a non-object value.
NON_OBJECT_BODIES = ["[]", "null", "123", '"str"', "true", "1.5"]

LEAK_MARKERS = ("UNIQUE", "constraint", "sqlite", "devices.", "key_hash")


def use_settings(monkeypatch: pytest.MonkeyPatch, db, **overrides) -> None:
    overrides.setdefault("new_api_token", "test-upstream-token")
    overrides.setdefault("new_api_base_url", "http://upstream.test")
    monkeypatch.setattr(
        main, "settings", replace(main.settings, database_path=db, **overrides)
    )
    main.DEVICE_AUTH_CACHE.clear()


def as_admin(monkeypatch: pytest.MonkeyPatch, user_id: int = 1) -> None:
    async def fake_dashboard_user(request, authorization):
        return main.DashboardUser(id=user_id, role=main.ADMIN_ROLE, username="root")

    monkeypatch.setattr(main, "resolve_dashboard_user", fake_dashboard_user)


def future_ts() -> int:
    return main.now_ts() + 30 * 86400


def raw_json(client: TestClient, method: str, url: str, body: str, **kw):
    headers = {"content-type": "application/json", **kw.pop("headers", {})}
    return getattr(client, method)(url, content=body, headers=headers, **kw)


def assert_no_sql_leak(message: str) -> None:
    for leak in LEAK_MARKERS:
        assert leak.lower() not in message.lower(), message


# --- B1: create / batch-create duplicates must not leak SQL -------------


def test_create_duplicate_does_not_leak_sql(monkeypatch, tmp_path):
    db = tmp_path / "r6-create.sqlite3"
    init_db(db)
    use_settings(monkeypatch, db)
    as_admin(monkeypatch)
    client = TestClient(main.app, raise_server_exceptions=False)

    first = client.post(
        "/api/device/",
        json={"mac": "BTF:R6:CREATE:DUP", "expires_at": future_ts()},
    )
    assert first.status_code == 200
    assert first.json()["success"] is True

    dup = client.post(
        "/api/device/",
        json={"mac": "BTF:R6:CREATE:DUP", "expires_at": future_ts()},
    )
    assert dup.status_code == 200
    body = dup.json()
    assert body["success"] is False
    assert body["message"] == "device id already exists"
    assert_no_sql_leak(body["message"])


def test_batch_create_duplicate_does_not_leak_sql(monkeypatch, tmp_path):
    db = tmp_path / "r6-batch.sqlite3"
    init_db(db)
    use_settings(monkeypatch, db)
    create_device(db, "BTF:R6:BATCH:DUP", 30)
    as_admin(monkeypatch)
    client = TestClient(main.app, raise_server_exceptions=False)

    response = client.post(
        "/api/device/batch",
        json={
            "macs": "BTF:R6:BATCH:DUP\nBTF:R6:BATCH:NEW",
            "expires_at": future_ts(),
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["created"] == 1
    assert data["failed"] == ["BTF:R6:BATCH:DUP"]
    failure = data["failures"][0]
    assert failure["mac"] == "BTF:R6:BATCH:DUP"
    assert failure["message"] == "device id already exists"
    assert_no_sql_leak(failure["message"])
    assert_no_sql_leak(json.dumps(data))


# --- B2a: /v1/* non-object JSON body -> 4xx, never 500 ------------------


def test_v1_chat_completions_non_object_body_is_4xx(monkeypatch, tmp_path):
    db = tmp_path / "r6-v1.sqlite3"
    init_db(db)
    use_settings(monkeypatch, db)
    key, _ = create_device(db, "BTF:R6:V1", 1)
    client = TestClient(main.app, raise_server_exceptions=False)

    for raw in NON_OBJECT_BODIES:
        response = raw_json(
            client,
            "post",
            "/v1/chat/completions",
            raw,
            headers={"content-type": "application/json", "authorization": f"Bearer {key}"},
        )
        assert response.status_code == 400, (raw, response.status_code, response.text)


def test_request_model_from_body_non_object_returns_empty():
    for raw in NON_OBJECT_BODIES:
        assert main.request_model_from_body(raw.encode()) == ""
    # Legitimate object bodies keep working.
    assert main.request_model_from_body(b'{"model":"btf-chat"}') == "btf-chat"


# --- B2b: mock notify non-object body -> 400/401, never 500 -------------


def test_mock_notify_non_object_body_is_not_500(monkeypatch, tmp_path):
    db = tmp_path / "r6-notify.sqlite3"
    init_db(db)
    use_settings(
        monkeypatch,
        db,
        demo_payment_enabled=True,
        mock_payment_secret="test-secret",
    )
    key, _ = create_device(db, "BTF:R6:NOTIFY", 1)
    client = TestClient(main.app, raise_server_exceptions=False)

    random_key = "not-a-real-device-key"
    for raw in NON_OBJECT_BODIES:
        response = client.post(
            "/api/device/payments/mock/notify",
            content=raw,
            headers={"content-type": "application/json", "X-Device-Key": random_key},
        )
        assert response.status_code in (400, 401), (raw, response.status_code, response.text)

    # With a valid device key the non-object payload is a 400 bad payload.
    for raw in NON_OBJECT_BODIES:
        response = client.post(
            "/api/device/payments/mock/notify",
            content=raw,
            headers={"content-type": "application/json", "X-Device-Key": key},
        )
        assert response.status_code == 400, (raw, response.status_code, response.text)
