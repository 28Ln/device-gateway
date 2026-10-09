"""Round-5 robustness: final input-boundary closure for integer/JSON bodies.

Same failure family as earlier rounds, remaining residue:

  1. Non-finite floats. JSON permits ``1e999`` (-> ``inf``) and, when the
     parser allows it, ``Infinity``/``-Infinity``/``NaN``. ``int(float('inf'))``
     raises ``OverflowError`` and ``int(float('nan'))`` raises ``ValueError``;
     ``int_or_400`` now rejects non-finite floats explicitly and also catches
     ``OverflowError``, so every integer body field answers 400.
  2. Path parameter ``user_id`` on ``PUT /api/device/users/{id}/limits`` was
     bound to SQL without an int64 range check -> ``OverflowError`` (500). It is
     now range-checked like ``api_get_device`` -> 400.
  3. Malformed / non-object JSON bodies (``{``, ``not json``, ``[]``, ``"str"``,
     ``123``, ``null``) surfaced as JSONDecodeError / AttributeError -> 500. A
     shared ``json_body_or_400`` helper turns them into 400 on every write
     endpoint without changing valid-object behaviour.
  4. Renaming a device onto an existing ``device_id`` returned 200 plus the raw
     SQLite error string; it is now a friendly 400.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.db import connect, init_db
from app.service import create_device, create_user

# JSON literals that parse to a non-finite float.
NON_FINITE = ["1e999", "Infinity", "-Infinity", "NaN"]

# Comfortably outside SQLite's signed 64-bit range.
HUGE = 10 ** 30

MALFORMED_BODIES = ["{", "not json", "[]", '"str"', "123", "null", ""]


def use_test_database(monkeypatch: pytest.MonkeyPatch, db: Path) -> None:
    monkeypatch.setattr(main, "settings", replace(main.settings, database_path=db))
    main.DEVICE_AUTH_CACHE.clear()


def as_admin(monkeypatch: pytest.MonkeyPatch, user_id: int = 1) -> None:
    async def fake_dashboard_user(request, authorization):
        return main.DashboardUser(id=user_id, role=main.ADMIN_ROLE, username="root")

    monkeypatch.setattr(main, "resolve_dashboard_user", fake_dashboard_user)


def future_ts() -> int:
    return main.now_ts() + 30 * 86400


def raw_json(client: TestClient, method: str, url: str, body: str):
    return getattr(client, method)(
        url, content=body, headers={"content-type": "application/json"}
    )


@pytest.fixture()
def env(monkeypatch, tmp_path):
    db = tmp_path / "round5-inputs.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    user = create_user(db, "r5-inputs", "Round5 Inputs")
    device = create_device(db, "BTF:R5:INPUTS:A", 30, user_id=user["id"])
    with connect(db) as conn:
        device_id = conn.execute(
            "select id from devices where device_id = 'BTF:R5:INPUTS:A'"
        ).fetchone()["id"]
    as_admin(monkeypatch)
    client = TestClient(main.app, raise_server_exceptions=False)
    return {
        "client": client,
        "db": db,
        "user": user,
        "device_id": device_id,
    }


# --- 1. non-finite floats in integer body fields -> 400 -------------------


def integer_endpoints(env) -> list[tuple[str, str, str]]:
    """(http method, url, integer-field-name) for each write endpoint."""
    uid = env["user"]["id"]
    return [
        ("post", "/api/device/", "expires_at"),
        ("put", "/api/device/", "id"),
        ("post", "/api/device/batch", "expires_at"),
        ("post", "/api/device/manage", "id"),
        ("put", "/api/device/plans/month_1", "token_limit"),
        ("put", f"/api/device/users/{uid}/limits", "token_limit"),
    ]


@pytest.mark.parametrize("literal", NON_FINITE)
def test_non_finite_expires_at_is_400(env, literal):
    for method, url, field in integer_endpoints(env):
        response = raw_json(
            env["client"], method, url, '{"%s": %s}' % (field, literal)
        )
        assert response.status_code == 400, (method, url, field, literal, response.text)


def test_positive_infinity_via_python_float_is_400(env):
    """A Python ``inf`` serialised with ``allow_nan=True``; still a 400."""
    body = json.dumps(
        {"expires_at": float("inf"), "mac": "BTF:R5:INPUTS:B"}, allow_nan=True
    )
    response = raw_json(env["client"], "post", "/api/device/", body)
    assert response.status_code == 400


def test_nan_expires_at_is_400(env):
    body = json.dumps(
        {"expires_at": float("nan"), "mac": "BTF:R5:INPUTS:C"}, allow_nan=True
    )
    response = raw_json(env["client"], "post", "/api/device/", body)
    assert response.status_code == 400


def test_finite_float_body_still_accepted(env):
    """A finite float (whole number) must not be rejected by the new guard."""
    response = env["client"].post(
        "/api/device/",
        json={"mac": "BTF:R5:INPUTS:D", "expires_at": float(future_ts())},
    )
    assert response.status_code == 200
    assert response.json()["success"] is True


# --- 2. oversized path parameter user_id -> 400 ---------------------------


def test_limits_oversized_path_user_id_is_400(env):
    response = env["client"].put(
        f"/api/device/users/{HUGE}/limits", json={"token_limit": 1}
    )
    assert response.status_code == 400


def test_limits_negative_path_user_id_is_400(env):
    response = env["client"].put(
        "/api/device/users/-5/limits", json={"token_limit": 1}
    )
    assert response.status_code == 400


def test_limits_valid_path_user_id_still_works(env):
    response = env["client"].put(
        f"/api/device/users/{env['user']['id']}/limits", json={"token_limit": 7}
    )
    assert response.status_code == 200
    assert response.json()["success"] is True


# --- 3. malformed / non-object JSON bodies -> 400 -------------------------


def malformed_endpoints(env) -> list[tuple[str, str]]:
    uid = env["user"]["id"]
    return [
        ("post", "/api/device/"),
        ("put", "/api/device/"),
        ("post", "/api/device/batch"),
        ("post", "/api/device/bind"),
        ("post", "/api/device/manage"),
        ("put", "/api/device/plans/month_1"),
        ("post", "/api/device/orders/whatever/pay"),
        ("post", "/api/device/orders"),
        ("post", "/api/device/orders/whatever/close"),
        ("post", "/api/device/orders/approve"),
        ("put", f"/api/device/users/{uid}/limits"),
    ]


@pytest.mark.parametrize("body", MALFORMED_BODIES)
def test_malformed_json_body_is_400(env, body):
    for method, url in malformed_endpoints(env):
        response = raw_json(env["client"], method, url, body)
        assert response.status_code == 400, (method, url, repr(body), response.text)


def test_json_list_body_is_400(env):
    response = env["client"].post("/api/device/", json=[1, 2, 3])
    assert response.status_code == 400


def test_valid_object_body_not_broken(env):
    response = env["client"].post(
        "/api/device/bind", json={"mac": "BTF:R5:INPUTS:MISS", "bind_code": "x"}
    )
    assert response.status_code == 200
    assert response.json()["success"] is False  # reached handler, not rejected


# --- 4. rename collision -> friendly 400 ----------------------------------


def test_rename_onto_existing_device_id_is_400(env):
    create_device(env["db"], "BTF:R5:INPUTS:B", 30, user_id=env["user"]["id"])
    response = env["client"].put(
        "/api/device/",
        json={
            "id": env["device_id"],
            "mac": "BTF:R5:INPUTS:B",
            "expires_at": future_ts(),
        },
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    # Friendly message: no raw SQLite / schema leakage.
    for leak in ("UNIQUE", "constraint", "sqlite", "devices.", "key_hash"):
        assert leak.lower() not in detail.lower(), detail


def test_self_update_same_device_id_still_works(env):
    response = env["client"].put(
        "/api/device/",
        json={
            "id": env["device_id"],
            "mac": "BTF:R5:INPUTS:A",
            "expires_at": future_ts(),
        },
    )
    assert response.status_code == 200
    assert response.json()["success"] is True


def test_rename_to_free_device_id_still_works(env):
    response = env["client"].put(
        "/api/device/",
        json={
            "id": env["device_id"],
            "mac": "BTF:R5:INPUTS:FREE",
            "expires_at": future_ts(),
        },
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
