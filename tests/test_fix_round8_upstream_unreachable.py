"""Round-8: ``/v1`` upstream-unreachable must surface as a structured 502.

When the backend New API is down, ``proxy_v1`` used to let
``httpx.ConnectError`` escape the ``except BaseException`` branch, producing a
bare (non-JSON) HTTP 500. Its sibling ``proxy_new_api`` already answers with
``502 {"detail": "upstream request failed"}``; this pins ``/v1`` to the same
contract and checks the device concurrency slot is still released exactly once.

No mocking of the upstream: the base URL is pointed at an unreachable local
port so the failure is a genuine transport error, not a synthetic 500.
"""
from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

import app.main as main
from app.db import init_db
from app.service import create_device

UNREACHABLE = "http://127.0.0.1:1"


def _use_test_database(monkeypatch, db, **overrides):
    overrides.setdefault("new_api_token", "test-upstream-token")
    overrides.setdefault("new_api_base_url", UNREACHABLE)
    monkeypatch.setattr(main, "settings", replace(main.settings, database_path=db, **overrides))
    monkeypatch.setattr(main, "_upstream_client", None)
    monkeypatch.setattr(main, "_stream_client", None)
    main.DEVICE_AUTH_CACHE.clear()
    main.ACTIVE_REQUESTS.clear()
    main.ACTIVE_USER_REQUESTS.clear()


def test_v1_models_unreachable_upstream_is_502_json(monkeypatch, tmp_path):
    db = tmp_path / "r8-models.sqlite3"
    init_db(db)
    _use_test_database(monkeypatch, db)
    device_id = "BTF:R8:MODELS"
    key, _ = create_device(db, device_id, 30)

    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get("/v1/models", headers={"authorization": f"Bearer {key}"})

    assert response.status_code == 502
    assert response.json() == {"detail": "upstream request failed"}
    # The concurrency slot must be released (no residue for the device).
    assert main.ACTIVE_REQUESTS.get(device_id, 0) == 0


def test_v1_chat_completions_unreachable_upstream_is_502_json(monkeypatch, tmp_path):
    db = tmp_path / "r8-chat.sqlite3"
    init_db(db)
    _use_test_database(monkeypatch, db)
    device_id = "BTF:R8:CHAT"
    key, _ = create_device(db, device_id, 30)

    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "btf-chat", "messages": [{"role": "user", "content": "hi"}]},
        headers={"authorization": f"Bearer {key}"},
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "upstream request failed"}
    assert main.ACTIVE_REQUESTS.get(device_id, 0) == 0
