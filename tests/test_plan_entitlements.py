from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.service import create_device, create_user, record_usage
from app.security import hash_key
import app.main as main


def use_test_database(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(main, "settings", replace(main.settings, database_path=db))


def body(model: str) -> bytes:
    return json.dumps({"model": model, "messages": []}).encode()


def test_day_plan_allows_deepseek_and_blocks_image(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:PLAN:DAY", 1)

    rewritten = main.enforce_plan_access(
        "BTF:PLAN:DAY",
        "chat/completions",
        "POST",
        body("btf-chat"),
    )
    assert json.loads(rewritten)["model"] == "deepseek-v4.1-flash"

    with pytest.raises(HTTPException) as exc:
        main.enforce_plan_access(
            "BTF:PLAN:DAY",
            "images/generations",
            "POST",
            body("[b]gpt-image-2"),
        )
    assert exc.value.status_code == 403


def test_month_plan_allows_configured_image_models(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:PLAN:MONTH", 30)

    rewritten = main.enforce_plan_access(
        "BTF:PLAN:MONTH",
        "images/generations",
        "POST",
        body("btf-image"),
    )
    assert json.loads(rewritten)["model"] == "[b]gpt-image-2"

    with pytest.raises(HTTPException) as exc:
        main.enforce_plan_access(
            "BTF:PLAN:MONTH",
            "images/generations",
            "POST",
            body("gemini-2.5-flash-image-preview"),
        )
    assert exc.value.status_code == 403


def test_models_response_is_filtered_by_plan(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:PLAN:DAY", 1)
    create_device(db, "BTF:PLAN:MONTH", 30)
    payload = {
        "object": "list",
        "data": [
            {"id": "deepseek-v4.1-flash"},
            {"id": "[b]gpt-image-2"},
            {"id": "gemini-2.5-flash-image-preview"},
        ],
    }

    day_models = json.loads(
        main.filter_models_response(
            json.dumps(payload).encode(),
            "application/json",
            "BTF:PLAN:DAY",
        )
    )
    assert [item["id"] for item in day_models["data"]] == ["btf-chat"]

    month_models = json.loads(
        main.filter_models_response(
            json.dumps(payload).encode(),
            "application/json",
            "BTF:PLAN:MONTH",
        )
    )
    assert [item["id"] for item in month_models["data"]] == [
        "btf-chat",
        "btf-image",
    ]


def test_completion_response_model_is_mapped_back_to_alias(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:PLAN:ALIAS", 1)
    payload = {"model": "deepseek-v4.1-flash", "choices": []}

    result = json.loads(
        main.alias_model_response(
            json.dumps(payload).encode(),
            "application/json",
            "BTF:PLAN:ALIAS",
        )
    )

    assert result["model"] == "btf-chat"


def test_image_response_model_is_mapped_back_to_alias(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:PLAN:IMAGE:ALIAS", 30)
    payload = {"model": "gpt-image-2", "data": []}

    result = json.loads(
        main.alias_model_response(
            json.dumps(payload).encode(),
            "application/json",
            "BTF:PLAN:IMAGE:ALIAS",
        )
    )

    assert result["model"] == "btf-image"


def test_image_generation_response_is_saved(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    image_dir = tmp_path / "generated-images"
    init_db(db)
    monkeypatch.setattr(
        main,
        "settings",
        replace(
            main.settings,
            database_path=db,
            generated_images_dir=image_dir,
            public_base_url="http://testserver",
        ),
    )
    user = create_user(db, "image-user", "Image User")
    create_device(db, "BTF:IMAGE:SAVE", 30, user_id=user["id"])
    usage_event_id = record_usage(
        db,
        "BTF:IMAGE:SAVE",
        "/v1/images/generations",
        200,
        user_id=user["id"],
        plan_id="month_1",
        requested_model="btf-image",
        upstream_model="[b]gpt-image-2",
    )
    payload = {
        "model": "gpt-image-2",
        "data": [
            {
                "b64_json": base64.b64encode(
                    b"\x89PNG\r\n\x1a\n" + b"test-image"
                ).decode()
            }
        ],
    }

    result = json.loads(
        asyncio.run(
            main.persist_image_generation_response(
                json.dumps(payload).encode(),
                "application/json",
                "BTF:IMAGE:SAVE",
                user["id"],
                "month_1",
                "btf-image",
                "[b]gpt-image-2",
                json.dumps({"prompt": "red square"}).encode(),
                usage_event_id,
            )
        )
    )

    public_url = result["data"][0]["url"]
    assert public_url.startswith("http://testserver/device-images/")
    relative_path = result["data"][0]["btf_local_path"]
    assert (image_dir / relative_path).is_file()
    with connect(db) as conn:
        row = conn.execute(
            """
            select usage_event_id, device_id, user_id, plan_id, requested_model,
                   upstream_model, prompt, public_url, byte_size
            from generated_images
            """
        ).fetchone()
    assert row["usage_event_id"] == usage_event_id
    assert row["device_id"] == "BTF:IMAGE:SAVE"
    assert row["user_id"] == user["id"]
    assert row["plan_id"] == "month_1"
    assert row["requested_model"] == "btf-image"
    assert row["upstream_model"] == "[b]gpt-image-2"
    assert row["prompt"] == "red square"
    assert row["public_url"] == public_url
    assert row["byte_size"] > 0


def test_plan_request_limit_blocks_device(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:PLAN:LIMIT", 1)
    with connect(db) as conn:
        conn.execute(
            "update plans set daily_request_limit = 1 where id = 'day_1'",
        )
        conn.execute(
            """
            insert into usage_events (
              device_id, path, status_code, prompt_tokens, completion_tokens, total_tokens, created_at
            )
            values (?, '/v1/chat/completions', 200, 0, 0, 0, ?)
            """,
            ("BTF:PLAN:LIMIT", main.now_ts()),
        )

    with pytest.raises(HTTPException) as exc:
        main.enforce_plan_access(
            "BTF:PLAN:LIMIT",
            "chat/completions",
            "POST",
            body("btf-chat"),
        )
    assert exc.value.status_code == 403


def test_plan_rpm_limit_returns_429(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:PLAN:RPM", 1)
    with connect(db) as conn:
        conn.execute("update plans set rpm_limit = 1 where id = 'day_1'")
        conn.execute(
            """
            insert into usage_events (
              device_id, path, status_code, prompt_tokens, completion_tokens, total_tokens, created_at
            )
            values (?, '/v1/chat/completions', 200, 0, 0, 0, ?)
            """,
            ("BTF:PLAN:RPM", main.now_ts()),
        )

    with pytest.raises(HTTPException) as exc:
        main.enforce_plan_access(
            "BTF:PLAN:RPM",
            "chat/completions",
            "POST",
            body("btf-chat"),
        )
    assert exc.value.status_code == 429


def test_user_daily_limit_blocks_all_devices_for_user(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    user = create_user(db, "heavy-user", "Heavy User")
    create_device(db, "BTF:USER:LIMIT:1", 1, user_id=user["id"])
    create_device(db, "BTF:USER:LIMIT:2", 1, user_id=user["id"])
    with connect(db) as conn:
        conn.execute(
            "update users set daily_request_limit = 1 where id = ?",
            (user["id"],),
        )
        conn.execute(
            """
            insert into usage_events (
              device_id, user_id, path, status_code, prompt_tokens,
              completion_tokens, total_tokens, created_at
            )
            values (?, ?, '/v1/chat/completions', 200, 0, 0, 0, ?)
            """,
            ("BTF:USER:LIMIT:1", user["id"], main.now_ts()),
        )

    with pytest.raises(HTTPException) as exc:
        main.enforce_plan_access(
            "BTF:USER:LIMIT:2",
            "chat/completions",
            "POST",
            body("btf-chat"),
        )
    assert exc.value.status_code == 403
    assert "user daily request" in exc.value.detail


def test_record_usage_stores_attribution_fields(tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    user = create_user(db, "usage-user", "Usage User")
    create_device(db, "BTF:USAGE:ATTR", 1, user_id=user["id"])

    record_usage(
        db,
        "BTF:USAGE:ATTR",
        "/v1/chat/completions",
        200,
        10,
        5,
        15,
        user["id"],
        "day_1",
        "btf-chat",
        "deepseek-v4.1-flash",
        0.1234,
    )

    with connect(db) as conn:
        row = conn.execute(
            """
            select user_id, plan_id, requested_model, upstream_model, estimated_cost
            from usage_events
            where device_id = ?
            """,
            ("BTF:USAGE:ATTR",),
        ).fetchone()
    assert row["user_id"] == user["id"]
    assert row["plan_id"] == "day_1"
    assert row["requested_model"] == "btf-chat"
    assert row["upstream_model"] == "deepseek-v4.1-flash"
    assert row["estimated_cost"] == 0.1234


def test_dashboard_user_can_bind_inventory_device(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:PLAN:BIND", 1)
    with connect(db) as conn:
        conn.execute(
            """
            update devices
            set user_id = null, bind_code_hash = ?, bind_code_mask = '****1234', bound_at = null
            where device_id = ?
            """,
            (hash_key("ABCD1234"), "BTF:PLAN:BIND"),
        )

    async def fake_dashboard_user(request, authorization):
        return main.DashboardUser(id=42, role=1, username="customer")

    monkeypatch.setattr(main, "resolve_dashboard_user", fake_dashboard_user)
    client = TestClient(main.app)

    response = client.post(
        "/api/device/bind",
        json={"mac": "BTF:PLAN:BIND", "bind_code": "abcd1234"},
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    with connect(db) as conn:
        row = conn.execute(
            "select user_id, bound_at from devices where device_id = ?",
            ("BTF:PLAN:BIND",),
        ).fetchone()
    assert row["user_id"] == 42
    assert row["bound_at"]


def test_public_plan_list_hides_internal_limits(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    client = TestClient(main.app)

    response = client.get("/api/device/plans")

    assert response.status_code == 200
    item = response.json()["data"]["items"][0]
    assert "token_limit" not in item
    assert "rpm_limit" not in item
    assert "chat_models" not in item
    assert "image_models" not in item
    assert "chat_alias" not in item
    assert "image_alias" not in item


def test_approved_order_updates_current_device_plan(tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    key, _ = create_device(db, "BTF:PLAN:ORDER", 1)

    from app.service import approve_order, create_order

    order = create_order(db, key, "month_1")
    approve_order(db, order["id"])

    with connect(db) as conn:
        row = conn.execute(
            "select plan_id from devices where device_id = ?",
            ("BTF:PLAN:ORDER",),
        ).fetchone()
    assert row["plan_id"] == "month_1"
