"""Round-5 residual state-defence tests.

Focus: a device row must never be both ``status='active'`` and already
expired. Three independent guards are covered:

  1. ``POST /api/device/manage {action: "enable"}`` on a residual
     active+expired row converges it to a consistent state.
  2. ``POST/PUT /api/device/`` can never persist active+expired, including
     when an explicit past ``expires_at`` is supplied.
  3. The db-layer repair migration normalises historic active+expired rows
     and is idempotent / version-guarded.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.db import MIGRATIONS, connect, init_db
from app.db import normalize_active_expired_devices
from app.service import create_device
import app.main as main


# --- helpers --------------------------------------------------------------


def use_test_database(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(main, "settings", replace(main.settings, database_path=db))
    main.DEVICE_AUTH_CACHE.clear()


def as_admin(monkeypatch: pytest.MonkeyPatch, user_id: int = 1):
    async def fake_dashboard_user(request, authorization):
        return main.DashboardUser(id=user_id, role=main.ADMIN_ROLE, username="root")

    monkeypatch.setattr(main, "resolve_dashboard_user", fake_dashboard_user)


def future_ts() -> int:
    return main.now_ts() + 30 * 86400


def past_ts() -> int:
    return main.now_ts() - 3600


def device_id_for(db, mac: str) -> int:
    with connect(db) as conn:
        return conn.execute(
            "select id from devices where device_id = ?", (mac,)
        ).fetchone()["id"]


def device_row(db, device_id: int):
    with connect(db) as conn:
        return conn.execute(
            "select status, expires_at from devices where id = ?", (device_id,)
        ).fetchone()


def make_residual_active_expired(db, mac: str) -> int:
    """Simulate historic data: an active row whose expiry already lapsed."""
    create_device(db, mac, 30)
    device_id = device_id_for(db, mac)
    with connect(db) as conn:
        conn.execute(
            "update devices set status = 'active', expires_at = ? where id = ?",
            (past_ts(), device_id),
        )
    # Precondition: the residual state really exists.
    row = device_row(db, device_id)
    assert row["status"] == "active" and row["expires_at"] <= main.now_ts()
    return device_id


# --- 1. enable converges a residual active+expired row --------------------


def test_enable_residual_active_expired_without_renewal_is_disabled(monkeypatch, tmp_path):
    db = tmp_path / "r5-enable-residual.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    device_id = make_residual_active_expired(db, "BTF:R5:RESIDUAL:A")
    as_admin(monkeypatch)
    client = TestClient(main.app)

    response = client.post(
        "/api/device/manage", json={"id": device_id, "action": "enable"}
    )
    assert response.status_code == 200
    assert response.json()["success"] is False

    row = device_row(db, device_id)
    # The residual combination is gone: no longer active+expired.
    assert not (row["status"] == "active" and row["expires_at"] <= main.now_ts())
    assert row["status"] == "disabled"
    # No entitlement was fabricated: the expiry is not silently extended.
    assert row["expires_at"] <= main.now_ts()


def test_enable_residual_active_expired_with_renewal_activates(monkeypatch, tmp_path):
    db = tmp_path / "r5-enable-renew.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    device_id = make_residual_active_expired(db, "BTF:R5:RESIDUAL:B")
    as_admin(monkeypatch)
    client = TestClient(main.app)

    target = future_ts()
    response = client.post(
        "/api/device/manage",
        json={"id": device_id, "action": "enable", "expires_at": target},
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    row = device_row(db, device_id)
    assert row["status"] == "active"
    assert row["expires_at"] == target
    assert row["expires_at"] > main.now_ts()


def test_enable_residual_active_expired_is_idempotent(monkeypatch, tmp_path):
    db = tmp_path / "r5-enable-idem.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    device_id = make_residual_active_expired(db, "BTF:R5:RESIDUAL:C")
    as_admin(monkeypatch)
    client = TestClient(main.app)

    client.post("/api/device/manage", json={"id": device_id, "action": "enable"})
    first = device_row(db, device_id)
    client.post("/api/device/manage", json={"id": device_id, "action": "enable"})
    second = device_row(db, device_id)
    assert first["status"] == second["status"] == "disabled"
    assert first["expires_at"] == second["expires_at"]


# --- 2. create / update never persist active+expired ----------------------


def test_create_with_past_expires_at_default_status_is_rejected(monkeypatch, tmp_path):
    db = tmp_path / "r5-create-past-default.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    as_admin(monkeypatch)
    client = TestClient(main.app)

    response = client.post(
        "/api/device/",
        json={"mac": "BTF:R5:CREATE:PAST", "expires_at": past_ts()},
    )
    assert response.status_code == 200
    assert response.json()["success"] is False
    with connect(db) as conn:
        assert conn.execute(
            "select count(*) as n from devices where device_id = 'BTF:R5:CREATE:PAST'"
        ).fetchone()["n"] == 0


def test_create_with_past_expires_at_as_expired_status_succeeds(monkeypatch, tmp_path):
    db = tmp_path / "r5-create-past-expired.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    as_admin(monkeypatch)
    client = TestClient(main.app)

    expiry = past_ts()
    response = client.post(
        "/api/device/",
        json={
            "mac": "BTF:R5:CREATE:EXPIRED",
            "status": "expired",
            "expires_at": expiry,
        },
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    row = device_row(db, device_id_for(db, "BTF:R5:CREATE:EXPIRED"))
    assert row["status"] == "expired"
    assert row["expires_at"] == expiry


def test_create_active_status_with_past_expiry_is_inconsistent_and_rejected(monkeypatch, tmp_path):
    db = tmp_path / "r5-create-active-past.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    as_admin(monkeypatch)
    client = TestClient(main.app)

    response = client.post(
        "/api/device/",
        json={
            "mac": "BTF:R5:CREATE:ACTIVEPAST",
            "status": "active",
            "expires_at": past_ts(),
        },
    )
    assert response.status_code == 200
    assert response.json()["success"] is False
    with connect(db) as conn:
        assert conn.execute(
            "select count(*) as n from devices where device_id = 'BTF:R5:CREATE:ACTIVEPAST'"
        ).fetchone()["n"] == 0


def test_update_active_with_past_expiry_is_rejected_and_row_unchanged(monkeypatch, tmp_path):
    db = tmp_path / "r5-update-active-past.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:R5:UPDATE:PAST", 30)
    device_id = device_id_for(db, "BTF:R5:UPDATE:PAST")
    before = device_row(db, device_id)
    as_admin(monkeypatch)
    client = TestClient(main.app)

    response = client.put(
        "/api/device/",
        json={
            "id": device_id,
            "mac": "BTF:R5:UPDATE:PAST",
            "status": "active",
            "expires_at": past_ts(),
        },
    )
    assert response.status_code == 200
    assert response.json()["success"] is False
    after = device_row(db, device_id)
    # The device was already active+future; the rejected update left it intact.
    assert not (after["status"] == "active" and after["expires_at"] <= main.now_ts())
    assert after["expires_at"] == before["expires_at"]


def test_update_to_expired_status_with_past_expiry_succeeds(monkeypatch, tmp_path):
    db = tmp_path / "r5-update-expired.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:R5:UPDATE:EXPIRED", 30)
    device_id = device_id_for(db, "BTF:R5:UPDATE:EXPIRED")
    as_admin(monkeypatch)
    client = TestClient(main.app)

    expiry = past_ts()
    response = client.put(
        "/api/device/",
        json={
            "id": device_id,
            "mac": "BTF:R5:UPDATE:EXPIRED",
            "status": "expired",
            "expires_at": expiry,
        },
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    row = device_row(db, device_id)
    assert row["status"] == "expired" and row["expires_at"] == expiry


def test_update_to_active_with_future_expiry_still_succeeds(monkeypatch, tmp_path):
    db = tmp_path / "r5-update-active-future.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF:R5:UPDATE:FUTURE", 30)
    device_id = device_id_for(db, "BTF:R5:UPDATE:FUTURE")
    as_admin(monkeypatch)
    client = TestClient(main.app)

    target = future_ts()
    response = client.put(
        "/api/device/",
        json={
            "id": device_id,
            "mac": "BTF:R5:UPDATE:FUTURE",
            "status": "active",
            "expires_at": target,
        },
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    row = device_row(db, device_id)
    assert row["status"] == "active" and row["expires_at"] == target


# --- 3. db-layer repair migration ----------------------------------------


def test_migration_normalizes_active_expired_rows(tmp_path):
    db = tmp_path / "r5-migration.sqlite3"
    init_db(db)
    # A healthy active+future device that must survive untouched.
    create_device(db, "BTF:R5:MIG:HEALTHY", 30)
    healthy_id = device_id_for(db, "BTF:R5:MIG:HEALTHY")
    healthy_expiry = device_row(db, healthy_id)["expires_at"]

    # Historic inconsistent rows: active but expired.
    create_device(db, "BTF:R5:MIG:STALE1", 30)
    create_device(db, "BTF:R5:MIG:STALE2", 30)
    for mac in ("BTF:R5:MIG:STALE1", "BTF:R5:MIG:STALE2"):
        with connect(db) as conn:
            conn.execute(
                "update devices set status = 'active', expires_at = ? where device_id = ?",
                (past_ts(), mac),
            )
    # Forget the repair migration so a later init_db runs it again.
    with connect(db) as conn:
        conn.execute("delete from schema_version where version = 3")

    init_db(db)

    assert device_row(db, healthy_id)["expires_at"] == healthy_expiry
    assert device_row(db, healthy_id)["status"] == "active"
    for mac in ("BTF:R5:MIG:STALE1", "BTF:R5:MIG:STALE2"):
        assert device_row(db, device_id_for(db, mac))["status"] == "disabled"
    # No active+expired row remains anywhere.
    with connect(db) as conn:
        assert conn.execute(
            "select count(*) as n from devices where status = 'active' and expires_at <= ?",
            (main.now_ts(),),
        ).fetchone()["n"] == 0


def test_normalize_helper_is_idempotent(tmp_path):
    db = tmp_path / "r5-idempotent.sqlite3"
    init_db(db)
    create_device(db, "BTF:R5:IDEM:STALE", 30)
    with connect(db) as conn:
        conn.execute(
            "update devices set status = 'active', expires_at = ? where device_id = ?",
            (past_ts(), "BTF:R5:IDEM:STALE"),
        )
    with connect(db) as conn:
        assert normalize_active_expired_devices(conn) == 1
        # Second pass repairs nothing.
        assert normalize_active_expired_devices(conn) == 0
    assert device_row(db, device_id_for(db, "BTF:R5:IDEM:STALE"))["status"] == "disabled"


def test_migration_versions_are_unique_and_sorted():
    versions = [version for version, _ in MIGRATIONS]
    assert versions == sorted(set(versions))
