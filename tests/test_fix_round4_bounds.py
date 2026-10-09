"""Round-4 robustness: integer request-body fields are range-checked.

``int_or_400`` previously only called ``int()``; an oversized JSON body
integer (e.g. ``10**30`` / ``2**70``) reached a SQL binding and raised
``OverflowError`` -> HTTP 500. It now enforces SQLite's signed 64-bit range
so every request-body integer field answers 400 instead.

Also covers:
  * ``POST /api/device/manage {action: "enable", expires_at: <absurd>}`` is
    bounded by ``MAX_EXPIRES_AT`` like ``renew``.
  * ``GET /api/device/?user_id=-5`` is rejected rather than silently returning
    every device.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.db import connect, init_db
from app.service import create_device, create_user

# Comfortably outside SQLite's signed 64-bit range.
HUGE = 10 ** 30
HUGE_2_70 = 2 ** 70
INT64_MAX = 2 ** 63 - 1


def use_test_database(monkeypatch: pytest.MonkeyPatch, db: Path) -> None:
    monkeypatch.setattr(main, "settings", replace(main.settings, database_path=db))
    main.DEVICE_AUTH_CACHE.clear()


def as_admin(monkeypatch: pytest.MonkeyPatch, user_id: int = 1) -> None:
    async def fake_dashboard_user(request, authorization):
        return main.DashboardUser(id=user_id, role=main.ADMIN_ROLE, username="root")

    monkeypatch.setattr(main, "resolve_dashboard_user", fake_dashboard_user)


def future_ts() -> int:
    return main.now_ts() + 30 * 86400


@pytest.fixture()
def env(monkeypatch, tmp_path):
    db = tmp_path / "round4-bounds.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    user = create_user(db, "r4-bounds", "Round4 Bounds")
    device = create_device(db, "BTF:R4:BOUNDS:A", 30, user_id=user["id"])
    as_admin(monkeypatch)
    client = TestClient(main.app, raise_server_exceptions=False)
    return {"client": client, "db": db, "user": user, "device": device}


# --- oversized body integers -> 400 (never 500) ---------------------------


def test_manage_device_id_huge_is_400(env):
    response = env["client"].post(
        "/api/device/manage", json={"id": HUGE, "action": "disable"}
    )
    assert response.status_code == 400


def test_manage_enable_huge_expires_at_is_400(env):
    with connect(env["db"]) as conn:
        device_id = conn.execute(
            "select id from devices where device_id = 'BTF:R4:BOUNDS:A'"
        ).fetchone()["id"]
        conn.execute("update devices set status = 'disabled' where id = ?", (device_id,))
        conn.execute("update devices set expires_at = 0 where id = ?", (device_id,))

    response = env["client"].post(
        "/api/device/manage",
        json={"id": device_id, "action": "enable", "expires_at": HUGE_2_70},
    )
    assert response.status_code == 400


def test_manage_enable_expires_at_above_max_is_400(env):
    """Absurd (but int64-valid) expiry is bounded like renew."""
    with connect(env["db"]) as conn:
        device_id = conn.execute(
            "select id from devices where device_id = 'BTF:R4:BOUNDS:A'"
        ).fetchone()["id"]
        conn.execute("update devices set status = 'disabled' where id = ?", (device_id,))
        conn.execute("update devices set expires_at = 0 where id = ?", (device_id,))

    response = env["client"].post(
        "/api/device/manage",
        json={"id": device_id, "action": "enable", "expires_at": INT64_MAX},
    )
    assert response.status_code == 400


def test_update_device_huge_id_is_400(env):
    response = env["client"].put(
        "/api/device/",
        json={"id": HUGE, "mac": "BTF:R4:BOUNDS:B", "expires_at": future_ts()},
    )
    assert response.status_code == 400


def test_create_device_huge_user_id_is_400(env):
    response = env["client"].post(
        "/api/device/",
        json={"user_id": HUGE, "mac": "BTF:R4:BOUNDS:C", "expires_at": future_ts()},
    )
    assert response.status_code == 400


def test_batch_create_huge_user_id_is_400(env):
    response = env["client"].post(
        "/api/device/batch",
        json={"user_id": HUGE, "macs": "BTF:R4:BOUNDS:D", "expires_at": future_ts()},
    )
    assert response.status_code == 400


def test_user_limits_huge_token_limit_is_400(env):
    response = env["client"].put(
        f"/api/device/users/{env['user']['id']}/limits",
        json={"token_limit": HUGE},
    )
    assert response.status_code == 400


def test_plan_update_huge_token_limit_is_400(env):
    response = env["client"].put(
        "/api/device/plans/month_1", json={"token_limit": HUGE}
    )
    assert response.status_code == 400


# --- int64 boundary remains usable ----------------------------------------


def test_manage_device_int64_max_still_accepted(env):
    """The boundary value itself must not be rejected as "too large"."""
    response = env["client"].post(
        "/api/device/manage", json={"id": INT64_MAX, "action": "disable"}
    )
    assert response.status_code == 200


def test_user_limits_int64_max_still_accepted(env):
    response = env["client"].put(
        f"/api/device/users/{env['user']['id']}/limits",
        json={"token_limit": INT64_MAX},
    )
    assert response.status_code == 200
    assert response.json()["success"] is True


def test_plan_update_int64_max_still_accepted(env):
    response = env["client"].put(
        "/api/device/plans/month_1", json={"token_limit": INT64_MAX}
    )
    assert response.status_code == 200
    assert response.json()["success"] is True


# --- /api/device/?user_id=-5 is rejected ----------------------------------


@pytest.mark.parametrize("value", ["-5", "-1"])
def test_device_list_negative_user_id_is_400(env, value):
    response = env["client"].get("/api/device/", params={"user_id": value})
    assert response.status_code == 400


def test_device_list_positive_user_id_still_works(env):
    response = env["client"].get(
        "/api/device/", params={"user_id": env["user"]["id"]}
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert len(response.json()["data"]["items"]) == 1
