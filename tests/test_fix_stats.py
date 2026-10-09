from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.service import create_device, create_user, record_usage
import app.main as main


def use_test_database(monkeypatch, db):
    monkeypatch.setattr(main, "settings", replace(main.settings, database_path=db))
    main.DEVICE_AUTH_CACHE.clear()


def as_customer(monkeypatch, user):
    async def fake_dashboard_user(request, authorization):
        return main.DashboardUser(id=user["id"], role=1, username=user["username"])

    monkeypatch.setattr(main, "resolve_dashboard_user", fake_dashboard_user)


def insert_usage(db, device_id, user_id, created_at, total_tokens, status_code=200):
    with connect(db) as conn:
        conn.execute(
            """
            insert into usage_events (
              device_id, user_id, plan_id, path, status_code,
              requested_model, upstream_model,
              prompt_tokens, completion_tokens, total_tokens, estimated_cost, created_at
            )
            values (?, ?, 'month_1', '/v1/chat/completions', ?, 'btf-chat',
                    'deepseek-v4.1-flash', 0, 0, ?, 0, ?)
            """,
            (device_id, user_id, status_code, total_tokens, created_at),
        )


# --- Fix 2: /api/log/self/stat rpm/tpm are a 60s window -------------------


def test_usage_stat_rpm_tpm_use_recent_window(monkeypatch, tmp_path):
    db = tmp_path / "stat.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    user = create_user(db, "stat-user", "Stat User")
    create_device(db, "BTF:STAT:A", 30, user_id=user["id"])

    now = main.now_ts()
    # One recent successful request and one stale one outside the window.
    insert_usage(db, "BTF:STAT:A", user["id"], now, 30)
    insert_usage(db, "BTF:STAT:A", user["id"], now - 3600, 500)

    as_customer(monkeypatch, user)
    client = TestClient(main.app)
    stats = client.get("/api/log/self/stat").json()["data"]

    # quota is the lifetime successful-token total ...
    assert stats["quota"] == 530
    # ... while rpm/tpm only reflect the last 60 seconds.
    assert stats["rpm"] == 1
    assert stats["tpm"] == 30


def test_usage_window_stats_excludes_errors(monkeypatch, tmp_path):
    db = tmp_path / "stat2.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    user = create_user(db, "stat-user2", "Stat User 2")
    create_device(db, "BTF:STAT:B", 30, user_id=user["id"])

    now = main.now_ts()
    insert_usage(db, "BTF:STAT:B", user["id"], now, 10, status_code=200)
    insert_usage(db, "BTF:STAT:B", user["id"], now, 999, status_code=500)

    window = main.btf_usage_window_stats(user["id"], 60)
    assert window == {"rpm": 1, "tpm": 10}


def test_usage_window_scopes_to_owner(monkeypatch, tmp_path):
    db = tmp_path / "stat3.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    user_a = create_user(db, "stat-a", "Stat A")
    user_b = create_user(db, "stat-b", "Stat B")
    create_device(db, "BTF:STAT:A2", 30, user_id=user_a["id"])
    create_device(db, "BTF:STAT:B2", 30, user_id=user_b["id"])

    now = main.now_ts()
    insert_usage(db, "BTF:STAT:A2", user_a["id"], now, 5)
    insert_usage(db, "BTF:STAT:B2", user_b["id"], now, 700)

    assert main.btf_usage_window_stats(user_a["id"], 60) == {"rpm": 1, "tpm": 5}


# --- Fix 7: btf_usage_where type filter semantics -------------------------


def make_request(params):
    from starlette.requests import Request

    query = "&".join(f"{k}={v}" for k, v in params.items())
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/log/self",
        "query_string": query.encode(),
        "headers": [],
    }
    return Request(scope)


def test_btf_usage_where_status_type_filter():
    success_where, _ = main.btf_usage_where(1, make_request({"type": 2}))
    assert "e.status_code < 400" in success_where

    error_where, _ = main.btf_usage_where(1, make_request({"type": 5}))
    assert "e.status_code >= 400" in error_where


def test_btf_usage_where_unsupported_type_is_empty_not_partial():
    # New API "Top-up" (type 1) has no backing data in this gateway. It must
    # resolve to an explicitly empty result rather than silently matching
    # unrelated rows or crashing.
    where, params = main.btf_usage_where(1, make_request({"type": 1}))
    assert "1 = 0" in where
    assert params == [1]


def test_usage_log_type_top_up_endpoint_is_empty(monkeypatch, tmp_path):
    db = tmp_path / "type.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    user = create_user(db, "type-user", "Type User")
    create_device(db, "BTF:TYPE:A", 30, user_id=user["id"])
    insert_usage(db, "BTF:TYPE:A", user["id"], main.now_ts(), 42)

    as_customer(monkeypatch, user)
    client = TestClient(main.app)

    assert client.get("/api/log/self", params={"type": 2}).json()["data"]["total"] == 1
    assert client.get("/api/log/self", params={"type": 5}).json()["data"]["total"] == 0
    assert client.get("/api/log/self", params={"type": 1}).json()["data"]["total"] == 0
