from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    public_base_url: str
    new_api_base_url: str
    new_api_token: str
    internal_admin_token: str
    admin_username: str
    admin_password: str
    payment_instructions: str
    demo_payment_enabled: bool
    payment_notify_url: str
    mock_payment_secret: str
    wechat_app_id: str
    wechat_mch_id: str
    wechat_serial_no: str
    wechat_platform_serial_no: str
    wechat_private_key: str
    wechat_api_v3_key: str
    wechat_platform_public_key: str
    wechat_api_base_url: str
    alipay_app_id: str
    alipay_private_key: str
    alipay_public_key: str
    alipay_gateway: str


def env_pem(name: str) -> str:
    return os.getenv(name, "").replace("\\n", "\n").strip()


def load_settings() -> Settings:
    data_dir = Path(os.getenv("BTF_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    internal_admin_token = os.getenv("INTERNAL_ADMIN_TOKEN", secrets.token_urlsafe(32))
    return Settings(
        data_dir=data_dir,
        database_path=data_dir / "btf-device-gateway.sqlite3",
        public_base_url=os.getenv("PUBLIC_BASE_URL", "http://115.191.10.45").rstrip("/"),
        new_api_base_url=os.getenv("NEW_API_BASE_URL", "http://127.0.0.1:3000").rstrip("/"),
        new_api_token=os.getenv("NEW_API_TOKEN", ""),
        internal_admin_token=internal_admin_token,
        admin_username=os.getenv("BTF_ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("BTF_ADMIN_PASSWORD", internal_admin_token),
        payment_instructions=os.getenv(
            "PAYMENT_INSTRUCTIONS",
            "请联系销售确认付款，订单确认后设备授权会自动延期。",
        ),
        demo_payment_enabled=os.getenv("DEMO_PAYMENT_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
        payment_notify_url=os.getenv("PAYMENT_NOTIFY_URL", "").rstrip("/"),
        mock_payment_secret=os.getenv("MOCK_PAYMENT_SECRET", ""),
        wechat_app_id=os.getenv("WECHAT_APP_ID", ""),
        wechat_mch_id=os.getenv("WECHAT_MCH_ID", ""),
        wechat_serial_no=os.getenv("WECHAT_SERIAL_NO", ""),
        wechat_platform_serial_no=os.getenv("WECHAT_PLATFORM_SERIAL_NO", ""),
        wechat_private_key=env_pem("WECHAT_PRIVATE_KEY"),
        wechat_api_v3_key=os.getenv("WECHAT_API_V3_KEY", ""),
        wechat_platform_public_key=env_pem("WECHAT_PLATFORM_PUBLIC_KEY"),
        wechat_api_base_url=os.getenv("WECHAT_API_BASE_URL", "https://api.mch.weixin.qq.com").rstrip("/"),
        alipay_app_id=os.getenv("ALIPAY_APP_ID", ""),
        alipay_private_key=env_pem("ALIPAY_PRIVATE_KEY"),
        alipay_public_key=env_pem("ALIPAY_PUBLIC_KEY"),
        alipay_gateway=os.getenv("ALIPAY_GATEWAY", "https://openapi.alipay.com/gateway.do"),
    )
