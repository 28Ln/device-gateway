from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qs, urlencode

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import Settings


class PaymentError(Exception):
    pass


class PaymentNotConfigured(PaymentError):
    pass


class PaymentSignatureError(PaymentError):
    pass


class PaymentProviderError(PaymentError):
    pass


@dataclass(frozen=True)
class PaymentIntent:
    provider: str
    order_id: str
    provider_order_id: str
    status: str
    checkout_url: str | None
    expires_at: int | None


@dataclass(frozen=True)
class PaymentNotification:
    provider: str
    order_id: str
    provider_order_id: str
    status: str
    amount: Decimal
    currency: str
    raw_payload: str

    @property
    def succeeded(self) -> bool:
        return self.status in {
            "paid",
            "success",
            "SUCCESS",
            "TRADE_SUCCESS",
            "TRADE_FINISHED",
        }


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise PaymentProviderError(f"invalid payment amount: {value}") from exc


def _minor_units(value: Any) -> int:
    return int((_decimal(value) * 100).to_integral_value())


def _load_private_key(value: str):
    if not value:
        raise PaymentNotConfigured("payment private key is not configured")
    try:
        return serialization.load_pem_private_key(value.encode(), password=None)
    except Exception as exc:
        raise PaymentNotConfigured("payment private key is invalid") from exc


def _load_public_key(value: str):
    if not value:
        raise PaymentNotConfigured("payment public key is not configured")
    try:
        return serialization.load_pem_public_key(value.encode())
    except Exception as exc:
        raise PaymentNotConfigured("payment public key is invalid") from exc


def _rsa_sign(private_key: Any, message: str) -> str:
    signature = private_key.sign(
        message.encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def _rsa_verify(public_key: Any, message: str, signature: str) -> None:
    try:
        public_key.verify(
            base64.b64decode(signature),
            message.encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except Exception as exc:
        raise PaymentSignatureError("invalid payment signature") from exc


def _alipay_signing_string(params: dict[str, Any]) -> str:
    return "&".join(
        f"{key}={params[key]}"
        for key in sorted(params)
        if params[key] is not None and params[key] != "" and key not in {"sign", "sign_type"}
    )


def _json_response_payload(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise PaymentProviderError(f"payment provider returned invalid JSON: {response.text[:200]}") from exc
    if response.is_error:
        raise PaymentProviderError(f"payment provider HTTP {response.status_code}: {payload}")
    return payload


class MockPaymentProvider:
    name = "mock"

    def __init__(self, secret: str):
        if not secret:
            raise PaymentNotConfigured("MOCK_PAYMENT_SECRET is not configured")
        self.secret = secret

    async def create_payment(
        self,
        order: dict[str, Any],
        notify_url: str,
        return_url: str | None = None,
    ) -> PaymentIntent:
        provider_order_id = f"mock_{order['id']}"
        checkout_url = f"{notify_url.rsplit('/notify', 1)[0]}/complete/{provider_order_id}"
        return PaymentIntent(
            provider=self.name,
            order_id=order["id"],
            provider_order_id=provider_order_id,
            status="created",
            checkout_url=checkout_url,
            expires_at=int(time.time()) + 900,
        )

    def parse_notification(
        self,
        body: bytes,
        headers: dict[str, str],
    ) -> PaymentNotification:
        signature = headers.get("x-btf-signature", "")
        expected = hmac.new(self.secret.encode(), body, sha256).hexdigest()
        if not signature or not hmac.compare_digest(signature, expected):
            raise PaymentSignatureError("invalid mock payment signature")
        try:
            payload = json.loads(body.decode())
            amount = _decimal(payload["amount"])
            return PaymentNotification(
                provider=self.name,
                order_id=str(payload["order_id"]),
                provider_order_id=str(payload["provider_order_id"]),
                status=str(payload.get("status", "paid")),
                amount=amount,
                currency=str(payload.get("currency", "CNY")).upper(),
                raw_payload=body.decode(),
            )
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise PaymentProviderError("invalid mock payment callback") from exc

    @staticmethod
    def sign_callback(secret: str, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        return hmac.new(secret.encode(), body, sha256).hexdigest()


class WechatPayProvider:
    name = "wechat"

    def __init__(self, settings: Settings):
        self.settings = settings
        required = (
            settings.wechat_app_id,
            settings.wechat_mch_id,
            settings.wechat_serial_no,
            settings.wechat_platform_serial_no,
            settings.wechat_private_key,
            settings.wechat_api_v3_key,
            settings.wechat_platform_public_key,
        )
        if not all(required):
            raise PaymentNotConfigured(
                "微信支付未配置完整，需要 WECHAT_APP_ID、WECHAT_MCH_ID、"
                "WECHAT_SERIAL_NO、WECHAT_PLATFORM_SERIAL_NO、"
                "WECHAT_PRIVATE_KEY、WECHAT_API_V3_KEY、WECHAT_PLATFORM_PUBLIC_KEY"
            )
        if len(settings.wechat_api_v3_key.encode()) != 32:
            raise PaymentNotConfigured("WECHAT_API_V3_KEY must be 32 bytes")
        self.private_key = _load_private_key(settings.wechat_private_key)
        self.platform_public_key = _load_public_key(settings.wechat_platform_public_key)

    def _authorization(self, method: str, path: str, body: str) -> str:
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(16)
        message = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body}\n"
        signature = _rsa_sign(self.private_key, message)
        return (
            'WECHATPAY2-SHA256-RSA2048 '
            f'mchid="{self.settings.wechat_mch_id}",'
            f'nonce_str="{nonce}",'
            f'signature="{signature}",'
            f'timestamp="{timestamp}",'
            f'serial_no="{self.settings.wechat_serial_no}"'
        )

    async def create_payment(
        self,
        order: dict[str, Any],
        notify_url: str,
        return_url: str | None = None,
    ) -> PaymentIntent:
        body = json.dumps(
            {
                "appid": self.settings.wechat_app_id,
                "mchid": self.settings.wechat_mch_id,
                "description": f"BTF 设备续费 {order['id']}",
                "out_trade_no": order["id"],
                "notify_url": notify_url,
                "amount": {
                    "total": _minor_units(order["amount"]),
                    "currency": order["currency"],
                },
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
        path = "/v3/pay/transactions/native"
        headers = {
            "Authorization": self._authorization("POST", path, body),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "btf-device-gateway/0.1",
        }
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.settings.wechat_api_base_url}{path}",
                content=body.encode(),
                headers=headers,
            )
        payload = _json_response_payload(response)
        code_url = payload.get("code_url")
        if not code_url:
            raise PaymentProviderError(f"微信支付未返回 code_url: {payload}")
        return PaymentIntent(
            provider=self.name,
            order_id=order["id"],
            provider_order_id=order["id"],
            status="created",
            checkout_url=str(code_url),
            expires_at=int(time.time()) + 900,
        )

    def parse_notification(
        self,
        body: bytes,
        headers: dict[str, str],
    ) -> PaymentNotification:
        timestamp = headers.get("wechatpay-timestamp", "")
        nonce = headers.get("wechatpay-nonce", "")
        signature = headers.get("wechatpay-signature", "")
        serial = headers.get("wechatpay-serial", "")
        if not timestamp or not nonce or not signature:
            raise PaymentSignatureError("微信支付回调缺少签名头")
        if serial and serial != self.settings.wechat_platform_serial_no:
            raise PaymentSignatureError("微信支付平台证书序列号不匹配")
        message = f"{timestamp}\n{nonce}\n{body.decode()}\n"
        _rsa_verify(self.platform_public_key, message, signature)
        try:
            envelope = json.loads(body.decode())
            resource = envelope["resource"]
            ciphertext = base64.b64decode(resource["ciphertext"])
            decrypted = AESGCM(self.settings.wechat_api_v3_key.encode()).decrypt(
                resource["nonce"].encode(),
                ciphertext,
                resource["associated_data"].encode(),
            )
            payload = json.loads(decrypted.decode())
            amount = _decimal(Decimal(payload["amount"]["total"]) / Decimal(100))
            return PaymentNotification(
                provider=self.name,
                order_id=str(payload["out_trade_no"]),
                provider_order_id=str(payload.get("transaction_id") or payload["out_trade_no"]),
                status=str(payload.get("trade_state", "SUCCESS")),
                amount=amount,
                currency=str(payload["amount"].get("currency", "CNY")).upper(),
                raw_payload=decrypted.decode(),
            )
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise PaymentProviderError("微信支付回调内容无效") from exc


class AlipayProvider:
    name = "alipay"

    def __init__(self, settings: Settings):
        self.settings = settings
        if not settings.alipay_app_id or not settings.alipay_private_key or not settings.alipay_public_key:
            raise PaymentNotConfigured(
                "支付宝未配置完整，需要 ALIPAY_APP_ID、ALIPAY_PRIVATE_KEY、ALIPAY_PUBLIC_KEY"
            )
        self.private_key = _load_private_key(settings.alipay_private_key)
        self.public_key = _load_public_key(settings.alipay_public_key)

    async def create_payment(
        self,
        order: dict[str, Any],
        notify_url: str,
        return_url: str | None = None,
    ) -> PaymentIntent:
        params: dict[str, Any] = {
            "app_id": self.settings.alipay_app_id,
            "method": "alipay.trade.precreate",
            "format": "JSON",
            "charset": "utf-8",
            "sign_type": "RSA2",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": "1.0",
            "notify_url": notify_url,
            "biz_content": json.dumps(
                {
                    "out_trade_no": order["id"],
                    "subject": f"BTF 设备续费 {order['id']}",
                    "total_amount": f"{Decimal(str(order['amount'])):.2f}",
                    "product_code": "FACE_TO_FACE_PAYMENT",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        params["sign"] = _rsa_sign(self.private_key, _alipay_signing_string(params))
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self.settings.alipay_gateway,
                data=urlencode(params).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        payload = _json_response_payload(response)
        result = payload.get("alipay_trade_precreate_response", {})
        if result.get("code") != "10000" or not result.get("qr_code"):
            raise PaymentProviderError(f"支付宝预创建失败: {payload}")
        return PaymentIntent(
            provider=self.name,
            order_id=order["id"],
            provider_order_id=order["id"],
            status="created",
            checkout_url=str(result["qr_code"]),
            expires_at=int(time.time()) + 900,
        )

    def parse_notification(
        self,
        body: bytes,
        headers: dict[str, str],
    ) -> PaymentNotification:
        raw = {
            key: values[-1]
            for key, values in parse_qs(body.decode(), keep_blank_values=True).items()
        }
        signature = raw.pop("sign", "")
        raw.pop("sign_type", None)
        if not signature:
            raise PaymentSignatureError("支付宝回调缺少 sign")
        _rsa_verify(self.public_key, _alipay_signing_string(raw), signature)
        status = raw.get("trade_status", "")
        if status not in {"TRADE_SUCCESS", "TRADE_FINISHED"}:
            raise PaymentProviderError(f"支付宝订单未支付成功: {status}")
        return PaymentNotification(
            provider=self.name,
            order_id=raw["out_trade_no"],
            provider_order_id=raw.get("trade_no", raw["out_trade_no"]),
            status=status,
            amount=_decimal(raw["total_amount"]),
            currency=raw.get("currency", "CNY").upper(),
            raw_payload=json.dumps(raw, ensure_ascii=False, sort_keys=True),
        )


def configured_provider_names(settings: Settings) -> list[str]:
    names: list[str] = []
    if settings.demo_payment_enabled and settings.mock_payment_secret:
        names.append("mock")
    try:
        WechatPayProvider(settings)
    except PaymentNotConfigured:
        pass
    else:
        names.append("wechat")
    try:
        AlipayProvider(settings)
    except PaymentNotConfigured:
        pass
    else:
        names.append("alipay")
    return names


def get_payment_provider(name: str, settings: Settings):
    normalized = name.strip().lower()
    if normalized == "mock" and settings.demo_payment_enabled:
        return MockPaymentProvider(settings.mock_payment_secret)
    if normalized == "wechat":
        return WechatPayProvider(settings)
    if normalized == "alipay":
        return AlipayProvider(settings)
    raise PaymentNotConfigured(f"支付渠道未配置或不可用: {name}")
