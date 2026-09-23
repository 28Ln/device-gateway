from __future__ import annotations

from app.main import clean_relative_path, clean_v1_proxy_path, frontend_file_for_path


def test_static_path_rejects_traversal_and_absolute_paths():
    unsafe_paths = [
        "../.env",
        "%2e%2e/.env",
        "..%2f.env",
        "/etc/passwd",
        "//etc/passwd",
        "static//index.js",
        "static\\..\\.env",
        "static/%2e%2e/.env",
    ]

    for path in unsafe_paths:
        assert clean_relative_path(path) is None
        assert frontend_file_for_path(path) is None


def test_static_path_accepts_dashboard_and_asset_paths():
    assert clean_relative_path("dashboard/overview") == "dashboard/overview"
    assert clean_relative_path("static/js/index.abc123.js") == "static/js/index.abc123.js"


def test_v1_proxy_rejects_non_openai_paths_and_traversal():
    unsafe_paths = [
        "../api/status",
        "%2e%2e/api/status",
        "api/status",
        "/api/status",
        "//api/status",
        "chat/../api/status",
    ]

    for path in unsafe_paths:
        assert clean_v1_proxy_path(path) is None


def test_v1_proxy_accepts_openai_compatible_paths():
    assert clean_v1_proxy_path("models") == "models"
    assert clean_v1_proxy_path("models/deepseek-v4.1-flash") == "models/deepseek-v4.1-flash"
    assert clean_v1_proxy_path("chat/completions") == "chat/completions"
