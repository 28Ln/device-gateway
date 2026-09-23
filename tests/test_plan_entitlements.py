from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi import HTTPException

from app.db import connect, init_db
from app.service import create_device
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

    main.enforce_plan_access(
        "BTF:PLAN:DAY",
        "chat/completions",
        "POST",
        body("deepseek-v4.1-flash"),
    )

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

    main.enforce_plan_access(
        "BTF:PLAN:MONTH",
        "images/generations",
        "POST",
        body("[b]gpt-image-2"),
    )

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
    assert [item["id"] for item in day_models["data"]] == ["deepseek-v4.1-flash"]

    month_models = json.loads(
        main.filter_models_response(
            json.dumps(payload).encode(),
            "application/json",
            "BTF:PLAN:MONTH",
        )
    )
    assert [item["id"] for item in month_models["data"]] == [
        "deepseek-v4.1-flash",
        "[b]gpt-image-2",
    ]


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
