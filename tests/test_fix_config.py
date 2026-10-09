from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import config

ROOT = Path(__file__).resolve().parents[1]


def _fresh_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("BTF_DATA_DIR", str(tmp_path))
    return config.load_settings()


def test_empty_env_vars_fall_back_to_defaults(monkeypatch, tmp_path):
    """An empty env var must behave as unset, not override the default."""
    expected = {
        "PUBLIC_BASE_URL": ("public_base_url", config.DEFAULT_PUBLIC_BASE_URL),
        "NEW_API_BASE_URL": ("new_api_base_url", "http://127.0.0.1:3000"),
        "NEW_API_TOKEN": ("new_api_token", ""),
        "MOCK_PAYMENT_SECRET": ("mock_payment_secret", ""),
        "WECHAT_API_BASE_URL": ("wechat_api_base_url", "https://api.mch.weixin.qq.com"),
        "ALIPAY_GATEWAY": ("alipay_gateway", "https://openapi.alipay.com/gateway.do"),
    }
    for env_name, (field, want) in expected.items():
        monkeypatch.setenv(env_name, "")
        settings = _fresh_settings(monkeypatch, tmp_path)
        assert getattr(settings, field) == want, f"{env_name} empty did not fall back"


def test_whitespace_only_env_vars_fall_back_to_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLIC_BASE_URL", "   ")
    monkeypatch.setenv("NEW_API_BASE_URL", "  ")
    settings = _fresh_settings(monkeypatch, tmp_path)
    assert settings.public_base_url == config.DEFAULT_PUBLIC_BASE_URL
    assert settings.new_api_base_url == "http://127.0.0.1:3000"


def test_non_empty_env_var_overrides_default(monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com/")
    settings = _fresh_settings(monkeypatch, tmp_path)
    assert settings.public_base_url == "https://api.example.com"


def test_public_base_url_default_is_safe_local_placeholder(monkeypatch, tmp_path):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    settings = _fresh_settings(monkeypatch, tmp_path)
    assert config.DEFAULT_PUBLIC_BASE_URL == "http://127.0.0.1:8080"
    assert settings.public_base_url == "http://127.0.0.1:8080"


def test_dead_config_fields_are_removed():
    fields = set(config.Settings.__dataclass_fields__)
    for dead in (
        "internal_admin_token",
        "admin_username",
        "admin_password",
        "payment_instructions",
    ):
        assert dead not in fields, f"dead field {dead!r} still present"


def test_env_str_helper_semantics(monkeypatch):
    monkeypatch.delenv("BTF_TEST_TOKEN", raising=False)
    assert config.env_str("BTF_TEST_TOKEN", "fallback") == "fallback"
    monkeypatch.setenv("BTF_TEST_TOKEN", "")
    assert config.env_str("BTF_TEST_TOKEN", "fallback") == "fallback"
    monkeypatch.setenv("BTF_TEST_TOKEN", "   ")
    assert config.env_str("BTF_TEST_TOKEN", "fallback") == "fallback"
    monkeypatch.setenv("BTF_TEST_TOKEN", " value ")
    assert config.env_str("BTF_TEST_TOKEN", "fallback") == "value"


def test_env_pem_empty_is_empty_and_unescapes_newlines(monkeypatch):
    monkeypatch.setenv("BTF_TEST_PEM", "")
    assert config.env_pem("BTF_TEST_PEM") == ""
    monkeypatch.setenv("BTF_TEST_PEM", "line1\\nline2")
    assert config.env_pem("BTF_TEST_PEM") == "line1\nline2"


def test_compose_has_no_valueless_env_substitution():
    """A bare `${VAR}` makes Compose inject an empty string and clobber defaults."""
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    bare = re.findall(r"\$\{[A-Z0-9_]+\}", text)
    assert not bare, f"docker-compose.yml has defaultless substitutions: {bare}"


def test_compose_and_env_example_drop_dead_config():
    dead_keys = (
        "INTERNAL_ADMIN_TOKEN",
        "BTF_ADMIN_USERNAME",
        "BTF_ADMIN_PASSWORD",
        "PAYMENT_INSTRUCTIONS",
    )
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in dead_keys:
        assert key not in compose, f"{key} still referenced in docker-compose.yml"
        assert key not in env_example, f"{key} still referenced in .env.example"
