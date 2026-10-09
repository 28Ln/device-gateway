"""Round-3 security hardening tests.

Covers:
  * SSRF: CGNAT 100.64.0.0/10 and cloud metadata 100.100.100.200 rejected.
  * WeChat callback: missing appid/mchid is a hard failure.
  * CSRF: cross-site Sec-Fetch-Site rejected, same-origin/none allowed.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient
from starlette.requests import Request

import app.main as main
from app.db import init_db
from app.payments import PaymentSignatureError, WechatPayProvider, _rsa_sign

EVIL_ORIGIN = "https://evil.example"


# --------------------------------------------------------------------------
# 1) SSRF: special-purpose ranges must not be considered globally routable.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "address",
    [
        "100.64.0.0",
        "100.64.0.1",
        "100.100.100.200",  # Aliyun metadata service
        "100.127.255.255",
        "192.0.0.1",
        "198.18.0.0",
        "198.19.255.255",
        "240.0.0.1",
        "255.255.255.254",
        "0.0.0.0",
        "0.1.2.3",
    ],
)
def test_special_purpose_ranges_are_not_public(address):
    assert main.is_public_ip(address) is False


@pytest.mark.parametrize("address", ["8.8.8.8", "93.184.216.34", "1.1.1.1", "2606:4700::1111"])
def test_global_unicast_addresses_remain_public(address):
    assert main.is_public_ip(address) is True


def test_cgnat_and_metadata_literals_rejected_by_url_guard(monkeypatch):
    for url in ("http://100.100.100.200/latest/meta-data/", "http://100.64.0.1/x.png"):
        with pytest.raises(ValueError, match="non-public"):
            main.validate_image_url(url)


def test_cgnat_hostname_resolution_rejected(monkeypatch):
    monkeypatch.setattr(main, "resolve_host_ips", lambda host: ["100.100.100.200"])
    with pytest.raises(ValueError, match="100.100.100.200"):
        main.validate_image_url("http://metadata.example.com/x.png")


# --------------------------------------------------------------------------
# 2) WeChat callback: appid/mchid must be present and match configuration.
# --------------------------------------------------------------------------

def _wechat_provider():
    merchant = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    platform = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    platform_public = platform.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    settings = SimpleNamespace(
        wechat_app_id="wx-app",
        wechat_mch_id="mch-1",
        wechat_serial_no="merchant-serial",
        wechat_platform_serial_no="platform-serial",
        wechat_private_key=merchant.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        wechat_api_v3_key="a" * 32,
        wechat_platform_public_key=platform_public,
        wechat_api_base_url="https://example.test",
    )
    return WechatPayProvider(settings), platform


def _wechat_callback(provider, platform, payload):
    nonce = "123456789012"
    aad = "test-aad"
    ciphertext = AESGCM(b"a" * 32).encrypt(
        nonce.encode(),
        json.dumps(payload, separators=(",", ":")).encode(),
        aad.encode(),
    )
    envelope = {
        "resource": {
            "algorithm": "AEAD_AES_256_GCM",
            "ciphertext": base64.b64encode(ciphertext).decode(),
            "nonce": nonce,
            "associated_data": aad,
        }
    }
    body = json.dumps(envelope, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    callback_nonce = "callback-nonce"
    message = f"{ts}\n{callback_nonce}\n{body.decode()}\n"
    signature = _rsa_sign(platform, message)
    return provider.parse_notification(
        body,
        {
            "wechatpay-timestamp": ts,
            "wechatpay-nonce": callback_nonce,
            "wechatpay-signature": signature,
            "wechatpay-serial": "platform-serial",
        },
    )


def test_wechat_missing_appid_is_rejected():
    provider, platform = _wechat_provider()
    payload = {
        "out_trade_no": "btf_ord_no_appid",
        "trade_state": "SUCCESS",
        "mchid": "mch-1",
        "amount": {"total": 999, "currency": "CNY"},
    }
    with pytest.raises(PaymentSignatureError, match="appid"):
        _wechat_callback(provider, platform, payload)


def test_wechat_missing_mchid_is_rejected():
    provider, platform = _wechat_provider()
    payload = {
        "out_trade_no": "btf_ord_no_mchid",
        "trade_state": "SUCCESS",
        "appid": "wx-app",
        "amount": {"total": 999, "currency": "CNY"},
    }
    with pytest.raises(PaymentSignatureError, match="mchid"):
        _wechat_callback(provider, platform, payload)


def test_wechat_matching_appid_and_mchid_still_parses():
    provider, platform = _wechat_provider()
    payload = {
        "out_trade_no": "btf_ord_ok",
        "trade_state": "SUCCESS",
        "appid": "wx-app",
        "mchid": "mch-1",
        "amount": {"total": 999, "currency": "CNY"},
    }
    assert _wechat_callback(provider, platform, payload).succeeded


# --------------------------------------------------------------------------
# 3) CSRF: Sec-Fetch-Site closes the Origin-less cross-site gap.
# --------------------------------------------------------------------------

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


def test_cross_site_sec_fetch_site_without_origin_is_rejected():
    request = make_request("POST", {"Cookie": "session=abc", "Sec-Fetch-Site": "cross-site"})
    rejection = main.cross_origin_rejection(request)
    assert rejection is not None and rejection.status_code == 403


def test_same_site_sec_fetch_site_without_origin_is_rejected():
    request = make_request("POST", {"Cookie": "session=abc", "Sec-Fetch-Site": "same-site"})
    rejection = main.cross_origin_rejection(request)
    assert rejection is not None and rejection.status_code == 403


def test_same_origin_sec_fetch_site_is_allowed():
    request = make_request("POST", {"Cookie": "session=abc", "Sec-Fetch-Site": "same-origin"})
    assert main.cross_origin_rejection(request) is None


def test_none_sec_fetch_site_is_allowed():
    request = make_request("POST", {"Cookie": "session=abc", "Sec-Fetch-Site": "none"})
    assert main.cross_origin_rejection(request) is None


def test_absent_signals_allow_server_to_server_call():
    # No Origin/Referer/Sec-Fetch-Site: a genuine server-side cookie call must
    # not be miskilled.
    request = make_request("POST", {"Cookie": "session=abc"})
    assert main.cross_origin_rejection(request) is None


def test_bearer_request_exempt_from_sec_fetch_site():
    request = make_request(
        "POST",
        {"Authorization": "Bearer device-key", "Sec-Fetch-Site": "cross-site"},
    )
    assert main.cross_origin_rejection(request) is None


def test_get_exempt_from_sec_fetch_site():
    request = make_request("GET", {"Cookie": "session=abc", "Sec-Fetch-Site": "cross-site"})
    assert main.cross_origin_rejection(request) is None


def test_sec_fetch_site_cross_site_rejected_on_write_endpoint(monkeypatch, tmp_path):
    db = tmp_path / "round3.sqlite3"
    init_db(db)
    monkeypatch.setattr(main, "settings", replace(main.settings, database_path=db))
    client = TestClient(main.app)
    blocked = client.post(
        "/api/device/",
        json={"mac": "BTF-R3-1", "expires_at": 4102444800},
        headers={"Cookie": "session=abc", "Sec-Fetch-Site": "cross-site"},
    )
    assert blocked.status_code == 403


def test_sec_fetch_site_same_origin_write_endpoint_succeeds(monkeypatch, tmp_path):
    db = tmp_path / "round3b.sqlite3"
    init_db(db)
    monkeypatch.setattr(main, "settings", replace(main.settings, database_path=db))
    client = TestClient(main.app)
    # Passes the CSRF middleware and reaches the auth layer (401 without a
    # stubbed upstream), i.e. it is not blocked with 403.
    ok = client.post(
        "/api/device/",
        json={"mac": "BTF-R3-OK", "expires_at": 4102444800},
        headers={"Cookie": "session=abc", "Sec-Fetch-Site": "same-origin"},
    )
    assert ok.status_code != 403
