"""Round-3 robustness fixes: bounds checks so oversized integers cannot 500.

Covers:
  * ``GET /api/log/self?p=<huge>`` must not raise OverflowError (clamped).
  * ``GET /api/device/?user_id=<huge>`` must answer 400, not 500.
  * Malformed numeric env vars must fall back to defaults instead of crashing
    ``load_settings``; ``BTF_IMAGE_URL_TTL_SECONDS`` is clamped to a sane max.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import config
from app.db import init_db
from app.service import create_device, create_user

# Comfortably outside SQLite's signed 64-bit range, so binding it would raise
# OverflowError if it ever reached a query.
HUGE = 10 ** 30


def use_test_database(monkeypatch, db):
    monkeypatch.setattr(main, "settings", replace(main.settings, database_path=db))
    main.DEVICE_AUTH_CACHE.clear()


def as_user(monkeypatch, user_id=1, role=10, username="bounds-user"):
    async def fake_dashboard_user(request, authorization):
        return main.DashboardUser(id=user_id, role=role, username=username)

    monkeypatch.setattr(main, "resolve_dashboard_user", fake_dashboard_user)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db = tmp_path / "bounds.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    user = create_user(db, "bounds-user", "Bounds User")
    create_device(db, "BTF:BOUNDS:A", 30, user_id=user["id"])
    as_user(monkeypatch, user_id=user["id"])
    return TestClient(main.app, raise_server_exceptions=False)


# --- 1. /api/log/self oversized page is clamped, never a 500 -------------


def test_log_self_huge_page_does_not_500(client):
    response = client.get("/api/log/self", params={"p": HUGE})
    assert response.status_code == 200
    page = response.json()["data"]["page"]
    assert page == main.MAX_PAGE
    assert isinstance(page, int)


@pytest.mark.parametrize("p", ["abc", "", "-5", "0", "1e999999"])
def test_log_self_junk_page_does_not_500(client, p):
    response = client.get("/api/log/self", params={"p": p})
    assert response.status_code == 200
    assert 1 <= response.json()["data"]["page"] <= main.MAX_PAGE


def test_log_self_huge_page_size_clamped(client):
    response = client.get("/api/log/self", params={"p": 1, "page_size": HUGE})
    assert response.status_code == 200
    assert response.json()["data"]["page_size"] == main.MAX_PAGE_SIZE


# --- 2. integer query parameters are range-checked (400, not 500) --------


@pytest.mark.parametrize(
    "url",
    [
        "/api/device/?user_id=%d" % HUGE,
        "/api/device/%d" % HUGE,
        "/api/device/orders?limit=%d" % HUGE,
        "/api/device/usage?limit=%d" % HUGE,
        "/api/device/images?limit=%d" % HUGE,
    ],
)
def test_oversized_integer_params_return_400_not_500(client, url):
    response = client.get(url)
    assert response.status_code == 400, url


@pytest.mark.parametrize("value", ["abc", "1.5"])
def test_non_numeric_user_id_returns_400(client, value):
    response = client.get("/api/device/", params={"user_id": value})
    assert response.status_code == 400


def test_empty_user_id_is_treated_as_unset(client):
    response = client.get("/api/device/", params={"user_id": ""})
    assert response.status_code == 200


def test_in_range_user_id_still_works(client):
    response = client.get("/api/device/", params={"user_id": 1})
    assert response.status_code == 200
    assert response.json()["success"] is True


# --- 3. config.py numeric parsing is failure-tolerant --------------------


@pytest.mark.parametrize(
    "env_name, attr, expected",
    [
        ("BTF_CHAT_PROMPT_COST_PER_MILLION", "chat_prompt_cost_per_million", 0.0),
        ("BTF_CHAT_COMPLETION_COST_PER_MILLION", "chat_completion_cost_per_million", 0.0),
        ("BTF_IMAGE_COST_PER_REQUEST", "image_cost_per_request", 0.0),
        (
            "BTF_ORDER_EXPIRES_SECONDS",
            "order_expires_seconds",
            config.DEFAULT_ORDER_EXPIRES_SECONDS,
        ),
        (
            "BTF_IMAGE_URL_TTL_SECONDS",
            "image_url_ttl_seconds",
            config.DEFAULT_IMAGE_URL_TTL_SECONDS,
        ),
    ],
)
def test_malformed_numeric_env_falls_back_to_default(
    monkeypatch, tmp_path, env_name, attr, expected
):
    monkeypatch.setenv("BTF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(env_name, "not-a-number")
    settings = config.load_settings()  # must not raise
    assert getattr(settings, attr) == expected


def test_malformed_numeric_env_does_not_crash_startup(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("BTF_DATA_DIR", str(tmp_path))
    for name in (
        "BTF_CHAT_PROMPT_COST_PER_MILLION",
        "BTF_CHAT_COMPLETION_COST_PER_MILLION",
        "BTF_IMAGE_COST_PER_REQUEST",
        "BTF_ORDER_EXPIRES_SECONDS",
        "BTF_IMAGE_URL_TTL_SECONDS",
    ):
        monkeypatch.setenv(name, "oops")
    with caplog.at_level("WARNING", logger="btf_device_gateway"):
        config.load_settings()
    assert any(record.levelno >= 30 for record in caplog.records)


def test_valid_numeric_env_values_are_preserved(monkeypatch, tmp_path):
    monkeypatch.setenv("BTF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BTF_CHAT_PROMPT_COST_PER_MILLION", "2.5")
    monkeypatch.setenv("BTF_CHAT_COMPLETION_COST_PER_MILLION", "4")
    monkeypatch.setenv("BTF_IMAGE_COST_PER_REQUEST", "0.25")
    monkeypatch.setenv("BTF_ORDER_EXPIRES_SECONDS", "3600")
    monkeypatch.setenv("BTF_IMAGE_URL_TTL_SECONDS", "300")
    settings = config.load_settings()
    assert settings.chat_prompt_cost_per_million == 2.5
    assert settings.chat_completion_cost_per_million == 4.0
    assert settings.image_cost_per_request == 0.25
    assert settings.order_expires_seconds == 3600
    assert settings.image_url_ttl_seconds == 300


def test_image_url_ttl_is_capped_at_upper_bound(monkeypatch, tmp_path):
    monkeypatch.setenv("BTF_DATA_DIR", str(tmp_path))
    # ~10 years is well above the 30-day ceiling and must be clamped down.
    monkeypatch.setenv("BTF_IMAGE_URL_TTL_SECONDS", str(10 * 365 * 24 * 60 * 60))
    settings = config.load_settings()
    assert settings.image_url_ttl_seconds == config.MAX_IMAGE_URL_TTL_SECONDS
    assert config.MAX_IMAGE_URL_TTL_SECONDS <= 30 * 24 * 60 * 60


def test_image_url_ttl_below_minimum_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setenv("BTF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BTF_IMAGE_URL_TTL_SECONDS", "5")
    settings = config.load_settings()
    assert settings.image_url_ttl_seconds == config.DEFAULT_IMAGE_URL_TTL_SECONDS
