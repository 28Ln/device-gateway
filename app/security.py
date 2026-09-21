from __future__ import annotations

import hashlib
import secrets


def generate_device_key() -> str:
    return f"btf_dev_{secrets.token_urlsafe(32)}"


def hash_key(device_key: str) -> str:
    return hashlib.sha256(device_key.encode("utf-8")).hexdigest()


def mask_key(device_key: str) -> str:
    if len(device_key) <= 16:
        return "***"
    return f"{device_key[:10]}...{device_key[-6:]}"

