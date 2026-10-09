from __future__ import annotations

import asyncio
import base64
import json
import time
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.db import init_db
from app.payments import (
    AlipayProvider,
    MockPaymentProvider,
    PaymentIntent,
    PaymentNotification,
    PaymentSignatureError,
    WechatPayProvider,
    _alipay_signing_string,
    _rsa_sign,
)
from app.service import (
    InvalidState,
    apply_payment_notification,
    create_device,
    create_order,
    get_device_by_key,
    list_payment_transactions,
    save_payment_intent,
)


def test_mock_payment_callback_approves_order_once(tmp_path):
    db = tmp_path / "payments.sqlite3"
    init_db(db)
    key, device = create_device(db, "BTF-PAY-001", 1)
    order = create_order(db, key, "day_1")
    provider = MockPaymentProvider("test-secret")
    intent = asyncio.run(provider.create_payment(order, "https://example.test/notify"))
    transaction = save_payment_intent(db, order["id"], intent, "idempotency-1")

    payload = {
        "order_id": order["id"],
        "provider_order_id": transaction["provider_order_id"],
        "status": "paid",
        "amount": order["amount"],
        "currency": order["currency"],
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    notification = provider.parse_notification(
        body,
        {"x-btf-signature": provider.sign_callback("test-secret", payload)},
    )
    approved = apply_payment_notification(db, notification)
    expires_after_first_callback = get_device_by_key(db, key)["expires_at"]

    assert approved["status"] == "approved"
    assert expires_after_first_callback >= device["expires_at"] + 86400

    approved_again = apply_payment_notification(db, notification)
    expires_after_second_callback = get_device_by_key(db, key)["expires_at"]
    assert approved_again["status"] == "approved"
    assert expires_after_second_callback == expires_after_first_callback


def test_mock_payment_rejects_bad_signature_and_amount(tmp_path):
    db = tmp_path / "payments-invalid.sqlite3"
    init_db(db)
    key, _ = create_device(db, "BTF-PAY-002", 1)
    order = create_order(db, key, "day_1")
    provider = MockPaymentProvider("test-secret")
    intent = asyncio.run(provider.create_payment(order, "https://example.test/notify"))
    save_payment_intent(db, order["id"], intent)

    payload = {
        "order_id": order["id"],
        "provider_order_id": intent.provider_order_id,
        "status": "paid",
        "amount": order["amount"],
        "currency": order["currency"],
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    with pytest.raises(PaymentSignatureError):
        provider.parse_notification(body, {"x-btf-signature": "bad"})

    payload["amount"] = 0.01
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    notification = provider.parse_notification(
        body,
        {"x-btf-signature": provider.sign_callback("test-secret", payload)},
    )
    with pytest.raises(InvalidState, match="amount"):
        apply_payment_notification(db, notification)


def test_real_provider_uses_merchant_order_and_stores_transaction_id(tmp_path):
    db = tmp_path / "provider-order.sqlite3"
    init_db(db)
    key, _ = create_device(db, "BTF-PAY-REAL-SHAPE", 1)
    order = create_order(db, key, "day_1")
    intent = PaymentIntent(
        provider="wechat",
        order_id=order["id"],
        provider_order_id=order["id"],
        status="created",
        checkout_url="weixin://wxpay/bizpayurl",
        expires_at=None,
    )
    save_payment_intent(db, order["id"], intent)
    notification = PaymentNotification(
        provider="wechat",
        order_id=order["id"],
        provider_order_id="wx-platform-transaction-1",
        status="SUCCESS",
        amount=Decimal("9.99"),
        currency="CNY",
        raw_payload="{}",
    )
    approved = apply_payment_notification(db, notification)
    assert approved["status"] == "approved"
    assert list_payment_transactions(db, order["id"])[0]["provider_transaction_id"] == (
        "wx-platform-transaction-1"
    )


def test_alipay_callback_rsa_verification():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    provider = AlipayProvider(
        SimpleNamespace(
            alipay_app_id="app",
            alipay_private_key=private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode(),
            alipay_public_key=public_key,
            alipay_gateway="https://example.test/gateway",
        )
    )
    fields = {
        "out_trade_no": "btf_ord_alipay",
        "trade_no": "alipay_tx_1",
        "trade_status": "TRADE_SUCCESS",
        "total_amount": "39.99",
        "currency": "CNY",
    }
    fields["sign"] = _rsa_sign(private_key, _alipay_signing_string(fields))
    notification = provider.parse_notification(
        urlencode(fields).encode(),
        {},
    )
    assert notification.order_id == "btf_ord_alipay"
    assert notification.provider_order_id == "alipay_tx_1"
    assert notification.succeeded


def test_wechat_callback_decrypts_and_verifies_signature():
    merchant_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    platform_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    platform_public_key = platform_private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    settings = SimpleNamespace(
        wechat_app_id="wx-app",
        wechat_mch_id="mch",
        wechat_serial_no="serial",
        wechat_platform_serial_no="serial",
        wechat_private_key=merchant_private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        wechat_api_v3_key="a" * 32,
        wechat_platform_public_key=platform_public_key,
        wechat_api_base_url="https://example.test",
    )
    provider = WechatPayProvider(settings)
    decrypted_payload = {
        "out_trade_no": "btf_ord_wechat",
        "transaction_id": "wx_tx_1",
        "trade_state": "SUCCESS",
        # appid/mchid are now mandatory and must match the configured merchant.
        "appid": "wx-app",
        "mchid": "mch",
        "amount": {"total": 3999, "currency": "CNY"},
    }
    nonce = "123456789012"
    aad = "test-aad"
    ciphertext = AESGCM(b"a" * 32).encrypt(
        nonce.encode(),
        json.dumps(decrypted_payload, separators=(",", ":")).encode(),
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
    timestamp = str(int(time.time()))
    callback_nonce = "callback-nonce"
    message = f"{timestamp}\n{callback_nonce}\n{body.decode()}\n"
    signature = _rsa_sign(platform_private_key, message)
    notification = provider.parse_notification(
        body,
        {
            "wechatpay-timestamp": timestamp,
            "wechatpay-nonce": callback_nonce,
            "wechatpay-signature": signature,
            "wechatpay-serial": "serial",
        },
    )
    assert notification.order_id == "btf_ord_wechat"
    assert notification.amount == Decimal("39.99")
    assert notification.succeeded
