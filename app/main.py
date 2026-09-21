from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.config import load_settings
from app.db import now_ts
from app.service import (
    DeviceError,
    authenticate_device,
    create_order,
    ensure_ready,
    get_device,
    plans,
    record_usage,
)

settings = load_settings()
ensure_ready(settings.database_path)

app = FastAPI(title="BTF Device Gateway")


def fmt_ts(value: int | None) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def html_page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ margin: 0; font-family: Arial, sans-serif; background: #f6f8fb; color: #18202a; }}
    header {{ background: #102033; color: white; padding: 18px 24px; }}
    main {{ max-width: 960px; margin: 0 auto; padding: 24px; }}
    form, .panel {{ background: white; border: 1px solid #d9e0ea; border-radius: 8px; padding: 18px; margin: 16px 0; }}
    input, select, button {{ font-size: 16px; padding: 10px; }}
    input, select {{ width: 100%; box-sizing: border-box; margin: 8px 0 14px; border: 1px solid #c8d1dc; border-radius: 6px; }}
    button {{ background: #0f6bff; border: 0; color: white; border-radius: 6px; cursor: pointer; }}
    table {{ width: 100%; border-collapse: collapse; background: white; }}
    th, td {{ text-align: left; border-bottom: 1px solid #e3e8ef; padding: 10px; }}
    .muted {{ color: #687586; }}
    .ok {{ color: #087443; }}
    .bad {{ color: #b42318; }}
    code {{ background: #eef2f7; padding: 2px 5px; border-radius: 4px; }}
  </style>
</head>
<body>
  <header><strong>BTF API</strong> <span class="muted">Device AI Authorization</span></header>
  <main>{body}</main>
</body>
</html>"""
    )


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization.split(" ", 1)[1].strip()


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "btf-device-gateway"}


@app.get("/device", response_class=HTMLResponse)
def device_home() -> HTMLResponse:
    return html_page(
        "BTF API Device",
        """
<h1>设备授权</h1>
<form action="/device/status" method="post">
  <label>设备 API Key</label>
  <input name="device_key" type="password" autocomplete="off" required>
  <button type="submit">查看状态</button>
</form>
""",
    )


@app.post("/device/status", response_class=HTMLResponse)
def device_status_page(device_key: str = Form(...)) -> HTMLResponse:
    try:
        auth = authenticate_device(settings.database_path, device_key)
        device = get_device(settings.database_path, auth.device_id)
    except DeviceError as exc:
        return html_page("设备状态", f"<div class=\"panel bad\">{exc}</div>")
    active = "ok" if auth.is_active else "bad"
    plan_rows = "".join(
        f"<option value=\"{plan['id']}\">{plan['title']} - {plan['price_amount']} {plan['currency']}</option>"
        for plan in plans(settings.database_path)
    )
    return html_page(
        "设备状态",
        f"""
<h1>设备状态</h1>
<div class="panel">
  <p>设备：<code>{device['device_id']}</code></p>
  <p>状态：<strong class="{active}">{device['status']}</strong></p>
  <p>到期：{fmt_ts(device['expires_at'])}</p>
  <p>请求次数：{device['request_count']}</p>
  <p>总 Token：{device['total_tokens']}</p>
</div>
<form action="/device/order" method="post">
  <input name="device_key" type="hidden" value="{device_key}">
  <label>续费套餐</label>
  <select name="plan_id">{plan_rows}</select>
  <button type="submit">创建续费订单</button>
</form>
""",
    )


@app.post("/device/order", response_class=HTMLResponse)
def device_order_page(device_key: str = Form(...), plan_id: str = Form(...)) -> HTMLResponse:
    try:
        order = create_order(settings.database_path, device_key, plan_id)
    except DeviceError as exc:
        return html_page("续费订单", f"<div class=\"panel bad\">{exc}</div>")
    return html_page(
        "续费订单",
        f"""
<h1>续费订单</h1>
<div class="panel">
  <p>订单号：<code>{order['id']}</code></p>
  <p>设备：<code>{order['device_id']}</code></p>
  <p>金额：{order['amount']} {order['currency']}</p>
  <p>状态：{order['status']}</p>
  <p>{settings.payment_instructions}</p>
</div>
""",
    )


@app.get("/api/device/status")
def api_device_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = authenticate_device(settings.database_path, bearer_token(authorization))
    device = get_device(settings.database_path, auth.device_id)
    return {
        "device_id": device["device_id"],
        "status": device["status"],
        "active": auth.is_active,
        "expires_at": device["expires_at"],
        "expires_at_text": fmt_ts(device["expires_at"]),
        "request_count": device["request_count"],
        "total_tokens": device["total_tokens"],
    }


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


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_v1(path: str, request: Request, authorization: str | None = Header(default=None)) -> Response:
    if not settings.new_api_token:
        raise HTTPException(status_code=500, detail="gateway missing NEW_API_TOKEN")
    auth = authenticate_device(settings.database_path, bearer_token(authorization))
    body = await request.body()
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "authorization", "content-length"}
    }
    headers["authorization"] = f"Bearer {settings.new_api_token}"
    target = f"{settings.new_api_base_url}/v1/{path}"
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
        f"/v1/{path}",
        upstream.status_code,
        prompt,
        completion,
        total,
    )
    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in {"content-encoding", "transfer-encoding", "connection"}
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_new_api(path: str, request: Request) -> Response:
    body = await request.body()
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length"}
    }
    target = f"{settings.new_api_base_url}/{path}"
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=False) as client:
        upstream = await client.request(
            request.method,
            target,
            content=body,
            params=request.query_params,
            headers=headers,
        )
    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in {"content-encoding", "transfer-encoding", "connection"}
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


@app.exception_handler(DeviceError)
async def device_error_handler(_: Request, exc: DeviceError) -> JSONResponse:
    return JSONResponse(status_code=403, content={"error": str(exc), "timestamp": now_ts()})

