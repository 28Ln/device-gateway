from __future__ import annotations

import sqlite3
import time

import pytest

from app.db import connect, init_db, MIGRATIONS
from app.service import create_device


def _plan_row(db, plan_id):
    with connect(db) as conn:
        return conn.execute("select * from plans where id = ?", (plan_id,)).fetchone()


def test_connect_enables_foreign_keys_and_busy_timeout(tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    # A brand-new connection must re-enable the per-connection pragmas;
    # executing them once inside the schema script is not enough.
    with connect(db) as conn:
        assert conn.execute("pragma foreign_keys").fetchone()[0] == 1
        assert conn.execute("pragma busy_timeout").fetchone()[0] == 5000


def test_foreign_keys_block_orphan_rows(tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    create_device(db, "BTF:FK:DEVICE", 1)

    with connect(db) as conn:
        # device_id references a device that does not exist.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                insert into orders (
                  id, device_id, plan_id, status, amount, currency, created_at
                ) values ('btf_ord_orphan_device', 'BTF:FK:MISSING', 'day_1',
                          'pending', 1.0, 'CNY', ?)
                """,
                (int(time.time()),),
            )
        # plan_id references a plan that does not exist.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                insert into orders (
                  id, device_id, plan_id, status, amount, currency, created_at
                ) values ('btf_ord_orphan_plan', 'BTF:FK:DEVICE', 'no_such_plan',
                          'pending', 1.0, 'CNY', ?)
                """,
                (int(time.time()),),
            )

    with connect(db) as conn:
        assert conn.execute("select count(*) from orders").fetchone()[0] == 0


def test_init_db_creates_all_tables_and_builtin_plans(tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)

    with connect(db) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "select name from sqlite_master where type = 'table'"
            ).fetchall()
        }
        plan_ids = {
            row["id"] for row in conn.execute("select id from plans").fetchall()
        }
    expected_tables = {
        "schema_version",
        "devices",
        "users",
        "plans",
        "orders",
        "payment_transactions",
        "usage_events",
        "generated_images",
    }
    assert expected_tables <= tables

    expected_plans = {"trial_1h", "day_1", "day_3", "week_1", "month_1"}
    assert plan_ids == expected_plans


def test_repeated_init_db_is_idempotent(tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    create_device(db, "BTF:IDEMPOTENT", 1)

    init_db(db)
    init_db(db)

    with connect(db) as conn:
        assert conn.execute("select count(*) from plans").fetchone()[0] == 5
        assert conn.execute("select count(*) from devices").fetchone()[0] == 1
        versions = [
            row["version"] for row in conn.execute(
                "select version from schema_version order by version"
            ).fetchall()
        ]
    # Each migration is recorded exactly once, in order, and no duplicates.
    assert versions == [version for version, _ in sorted(MIGRATIONS)]


def test_admin_edits_to_builtin_plan_survive_reinit(tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)

    with connect(db) as conn:
        conn.execute(
            """
            update plans
            set title = '管理员自定义套餐',
                price_amount = 123.45,
                token_limit = 999999,
                daily_request_limit = 4242,
                chat_alias = 'my-chat',
                enabled = 0
            where id = 'day_1'
            """
        )

    init_db(db)
    init_db(db)

    row = _plan_row(db, "day_1")
    assert row["title"] == "管理员自定义套餐"
    assert row["price_amount"] == 123.45
    assert row["token_limit"] == 999999
    assert row["daily_request_limit"] == 4242
    assert row["chat_alias"] == "my-chat"
    assert row["enabled"] == 0
    # Untouched plans keep their built-in values.
    assert _plan_row(db, "month_1")["title"] == "30天月卡"
