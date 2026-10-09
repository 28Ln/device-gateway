from __future__ import annotations

import hashlib
import secrets


def generate_device_key() -> str:
    return f"btf_dev_{secrets.token_urlsafe(32)}"


def hash_key(device_key: str) -> str:
    return hashlib.sha256(device_key.encode("utf-8")).hexdigest()


def mask_secret(value: str, prefix: int = 4, suffix: int = 4) -> str:
    """Return a display-only hint of a secret, never the full value.

    Only the leading ``prefix`` and trailing ``suffix`` characters survive, so
    credentials (device keys, bind codes) can be identified without leaking
    material that could be replayed for authentication.
    """
    value = value or ""
    if len(value) <= prefix + suffix:
        return "****"
    return f"{value[:prefix]}****{value[-suffix:]}"


def mask_key(device_key: str) -> str:
    return mask_secret(device_key)


def constant_time_equals(left: str, right: str) -> bool:
    """Compare two strings without leaking their length/content via timing."""
    if left is None or right is None:
        return False
    return secrets.compare_digest(
        str(left).encode("utf-8"),
        str(right).encode("utf-8"),
    )


def verify_bind_code(stored_hash: str | None, bind_code: str) -> bool:
    """Constant-time check of a candidate bind code against its stored hash."""
    if not stored_hash:
        return False
    return constant_time_equals(stored_hash, hash_key(bind_code))

