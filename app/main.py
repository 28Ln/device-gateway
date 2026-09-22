from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app.config import load_settings
from app.db import now_ts
from app.service import (
    DeviceError,
    authenticate_device,
    ensure_ready,
    get_device_by_key,
    record_usage,
)

settings = load_settings()
ensure_ready(settings.database_path)

app = FastAPI(title="BTF Device Gateway")


def fmt_ts(value: int | None) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization.split(" ", 1)[1].strip()


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
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=proxy_response_headers(upstream.headers),
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
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=proxy_response_headers(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )


@app.exception_handler(DeviceError)
async def device_error_handler(_: Request, exc: DeviceError) -> JSONResponse:
    return JSONResponse(status_code=403, content={"error": str(exc), "timestamp": now_ts()})
