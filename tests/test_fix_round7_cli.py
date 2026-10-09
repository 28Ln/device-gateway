"""Round-7 CLI/service state-consistency tests for ``enable-device``.

The HTTP ``action=enable`` path was already hardened (round 5) so it can never
persist an ``active`` device whose ``expires_at`` has already lapsed. The CLI
``enable-device`` path forwarded ``--days-if-expired`` straight into
``enable_device``, so a non-positive value (e.g. ``-5``) wrote ``status='active'``
together with a past ``expires_at`` -- the exact active+expired combination the
HTTP side forbids.

These tests pin the service-level contract and the CLI end-to-end behaviour.
"""
from __future__ import annotations

import sys
from dataclasses import replace

import pytest

import app.cli as cli
import app.service as service
from app.config import load_settings
from app.db import connect, init_db
from app.service import create_device, enable_device


MAC = "BTF:R7:ENABLE:0001"


def _past_ts() -> int:
    return service.now_ts() - 3600


def _expired_device(db, mac: str = MAC) -> None:
    """Create a device, then force it into a lapsed state."""
    create_device(db, mac, 30)
    with connect(db) as conn:
        conn.execute(
            "update devices set status = 'disabled', expires_at = ? where device_id = ?",
            (_past_ts(), mac),
        )


def _row(db, mac: str = MAC):
    with connect(db) as conn:
        return conn.execute(
            "select status, expires_at from devices where device_id = ?", (mac,)
        ).fetchone()


def _is_active_expired(row) -> bool:
    return row["status"] == "active" and row["expires_at"] <= service.now_ts()


# --- service-level: argument validation -----------------------------------


@pytest.mark.parametrize("bad", [-5, -1, 0])
def test_enable_device_rejects_non_positive_days(tmp_path, bad):
    db = tmp_path / "r7-svc-bad.sqlite3"
    init_db(db)
    create_device(db, MAC, 30)

    before = _row(db)
    with pytest.raises(ValueError):
        enable_device(db, MAC, bad)
    # Rejected before any write: the row is untouched.
    after = _row(db)
    assert (before["status"], before["expires_at"]) == (after["status"], after["expires_at"])


# --- service-level: expired device semantics ------------------------------


def test_enable_expired_without_valid_window_stays_non_active(tmp_path):
    db = tmp_path / "r7-svc-expired.sqlite3"
    init_db(db)
    _expired_device(db)

    # A non-positive window is refused rather than fabricating active+expired.
    with pytest.raises(ValueError):
        enable_device(db, MAC, -5)
    row = _row(db)
    assert not _is_active_expired(row)
    assert row["status"] != "active"


def test_enable_expired_with_positive_window_activates(tmp_path):
    db = tmp_path / "r7-svc-renew.sqlite3"
    init_db(db)
    _expired_device(db)

    device = enable_device(db, MAC, 7)
    row = _row(db)
    assert row["status"] == "active"
    assert row["expires_at"] > service.now_ts()
    assert not _is_active_expired(row)
    assert device["expires_at"] == row["expires_at"]


def test_enable_fresh_device_keeps_future_expiry(tmp_path):
    db = tmp_path / "r7-svc-fresh.sqlite3"
    init_db(db)
    create_device(db, MAC, 30)
    original = _row(db)["expires_at"]

    enable_device(db, MAC, 5)
    row = _row(db)
    assert row["status"] == "active"
    # A device that has not expired keeps its existing expiry; the window is
    # only applied to lapsed devices.
    assert row["expires_at"] == original


# --- CLI end-to-end --------------------------------------------------------


def _settings_for(db):
    return replace(load_settings(), database_path=db)


def _run_cli(monkeypatch, db, argv: list[str]) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: _settings_for(db))
    monkeypatch.setattr(sys, "argv", ["btf", *argv])
    cli.main()


def test_cli_rejects_negative_days_if_expired(tmp_path, monkeypatch, capsys):
    db = tmp_path / "r7-cli-neg.sqlite3"
    init_db(db)
    _expired_device(db)

    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, db, ["enable-device", MAC, "--days-if-expired", "-5"])
    assert "days-if-expired" in str(exc.value)
    row = _row(db)
    assert not _is_active_expired(row)
    assert row["status"] != "active"


def test_cli_rejects_zero_days_if_expired(tmp_path, monkeypatch):
    db = tmp_path / "r7-cli-zero.sqlite3"
    init_db(db)
    create_device(db, MAC, 30)

    with pytest.raises(SystemExit):
        _run_cli(monkeypatch, db, ["enable-device", MAC, "--days-if-expired", "0"])


def test_cli_positive_renewal_activates_expired_device(tmp_path, monkeypatch, capsys):
    db = tmp_path / "r7-cli-pos.sqlite3"
    init_db(db)
    _expired_device(db)

    _run_cli(monkeypatch, db, ["enable-device", MAC, "--days-if-expired", "10"])
    out = capsys.readouterr().out
    assert "status=active" in out
    row = _row(db)
    assert row["status"] == "active"
    assert row["expires_at"] > service.now_ts()
    assert not _is_active_expired(row)


def test_cli_parser_documents_positive_window():
    parser = cli.build_parser()
    args = parser.parse_args(["enable-device", MAC])
    assert args.days_if_expired == 30
    assert args.func is cli.cmd_enable_device
