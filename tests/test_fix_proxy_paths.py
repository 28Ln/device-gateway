from __future__ import annotations

from dataclasses import replace

import httpx
from fastapi.testclient import TestClient

import app.main as main
from app.main import (
    IMMUTABLE_CACHE_CONTROL,
    NO_CACHE_CONTROL,
    clean_new_api_proxy_path,
    proxy_response_headers,
    static_cache_control,
)


# --- path sanitization -----------------------------------------------------

def test_new_api_proxy_rejects_traversal_absolute_and_encodings():
    unsafe = [
        "../api/device/plans",
        "%2e%2e/api/device/plans",
        "..%2fapi/device/plans",
        "api/device/../../health",
        "api/%2e%2e/admin",
        "api/%252e%252e/admin",
        "/etc/passwd",
        "//etc/passwd",
        "api\\..\\admin",
        "api/\x00status",
    ]
    for path in unsafe:
        assert clean_new_api_proxy_path(path) is None, path


def test_new_api_proxy_enforces_prefix_allowlist():
    assert clean_new_api_proxy_path("api/status") == "api/status"
    assert clean_new_api_proxy_path("api/user/self") == "api/user/self"
    assert clean_new_api_proxy_path("health") == "health"
    assert clean_new_api_proxy_path("v1/models") == "v1/models"
    # Non-allowlisted / non-forwardable paths (the SPA is served from disk).
    assert clean_new_api_proxy_path("dashboard") is None
    assert clean_new_api_proxy_path("static/js/app.js") is None


def test_route_rejects_traversal_before_proxying():
    client = TestClient(main.app, raise_server_exceptions=False)
    for path in ("/%2e%2e/api/status", "/..%2fapi/status", "/api/%2e%2e/admin"):
        response = client.get(path)
        assert response.status_code == 404, path


# --- static cache policy ---------------------------------------------------

def test_hashed_build_assets_are_cached_immutably():
    assert static_cache_control("assets/index.abc12345.js") == IMMUTABLE_CACHE_CONTROL
    assert static_cache_control("static/js/main.9f8a7b6c.css") == IMMUTABLE_CACHE_CONTROL
    assert "max-age=31536000" in IMMUTABLE_CACHE_CONTROL
    assert "immutable" in IMMUTABLE_CACHE_CONTROL


def test_html_and_unhashed_files_are_revalidated():
    assert static_cache_control("index.html") == NO_CACHE_CONTROL
    assert static_cache_control("") == NO_CACHE_CONTROL
    assert static_cache_control("static/js/app.js") == NO_CACHE_CONTROL
    assert static_cache_control("about") == NO_CACHE_CONTROL
    # A short hex run that is not a content hash must not be treated as one.
    assert static_cache_control("assets/logo.abc.png") == NO_CACHE_CONTROL


# --- multi-value response headers ------------------------------------------

def test_proxy_response_headers_preserve_duplicate_set_cookie():
    upstream = httpx.Headers(
        [
            ("set-cookie", "a=1; Path=/"),
            ("set-cookie", "b=2; Path=/; HttpOnly"),
            ("content-type", "text/plain"),
            ("content-encoding", "gzip"),
            ("content-length", "123"),
        ]
    )
    forwarded = proxy_response_headers(upstream)
    cookies = [value for key, value in forwarded.items() if key.lower() == "set-cookie"]
    assert cookies == ["a=1; Path=/", "b=2; Path=/; HttpOnly"]
    keys = {key.lower() for key, _ in forwarded.items()}
    assert "content-encoding" not in keys
    assert "content-length" not in keys


def test_proxy_route_forwards_multiple_set_cookie_and_clean_url(monkeypatch, tmp_path):
    real_client = httpx.AsyncClient

    class _Transport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.requests: list[httpx.Request] = []

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(
                200,
                headers=[
                    ("set-cookie", "a=1; Path=/"),
                    ("set-cookie", "b=2; Path=/; HttpOnly"),
                    ("content-type", "text/plain"),
                ],
                content=b"ok",
            )

    transport = _Transport()

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(
        main,
        "settings",
        replace(main.settings, new_api_token="test-token", new_api_base_url="http://upstream:3000"),
    )

    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get("/api/status")

    assert response.status_code == 200
    assert response.headers.get_list("set-cookie") == ["a=1; Path=/", "b=2; Path=/; HttpOnly"]
    assert transport.requests[0].url.path == "/api/status"
