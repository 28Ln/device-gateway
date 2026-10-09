from __future__ import annotations

import sqlite3
import secrets
import re
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator

from app.db import connect, init_db, now_ts
from app.models import DeviceAuth
from app.payments import PaymentIntent, PaymentNotification
from app.security import generate_device_key, hash_key, mask_key


# Read paths no longer force a write sweep on every call; the timeout sweep is
# throttled to avoid turning GET handlers into unconditional writers.
EXPIRE_SWEEP_INTERVAL_SECONDS = 30
_LAST_EXPIRE_SWEEP: dict[str, int] = {}


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


@contextmanager
def _immediate(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a connection with a write lock held for the whole transaction.

    ``BEGIN IMMEDIATE`` acquires the reserved lock up-front so concurrent
    order settlements (webhook + manual approval, retries, duplicated callbacks)
    serialise instead of racing on a read-then-write check. The caller is
    responsible for committing.
    """
    conn = sqlite3.connect(database_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    # Match ``db.connect``: ``foreign_keys`` is a per-connection pragma and a
    # no-op inside a transaction, so it must be set here too or this write path
    # would silently bypass FK protection (dangling device/order references).
    conn.execute("pragma busy_timeout=5000")
    conn.execute("pragma foreign_keys=on")
    try:
        conn.execute("begin immediate")
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()


def money_to_minor(value: object) -> int:
    """Normalise a monetary value to integer minor units (fen).

    Comparing and storing money as integers avoids binary float rounding
    errors (e.g. 0.1 + 0.2) that make strict equality checks unreliable.
    """
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid monetary amount: {value!r}") from exc
    return int((amount * 100).to_integral_value())


def amounts_match(left: object, right: object) -> bool:
    try:
        return money_to_minor(left) == money_to_minor(right)
    except ValueError:
        return False


def plan_id_for_days(days: int) -> str:
    if days >= 30:
        return "month_1"
    if days >= 7:
        return "week_1"
    if days >= 3:
        return "day_3"
    return "day_1"


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


def create_device(
    database_path: Path,
    device_id: str,
    days: int,
    note: str = "",
    user_id: int | None = None,
    device_key: str | None = None,
) -> tuple[str, dict]:
    validate_device_id(device_id)
    if days <= 0:
        raise ValueError("days must be greater than 0")
    if user_id is not None:
        get_user(database_path, user_id)
    # BTF devices authenticate with their stable hardware identifier by default,
    # which keeps CLI, batch import, and HTTP-created devices on the same
    # contract and does not break existing hardware provisioning. Callers that
    # can provision a high-entropy secret (HTTP/CLI) may pass ``device_key`` to
    # decouple the auth credential from the displayable device_id.
    key = device_key if device_key is not None else device_id
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
              created_at, updated_at, note, plan_id, plan_started_at
            )
            values (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
            """,
            (
                device_id,
                user_id,
                hash_key(key),
                mask_key(key),
                expires_at,
                ts,
                ts,
                note,
                plan_id_for_days(days),
                ts,
            ),
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
    """Re-activate a device, mirroring the HTTP ``action=enable`` semantics.

    Enabling must never produce -- or leave behind -- a device that is both
    ``active`` and already ``expired``. A negative/zero ``days_if_expired``
    would do exactly that by writing a past ``expires_at``, so it is rejected
    outright (ValueError). An already-expired device therefore only becomes
    ``active`` when an explicit positive renewal window is supplied; without
    one the call fails and leaves the row untouched (non-active), which is the
    same "expiry wins" normalisation the HTTP ``action=enable`` path applies.
    """
    if days_if_expired <= 0:
        raise ValueError("days_if_expired must be greater than 0")
    device = get_device(database_path, device_id)
    ts = now_ts()
    expires_at = device["expires_at"]
    if expires_at <= ts:
        # A lapsed device needs an explicit positive renewal window; extending
        # from "now" (not from the stale expiry) keeps the new expiry future-dated,
        # so the row can safely become ``active`` instead of active+expired.
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


def create_order(
    database_path: Path,
    device_key: str,
    plan_id: str,
    expires_seconds: int = 86400,
) -> dict:
    device = get_device_by_key(database_path, device_key)
    ts = now_ts()
    payment_expires_at = ts + max(expires_seconds, 300)
    with connect(database_path) as conn:
        plan = conn.execute(
            "select * from plans where id = ? and enabled = 1",
            (plan_id,),
        ).fetchone()
        if not plan:
            raise NotFound(f"plan not found: {plan_id}")
        order_id = f"btf_ord_{secrets.token_urlsafe(12)}"
        # Persist the amount rounded to fen so comparisons never trip over
        # binary-float noise.
        amount = money_to_minor(plan["price_amount"]) / 100
        conn.execute(
            """
            insert into orders (
              id, device_id, plan_id, status, amount, currency, created_at, payment_expires_at
            )
            values (?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                order_id,
                device["device_id"],
                plan_id,
                amount,
                plan["currency"],
                ts,
                payment_expires_at,
            ),
        )
        row = conn.execute("select * from orders where id = ?", (order_id,)).fetchone()
    return dict(row)


def get_order(database_path: Path, order_id: str) -> dict:
    _maybe_expire_pending_orders(database_path)
    with connect(database_path) as conn:
        row = conn.execute("select * from orders where id = ?", (order_id,)).fetchone()
    if not row:
        raise NotFound(f"order not found: {order_id}")
    return dict(row)


def expire_pending_orders(database_path: Path) -> int:
    with _immediate(database_path) as conn:
        cur = conn.execute(
            """
            update orders
            set status = 'expired',
                closed_at = ?,
                close_reason = coalesce(close_reason, 'timeout')
            where status = 'pending'
              and payment_expires_at is not null
              and payment_expires_at > 0
              and payment_expires_at < ?
            """,
            (now_ts(), now_ts()),
        )
    return cur.rowcount


def _maybe_expire_pending_orders(
    database_path: Path,
    interval: int = EXPIRE_SWEEP_INTERVAL_SECONDS,
) -> int:
    """Throttled timeout sweep for read paths.

    ``get_order``/``list_orders`` used to run an unconditional ``UPDATE`` on
    every call, so plain reads took a write lock and could contend with the
    payment webhooks. We now throttle the sweep; status filters and payment
    callbacks enforce the authoritative expiry semantics in the write paths.
    """
    now = now_ts()
    key = str(database_path.resolve())
    if now - _LAST_EXPIRE_SWEEP.get(key, 0) < interval:
        return 0
    rowcount = expire_pending_orders(database_path)
    _LAST_EXPIRE_SWEEP[key] = now
    return rowcount


def close_order(database_path: Path, order_id: str, reason: str = "manual") -> dict:
    ts = now_ts()
    reason = reason.strip() or "manual"
    with _immediate(database_path) as conn:
        order = conn.execute("select * from orders where id = ?", (order_id,)).fetchone()
        if not order:
            raise NotFound(f"order not found: {order_id}")
        if order["status"] == "approved":
            raise InvalidState("approved order cannot be closed")
        if order["status"] in {"closed", "expired", "cancelled", "canceled"}:
            return dict(order)
        # A order whose provider payment already landed must not be closed;
        # otherwise the money would be captured with no way to grant access.
        if order["status"] == "paid":
            raise InvalidState("paid order cannot be closed before refund handling")
        payment = conn.execute(
            """
            select 1 from payment_transactions
            where order_id = ? and status = 'paid'
            limit 1
            """,
            (order_id,),
        ).fetchone()
        if payment:
            raise InvalidState("order has a captured payment; refund handling required")
        cur = conn.execute(
            """
            update orders
            set status = 'closed', closed_at = ?, close_reason = ?
            where id = ? and status in ('pending', 'paid')
            """,
            (ts, reason, order_id),
        )
        if cur.rowcount == 0:
            current = conn.execute("select * from orders where id = ?", (order_id,)).fetchone()
            return dict(current)
        updated = conn.execute("select * from orders where id = ?", (order_id,)).fetchone()
    return dict(updated)


def save_payment_intent(
    database_path: Path,
    order_id: str,
    intent: PaymentIntent,
    idempotency_key: str | None = None,
) -> dict:
    order = get_order(database_path, order_id)
    if order["status"] == "approved":
        raise InvalidState("order is already approved")
    ts = now_ts()
    with connect(database_path) as conn:
        existing = None
        if idempotency_key:
            existing = conn.execute(
                """
                select * from payment_transactions
                where order_id = ? and provider = ? and idempotency_key = ?
                order by id desc limit 1
                """,
                (order_id, intent.provider, idempotency_key),
            ).fetchone()
        if existing:
            return dict(existing)
        existing = conn.execute(
            """
            select * from payment_transactions
            where order_id = ? and provider = ? and status in ('created', 'paid')
            order by id desc limit 1
            """,
            (order_id, intent.provider),
        ).fetchone()
        if existing:
            return dict(existing)
        try:
            conn.execute(
                """
                insert into payment_transactions (
                  order_id, provider, provider_order_id, status, amount, currency,
                  checkout_url, idempotency_key, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    intent.provider,
                    intent.provider_order_id,
                    intent.status,
                    float(order["amount"]),
                    order["currency"],
                    intent.checkout_url,
                    idempotency_key,
                    ts,
                ),
            )
        except sqlite3.IntegrityError:
            if not idempotency_key:
                raise
            existing = conn.execute(
                """
                select * from payment_transactions
                where order_id = ? and provider = ? and idempotency_key = ?
                """,
                (order_id, intent.provider, idempotency_key),
            ).fetchone()
            if not existing:
                raise
            return dict(existing)
        conn.execute(
            """
            update orders
            set payment_provider = ?, provider_order_id = ?,
                payment_url = ?, payment_expires_at = ?
            where id = ?
            """,
            (
                intent.provider,
                intent.provider_order_id,
                intent.checkout_url,
                intent.expires_at,
                order_id,
            ),
        )
        row = conn.execute(
            "select * from payment_transactions where order_id = ? and provider = ? order by id desc limit 1",
            (order_id, intent.provider),
        ).fetchone()
    return dict(row)


def list_payment_transactions(database_path: Path, order_id: str | None = None) -> list[dict]:
    with connect(database_path) as conn:
        if order_id:
            rows = conn.execute(
                """
                select * from payment_transactions
                where order_id = ?
                order by id desc
                """,
                (order_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "select * from payment_transactions order by id desc",
            ).fetchall()
    return [dict(row) for row in rows]


def _mark_transaction_paid(
    conn: sqlite3.Connection,
    order_id: str,
    provider: str,
    provider_order_id: str | None,
    raw_payload: str,
    ts: int,
) -> None:
    payment = conn.execute(
        """
        select * from payment_transactions
        where order_id = ? and provider = ?
        order by id desc limit 1
        """,
        (order_id, provider),
    ).fetchone()
    if not payment or payment["status"] == "paid":
        return
    conn.execute(
        """
        update payment_transactions
        set status = 'paid', paid_at = ?, raw_payload = ?,
            provider_transaction_id = ?
        where id = ? and status <> 'paid'
        """,
        (ts, raw_payload, provider_order_id, payment["id"]),
    )


def _extend_device_for_order(conn: sqlite3.Connection, order: sqlite3.Row, ts: int) -> None:
    """Apply the plan's duration to the order's device.

    Must be called at most once per order while the order is claimed as
    ``paid``; concurrent callers rely on the caller's optimistic-lock rowcount
    check to guarantee that.
    """
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
        expires_at = base + 3600
    else:
        expires_at = base + duration_days * 86400
    conn.execute(
        """
        update devices
        set expires_at = ?, status = 'active', updated_at = ?, plan_id = ?, plan_started_at = ?
        where device_id = ?
        """,
        (expires_at, ts, order["plan_id"], ts, order["device_id"]),
    )


def apply_payment_notification(
    database_path: Path,
    notification: PaymentNotification,
    *,
    authorized_amount: object | None = None,
    authorized_currency: str | None = None,
) -> dict:
    """Settle a payment callback atomically.

    ``authorized_amount``/``authorized_currency`` are the provider-authoritative
    values (queried from the provider API or a locally signed mock record); the
    notification body itself is client-reachable and must never be trusted for
    the amount. When omitted, the notification is trusted (tests / adapters
    that already verified the payload).
    """
    if not notification.succeeded:
        raise InvalidState(f"payment notification is not successful: {notification.status}")
    # Priority: explicit caller override > value the provider attached > body.
    if authorized_amount is not None:
        check_amount: object = authorized_amount
    elif notification.authorized_amount is not None:
        check_amount = notification.authorized_amount
    else:
        check_amount = notification.amount
    if authorized_currency is not None:
        check_currency: str = authorized_currency
    elif notification.authorized_currency is not None:
        check_currency = notification.authorized_currency
    else:
        check_currency = notification.currency
    with _immediate(database_path) as conn:
        order = conn.execute(
            "select * from orders where id = ?",
            (notification.order_id,),
        ).fetchone()
        if not order:
            raise NotFound(f"order not found: {notification.order_id}")
        if not amounts_match(check_amount, order["amount"]):
            raise InvalidState("payment amount does not match order amount")
        if str(check_currency).upper() != str(order["currency"]).upper():
            raise InvalidState("payment currency does not match order currency")
        payment = conn.execute(
            """
            select * from payment_transactions
            where order_id = ? and provider = ?
            order by id desc limit 1
            """,
            (notification.order_id, notification.provider),
        ).fetchone()
        if not payment:
            raise NotFound("payment transaction not found")
        if notification.provider == "mock":
            if payment["provider_order_id"] != notification.provider_order_id:
                raise InvalidState("payment provider order id does not match")
        elif payment["provider_order_id"] != notification.order_id:
            raise InvalidState("payment merchant order id does not match")
        ts = now_ts()
        # Mark paid first, bounded to statuses that can still be fulfilled. The
        # rowcount is the optimistic lock: exactly one caller sees 1 and runs
        # the device extension, so retries/duplicate callbacks never double it.
        # Note: a pending order whose checkout window lapsed is still settled
        # here — the provider captured the money, so stranding it would cause
        # asset loss.
        paid = conn.execute(
            """
            update orders
            set status = 'paid', paid_at = coalesce(paid_at, ?)
            where id = ? and status in ('pending', 'paid')
            """,
            (ts, notification.order_id),
        )
        _mark_transaction_paid(
            conn,
            notification.order_id,
            notification.provider,
            notification.provider_order_id,
            notification.raw_payload,
            ts,
        )
        if paid.rowcount == 0:
            if order["status"] == "approved":
                # Fully settled already: idempotent no-op.
                return dict(order)
            if order["status"] in {"closed", "expired", "cancelled", "canceled"}:
                # The money was captured after the order left the checkout
                # window. Re-open it for fulfilment instead of losing the
                # payment; the original close reason is preserved in the
                # payment_transactions audit trail.
                conn.execute(
                    """
                    update orders
                    set status = 'paid', paid_at = coalesce(paid_at, ?),
                        closed_at = null, close_reason = null
                    where id = ?
                    """,
                    (ts, notification.order_id),
                )
            else:
                raise InvalidState(f"order cannot be paid from status {order['status']}")
        _extend_device_for_order(conn, order, ts)
        conn.execute(
            """
            update orders
            set status = 'approved', paid_at = coalesce(paid_at, ?), approved_at = ?
            where id = ? and status = 'paid'
            """,
            (ts, ts, notification.order_id),
        )
        updated = conn.execute(
            "select * from orders where id = ?",
            (notification.order_id,),
        ).fetchone()
    return dict(updated)


def list_orders(
    database_path: Path,
    limit: int = 200,
    user_id: int | None = None,
) -> list[dict]:
    _maybe_expire_pending_orders(database_path)
    with connect(database_path) as conn:
        if user_id is None:
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
        else:
            rows = conn.execute(
                """
                select o.*, p.title as plan_title
                from orders o
                join devices d on d.device_id = o.device_id
                left join plans p on p.id = o.plan_id
                where d.user_id = ?
                order by o.created_at desc
                limit ?
                """,
                (user_id, limit),
            ).fetchall()
    return [dict(row) for row in rows]


def approve_order(database_path: Path, order_id: str) -> dict:
    """Approve an order exactly once and extend its device atomically.

    Uses ``BEGIN IMMEDIATE`` plus an optimistic status guard (rowcount) so
    concurrent approvals / repeated callbacks can never grant the plan twice or
    lose the update.
    """
    expire_pending_orders(database_path)
    ts = now_ts()
    with _immediate(database_path) as conn:
        order = conn.execute("select * from orders where id = ?", (order_id,)).fetchone()
        if not order:
            raise NotFound(f"order not found: {order_id}")
        if order["status"] == "approved":
            return dict(order)
        if order["status"] not in ("pending", "paid"):
            raise InvalidState(f"order cannot be approved from status {order['status']}")
        claimed = conn.execute(
            """
            update orders
            set status = 'approved', paid_at = coalesce(paid_at, ?), approved_at = ?
            where id = ? and status in ('pending', 'paid')
            """,
            (ts, ts, order_id),
        )
        if claimed.rowcount == 0:
            # Lost an approval race; the winner already extended the device.
            return dict(conn.execute("select * from orders where id = ?", (order_id,)).fetchone())
        _extend_device_for_order(conn, order, ts)
        updated = conn.execute("select * from orders where id = ?", (order_id,)).fetchone()
    return dict(updated)


def approve_order_for_key(database_path: Path, order_id: str, device_key: str) -> dict:
    device = get_device_by_key(database_path, device_key)
    order = get_order(database_path, order_id)
    if order["device_id"] != device["device_id"]:
        raise InvalidState("order does not belong to this device")
    return approve_order(database_path, order_id)


def list_usage(
    database_path: Path,
    device_id: str | None = None,
    limit: int = 200,
    user_id: int | None = None,
) -> list[dict]:
    with connect(database_path) as conn:
        conditions: list[str] = []
        params: list[object] = []
        if device_id:
            conditions.append("u.device_id = ?")
            params.append(device_id)
        if user_id is not None:
            conditions.append("d.user_id = ?")
            params.append(user_id)
        where = f"where {' and '.join(conditions)}" if conditions else ""
        rows = conn.execute(
            f"""
            select u.*
            from usage_events u
            join devices d on d.device_id = u.device_id
            {where}
            order by u.id desc
            limit ?
            """,
            (*params, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def list_generated_images(
    database_path: Path,
    device_id: str | None = None,
    limit: int = 200,
    user_id: int | None = None,
) -> list[dict]:
    with connect(database_path) as conn:
        conditions: list[str] = []
        params: list[object] = []
        if device_id:
            conditions.append("g.device_id = ?")
            params.append(device_id)
        if user_id is not None:
            conditions.append("d.user_id = ?")
            params.append(user_id)
        where = f"where {' and '.join(conditions)}" if conditions else ""
        rows = conn.execute(
            f"""
            select g.*, u.username as owner_username, u.display_name as owner_display_name
            from generated_images g
            join devices d on d.device_id = g.device_id
            left join users u on u.id = g.user_id
            {where}
            order by g.id desc
            limit ?
            """,
            (*params, limit),
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
    user_id: int | None = None,
    plan_id: str = "",
    requested_model: str = "",
    upstream_model: str = "",
    estimated_cost: float = 0,
) -> int:
    ts = now_ts()
    with connect(database_path) as conn:
        cursor = conn.execute(
            """
            insert into usage_events (
              device_id, user_id, plan_id, path, status_code,
              requested_model, upstream_model,
              prompt_tokens, completion_tokens, total_tokens, estimated_cost, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                device_id,
                user_id,
                plan_id,
                path,
                status_code,
                requested_model,
                upstream_model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                estimated_cost,
                ts,
            ),
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
        return int(cursor.lastrowid)


def record_generated_image(
    database_path: Path,
    usage_event_id: int | None,
    device_id: str,
    user_id: int | None,
    plan_id: str,
    requested_model: str,
    upstream_model: str,
    prompt: str,
    original_url: str,
    local_path: str,
    public_url: str,
    content_type: str,
    byte_size: int,
) -> int:
    with connect(database_path) as conn:
        cursor = conn.execute(
            """
            insert into generated_images (
              usage_event_id, device_id, user_id, plan_id, requested_model,
              upstream_model, prompt, original_url, local_path, public_url,
              content_type, byte_size, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
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
                content_type,
                byte_size,
                now_ts(),
            ),
        )
        return int(cursor.lastrowid)
