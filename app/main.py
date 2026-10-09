from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from starlette.background import BackgroundTask

from app.config import load_settings
from app.db import DEFAULT_CHAT_MODELS, connect
from app.db import now_ts
from app.service import (
    DeviceError,
    approve_order,
    apply_payment_notification,
    authenticate_device,
    close_order,
    create_order,
    ensure_ready,
    get_order,
    get_device_by_key,
    list_generated_images,
    list_orders,
    list_usage,
    plans,
    record_generated_image,
    record_usage,
    save_payment_intent,
)
from app.security import hash_key, mask_key, verify_bind_code
from app.payments import (
    MockPaymentProvider,
    PaymentError,
    PaymentNotConfigured,
    PaymentProviderError,
    PaymentSignatureError,
    configured_provider_names,
    get_payment_provider,
)

settings = load_settings()
ensure_ready(settings.database_path)
frontend_dist = Path(__file__).resolve().parents[1] / "new-api-dist"
ADMIN_ROLE = 10
OPENAI_V1_PREFIXES = {
    "audio",
    "batches",
    "chat",
    "completions",
    "embeddings",
    "files",
    "fine_tuning",
    "images",
    "models",
    "moderations",
    "responses",
}
# First path segments that the catch-all route is allowed to forward upstream
# to new-api. This is intentionally a superset of the prefixes consumed by the
# more specific routes declared above (``/api/device/...``, ``/api/log/...``)
# plus the new-api endpoints the SPA relies on.
NEW_API_PROXY_PREFIXES = {
    "api",
    "v1",
    "health",
}
SAFE_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~:-]+$")
DEEPSEEK_MODELS = {"deepseek-v4.1-flash"}
WEB_SEARCH_MODELS = {"gpt-5.5"}
DEFAULT_IMAGE_MODELS = {"[b2]gpt-image-2"}
DEFAULT_IMAGE_MODEL_TEXT = "[b2]gpt-image-2"
BTF_CHAT_ALIAS = "btf-chat"
BTF_IMAGE_ALIAS = "btf-image"
ACTIVE_REQUESTS: dict[str, int] = {}
ACTIVE_USER_REQUESTS: dict[int, int] = {}
MAX_GENERATED_IMAGE_BYTES = 25 * 1024 * 1024
# Upper bound for a device/plan expiry (~100 years) so a typo such as an
# unusually large epoch cannot produce a nonsensical "forever" licence.
MAX_EXPIRES_AT = 4_102_444_800
MAX_PAGE_SIZE = 200
MAX_PAGE = 1_000_000
DEVICE_STATUSES = ("active", "disabled", "expired")
# Cache the expensive device authentication so the /v1/models early path does
# not double-verify the key on every request. Entries expire quickly, so device
# disable/expiry still takes effect within the TTL.
DEVICE_AUTH_CACHE: dict[tuple[str, str], tuple[float, "Any"]] = {}
DEVICE_AUTH_CACHE_TTL = 15.0
SSE_USAGE_TAIL_BYTES = 64 * 1024

# Shared upstream client for the device proxy: connection reuse plus split
# connect/read/write/pool timeouts. A single total cap of 120s matches the
# previous behaviour for legacy (non-streaming) responses.
_UPSTREAM_TIMEOUT = httpx.Timeout(120.0, connect=10.0, read=120.0, write=30.0, pool=10.0)
# Dedicated client for SSE: each read may legitimately idle between events,
# so give reads a longer ceiling while still bounding connect/pool.
_STREAM_TIMEOUT = httpx.Timeout(600.0, connect=10.0, read=300.0, write=30.0, pool=10.0)
_upstream_client: httpx.AsyncClient | None = None
_stream_client: httpx.AsyncClient | None = None


def upstream_client() -> httpx.AsyncClient:
    """Return the lazily created shared upstream client.

    The client is created on first use (inside the running event loop) and kept
    for connection reuse across requests. Tests may rebind ``_upstream_client``.
    """
    global _upstream_client
    if _upstream_client is None:
        _upstream_client = httpx.AsyncClient(
            timeout=_UPSTREAM_TIMEOUT,
            follow_redirects=False,
        )
    return _upstream_client


def stream_client() -> httpx.AsyncClient:
    """Return the shared client used for streaming (SSE) upstream calls."""
    global _stream_client
    if _stream_client is None:
        _stream_client = httpx.AsyncClient(
            timeout=_STREAM_TIMEOUT,
            follow_redirects=False,
        )
    return _stream_client


MAX_IMAGE_REDIRECTS = 3
IMAGE_DOWNLOAD_CHUNK_BYTES = 64 * 1024
IMAGE_DOWNLOAD_ALLOWED_SCHEMES = {"http", "https"}
logger = logging.getLogger("btf_device_gateway")


@dataclass(frozen=True)
class DashboardUser:
    id: int
    role: int
    username: str

    @property
    def is_admin(self) -> bool:
        return self.role >= ADMIN_ROLE

app = FastAPI(title="BTF Device Gateway")


def fmt_ts(value: int | None) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def fully_unquote_path(path: str) -> str:
    decoded = path
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def clean_relative_path(path: str) -> str | None:
    decoded = fully_unquote_path(path).replace("\\", "/")
    if not decoded or "\x00" in decoded or decoded.startswith("/") or "//" in decoded:
        return None
    parts = decoded.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    if any(not SAFE_PATH_SEGMENT_RE.match(part) for part in parts):
        return None
    return "/".join(parts)


def frontend_file_for_path(path: str) -> Path | None:
    clean_path = clean_relative_path(path)
    if clean_path is None:
        return None
    dist_root = frontend_dist.resolve()
    candidate = (dist_root / clean_path).resolve()
    try:
        candidate.relative_to(dist_root)
    except ValueError:
        return None
    return candidate


def clean_v1_proxy_path(path: str) -> str | None:
    clean_path = clean_relative_path(path)
    if clean_path is None:
        return None
    first_segment = clean_path.split("/", 1)[0]
    if first_segment not in OPENAI_V1_PREFIXES:
        return None
    return clean_path


def clean_new_api_proxy_path(path: str) -> str | None:
    """Sanitize a path before it is appended to the upstream new-api base URL.

    ``{path:path}`` is attacker controllable. Rejecting traversal / absolute /
    malformed encodings here keeps unauthenticated callers from reaching
    arbitrary new-api routes once httpx normalizes the URL. Only the
    documented prefixes may be forwarded.
    """
    raw = path or ""
    raw_segments = raw.replace("\\", "/").split("/")
    if any(segment in {"..", "."} for segment in raw_segments):
        return None
    clean_path = clean_relative_path(raw)
    if clean_path is None:
        return None
    first_segment = clean_path.split("/", 1)[0]
    if first_segment not in NEW_API_PROXY_PREFIXES:
        return None
    return clean_path


async def resolve_dashboard_user(
    request: Request,
    authorization: str | None,
) -> DashboardUser:
    """Resolve the browser's new-api session without using the internal token."""
    headers: dict[str, str] = {}
    if authorization:
        headers["authorization"] = authorization
    cookie = request.headers.get("cookie")
    if cookie:
        headers["cookie"] = cookie
    if not headers:
        raise HTTPException(status_code=401, detail="missing dashboard authentication")

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            upstream = await client.get(
                f"{settings.new_api_base_url}/api/user/self",
                headers=headers,
            )
            # A browser may retain only the HttpOnly refresh cookie after the
            # short-lived access token expires. Refresh once so dashboard
            # device pages remain usable without a second login.
            if upstream.status_code == 401 and cookie:
                refresh = await client.post(
                    f"{settings.new_api_base_url}/api/user/auth/refresh",
                    headers={"cookie": cookie},
                )
                if refresh.status_code == 200:
                    refresh_payload = refresh.json()
                    access_token = str(
                        (refresh_payload.get("data") or {}).get("access_token") or ""
                    )
                    if access_token:
                        upstream = await client.get(
                            f"{settings.new_api_base_url}/api/user/self",
                            headers={"authorization": f"Bearer {access_token}"},
                        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="unable to verify dashboard session") from exc

    if upstream.status_code != 200:
        raise HTTPException(status_code=401, detail="invalid dashboard session")
    try:
        payload = upstream.json()
        user = payload.get("data") or {}
        user_id = int(user.get("id") or 0)
        role = int(user.get("role") or 0)
        username = str(user.get("username") or "")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="invalid dashboard session") from exc
    if not payload.get("success") or user_id <= 0:
        raise HTTPException(status_code=401, detail="invalid dashboard session")
    return DashboardUser(id=user_id, role=role, username=username)


def require_admin(user: DashboardUser) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="administrator access required")


def require_owned_device(device: dict[str, Any], user: DashboardUser) -> None:
    if user.is_admin:
        return
    if int(device.get("user_id") or 0) != user.id:
        raise HTTPException(status_code=403, detail="device does not belong to this user")


def split_model_list(value: Any) -> set[str]:
    return {
        item.strip()
        for item in str(value or "").replace("\n", ",").split(",")
        if item.strip()
    }


def request_model_from_body(body: bytes) -> str:
    if not body:
        return ""
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return ""
    return str(payload.get("model") or "").strip()


def request_prompt_from_body(body: bytes) -> str:
    if not body:
        return ""
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    prompt = payload.get("prompt") or payload.get("input") or ""
    if isinstance(prompt, str):
        return prompt
    return json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))


def request_body_with_model(body: bytes, model: str) -> bytes:
    if not body or not model:
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return body
    if not isinstance(payload, dict):
        return body
    payload["model"] = model
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def request_body_with_web_search(body: bytes, model: str) -> bytes:
    """Enable web search only for the upstream models that support it."""
    if model not in WEB_SEARCH_MODELS:
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return body
    if not isinstance(payload, dict):
        return body
    tools = payload.get("tools")
    if tools is None:
        payload["tools"] = [{"type": "web_search"}]
    elif isinstance(tools, list) and not any(
        isinstance(tool, dict) and tool.get("type") == "web_search"
        for tool in tools
    ):
        payload["tools"] = [*tools, {"type": "web_search"}]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def plan_id_from_text(value: str) -> str:
    normalized = value.strip().lower()
    mapping = {
        "trial_1h": "trial_1h",
        "1小时体验": "trial_1h",
        "day_1": "day_1",
        "1天通行": "day_1",
        "1天套餐": "day_1",
        "day_3": "day_3",
        "3天通行": "day_3",
        "3天套餐": "day_3",
        "week_1": "week_1",
        "7天周卡": "week_1",
        "7天套餐": "week_1",
        "month_1": "month_1",
        "30天月卡": "month_1",
        "30天套餐": "month_1",
    }
    if normalized in mapping:
        return mapping[normalized]
    for needle, plan_id in (
        ("30天", "month_1"),
        ("7天", "week_1"),
        ("3天", "day_3"),
        ("1天", "day_1"),
        ("1小时", "trial_1h"),
    ):
        if needle in value:
            return plan_id
    return normalized if normalized else "day_1"


def plan_entitlements_for_device(device_id: str) -> dict[str, Any]:
    with connect(settings.database_path) as conn:
        row = conn.execute(
            """
            select p.*,
                   d.user_id as device_user_id,
                   d.plan_id as device_plan_id,
                   d.plan_started_at as device_plan_started_at
            from devices d
            left join plans p on p.id = d.plan_id
            where d.device_id = ?
            """,
            (device_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=403, detail="device plan is not configured")
    plan = dict(row)
    if not plan.get("id"):
        raise HTTPException(status_code=403, detail="device plan is not configured")
    return plan


def user_limits_for_id(user_id: int) -> dict[str, Any] | None:
    if user_id <= 0:
        return None
    with connect(settings.database_path) as conn:
        row = conn.execute(
            """
            select id, request_limit, daily_request_limit, token_limit,
                   daily_token_limit, rpm_limit, concurrency_limit
            from users
            where id = ? and status = 'active'
            """,
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def allowed_models_for_plan(plan: dict[str, Any]) -> set[str]:
    allowed: set[str] = set()
    if int(plan.get("allow_deepseek") or 0):
        allowed.update(
            split_model_list(plan.get("chat_models"))
            or split_model_list(DEFAULT_CHAT_MODELS)
        )
    if int(plan.get("allow_image") or 0):
        allowed.update(split_model_list(plan.get("image_models")) or DEFAULT_IMAGE_MODELS)
    return allowed


def day_start_ts(ts: int) -> int:
    value = datetime.fromtimestamp(ts)
    return int(value.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def int_limit(plan: dict[str, Any], key: str) -> int:
    return max(int(plan.get(key) or 0), 0)


def plan_alias(plan: dict[str, Any], key: str, fallback: str) -> str:
    return str(plan.get(key) or fallback).strip() or fallback


def default_chat_model(chat_models: set[str]) -> str:
    """Return the model used when a client selects the btf-chat alias."""
    if "deepseek-v4.1-flash" in chat_models:
        return "deepseek-v4.1-flash"
    return sorted(chat_models)[0]


def actual_model_for_request(plan: dict[str, Any], first: str, requested_model: str) -> str:
    if first == "images":
        if not int(plan.get("allow_image") or 0):
            raise HTTPException(status_code=403, detail="current device plan does not include GPT image generation")
        image_models = split_model_list(plan.get("image_models")) or DEFAULT_IMAGE_MODELS
        image_alias = plan_alias(plan, "image_alias", BTF_IMAGE_ALIAS)
        if requested_model == image_alias:
            return sorted(image_models)[0]
        if requested_model in image_models:
            return requested_model
        raise HTTPException(status_code=403, detail="image model is not included in current device plan")
    if first in {"chat", "completions", "responses"}:
        if not int(plan.get("allow_deepseek") or 0):
            raise HTTPException(status_code=403, detail="current device plan does not include DeepSeek")
        chat_models = split_model_list(plan.get("chat_models")) or split_model_list(
            DEFAULT_CHAT_MODELS
        )
        chat_alias = plan_alias(plan, "chat_alias", BTF_CHAT_ALIAS)
        if requested_model == chat_alias:
            # The compatibility alias must resolve to the same default model
            # that alias_model_response() maps back. Using sorted()[0] could
            # pick an alternate model when the plan lists one that sorts
            # before the default DeepSeek model.
            return default_chat_model(chat_models)
        if requested_model in chat_models:
            return requested_model
        raise HTTPException(status_code=403, detail="chat model is not included in current device plan")
    if requested_model in allowed_models_for_plan(plan):
        return requested_model
    raise HTTPException(status_code=403, detail="model is not included in current device plan")


def usage_counters(device_id: str, plan_started_at: int, today_started_at: int) -> dict[str, int]:
    with connect(settings.database_path) as conn:
        total = conn.execute(
            """
            select count(*) as requests,
                   coalesce(sum(total_tokens), 0) as tokens,
                   coalesce(sum(case when path like '/v1/images/%' and status_code < 400 then 1 else 0 end), 0) as images
            from usage_events
            where device_id = ? and created_at >= ?
            """,
            (device_id, plan_started_at),
        ).fetchone()
        daily = conn.execute(
            """
            select count(*) as requests,
                   coalesce(sum(total_tokens), 0) as tokens,
                   coalesce(sum(case when path like '/v1/images/%' and status_code < 400 then 1 else 0 end), 0) as images
            from usage_events
            where device_id = ? and created_at >= ?
            """,
            (device_id, today_started_at),
        ).fetchone()
        recent = conn.execute(
            """
            select count(*) as requests
            from usage_events
            where device_id = ? and created_at >= ?
            """,
            (device_id, now_ts() - 60),
        ).fetchone()
    return {
        "requests": int(total["requests"] or 0),
        "tokens": int(total["tokens"] or 0),
        "images": int(total["images"] or 0),
        "daily_requests": int(daily["requests"] or 0),
        "daily_tokens": int(daily["tokens"] or 0),
        "daily_images": int(daily["images"] or 0),
        "rpm": int(recent["requests"] or 0),
    }


def user_usage_counters(user_id: int, today_started_at: int) -> dict[str, int]:
    with connect(settings.database_path) as conn:
        total = conn.execute(
            """
            select count(*) as requests,
                   coalesce(sum(u.total_tokens), 0) as tokens
            from usage_events u
            join devices d on d.device_id = u.device_id
            where coalesce(u.user_id, d.user_id) = ?
            """,
            (user_id,),
        ).fetchone()
        daily = conn.execute(
            """
            select count(*) as requests,
                   coalesce(sum(u.total_tokens), 0) as tokens
            from usage_events u
            join devices d on d.device_id = u.device_id
            where coalesce(u.user_id, d.user_id) = ? and u.created_at >= ?
            """,
            (user_id, today_started_at),
        ).fetchone()
        recent = conn.execute(
            """
            select count(*) as requests
            from usage_events u
            join devices d on d.device_id = u.device_id
            where coalesce(u.user_id, d.user_id) = ? and u.created_at >= ?
            """,
            (user_id, now_ts() - 60),
        ).fetchone()
    return {
        "requests": int(total["requests"] or 0),
        "tokens": int(total["tokens"] or 0),
        "daily_requests": int(daily["requests"] or 0),
        "daily_tokens": int(daily["tokens"] or 0),
        "rpm": int(recent["requests"] or 0),
    }


def enforce_user_limits(user_id: int | None, today_started_at: int) -> None:
    if not user_id:
        return
    limits = user_limits_for_id(user_id)
    if not limits:
        return
    counters = user_usage_counters(user_id, today_started_at)
    checks = [
        ("request_limit", "requests", "user request quota exceeded"),
        ("daily_request_limit", "daily_requests", "user daily request quota exceeded"),
        ("token_limit", "tokens", "user token quota exceeded"),
        ("daily_token_limit", "daily_tokens", "user daily token quota exceeded"),
        ("rpm_limit", "rpm", "user rate limit exceeded"),
    ]
    for limit_key, counter_key, message in checks:
        limit = int_limit(limits, limit_key)
        if limit and counters[counter_key] >= limit:
            raise HTTPException(status_code=429 if limit_key == "rpm_limit" else 403, detail=message)
    concurrency_limit = int_limit(limits, "concurrency_limit")
    if concurrency_limit and ACTIVE_USER_REQUESTS.get(user_id, 0) >= concurrency_limit:
        raise HTTPException(status_code=429, detail="user concurrency limit exceeded")


def enforce_plan_limits(device_id: str, first: str, plan: dict[str, Any]) -> None:
    ts = now_ts()
    today_started_at = day_start_ts(ts)
    plan_started_at = int(plan.get("device_plan_started_at") or ts)
    counters = usage_counters(device_id, plan_started_at, today_started_at)
    checks = [
        ("request_limit", "requests", "device plan request quota exceeded"),
        ("daily_request_limit", "daily_requests", "device daily request quota exceeded"),
        ("token_limit", "tokens", "device plan token quota exceeded"),
        ("daily_token_limit", "daily_tokens", "device daily token quota exceeded"),
        ("rpm_limit", "rpm", "device rate limit exceeded"),
    ]
    for limit_key, counter_key, message in checks:
        limit = int_limit(plan, limit_key)
        if limit and counters[counter_key] >= limit:
            raise HTTPException(status_code=429 if limit_key == "rpm_limit" else 403, detail=message)
    if first == "images":
        for limit_key, counter_key, message in [
            ("image_limit", "images", "device plan image quota exceeded"),
            ("daily_image_limit", "daily_images", "device daily image quota exceeded"),
        ]:
            limit = int_limit(plan, limit_key)
            if limit and counters[counter_key] >= limit:
                raise HTTPException(status_code=403, detail=message)
    concurrency_limit = int_limit(plan, "concurrency_limit")
    if concurrency_limit and ACTIVE_REQUESTS.get(device_id, 0) >= concurrency_limit:
        raise HTTPException(status_code=429, detail="device concurrency limit exceeded")
    enforce_user_limits(
        int(plan.get("device_user_id") or 0) or None,
        today_started_at,
    )


def enforce_plan_access(device_id: str, path: str, method: str, body: bytes) -> bytes:
    if method == "OPTIONS":
        return body
    plan = plan_entitlements_for_device(device_id)
    first = path.split("/", 1)[0]
    # Model discovery is an authenticated endpoint too: it must honour the
    # same quota/rate limits as inference requests instead of short-circuiting
    # before the limit checks.
    enforce_plan_limits(device_id, first, plan)
    if first == "models":
        return body
    model = request_model_from_body(body)
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    actual_model = actual_model_for_request(plan, first, model)
    rewritten = request_body_with_model(body, actual_model)
    return request_body_with_web_search(rewritten, actual_model)


def filter_models_response(content: bytes, content_type: str, device_id: str) -> bytes:
    if "application/json" not in content_type.lower():
        return content
    try:
        payload = json.loads(content.decode("utf-8"))
    except Exception:
        return content
    if not isinstance(payload.get("data"), list):
        return content
    plan = plan_entitlements_for_device(device_id)
    data: list[dict[str, Any]] = []
    if int(plan.get("allow_deepseek") or 0):
        chat_models = sorted(
            split_model_list(plan.get("chat_models"))
            or split_model_list(DEFAULT_CHAT_MODELS)
        )
        chat_ids = [plan_alias(plan, "chat_alias", BTF_CHAT_ALIAS)]
        if len(chat_models) > 1:
            chat_ids.extend(chat_models)
        for model_id in dict.fromkeys(chat_ids):
            data.append({
                "id": model_id,
                "object": "model",
                "created": 1626777600,
                "owned_by": "btf",
                "supported_endpoint_types": ["chat", "openai"],
            })
    if int(plan.get("allow_image") or 0):
        data.append({
            "id": plan_alias(plan, "image_alias", BTF_IMAGE_ALIAS),
            "object": "model",
            "created": 1626777600,
            "owned_by": "btf",
            "supported_endpoint_types": ["image-generation", "openai"],
        })
    payload["data"] = data
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def alias_model_response(content: bytes, content_type: str, device_id: str) -> bytes:
    if "application/json" not in content_type.lower():
        return content
    try:
        payload = json.loads(content.decode("utf-8"))
    except Exception:
        return content
    if not isinstance(payload, dict):
        return content
    model = str(payload.get("model") or "")
    if not model:
        return content
    plan = plan_entitlements_for_device(device_id)
    chat_models = split_model_list(plan.get("chat_models")) or split_model_list(
        DEFAULT_CHAT_MODELS
    )
    image_models = split_model_list(plan.get("image_models")) or DEFAULT_IMAGE_MODELS
    normalized_image_models = {
        item.split("]", 1)[1] if item.startswith("[") and "]" in item else item
        for item in image_models
    } | image_models
    if model in chat_models:
        if model == default_chat_model(chat_models):
            payload["model"] = plan_alias(plan, "chat_alias", BTF_CHAT_ALIAS)
        else:
            # Keep explicitly selected alternate models visible to clients.
            payload["model"] = model
    elif model in normalized_image_models:
        payload["model"] = plan_alias(plan, "image_alias", BTF_IMAGE_ALIAS)
    else:
        return content
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def image_extension(content_type: str, data: bytes) -> str:
    normalized = content_type.lower().split(";", 1)[0].strip()
    if normalized == "image/jpeg" or data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if normalized == "image/webp" or data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return ".webp"
    if normalized == "image/gif" or data.startswith(b"GIF8"):
        return ".gif"
    return ".png"


def generated_image_public_url(relative_path: str) -> str:
    return f"{settings.public_base_url}/device-images/{relative_path}"


def save_generated_image_bytes(data: bytes, content_type: str, index: int) -> tuple[str, str, str, int]:
    if not data or len(data) > MAX_GENERATED_IMAGE_BYTES:
        raise ValueError("generated image is empty or too large")
    ext = image_extension(content_type, data)
    day = datetime.fromtimestamp(now_ts()).strftime("%Y%m%d")
    digest = hashlib.sha256(data).hexdigest()[:16]
    filename = f"{now_ts()}-{secrets.token_urlsafe(6)}-{index}-{digest}{ext}"
    relative_path = f"{day}/{filename}"
    target = settings.generated_images_dir / day / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    write_bytes_atomic(target, data)
    return relative_path, str(target), generated_image_public_url(relative_path), len(data)


def write_bytes_atomic(target: Path, data: bytes) -> None:
    """Write *data* to *target* atomically via a temp file + os.replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def decode_image_b64(value: str) -> tuple[bytes, str]:
    content_type = "image/png"
    encoded = value
    if value.startswith("data:") and "," in value:
        metadata, encoded = value.split(",", 1)
        content_type = metadata.removeprefix("data:").split(";", 1)[0] or content_type
    return base64.b64decode(encoded, validate=False), content_type


def is_public_ip(value: str) -> bool:
    """Return True only for globally routable unicast IP addresses.

    Rejects private/loopback/link-local/reserved/multicast/unspecified ranges,
    including IPv4-mapped IPv6 addresses (which are unwrapped and re-checked).
    """
    try:
        ip = ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def resolve_host_ips(host: str) -> list[str]:
    """Resolve a hostname to all of its IP addresses (literals included)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0] not in addresses:
            addresses.append(sockaddr[0])
    return addresses


def validate_image_url(url: str) -> str:
    """Validate that *url* is safe to fetch for generated-image persistence.

    Enforces an http/https scheme and that every resolved IP is publicly
    routable (SSRF guard). Returns the parsed host on success, raising
    ValueError otherwise.
    """
    if not url or not isinstance(url, str):
        raise ValueError("generated image URL is empty")
    parts = urlparse(url)
    scheme = parts.scheme.lower()
    if scheme not in IMAGE_DOWNLOAD_ALLOWED_SCHEMES:
        raise ValueError(f"generated image URL scheme not allowed: {scheme or '<none>'}")
    host = parts.hostname
    if not host:
        raise ValueError("generated image URL has no host")
    addresses = resolve_host_ips(host)
    if not addresses:
        raise ValueError(f"generated image URL host did not resolve: {host}")
    for address in addresses:
        if not is_public_ip(address):
            raise ValueError(f"generated image URL resolves to non-public address: {address}")
    return host


async def download_image_url(url: str) -> tuple[bytes, str]:
    """Fetch an image URL with SSRF protection, streaming and size capping.

    Redirects are followed manually (redirects disabled on the client) so that
    every hop's host is re-validated, and bytes are accumulated incrementally so
    an oversized response aborts as soon as it exceeds MAX_GENERATED_IMAGE_BYTES.
    """
    current_url = url
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
        for _ in range(MAX_IMAGE_REDIRECTS + 1):
            validate_image_url(current_url)
            request = client.build_request(
                "GET",
                current_url,
                headers={
                    "accept": "image/*,*/*;q=0.8",
                    "user-agent": "Mozilla/5.0",
                },
            )
            response = await client.send(request, stream=True)
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    await response.aclose()
                    if not location:
                        raise ValueError("generated image URL redirect missing location")
                    current_url = str(httpx.URL(current_url).join(location))
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "image/png")
                if "image/" not in content_type.lower():
                    raise ValueError("generated image URL did not return an image")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes(IMAGE_DOWNLOAD_CHUNK_BYTES):
                    total += len(chunk)
                    if total > MAX_GENERATED_IMAGE_BYTES:
                        raise ValueError("generated image is too large")
                    chunks.append(chunk)
                if total == 0:
                    raise ValueError("generated image is empty")
                return b"".join(chunks), content_type
            finally:
                await response.aclose()
    raise ValueError("generated image URL exceeded the redirect limit")


async def persist_image_generation_response(
    content: bytes,
    content_type: str,
    device_id: str,
    user_id: int | None,
    plan_id: str,
    requested_model: str,
    upstream_model: str,
    request_body: bytes,
    usage_event_id: int | None,
) -> bytes:
    if "application/json" not in content_type.lower():
        return content
    try:
        payload = json.loads(content.decode("utf-8"))
    except Exception:
        return content
    data = payload.get("data")
    if not isinstance(payload, dict) or not isinstance(data, list):
        return content
    prompt = request_prompt_from_body(request_body)
    changed = False
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        original_url = str(item.get("url") or "")
        try:
            if item.get("b64_json"):
                image_bytes, image_content_type = decode_image_b64(str(item["b64_json"]))
            elif original_url:
                image_bytes, image_content_type = await download_image_url(original_url)
            else:
                continue
            relative_path, local_path, public_url, byte_size = save_generated_image_bytes(
                image_bytes,
                image_content_type,
                index,
            )
        except Exception as exc:
            logger.warning(
                "generated image persistence failed for device=%s index=%s url=%s: %s",
                device_id,
                index,
                original_url,
                exc,
            )
            item["btf_image_error"] = str(exc) or exc.__class__.__name__
            changed = True
            continue
        record_generated_image(
            settings.database_path,
            usage_event_id,
            device_id,
            user_id,
            plan_id,
            requested_model,
            upstream_model,
            prompt,
            original_url,
            local_path,
            public_url,
            image_content_type,
            byte_size,
        )
        if original_url:
            item["btf_original_url"] = original_url
        item["url"] = public_url
        item["btf_local_path"] = relative_path
        changed = True
    if not changed:
        return content
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "btf-device-gateway"}


@app.get("/api/device/status")
def api_device_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    device = get_device_by_key(settings.database_path, bearer_token(authorization))
    active = device["status"] == "active" and device["expires_at"] > now_ts()
    return {
        "device_id": device["device_id"],
        "user_id": device.get("user_id"),
        "status": device["status"],
        "active": active,
        "expires_at": device["expires_at"],
        "expires_at_text": fmt_ts(device["expires_at"]),
        "request_count": device["request_count"],
        "total_tokens": device["total_tokens"],
    }


@app.get("/device-images/{path:path}")
def generated_device_image(path: str) -> FileResponse:
    clean_path = clean_relative_path(path)
    if clean_path is None:
        raise HTTPException(status_code=404, detail="not found")
    target = settings.generated_images_dir / clean_path
    try:
        target.relative_to(settings.generated_images_dir)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found") from None
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"})


def normalize_mac(value: Any) -> str:
    return str(value or "").strip().upper()


def normalize_bind_code(value: Any) -> str:
    return str(value or "").strip().upper().replace(" ", "")


def new_bind_code() -> str:
    return secrets.token_hex(4).upper()


def mask_bind_code(value: str) -> str:
    if len(value) <= 4:
        return "****"
    return f"****{value[-4:]}"


def int_or_400(value: Any, field: str) -> int:
    """Parse an integer payload field, rejecting malformed input with 400."""
    try:
        return int(value or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid {field}") from exc


def optional_user_id(value: Any) -> int | None:
    user_id = int_or_400(value, "user_id")
    return user_id if user_id > 0 else None


def positive_expires_at(value: Any, field: str = "expires_at") -> int:
    """Parse and bound a required expiry timestamp.

    Rejects non-numeric input with 400 and out-of-range values (``0``,
    negatives, or absurdly large epochs) so a device can never end up stored
    with an already-invalid expiry.
    """
    expires_at = int_or_400(value, field)
    if expires_at <= 0 or expires_at > MAX_EXPIRES_AT:
        raise HTTPException(status_code=400, detail=f"invalid {field}")
    return expires_at


def split_macs(value: Any) -> list[str]:
    raw = str(value or "")
    parts = raw.replace(",", "\n").replace(";", "\n").splitlines()
    macs: list[str] = []
    seen: set[str] = set()
    for part in parts:
        mac = normalize_mac(part)
        if mac and mac not in seen:
            macs.append(mac)
            seen.add(mac)
    return macs


def compose_note(name: str, plan: str, remark: str) -> str:
    name = name.strip()
    plan = plan.strip()
    remark = remark.strip()
    if name and plan:
        return f"{name} / {plan}"
    if remark:
        return remark
    return name or plan


def device_display_fields(note: str) -> tuple[str, str]:
    note = note or ""
    if " / " in note:
        name, plan = note.split(" / ", 1)
        return name, plan
    return note, ""


def device_effective_status(status: str, expires_at: int, now: int) -> str:
    if status == "active" and int(expires_at or 0) <= now:
        return "expired"
    return status


def device_row_to_new_api(row: dict[str, Any], now: int | None = None) -> dict[str, Any]:
    note = row.get("note") or ""
    name, plan = device_display_fields(note)
    if now is None:
        now = now_ts()
    status = str(row.get("status") or "")
    return {
        "id": row["id"],
        "user_id": row.get("user_id") or 0,
        "username": row.get("owner_display_name") or row.get("owner_username") or "",
        "mac": row["device_id"],
        "name": name,
        "plan": row.get("plan_title") or plan,
        "plan_id": row.get("plan_id") or "",
        "allow_deepseek": bool(row.get("allow_deepseek") or 0),
        "allow_image": bool(row.get("allow_image") or 0),
        "bind_code_mask": row.get("bind_code_mask") or "",
        "bound_at": row.get("bound_at") or 0,
        "status": device_effective_status(status, row.get("expires_at") or 0, now),
        "expires_at": row["expires_at"],
        "last_seen_at": row.get("last_seen_at") or 0,
        "request_count": row["request_count"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "total_tokens": row["total_tokens"],
        "remark": note,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def clamp_page_bounds(p: int, page_size: int) -> tuple[int, int]:
    """Validate pagination bounds, rejecting illegal values with HTTP 400."""
    if p < 1 or p > MAX_PAGE:
        raise HTTPException(status_code=400, detail="invalid page")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise HTTPException(status_code=400, detail="invalid page_size")
    return p, page_size


def normalized_device_statuses(status: str) -> list[str]:
    """Split a status filter into known statuses, rejecting unknown ones.

    ``expired`` is virtual: it is derived from ``expires_at`` rather than the
    stored status column, so it maps to an extra SQL predicate.
    """
    requested = [part.strip() for part in str(status or "").split(",") if part.strip()]
    if not requested:
        return []
    unknown = [value for value in requested if value not in DEVICE_STATUSES]
    if unknown:
        raise HTTPException(status_code=400, detail="invalid status")
    return list(dict.fromkeys(requested))


def new_api_page(items: list[dict[str, Any]], page: int, page_size: int, total: int | None = None) -> dict[str, Any]:
    """Build a page envelope.

    When ``total`` is provided the caller has already paginated (e.g. in SQL)
    and ``items`` is the page itself, so it is not sliced again.
    """
    if total is None:
        start = max(page - 1, 0) * page_size
        items = items[start:start + page_size]
        total = len(items)
    return {
        "items": items,
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


@app.get("/api/device/")
async def api_list_devices(
    request: Request,
    authorization: str | None = Header(default=None),
    keyword: str = "",
    status: str = "",
    user_id: int = 0,
    sort_by: str = "id",
    sort_order: str = "desc",
    p: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    p, page_size = clamp_page_bounds(p, page_size)
    statuses = normalized_device_statuses(status)
    where: list[str] = []
    params: list[Any] = []
    if not dashboard_user.is_admin:
        where.append("d.user_id = ?")
        params.append(dashboard_user.id)
    if dashboard_user.is_admin and user_id > 0:
        where.append("d.user_id = ?")
        params.append(user_id)
    if statuses:
        clauses: list[str] = []
        stored = [value for value in statuses if value != "expired"]
        if stored:
            placeholders = ", ".join("?" for _ in stored)
            clauses.append(f"d.status in ({placeholders})")
            params.extend(stored)
        if "expired" in statuses:
            clauses.append("d.status = 'active' and d.expires_at <= ?")
            params.append(now_ts())
        where.append("(" + " or ".join(clauses) + ")")
    if keyword:
        needle = f"%{keyword.lower()}%"
        where.append(
            "(lower(d.device_id) like ? or lower(coalesce(d.note, '')) like ?"
            " or lower(coalesce(u.username, '')) like ?"
            " or lower(coalesce(u.display_name, '')) like ?)"
        )
        params.extend([needle, needle, needle, needle])
    sort_fields = {
        "id": "d.id",
        "mac": "d.device_id",
        "user_id": "d.user_id",
        "status": "d.status",
        "expires_at": "d.expires_at",
        "last_seen_at": "d.last_seen_at",
        "request_count": "d.request_count",
        "created_at": "d.created_at",
    }
    sort_column = sort_fields.get(sort_by, "d.id")
    direction = "asc" if str(sort_order).lower() == "asc" else "desc"
    where_sql = (" where " + " and ".join(where)) if where else ""
    offset = (p - 1) * page_size
    with connect(settings.database_path) as conn:
        total = int(
            conn.execute(
                f"""
                select count(*)
                from devices d
                left join users u on u.id = d.user_id
                {where_sql}
                """,
                params,
            ).fetchone()[0]
            or 0
        )
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                select d.*, u.username as owner_username, u.display_name as owner_display_name,
                       p.title as plan_title, p.allow_deepseek, p.allow_image
                from devices d
                left join users u on u.id = d.user_id
                left join plans p on p.id = d.plan_id
                {where_sql}
                order by {sort_column} {direction}, d.id desc
                limit ? offset ?
                """,
                (*params, page_size, offset),
            ).fetchall()
        ]
    items = [device_row_to_new_api(row) for row in rows]
    return {"success": True, "message": "", "data": new_api_page(items, p, page_size, total)}


@app.post("/api/device/")
async def api_create_device(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    require_admin(dashboard_user)
    payload = await request.json()
    user_id = optional_user_id(payload.get("user_id"))
    mac = normalize_mac(payload.get("mac"))
    expires_at = int_or_400(payload.get("expires_at"), "expires_at")
    status = str(payload.get("status") or "active").strip() or "active"
    plan_id = plan_id_from_text(str(payload.get("plan_id") or payload.get("plan") or "day_1"))
    bind_code = normalize_bind_code(payload.get("bind_code"))
    if not bind_code and payload.get("generate_bind_code"):
        bind_code = new_bind_code()
    # Optional explicit auth credential. When absent we keep the historical
    # MAC-as-key behaviour so existing provisioning is unaffected.
    device_key = str(payload.get("device_key") or "").strip() or None
    resolved_key = device_key or mac
    if not mac or not expires_at:
        return {"success": False, "message": "invalid params"}
    if status not in {"active", "disabled"}:
        return {"success": False, "message": "invalid status"}
    ts = now_ts()
    if status == "active":
        expires_at = positive_expires_at(payload.get("expires_at"), "expires_at")
        if expires_at <= ts:
            return {"success": False, "message": "expires_at must be in the future for an active device"}
    note = compose_note(
        str(payload.get("name") or ""),
        str(payload.get("plan") or ""),
        str(payload.get("remark") or ""),
    )
    with connect(settings.database_path) as conn:
        if user_id is not None:
            user = conn.execute("select id from users where id = ?", (user_id,)).fetchone()
            if not user:
                return {"success": False, "message": "user not found"}
        plan = conn.execute("select id from plans where id = ? and enabled = 1", (plan_id,)).fetchone()
        if not plan:
            return {"success": False, "message": "plan not found"}
        try:
            conn.execute(
                """
                insert into devices (
                  device_id, user_id, key_hash, key_mask, status, expires_at,
                  created_at, updated_at, note, plan_id, plan_started_at,
                  bind_code_hash, bind_code_mask, bound_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mac,
                    user_id,
                    hash_key(resolved_key),
                    mask_key(resolved_key),
                    status,
                    expires_at,
                    ts,
                    ts,
                    note,
                    plan_id,
                    ts,
                    hash_key(bind_code) if bind_code else None,
                    mask_bind_code(bind_code) if bind_code else "",
                    ts if user_id is not None else None,
                ),
            )
        except Exception as exc:
            return {"success": False, "message": str(exc)}
    return {
        "success": True,
        "message": "",
        "data": {
            "created": True,
            "device_key": resolved_key if device_key else "",
            "bind_code": bind_code if bind_code and dashboard_user.is_admin else "",
        },
    }


@app.put("/api/device/")
async def api_update_device(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    require_admin(dashboard_user)
    payload = await request.json()
    device_id = int_or_400(payload.get("id"), "id")
    user_id = optional_user_id(payload.get("user_id"))
    mac = normalize_mac(payload.get("mac"))
    expires_at = int_or_400(payload.get("expires_at"), "expires_at")
    status = str(payload.get("status") or "active").strip() or "active"
    plan_id = plan_id_from_text(str(payload.get("plan_id") or payload.get("plan") or "day_1"))
    bind_code = normalize_bind_code(payload.get("bind_code"))
    if not device_id or not mac or not expires_at:
        return {"success": False, "message": "invalid params"}
    if status not in {"active", "disabled"}:
        return {"success": False, "message": "invalid status"}
    note = compose_note(
        str(payload.get("name") or ""),
        str(payload.get("plan") or ""),
        str(payload.get("remark") or ""),
    )
    ts = now_ts()
    if status == "active":
        expires_at = positive_expires_at(payload.get("expires_at"), "expires_at")
        if expires_at <= ts:
            return {"success": False, "message": "expires_at must be in the future for an active device"}
    with connect(settings.database_path) as conn:
        if user_id is not None:
            user = conn.execute("select id from users where id = ?", (user_id,)).fetchone()
            if not user:
                return {"success": False, "message": "user not found"}
        plan = conn.execute("select id from plans where id = ? and enabled = 1", (plan_id,)).fetchone()
        if not plan:
            return {"success": False, "message": "plan not found"}
        row = conn.execute("select id, user_id, plan_id from devices where id = ?", (device_id,)).fetchone()
        if not row:
            return {"success": False, "message": "device not found"}
        plan_started_at = ts if str(row["plan_id"] or "") != plan_id else None
        old_user_id = int(row["user_id"] or 0)
        next_user_id = user_id or 0
        bound_at = None if user_id is None else (ts if old_user_id != next_user_id else None)
        try:
            conn.execute(
                """
                update devices
                set device_id = ?, user_id = ?, key_hash = ?, key_mask = ?,
                    status = ?, expires_at = ?, updated_at = ?, note = ?, plan_id = ?,
                    plan_started_at = coalesce(?, plan_started_at),
                    bind_code_hash = coalesce(?, bind_code_hash),
                    bind_code_mask = coalesce(?, bind_code_mask),
                    bound_at = case when ? is null then null else coalesce(?, bound_at) end
                where id = ?
                """,
                (
                    mac,
                    user_id,
                    hash_key(mac),
                    mask_key(mac),
                    status,
                    expires_at,
                    ts,
                    note,
                    plan_id,
                    plan_started_at,
                    hash_key(bind_code) if bind_code else None,
                    mask_bind_code(bind_code) if bind_code else None,
                    user_id,
                    bound_at,
                    device_id,
                ),
            )
        except Exception as exc:
            return {"success": False, "message": str(exc)}
    return {"success": True, "message": "", "data": {"updated": True}}


@app.post("/api/device/batch")
async def api_batch_create_devices(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    require_admin(dashboard_user)
    payload = await request.json()
    user_id = optional_user_id(payload.get("user_id"))
    macs = split_macs(payload.get("macs"))
    expires_at = int_or_400(payload.get("expires_at"), "expires_at")
    plan = str(payload.get("plan") or "")
    plan_id = plan_id_from_text(str(payload.get("plan_id") or plan or "day_1"))
    remark = str(payload.get("remark") or "")
    bind_code = normalize_bind_code(payload.get("bind_code"))
    generate_bind_code = bool(payload.get("generate_bind_code"))
    if not macs or not expires_at:
        return {"success": False, "message": "invalid params"}
    ts = now_ts()
    expires_at = positive_expires_at(payload.get("expires_at"), "expires_at")
    if expires_at <= ts:
        return {"success": False, "message": "expires_at must be in the future"}
    created = 0
    failed: list[str] = []
    failures: list[dict[str, str]] = []
    bind_codes: dict[str, str] = {}
    with connect(settings.database_path) as conn:
        if user_id is not None:
            user = conn.execute("select id from users where id = ?", (user_id,)).fetchone()
            if not user:
                return {"success": False, "message": "user not found"}
        plan_row = conn.execute("select id from plans where id = ? and enabled = 1", (plan_id,)).fetchone()
        if not plan_row:
            return {"success": False, "message": "plan not found"}
        for mac in macs:
            note = compose_note(mac, plan, remark)
            row_bind_code = bind_code or (new_bind_code() if generate_bind_code else "")
            try:
                conn.execute(
                    """
                    insert into devices (
                      device_id, user_id, key_hash, key_mask, status, expires_at,
                      created_at, updated_at, note, plan_id, plan_started_at,
                      bind_code_hash, bind_code_mask, bound_at
                    )
                    values (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mac,
                        user_id,
                        hash_key(mac),
                        mask_key(mac),
                        expires_at,
                        ts,
                        ts,
                        note,
                        plan_id,
                        ts,
                        hash_key(row_bind_code) if row_bind_code else None,
                        mask_bind_code(row_bind_code) if row_bind_code else "",
                        ts if user_id is not None else None,
                    ),
                )
                created += 1
                if row_bind_code:
                    bind_codes[mac] = row_bind_code
            except Exception as exc:
                # Keep the real per-device failure instead of silently
                # reporting success for rejected inserts.
                failed.append(mac)
                failures.append({"mac": mac, "message": str(exc)})
    return {
        "success": created > 0,
        "message": "" if not failures else "some devices could not be created",
        "data": {
            "created": created,
            "failed": failed,
            "failures": failures,
            "bind_codes": bind_codes,
        },
    }


@app.post("/api/device/bind")
async def api_bind_device_to_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    payload = await request.json()
    mac = normalize_mac(payload.get("mac"))
    bind_code = normalize_bind_code(payload.get("bind_code"))
    if not mac or not bind_code:
        return {"success": False, "message": "invalid params"}
    ts = now_ts()
    with connect(settings.database_path) as conn:
        row = conn.execute("select * from devices where device_id = ?", (mac,)).fetchone()
        if not row:
            return {"success": False, "message": "device not found"}
        device = dict(row)
        if device.get("user_id") and int(device["user_id"]) != dashboard_user.id:
            return {"success": False, "message": "device already bound"}
        if not device.get("bind_code_hash"):
            return {"success": False, "message": "device binding code is not configured"}
        if not verify_bind_code(device.get("bind_code_hash"), bind_code):
            return {"success": False, "message": "invalid binding code"}
        conn.execute(
            """
            update devices
            set user_id = ?, bound_at = coalesce(bound_at, ?), updated_at = ?
            where device_id = ?
            """,
            (dashboard_user.id, ts, ts, mac),
        )
    return {"success": True, "message": "", "data": {"bound": True}}


@app.post("/api/device/manage")
async def api_manage_device(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    require_admin(dashboard_user)
    payload = await request.json()
    device_id = int_or_400(payload.get("id"), "id")
    action = payload.get("action")
    expires_at = int_or_400(payload.get("expires_at"), "expires_at")
    ts = now_ts()
    with connect(settings.database_path) as conn:
        row = conn.execute("select device_id, status from devices where id = ?", (device_id,)).fetchone()
        if not row:
            return {"success": False, "message": "device not found"}
        if action == "enable":
            conn.execute("update devices set status = 'active', updated_at = ? where id = ?", (ts, device_id))
        elif action == "disable":
            conn.execute("update devices set status = 'disabled', updated_at = ? where id = ?", (ts, device_id))
        elif action == "renew":
            expires_at = positive_expires_at(payload.get("expires_at"), "expires_at")
            if expires_at <= ts:
                return {"success": False, "message": "expires_at must be in the future"}
            conn.execute(
                "update devices set expires_at = ?, status = 'active', updated_at = ?, plan_started_at = ? where id = ?",
                (expires_at, ts, ts, device_id),
            )
        else:
            return {"success": False, "message": "unsupported action"}
    return {"success": True, "message": "", "data": {"updated": True}}


def public_plan(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "title": row.get("title"),
        "duration_days": row.get("duration_days"),
        "price_amount": row.get("price_amount"),
        "currency": row.get("currency"),
        "sort_order": row.get("sort_order"),
        "enabled": row.get("enabled"),
        "allow_deepseek": row.get("allow_deepseek"),
        "allow_image": row.get("allow_image"),
    }


@app.get("/api/device/plans")
async def api_list_device_plans(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    items = plans(settings.database_path)
    dashboard_user = None
    if authorization or request.headers.get("cookie"):
        try:
            dashboard_user = await resolve_dashboard_user(request, authorization)
        except HTTPException:
            dashboard_user = None
    if not dashboard_user or not dashboard_user.is_admin:
        items = [public_plan(item) for item in items]
    return {"success": True, "message": "", "data": {"items": items}}


@app.put("/api/device/plans/{plan_id}")
async def api_update_device_plan(
    plan_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    require_admin(dashboard_user)
    payload = await request.json()
    with connect(settings.database_path) as conn:
        row = conn.execute("select * from plans where id = ?", (plan_id,)).fetchone()
        if not row:
            return {"success": False, "message": "plan not found"}
        current = dict(row)
        allow_deepseek = (
            1 if payload.get("allow_deepseek") else 0
            if "allow_deepseek" in payload
            else int(current.get("allow_deepseek") or 0)
        )
        allow_image = (
            1 if payload.get("allow_image") else 0
            if "allow_image" in payload
            else int(current.get("allow_image") or 0)
        )
        chat_models = str(
            payload.get("chat_models")
            if "chat_models" in payload
            else current.get("chat_models") or DEFAULT_CHAT_MODELS
        ).strip()
        image_models = str(
            payload.get("image_models")
            if "image_models" in payload
            else current.get("image_models") or ""
        ).strip()
        if allow_image and not image_models:
            image_models = DEFAULT_IMAGE_MODEL_TEXT
        limit_fields = {
            "token_limit": int(current.get("token_limit") or 0),
            "daily_token_limit": int(current.get("daily_token_limit") or 0),
            "request_limit": int(current.get("request_limit") or 0),
            "daily_request_limit": int(current.get("daily_request_limit") or 0),
            "image_limit": int(current.get("image_limit") or 0),
            "daily_image_limit": int(current.get("daily_image_limit") or 0),
            "rpm_limit": int(current.get("rpm_limit") or 0),
            "concurrency_limit": int(current.get("concurrency_limit") or 0),
        }
        for field in limit_fields:
            if field in payload:
                limit_fields[field] = max(int_or_400(payload.get(field), field), 0)
        chat_alias = str(payload.get("chat_alias") if "chat_alias" in payload else current.get("chat_alias") or BTF_CHAT_ALIAS).strip() or BTF_CHAT_ALIAS
        image_alias = str(payload.get("image_alias") if "image_alias" in payload else current.get("image_alias") or BTF_IMAGE_ALIAS).strip() or BTF_IMAGE_ALIAS
        conn.execute(
            """
            update plans
            set allow_deepseek = ?,
                allow_image = ?,
                chat_models = ?,
                image_models = ?,
                token_limit = ?,
                daily_token_limit = ?,
                request_limit = ?,
                daily_request_limit = ?,
                image_limit = ?,
                daily_image_limit = ?,
                rpm_limit = ?,
                concurrency_limit = ?,
                chat_alias = ?,
                image_alias = ?
            where id = ?
            """,
            (
                allow_deepseek,
                allow_image,
                chat_models,
                image_models,
                limit_fields["token_limit"],
                limit_fields["daily_token_limit"],
                limit_fields["request_limit"],
                limit_fields["daily_request_limit"],
                limit_fields["image_limit"],
                limit_fields["daily_image_limit"],
                limit_fields["rpm_limit"],
                limit_fields["concurrency_limit"],
                chat_alias,
                image_alias,
                plan_id,
            ),
        )
        updated = conn.execute("select * from plans where id = ?", (plan_id,)).fetchone()
    return {"success": True, "message": "", "data": dict(updated)}


@app.get("/api/device/payment-providers")
def api_list_payment_providers() -> dict[str, Any]:
    return {
        "success": True,
        "message": "",
        "data": {
            "items": [
                {"id": name, "title": {"mock": "模拟支付", "wechat": "微信支付", "alipay": "支付宝"}[name]}
                for name in configured_provider_names(settings)
            ]
        },
    }


@app.get("/api/device/orders")
async def api_list_device_orders(
    request: Request,
    authorization: str | None = Header(default=None),
    keyword: str = "",
    status: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    items = list_orders(
        settings.database_path,
        limit,
        None if dashboard_user.is_admin else dashboard_user.id,
    )
    if status:
        items = [item for item in items if item["status"] == status]
    if keyword:
        needle = keyword.lower()
        items = [
            item
            for item in items
            if needle in item["id"].lower()
            or needle in item["device_id"].lower()
            or needle in item["plan_id"].lower()
            or needle in str(item.get("plan_title") or "").lower()
        ]
    return {"success": True, "message": "", "data": {"items": items, "total": len(items)}}


def payment_notify_url(provider: str) -> str:
    base = settings.payment_notify_url or settings.public_base_url
    return f"{base}/api/device/payments/{provider}/notify"


@app.post("/api/device/orders/{order_id}/pay")
async def api_start_device_payment(
    order_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_idempotency_key: str | None = Header(default=None),
) -> dict[str, Any]:
    payload = await request.json()
    provider_name = str(payload.get("provider") or "").strip().lower()
    if not provider_name:
        return {"success": False, "message": "provider is required"}
    try:
        order = get_order(settings.database_path, order_id)
        # A device may only start payment for its own order. Admin-side
        # confirmation remains available through the existing authenticated UI.
        device = get_device_by_key(settings.database_path, bearer_token(authorization))
        if device["device_id"] != order["device_id"]:
            raise DeviceError("order does not belong to this device")
        provider = get_payment_provider(provider_name, settings)
        intent = await provider.create_payment(
            order,
            payment_notify_url(provider_name),
            str(payload.get("return_url") or "") or None,
        )
        transaction = save_payment_intent(
            settings.database_path,
            order_id,
            intent,
            x_idempotency_key,
        )
    except DeviceError as exc:
        return {"success": False, "message": str(exc)}
    except PaymentNotConfigured as exc:
        return {"success": False, "message": str(exc)}
    except PaymentError as exc:
        return {"success": False, "message": str(exc)}
    return {
        "success": True,
        "message": "",
        "data": {
            "order": get_order(settings.database_path, order_id),
            "payment": transaction,
        },
    }


@app.post("/api/device/orders")
async def api_create_device_order(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    payload = await request.json()
    mac = normalize_mac(payload.get("mac"))
    plan_id = str(payload.get("plan_id") or "").strip()
    if not mac or not plan_id:
        return {"success": False, "message": "invalid params"}
    try:
        # The public /device/pay flow has no dashboard session. When a
        # customer is logged in, bind the order request to that customer's
        # device ownership instead of accepting a foreign MAC.
        dashboard_user = None
        if authorization or request.headers.get("cookie"):
            dashboard_user = await resolve_dashboard_user(request, authorization)
        if dashboard_user:
            device = get_device_by_key(settings.database_path, mac)
            require_owned_device(device, dashboard_user)
        order = create_order(
            settings.database_path,
            mac,
            plan_id,
            settings.order_expires_seconds,
        )
    except DeviceError as exc:
        return {"success": False, "message": str(exc)}
    return {"success": True, "message": "", "data": order}


@app.post("/api/device/orders/{order_id}/close")
async def api_close_device_order(
    order_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    payload = await request.json()
    reason = str(payload.get("reason") or "manual").strip() or "manual"
    try:
        order = get_order(settings.database_path, order_id)
        if not dashboard_user.is_admin:
            with connect(settings.database_path) as conn:
                device = conn.execute(
                    "select user_id from devices where device_id = ?",
                    (order["device_id"],),
                ).fetchone()
            if not device or int(device["user_id"] or 0) != dashboard_user.id:
                raise DeviceError("order does not belong to this user")
        closed = close_order(settings.database_path, order_id, reason)
    except DeviceError as exc:
        return {"success": False, "message": str(exc)}
    return {"success": True, "message": "", "data": closed}


@app.post("/api/device/orders/approve")
async def api_approve_device_order(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    require_admin(dashboard_user)
    payload = await request.json()
    order_id = str(payload.get("order_id") or "").strip()
    if not order_id:
        return {"success": False, "message": "invalid order_id"}
    try:
        order = approve_order(settings.database_path, order_id)
    except DeviceError as exc:
        return {"success": False, "message": str(exc)}
    return {"success": True, "message": "", "data": order}


@app.get("/api/device/usage")
async def api_list_device_usage(
    request: Request,
    authorization: str | None = Header(default=None),
    device_id: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    items = list_usage(
        settings.database_path,
        normalize_mac(device_id) or None,
        limit,
        None if dashboard_user.is_admin else dashboard_user.id,
    )
    return {"success": True, "message": "", "data": {"items": items, "total": len(items)}}


@app.get("/api/device/images")
async def api_list_device_images(
    request: Request,
    authorization: str | None = Header(default=None),
    device_id: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    items = list_generated_images(
        settings.database_path,
        normalize_mac(device_id) or None,
        max(1, min(limit, 500)),
        None if dashboard_user.is_admin else dashboard_user.id,
    )
    return {"success": True, "message": "", "data": {"items": items, "total": len(items)}}


def user_usage_rows(user_id: int | None = None) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = ""
    if user_id is not None:
        where = "where u.id = ?"
        params.append(user_id)
    today_started_at = day_start_ts(now_ts())
    with connect(settings.database_path) as conn:
        rows = conn.execute(
            f"""
            select u.id, u.username, u.display_name, u.status,
                   u.request_limit, u.daily_request_limit,
                   u.token_limit, u.daily_token_limit,
                   u.rpm_limit, u.concurrency_limit,
                   count(e.id) as request_count,
                   coalesce(sum(e.total_tokens), 0) as total_tokens,
                   coalesce(sum(e.estimated_cost), 0) as estimated_cost,
                   coalesce(sum(case when e.created_at >= ? then 1 else 0 end), 0) as daily_request_count,
                   coalesce(sum(case when e.created_at >= ? then e.total_tokens else 0 end), 0) as daily_tokens,
                   coalesce(sum(case when e.created_at >= ? then e.estimated_cost else 0 end), 0) as daily_estimated_cost
            from users u
            left join usage_events e on e.user_id = u.id
            {where}
            group by u.id
            order by total_tokens desc, request_count desc, u.id
            """,
            (today_started_at, today_started_at, today_started_at, *params),
        ).fetchall()
    return [dict(row) for row in rows]


def btf_usage_where(
    user_id: int,
    request: Request,
) -> tuple[str, list[Any]]:
    """Build a customer-scoped usage query.

    ``usage_events.user_id`` is authoritative for new records. The device
    owner fallback keeps older records visible after the migration that added
    the column.
    """
    conditions = ["coalesce(e.user_id, d.user_id) = ?"]
    params: list[Any] = [user_id]
    query = request.query_params

    try:
        start_timestamp = int(query.get("start_timestamp") or 0)
    except ValueError:
        start_timestamp = 0
    try:
        end_timestamp = int(query.get("end_timestamp") or 0)
    except ValueError:
        end_timestamp = 0
    if start_timestamp > 0:
        conditions.append("e.created_at >= ?")
        params.append(start_timestamp)
    if end_timestamp > 0:
        conditions.append("e.created_at <= ?")
        params.append(end_timestamp)

    model_name = str(query.get("model_name") or "").strip()
    if model_name:
        conditions.append(
            "(e.requested_model = ? or e.upstream_model = ? or e.path like ?)"
        )
        params.extend([model_name, model_name, f"%{model_name}%"])

    token_name = str(query.get("token_name") or "").strip()
    if token_name:
        conditions.append("e.device_id like ?")
        params.append(f"%{token_name}%")

    group = str(query.get("group") or "").strip()
    if group:
        conditions.append("coalesce(e.plan_id, '') = ?")
        params.append(group)

    try:
        log_type = int(query.get("type") or 0)
    except ValueError:
        log_type = 0
    if log_type == 2:
        conditions.append("e.status_code < 400")
    elif log_type == 5:
        conditions.append("e.status_code >= 400")
    elif log_type not in {0, 2, 5}:
        # This gateway only has consumption (2) and error (5) log types.
        # New API's "Top-up"/recharge type has no backing data here, so it is
        # explicitly unsupported and returns an empty page instead of
        # silently matching unrelated rows.
        conditions.append("1 = 0")

    return " and ".join(conditions), params


def btf_usage_window_stats(user_id: int, window_seconds: int = 60) -> dict[str, int]:
    """Aggregate a customer's successful requests over a recent time window.

    Used for rate-style statistics (rpm/tpm) so they reflect the last
    ``window_seconds`` rather than all-time totals.
    """
    now = now_ts()
    window_start = now - max(int(window_seconds), 0)
    with connect(settings.database_path) as conn:
        row = conn.execute(
            """
            select count(*) as requests,
                   coalesce(sum(e.total_tokens), 0) as tokens
            from usage_events e
            join devices d on d.device_id = e.device_id
            where coalesce(e.user_id, d.user_id) = ?
              and e.created_at >= ?
              and e.created_at <= ?
              and e.status_code < 400
            """,
            (user_id, window_start, now),
        ).fetchone()
    requests = int(row["requests"] or 0)
    tokens = int(row["tokens"] or 0)
    return {"rpm": requests, "tpm": tokens}


def btf_usage_model(path: str, requested_model: str, upstream_model: str) -> str:
    return (
        requested_model
        or upstream_model
        or ("btf-image" if "/images/" in path else "btf-chat" if "/chat/" in path else path.rsplit("/", 1)[-1])
    )


def btf_usage_log_row(row: dict[str, Any], username: str) -> dict[str, Any]:
    path = str(row.get("path") or "")
    requested_model = str(row.get("requested_model") or "")
    upstream_model = str(row.get("upstream_model") or "")
    status_code = int(row.get("status_code") or 0)
    total_tokens = int(row.get("total_tokens") or 0)
    log_type = 2 if status_code < 400 else 5
    other = {
        "btf": True,
        "device_id": row.get("device_id") or "",
        "device_numeric_id": int(row.get("device_numeric_id") or 0),
        "plan_id": row.get("plan_id") or "",
        "status_code": status_code,
        "upstream_model": upstream_model,
        "estimated_cost": float(row.get("estimated_cost") or 0),
    }
    return {
        "id": int(row["id"]),
        "user_id": int(row.get("effective_user_id") or row.get("user_id") or 0),
        "created_at": int(row.get("created_at") or 0),
        "type": log_type,
        "content": f"{path} · {row.get('device_id') or ''}",
        "username": username,
        "token_name": row.get("device_id") or "",
        "model_name": btf_usage_model(path, requested_model, upstream_model),
        # BTF is subscription/token based. The New API quota fields are
        # populated with token units so existing charts remain meaningful.
        "quota": total_tokens,
        "prompt_tokens": int(row.get("prompt_tokens") or 0),
        "completion_tokens": int(row.get("completion_tokens") or 0),
        "use_time": 0,
        "is_stream": False,
        "channel": 0,
        "channel_name": "BTF Device Gateway",
        "token_id": int(row.get("device_numeric_id") or 0),
        "group": row.get("plan_id") or "",
        "ip": "",
        "other": json.dumps(other, ensure_ascii=False, separators=(",", ":")),
        "request_id": f"btf-usage-{int(row['id'])}",
        "upstream_request_id": "",
    }


def btf_usage_events(
    user_id: int,
    request: Request,
    *,
    successful_only: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where, params = btf_usage_where(user_id, request)
    if successful_only:
        where += " and e.status_code < 400"
    pagination = ""
    if limit is not None:
        pagination = " limit ? offset ?"
    with connect(settings.database_path) as conn:
        rows = conn.execute(
            f"""
            select e.*, d.id as device_numeric_id,
                   coalesce(e.user_id, d.user_id) as effective_user_id
            from usage_events e
            join devices d on d.device_id = e.device_id
            where {where}
            order by e.created_at desc, e.id desc{pagination}
            """,
            (*params, limit, offset) if limit is not None else params,
        ).fetchall()
    return [dict(row) for row in rows]


def btf_usage_count(user_id: int, request: Request) -> int:
    where, params = btf_usage_where(user_id, request)
    with connect(settings.database_path) as conn:
        row = conn.execute(
            f"""
            select count(*) as total
            from usage_events e
            join devices d on d.device_id = e.device_id
            where {where}
            """,
            params,
        ).fetchone()
    return int(row["total"] or 0)


def btf_usage_summary(user_id: int) -> dict[str, int]:
    with connect(settings.database_path) as conn:
        row = conn.execute(
            """
            select count(*) as requests,
                   coalesce(sum(e.total_tokens), 0) as tokens,
                   coalesce(sum(case when e.status_code < 400 then e.total_tokens else 0 end), 0) as successful_tokens
            from usage_events e
            join devices d on d.device_id = e.device_id
            where coalesce(e.user_id, d.user_id) = ?
            """,
            (user_id,),
        ).fetchone()
    return {
        "requests": int(row["requests"] or 0),
        "tokens": int(row["tokens"] or 0),
        "successful_tokens": int(row["successful_tokens"] or 0),
    }


def btf_account_summary(user_id: int) -> dict[str, int]:
    usage = btf_usage_summary(user_id)
    with connect(settings.database_path) as conn:
        row = conn.execute(
            """
            select coalesce(sum(p.token_limit), 0) as token_budget,
                   coalesce(sum((
                       select sum(e.total_tokens)
                       from usage_events e
                       where e.device_id = d.device_id
                         and e.status_code < 400
                         and e.created_at >= coalesce(d.plan_started_at, d.created_at)
                   )), 0) as current_tokens,
                   coalesce(sum((
                       select count(*)
                       from usage_events e
                       where e.device_id = d.device_id
                         and e.status_code < 400
                         and e.created_at >= coalesce(d.plan_started_at, d.created_at)
                   )), 0) as current_requests
            from devices d
            join plans p on p.id = d.plan_id
            where d.user_id = ?
              and d.status = 'active'
              and d.expires_at > ?
            """,
            (user_id, now_ts()),
        ).fetchone()
    token_budget = int(row["token_budget"] or 0)
    current_tokens = int(row["current_tokens"] or 0)
    return {
        **usage,
        "current_tokens": current_tokens,
        "current_requests": int(row["current_requests"] or 0),
        "quota": max(token_budget - current_tokens, 0),
    }


@app.get("/api/log/self")
async def api_customer_usage_logs(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    try:
        page = max(int(request.query_params.get("p") or 1), 1)
    except ValueError:
        page = 1
    try:
        page_size = max(min(int(request.query_params.get("page_size") or 20), 200), 1)
    except ValueError:
        page_size = 20
    start = (page - 1) * page_size
    # Paginate in SQL so a large history is not loaded into memory for a
    # single page, while a separate count keeps the total stable.
    events = btf_usage_events(
        dashboard_user.id,
        request,
        limit=page_size,
        offset=start,
    )
    items = [
        btf_usage_log_row(row, dashboard_user.username)
        for row in events
    ]
    return {
        "success": True,
        "message": "",
        "data": {
            "items": items,
            "total": btf_usage_count(dashboard_user.id, request),
            "page": page,
            "page_size": page_size,
        },
    }


@app.get("/api/log/self/stat")
async def api_customer_usage_log_stats(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    # ``quota`` is the lifetime successful-token total; ``rpm``/``tpm`` are the
    # requests and tokens observed in the last 60 seconds (matching New API's
    # rate semantics) rather than an all-time aggregate.
    window = btf_usage_window_stats(dashboard_user.id, 60)
    events = btf_usage_events(dashboard_user.id, request, successful_only=True)
    return {
        "success": True,
        "message": "",
        "data": {
            "quota": sum(int(row.get("total_tokens") or 0) for row in events),
            "rpm": window["rpm"],
            "tpm": window["tpm"],
        },
    }


@app.get("/api/data/self")
async def api_customer_usage_data(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    events = btf_usage_events(dashboard_user.id, request, successful_only=True)
    grouped: dict[tuple[int, str], dict[str, Any]] = {}
    for row in events:
        timestamp = int(row.get("created_at") or 0)
        bucket = timestamp - timestamp % 3600
        model = btf_usage_model(
            str(row.get("path") or ""),
            str(row.get("requested_model") or ""),
            str(row.get("upstream_model") or ""),
        )
        key = (bucket, model)
        item = grouped.setdefault(
            key,
            {
                "user_id": dashboard_user.id,
                "username": dashboard_user.username,
                "model_name": model,
                "created_at": bucket,
                "token_used": 0,
                "count": 0,
                "quota": 0,
            },
        )
        tokens = int(row.get("total_tokens") or 0)
        item["token_used"] += tokens
        item["quota"] += tokens
        item["count"] += 1
    return {"success": True, "message": "", "data": sorted(
        grouped.values(), key=lambda item: (item["created_at"], item["model_name"])
    )}


@app.get("/api/data/flow/self")
async def api_customer_usage_flow(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    events = btf_usage_events(dashboard_user.id, request, successful_only=True)
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in events:
        device_id = str(row.get("device_id") or "")
        model = btf_usage_model(
            str(row.get("path") or ""),
            str(row.get("requested_model") or ""),
            str(row.get("upstream_model") or ""),
        )
        plan_id = str(row.get("plan_id") or "")
        key = (device_id, plan_id, model)
        item = grouped.setdefault(
            key,
            {
                "user_id": dashboard_user.id,
                "username": dashboard_user.username,
                "node_name": "BTF Device Gateway",
                "token_id": int(row.get("device_numeric_id") or 0),
                "token_name": device_id,
                "use_group": plan_id or device_id,
                "channel_id": 0,
                "channel_name": "",
                "model_name": model,
                "token_used": 0,
                "count": 0,
                "quota": 0,
            },
        )
        tokens = int(row.get("total_tokens") or 0)
        item["token_used"] += tokens
        item["quota"] += tokens
        item["count"] += 1
    return {"success": True, "message": "", "data": sorted(
        grouped.values(), key=lambda item: item["quota"], reverse=True
    )}


@app.get("/api/device/users/usage")
async def api_list_device_user_usage(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    items = user_usage_rows(None if dashboard_user.is_admin else dashboard_user.id)
    return {"success": True, "message": "", "data": {"items": items, "total": len(items)}}


@app.put("/api/device/users/{user_id}/limits")
async def api_update_device_user_limits(
    user_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    require_admin(dashboard_user)
    payload = await request.json()
    fields = {
        "request_limit": 0,
        "daily_request_limit": 0,
        "token_limit": 0,
        "daily_token_limit": 0,
        "rpm_limit": 0,
        "concurrency_limit": 0,
    }
    with connect(settings.database_path) as conn:
        row = conn.execute("select * from users where id = ?", (user_id,)).fetchone()
        if not row:
            return {"success": False, "message": "user not found"}
        current = dict(row)
        values = {
            field: max(int_or_400(payload.get(field, current.get(field) or default), field), 0)
            for field, default in fields.items()
        }
        conn.execute(
            """
            update users
            set request_limit = ?,
                daily_request_limit = ?,
                token_limit = ?,
                daily_token_limit = ?,
                rpm_limit = ?,
                concurrency_limit = ?,
                updated_at = ?
            where id = ?
            """,
            (
                values["request_limit"],
                values["daily_request_limit"],
                values["token_limit"],
                values["daily_token_limit"],
                values["rpm_limit"],
                values["concurrency_limit"],
                now_ts(),
                user_id,
            ),
        )
    return {"success": True, "message": "", "data": user_usage_rows(user_id)[0]}


@app.post("/api/device/payments/{provider_name}/notify")
async def api_payment_notify(
    provider_name: str,
    request: Request,
    x_device_key: str | None = Header(default=None),
) -> Response:
    body = await request.body()
    try:
        provider = get_payment_provider(provider_name, settings)
        headers = dict(request.headers)
        # Real payment gateways sign each callback with the merchant/platform
        # key, so a URL secret is unnecessary for them. The demo provider signs
        # with a shared secret the client would otherwise have to embed, so we
        # additionally require the order owner's device key to authorise it.
        if provider_name.strip().lower() == "mock":
            _require_mock_notify_device_key(request, x_device_key, body)
        notification = provider.parse_notification(body, headers)
        order = apply_payment_notification(settings.database_path, notification)
    except PaymentNotConfigured as exc:
        return JSONResponse(status_code=503, content={"success": False, "message": str(exc)})
    except (PaymentSignatureError, PaymentProviderError, DeviceError) as exc:
        return JSONResponse(status_code=400, content={"success": False, "message": str(exc)})
    if provider_name.lower() == "alipay":
        return PlainTextResponse("success")
    return JSONResponse(
        status_code=200,
        content={"code": "SUCCESS", "message": "", "order_id": order["id"]},
    )


def _request_secret(request: Request, explicit_key: str | None) -> str:
    if explicit_key:
        return explicit_key.strip()
    authorization = request.headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return ""


def _require_mock_notify_device_key(
    request: Request,
    explicit_key: str | None,
    body: bytes,
) -> None:
    candidate = _request_secret(request, explicit_key)
    if not candidate:
        raise DeviceError("mock notification requires a device key")
    device = get_device_by_key(settings.database_path, candidate)
    try:
        payload = json.loads(body.decode())
        order_id = str(payload.get("order_id") or "")
    except (ValueError, json.JSONDecodeError):
        raise DeviceError("invalid mock notification payload")
    if not order_id:
        raise DeviceError("mock notification order_id is required")
    with connect(settings.database_path) as conn:
        order = conn.execute(
            "select device_id from orders where id = ?",
            (order_id,),
        ).fetchone()
    if not order or order["device_id"] != device["device_id"]:
        raise DeviceError("order does not belong to this device")


@app.post("/api/device/payments/mock/complete/{provider_order_id}")
async def api_complete_mock_payment(
    provider_order_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    # Demo-only endpoint. Hard 404 in production so the mock settlement path is
    # unreachable when DEMO_PAYMENT_ENABLED is false.
    if not settings.demo_payment_enabled:
        raise HTTPException(status_code=404, detail="mock payment is disabled")
    try:
        # Require the device key of the order's owner; without it anyone could
        # settle someone else's order.
        device = get_device_by_key(settings.database_path, bearer_token(authorization))
        provider = MockPaymentProvider(settings.mock_payment_secret)
        with connect(settings.database_path) as conn:
            row = conn.execute(
                """
                select * from payment_transactions
                where provider = 'mock' and provider_order_id = ?
                """,
                (provider_order_id,),
            ).fetchone()
            if not row:
                raise PaymentProviderError("mock payment transaction not found")
            order = conn.execute(
                "select device_id from orders where id = ?",
                (row["order_id"],),
            ).fetchone()
        if not order or order["device_id"] != device["device_id"]:
            raise DeviceError("order does not belong to this device")
        payload = {
            "order_id": row["order_id"],
            "provider_order_id": row["provider_order_id"],
            "status": "paid",
            "amount": row["amount"],
            "currency": row["currency"],
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        notification = provider.parse_notification(
            body,
            {"x-btf-signature": provider.sign_callback(settings.mock_payment_secret, payload)},
        )
        # Settle against the stored transaction amount, not the request body.
        settled = apply_payment_notification(
            settings.database_path,
            notification,
            authorized_amount=row["amount"],
            authorized_currency=row["currency"],
        )
    except PaymentError as exc:
        return {"success": False, "message": str(exc)}
    except DeviceError as exc:
        return {"success": False, "message": str(exc)}
    return {"success": True, "message": "", "data": settled}


@app.get("/api/device/{device_id}")
async def api_get_device(
    device_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    with connect(settings.database_path) as conn:
        row = conn.execute(
            """
            select d.*, u.username as owner_username, u.display_name as owner_display_name,
                   p.title as plan_title, p.allow_deepseek, p.allow_image
            from devices d
            left join users u on u.id = d.user_id
            left join plans p on p.id = d.plan_id
            where d.id = ?
            """,
            (device_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="device not found")
    device = dict(row)
    require_owned_device(device, dashboard_user)
    return {"success": True, "message": "", "data": device_row_to_new_api(device)}


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_usage(payload: Any) -> tuple[int, int, int]:
    """Return (prompt, completion, total) tokens from an OpenAI-style payload."""
    if not isinstance(payload, dict):
        return 0, 0, 0
    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        return 0, 0, 0
    prompt = _safe_int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = _safe_int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = _safe_int(usage.get("total_tokens") or prompt + completion)
    return prompt, completion, total


def usage_from_payload_bytes(payload: bytes) -> tuple[int, int, int]:
    try:
        data = json.loads(payload.decode("utf-8"))
    except Exception:
        return 0, 0, 0
    return normalize_usage(data)


def extract_usage(payload: bytes, content_type: str) -> tuple[int, int, int]:
    if "application/json" not in content_type.lower():
        return 0, 0, 0
    return usage_from_payload_bytes(payload)


def request_wants_stream(body: bytes, content_type: str) -> bool:
    """Detect an explicit client request for streaming.

    Conservative: any parse failure or non-JSON content type means "not
    streaming", so the legacy buffered behaviour is preserved.
    """
    if "application/json" not in (content_type or "").lower():
        return False
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and data.get("stream") is True


def upstream_response_is_stream(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    return "text/event-stream" in content_type.lower()


def extract_stream_usage(buffer: str) -> tuple[int, int, int]:
    """Parse the last ``data:`` usage block seen in a buffered SSE tail.

    Scans from the most recent line backwards so the final usage chunk wins,
    which is what ``stream_options.include_usage`` providers emit last.
    """
    for raw_line in reversed(buffer.splitlines()):
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            continue
        prompt, completion, total = usage_from_payload_bytes(data.encode("utf-8"))
        if total or prompt or completion:
            return prompt, completion, total
    return 0, 0, 0


async def stream_proxy_body(
    upstream: httpx.Response,
    buffer: list[bytearray],
    on_complete: Callable[[], None] | None = None,
) -> AsyncIterator[bytes]:
    """Yield upstream SSE bytes verbatim, keeping a bounded usage tail.

    ``buffer`` accumulates the last ``SSE_USAGE_TAIL_BYTES`` of the body so the
    final usage chunk can be parsed after the stream completes without
    allocating the whole response in memory. ``on_complete`` (if given) runs
    once the generator ends, including on client disconnect / cancellation.
    """
    try:
        tail = bytearray()
        async for chunk in upstream.aiter_bytes():
            if not chunk:
                continue
            yield chunk
            tail.extend(chunk)
            if len(tail) > SSE_USAGE_TAIL_BYTES:
                del tail[: len(tail) - SSE_USAGE_TAIL_BYTES]
        buffer[0] = tail
    finally:
        # Guarantee the connection is released even if the client disconnects.
        await upstream.aclose()
        if on_complete is not None:
            on_complete()


def estimated_request_cost(
    path: str,
    prompt_tokens: int,
    completion_tokens: int,
    upstream_model: str,
) -> float:
    first = path.split("/", 1)[0]
    if first == "images":
        return float(settings.image_cost_per_request)
    if upstream_model in DEEPSEEK_MODELS or first in {"chat", "completions", "responses"}:
        return round(
            (prompt_tokens / 1_000_000) * settings.chat_prompt_cost_per_million
            + (completion_tokens / 1_000_000) * settings.chat_completion_cost_per_million,
            8,
        )
    return 0.0


class UpstreamHeaders:
    """Header view for upstream chunks that must not drop duplicates.

    Starlette/httpx merge repeated headers into a comma separated value, which
    breaks ``Set-Cookie``. ``init_headers`` only relies on ``.items()`` so we
    expose the raw pairs from ``multi_items()`` instead.
    """

    __slots__ = ("_items",)

    def __init__(self, items: list[tuple[str, str]]) -> None:
        self._items = items

    def items(self) -> list[tuple[str, str]]:
        return list(self._items)


def proxy_response_headers(headers: httpx.Headers) -> UpstreamHeaders:
    """Forward upstream response headers while preserving duplicate values."""
    return UpstreamHeaders(
        [
            (key, value)
            for key, value in headers.multi_items()
            if key.lower()
            not in {"content-encoding", "transfer-encoding", "connection", "content-length"}
        ]
    )


_HASHED_ASSET_RE = re.compile(r"[.-][0-9a-fA-F]{8,}\.[A-Za-z0-9]+$")
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
NO_CACHE_CONTROL = "no-cache, must-revalidate"


def static_cache_control(path: str) -> str:
    """Pick a Cache-Control policy for a served frontend file.

    Content-hashed build artifacts never change, so they are safe to cache
    forever. HTML entry points must always be revalidated so clients pick up
    new asset hashes.
    """
    normalized = path.replace("\\", "/").lstrip("/")
    if normalized.endswith(".html") or normalized == "":
        return NO_CACHE_CONTROL
    filename = normalized.rsplit("/", 1)[-1]
    if _HASHED_ASSET_RE.search(filename):
        return IMMUTABLE_CACHE_CONTROL
    return NO_CACHE_CONTROL


def upstream_proxy_headers(request: Request) -> dict[str, str]:
    """Forward the request while preserving the actual client address."""
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower()
        not in {
            "host",
            "content-length",
            "x-forwarded-for",
            "x-forwarded-host",
            "x-forwarded-proto",
            "x-real-ip",
        }
    }
    client_host = request.client.host if request.client else ""
    if client_host:
        headers["x-forwarded-for"] = client_host
        headers["x-real-ip"] = client_host
    headers["x-forwarded-proto"] = request.url.scheme
    return headers


def cached_device_auth(device_key: str) -> "Any":
    """Authenticate a device key, caching the result briefly.

    The /v1/models path used to authenticate, early-return, then authenticate
    again; caching keeps that flow cheap while still rejecting disabled or
    expired devices within the (short) TTL.
    """
    now = time.monotonic()
    cache_key = (str(settings.database_path), device_key)
    cached = DEVICE_AUTH_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]
    auth = authenticate_device(settings.database_path, device_key)
    if len(DEVICE_AUTH_CACHE) >= 4096:
        # Bound memory: drop entries whose TTL has elapsed before inserting.
        for key in [k for k, (expiry, _) in DEVICE_AUTH_CACHE.items() if expiry <= now]:
            DEVICE_AUTH_CACHE.pop(key, None)
    DEVICE_AUTH_CACHE[cache_key] = (now + DEVICE_AUTH_CACHE_TTL, auth)
    return auth


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_v1(path: str, request: Request, authorization: str | None = Header(default=None)) -> Response:
    if not settings.new_api_token:
        raise HTTPException(status_code=500, detail="gateway missing NEW_API_TOKEN")
    clean_path = clean_v1_proxy_path(path)
    if clean_path is None:
        raise HTTPException(status_code=404, detail="not found")
    # Device authentication and plan/quota checks happen here, before any
    # upstream call or streaming response starts. Streaming requests therefore
    # can never emit bytes before they are authorized.
    auth = cached_device_auth(bearer_token(authorization))
    body = await request.body()
    original_body = body
    requested_model = request_model_from_body(body)
    wants_stream = request_wants_stream(body, request.headers.get("content-type", ""))
    body = enforce_plan_access(auth.device_id, clean_path, request.method, body)
    upstream_model = request_model_from_body(body)
    plan = plan_entitlements_for_device(auth.device_id)
    user_id = int(plan.get("device_user_id") or 0) or None
    plan_id = str(plan.get("device_plan_id") or plan.get("id") or "")
    headers = upstream_proxy_headers(request)
    headers.pop("authorization", None)
    headers["authorization"] = f"Bearer {settings.new_api_token}"
    target = f"{settings.new_api_base_url}/v1/{clean_path}"
    ACTIVE_REQUESTS[auth.device_id] = ACTIVE_REQUESTS.get(auth.device_id, 0) + 1
    if user_id:
        ACTIVE_USER_REQUESTS[user_id] = ACTIVE_USER_REQUESTS.get(user_id, 0) + 1

    # Reuse a shared upstream client (connection pooling). Streaming requests
    # use the client with the longer read ceiling since SSE frames may idle
    # between events. Either way the request is sent un-buffered (stream=True)
    # so that an upstream that answers with text/event-stream can be relayed
    # chunk-by-chunk without ever materialising the whole body.
    client = stream_client() if wants_stream else upstream_client()
    try:
        upstream_request = client.build_request(
            request.method,
            target,
            content=body,
            params=request.query_params,
            headers=headers,
        )
        upstream = await client.send(upstream_request, stream=True)
    except BaseException:
        _release_active_request(auth.device_id, user_id)
        raise
    content_type = upstream.headers.get("content-type", "")

    if upstream.status_code < 400 and upstream_response_is_stream(upstream):
        # Stream SSE bytes straight through. Usage is parsed from the buffered
        # tail afterwards. The concurrency slot stays reserved for the whole
        # streamed response (matching the previous "hold during request"
        # semantics) and is released when the body generator finishes or is
        # cancelled.
        stream_buffer: list[bytearray] = [bytearray()]
        return StreamingResponse(
            stream_proxy_body(
                upstream,
                stream_buffer,
                on_complete=lambda: _release_active_request(auth.device_id, user_id),
            ),
            status_code=upstream.status_code,
            media_type=content_type or "text/event-stream",
            headers=proxy_response_headers(upstream.headers),
            background=BackgroundTask(
                _finalize_stream_usage,
                stream_buffer,
                auth.device_id,
                clean_path,
                user_id,
                plan_id,
                requested_model,
                upstream_model,
            ),
        )

    # Non-streaming (or failed) response: read the body, record usage (cost 0
    # on upstream errors) and answer exactly as before.
    try:
        content = await upstream.aread()
    finally:
        await upstream.aclose()
    _release_active_request(auth.device_id, user_id)
    return await _record_and_build_response(
        request,
        clean_path,
        auth.device_id,
        user_id,
        plan_id,
        requested_model,
        upstream_model,
        upstream.status_code,
        content,
        content_type,
        upstream.headers,
        original_body,
    )


def _release_active_request(device_id: str, user_id: int | None) -> None:
    ACTIVE_REQUESTS[device_id] = max(ACTIVE_REQUESTS.get(device_id, 1) - 1, 0)
    if ACTIVE_REQUESTS[device_id] == 0:
        ACTIVE_REQUESTS.pop(device_id, None)
    if user_id:
        ACTIVE_USER_REQUESTS[user_id] = max(ACTIVE_USER_REQUESTS.get(user_id, 1) - 1, 0)
        if ACTIVE_USER_REQUESTS[user_id] == 0:
            ACTIVE_USER_REQUESTS.pop(user_id, None)


async def _record_and_build_response(
    request: Request,
    clean_path: str,
    device_id: str,
    user_id: int | None,
    plan_id: str,
    requested_model: str,
    upstream_model: str,
    status_code: int,
    content_bytes: bytes,
    content_type: str,
    upstream_headers: httpx.Headers,
    original_body: bytes,
) -> Response:
    prompt, completion, total = extract_usage(content_bytes, content_type)
    # A failed upstream response must not be billed; the event is still
    # recorded so the request is auditable and counted as a request.
    estimated_cost = (
        0.0
        if status_code >= 400
        else estimated_request_cost(clean_path, prompt, completion, upstream_model)
    )
    usage_event_id = record_usage(
        settings.database_path,
        device_id,
        f"/v1/{clean_path}",
        status_code,
        prompt,
        completion,
        total,
        user_id,
        plan_id,
        requested_model,
        upstream_model,
        estimated_cost,
    )
    content = content_bytes
    if status_code < 400 and clean_path == "images/generations":
        content = await persist_image_generation_response(
            content,
            content_type,
            device_id,
            user_id,
            plan_id,
            requested_model,
            upstream_model,
            original_body,
            usage_event_id,
        )
    if request.method == "GET" and clean_path == "models" and status_code == 200:
        content = filter_models_response(content, content_type, device_id)
    elif status_code < 400:
        content = alias_model_response(content, content_type, device_id)
    return Response(
        content=content,
        status_code=status_code,
        headers=proxy_response_headers(upstream_headers),
        media_type=upstream_headers.get("content-type"),
    )


async def _finalize_stream_usage(
    stream_buffer: list[bytearray],
    device_id: str,
    clean_path: str,
    user_id: int | None,
    plan_id: str,
    requested_model: str,
    upstream_model: str,
) -> None:
    """Record usage for a streamed response after all bytes were forwarded.

    Usage comes from the last ``data:`` usage block when the upstream honours
    ``stream_options.include_usage``; otherwise the request is still recorded
    with zero tokens so streaming never silently disappears from the ledger.
    """
    raw = bytes(stream_buffer[0]) if stream_buffer else b""
    prompt, completion, total = extract_stream_usage(raw.decode("utf-8", "replace"))
    estimated_cost = estimated_request_cost(clean_path, prompt, completion, upstream_model)
    record_usage(
        settings.database_path,
        device_id,
        f"/v1/{clean_path}",
        200,
        prompt,
        completion,
        total,
        user_id,
        plan_id,
        requested_model,
        upstream_model,
        estimated_cost,
    )



def augment_customer_self_response(content: bytes, content_type: str) -> bytes:
    if "application/json" not in content_type.lower():
        return content
    try:
        payload = json.loads(content.decode("utf-8"))
    except Exception:
        return content
    if not isinstance(payload, dict) or not payload.get("success"):
        return content
    user = payload.get("data")
    if not isinstance(user, dict):
        return content
    try:
        user_id = int(user.get("id") or 0)
        role = int(user.get("role") or 0)
    except (TypeError, ValueError):
        return content
    if user_id <= 0 or role >= ADMIN_ROLE:
        return content

    summary = btf_account_summary(user_id)
    user["quota"] = summary["quota"]
    user["used_quota"] = summary["current_tokens"]
    user["request_count"] = summary["current_requests"]
    user["btf_usage"] = {
        "tokens": summary["tokens"],
        "successful_tokens": summary["successful_tokens"],
        "active_tokens": summary["current_tokens"],
        "active_requests": summary["current_requests"],
        "active_token_budget": max(summary["quota"] + summary["current_tokens"], 0),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@app.api_route(
    "/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy_new_api(path: str, request: Request) -> Response:
    raw_path = path or ""

    # The SPA entry point is the only request that maps to an empty path.
    if request.method in {"GET", "HEAD"} and raw_path.strip("/") == "":
        if frontend_dist.exists():
            index_file = frontend_dist / "index.html"
            if index_file.is_file():
                return FileResponse(
                    index_file,
                    headers={"Cache-Control": static_cache_control("index.html")},
                )
        raise HTTPException(status_code=404, detail="not found")

    # Reject traversal / absolute / malformed encodings before anything else so
    # unauthenticated callers cannot steer httpx to an arbitrary upstream path.
    clean_path = clean_new_api_proxy_path(raw_path)
    if clean_path is None:
        raise HTTPException(status_code=404, detail="not found")

    if request.method in {"GET", "HEAD"} and frontend_dist.exists():
        first = clean_path.split("/", 1)[0]
        if first not in {"api", "v1", "health"}:
            static_asset_roots = {"assets", "favicon.ico", "static"}
            target_file = frontend_file_for_path(clean_path)
            if target_file is None:
                raise HTTPException(status_code=404, detail="not found")
            if target_file.is_file():
                return FileResponse(
                    target_file,
                    headers={"Cache-Control": static_cache_control(clean_path)},
                )
            if first in static_asset_roots:
                raise HTTPException(status_code=404, detail="not found")
            index_file = frontend_dist / "index.html"
            if index_file.is_file():
                return FileResponse(
                    index_file,
                    headers={"Cache-Control": static_cache_control("index.html")},
                )

    if not settings.new_api_token:
        raise HTTPException(status_code=500, detail="gateway missing NEW_API_TOKEN")

    body = await request.body()
    headers = upstream_proxy_headers(request)
    target = f"{settings.new_api_base_url}/{clean_path}"
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=False) as client:
        upstream = await client.request(
            request.method,
            target,
            content=body,
            params=request.query_params,
            headers=headers,
        )
    content = upstream.content
    if request.method == "GET" and clean_path == "api/user/self" and upstream.status_code == 200:
        content = augment_customer_self_response(
            content,
            upstream.headers.get("content-type", ""),
        )
    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=proxy_response_headers(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )


@app.exception_handler(DeviceError)
async def device_error_handler(_: Request, exc: DeviceError) -> JSONResponse:
    return JSONResponse(status_code=403, content={"error": str(exc), "timestamp": now_ts()})
