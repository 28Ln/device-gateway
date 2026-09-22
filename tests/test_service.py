from __future__ import annotations

import time

import pytest

from app.db import init_db
from app.service import (
    InvalidState,
    approve_order,
    approve_order_for_key,
    authenticate_device,
    batch_create_devices,
    create_device,
    create_order,
    disable_device,
    get_device_by_key,
    renew_device,
    rotate_device_key,
)


def test_device_lifecycle(tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)

    key, device = create_device(db, "BTF-UNIT-001", 30)
    assert device["device_id"] == "BTF-UNIT-001"
    assert authenticate_device(db, key).device_id == "BTF-UNIT-001"

    renewed = renew_device(db, "BTF-UNIT-001", 30)
    assert renewed["expires_at"] > device["expires_at"]

    disabled = disable_device(db, "BTF-UNIT-001")
    assert disabled["status"] == "disabled"
    with pytest.raises(InvalidState):
        authenticate_device(db, key)


def test_order_approval_extends_device(tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)

    key, device = create_device(db, "BTF-UNIT-002", 1)
    order = create_order(db, key, "month_1")
    approved = approve_order(db, order["id"])
    assert approved["status"] == "approved"

    auth = authenticate_device(db, key)
    assert auth.expires_at >= device["expires_at"] + 29 * 86400


def test_expired_device_is_rejected(tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)

    key, _ = create_device(db, "BTF-UNIT-003", 1)
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("update devices set expires_at = ?", (int(time.time()) - 1,))
    conn.commit()
    conn.close()

    with pytest.raises(InvalidState):
        authenticate_device(db, key)

    device = get_device_by_key(db, key)
    assert device["device_id"] == "BTF-UNIT-003"

    order = create_order(db, key, "day_1")
    assert order["device_id"] == "BTF-UNIT-003"


def test_rotate_key_invalidates_old_key(tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)

    old_key, _ = create_device(db, "BTF-UNIT-004", 30)
    new_key, device = rotate_device_key(db, "BTF-UNIT-004")
    assert device["device_id"] == "BTF-UNIT-004"
    assert authenticate_device(db, new_key).device_id == "BTF-UNIT-004"
    with pytest.raises(Exception):
        authenticate_device(db, old_key)


def test_batch_create_devices(tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)

    rows = batch_create_devices(db, "BTF-BATCH-", 3, 30)
    assert [device["device_id"] for _, device in rows] == [
        "BTF-BATCH-0001",
        "BTF-BATCH-0002",
        "BTF-BATCH-0003",
    ]


def test_order_must_belong_to_device_key(tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)

    key1, _ = create_device(db, "BTF-ORDER-001", 1)
    key2, _ = create_device(db, "BTF-ORDER-002", 1)
    order = create_order(db, key1, "month_1")
    with pytest.raises(InvalidState):
        approve_order_for_key(db, order["id"], key2)
