from __future__ import annotations

import json
import re
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from app.config import load_settings
from app.db import connect
from app.db import now_ts
from app.service import (
    DeviceError,
    approve_order,
    apply_payment_notification,
    authenticate_device,
    create_order,
    ensure_ready,
    get_order,
    get_device_by_key,
    list_orders,
    list_usage,
    plans,
    record_usage,
    save_payment_intent,
)
from app.security import hash_key
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
SAFE_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~:-]+$")
DEEPSEEK_MODELS = {"deepseek-v4.1-flash"}
DEFAULT_IMAGE_MODELS = {"[b]gpt-image-2"}
DEFAULT_IMAGE_MODEL_TEXT = "[b]gpt-image-2"


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
            select p.*
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


def allowed_models_for_plan(plan: dict[str, Any]) -> set[str]:
    allowed: set[str] = set()
    if int(plan.get("allow_deepseek") or 0):
        allowed.update(split_model_list(plan.get("chat_models")) or DEEPSEEK_MODELS)
    if int(plan.get("allow_image") or 0):
        allowed.update(split_model_list(plan.get("image_models")) or DEFAULT_IMAGE_MODELS)
    return allowed


def enforce_plan_access(device_id: str, path: str, method: str, body: bytes) -> None:
    if method == "OPTIONS":
        return
    plan = plan_entitlements_for_device(device_id)
    first = path.split("/", 1)[0]
    if first == "models":
        return
    model = request_model_from_body(body)
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    if first == "images":
        if not int(plan.get("allow_image") or 0):
            raise HTTPException(status_code=403, detail="current device plan does not include GPT image generation")
        if model not in (split_model_list(plan.get("image_models")) or DEFAULT_IMAGE_MODELS):
            raise HTTPException(status_code=403, detail="image model is not included in current device plan")
        return
    if first in {"chat", "completions", "responses"}:
        if not int(plan.get("allow_deepseek") or 0):
            raise HTTPException(status_code=403, detail="current device plan does not include DeepSeek")
        if model not in (split_model_list(plan.get("chat_models")) or DEEPSEEK_MODELS):
            raise HTTPException(status_code=403, detail="chat model is not included in current device plan")
        return
    if model not in allowed_models_for_plan(plan):
        raise HTTPException(status_code=403, detail="model is not included in current device plan")


def filter_models_response(content: bytes, content_type: str, device_id: str) -> bytes:
    if "application/json" not in content_type.lower():
        return content
    try:
        payload = json.loads(content.decode("utf-8"))
    except Exception:
        return content
    if not isinstance(payload.get("data"), list):
        return content
    allowed = allowed_models_for_plan(plan_entitlements_for_device(device_id))
    payload["data"] = [
        item for item in payload["data"]
        if str(item.get("id") or "") in allowed
    ]
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


def normalize_mac(value: Any) -> str:
    return str(value or "").strip().upper()


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


def device_row_to_new_api(row: dict[str, Any]) -> dict[str, Any]:
    note = row.get("note") or ""
    name = note
    plan = ""
    if " / " in note:
        name, plan = note.split(" / ", 1)
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
        "status": row["status"],
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


def new_api_page(items: list[dict[str, Any]], page: int, page_size: int) -> dict[str, Any]:
    start = max(page - 1, 0) * page_size
    end = start + page_size
    return {
        "items": items[start:end],
        "total": len(items),
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
    where: list[str] = []
    params: list[Any] = []
    if not dashboard_user.is_admin:
        where.append("d.user_id = ?")
        params.append(dashboard_user.id)
    query = """
        select d.*, u.username as owner_username, u.display_name as owner_display_name,
               p.title as plan_title, p.allow_deepseek, p.allow_image
        from devices d
        left join users u on u.id = d.user_id
        left join plans p on p.id = d.plan_id
    """
    if where:
        query += " where " + " and ".join(where)
    query += " order by d.id desc"
    with connect(settings.database_path) as conn:
        rows = [dict(row) for row in conn.execute(query, params).fetchall()]
    items = [device_row_to_new_api(row) for row in rows]
    if dashboard_user.is_admin and user_id > 0:
        items = [item for item in items if item["user_id"] == user_id]
    if status:
        items = [item for item in items if item["status"] == status]
    if keyword:
        needle = keyword.lower()
        items = [
            item
            for item in items
            if needle in item["mac"].lower()
            or needle in item["name"].lower()
            or needle in item["username"].lower()
            or needle in item["remark"].lower()
        ]
    sort_fields = {
        "id",
        "mac",
        "user_id",
        "status",
        "expires_at",
        "last_seen_at",
        "request_count",
        "created_at",
    }
    if sort_by in sort_fields:
        items.sort(key=lambda item: item.get(sort_by) or 0, reverse=sort_order != "asc")
    return {"success": True, "message": "", "data": new_api_page(items, p, page_size)}


@app.post("/api/device/")
async def api_create_device(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    require_admin(dashboard_user)
    payload = await request.json()
    user_id = int(payload.get("user_id") or 0)
    mac = normalize_mac(payload.get("mac"))
    expires_at = int(payload.get("expires_at") or 0)
    status = str(payload.get("status") or "active").strip() or "active"
    plan_id = plan_id_from_text(str(payload.get("plan_id") or payload.get("plan") or "day_1"))
    if not user_id or not mac or not expires_at:
        return {"success": False, "message": "invalid params"}
    if status not in {"active", "disabled"}:
        return {"success": False, "message": "invalid status"}
    note = compose_note(
        str(payload.get("name") or ""),
        str(payload.get("plan") or ""),
        str(payload.get("remark") or ""),
    )
    ts = now_ts()
    with connect(settings.database_path) as conn:
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
                  created_at, updated_at, note, plan_id
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (mac, user_id, hash_key(mac), mac, status, expires_at, ts, ts, note, plan_id),
            )
        except Exception as exc:
            return {"success": False, "message": str(exc)}
    return {"success": True, "message": "", "data": {"created": True}}


@app.put("/api/device/")
async def api_update_device(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    require_admin(dashboard_user)
    payload = await request.json()
    device_id = int(payload.get("id") or 0)
    user_id = int(payload.get("user_id") or 0)
    mac = normalize_mac(payload.get("mac"))
    expires_at = int(payload.get("expires_at") or 0)
    status = str(payload.get("status") or "active").strip() or "active"
    plan_id = plan_id_from_text(str(payload.get("plan_id") or payload.get("plan") or "day_1"))
    if not device_id or not user_id or not mac or not expires_at:
        return {"success": False, "message": "invalid params"}
    if status not in {"active", "disabled"}:
        return {"success": False, "message": "invalid status"}
    note = compose_note(
        str(payload.get("name") or ""),
        str(payload.get("plan") or ""),
        str(payload.get("remark") or ""),
    )
    ts = now_ts()
    with connect(settings.database_path) as conn:
        user = conn.execute("select id from users where id = ?", (user_id,)).fetchone()
        if not user:
            return {"success": False, "message": "user not found"}
        plan = conn.execute("select id from plans where id = ? and enabled = 1", (plan_id,)).fetchone()
        if not plan:
            return {"success": False, "message": "plan not found"}
        row = conn.execute("select id from devices where id = ?", (device_id,)).fetchone()
        if not row:
            return {"success": False, "message": "device not found"}
        try:
            conn.execute(
                """
                update devices
                set device_id = ?, user_id = ?, key_hash = ?, key_mask = ?,
                    status = ?, expires_at = ?, updated_at = ?, note = ?, plan_id = ?
                where id = ?
                """,
                (mac, user_id, hash_key(mac), mac, status, expires_at, ts, note, plan_id, device_id),
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
    user_id = int(payload.get("user_id") or 0)
    macs = split_macs(payload.get("macs"))
    expires_at = int(payload.get("expires_at") or 0)
    plan = str(payload.get("plan") or "")
    plan_id = plan_id_from_text(str(payload.get("plan_id") or plan or "day_1"))
    remark = str(payload.get("remark") or "")
    if not user_id or not macs or not expires_at:
        return {"success": False, "message": "invalid params"}
    ts = now_ts()
    created = 0
    failed: list[str] = []
    with connect(settings.database_path) as conn:
        user = conn.execute("select id from users where id = ?", (user_id,)).fetchone()
        if not user:
            return {"success": False, "message": "user not found"}
        plan_row = conn.execute("select id from plans where id = ? and enabled = 1", (plan_id,)).fetchone()
        if not plan_row:
            return {"success": False, "message": "plan not found"}
        for mac in macs:
            note = compose_note(mac, plan, remark)
            try:
                conn.execute(
                    """
                    insert into devices (
                      device_id, user_id, key_hash, key_mask, status, expires_at,
                      created_at, updated_at, note, plan_id
                    )
                    values (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
                    """,
                    (mac, user_id, hash_key(mac), mac, expires_at, ts, ts, note, plan_id),
                )
                created += 1
            except Exception:
                failed.append(mac)
    return {"success": True, "message": "", "data": {"created": created, "failed": failed}}


@app.post("/api/device/manage")
async def api_manage_device(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    dashboard_user = await resolve_dashboard_user(request, authorization)
    require_admin(dashboard_user)
    payload = await request.json()
    device_id = int(payload.get("id") or 0)
    action = payload.get("action")
    expires_at = int(payload.get("expires_at") or 0)
    ts = now_ts()
    with connect(settings.database_path) as conn:
        row = conn.execute("select device_id from devices where id = ?", (device_id,)).fetchone()
        if not row:
            return {"success": False, "message": "device not found"}
        if action == "enable":
            conn.execute("update devices set status = 'active', updated_at = ? where id = ?", (ts, device_id))
        elif action == "disable":
            conn.execute("update devices set status = 'disabled', updated_at = ? where id = ?", (ts, device_id))
        elif action == "renew" and expires_at > 0:
            conn.execute(
                "update devices set expires_at = ?, status = 'active', updated_at = ? where id = ?",
                (expires_at, ts, device_id),
            )
        else:
            return {"success": False, "message": "unsupported action"}
    return {"success": True, "message": "", "data": {"updated": True}}


@app.get("/api/device/plans")
def api_list_device_plans() -> dict[str, Any]:
    return {"success": True, "message": "", "data": {"items": plans(settings.database_path)}}


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
            else current.get("chat_models") or "deepseek-v4.1-flash"
        ).strip()
        image_models = str(
            payload.get("image_models")
            if "image_models" in payload
            else current.get("image_models") or ""
        ).strip()
        if allow_image and not image_models:
            image_models = DEFAULT_IMAGE_MODEL_TEXT
        conn.execute(
            """
            update plans
            set allow_deepseek = ?,
                allow_image = ?,
                chat_models = ?,
                image_models = ?
            where id = ?
            """,
            (allow_deepseek, allow_image, chat_models, image_models, plan_id),
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
        order = create_order(settings.database_path, mac, plan_id)
    except DeviceError as exc:
        return {"success": False, "message": str(exc)}
    return {"success": True, "message": "", "data": order}


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


@app.post("/api/device/payments/{provider_name}/notify")
async def api_payment_notify(provider_name: str, request: Request) -> Response:
    body = await request.body()
    try:
        provider = get_payment_provider(provider_name, settings)
        notification = provider.parse_notification(body, dict(request.headers))
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


@app.post("/api/device/payments/mock/complete/{provider_order_id}")
async def api_complete_mock_payment(provider_order_id: str) -> dict[str, Any]:
    if not settings.demo_payment_enabled:
        raise HTTPException(status_code=404, detail="mock payment is disabled")
    try:
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
        order = apply_payment_notification(settings.database_path, notification)
    except PaymentError as exc:
        return {"success": False, "message": str(exc)}
    return {"success": True, "message": "", "data": order}


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


def extract_usage(payload: bytes, content_type: str) -> tuple[int, int, int]:
    if "application/json" not in content_type.lower():
        return 0, 0, 0
    try:
        data = json.loads(payload.decode("utf-8"))
    except Exception:
        return 0, 0, 0
    usage = data.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or prompt + completion)
    return prompt, completion, total


def proxy_response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in {"content-encoding", "transfer-encoding", "connection", "content-length"}
    }


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


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_v1(path: str, request: Request, authorization: str | None = Header(default=None)) -> Response:
    if not settings.new_api_token:
        raise HTTPException(status_code=500, detail="gateway missing NEW_API_TOKEN")
    clean_path = clean_v1_proxy_path(path)
    if clean_path is None:
        raise HTTPException(status_code=404, detail="not found")
    auth = authenticate_device(settings.database_path, bearer_token(authorization))
    body = await request.body()
    enforce_plan_access(auth.device_id, clean_path, request.method, body)
    headers = upstream_proxy_headers(request)
    headers.pop("authorization", None)
    headers["authorization"] = f"Bearer {settings.new_api_token}"
    target = f"{settings.new_api_base_url}/v1/{clean_path}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        upstream = await client.request(
            request.method,
            target,
            content=body,
            params=request.query_params,
            headers=headers,
        )
    prompt, completion, total = extract_usage(
        upstream.content,
        upstream.headers.get("content-type", ""),
    )
    record_usage(
        settings.database_path,
        auth.device_id,
        f"/v1/{clean_path}",
        upstream.status_code,
        prompt,
        completion,
        total,
    )
    content = upstream.content
    if request.method == "GET" and clean_path == "models" and upstream.status_code == 200:
        content = filter_models_response(
            upstream.content,
            upstream.headers.get("content-type", ""),
            auth.device_id,
        )
    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=proxy_response_headers(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_new_api(path: str, request: Request) -> Response:
    if request.method in {"GET", "HEAD"} and frontend_dist.exists():
        first = path.split("/", 1)[0]
        if first not in {"api", "v1", "health"}:
            if not path:
                index_file = frontend_dist / "index.html"
                if index_file.is_file():
                    return FileResponse(
                        index_file,
                        headers={"Cache-Control": "no-store"},
                    )
            target_file = frontend_file_for_path(path)
            if target_file is None:
                raise HTTPException(status_code=404, detail="not found")
            if target_file.is_file():
                return FileResponse(
                    target_file,
                    headers={"Cache-Control": "no-store"},
                )
            index_file = frontend_dist / "index.html"
            if index_file.is_file():
                return FileResponse(
                    index_file,
                    headers={"Cache-Control": "no-store"},
                )

    body = await request.body()
    headers = upstream_proxy_headers(request)
    target = f"{settings.new_api_base_url}/{path}"
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=False) as client:
        upstream = await client.request(
            request.method,
            target,
            content=body,
            params=request.query_params,
            headers=headers,
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=proxy_response_headers(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )


@app.exception_handler(DeviceError)
async def device_error_handler(_: Request, exc: DeviceError) -> JSONResponse:
    return JSONResponse(status_code=403, content={"error": str(exc), "timestamp": now_ts()})
