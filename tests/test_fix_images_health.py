"""Access-control + health-check regressions for generated images (P1-11/P2-16).

Covers:
  * ``/device-images/{path}`` refuses unsigned, tampered and expired links, and
    serves only correctly signed, path-bound URLs.
  * Generated filenames carry a >=128-bit random segment (not enumerable).
  * ``/health`` is liveness-only; ``/ready`` probes the DB and returns 503 when
    the database is unreadable or the upstream is down.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.db import init_db, now_ts
from app.service import create_device, create_user, record_usage

PNG = b"\x89PNG\r\n\x1a\n" + b"image-health-test"


def _use_test_database(monkeypatch: pytest.MonkeyPatch, tmp_path, **overrides):
    db = tmp_path / "test.sqlite3"
    image_dir = tmp_path / "generated-images"
    init_db(db)
    overrides.setdefault("public_base_url", "http://testserver")
    overrides.setdefault("new_api_base_url", "")
    monkeypatch.setattr(
        main,
        "settings",
        replace(
            main.settings,
            database_path=db,
            generated_images_dir=image_dir,
            **overrides,
        ),
    )
    return db, image_dir


def _make_image(image_dir, relative_path: str) -> None:
    target = image_dir / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG)


def _signed_params(relative_path: str, *, ttl: int = 300) -> dict[str, object]:
    exp = now_ts() + ttl
    return {"exp": exp, "sig": main.image_url_signature(relative_path, exp)}


# --- filename entropy ------------------------------------------------------

def test_generated_filename_has_high_entropy_segment(monkeypatch, tmp_path):
    _, image_dir = _use_test_database(monkeypatch, tmp_path)
    segments = set()
    for index in range(5):
        relative_path, _, _, _ = main.save_generated_image_bytes(PNG, "image/png", index)
        segment = main.generated_image_data_segment(relative_path)
        assert segment is not None, relative_path
        # 22 URL-safe chars ~= 132 bits; the implementation uses 32 bytes (~256 bits).
        assert len(segment) >= 22
        assert len(segment) * 6 >= 128
        segments.add(segment)
    assert len(segments) == 5  # no collisions / predictable names


def test_public_url_is_signed_and_usable(monkeypatch, tmp_path):
    _, image_dir = _use_test_database(monkeypatch, tmp_path)
    relative_path, _, public_url, _ = main.save_generated_image_bytes(PNG, "image/png", 0)
    assert "/device-images/" in public_url
    assert "exp=" in public_url and "sig=" in public_url

    client = TestClient(main.app)
    response = client.get(public_url)
    assert response.status_code == 200
    assert response.content == PNG

    # The bare (query-stripped) URL must not be readable.
    bare = public_url.split("?", 1)[0]
    assert client.get(bare).status_code == 403


# --- access control --------------------------------------------------------

def test_unsigned_request_is_rejected(monkeypatch, tmp_path):
    _, image_dir = _use_test_database(monkeypatch, tmp_path)
    relative_path = f"20260101/{now_ts()}-{'A' * 43}-0-{'a' * 16}.png"
    _make_image(image_dir, relative_path)

    client = TestClient(main.app)
    response = client.get(f"/device-images/{relative_path}")
    assert response.status_code == 403


def test_wrong_signature_is_rejected(monkeypatch, tmp_path):
    _, image_dir = _use_test_database(monkeypatch, tmp_path)
    relative_path = f"20260101/{now_ts()}-{'A' * 43}-0-{'a' * 16}.png"
    _make_image(image_dir, relative_path)

    params = _signed_params(relative_path)
    params["sig"] = "x" * len(str(params["sig"]))
    client = TestClient(main.app)
    assert client.get(f"/device-images/{relative_path}", params=params).status_code == 403


def test_expired_signature_is_rejected(monkeypatch, tmp_path):
    _, image_dir = _use_test_database(monkeypatch, tmp_path)
    relative_path = f"20260101/{now_ts()}-{'A' * 43}-0-{'a' * 16}.png"
    _make_image(image_dir, relative_path)

    exp = now_ts() - 10
    sig = main.image_url_signature(relative_path, exp)  # correctly signed, expired
    client = TestClient(main.app)
    response = client.get(
        f"/device-images/{relative_path}", params={"exp": exp, "sig": sig}
    )
    assert response.status_code == 403


def test_valid_signature_serves_image(monkeypatch, tmp_path):
    _, image_dir = _use_test_database(monkeypatch, tmp_path)
    relative_path = f"20260101/{now_ts()}-{'B' * 43}-0-{'b' * 16}.png"
    _make_image(image_dir, relative_path)

    client = TestClient(main.app)
    response = client.get(
        f"/device-images/{relative_path}", params=_signed_params(relative_path)
    )
    assert response.status_code == 200
    assert response.content == PNG
    assert "private" in response.headers.get("cache-control", "")


def test_signature_is_bound_to_its_path(monkeypatch, tmp_path):
    _, image_dir = _use_test_database(monkeypatch, tmp_path)
    path_a = f"20260101/{now_ts()}-{'C' * 43}-0-{'c' * 16}.png"
    path_b = f"20260101/{now_ts()}-{'D' * 43}-0-{'d' * 16}.png"
    _make_image(image_dir, path_a)
    _make_image(image_dir, path_b)

    params = _signed_params(path_a)  # signature minted for path A
    client = TestClient(main.app)
    assert (
        client.get(f"/device-images/{path_b}", params=params).status_code == 403
    )


def test_missing_file_is_not_found(monkeypatch, tmp_path):
    _use_test_database(monkeypatch, tmp_path)
    missing = f"20260101/{now_ts()}-{'E' * 43}-0-{'e' * 16}.png"
    client = TestClient(main.app)
    assert (
        client.get(
            f"/device-images/{missing}", params=_signed_params(missing)
        ).status_code
        == 404
    )


def _store_image_for_owner(monkeypatch, tmp_path) -> str:
    """Persist one image as a device of a specific user; return its record."""
    db, image_dir = _use_test_database(monkeypatch, tmp_path)
    user = create_user(db, "img-owner", "Image Owner")
    create_device(db, "BTF:IMG:OWNER", 30, user_id=user["id"])
    usage_event_id = record_usage(
        db,
        "BTF:IMG:OWNER",
        "/v1/images/generations",
        200,
        user_id=user["id"],
        plan_id="month_1",
        requested_model="btf-image",
        upstream_model="[b2]gpt-image-2",
    )
    payload = {
        "data": [{"b64_json": base64.b64encode(PNG).decode()}],
    }
    asyncio.run(
        main.persist_image_generation_response(
            json.dumps(payload).encode(),
            "application/json",
            "BTF:IMG:OWNER",
            user["id"],
            "month_1",
            "btf-image",
            "[b2]gpt-image-2",
            json.dumps({"prompt": "red square"}).encode(),
            usage_event_id,
        )
    )

    async def fake_customer(request, authorization):
        return main.DashboardUser(id=user["id"], role=1, username=user["username"])

    monkeypatch.setattr(main, "resolve_dashboard_user", fake_customer)
    client = TestClient(main.app)
    response = client.get("/api/device/images")
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert len(items) == 1
    return items[0]


def test_dashboard_listing_hands_owner_a_valid_resigned_url(monkeypatch, tmp_path):
    record = _store_image_for_owner(monkeypatch, tmp_path)
    public_url = record["public_url"]
    assert "/device-images/" in public_url
    assert "exp=" in public_url and "sig=" in public_url

    # The freshly resigned URL must actually serve the owner's image.
    client = TestClient(main.app)
    response = client.get(public_url)
    assert response.status_code == 200
    assert response.content == PNG


# --- /health and /ready ----------------------------------------------------

def test_health_is_liveness_only_and_db_independent(monkeypatch, tmp_path):
    # Point the DB at an unreadable location; liveness must still report ok.
    monkeypatch.setattr(
        main, "settings", replace(main.settings, database_path=tmp_path / "missing" / "x.sqlite3")
    )
    client = TestClient(main.app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_ready_ok_when_database_readable(monkeypatch, tmp_path):
    _use_test_database(monkeypatch, tmp_path)  # new_api_base_url="" -> no upstream probe
    client = TestClient(main.app)
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["checks"]["database"] is True


def test_ready_returns_503_when_database_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(
        main,
        "settings",
        replace(
            main.settings,
            database_path=tmp_path / "does-not-exist" / "gateway.sqlite3",
            new_api_base_url="",
        ),
    )
    client = TestClient(main.app)
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["checks"]["database"] is False


def test_ready_returns_503_when_upstream_down(monkeypatch, tmp_path):
    _use_test_database(monkeypatch, tmp_path, new_api_base_url="http://upstream:3000")

    class _DownClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            raise httpx.ConnectError("upstream down")

    monkeypatch.setattr(main.httpx, "AsyncClient", _DownClient)

    client = TestClient(main.app)
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["database"] is True
    assert body["checks"]["upstream"] is False


def test_ready_ok_when_upstream_reachable(monkeypatch, tmp_path):
    _use_test_database(monkeypatch, tmp_path, new_api_base_url="http://upstream:3000")

    class _UpClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(main.httpx, "AsyncClient", _UpClient)

    client = TestClient(main.app)
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["checks"] == {"database": True, "upstream": True}
