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
    payment_instructions: str


def load_settings() -> Settings:
    data_dir = Path(os.getenv("BTF_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=data_dir,
        database_path=data_dir / "btf-device-gateway.sqlite3",
        public_base_url=os.getenv("PUBLIC_BASE_URL", "http://115.191.10.45").rstrip("/"),
        new_api_base_url=os.getenv("NEW_API_BASE_URL", "http://127.0.0.1:3000").rstrip("/"),
        new_api_token=os.getenv("NEW_API_TOKEN", ""),
        internal_admin_token=os.getenv("INTERNAL_ADMIN_TOKEN", secrets.token_urlsafe(32)),
        payment_instructions=os.getenv(
            "PAYMENT_INSTRUCTIONS",
            "请联系销售确认付款，订单确认后设备授权会自动延期。",
        ),
    )

