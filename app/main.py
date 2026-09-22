from __future__ import annotations

import json
from html import escape
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.config import load_settings
from app.db import now_ts
from app.service import (
    DeviceError,
    approve_order,
    approve_order_for_key,
    batch_create_devices,
    authenticate_device,
    create_device,
    create_order,
    disable_device,
    enable_device,
    ensure_ready,
    get_device,
    get_device_by_key,
    get_order,
    list_devices,
    list_orders,
    list_usage,
    plans,
    record_usage,
    renew_device,
    rotate_device_key,
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
    :root {{
      --bg: #f7f8fa;
      --surface: #ffffff;
      --surface-muted: #f9fafb;
      --border: #e5e7eb;
      --text: #111827;
      --muted: #6b7280;
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      --ok: #059669;
      --bad: #dc2626;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      font-size: 14px;
      line-height: 1.5;
    }}
    header {{
      height: 56px;
      display: flex;
      align-items: center;
      gap: 10px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 0 24px;
      position: sticky;
      top: 0;
      z-index: 10;
    }}
    header img {{ width: 30px; height: 30px; border-radius: 8px; }}
    header strong {{ font-size: 15px; font-weight: 650; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
    h1 {{ font-size: 24px; line-height: 1.25; margin: 0 0 18px; font-weight: 650; }}
    h2 {{ font-size: 16px; margin: 0 0 14px; font-weight: 650; }}
    nav {{
      display: flex;
      align-items: center;
      gap: 6px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 6px;
      margin-bottom: 22px;
      overflow-x: auto;
    }}
    nav a {{
      color: #374151;
      padding: 8px 12px;
      border-radius: 6px;
      text-decoration: none;
      white-space: nowrap;
    }}
    nav a:hover {{ background: var(--surface-muted); color: var(--text); }}
    form, .panel {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 18px;
      margin: 16px 0;
    }}
    label {{ display: block; margin-bottom: 6px; color: #374151; font-weight: 500; }}
    input, select, button, textarea {{
      font: inherit;
    }}
    input, select {{
      width: 100%;
      padding: 9px 11px;
      margin: 0 0 14px;
      border: 1px solid #d1d5db;
      border-radius: 6px;
      background: var(--surface);
      color: var(--text);
    }}
    input:focus, select:focus, textarea:focus {{
      outline: 2px solid rgba(37, 99, 235, .18);
      border-color: var(--primary);
    }}
    button {{
      min-height: 36px;
      padding: 8px 13px;
      background: var(--primary);
      border: 1px solid var(--primary);
      color: white;
      border-radius: 6px;
      cursor: pointer;
      font-weight: 500;
    }}
    button:hover {{ background: var(--primary-hover); border-color: var(--primary-hover); }}
    table {{
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{ text-align: left; border-bottom: 1px solid var(--border); padding: 11px 12px; vertical-align: top; }}
    th {{ background: var(--surface-muted); color: #4b5563; font-weight: 600; }}
    tr:last-child td {{ border-bottom: 0; }}
    .muted {{ color: var(--muted); }}
    .ok {{ color: var(--ok); }}
    .bad {{ color: var(--bad); }}
    .actions form {{ display: inline-block; padding: 0; border: 0; margin: 0 4px 4px 0; background: transparent; }}
    .actions button {{ min-height: 30px; padding: 5px 9px; font-size: 13px; }}
    textarea {{
      width: 100%;
      min-height: 220px;
      padding: 10px;
      border: 1px solid #d1d5db;
      border-radius: 6px;
      background: var(--surface);
      color: var(--text);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    code {{ background: #f3f4f6; padding: 2px 5px; border-radius: 4px; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
    @media (max-width: 760px) {{
      header {{ padding: 0 16px; }}
      main {{ padding: 16px; }}
      table {{ display: block; overflow-x: auto; }}
    }}
  </style>
</head>
<body>
  <header>
    <img src="/data/public/btf-iot-logo.svg" alt="">
    <strong>BTF API</strong>
    <span class="muted">Device AI Authorization</span>
  </header>
  <main>{body}</main>
</body>
</html>"""
    )


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def admin_ok(request: Request) -> bool:
    token = request.cookies.get("btf_admin") or request.query_params.get("token") or request.headers.get("x-admin-token")
    return bool(settings.internal_admin_token) and token == settings.internal_admin_token


def require_admin_page(request: Request) -> RedirectResponse | None:
    if admin_ok(request):
        return None
    return RedirectResponse("/admin/login", status_code=303)


def admin_nav() -> str:
    return """
<nav>
  <a href="/admin">总览</a>
  <a href="/admin/devices">设备</a>
  <a href="/admin/orders">订单</a>
  <a href="/admin/usage">用量</a>
  <a href="/device">设备自助</a>
</nav>
"""


def status_class(device: dict) -> str:
    return "ok" if device["status"] == "active" and device["expires_at"] > now_ts() else "bad"


def device_status_text(device: dict) -> str:
    if device["status"] != "active":
        return "disabled"
    if device["expires_at"] <= now_ts():
        return "expired"
    return "active"


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
<p class="muted">设备可以在这里查看 AI 授权状态、用量和续费套餐。</p>
""",
    )


@app.get("/device/pay", response_class=HTMLResponse)
def device_pay() -> HTMLResponse:
    return html_page(
        "设备续费",
        """
<h1>设备续费</h1>
<form action="/device/status" method="post">
  <label>设备 API Key</label>
  <input name="device_key" type="password" autocomplete="off" required>
  <button type="submit">继续</button>
</form>
""",
    )


@app.post("/device/status", response_class=HTMLResponse)
def device_status_page(device_key: str = Form(...)) -> HTMLResponse:
    try:
        device = get_device_by_key(settings.database_path, device_key)
    except DeviceError as exc:
        return html_page("设备状态", f"<div class=\"panel bad\">{exc}</div>")
    plan_rows = "".join(
        f"<option value=\"{escape(plan['id'])}\">{escape(plan['title'])} - {plan['price_amount']} {escape(plan['currency'])}</option>"
        for plan in plans(settings.database_path)
    )
    return html_page(
        "设备状态",
        f"""
<h1>设备状态</h1>
<div class="panel">
  <p>设备：<code>{escape(device['device_id'])}</code></p>
  <p>状态：<strong class="{status_class(device)}">{device_status_text(device)}</strong></p>
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
  <p>订单号：<code>{escape(order['id'])}</code></p>
  <p>设备：<code>{escape(order['device_id'])}</code></p>
  <p>金额：{order['amount']} {order['currency']}</p>
  <p>状态：{order['status']}</p>
  <p>{settings.payment_instructions}</p>
  {f'<form action="/device/order/{escape(order["id"])}/demo-pay" method="post"><input name="device_key" type="hidden" value="{escape(device_key)}"><button type="submit">测试支付并开通</button></form>' if settings.demo_payment_enabled else ''}
</div>
""",
    )


@app.post("/device/order/{order_id}/demo-pay", response_class=HTMLResponse)
def device_demo_pay(order_id: str, device_key: str = Form(...)) -> HTMLResponse:
    if not settings.demo_payment_enabled:
        raise HTTPException(status_code=404, detail="payment provider not enabled")
    try:
        order = approve_order_for_key(settings.database_path, order_id, device_key)
        device = get_device(settings.database_path, order["device_id"])
    except DeviceError as exc:
        return html_page("支付失败", f"<div class=\"panel bad\">{escape(str(exc))}</div>")
    return html_page(
        "支付完成",
        f"""
<h1>支付完成</h1>
<div class="panel ok">
  <p>订单：<code>{escape(order['id'])}</code></p>
  <p>设备：<code>{escape(device['device_id'])}</code></p>
  <p>新到期时间：{fmt_ts(device['expires_at'])}</p>
</div>
""",
    )


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login() -> HTMLResponse:
    return html_page(
        "管理登录",
        """
<h1>管理登录</h1>
<form action="/admin/login" method="post">
  <label>账号</label>
  <input name="username" autocomplete="username" required>
  <label>密码</label>
  <input name="password" type="password" autocomplete="current-password" required>
  <button type="submit">登录</button>
</form>
""",
    )


@app.post("/admin/login")
def admin_login_post(username: str = Form(...), password: str = Form(...)) -> Response:
    if username != settings.admin_username or password != settings.admin_password:
        return html_page("管理登录", "<div class=\"panel bad\">账号或密码不正确</div>")
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie("btf_admin", settings.internal_admin_token, httponly=True, samesite="lax")
    return response


@app.post("/admin/logout")
def admin_logout() -> RedirectResponse:
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie("btf_admin")
    return response


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request) -> Response:
    redirect = require_admin_page(request)
    if redirect:
        return redirect
    devices = list_devices(settings.database_path)
    orders = list_orders(settings.database_path, 20)
    active_count = sum(1 for item in devices if device_status_text(item) == "active")
    expired_count = sum(1 for item in devices if device_status_text(item) == "expired")
    disabled_count = sum(1 for item in devices if item["status"] != "active")
    pending_count = sum(1 for item in orders if item["status"] == "pending")
    return html_page(
        "BTF 管理后台",
        f"""
{admin_nav()}
<h1>总览</h1>
<div class="panel">
  <p>设备总数：{len(devices)}</p>
  <p>可用设备：{active_count}</p>
  <p>到期设备：{expired_count}</p>
  <p>禁用设备：{disabled_count}</p>
  <p>待确认订单：{pending_count}</p>
</div>
<form action="/admin/logout" method="post"><button type="submit">退出</button></form>
""",
    )


@app.get("/admin/devices", response_class=HTMLResponse)
def admin_devices(request: Request) -> Response:
    redirect = require_admin_page(request)
    if redirect:
        return redirect
    rows = "".join(
        f"""
<tr>
  <td><code>{escape(device['device_id'])}</code></td>
  <td class="{status_class(device)}">{device_status_text(device)}</td>
  <td>{escape(device['key_mask'])}</td>
  <td>{fmt_ts(device['expires_at'])}</td>
  <td>{device['request_count']}</td>
  <td>{device['total_tokens']}</td>
  <td class="actions">
    <form action="/admin/devices/{escape(device['device_id'])}/renew" method="post"><input name="days" type="hidden" value="30"><button>续30天</button></form>
    <form action="/admin/devices/{escape(device['device_id'])}/enable" method="post"><button>启用</button></form>
    <form action="/admin/devices/{escape(device['device_id'])}/disable" method="post"><button>禁用</button></form>
    <form action="/admin/devices/{escape(device['device_id'])}/rotate" method="post"><button>换Key</button></form>
  </td>
</tr>
"""
        for device in list_devices(settings.database_path)
    )
    return html_page(
        "设备管理",
        f"""
{admin_nav()}
<h1>设备管理</h1>
<form action="/admin/devices" method="post">
  <h2>创建设备</h2>
  <label>设备编号</label><input name="device_id" required>
  <label>授权天数</label><input name="days" type="number" value="30" min="1" required>
  <label>备注</label><input name="note">
  <button type="submit">创建</button>
</form>
<form action="/admin/devices/batch" method="post">
  <h2>批量创建设备</h2>
  <label>编号前缀</label><input name="prefix" value="BTF-DEVICE-" required>
  <label>数量</label><input name="count" type="number" value="10" min="1" max="1000" required>
  <label>授权天数</label><input name="days" type="number" value="30" min="1" required>
  <button type="submit">批量生成</button>
</form>
<table>
  <tr><th>设备</th><th>状态</th><th>Key</th><th>到期</th><th>请求</th><th>Token</th><th>操作</th></tr>
  {rows}
</table>
""",
    )


@app.post("/admin/devices", response_class=HTMLResponse)
def admin_create_device(request: Request, device_id: str = Form(...), days: int = Form(...), note: str = Form("")) -> Response:
    redirect = require_admin_page(request)
    if redirect:
        return redirect
    try:
        key, device = create_device(settings.database_path, device_id, days, note)
    except Exception as exc:
        return html_page("创建设备失败", f"{admin_nav()}<div class=\"panel bad\">{escape(str(exc))}</div>")
    return html_page(
        "设备已创建",
        f"""
{admin_nav()}
<h1>设备已创建</h1>
<div class="panel">
  <p>设备：<code>{escape(device['device_id'])}</code></p>
  <p>API Key：<code>{escape(key)}</code></p>
  <p>Base URL：<code>{settings.public_base_url}/v1</code></p>
  <p>模型：<code>deepseek-v4.1-flash</code></p>
  <p>到期：{fmt_ts(device['expires_at'])}</p>
</div>
""",
    )


@app.post("/admin/devices/batch", response_class=HTMLResponse)
def admin_batch_devices(request: Request, prefix: str = Form(...), count: int = Form(...), days: int = Form(...)) -> Response:
    redirect = require_admin_page(request)
    if redirect:
        return redirect
    try:
        rows = batch_create_devices(settings.database_path, prefix, count, days)
    except Exception as exc:
        return html_page("批量创建设备失败", f"{admin_nav()}<div class=\"panel bad\">{escape(str(exc))}</div>")
    csv_rows = ["device_id,api_key,base_url,model,expires_at"]
    for key, device in rows:
        csv_rows.append(
            f"{device['device_id']},{key},{settings.public_base_url}/v1,deepseek-v4.1-flash,{fmt_ts(device['expires_at'])}"
        )
    return html_page(
        "批量设备已创建",
        f"""
{admin_nav()}
<h1>批量设备已创建</h1>
<p class="muted">下面内容只显示一次。</p>
<textarea>{escape(chr(10).join(csv_rows))}</textarea>
""",
    )


@app.post("/admin/devices/{device_id}/renew")
def admin_renew_device(request: Request, device_id: str, days: int = Form(30)) -> Response:
    redirect = require_admin_page(request)
    if redirect:
        return redirect
    renew_device(settings.database_path, device_id, days)
    return RedirectResponse("/admin/devices", status_code=303)


@app.post("/admin/devices/{device_id}/disable")
def admin_disable_device(request: Request, device_id: str) -> Response:
    redirect = require_admin_page(request)
    if redirect:
        return redirect
    disable_device(settings.database_path, device_id)
    return RedirectResponse("/admin/devices", status_code=303)


@app.post("/admin/devices/{device_id}/enable")
def admin_enable_device(request: Request, device_id: str) -> Response:
    redirect = require_admin_page(request)
    if redirect:
        return redirect
    enable_device(settings.database_path, device_id)
    return RedirectResponse("/admin/devices", status_code=303)


@app.post("/admin/devices/{device_id}/rotate", response_class=HTMLResponse)
def admin_rotate_device(request: Request, device_id: str) -> Response:
    redirect = require_admin_page(request)
    if redirect:
        return redirect
    key, device = rotate_device_key(settings.database_path, device_id)
    return html_page(
        "设备 Key 已更新",
        f"""
{admin_nav()}
<h1>设备 Key 已更新</h1>
<div class="panel">
  <p>设备：<code>{escape(device['device_id'])}</code></p>
  <p>新 API Key：<code>{escape(key)}</code></p>
  <p class="bad">旧 key 已失效，新 key 只显示一次。</p>
</div>
""",
    )


@app.get("/admin/orders", response_class=HTMLResponse)
def admin_orders(request: Request) -> Response:
    redirect = require_admin_page(request)
    if redirect:
        return redirect
    rows = "".join(
        f"""
<tr>
  <td><code>{escape(order['id'])}</code></td>
  <td>{escape(order['device_id'])}</td>
  <td>{escape(order.get('plan_title') or order['plan_id'])}</td>
  <td>{order['amount']} {escape(order['currency'])}</td>
  <td>{escape(order['status'])}</td>
  <td>{fmt_ts(order['created_at'])}</td>
  <td class="actions"><form action="/admin/orders/{escape(order['id'])}/approve" method="post"><button>确认开通</button></form></td>
</tr>
"""
        for order in list_orders(settings.database_path)
    )
    return html_page(
        "订单管理",
        f"""
{admin_nav()}
<h1>订单管理</h1>
<table>
  <tr><th>订单</th><th>设备</th><th>套餐</th><th>金额</th><th>状态</th><th>创建时间</th><th>操作</th></tr>
  {rows}
</table>
""",
    )


@app.post("/admin/orders/{order_id}/approve")
def admin_approve_order(request: Request, order_id: str) -> Response:
    redirect = require_admin_page(request)
    if redirect:
        return redirect
    approve_order(settings.database_path, order_id)
    return RedirectResponse("/admin/orders", status_code=303)


@app.get("/admin/usage", response_class=HTMLResponse)
def admin_usage(request: Request, device_id: str | None = None) -> Response:
    redirect = require_admin_page(request)
    if redirect:
        return redirect
    rows = "".join(
        f"""
<tr>
  <td>{event['id']}</td>
  <td>{escape(event['device_id'])}</td>
  <td>{escape(event['path'])}</td>
  <td>{event['status_code']}</td>
  <td>{event['prompt_tokens']}</td>
  <td>{event['completion_tokens']}</td>
  <td>{event['total_tokens']}</td>
  <td>{fmt_ts(event['created_at'])}</td>
</tr>
"""
        for event in list_usage(settings.database_path, device_id)
    )
    return html_page(
        "用量记录",
        f"""
{admin_nav()}
<h1>用量记录</h1>
<form action="/admin/usage" method="get">
  <label>设备编号</label>
  <input name="device_id" value="{escape(device_id or '')}">
  <button type="submit">筛选</button>
</form>
<table>
  <tr><th>ID</th><th>设备</th><th>接口</th><th>状态码</th><th>输入</th><th>输出</th><th>总计</th><th>时间</th></tr>
  {rows}
</table>
""",
    )


@app.get("/api/device/status")
def api_device_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    device = get_device_by_key(settings.database_path, bearer_token(authorization))
    return {
        "device_id": device["device_id"],
        "status": device["status"],
        "active": device_status_text(device) == "active",
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
        if key.lower() not in {"content-encoding", "transfer-encoding", "connection", "content-length"}
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
        if key.lower() not in {"content-encoding", "transfer-encoding", "connection", "content-length"}
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
