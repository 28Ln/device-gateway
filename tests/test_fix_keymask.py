from __future__ import annotations

import secrets
from dataclasses import replace

from fastapi.testclient import TestClient

import app.main as main
from app.db import connect, init_db
from app.security import constant_time_equals, hash_key, mask_key, verify_bind_code
from app.service import authenticate_device, create_device, get_device


def use_test_database(monkeypatch, db):
    monkeypatch.setattr(main, "settings", replace(main.settings, database_path=db))


def fake_admin(monkeypatch):
    async def resolver(request, authorization):
        return main.DashboardUser(id=1, role=main.ADMIN_ROLE, username="admin")

    monkeypatch.setattr(main, "resolve_dashboard_user", resolver)


# --- 1) key_mask must never contain the usable plaintext key ----------------

def test_create_device_stores_masked_key_not_plaintext(tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)

    key, device = create_device(db, "BTF-KEYMASK-001", 30)

    stored = get_device(db, "BTF-KEYMASK-001")
    assert stored["key_mask"] == mask_key(key)
    assert stored["key_mask"] != key
    assert key not in stored["key_mask"]
    # device_id is an identifier and may still be shown verbatim.
    assert stored["device_id"] == "BTF-KEYMASK-001"


def test_mask_key_shape_hides_middle(tmp_path):
    masked = mask_key("abcdefghijklmnopwxyz")
    assert masked == "abcd****wxyz"
    assert "efghijklmnop" not in masked
    # short secrets collapse entirely rather than leaking.
    assert mask_key("short") == "****"


def test_rotate_device_key_masks_new_key(tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    create_device(db, "BTF-KEYMASK-ROT", 30)

    from app.service import rotate_device_key

    new_key, device = rotate_device_key(db, "BTF-KEYMASK-ROT")
    assert device["key_mask"] == mask_key(new_key)
    assert new_key not in device["key_mask"]


def test_http_create_device_masks_key(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    fake_admin(monkeypatch)
    client = TestClient(main.app)

    response = client.post(
        "/api/device/",
        json={"mac": "BTF-KEYMASK-HTTP", "expires_at": 4102444800},
    )
    assert response.status_code == 200
    assert response.json()["success"] is True

    with connect(db) as conn:
        row = conn.execute(
            "select key_hash, key_mask from devices where device_id = ?",
            ("BTF-KEYMASK-HTTP",),
        ).fetchone()
    assert row["key_mask"] != "BTF-KEYMASK-HTTP"
    assert "BTF-KEYMASK-HTTP" not in row["key_mask"]
    assert row["key_hash"] == hash_key("BTF-KEYMASK-HTTP")


# --- 2) constant-time comparison ---------------------------------------------

def test_constant_time_equals_matches_and_rejects():
    assert constant_time_equals("abc", "abc") is True
    assert constant_time_equals("abc", "abd") is False
    assert constant_time_equals("abc", "abcd") is False
    assert constant_time_equals(None, "abc") is False


def test_verify_bind_code_helper():
    stored = hash_key("ABCD1234")
    assert verify_bind_code(stored, "ABCD1234") is True
    assert verify_bind_code(stored, "WRONG") is False
    assert verify_bind_code(None, "ABCD1234") is False


def test_bind_endpoint_uses_constant_time_compare(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    create_device(db, "BTF-KEYMASK-BIND", 1)
    with connect(db) as conn:
        conn.execute(
            """
            update devices
            set user_id = null, bind_code_hash = ?, bind_code_mask = '****1234', bound_at = null
            where device_id = ?
            """,
            (hash_key("ABCD1234"), "BTF-KEYMASK-BIND"),
        )

    calls = {"n": 0}
    real = secrets.compare_digest

    def spy(left, right):
        calls["n"] += 1
        return real(left, right)

    monkeypatch.setattr(secrets, "compare_digest", spy)

    async def fake_dashboard_user(request, authorization):
        return main.DashboardUser(id=7, role=1, username="customer")

    monkeypatch.setattr(main, "resolve_dashboard_user", fake_dashboard_user)
    client = TestClient(main.app)

    ok = client.post(
        "/api/device/bind",
        json={"mac": "BTF-KEYMASK-BIND", "bind_code": "abcd1234"},
    )
    assert ok.status_code == 200
    assert ok.json()["success"] is True
    assert calls["n"] >= 1

    bad = client.post(
        "/api/device/bind",
        json={"mac": "BTF-KEYMASK-BIND", "bind_code": "deadbeef"},
    )
    assert bad.json()["success"] is False
    assert bad.json()["message"] == "invalid binding code"
    assert calls["n"] >= 2


# --- 3) default behaviour preserved, explicit key is opt-in ------------------

def test_default_key_still_matches_device_id(tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)

    key, device = create_device(db, "BTF-KEYMASK-DEFAULT", 30)
    assert key == "BTF-KEYMASK-DEFAULT"
    assert authenticate_device(db, key).device_id == "BTF-KEYMASK-DEFAULT"
    assert device["key_mask"] == mask_key("BTF-KEYMASK-DEFAULT")


def test_explicit_device_key_decouples_from_device_id(tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)

    secret = "btf_dev_" + "A" * 43
    key, device = create_device(db, "BTF-KEYMASK-EXPLICIT", 30, device_key=secret)
    assert key == secret
    assert key != device["device_id"]
    assert device["key_mask"] == mask_key(secret)
    assert secret not in device["key_mask"]
    assert authenticate_device(db, secret).device_id == "BTF-KEYMASK-EXPLICIT"


def test_http_explicit_device_key_returned_masked_in_listing(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    fake_admin(monkeypatch)
    client = TestClient(main.app)

    secret = "btf_dev_" + "B" * 43
    response = client.post(
        "/api/device/",
        json={"mac": "BTF-KEYMASK-HTTP2", "expires_at": 4102444800, "device_key": secret},
    )
    assert response.json()["success"] is True
    assert response.json()["data"]["device_key"] == secret

    with connect(db) as conn:
        row = conn.execute(
            "select key_hash, key_mask from devices where device_id = ?",
            ("BTF-KEYMASK-HTTP2",),
        ).fetchone()
    assert row["key_hash"] == hash_key(secret)
    assert row["key_mask"] == mask_key(secret)
    assert secret not in row["key_mask"]

    # Listing must not echo the full key.
    listing = client.get("/api/device/")
    assert secret not in listing.text


def test_http_default_create_does_not_echo_key(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    fake_admin(monkeypatch)
    client = TestClient(main.app)

    response = client.post(
        "/api/device/",
        json={"mac": "BTF-KEYMASK-HTTP3", "expires_at": 4102444800},
    )
    assert response.json()["data"]["device_key"] == ""
