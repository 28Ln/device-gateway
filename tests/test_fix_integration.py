"""End-to-end integration tests for the core device-gateway journeys.

These exercise the full ASGI stack with ``TestClient`` and a mocked upstream
HTTP transport (``httpx.MockTransport``), so no real network, database or
credentials are involved. They lock in the contract that matters for a safe
launch:

  1. ``/v1/chat/completions``: device auth -> plan/quota -> model alias
     rewrite -> upstream call -> usage persisted with sane tokens/cost.
  2. ``/v1/models``: alias visibility and the fact that it no longer
     early-returns before the quota checks.
  3. Device/order CRUD ownership isolation (non-admin 403, admin allowed).
  4. Concurrency limits reject requests that would exceed the configured slot.
  5. ``/device-images/..`` traversal is rejected.

Every test rebinds ``app.main.settings`` (and clears the auth cache) so it runs
against a throwaway SQLite database under ``tmp_path``.
"""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.service import create_device, create_order, create_user
import app.main as main


# --- helpers ---------------------------------------------------------------


def use_settings(monkeypatch: pytest.MonkeyPatch, db, **overrides):
    """Point the gateway at a throwaway DB / upstream and reset shared caches."""
    overrides.setdefault("new_api_token", "test-upstream-token")
    overrides.setdefault("new_api_base_url", "http://upstream.test")
    monkeypatch.setattr(
        main, "settings", replace(main.settings, database_path=db, **overrides)
    )
    main.DEVICE_AUTH_CACHE.clear()


def reset_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main, "_upstream_client", None)
    monkeypatch.setattr(main, "_stream_client", None)
    main.ACTIVE_REQUESTS.clear()
    main.ACTIVE_USER_REQUESTS.clear()


def install_upstream(monkeypatch: pytest.MonkeyPatch, handler):
    """Route every upstream call (plain + stream clients) through a mock."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(main, "upstream_client", lambda: client)
    monkeypatch.setattr(main, "stream_client", lambda: client)
    return client


def as_user(monkeypatch: pytest.MonkeyPatch, user_id: int, username: str = "customer"):
    async def fake_dashboard_user(request, authorization):
        return main.DashboardUser(id=user_id, role=1, username=username)

    monkeypatch.setattr(main, "resolve_dashboard_user", fake_dashboard_user)


def as_admin(monkeypatch: pytest.MonkeyPatch, user_id: int = 1, username: str = "root"):
    async def fake_dashboard_user(request, authorization):
        return main.DashboardUser(id=user_id, role=main.ADMIN_ROLE, username=username)

    monkeypatch.setattr(main, "resolve_dashboard_user", fake_dashboard_user)


def future_ts() -> int:
    return main.now_ts() + 30 * 86400


def json_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/json"},
        content=json.dumps(payload).encode(),
    )


def chat_body(model: str = "btf-chat") -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "hi"}]}


# --- 1) /v1/chat/completions full chain ------------------------------------


def test_chat_completions_end_to_end_records_usage(monkeypatch, tmp_path):
    db = tmp_path / "chat.sqlite3"
    init_db(db)
    use_settings(
        monkeypatch,
        db,
        chat_prompt_cost_per_million=2.0,
        chat_completion_cost_per_million=4.0,
    )
    reset_state(monkeypatch)
    owner = create_user(db, "chat-owner", "Chat Owner")
    key, _ = create_device(db, "BTF:INT:CHAT", 1, user_id=owner["id"])

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content.decode())
        return json_response(
            {
                "id": "chatcmpl-1",
                "model": "deepseek-v4.1-flash",
                "usage": {
                    "prompt_tokens": 200_000,
                    "completion_tokens": 100_000,
                    "total_tokens": 300_000,
                },
            }
        )

    install_upstream(monkeypatch, handler)
    client = TestClient(main.app)

    response = client.post(
        "/v1/chat/completions",
        json=chat_body("btf-chat"),
        headers={"authorization": f"Bearer {key}"},
    )

    assert response.status_code == 200
    # The client keeps seeing the friendly alias, not the upstream model id.
    assert response.json()["model"] == "btf-chat"
    # Upstream got the real model id and the internal relay token, never the
    # device key.
    assert seen["path"] == "/v1/chat/completions"
    assert seen["body"]["model"] == "deepseek-v4.1-flash"
    assert seen["auth"] == "Bearer test-upstream-token"

    with connect(db) as conn:
        row = conn.execute(
            "select * from usage_events where device_id = ?", ("BTF:INT:CHAT",)
        ).fetchone()
        device = conn.execute(
            "select request_count, total_tokens from devices where device_id = ?",
            ("BTF:INT:CHAT",),
        ).fetchone()

    assert row is not None
    assert row["status_code"] == 200
    assert row["requested_model"] == "btf-chat"
    assert row["upstream_model"] == "deepseek-v4.1-flash"
    assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (
        200_000,
        100_000,
        300_000,
    )
    # 0.2M * 2 + 0.1M * 4 = 0.4 + 0.4 = 0.8
    assert row["estimated_cost"] == pytest.approx(0.8)
    assert row["user_id"] == owner["id"]
    assert device["request_count"] == 1
    assert device["total_tokens"] == 300_000


def test_chat_plan_quota_rejects_before_upstream_and_records_nothing(monkeypatch, tmp_path):
    db = tmp_path / "chat-limit.sqlite3"
    init_db(db)
    use_settings(monkeypatch, db)
    reset_state(monkeypatch)
    key, _ = create_device(db, "BTF:INT:CHAT:LIMIT", 1)
    with connect(db) as conn:
        conn.execute("update plans set daily_request_limit = 1 where id = 'day_1'")
        conn.execute(
            """
            insert into usage_events (device_id, path, status_code, created_at)
            values (?, '/v1/chat/completions', 200, ?)
            """,
            ("BTF:INT:CHAT:LIMIT", main.now_ts()),
        )

    calls: list[int] = []
    install_upstream(
        monkeypatch,
        lambda request: calls.append(1) or json_response({"model": "deepseek-v4.1-flash"}),
    )
    client = TestClient(main.app)

    response = client.post(
        "/v1/chat/completions",
        json=chat_body(),
        headers={"authorization": f"Bearer {key}"},
    )

    assert response.status_code == 403
    assert calls == []  # quota enforced before any upstream call
    with connect(db) as conn:
        count = conn.execute(
            "select count(*) as n from usage_events where device_id = ?",
            ("BTF:INT:CHAT:LIMIT",),
        ).fetchone()["n"]
    assert count == 1  # the seeded row only; the rejected request is not billed


@pytest.mark.parametrize("state", ["expired", "disabled"])
def test_expired_and_disabled_devices_are_rejected(monkeypatch, tmp_path, state):
    db = tmp_path / f"auth-{state}.sqlite3"
    init_db(db)
    use_settings(monkeypatch, db)
    reset_state(monkeypatch)
    device_id = f"BTF:INT:AUTH:{state.upper()}"
    key, _ = create_device(db, device_id, 1)
    with connect(db) as conn:
        if state == "expired":
            conn.execute(
                "update devices set expires_at = ? where device_id = ?",
                (main.now_ts() - 10, device_id),
            )
        else:
            conn.execute(
                "update devices set status = 'disabled' where device_id = ?", (device_id,)
            )

    calls: list[int] = []
    install_upstream(
        monkeypatch,
        lambda request: calls.append(1) or json_response({"model": "deepseek-v4.1-flash"}),
    )
    client = TestClient(main.app)

    response = client.post(
        "/v1/chat/completions",
        json=chat_body(),
        headers={"authorization": f"Bearer {key}"},
    )

    assert response.status_code == 403
    assert calls == []


def test_missing_bearer_token_is_401(monkeypatch, tmp_path):
    db = tmp_path / "noauth.sqlite3"
    init_db(db)
    use_settings(monkeypatch, db)
    reset_state(monkeypatch)
    install_upstream(monkeypatch, lambda request: json_response({"model": "x"}))
    client = TestClient(main.app, raise_server_exceptions=False)

    response = client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == 401


# --- 2) /v1/models: alias visibility + limits interaction ------------------


def test_models_lists_plan_aliases_and_is_limited(monkeypatch, tmp_path):
    db = tmp_path / "models.sqlite3"
    init_db(db)
    use_settings(monkeypatch, db)
    reset_state(monkeypatch)
    key, _ = create_device(db, "BTF:INT:MODELS", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            {
                "object": "list",
                "data": [
                    {"id": "deepseek-v4.1-flash", "object": "model"},
                    {"id": "gpt-5.5", "object": "model"},
                    {"id": "some-other-upstream", "object": "model"},
                ],
            }
        )

    install_upstream(monkeypatch, handler)
    client = TestClient(main.app)

    response = client.get("/v1/models", headers={"authorization": f"Bearer {key}"})

    assert response.status_code == 200
    ids = [model["id"] for model in response.json()["data"]]
    assert "btf-chat" in ids  # plan alias is visible
    assert "some-other-upstream" not in ids  # not entitled -> filtered out
    # The models request is itself recorded, proving it went through the same
    # quota-accounted path (no early-return bypass).
    with connect(db) as conn:
        count = conn.execute(
            "select count(*) as n from usage_events where device_id = ?",
            ("BTF:INT:MODELS",),
        ).fetchone()["n"]
    assert count == 1


def test_models_is_blocked_once_quota_exhausted(monkeypatch, tmp_path):
    db = tmp_path / "models-limit.sqlite3"
    init_db(db)
    use_settings(monkeypatch, db)
    reset_state(monkeypatch)
    key, _ = create_device(db, "BTF:INT:MODELS:LIMIT", 1)
    with connect(db) as conn:
        conn.execute("update plans set daily_request_limit = 1 where id = 'day_1'")
        conn.execute(
            """
            insert into usage_events (device_id, path, status_code, created_at)
            values (?, '/v1/models', 200, ?)
            """,
            ("BTF:INT:MODELS:LIMIT", main.now_ts()),
        )

    calls: list[int] = []
    install_upstream(
        monkeypatch, lambda request: calls.append(1) or json_response({"object": "list", "data": []})
    )
    client = TestClient(main.app)

    response = client.get(
        "/v1/models", headers={"authorization": f"Bearer {key}"}
    )

    assert response.status_code == 403
    assert calls == []


def test_models_rejects_unknown_device(monkeypatch, tmp_path):
    db = tmp_path / "models-auth.sqlite3"
    init_db(db)
    use_settings(monkeypatch, db)
    reset_state(monkeypatch)
    calls: list[int] = []
    install_upstream(
        monkeypatch, lambda request: calls.append(1) or json_response({"object": "list", "data": []})
    )
    client = TestClient(main.app, raise_server_exceptions=False)

    response = client.get(
        "/v1/models", headers={"authorization": "Bearer not-a-real-device"}
    )
    assert response.status_code == 403
    assert calls == []


# --- 3) device / order ownership isolation ---------------------------------


def test_device_crud_isolation_between_users_and_admin(monkeypatch, tmp_path):
    db = tmp_path / "crud.sqlite3"
    init_db(db)
    use_settings(monkeypatch, db)
    reset_state(monkeypatch)
    owner_a = create_user(db, "owner-a", "Owner A")
    owner_b = create_user(db, "owner-b", "Owner B")
    _, device_a = create_device(db, "BTF:INT:OWN:A", 30, user_id=owner_a["id"])
    _, device_b = create_device(db, "BTF:INT:OWN:B", 30, user_id=owner_b["id"])

    client = TestClient(main.app, raise_server_exceptions=False)

    # A normal user only sees/reads their own device.
    as_user(monkeypatch, owner_b["id"], "owner-b")
    assert client.get(f"/api/device/{device_a['id']}").status_code == 403
    assert client.get(f"/api/device/{device_b['id']}").status_code == 200

    # ...and cannot create, manage, or approve as if admin.
    assert (
        client.post(
            "/api/device/",
            json={"mac": "BTF:INT:NEW", "expires_at": future_ts()},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/device/manage",
            json={"id": device_a["id"], "action": "disable"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/device/orders/approve", json={"order_id": "whatever"}
        ).status_code
        == 403
    )

    # The admin may do all of the above.
    as_admin(monkeypatch, user_id=999)
    assert client.get(f"/api/device/{device_a['id']}").status_code == 200
    managed = client.post(
        "/api/device/manage",
        json={"id": device_a["id"], "action": "disable"},
    )
    assert managed.json()["success"] is True
    created = client.post(
        "/api/device/",
        json={"mac": "BTF:INT:NEW", "expires_at": future_ts()},
    )
    assert created.json()["success"] is True


def test_order_access_is_isolated_per_owner(monkeypatch, tmp_path):
    db = tmp_path / "orders.sqlite3"
    init_db(db)
    use_settings(monkeypatch, db)
    reset_state(monkeypatch)
    owner_a = create_user(db, "order-a", "Order A")
    owner_b = create_user(db, "order-b", "Order B")
    create_device(db, "BTF:INT:ORD:A", 30, user_id=owner_a["id"])
    create_device(db, "BTF:INT:ORD:B", 30, user_id=owner_b["id"])
    order_a = create_order(db, "BTF:INT:ORD:A", "day_1")

    client = TestClient(main.app, raise_server_exceptions=False)

    # B does not see A's order, and cannot close it.
    as_user(monkeypatch, owner_b["id"], "order-b")
    items_b = client.get("/api/device/orders").json()["data"]["items"]
    assert all(item["id"] != order_a["id"] for item in items_b)
    closed = client.post(
        f"/api/device/orders/{order_a['id']}/close", json={"reason": "nope"}
    )
    assert closed.json()["success"] is False

    # A sees their own order.
    as_user(monkeypatch, owner_a["id"], "order-a")
    items_a = client.get("/api/device/orders").json()["data"]["items"]
    assert any(item["id"] == order_a["id"] for item in items_a)

    # An admin sees every order.
    as_admin(monkeypatch, user_id=999)
    items_admin = client.get("/api/device/orders").json()["data"]["items"]
    assert {item["id"] for item in items_admin} >= {order_a["id"]}


# --- 4) concurrency limits -------------------------------------------------


def test_device_concurrency_limit_rejects_when_slot_taken(monkeypatch, tmp_path):
    db = tmp_path / "conc.sqlite3"
    init_db(db)
    use_settings(monkeypatch, db)
    reset_state(monkeypatch)
    key, _ = create_device(db, "BTF:INT:CONC", 1)
    with connect(db) as conn:
        conn.execute("update plans set concurrency_limit = 1 where id = 'day_1'")

    calls: list[int] = []
    install_upstream(
        monkeypatch,
        lambda request: calls.append(1) or json_response({"model": "deepseek-v4.1-flash"}),
    )
    client = TestClient(main.app)

    # Simulate an in-flight request holding the single slot.
    main.ACTIVE_REQUESTS["BTF:INT:CONC"] = 1
    response = client.post(
        "/v1/chat/completions",
        json=chat_body(),
        headers={"authorization": f"Bearer {key}"},
    )
    assert response.status_code == 429
    assert calls == []  # rejected before the upstream call

    # Releasing the slot lets the next request through.
    main.ACTIVE_REQUESTS.pop("BTF:INT:CONC")
    ok = client.post(
        "/v1/chat/completions",
        json=chat_body(),
        headers={"authorization": f"Bearer {key}"},
    )
    assert ok.status_code == 200
    assert main.ACTIVE_REQUESTS.get("BTF:INT:CONC") is None  # slot released again


def test_user_concurrency_limit_rejects_when_slot_taken(monkeypatch, tmp_path):
    db = tmp_path / "conc-user.sqlite3"
    init_db(db)
    use_settings(monkeypatch, db)
    reset_state(monkeypatch)
    owner = create_user(db, "conc-owner", "Conc Owner")
    key, _ = create_device(db, "BTF:INT:CONC:USER", 1, user_id=owner["id"])
    with connect(db) as conn:
        # Isolate the user-level limit from the plan-level one.
        conn.execute("update plans set concurrency_limit = 0 where id = 'day_1'")
        conn.execute(
            "update users set concurrency_limit = 1 where id = ?", (owner["id"],)
        )

    calls: list[int] = []
    install_upstream(
        monkeypatch,
        lambda request: calls.append(1) or json_response({"model": "deepseek-v4.1-flash"}),
    )
    client = TestClient(main.app)

    main.ACTIVE_USER_REQUESTS[owner["id"]] = 1
    response = client.post(
        "/v1/chat/completions",
        json=chat_body(),
        headers={"authorization": f"Bearer {key}"},
    )
    assert response.status_code == 429
    assert calls == []


# --- 5) /device-images traversal -------------------------------------------


def test_device_images_rejects_traversal(monkeypatch, tmp_path):
    db = tmp_path / "images.sqlite3"
    init_db(db)
    images_dir = tmp_path / "generated-images"
    use_settings(monkeypatch, db, generated_images_dir=images_dir)
    reset_state(monkeypatch)

    client = TestClient(main.app, raise_server_exceptions=False)
    for path in (
        "/device-images/../btf.db",
        "/device-images/../../etc/passwd",
        "/device-images/%2e%2e/btf.db",
        "/device-images/..%2fbtf.db",
        "/device-images/./x.png",
    ):
        response = client.get(path)
        assert response.status_code in (400, 404), path

    # A legitimately stored image is still served.
    target = images_dir / "20260101" / "sample.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"integration")
    ok = client.get("/device-images/20260101/sample.png")
    assert ok.status_code == 200
    assert ok.content.startswith(b"\x89PNG")
