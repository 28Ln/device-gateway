from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.service import create_device
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
