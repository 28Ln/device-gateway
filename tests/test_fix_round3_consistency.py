"""Round-3 data-consistency regression tests.

Covers four reported defects:
  1. ``service._immediate`` bypassing SQLite foreign-key enforcement.
  2. Device rename via ``PUT /api/device/`` either succeeding atomically or
     being refused with an explicit 400 (never a silent ``success=False`` 200).
  3. ``POST /api/device/manage {action: "enable"}`` never producing an
     ``active`` device that is already ``expired``.
  4. ``/api/device/users/usage`` and ``/api/log/self`` agreeing on usage whose
     ``usage_events.user_id`` is NULL (device-owner fallback).
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.service import _immediate, create_device, create_user, record_usage
import app.main as main


# --- helpers --------------------------------------------------------------


def use_test_database(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(main, "settings", replace(main.settings, database_path=db))
    main.DEVICE_AUTH_CACHE.clear()


def as_admin(monkeypatch: pytest.MonkeyPatch, user_id: int = 1):
    async def fake_dashboard_user(request, authorization):
        return main.DashboardUser(id=user_id, role=main.ADMIN_ROLE, username="root")

    monkeypatch.setattr(main, "resolve_dashboard_user", fake_dashboard_user)


def as_customer(monkeypatch: pytest.MonkeyPatch, user_id: int, username: str):
    async def fake_dashboard_user(request, authorization):
        return main.DashboardUser(id=user_id, role=1, username=username)

    monkeypatch.setattr(main, "resolve_dashboard_user", fake_dashboard_user)


def future_ts() -> int:
    return main.now_ts() + 30 * 86400


# --- 1. _immediate enforces foreign keys ----------------------------------


def test_immediate_connection_enforces_foreign_keys(monkeypatch, tmp_path):
    db = tmp_path / "fk.sqlite3"
    init_db(db)
    create_device(db, "BTF:R3:FK", 30)

    # A dangling usage_events.device_id must be rejected on the _immediate
    # write path exactly like on db.connect.
    with pytest.raises(sqlite3.IntegrityError):
        with _immediate(db) as conn:
            conn.execute(
                """
                insert into usage_events (device_id, path, status_code, created_at)
                values ('BTF:R3:MISSING', '/v1/chat/completions', 200, ?)
                """,
                (main.now_ts(),),
            )

    # The failed transaction left no rows behind.
    with connect(db) as conn:
        assert conn.execute(
            "select count(*) as n from usage_events where device_id = 'BTF:R3:MISSING'"
        ).fetchone()["n"] == 0


# --- 2. device rename -----------------------------------------------------


def test_rename_device_without_history_succeeds(monkeypatch, tmp_path):
    db = tmp_path / "rename-ok.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:R3:OLD", 30)
    as_admin(monkeypatch)
    client = TestClient(main.app)

    with connect(db) as conn:
        device_id = conn.execute(
            "select id from devices where device_id = 'BTF:R3:OLD'"
        ).fetchone()["id"]

    response = client.put(
        "/api/device/",
        json={
            "id": device_id,
            "mac": "BTF:R3:NEW",
            "expires_at": future_ts(),
            "status": "active",
        },
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    with connect(db) as conn:
        row = conn.execute(
            "select device_id from devices where id = ?", (device_id,)
        ).fetchone()
    assert row["device_id"] == "BTF:R3:NEW"


def test_rename_device_with_history_is_refused_with_400(monkeypatch, tmp_path):
    db = tmp_path / "rename-history.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    owner = create_user(db, "rename-owner", "Rename Owner")
    create_device(db, "BTF:R3:HIST", 30, user_id=owner["id"])
    record_usage(
        db,
        "BTF:R3:HIST",
        "/v1/chat/completions",
        200,
        total_tokens=7,
        user_id=owner["id"],
    )
    as_admin(monkeypatch)
    client = TestClient(main.app, raise_server_exceptions=False)

    with connect(db) as conn:
        device_id = conn.execute(
            "select id from devices where device_id = 'BTF:R3:HIST'"
        ).fetchone()["id"]

    response = client.put(
        "/api/device/",
        json={
            "id": device_id,
            "mac": "BTF:R3:HIST:NEW",
            "expires_at": future_ts(),
            "status": "active",
        },
    )
    assert response.status_code == 400
    # The device id (and therefore the referencing history) is untouched.
    with connect(db) as conn:
        assert conn.execute(
            "select device_id from devices where id = ?", (device_id,)
        ).fetchone()["device_id"] == "BTF:R3:HIST"
        assert conn.execute(
            "select count(*) as n from usage_events where device_id = 'BTF:R3:HIST'"
        ).fetchone()["n"] == 1


# --- 3. enable never yields active+expired --------------------------------


def _make_expired_disabled(db, mac: str) -> int:
    create_device(db, mac, 1)
    with connect(db) as conn:
        row = conn.execute(
            "select id, expires_at from devices where device_id = ?", (mac,)
        ).fetchone()
        device_id = row["id"]
        original_expiry = row["expires_at"]
        conn.execute(
            "update devices set expires_at = ?, status = 'disabled' where id = ?",
            (main.now_ts() - 100, device_id),
        )
    assert original_expiry > 0
    return device_id


def test_enable_expired_device_without_extension_is_rejected(monkeypatch, tmp_path):
    db = tmp_path / "enable-expired.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    device_id = _make_expired_disabled(db, "BTF:R3:ENABLE:EXPIRED")
    as_admin(monkeypatch)
    client = TestClient(main.app)

    response = client.post(
        "/api/device/manage",
        json={"id": device_id, "action": "enable"},
    )
    assert response.status_code == 200
    assert response.json()["success"] is False
    with connect(db) as conn:
        row = conn.execute(
            "select status, expires_at from devices where id = ?", (device_id,)
        ).fetchone()
    # Never left active while expired.
    assert not (row["status"] == "active" and row["expires_at"] <= main.now_ts())


def test_enable_expired_device_extends_in_same_request(monkeypatch, tmp_path):
    db = tmp_path / "enable-extend.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    device_id = _make_expired_disabled(db, "BTF:R3:ENABLE:EXTEND")
    as_admin(monkeypatch)
    client = TestClient(main.app)

    target = future_ts()
    response = client.post(
        "/api/device/manage",
        json={"id": device_id, "action": "enable", "expires_at": target},
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    with connect(db) as conn:
        row = conn.execute(
            "select status, expires_at from devices where id = ?", (device_id,)
        ).fetchone()
    assert row["status"] == "active"
    assert row["expires_at"] == target
    assert row["expires_at"] > main.now_ts()


def test_enable_current_device_needs_no_extension(monkeypatch, tmp_path):
    db = tmp_path / "enable-current.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:R3:ENABLE:OK", 30)
    as_admin(monkeypatch)
    client = TestClient(main.app)

    with connect(db) as conn:
        device_id = conn.execute(
            "select id from devices where device_id = 'BTF:R3:ENABLE:OK'"
        ).fetchone()["id"]
        conn.execute(
            "update devices set status = 'disabled' where id = ?", (device_id,)
        )

    response = client.post(
        "/api/device/manage",
        json={"id": device_id, "action": "enable"},
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    with connect(db) as conn:
        assert conn.execute(
            "select status from devices where id = ?", (device_id,)
        ).fetchone()["status"] == "active"


# --- 4. NULL-owner attribution is consistent across endpoints -------------


def test_null_owner_usage_agrees_between_usage_and_log_views(monkeypatch, tmp_path):
    db = tmp_path / "null-owner.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    owner = create_user(db, "null-owner", "Null Owner")
    create_device(db, "BTF:R3:NULL", 30, user_id=owner["id"])
    record_usage(
        db,
        "BTF:R3:NULL",
        "/v1/chat/completions",
        200,
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        user_id=owner["id"],
    )
    record_usage(
        db,
        "BTF:R3:NULL",
        "/v1/chat/completions",
        200,
        prompt_tokens=40,
        completion_tokens=50,
        total_tokens=90,
        user_id=owner["id"],
    )
    # Simulate legacy rows written before usage_events.user_id existed: only
    # the device still carries the owner.
    with connect(db) as conn:
        conn.execute("update usage_events set user_id = null where device_id = 'BTF:R3:NULL'")

    # Admin usage view.
    as_admin(monkeypatch)
    admin_client = TestClient(main.app)
    usage = admin_client.get("/api/device/users/usage").json()["data"]["items"]
    row = next(item for item in usage if item["id"] == owner["id"])
    assert row["request_count"] == 2
    assert row["total_tokens"] == 120

    # Customer self log view.
    as_customer(monkeypatch, owner["id"], owner["username"])
    customer_client = TestClient(main.app)
    logs = customer_client.get("/api/log/self", params={"page_size": 20}).json()["data"]
    assert logs["total"] == 2
    assert sum(item["quota"] for item in logs["items"]) == 120
