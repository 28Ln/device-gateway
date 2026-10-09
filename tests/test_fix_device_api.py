from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.service import create_device, create_user, record_usage
import app.main as main


def use_test_database(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(main, "settings", replace(main.settings, database_path=db))
    main.DEVICE_AUTH_CACHE.clear()


def as_admin(monkeypatch, user_id: int = 1, username: str = "root"):
    async def fake_dashboard_user(request, authorization):
        return main.DashboardUser(id=user_id, role=main.ADMIN_ROLE, username=username)

    monkeypatch.setattr(main, "resolve_dashboard_user", fake_dashboard_user)


def future_ts() -> int:
    return main.now_ts() + 30 * 86400


def body(model: str) -> bytes:
    return json.dumps({"model": model, "messages": []}).encode()


# --- Fix 1: /v1/models must not bypass limit/auth -------------------------


def test_models_path_enforces_plan_rpm_limit(monkeypatch, tmp_path):
    db = tmp_path / "models.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:FIX:MODELS", 1)
    with connect(db) as conn:
        conn.execute("update plans set rpm_limit = 1 where id = 'day_1'")
        conn.execute(
            """
            insert into usage_events (device_id, path, status_code, created_at)
            values (?, 'models', 200, ?)
            """,
            ("BTF:FIX:MODELS", main.now_ts()),
        )

    with pytest.raises(HTTPException) as exc:
        main.enforce_plan_access("BTF:FIX:MODELS", "models", "GET", b"")
    assert exc.value.status_code == 429


def test_models_path_enforces_daily_request_limit(monkeypatch, tmp_path):
    db = tmp_path / "models2.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:FIX:MODELS:DAILY", 1)
    with connect(db) as conn:
        conn.execute("update plans set daily_request_limit = 1 where id = 'day_1'")
        conn.execute(
            """
            insert into usage_events (device_id, path, status_code, created_at)
            values (?, '/v1/chat/completions', 200, ?)
            """,
            ("BTF:FIX:MODELS:DAILY", main.now_ts()),
        )

    with pytest.raises(HTTPException) as exc:
        main.enforce_plan_access("BTF:FIX:MODELS:DAILY", "models", "GET", b"")
    assert exc.value.status_code == 403


def test_models_path_within_limits_passes_through(monkeypatch, tmp_path):
    db = tmp_path / "models3.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:FIX:MODELS:OK", 1)

    # No limits configured: the models body is returned untouched.
    assert main.enforce_plan_access("BTF:FIX:MODELS:OK", "models", "GET", b"") == b""


def test_proxy_v1_models_requires_valid_device(monkeypatch, tmp_path):
    db = tmp_path / "models4.sqlite3"
    init_db(db)
    monkeypatch.setattr(
        main,
        "settings",
        replace(main.settings, database_path=db, new_api_token="internal-token"),
    )
    main.DEVICE_AUTH_CACHE.clear()
    client = TestClient(main.app, raise_server_exceptions=False)

    # Unknown device key must be rejected with a device error, never proxied.
    response = client.get("/v1/models", headers={"authorization": "Bearer does-not-exist"})
    assert response.status_code == 403


# --- Fix 3: device list SQL pushdown + pagination bounds ------------------


def seed_devices(db, count: int = 5):
    user = create_user(db, "owner", "Owner")
    macs = []
    for index in range(count):
        mac = f"BTF:FIX:LIST:{index:02d}"
        create_device(db, mac, 30, user_id=user["id"])
        macs.append(mac)
    return user, macs


def test_device_list_paginates_in_sql(monkeypatch, tmp_path):
    db = tmp_path / "list.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    seed_devices(db, 5)
    as_admin(monkeypatch)
    client = TestClient(main.app)

    response = client.get("/api/device/", params={"p": 2, "page_size": 2, "sort_by": "id", "sort_order": "asc"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 5
    assert data["page"] == 2
    assert data["page_size"] == 2
    assert [item["mac"] for item in data["items"]] == ["BTF:FIX:LIST:02", "BTF:FIX:LIST:03"]


def test_device_list_filters_in_sql(monkeypatch, tmp_path):
    db = tmp_path / "list2.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    user, _ = seed_devices(db, 3)
    create_device(db, "ZZZ-OTHER", 30)
    as_admin(monkeypatch)
    client = TestClient(main.app)

    keyword = client.get("/api/device/", params={"keyword": "fix:list:01"}).json()["data"]
    assert keyword["total"] == 1
    assert keyword["items"][0]["mac"] == "BTF:FIX:LIST:01"

    by_user = client.get("/api/device/", params={"user_id": user["id"]}).json()["data"]
    assert by_user["total"] == 3

    disabled = client.get("/api/device/", params={"status": "disabled"}).json()["data"]
    assert disabled["total"] == 0
    active = client.get("/api/device/", params={"status": "active"}).json()["data"]
    assert active["total"] == 4


def test_device_list_expired_status_is_derived(monkeypatch, tmp_path):
    db = tmp_path / "list3.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:FIX:EXPIRED", 30)
    with connect(db) as conn:
        conn.execute(
            "update devices set expires_at = ? where device_id = 'BTF:FIX:EXPIRED'",
            (main.now_ts() - 10,),
        )
    as_admin(monkeypatch)
    client = TestClient(main.app)

    row = client.get("/api/device/", params={"status": "expired"}).json()["data"]
    assert row["total"] == 1
    assert row["items"][0]["status"] == "expired"


@pytest.mark.parametrize(
    "params",
    [
        {"p": 0},
        {"p": -1},
        {"page_size": 0},
        {"page_size": -5},
        {"page_size": main.MAX_PAGE_SIZE + 1},
    ],
)
def test_device_list_rejects_bad_pagination_with_400(monkeypatch, tmp_path, params):
    db = tmp_path / f"badpage-{abs(hash(str(params))) % 1000}.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:FIX:PAGE", 30)
    as_admin(monkeypatch)
    client = TestClient(main.app)

    response = client.get("/api/device/", params=params)
    assert response.status_code == 400


def test_device_list_rejects_unknown_status_with_400(monkeypatch, tmp_path):
    db = tmp_path / "badstatus.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:FIX:STATUS", 30)
    as_admin(monkeypatch)
    client = TestClient(main.app)

    response = client.get("/api/device/", params={"status": "bogus"})
    assert response.status_code == 400


# --- Fix 4: malformed int payloads -> 400 (not 500) -----------------------


def test_create_device_malformed_int_returns_400(monkeypatch, tmp_path):
    db = tmp_path / "create.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    as_admin(monkeypatch)
    client = TestClient(main.app, raise_server_exceptions=False)

    response = client.post(
        "/api/device/",
        json={"mac": "BTF:FIX:CREATE", "expires_at": "not-a-number"},
    )
    assert response.status_code == 400


def test_update_device_malformed_int_returns_400(monkeypatch, tmp_path):
    db = tmp_path / "update.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:FIX:UPDATE", 30)
    as_admin(monkeypatch)
    client = TestClient(main.app, raise_server_exceptions=False)

    response = client.put(
        "/api/device/",
        json={"id": "abc", "mac": "BTF:FIX:UPDATE", "expires_at": future_ts()},
    )
    assert response.status_code == 400


def test_manage_device_malformed_int_returns_400(monkeypatch, tmp_path):
    db = tmp_path / "manage.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    as_admin(monkeypatch)
    client = TestClient(main.app, raise_server_exceptions=False)

    response = client.post(
        "/api/device/manage",
        json={"id": "nope", "action": "renew", "expires_at": future_ts()},
    )
    assert response.status_code == 400


# --- Fix 6: renew/enable cannot store an already-expired active device ----


def test_manage_renew_rejects_past_expiry(monkeypatch, tmp_path):
    db = tmp_path / "renew.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:FIX:RENEW", 30)
    as_admin(monkeypatch)
    client = TestClient(main.app)

    with connect(db) as conn:
        row = conn.execute(
            "select id, expires_at from devices where device_id = 'BTF:FIX:RENEW'"
        ).fetchone()
        device_id = row["id"]
        original_expiry = row["expires_at"]

    response = client.post(
        "/api/device/manage",
        json={"id": device_id, "action": "renew", "expires_at": main.now_ts() - 100},
    )
    assert response.status_code == 200
    assert response.json()["success"] is False

    with connect(db) as conn:
        assert conn.execute(
            "select expires_at from devices where id = ?", (device_id,)
        ).fetchone()["expires_at"] == original_expiry


def test_manage_renew_accepts_future_expiry(monkeypatch, tmp_path):
    db = tmp_path / "renew2.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:FIX:RENEW:OK", 1)
    as_admin(monkeypatch)
    client = TestClient(main.app)

    with connect(db) as conn:
        device_id = conn.execute(
            "select id from devices where device_id = 'BTF:FIX:RENEW:OK'"
        ).fetchone()["id"]

    target = future_ts()
    response = client.post(
        "/api/device/manage",
        json={"id": device_id, "action": "renew", "expires_at": target},
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    with connect(db) as conn:
        row = conn.execute(
            "select expires_at, status from devices where id = ?", (device_id,)
        ).fetchone()
    assert row["expires_at"] == target
    assert row["status"] == "active"


def test_create_active_device_rejects_past_expiry(monkeypatch, tmp_path):
    db = tmp_path / "create-past.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    as_admin(monkeypatch)
    client = TestClient(main.app)

    response = client.post(
        "/api/device/",
        json={"mac": "BTF:FIX:CREATE:PAST", "expires_at": main.now_ts() - 10, "status": "active"},
    )
    assert response.status_code == 200
    assert response.json()["success"] is False


# --- Fix 5: batch create preserves real failure info ---------------------


def test_batch_create_reports_duplicate_failure(monkeypatch, tmp_path):
    db = tmp_path / "batch.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:FIX:BATCH:DUP", 30)
    as_admin(monkeypatch)
    client = TestClient(main.app)

    response = client.post(
        "/api/device/batch",
        json={"macs": "BTF:FIX:BATCH:DUP\nBTF:FIX:BATCH:NEW", "expires_at": future_ts()},
    )
    assert response.status_code == 200
    payload = response.json()
    data = payload["data"]
    assert data["created"] == 1
    assert data["failed"] == ["BTF:FIX:BATCH:DUP"]
    assert data["failures"] and "BTF:FIX:BATCH:DUP" in data["failures"][0]["mac"]
    assert data["failures"][0]["message"]
    # A partial/rejected batch must not claim unconditional success.
    assert payload["message"]


def test_batch_create_malformed_expiry_returns_400(monkeypatch, tmp_path):
    db = tmp_path / "batch2.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    as_admin(monkeypatch)
    client = TestClient(main.app, raise_server_exceptions=False)

    response = client.post(
        "/api/device/batch",
        json={"macs": "BTF:FIX:BATCH:X", "expires_at": "later"},
    )
    assert response.status_code == 400
