"""Round-8 tests: bound CLI/service ``days`` and ``limit`` inputs.

Verification found that oversized integers (``> 2**63`` after ``days * 86400``
or a raw ``limit``) reached SQLite's INTEGER binding and raised an uncaught
``OverflowError`` (exit code 1, traceback). A merely large-but-not-overflowing
value (e.g. ``10**12`` days) additionally persisted an absurd ``expires_at``.

The Web API already guarded this via ``MAX_EXPIRES_AT`` / ``int_or_400``; these
tests pin the equivalent service-layer contract so the CLI fails the same
friendly way (``ValueError`` -> ``SystemExit``) without touching the database.
"""
from __future__ import annotations

import sys
from dataclasses import replace

import pytest

import app.cli as cli
import app.service as service
from app.config import load_settings
from app.db import connect, init_db
from app.service import (
    MAX_DEVICE_DAYS,
    MAX_LIST_LIMIT,
    create_device,
    enable_device,
    list_orders,
    list_usage,
    renew_device,
)

MAC = "BTF:R8:BOUNDS:0001"

# Huge values from the bug report plus boundary-adjacent absurdities.
OVERSIZED_DAYS = [10**12, 10**16, 2**63, 10**20]
BAD_DAYS = [0, -1, -10**16, True]
OVERSIZED_LIMITS = [10**16, 2**63, 10**20]
BAD_LIMITS = [-1, -10**16, True]


def _seed(db) -> None:
    create_device(db, MAC, 30)


def _device_row(db, mac: str = MAC):
    with connect(db) as conn:
        return conn.execute(
            "select device_id, status, expires_at, key_hash from devices where device_id = ?",
            (mac,),
        ).fetchone()


def _counts(db) -> tuple[int, int, int]:
    with connect(db) as conn:
        return (
            conn.execute("select count(*) from devices").fetchone()[0],
            conn.execute("select count(*) from orders").fetchone()[0],
            conn.execute("select count(*) from usage_events").fetchone()[0],
        )


# --- service-level: days guards -------------------------------------------


@pytest.mark.parametrize("bad", OVERSIZED_DAYS + BAD_DAYS)
def test_create_device_rejects_bad_days_without_write(tmp_path, bad):
    db = tmp_path / "r8-create.sqlite3"
    init_db(db)
    before = _counts(db)
    with pytest.raises(ValueError):
        create_device(db, MAC, bad)
    assert _counts(db) == before
    assert _device_row(db) is None


@pytest.mark.parametrize("bad", OVERSIZED_DAYS + BAD_DAYS)
def test_renew_device_rejects_bad_days_without_write(tmp_path, bad):
    db = tmp_path / "r8-renew.sqlite3"
    init_db(db)
    _seed(db)
    before = _device_row(db)
    with pytest.raises(ValueError):
        renew_device(db, MAC, bad)
    after = _device_row(db)
    assert (before["status"], before["expires_at"]) == (after["status"], after["expires_at"])


@pytest.mark.parametrize("bad", OVERSIZED_DAYS + BAD_DAYS)
def test_enable_device_rejects_bad_days_without_write(tmp_path, bad):
    db = tmp_path / "r8-enable.sqlite3"
    init_db(db)
    _seed(db)
    before = _device_row(db)
    with pytest.raises(ValueError):
        enable_device(db, MAC, bad)
    after = _device_row(db)
    assert (before["status"], before["expires_at"]) == (after["status"], after["expires_at"])


def test_import_and_batch_reject_bad_days(tmp_path):
    db = tmp_path / "r8-import.sqlite3"
    init_db(db)
    before = _counts(db)
    with pytest.raises(ValueError):
        service.import_devices(db, [MAC], 10**16)
    with pytest.raises(ValueError):
        service.batch_create_devices(db, "BTF:R8:", 1, 10**16)
    assert _counts(db) == before


# --- service-level: limit guards ------------------------------------------


@pytest.mark.parametrize("bad", OVERSIZED_LIMITS + BAD_LIMITS)
def test_list_usage_rejects_bad_limit(tmp_path, bad):
    db = tmp_path / "r8-usage.sqlite3"
    init_db(db)
    _seed(db)
    with pytest.raises(ValueError):
        list_usage(db, None, bad)


@pytest.mark.parametrize("bad", OVERSIZED_LIMITS + BAD_LIMITS)
def test_list_orders_rejects_bad_limit(tmp_path, bad):
    db = tmp_path / "r8-orders.sqlite3"
    init_db(db)
    _seed(db)
    with pytest.raises(ValueError):
        list_orders(db, bad)


def test_overflow_is_never_raised(tmp_path):
    """The regression is an uncaught OverflowError, not just any error."""
    db = tmp_path / "r8-overflow.sqlite3"
    init_db(db)
    _seed(db)
    for call in (
        lambda: create_device(db, "BTF:R8:NEW", 10**16),
        lambda: renew_device(db, MAC, 10**16),
        lambda: enable_device(db, MAC, 10**16),
        lambda: list_usage(db, None, 10**20),
        lambda: list_orders(db, 10**20),
    ):
        with pytest.raises(ValueError):
            call()


# --- service-level: legal boundaries --------------------------------------


def test_days_boundary(tmp_path):
    db = tmp_path / "r8-days-bound.sqlite3"
    init_db(db)
    create_device(db, MAC, MAX_DEVICE_DAYS)  # exactly at the cap: allowed
    assert _device_row(db) is not None
    with pytest.raises(ValueError):
        create_device(db, "BTF:R8:OVER", MAX_DEVICE_DAYS + 1)


def test_limit_boundary(tmp_path):
    db = tmp_path / "r8-limit-bound.sqlite3"
    init_db(db)
    _seed(db)
    # At cap and zero (the historical "unbounded-ish" floor) are both accepted.
    assert list_usage(db, None, MAX_LIST_LIMIT) == []
    assert list_usage(db, None, 0) == []
    assert list_orders(db, MAX_LIST_LIMIT) == []
    assert list_orders(db, 0) == []
    with pytest.raises(ValueError):
        list_usage(db, None, MAX_LIST_LIMIT + 1)
    with pytest.raises(ValueError):
        list_orders(db, MAX_LIST_LIMIT + 1)


def test_non_int_types_rejected(tmp_path):
    db = tmp_path / "r8-types.sqlite3"
    init_db(db)
    with pytest.raises(ValueError):
        create_device(db, MAC, "365")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        list_usage(db, None, 3.5)  # type: ignore[arg-type]


# --- CLI end-to-end --------------------------------------------------------


def _settings_for(db):
    return replace(load_settings(), database_path=db)


def _run_cli(monkeypatch, db, argv: list[str]) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: _settings_for(db))
    monkeypatch.setattr(sys, "argv", ["btf", *argv])
    cli.main()


@pytest.mark.parametrize(
    "argv",
    [
        ["create-device", "BTF:R8:CLI1", "--days", str(10**16)],
        ["renew-device", MAC, "--days", str(10**16)],
        ["enable-device", MAC, "--days-if-expired", str(10**16)],
        ["list-usage", "--limit", str(10**20)],
        ["list-orders", "--limit", str(10**20)],
        ["create-device", "BTF:R8:CLI2", "--days", str(10**12)],
    ],
)
def test_cli_huge_values_exit_cleanly(tmp_path, monkeypatch, argv):
    db = tmp_path / "r8-cli.sqlite3"
    init_db(db)
    _seed(db)
    before = _counts(db)
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, db, argv)
    # Friendly message, not a traceback.
    assert "days" in str(exc.value) or "limit" in str(exc.value)
    assert _counts(db) == before
    assert _device_row(db, "BTF:R8:CLI1") is None
    assert _device_row(db, "BTF:R8:CLI2") is None


def test_cli_rejects_oversized_import_days(tmp_path, monkeypatch):
    db = tmp_path / "r8-cli-import.sqlite3"
    init_db(db)
    src = tmp_path / "ids.txt"
    src.write_text("BTF:R8:IMP:0001\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        _run_cli(monkeypatch, db, ["import-devices", str(src), "--days", str(10**16)])


def test_cli_boundary_days_accepted(tmp_path, monkeypatch, capsys):
    db = tmp_path / "r8-cli-ok.sqlite3"
    init_db(db)
    _run_cli(monkeypatch, db, ["create-device", MAC, "--days", str(MAX_DEVICE_DAYS)])
    assert "device_id=" in capsys.readouterr().out
    assert _device_row(db) is not None
