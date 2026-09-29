from __future__ import annotations

import json
from dataclasses import replace

from fastapi.testclient import TestClient

from app.db import init_db
from app.service import create_device, create_user, record_usage
import app.main as main


def test_customer_usage_endpoints_are_scoped_to_bound_devices(monkeypatch, tmp_path):
    db = tmp_path / "usage.sqlite3"
    init_db(db)
    monkeypatch.setattr(main, "settings", replace(main.settings, database_path=db))

    customer_a = create_user(db, "customer-a", "Customer A")
    customer_b = create_user(db, "customer-b", "Customer B")
    create_device(db, "BTF:TEST:A", 30, user_id=customer_a["id"])
    create_device(db, "BTF:TEST:B", 30, user_id=customer_b["id"])

    record_usage(
        db,
        "BTF:TEST:A",
        "/v1/chat/completions",
        200,
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        user_id=customer_a["id"],
        plan_id="month_1",
        requested_model="btf-chat",
        upstream_model="deepseek-v4.1-flash",
    )
    record_usage(
        db,
        "BTF:TEST:B",
        "/v1/chat/completions",
        200,
        prompt_tokens=100,
        completion_tokens=200,
        total_tokens=300,
        user_id=customer_b["id"],
        plan_id="month_1",
        requested_model="btf-chat",
        upstream_model="deepseek-v4.1-flash",
    )

    async def fake_customer(request, authorization):
        return main.DashboardUser(
            id=customer_a["id"],
            role=1,
            username=customer_a["username"],
        )

    monkeypatch.setattr(main, "resolve_dashboard_user", fake_customer)
    client = TestClient(main.app)

    logs = client.get("/api/log/self", params={"p": 1, "page_size": 20})
    assert logs.status_code == 200
    log_data = logs.json()["data"]
    assert log_data["total"] == 1
    assert log_data["items"][0]["token_name"] == "BTF:TEST:A"
    assert log_data["items"][0]["quota"] == 30

    stats = client.get("/api/log/self/stat")
    assert stats.status_code == 200
    assert stats.json()["data"] == {"quota": 30, "rpm": 1, "tpm": 30}

    chart = client.get(
        "/api/data/self",
        params={"start_timestamp": 1, "end_timestamp": main.now_ts() + 1},
    )
    assert chart.status_code == 200
    assert chart.json()["data"][0]["token_used"] == 30

    flow = client.get(
        "/api/data/flow/self",
        params={"start_timestamp": 1, "end_timestamp": main.now_ts() + 1},
    )
    assert flow.status_code == 200
    assert flow.json()["data"][0]["token_name"] == "BTF:TEST:A"

    foreign = client.get(
        "/api/log/self",
        params={"token_name": "BTF:TEST:B", "page_size": 20},
    )
    assert foreign.status_code == 200
    assert foreign.json()["data"]["total"] == 0

    top_up = client.get("/api/log/self", params={"type": 1, "page_size": 20})
    assert top_up.status_code == 200
    assert top_up.json()["data"]["total"] == 0


def test_customer_self_response_uses_btf_subscription_usage(monkeypatch, tmp_path):
    db = tmp_path / "self.sqlite3"
    init_db(db)
    monkeypatch.setattr(main, "settings", replace(main.settings, database_path=db))

    customer = create_user(db, "customer-self", "Customer Self")
    create_device(db, "BTF:TEST:SELF", 30, user_id=customer["id"])
    record_usage(
        db,
        "BTF:TEST:SELF",
        "/v1/chat/completions",
        200,
        total_tokens=123,
        user_id=customer["id"],
        plan_id="month_1",
        requested_model="btf-chat",
        upstream_model="deepseek-v4.1-flash",
    )

    content = json.dumps(
        {
            "success": True,
            "data": {
                "id": customer["id"],
                "role": 1,
                "username": customer["username"],
                "quota": 0,
                "used_quota": 0,
                "request_count": 0,
            },
        }
    ).encode()
    result = json.loads(main.augment_customer_self_response(content, "application/json"))

    assert result["data"]["used_quota"] == 123
    assert result["data"]["request_count"] == 1
    assert result["data"]["quota"] == 3_000_000 - 123
    assert result["data"]["btf_usage"]["successful_tokens"] == 123
