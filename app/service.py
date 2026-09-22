from __future__ import annotations

import secrets
import re
from pathlib import Path

from app.db import connect, init_db, now_ts
from app.models import DeviceAuth
from app.security import generate_device_key, hash_key, mask_key


SYSTEM_QUOTA_DAYS = 3650


class DeviceError(Exception):
    pass


class NotFound(DeviceError):
    pass


class Conflict(DeviceError):
    pass


class InvalidState(DeviceError):
    pass


DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


def validate_device_id(device_id: str) -> None:
    if not DEVICE_ID_RE.match(device_id):
        raise ValueError("device_id may only contain letters, numbers, dot, underscore, colon, or dash")


def parse_optional_int(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def ensure_ready(database_path: Path) -> None:
    init_db(database_path)


def create_user(database_path: Path, username: str, display_name: str, contact: str = "", note: str = "") -> dict:
    username = username.strip()
    display_name = display_name.strip() or username
    if not username:
        raise ValueError("username is required")
    ts = now_ts()
    with connect(database_path) as conn:
        exists = conn.execute("select 1 from users where username = ?", (username,)).fetchone()
        if exists:
            raise Conflict(f"user already exists: {username}")
        conn.execute(
            """
            insert into users (username, display_name, contact, status, created_at, updated_at, note)
            values (?, ?, ?, 'active', ?, ?, ?)
            """,
            (username, display_name, contact, ts, ts, note),
        )
        row = conn.execute("select * from users where username = ?", (username,)).fetchone()
    return dict(row)


def get_user(database_path: Path, user_id: int) -> dict:
    with connect(database_path) as conn:
        row = conn.execute("select * from users where id = ?", (user_id,)).fetchone()
    if not row:
        raise NotFound(f"user not found: {user_id}")
    return dict(row)


def list_users(database_path: Path) -> list[dict]:
    with connect(database_path) as conn:
        rows = conn.execute("select * from users order by id desc").fetchall()
    return [dict(row) for row in rows]


def bind_device_user(database_path: Path, device_id: str, user_id: int | None) -> dict:
    if user_id is not None:
        get_user(database_path, user_id)
    ts = now_ts()
    with connect(database_path) as conn:
        cur = conn.execute(
            "update devices set user_id = ?, updated_at = ? where device_id = ?",
            (user_id, ts, device_id),
        )
        if cur.rowcount == 0:
            raise NotFound(f"device not found: {device_id}")
    return get_device(database_path, device_id)


def create_device(database_path: Path, device_id: str, days: int, note: str = "", user_id: int | None = None) -> tuple[str, dict]:
    validate_device_id(device_id)
    if days <= 0:
        raise ValueError("days must be greater than 0")
    if user_id is not None:
        get_user(database_path, user_id)
    key = generate_device_key()
    ts = now_ts()
    expires_at = ts + days * 86400
    with connect(database_path) as conn:
        exists = conn.execute(
            "select 1 from devices where device_id = ?",
            (device_id,),
        ).fetchone()
        if exists:
            raise Conflict(f"device already exists: {device_id}")
        conn.execute(
            """
            insert into devices (
              device_id, user_id, key_hash, key_mask, status, expires_at,
              created_at, updated_at, note
            )
            values (?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (device_id, user_id, hash_key(key), mask_key(key), expires_at, ts, ts, note),
        )
    return key, get_device(database_path, device_id)


def get_device(database_path: Path, device_id: str) -> dict:
    with connect(database_path) as conn:
        row = conn.execute(
            """
            select d.*, u.username as owner_username, u.display_name as owner_display_name
            from devices d
            left join users u on u.id = d.user_id
            where d.device_id = ?
            """,
            (device_id,),
        ).fetchone()
    if not row:
        raise NotFound(f"device not found: {device_id}")
    return dict(row)


def get_device_by_key(database_path: Path, device_key: str) -> dict:
    with connect(database_path) as conn:
        row = conn.execute(
            """
            select *
            from devices
            where key_hash = ?
            """,
            (hash_key(device_key),),
        ).fetchone()
    if not row:
        raise NotFound("invalid device key")
    return dict(row)


def list_devices(database_path: Path) -> list[dict]:
    with connect(database_path) as conn:
        rows = conn.execute(
            """
            select d.*, u.username as owner_username, u.display_name as owner_display_name
            from devices d
            left join users u on u.id = d.user_id
            order by d.id desc
            """,
        ).fetchall()
    return [dict(row) for row in rows]


def device_auth_from_row(row: dict) -> DeviceAuth:
    return DeviceAuth(
        device_id=row["device_id"],
        status=row["status"],
        expires_at=row["expires_at"],
        request_count=row["request_count"],
        total_tokens=row["total_tokens"],
    )


def authenticate_device(database_path: Path, device_key: str) -> DeviceAuth:
    auth = device_auth_from_row(get_device_by_key(database_path, device_key))
    if not auth.is_active:
        raise InvalidState("device authorization expired or disabled")
    return auth


def rotate_device_key(database_path: Path, device_id: str) -> tuple[str, dict]:
    key = generate_device_key()
    ts = now_ts()
    with connect(database_path) as conn:
        cur = conn.execute(
            """
            update devices
            set key_hash = ?, key_mask = ?, updated_at = ?
            where device_id = ?
            """,
            (hash_key(key), mask_key(key), ts, device_id),
        )
        if cur.rowcount == 0:
            raise NotFound(f"device not found: {device_id}")
    return key, get_device(database_path, device_id)


def batch_create_devices(
    database_path: Path,
    prefix: str,
    count: int,
    days: int,
    note: str = "",
    user_id: int | None = None,
) -> list[tuple[str, dict]]:
    if not DEVICE_ID_RE.match(f"{prefix}0001"):
        raise ValueError("prefix would create invalid device ids")
    if count <= 0:
        raise ValueError("count must be greater than 0")
    if user_id is not None:
        get_user(database_path, user_id)
    width = max(4, len(str(count)))
    created: list[tuple[str, dict]] = []
    for idx in range(1, count + 1):
        device_id = f"{prefix}{idx:0{width}d}"
        created.append(create_device(database_path, device_id, days, note, user_id))
    return created


def import_devices(
    database_path: Path,
    device_ids: list[str],
    days: int,
    note: str = "",
    user_id: int | None = None,
) -> list[tuple[str, dict]]:
    if days <= 0:
        raise ValueError("days must be greater than 0")
    if user_id is not None:
        get_user(database_path, user_id)
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw_id in device_ids:
        device_id = raw_id.strip()
        if not device_id:
            continue
        validate_device_id(device_id)
        if device_id in seen:
            continue
        seen.add(device_id)
        cleaned.append(device_id)
    if not cleaned:
        raise ValueError("no device ids provided")
    created: list[tuple[str, dict]] = []
    for device_id in cleaned:
        created.append(create_device(database_path, device_id, days, note, user_id))
    return created


def renew_device(database_path: Path, device_id: str, days: int) -> dict:
    if days <= 0:
        raise ValueError("days must be greater than 0")
    ts = now_ts()
    with connect(database_path) as conn:
        row = conn.execute(
            "select expires_at from devices where device_id = ?",
            (device_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"device not found: {device_id}")
        base = max(row["expires_at"], ts)
        expires_at = base + days * 86400
        conn.execute(
            """
            update devices
            set expires_at = ?, status = 'active', updated_at = ?
            where device_id = ?
            """,
            (expires_at, ts, device_id),
        )
    return get_device(database_path, device_id)


def disable_device(database_path: Path, device_id: str) -> dict:
    ts = now_ts()
    with connect(database_path) as conn:
        cur = conn.execute(
            """
            update devices
            set status = 'disabled', updated_at = ?
            where device_id = ?
            """,
            (ts, device_id),
        )
        if cur.rowcount == 0:
            raise NotFound(f"device not found: {device_id}")
    return get_device(database_path, device_id)


def enable_device(database_path: Path, device_id: str, days_if_expired: int = 30) -> dict:
    device = get_device(database_path, device_id)
    ts = now_ts()
    expires_at = device["expires_at"]
    if expires_at <= ts:
        expires_at = ts + days_if_expired * 86400
    with connect(database_path) as conn:
        conn.execute(
            """
            update devices
            set status = 'active', expires_at = ?, updated_at = ?
            where device_id = ?
            """,
            (expires_at, ts, device_id),
        )
    return get_device(database_path, device_id)


def plans(database_path: Path) -> list[dict]:
    with connect(database_path) as conn:
        rows = conn.execute(
            """
            select * from plans
            where enabled = 1
            order by sort_order, price_amount
            """,
        ).fetchall()
    return [dict(row) for row in rows]


def create_order(database_path: Path, device_key: str, plan_id: str) -> dict:
    device = get_device_by_key(database_path, device_key)
    ts = now_ts()
    with connect(database_path) as conn:
        plan = conn.execute(
            "select * from plans where id = ? and enabled = 1",
            (plan_id,),
        ).fetchone()
        if not plan:
            raise NotFound(f"plan not found: {plan_id}")
        order_id = f"btf_ord_{secrets.token_urlsafe(12)}"
        conn.execute(
            """
            insert into orders (id, device_id, plan_id, status, amount, currency, created_at)
            values (?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                order_id,
                device["device_id"],
                plan_id,
                float(plan["price_amount"]),
                plan["currency"],
                ts,
            ),
        )
        row = conn.execute("select * from orders where id = ?", (order_id,)).fetchone()
    return dict(row)


def get_order(database_path: Path, order_id: str) -> dict:
    with connect(database_path) as conn:
        row = conn.execute("select * from orders where id = ?", (order_id,)).fetchone()
    if not row:
        raise NotFound(f"order not found: {order_id}")
    return dict(row)


def list_orders(database_path: Path, limit: int = 200) -> list[dict]:
    with connect(database_path) as conn:
        rows = conn.execute(
            """
            select o.*, p.title as plan_title
            from orders o
            left join plans p on p.id = o.plan_id
            order by o.created_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def approve_order(database_path: Path, order_id: str) -> dict:
    ts = now_ts()
    with connect(database_path) as conn:
        order = conn.execute("select * from orders where id = ?", (order_id,)).fetchone()
        if not order:
            raise NotFound(f"order not found: {order_id}")
        if order["status"] == "approved":
            return dict(order)
        if order["status"] not in ("pending", "paid"):
            raise InvalidState(f"order cannot be approved from status {order['status']}")
        plan = conn.execute("select * from plans where id = ?", (order["plan_id"],)).fetchone()
        if not plan:
            raise NotFound(f"plan not found: {order['plan_id']}")
        device = conn.execute(
            "select expires_at from devices where device_id = ?",
            (order["device_id"],),
        ).fetchone()
        if not device:
            raise NotFound(f"device not found: {order['device_id']}")
        base = max(device["expires_at"], ts)
        duration_days = int(plan["duration_days"])
        if duration_days == 0:
            # Trial plan is one hour.
            expires_at = max(device["expires_at"], ts) + 3600
        else:
            expires_at = base + duration_days * 86400
        conn.execute(
            """
            update devices
            set expires_at = ?, status = 'active', updated_at = ?
            where device_id = ?
            """,
            (expires_at, ts, order["device_id"]),
        )
        conn.execute(
            """
            update orders
            set status = 'approved', paid_at = coalesce(paid_at, ?), approved_at = ?
            where id = ?
            """,
            (ts, ts, order_id),
        )
        updated = conn.execute("select * from orders where id = ?", (order_id,)).fetchone()
    return dict(updated)


def approve_order_for_key(database_path: Path, order_id: str, device_key: str) -> dict:
    device = get_device_by_key(database_path, device_key)
    order = get_order(database_path, order_id)
    if order["device_id"] != device["device_id"]:
        raise InvalidState("order does not belong to this device")
    return approve_order(database_path, order_id)


def list_usage(database_path: Path, device_id: str | None = None, limit: int = 200) -> list[dict]:
    with connect(database_path) as conn:
        if device_id:
            rows = conn.execute(
                """
                select * from usage_events
                where device_id = ?
                order by id desc
                limit ?
                """,
                (device_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                select * from usage_events
                order by id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


def record_usage(
    database_path: Path,
    device_id: str,
    path: str,
    status_code: int,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
) -> None:
    ts = now_ts()
    with connect(database_path) as conn:
        conn.execute(
            """
            insert into usage_events (
              device_id, path, status_code, prompt_tokens, completion_tokens, total_tokens, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (device_id, path, status_code, prompt_tokens, completion_tokens, total_tokens, ts),
        )
        conn.execute(
            """
            update devices
            set request_count = request_count + 1,
                prompt_tokens = prompt_tokens + ?,
                completion_tokens = completion_tokens + ?,
                total_tokens = total_tokens + ?,
                last_seen_at = ?,
                updated_at = ?
            where device_id = ?
            """,
            (prompt_tokens, completion_tokens, total_tokens, ts, ts, device_id),
        )
