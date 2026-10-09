from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

# Single source of truth for the public base URL default. This is a safe local
# placeholder only; production MUST override it via the PUBLIC_BASE_URL env var.
DEFAULT_PUBLIC_BASE_URL = "http://127.0.0.1:8080"


def env_str(name: str, default: str = "") -> str:
    """Return an env var, treating empty/whitespace-only values as unset.

    An empty string must never override a caller-provided default, especially
    for tokens and passwords where a blank value would silently weaken auth.
    """
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def normalize_origin(value: str) -> str:
    """Return a canonical ``scheme://host[:port]`` origin, or "" if unusable.

    The path, query, fragment and userinfo are discarded so that any URL on an
    allowed origin (e.g. a deep-link Referer) maps to the same trusted value.
    A bare host without a scheme is assumed to be HTTPS. Wildcards are *not*
    supported: an entry like ``*`` normalizes to a literal host that never
    matches a real Origin, so it simply never authorizes anything.
    """
    text = (value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"//{text}")
    hostname = parsed.hostname
    if not hostname:
        return ""
    scheme = (parsed.scheme or "https").lower()
    host = hostname.lower()
    # A real origin host never contains whitespace; rejecting it keeps junk
    # entries (and junk Origin headers) from ever matching as a literal host.
    if any(char.isspace() for char in host):
        return ""
    if ":" in host:  # IPv6 literal must keep its brackets in the origin.
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def parse_allowed_origins(raw: str, public_base_url: str) -> tuple[str, ...]:
    """Parse the comma-separated ALLOWED_ORIGINS list.

    Falls back to the single same-origin derived from PUBLIC_BASE_URL when the
    variable is unset or contains only empty/unparseable entries, so the default
    deployment trusts exactly its own origin and nothing wider.
    """
    origins: list[str] = []
    for token in (raw or "").split(","):
        origin = normalize_origin(token)
        if origin and origin not in origins:
            origins.append(origin)
    if not origins:
        derived = normalize_origin(public_base_url)
        if derived:
            origins.append(derived)
    return tuple(origins)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    generated_images_dir: Path
    public_base_url: str
    allowed_origins: tuple[str, ...]
    new_api_base_url: str
    new_api_token: str
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
    alipay_seller_id: str
    alipay_private_key: str
    alipay_public_key: str
    alipay_gateway: str
    chat_prompt_cost_per_million: float
    chat_completion_cost_per_million: float
    image_cost_per_request: float
    order_expires_seconds: int


def env_pem(name: str) -> str:
    return env_str(name).replace("\\n", "\n").strip()


def load_settings() -> Settings:
    data_dir = Path(env_str("BTF_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    public_base_url = env_str("PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).rstrip("/")
    return Settings(
        data_dir=data_dir,
        database_path=data_dir / "btf-device-gateway.sqlite3",
        generated_images_dir=data_dir / "generated-images",
        public_base_url=public_base_url,
        allowed_origins=parse_allowed_origins(env_str("ALLOWED_ORIGINS"), public_base_url),
        new_api_base_url=env_str("NEW_API_BASE_URL", "http://127.0.0.1:3000").rstrip("/"),
        new_api_token=env_str("NEW_API_TOKEN"),
        demo_payment_enabled=env_str("DEMO_PAYMENT_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
        payment_notify_url=env_str("PAYMENT_NOTIFY_URL").rstrip("/"),
        mock_payment_secret=env_str("MOCK_PAYMENT_SECRET"),
        wechat_app_id=env_str("WECHAT_APP_ID"),
        wechat_mch_id=env_str("WECHAT_MCH_ID"),
        wechat_serial_no=env_str("WECHAT_SERIAL_NO"),
        wechat_platform_serial_no=env_str("WECHAT_PLATFORM_SERIAL_NO"),
        wechat_private_key=env_pem("WECHAT_PRIVATE_KEY"),
        wechat_api_v3_key=env_str("WECHAT_API_V3_KEY"),
        wechat_platform_public_key=env_pem("WECHAT_PLATFORM_PUBLIC_KEY"),
        wechat_api_base_url=env_str("WECHAT_API_BASE_URL", "https://api.mch.weixin.qq.com").rstrip("/"),
        alipay_app_id=env_str("ALIPAY_APP_ID"),
        alipay_seller_id=env_str("ALIPAY_SELLER_ID"),
        alipay_private_key=env_pem("ALIPAY_PRIVATE_KEY"),
        alipay_public_key=env_pem("ALIPAY_PUBLIC_KEY"),
        alipay_gateway=env_str("ALIPAY_GATEWAY", "https://openapi.alipay.com/gateway.do"),
        chat_prompt_cost_per_million=float(env_str("BTF_CHAT_PROMPT_COST_PER_MILLION", "0") or 0),
        chat_completion_cost_per_million=float(env_str("BTF_CHAT_COMPLETION_COST_PER_MILLION", "0") or 0),
        image_cost_per_request=float(env_str("BTF_IMAGE_COST_PER_REQUEST", "0") or 0),
        order_expires_seconds=int(env_str("BTF_ORDER_EXPIRES_SECONDS", "86400") or 86400),
    )
