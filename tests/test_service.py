from __future__ import annotations

import time

import pytest

from app.db import init_db
from app.service import (
    InvalidState,
    approve_order,
    authenticate_device,
    create_device,
    create_order,
    disable_device,
    renew_device,
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

