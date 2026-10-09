from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

import app.main as main
from app.db import init_db
from app.payments import (
    PaymentIntent,
    PaymentProviderError,
    PaymentSignatureError,
    WechatPayProvider,
    _rsa_sign,
)
from app.service import (
    InvalidState,
    amounts_match,
    apply_payment_notification,
    approve_order,
    close_order,
    create_device,
    create_order,
    get_device_by_key,
    money_to_minor,
    save_payment_intent,
)


def _mock_order(tmp_path, device_id: str, plan_id: str = "day_1"):
    db = tmp_path / f"{device_id}.sqlite3"
    init_db(db)
    from app.payments import MockPaymentProvider

    key, device = create_device(db, device_id, 1)
    order = create_order(db, key, plan_id)
    provider = MockPaymentProvider("test-secret")
    intent = asyncio.run(provider.create_payment(order, "https://example.test/notify"))
    transaction = save_payment_intent(db, order["id"], intent)
    return db, key, device, order, provider, transaction


def _mock_notification(provider, order, transaction):
    payload = {
        "order_id": order["id"],
        "provider_order_id": transaction["provider_order_id"],
        "status": "paid",
        "amount": order["amount"],
        "currency": order["currency"],
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return provider.parse_notification(
        body,
        {"x-btf-signature": provider.sign_callback("test-secret", payload)},
    )


def test_concurrent_approval_extends_device_once(tmp_path):
    db = tmp_path / "concurrent.sqlite3"
    init_db(db)
    key, device = create_device(db, "BTF-CONC-001", 1)
    order = create_order(db, key, "month_1")

    results: list[dict] = []
    start = threading.Barrier(6)

    def worker():
        start.wait()
        results.append(approve_order(db, order["id"]))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(item["status"] == "approved" for item in results)
    # Exactly one 30-day extension, never six.
    final_expires = get_device_by_key(db, key)["expires_at"]
    assert final_expires - device["expires_at"] == 30 * 86400


def test_approve_order_is_idempotent_sequentially(tmp_path):
    db = tmp_path / "idempotent.sqlite3"
    init_db(db)
    key, device = create_device(db, "BTF-IDEM-001", 1)
    order = create_order(db, key, "day_3")

    first = approve_order(db, order["id"])
    expires_once = get_device_by_key(db, key)["expires_at"]
    second = approve_order(db, order["id"])
    expires_twice = get_device_by_key(db, key)["expires_at"]

    assert first["status"] == second["status"] == "approved"
    assert expires_once - device["expires_at"] == 3 * 86400
    assert expires_twice == expires_once


def test_payment_after_close_recovers_without_asset_loss(tmp_path):
    db, key, device, order, provider, transaction = _mock_order(tmp_path, "BTF-RECOVER-001")
    closed = close_order(db, order["id"], "customer_cancelled")
    assert closed["status"] == "closed"

    # The provider captured the money after the order left the checkout window.
    # Settlement must re-open and fulfil instead of raising (which would strand
    # the captured funds forever).
    notification = _mock_notification(provider, order, transaction)
    settled = apply_payment_notification(db, notification)
    assert settled["status"] == "approved"
    assert get_device_by_key(db, key)["expires_at"] == device["expires_at"] + 86400

    # A retry is a no-op, not a second extension.
    again = apply_payment_notification(db, notification)
    assert again["status"] == "approved"
    assert get_device_by_key(db, key)["expires_at"] == device["expires_at"] + 86400


def test_close_order_refuses_when_payment_captured(tmp_path):
    db, key, device, order, provider, transaction = _mock_order(tmp_path, "BTF-CLOSE-PAID")
    notification = _mock_notification(provider, order, transaction)
    apply_payment_notification(db, notification)
    # Order is approved already, so closing is still refused.
    with pytest.raises(InvalidState):
        close_order(db, order["id"], "late")


def test_money_comparison_is_decimal_and_exact():
    # Float arithmetic cannot represent 0.1 + 0.2, integer minor units can.
    assert 0.1 + 0.2 != 0.3
    assert money_to_minor(0.1) + money_to_minor(0.2) == money_to_minor(0.3)
    assert amounts_match("9.99", Decimal("9.990"))
    assert amounts_match(Decimal("39.99"), 39.99)
    assert not amounts_match("9.99", "9.98")


def test_amount_mismatch_still_rejected(tmp_path):
    db, key, device, order, provider, transaction = _mock_order(tmp_path, "BTF-AMT-001")
    payload = {
        "order_id": order["id"],
        "provider_order_id": transaction["provider_order_id"],
        "status": "paid",
        "amount": round(order["amount"] + 0.01, 2),
        "currency": order["currency"],
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    notification = provider.parse_notification(
        body,
        {"x-btf-signature": provider.sign_callback("test-secret", payload)},
    )
    with pytest.raises(InvalidState, match="amount"):
        apply_payment_notification(db, notification)


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


def _wechat_callback(provider, platform, payload, *, timestamp=None):
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
    ts = str(timestamp if timestamp is not None else int(time.time()))
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


def test_wechat_missing_trade_state_is_failure():
    provider, platform = _wechat_provider()
    payload = {
        "out_trade_no": "btf_ord_missing_state",
        "transaction_id": "wx_tx_missing",
        "amount": {"total": 999, "currency": "CNY"},
    }
    with pytest.raises(PaymentProviderError, match="trade_state"):
        _wechat_callback(provider, platform, payload)


def test_wechat_stale_timestamp_is_rejected():
    provider, platform = _wechat_provider()
    payload = {
        "out_trade_no": "btf_ord_replay",
        "trade_state": "SUCCESS",
        "appid": "wx-app",
        "mchid": "mch-1",
        "amount": {"total": 999, "currency": "CNY"},
    }
    with pytest.raises(PaymentSignatureError, match="时间戳"):
        _wechat_callback(provider, platform, payload, timestamp=int(time.time()) - 3600)


def test_wechat_appid_mismatch_is_rejected():
    provider, platform = _wechat_provider()
    payload = {
        "out_trade_no": "btf_ord_bad_appid",
        "trade_state": "SUCCESS",
        "appid": "someone-else",
        "mchid": "mch-1",
        "amount": {"total": 999, "currency": "CNY"},
    }
    with pytest.raises(PaymentSignatureError, match="appid"):
        _wechat_callback(provider, platform, payload)


def test_wechat_success_still_parses():
    provider, platform = _wechat_provider()
    payload = {
        "out_trade_no": "btf_ord_ok",
        "transaction_id": "wx_tx_ok",
        "trade_state": "SUCCESS",
        "appid": "wx-app",
        "mchid": "mch-1",
        "amount": {"total": 3999, "currency": "CNY"},
    }
    notification = _wechat_callback(provider, platform, payload)
    assert notification.succeeded
    assert notification.authorized_amount == Decimal("39.99")


def test_read_paths_do_not_force_unconditional_write(tmp_path, monkeypatch):
    db = tmp_path / "read-path.sqlite3"
    init_db(db)
    key, _ = create_device(db, "BTF-READ-001", 1)
    order = create_order(db, key, "month_1", expires_seconds=300)
    import sqlite3

    from app.service import expire_pending_orders, get_order, list_orders

    # Prime the throttle window with a read while the order is still valid.
    assert get_order(db, order["id"])["status"] == "pending"
    conn = sqlite3.connect(db)
    conn.execute("update orders set payment_expires_at = ?", (int(time.time()) - 1,))
    conn.commit()
    conn.close()

    # A read inside the throttle window must not mutate the row...
    assert get_order(db, order["id"])["status"] == "pending"
    # ...but an explicit sweep (used by write paths / scheduled jobs) does.
    expire_pending_orders(db)
    assert get_order(db, order["id"])["status"] == "expired"
    listed = next(item for item in list_orders(db) if item["id"] == order["id"])
    assert listed["status"] == "expired"


def test_mock_complete_requires_device_key(tmp_path, monkeypatch):
    db, key, device, order, provider, transaction = _mock_order(tmp_path, "BTF-MOCK-AUTH")
    monkeypatch.setattr(
        main,
        "settings",
        replace(
            main.settings,
            database_path=db,
            demo_payment_enabled=True,
            mock_payment_secret="test-secret",
        ),
    )
    client = TestClient(main.app)

    anonymous = client.post(f"/api/device/payments/mock/complete/{transaction['provider_order_id']}")
    assert anonymous.status_code == 401

    stranger_key, _ = create_device(db, "BTF-MOCK-STRANGER", 1)
    foreign = client.post(
        f"/api/device/payments/mock/complete/{transaction['provider_order_id']}",
        headers={"Authorization": f"Bearer {stranger_key}"},
    )
    assert foreign.json()["success"] is False

    owner = client.post(
        f"/api/device/payments/mock/complete/{transaction['provider_order_id']}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert owner.json()["success"] is True
    assert owner.json()["data"]["status"] == "approved"


def test_mock_complete_hidden_in_production(tmp_path, monkeypatch):
    db, key, device, order, provider, transaction = _mock_order(tmp_path, "BTF-MOCK-PROD")
    monkeypatch.setattr(
        main,
        "settings",
        replace(
            main.settings,
            database_path=db,
            demo_payment_enabled=False,
            mock_payment_secret="",
        ),
    )
    client = TestClient(main.app)
    response = client.post(
        f"/api/device/payments/mock/complete/{transaction['provider_order_id']}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 404


def test_alipay_sign_type_must_be_rsa2():
    from app.payments import AlipayProvider

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    provider = AlipayProvider(
        SimpleNamespace(
            alipay_app_id="app",
            alipay_seller_id="seller",
            alipay_private_key=private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode(),
            alipay_public_key=public_key,
            alipay_gateway="https://example.test/gateway",
        )
    )
    from app.payments import _alipay_signing_string

    fields = {
        "app_id": "app",
        "seller_id": "seller",
        "out_trade_no": "btf_ord_alipay_sign",
        "trade_no": "alipay_tx_sign",
        "trade_status": "TRADE_SUCCESS",
        "total_amount": "39.99",
        "currency": "CNY",
        "sign_type": "RSA",
    }
    fields["sign"] = _rsa_sign(private_key, _alipay_signing_string(fields))
    with pytest.raises(PaymentSignatureError, match="RSA2"):
        provider.parse_notification(urlencode(fields).encode(), {})


def test_mock_notify_requires_device_key(tmp_path, monkeypatch):
    db, key, device, order, provider, transaction = _mock_order(tmp_path, "BTF-MOCK-NOTIFY")
    monkeypatch.setattr(
        main,
        "settings",
        replace(
            main.settings,
            database_path=db,
            demo_payment_enabled=True,
            mock_payment_secret="test-secret",
        ),
    )
    client = TestClient(main.app)
    payload = {
        "order_id": order["id"],
        "provider_order_id": transaction["provider_order_id"],
        "status": "paid",
        "amount": order["amount"],
        "currency": order["currency"],
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    signed = {"x-btf-signature": provider.sign_callback("test-secret", payload)}

    # Correctly signed but no device key -> rejected (400).
    missing = client.post("/api/device/payments/mock/notify", content=body, headers=signed)
    assert missing.status_code == 400

    # A foreign device key cannot settle someone else's order.
    stranger_key, _ = create_device(db, "BTF-MOCK-NOTIFY-STRANGER", 1)
    foreign = client.post(
        "/api/device/payments/mock/notify",
        content=body,
        headers={**signed, "X-Device-Key": stranger_key},
    )
    assert foreign.status_code == 400

    ok = client.post(
        "/api/device/payments/mock/notify",
        content=body,
        headers={**signed, "X-Device-Key": key},
    )
    assert ok.status_code == 200
    assert get_device_by_key(db, key)["expires_at"] == device["expires_at"] + 86400
