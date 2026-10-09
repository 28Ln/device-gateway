"""Round-4 high-risk regression tests: ``user_usage_rows`` double counting.

Regression: ``/api/device/users/usage`` joined ``devices`` to ``users`` and
then ``usage_events`` on ``coalesce(e.user_id, d.user_id)``. For a user with
several devices every event was paired with every device, multiplying the
request/token/cost/daily counters by the device count (3 devices -> 3x).

These tests pin the corrected aggregation: each event resolves its owner
exactly once via ``coalesce(e.user_id, device_owner)`` before grouping, and
the totals agree with ``/api/log/self``.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.service import create_device, create_user, record_usage
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


def usage_row_for(client: TestClient, user_id: int) -> dict:
    items = client.get("/api/device/users/usage").json()["data"]["items"]
    return next(item for item in items if item["id"] == user_id)


# --- 1. multi-device users are not multiplied -----------------------------


def test_multi_device_user_is_not_double_counted(monkeypatch, tmp_path):
    """3 devices, all events on device A -> counted once, not 3x."""
    db = tmp_path / "multi-device.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    owner = create_user(db, "multi", "Multi Device")
    for i in range(3):
        create_device(db, f"BTF:R4:MULTI:{i}", 30, user_id=owner["id"])
    # Three events, all recorded against device A.
    for _ in range(3):
        record_usage(
            db,
            "BTF:R4:MULTI:0",
            "/v1/chat/completions",
            200,
            total_tokens=60,
            estimated_cost=0.5,
            user_id=owner["id"],
        )

    as_admin(monkeypatch)
    client = TestClient(main.app)
    row = usage_row_for(client, owner["id"])
    assert row["request_count"] == 3
    assert row["total_tokens"] == 180
    assert row["estimated_cost"] == pytest.approx(1.5)
    assert row["daily_request_count"] == 3
    assert row["daily_tokens"] == 180
    assert row["daily_estimated_cost"] == pytest.approx(1.5)


def test_multi_device_events_spread_across_devices_counted_once(
    monkeypatch, tmp_path
):
    """Events on each of the 3 devices sum exactly once each."""
    db = tmp_path / "spread.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    owner = create_user(db, "spread", "Spread")
    for i in range(3):
        create_device(db, f"BTF:R4:SPREAD:{i}", 30, user_id=owner["id"])
    for i in range(3):
        record_usage(
            db,
            f"BTF:R4:SPREAD:{i}",
            "/v1/chat/completions",
            200,
            total_tokens=10 * (i + 1),
            user_id=owner["id"],
        )

    as_admin(monkeypatch)
    client = TestClient(main.app)
    row = usage_row_for(client, owner["id"])
    assert row["request_count"] == 3
    assert row["total_tokens"] == 60  # 10 + 20 + 30, not multiplied


# --- 2. NULL owner falls back to the device owner exactly once -------------


def test_null_owner_events_attributed_to_device_owner_once(monkeypatch, tmp_path):
    """Legacy rows with NULL user_id resolve to the device's current owner."""
    db = tmp_path / "null-owner.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    owner = create_user(db, "legacy", "Legacy Owner")
    create_device(db, "BTF:R4:NULL:0", 30, user_id=owner["id"])
    create_device(db, "BTF:R4:NULL:1", 30, user_id=owner["id"])
    record_usage(
        db,
        "BTF:R4:NULL:0",
        "/v1/chat/completions",
        200,
        total_tokens=30,
        user_id=owner["id"],
    )
    record_usage(
        db,
        "BTF:R4:NULL:1",
        "/v1/chat/completions",
        200,
        total_tokens=90,
        user_id=owner["id"],
    )
    # Simulate rows written before usage_events.user_id existed.
    with connect(db) as conn:
        conn.execute(
            "update usage_events set user_id = null where device_id like 'BTF:R4:NULL:%'"
        )

    as_admin(monkeypatch)
    client = TestClient(main.app)
    row = usage_row_for(client, owner["id"])
    assert row["request_count"] == 2
    assert row["total_tokens"] == 120


def test_unbound_device_events_not_attributed_to_any_user(monkeypatch, tmp_path):
    """A NULL user_id on a device with no owner has no user to fall back to."""
    db = tmp_path / "unbound.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    owner = create_user(db, "bound", "Bound")
    create_device(db, "BTF:R4:BOUND", 30, user_id=owner["id"])
    create_device(db, "BTF:R4:UNBOUND", 30)  # no owner
    record_usage(
        db, "BTF:R4:BOUND", "/v1/chat/completions", 200, total_tokens=5, user_id=owner["id"]
    )
    record_usage(
        db, "BTF:R4:UNBOUND", "/v1/chat/completions", 200, total_tokens=999, user_id=None
    )

    as_admin(monkeypatch)
    client = TestClient(main.app)
    row = usage_row_for(client, owner["id"])
    # The unbound device's NULL-owner event must not inflate the bound owner.
    assert row["request_count"] == 1
    assert row["total_tokens"] == 5


# --- 3. agreement with /api/log/self --------------------------------------


def test_usage_view_matches_log_self_for_multi_device_user(monkeypatch, tmp_path):
    db = tmp_path / "agree.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    owner = create_user(db, "agree", "Agree")
    for i in range(3):
        create_device(db, f"BTF:R4:AGREE:{i}", 30, user_id=owner["id"])
    for _ in range(3):
        record_usage(
            db,
            "BTF:R4:AGREE:0",
            "/v1/chat/completions",
            200,
            prompt_tokens=20,
            completion_tokens=40,
            total_tokens=60,
            user_id=owner["id"],
        )
    # Legacy NULL-owner rows as well.
    record_usage(
        db,
        "BTF:R4:AGREE:1",
        "/v1/chat/completions",
        200,
        total_tokens=25,
        user_id=owner["id"],
    )
    with connect(db) as conn:
        conn.execute(
            "update usage_events set user_id = null "
            "where device_id = 'BTF:R4:AGREE:1'"
        )

    as_admin(monkeypatch)
    admin_client = TestClient(main.app)
    row = usage_row_for(admin_client, owner["id"])

    as_customer(monkeypatch, owner["id"], owner["username"])
    customer_client = TestClient(main.app)
    logs = customer_client.get("/api/log/self", params={"page_size": 50}).json()["data"]

    assert row["request_count"] == logs["total"] == 4
    assert row["total_tokens"] == sum(item["quota"] for item in logs["items"]) == 205
