from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient
from starlette.requests import Request

import app.main as main
from app import config
from app.db import init_db

EVIL_ORIGIN = "https://evil.example"


def trusted_origin() -> str:
    """The effective trusted origin from the loaded settings."""
    return main.settings.allowed_origins[0]


# --- fakes -----------------------------------------------------------------

class FakeUpstreamResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeAsyncClient:
    """Stands in for the httpx client resolve_dashboard_user calls upstream."""

    def __init__(self, role: int = main.ADMIN_ROLE):
        self._role = role
        self.calls: list[tuple[str, str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.calls.append(("GET", url, dict(headers or {})))
        return FakeUpstreamResponse(
            200,
            {"success": True, "data": {"id": 1, "role": self._role, "username": "root"}},
        )

    async def post(self, url, headers=None):
        self.calls.append(("POST", url, dict(headers or {})))
        return FakeUpstreamResponse(200, {"success": True, "data": {"access_token": "tok"}})


def use_test_database(monkeypatch, db):
    monkeypatch.setattr(main, "settings", replace(main.settings, database_path=db))


def patch_upstream(monkeypatch, role: int = main.ADMIN_ROLE) -> FakeAsyncClient:
    client = FakeAsyncClient(role=role)
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: client)
    return client


def make_request(method: str, headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": method,
        "headers": raw,
        "path": "/",
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


# --- config: ALLOWED_ORIGINS derivation ------------------------------------

def test_allowed_origins_defaults_to_public_base_url(monkeypatch, tmp_path):
    monkeypatch.setenv("BTF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    settings = config.load_settings()
    assert settings.allowed_origins == ("https://api.example.com",)


def test_allowed_origins_blank_falls_back_to_same_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("BTF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("ALLOWED_ORIGINS", "   ")
    settings = config.load_settings()
    assert settings.allowed_origins == ("https://api.example.com",)


def test_allowed_origins_parsed_normalized_and_deduped(monkeypatch, tmp_path):
    monkeypatch.setenv("BTF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv(
        "ALLOWED_ORIGINS",
        "https://Admin.Example.com/, https://api.example.com ,https://admin.example.com:443",
    )
    settings = config.load_settings()
    # Host is lower-cased, path/default port dropped and duplicates removed.
    assert settings.allowed_origins == ("https://admin.example.com", "https://api.example.com")


def test_allowed_origins_unrelated_to_removed_admin_token(monkeypatch, tmp_path):
    monkeypatch.setenv("BTF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INTERNAL_ADMIN_TOKEN", "should-be-ignored")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    settings = config.load_settings()
    assert settings.allowed_origins == (config.normalize_origin(config.DEFAULT_PUBLIC_BASE_URL),)
    assert "internal_admin_token" not in config.Settings.__dataclass_fields__


def test_normalize_origin_helper():
    assert config.normalize_origin("https://api.example.com/") == "https://api.example.com"
    assert config.normalize_origin("https://api.example.com:443") == "https://api.example.com"
    assert config.normalize_origin("http://api.example.com:80") == "http://api.example.com"
    assert config.normalize_origin("http://api.example.com:8080") == "http://api.example.com:8080"
    assert config.normalize_origin("api.example.com") == "https://api.example.com"
    assert config.normalize_origin("https://api.example.com/deep/link?x=1") == "https://api.example.com"
    assert config.normalize_origin("") == ""
    assert config.normalize_origin("not a url") == ""
    # A wildcard is a literal host and therefore authorizes nothing.
    assert config.normalize_origin("*") != "*"


# --- guard semantics -------------------------------------------------------

def test_login_required_distinguishes_bearer_from_cookie():
    assert main.login_required("Bearer abc") is False
    assert main.login_required("bearer abc") is False
    assert main.login_required(None) is True
    assert main.login_required("Basic abc") is True


def test_bearer_request_unaffected_by_cross_site_origin():
    request = make_request(
        "POST",
        {"Origin": EVIL_ORIGIN, "Authorization": "Bearer device-key-123"},
    )
    # Bearer callers (device /v1 API, server-to-server) are never blocked.
    assert main.cross_origin_rejection(request) is None
    main.same_origin_guard(request, "Bearer device-key-123")  # must not raise


def test_get_is_never_blocked():
    request = make_request("GET", {"Origin": EVIL_ORIGIN, "Cookie": "session=abc"})
    assert main.cross_origin_rejection(request) is None


def test_cookie_cross_site_origin_is_rejected():
    request = make_request("POST", {"Origin": EVIL_ORIGIN, "Cookie": "session=abc"})
    rejection = main.cross_origin_rejection(request)
    assert rejection is not None and rejection.status_code == 403


def test_cookie_same_origin_is_allowed():
    request = make_request("POST", {"Origin": trusted_origin(), "Cookie": "session=abc"})
    assert main.cross_origin_rejection(request) is None


def test_missing_origin_allows_cookie_request():
    # A plain HTML form cannot set Origin; require explicit rejection only when
    # a foreign Origin/Referer is actually presented.
    request = make_request("POST", {"Cookie": "session=abc"})
    assert main.cross_origin_rejection(request) is None


# --- endpoint integration --------------------------------------------------

def test_cross_site_origin_rejected_on_write_endpoint(monkeypatch, tmp_path):
    db = tmp_path / "csrf.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    patch_upstream(monkeypatch)
    client = TestClient(main.app)

    blocked = client.post(
        "/api/device/",
        json={"mac": "BTF-CSRF-1", "expires_at": 4102444800},
        headers={"Origin": EVIL_ORIGIN, "Cookie": "session=abc"},
    )
    assert blocked.status_code == 403

    blocked_referer = client.post(
        "/api/device/",
        json={"mac": "BTF-CSRF-2", "expires_at": 4102444800},
        headers={"Referer": f"{EVIL_ORIGIN}/dashboard", "Cookie": "session=abc"},
    )
    assert blocked_referer.status_code == 403


def test_same_origin_write_endpoint_succeeds(monkeypatch, tmp_path):
    db = tmp_path / "csrf2.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    patch_upstream(monkeypatch)
    client = TestClient(main.app)

    ok = client.post(
        "/api/device/",
        json={"mac": "BTF-CSRF-OK", "expires_at": 4102444800},
        headers={"Origin": trusted_origin(), "Cookie": "session=abc"},
    )
    assert ok.status_code == 200
    assert ok.json()["success"] is True


def test_bearer_write_endpoint_unaffected(monkeypatch, tmp_path):
    # Same cross-site Origin, but a Bearer credential instead of a cookie, so
    # the request must reach the handler (and not be blocked with 403).
    db = tmp_path / "csrf3.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    patch_upstream(monkeypatch)
    client = TestClient(main.app)

    response = client.post(
        "/api/device/",
        json={"mac": "BTF-CSRF-BEARER", "expires_at": 4102444800},
        headers={"Origin": EVIL_ORIGIN, "Authorization": "Bearer some-token"},
    )
    assert response.status_code != 403


def test_get_endpoint_unaffected_by_cross_site_origin(monkeypatch, tmp_path):
    db = tmp_path / "csrf4.sqlite3"
    init_db(db)
    use_test_database(monkeypatch, db)
    patch_upstream(monkeypatch)
    client = TestClient(main.app)

    response = client.get(
        "/api/device/",
        headers={"Origin": EVIL_ORIGIN, "Cookie": "session=abc"},
    )
    assert response.status_code == 200
