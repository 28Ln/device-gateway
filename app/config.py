from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("btf_device_gateway")

# Single source of truth for the public base URL default. This is a safe local
# placeholder only; production MUST override it via the PUBLIC_BASE_URL env var.
DEFAULT_PUBLIC_BASE_URL = "http://127.0.0.1:8080"

# How long a signed ``/device-images/...`` link stays valid. Kept short so a
# leaked URL (referer, logs, screenshots) cannot be replayed indefinitely, while
# still covering a browser session. Validated to >= 60s so a typo cannot make
# freshly generated images unreadable, and bounded above by 30 days so a bad
# value cannot mint effectively-permanent signed links.
DEFAULT_IMAGE_URL_TTL_SECONDS = 86400
MIN_IMAGE_URL_TTL_SECONDS = 60
MAX_IMAGE_URL_TTL_SECONDS = 30 * 24 * 60 * 60

# Defaults/limits for the numeric env vars parsed below. Kept as module
# constants so a malformed or out-of-range value can fall back to exactly the
# same number as an unset one. The order-expiry ceiling is derived from a ~100
# year window, which also keeps ``now + ttl`` inside SQLite's signed 64-bit
# integer range so the value can always be bound to a query.
DEFAULT_ORDER_EXPIRES_SECONDS = 86400
MAX_ORDER_EXPIRES_SECONDS = 100 * 365 * 24 * 60 * 60


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
    # Dedicated HMAC secret for short-lived generated-image URLs. Optional: when
    # unset the gateway derives a stable per-installation key from the data
    # directory, so no extra secret is required to run.
    image_url_signing_key: str
    image_url_ttl_seconds: int
    log_level: str
    payment_notify_rate_limit: int
    payment_notify_max_body_bytes: int


# Recognised logging levels. An unknown/blank value falls back to INFO so a typo
# can never silence the gateway's operational logs.
_VALID_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}


def env_log_level(name: str = "BTF_LOG_LEVEL", default: str = "INFO") -> str:
    value = env_str(name, default).upper()
    return value if value in _VALID_LOG_LEVELS else default


def env_positive_int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    """Return a positive integer env var, ignoring unset/blank/non-numeric input.

    Malformed input (non-numeric, too small or, when ``maximum`` is given, too
    large) falls back to ``default`` rather than raising, so a typo can never
    crash startup. Every fallback is logged so operators notice the mistake.
    """
    raw = env_str(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("invalid %s=%r; using default %r", name, raw, default)
        return default
    if value < minimum or (maximum is not None and value > maximum):
        logger.warning("out-of-range %s=%r; using default %r", name, raw, default)
        return default
    return value


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    """Return a non-negative float env var, ignoring unset/blank/non-numeric input.

    A malformed value falls back to ``default`` (and is logged) instead of
    letting ``float()`` raise during ``load_settings`` and crash startup.
    """
    raw = env_str(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("invalid %s=%r; using default %r", name, raw, default)
        return default
    if value != value or value < minimum:  # NaN is unordered; reject it too.
        logger.warning("out-of-range %s=%r; using default %r", name, raw, default)
        return default
    return value


def env_pem(name: str) -> str:
    return env_str(name).replace("\\n", "\n").strip()


def _derived_image_signing_key(data_dir: Path) -> str:
    """Return a stable per-installation secret for image URL signing.

    Used only when ``BTF_IMAGE_URL_SIGNING_KEY`` is not configured. The key is
    generated once with a CSPRNG and persisted (mode 0600) inside the data
    directory, so signatures stay valid across restarts without requiring extra
    operator configuration.
    """
    key_file = data_dir / ".image-url-key"
    try:
        existing = key_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except OSError:
        existing = ""
    if existing:
        return existing
    key = secrets.token_urlsafe(48)
    try:
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(key)
    except OSError:
        # Read-only data dir: fall back to an ephemeral process-local key.
        return key
    return key


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
        chat_prompt_cost_per_million=env_float("BTF_CHAT_PROMPT_COST_PER_MILLION", 0.0),
        chat_completion_cost_per_million=env_float(
            "BTF_CHAT_COMPLETION_COST_PER_MILLION", 0.0
        ),
        image_cost_per_request=env_float("BTF_IMAGE_COST_PER_REQUEST", 0.0),
        order_expires_seconds=env_positive_int(
            "BTF_ORDER_EXPIRES_SECONDS",
            DEFAULT_ORDER_EXPIRES_SECONDS,
            maximum=MAX_ORDER_EXPIRES_SECONDS,
        ),
        image_url_signing_key=env_str("BTF_IMAGE_URL_SIGNING_KEY")
        or _derived_image_signing_key(data_dir),
        image_url_ttl_seconds=min(
            MAX_IMAGE_URL_TTL_SECONDS,
            env_positive_int(
                "BTF_IMAGE_URL_TTL_SECONDS",
                DEFAULT_IMAGE_URL_TTL_SECONDS,
                minimum=MIN_IMAGE_URL_TTL_SECONDS,
            ),
        ),
        log_level=env_log_level(),
        # Process-local callback throttle: N callbacks per window (see main.py).
        payment_notify_rate_limit=env_positive_int("BTF_PAYMENT_NOTIFY_RATE_LIMIT", 60),
        payment_notify_max_body_bytes=env_positive_int(
            "BTF_PAYMENT_NOTIFY_MAX_BODY_BYTES", 256 * 1024
        ),
    )
