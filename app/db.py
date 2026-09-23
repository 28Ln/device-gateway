from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
pragma journal_mode = wal;
pragma foreign_keys = on;

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
  image_models text not null default ''
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
  path text not null,
  status_code integer not null,
  prompt_tokens integer not null default 0,
  completion_tokens integer not null default 0,
  total_tokens integer not null default 0,
  created_at integer not null,
  foreign key(device_id) references devices(device_id)
);
"""


DEFAULT_PLANS = [
    ("trial_1h", "1小时体验", 0, 1.99, "CNY", 10, 1, 0),
    ("day_1", "1天通行", 1, 9.99, "CNY", 20, 1, 0),
    ("day_3", "3天通行", 3, 19.99, "CNY", 25, 1, 0),
    ("week_1", "7天周卡", 7, 39.99, "CNY", 30, 1, 0),
    ("month_1", "30天月卡", 30, 99.99, "CNY", 40, 1, 1),
]

DEFAULT_IMAGE_MODELS = ",".join(
    [
        "[b]gpt-image-2",
        "[b]gpt-image-2.5",
        "[b]gpt-image-2.5-flare",
        "[b]gpt-image-2.5-sunburst",
    ]
)


@contextmanager
def connect(database_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


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
        plan_columns = {
            row["name"]
            for row in conn.execute("pragma table_info(plans)").fetchall()
        }
        added_plan_entitlements = False
        plan_migrations = {
            "allow_deepseek": "integer not null default 1",
            "allow_image": "integer not null default 0",
            "chat_models": "text not null default 'deepseek-v4.1-flash'",
            "image_models": "text not null default ''",
        }
        for column, column_type in plan_migrations.items():
            if column not in plan_columns:
                conn.execute(f"alter table plans add column {column} {column_type}")
                added_plan_entitlements = True
        order_columns = {
            row["name"]
            for row in conn.execute("pragma table_info(orders)").fetchall()
        }
        order_migrations = {
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
                  allow_deepseek, allow_image, chat_models, image_models
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, 'deepseek-v4.1-flash', ?)
                on conflict(id) do update set
                  title = excluded.title,
                  duration_days = excluded.duration_days,
                  price_amount = excluded.price_amount,
                  currency = excluded.currency,
                  sort_order = excluded.sort_order
                """,
                (*plan, DEFAULT_IMAGE_MODELS if plan[7] else ""),
            )
        if added_plan_entitlements:
            for plan in DEFAULT_PLANS:
                conn.execute(
                    """
                    update plans
                    set allow_deepseek = ?,
                        allow_image = ?,
                        chat_models = 'deepseek-v4.1-flash',
                        image_models = ?
                    where id = ?
                    """,
                    (plan[6], plan[7], DEFAULT_IMAGE_MODELS if plan[7] else "", plan[0]),
                )
            conn.execute(
                """
                update plans
                set image_models = ?
                where allow_image = 1 and (image_models is null or image_models = '')
                """,
                (DEFAULT_IMAGE_MODELS,),
            )
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
