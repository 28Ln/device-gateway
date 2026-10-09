from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


SCHEMA = """
pragma journal_mode = wal;
pragma foreign_keys = on;

create table if not exists schema_version (
  version integer not null
);

create table if not exists devices (
  id integer primary key autoincrement,
  device_id text not null unique,
  user_id integer,
  key_hash text not null unique,
  key_mask text not null,
  status text not null default 'active',
  expires_at integer not null,
  created_at integer not null,
  updated_at integer not null,
  last_seen_at integer,
  request_count integer not null default 0,
  prompt_tokens integer not null default 0,
  completion_tokens integer not null default 0,
  total_tokens integer not null default 0,
  plan_id text,
  plan_started_at integer,
  bind_code_hash text,
  bind_code_mask text,
  bound_at integer,
  note text not null default ''
);

create table if not exists users (
  id integer primary key autoincrement,
  username text not null unique,
  display_name text not null,
  contact text not null default '',
  status text not null default 'active',
  created_at integer not null,
  updated_at integer not null,
  request_limit integer not null default 0,
  daily_request_limit integer not null default 0,
  token_limit integer not null default 0,
  daily_token_limit integer not null default 0,
  rpm_limit integer not null default 0,
  concurrency_limit integer not null default 0,
  note text not null default ''
);

create table if not exists plans (
  id text primary key,
  title text not null,
  duration_days integer not null,
  price_amount real not null,
  currency text not null default 'CNY',
  sort_order integer not null default 0,
  enabled integer not null default 1,
  allow_deepseek integer not null default 1,
  allow_image integer not null default 0,
  chat_models text not null default 'deepseek-v4.1-flash',
  image_models text not null default '',
  token_limit integer not null default 0,
  daily_token_limit integer not null default 0,
  request_limit integer not null default 0,
  daily_request_limit integer not null default 0,
  image_limit integer not null default 0,
  daily_image_limit integer not null default 0,
  rpm_limit integer not null default 0,
  concurrency_limit integer not null default 0,
  chat_alias text not null default 'btf-chat',
  image_alias text not null default 'btf-image'
);

create table if not exists orders (
  id text primary key,
  device_id text not null,
  plan_id text not null,
  status text not null default 'pending',
  amount real not null,
  currency text not null,
  created_at integer not null,
  paid_at integer,
  approved_at integer,
  closed_at integer,
  close_reason text,
  payment_provider text,
  provider_order_id text,
  payment_url text,
  payment_expires_at integer,
  foreign key(device_id) references devices(device_id),
  foreign key(plan_id) references plans(id)
);

create table if not exists payment_transactions (
  id integer primary key autoincrement,
  order_id text not null,
  provider text not null,
  provider_order_id text not null,
  provider_transaction_id text,
  status text not null default 'created',
  amount real not null,
  currency text not null,
  checkout_url text,
  idempotency_key text,
  created_at integer not null,
  paid_at integer,
  raw_payload text not null default '',
  unique(provider, provider_order_id),
  foreign key(order_id) references orders(id)
);

create table if not exists usage_events (
  id integer primary key autoincrement,
  device_id text not null,
  user_id integer,
  plan_id text,
  path text not null,
  status_code integer not null,
  requested_model text not null default '',
  upstream_model text not null default '',
  prompt_tokens integer not null default 0,
  completion_tokens integer not null default 0,
  total_tokens integer not null default 0,
  estimated_cost real not null default 0,
  created_at integer not null,
  foreign key(device_id) references devices(device_id)
);

create table if not exists generated_images (
  id integer primary key autoincrement,
  usage_event_id integer,
  device_id text not null,
  user_id integer,
  plan_id text,
  requested_model text not null default '',
  upstream_model text not null default '',
  prompt text not null default '',
  original_url text not null default '',
  local_path text not null,
  public_url text not null,
  content_type text not null default '',
  byte_size integer not null default 0,
  created_at integer not null,
  foreign key(usage_event_id) references usage_events(id),
  foreign key(device_id) references devices(device_id)
);
"""


DEFAULT_PLANS = [
    ("trial_1h", "1小时体验", 0, 1.99, "CNY", 10, 1, 0, 10_000, 10_000, 100, 100, 0, 0, 10, 1),
    ("day_1", "1天通行", 1, 9.99, "CNY", 20, 1, 0, 100_000, 100_000, 1_000, 1_000, 0, 0, 30, 1),
    ("day_3", "3天通行", 3, 19.99, "CNY", 25, 1, 0, 300_000, 120_000, 3_000, 1_200, 0, 0, 40, 1),
    ("week_1", "7天周卡", 7, 39.99, "CNY", 30, 1, 0, 800_000, 150_000, 8_000, 1_500, 0, 0, 50, 2),
    ("month_1", "30天月卡", 30, 99.99, "CNY", 40, 1, 1, 3_000_000, 200_000, 30_000, 2_000, 100, 10, 60, 2),
]

DEFAULT_CHAT_MODELS = "deepseek-v4.1-flash,gpt-5.5"
DEFAULT_IMAGE_MODELS = "[b2]gpt-image-2"


@contextmanager
def connect(database_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    # Enable per-connection safety pragmas. ``foreign_keys`` is a no-op inside
    # a transaction, so it has to run on every fresh connection (not once in
    # ``executescript``) for foreign keys to be genuinely enforced. The busy
    # timeout keeps transient writer lock contention from raising immediately.
    conn.execute("pragma busy_timeout=5000")
    conn.execute("pragma foreign_keys=on")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _applied_migrations(conn: sqlite3.Connection) -> set[int]:
    try:
        rows = conn.execute("select version from schema_version").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {int(row[0]) for row in rows}


def _record_migration(conn: sqlite3.Connection, version: int) -> None:
    conn.execute("insert into schema_version(version) values (?)", (version,))


def _backfill_usage_attribution(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        update usage_events
        set user_id = (
                select user_id from devices
                where devices.device_id = usage_events.device_id
            ),
            plan_id = coalesce(plan_id, (
                select plan_id from devices
                where devices.device_id = usage_events.device_id
            ))
        where user_id is null or plan_id is null or plan_id = ''
        """
    )


def _apply_data_migrations(conn: sqlite3.Connection) -> None:
    """Ordered, idempotent data migrations guarded by ``schema_version``.

    Each entry runs once per database. Entries must therefore be safe to skip
    for fresh installations (the base ``SCHEMA`` + ``DEFAULT_PLANS`` insert
    already produce the intended state) and must never overwrite values an
    administrator has customized.
    """
    applied = _applied_migrations(conn)
    for version, migrate in MIGRATIONS:
        if version in applied:
            continue
        migrate(conn)
        _record_migration(conn, version)


def _apply_forward_migration(conn: sqlite3.Connection) -> None:
    """Targeted, idempotent fix-ups that are cheap enough to re-check.

    Unlike ``MIGRATIONS`` this is not version-gated: it only rewrites values
    that still equal a previous built-in default, so re-running it is
    idempotent and never touches administrator-customized plans.
    """
    _migration_legacy_plan_defaults(conn)


def _migration_legacy_plan_defaults(conn: sqlite3.Connection) -> None:
    # Installations created before the GPT-5.5 chat model and the corrected
    # image model carry the previous built-in defaults. Only rows whose value
    # exactly equals the old built-in default are rewritten, so any plan an
    # administrator customized is left untouched.
    previous_chat_models = "deepseek-v4.1-flash"
    previous_image_models = "[b]gpt-image-2"
    for plan in DEFAULT_PLANS:
        conn.execute(
            """
            update plans
            set chat_models = ?
            where id = ? and chat_models = ?
            """,
            (DEFAULT_CHAT_MODELS, plan[0], previous_chat_models),
        )
        if plan[7]:
            conn.execute(
                """
                update plans
                set image_models = ?
                where id = ? and image_models = ?
                """,
                (DEFAULT_IMAGE_MODELS, plan[0], previous_image_models),
            )
    conn.execute(
        """
        update plans
        set image_models = ?
        where allow_image = 1 and (image_models is null or image_models = '')
        """,
        (DEFAULT_IMAGE_MODELS,),
    )


def _migration_backfill_usage_attribution(conn: sqlite3.Connection) -> None:
    _backfill_usage_attribution(conn)


# Version-guarded, run-once data migrations. Version numbers are an ordered
# schema revision log; they are independent of the Python schema-generation
# column patches (which are individually guarded by ``pragma table_info``).
MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _migration_backfill_usage_attribution),
]


def init_db(database_path: Path) -> None:
    with connect(database_path) as conn:
        conn.executescript(SCHEMA)
        device_columns = {
            row["name"]
            for row in conn.execute("pragma table_info(devices)").fetchall()
        }
        if "user_id" not in device_columns:
            conn.execute("alter table devices add column user_id integer")
        if "plan_id" not in device_columns:
            conn.execute("alter table devices add column plan_id text")
            conn.execute(
                """
                update devices
                set plan_id = case
                  when note like '%30天%' then 'month_1'
                  when note like '%7天%' then 'week_1'
                  when note like '%3天%' then 'day_3'
                  when note like '%1天%' then 'day_1'
                  when note like '%1小时%' then 'trial_1h'
                  else 'day_1'
                end
                where plan_id is null or plan_id = ''
                """
            )
        device_migrations = {
            "plan_started_at": "integer",
            "bind_code_hash": "text",
            "bind_code_mask": "text",
            "bound_at": "integer",
        }
        for column, column_type in device_migrations.items():
            if column not in device_columns:
                conn.execute(f"alter table devices add column {column} {column_type}")
        conn.execute(
            """
            update devices
            set plan_started_at = coalesce(plan_started_at, created_at)
            where plan_started_at is null
            """
        )
        user_columns = {
            row["name"]
            for row in conn.execute("pragma table_info(users)").fetchall()
        }
        user_migrations = {
            "request_limit": "integer not null default 0",
            "daily_request_limit": "integer not null default 0",
            "token_limit": "integer not null default 0",
            "daily_token_limit": "integer not null default 0",
            "rpm_limit": "integer not null default 0",
            "concurrency_limit": "integer not null default 0",
        }
        for column, column_type in user_migrations.items():
            if column not in user_columns:
                conn.execute(f"alter table users add column {column} {column_type}")
        plan_columns = {
            row["name"]
            for row in conn.execute("pragma table_info(plans)").fetchall()
        }
        plan_migrations = {
            "allow_deepseek": "integer not null default 1",
            "allow_image": "integer not null default 0",
            "chat_models": "text not null default 'deepseek-v4.1-flash'",
            "image_models": "text not null default ''",
            "token_limit": "integer not null default 0",
            "daily_token_limit": "integer not null default 0",
            "request_limit": "integer not null default 0",
            "daily_request_limit": "integer not null default 0",
            "image_limit": "integer not null default 0",
            "daily_image_limit": "integer not null default 0",
            "rpm_limit": "integer not null default 0",
            "concurrency_limit": "integer not null default 0",
            "chat_alias": "text not null default 'btf-chat'",
            "image_alias": "text not null default 'btf-image'",
        }
        for column, column_type in plan_migrations.items():
            if column not in plan_columns:
                conn.execute(f"alter table plans add column {column} {column_type}")
        order_columns = {
            row["name"]
            for row in conn.execute("pragma table_info(orders)").fetchall()
        }
        order_migrations = {
            "closed_at": "integer",
            "close_reason": "text",
            "payment_provider": "text",
            "provider_order_id": "text",
            "payment_url": "text",
            "payment_expires_at": "integer",
        }
        for column, column_type in order_migrations.items():
            if column not in order_columns:
                conn.execute(f"alter table orders add column {column} {column_type}")
        payment_columns = {
            row["name"]
            for row in conn.execute("pragma table_info(payment_transactions)").fetchall()
        }
        if "idempotency_key" not in payment_columns:
            conn.execute("alter table payment_transactions add column idempotency_key text")
        if "provider_transaction_id" not in payment_columns:
            conn.execute(
                "alter table payment_transactions add column provider_transaction_id text"
            )
        usage_columns = {
            row["name"]
            for row in conn.execute("pragma table_info(usage_events)").fetchall()
        }
        usage_migrations = {
            "user_id": "integer",
            "plan_id": "text",
            "requested_model": "text not null default ''",
            "upstream_model": "text not null default ''",
            "estimated_cost": "real not null default 0",
        }
        for column, column_type in usage_migrations.items():
            if column not in usage_columns:
                conn.execute(f"alter table usage_events add column {column} {column_type}")
        conn.execute(
            "create index if not exists idx_usage_user_created on usage_events(user_id, created_at)"
        )
        conn.execute(
            "create index if not exists idx_usage_device_created on usage_events(device_id, created_at)"
        )
        conn.execute(
            "create index if not exists idx_generated_images_device_created on generated_images(device_id, created_at)"
        )
        conn.execute(
            "create index if not exists idx_generated_images_user_created on generated_images(user_id, created_at)"
        )
        conn.execute(
            """
            create unique index if not exists idx_payment_idempotency
              on payment_transactions(order_id, provider, idempotency_key)
              where idempotency_key is not null
            """
        )
        for plan in DEFAULT_PLANS:
            conn.execute(
                """
                insert into plans (
                  id, title, duration_days, price_amount, currency, sort_order,
                  allow_deepseek, allow_image, chat_models, image_models,
                  token_limit, daily_token_limit, request_limit, daily_request_limit,
                  image_limit, daily_image_limit, rpm_limit, concurrency_limit,
                  chat_alias, image_alias
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, 'btf-chat', 'btf-image')
                on conflict(id) do nothing
                """,
                (
                    *plan[:8],
                    DEFAULT_CHAT_MODELS,
                    DEFAULT_IMAGE_MODELS if plan[7] else "",
                    *plan[8:],
                ),
            )

        # Ordered, version-guarded data migrations (idempotent, once per
        # database) plus a cheap re-runnable forward migration. Fresh
        # databases already hold the intended state via ``SCHEMA`` and the
        # insert-or-ignore above, so both are no-ops there while upgrading
        # legacy databases without overwriting administrator customizations.
        _apply_data_migrations(conn)
        _apply_forward_migration(conn)

        conn.execute(
            """
            update devices
            set plan_id = case
              when note like '%30天%' then 'month_1'
              when note like '%7天%' then 'week_1'
              when note like '%3天%' then 'day_3'
              when note like '%1天%' then 'day_1'
              when note like '%1小时%' then 'trial_1h'
              else 'day_1'
            end
            where plan_id is null or plan_id = ''
            """
        )


def now_ts() -> int:
    return int(time.time())
