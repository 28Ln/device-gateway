"""Regression tests for the CLI hardening in app/cli.py.

Covers two properties required by the repo-hygiene task:

* device keys printed by the CLI are masked unless ``--show-secret`` is given;
* the default model printed by create/batch/import/rotate is derived from the
  configured plans instead of being hardcoded.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import app.cli as cli
from app.config import load_settings
from app.db import connect, init_db
from app.service import NotFound, create_device, get_device_by_key


def _settings_for(db):
    return replace(load_settings(), database_path=db)


def _api_key_line(out: str) -> str:
    line = next(line for line in out.splitlines() if line.startswith("api_key="))
    return line.split("=", 1)[1]


def test_emit_api_key_masks_by_default(capsys):
    cli.emit_api_key("btf_dev_supersecretvalue")
    out = capsys.readouterr().out
    assert "supersecretvalue" not in out
    assert "api_key=" in out

    cli.emit_api_key("btf_dev_supersecretvalue", True)
    revealed = capsys.readouterr().out
    assert "btf_dev_supersecretvalue" in revealed


def test_create_device_masks_api_key(tmp_path, monkeypatch, capsys):
    db = tmp_path / "cli.sqlite3"
    init_db(db)
    monkeypatch.setattr(cli, "load_settings", lambda: _settings_for(db))

    args = cli.build_parser().parse_args(["create-device", "BTF:CLI:MASK:0001"])
    args.func(args)
    out = capsys.readouterr().out

    # New devices use their device_id as the key; the printed key must not be a
    # full credential (either "***" or a truncated mask), never the raw value.
    shown = _api_key_line(out)
    assert shown in {"***"} or "..." in shown
    assert "device_id=BTF:CLI:MASK:0001" in out


def test_rotate_key_masks_and_show_secret_reveals(tmp_path, monkeypatch, capsys):
    db = tmp_path / "cli.sqlite3"
    init_db(db)
    monkeypatch.setattr(cli, "load_settings", lambda: _settings_for(db))
    create_device(db, "BTF:CLI:ROTATE:0001", 30)
    parser = cli.build_parser()

    args = parser.parse_args(["rotate-key", "BTF:CLI:ROTATE:0001"])
    args.func(args)
    masked = _api_key_line(capsys.readouterr().out)
    # The mask keeps a recognisable prefix/suffix but is never a usable key.
    assert "..." in masked
    assert len(masked) < 40
    with pytest.raises(NotFound):
        get_device_by_key(db, masked)

    args = parser.parse_args(["rotate-key", "BTF:CLI:ROTATE:0001", "--show-secret"])
    args.func(args)
    revealed = _api_key_line(capsys.readouterr().out)
    assert revealed.startswith("btf_dev_")
    # The revealed key is the real rotated credential, not a placeholder.
    assert get_device_by_key(db, revealed)["device_id"] == "BTF:CLI:ROTATE:0001"


def test_batch_and_import_output_is_masked(tmp_path, monkeypatch, capsys):
    db = tmp_path / "cli.sqlite3"
    init_db(db)
    monkeypatch.setattr(cli, "load_settings", lambda: _settings_for(db))
    parser = cli.build_parser()

    args = parser.parse_args(["batch-create", "BTF:CLI:B", "--count", "2"])
    args.func(args)
    out = capsys.readouterr().out
    for line in out.splitlines()[1:]:
        assert "BTF:CLI:" not in line.split(",")[1]  # column 2 is api_key

    ids = tmp_path / "ids.txt"
    ids.write_text("BTF:CLI:IMP:0001\nBTF:CLI:IMP:0002\n")
    args = parser.parse_args(["import-devices", str(ids)])
    args.func(args)
    out = capsys.readouterr().out
    for line in out.splitlines()[1:]:
        assert "BTF:CLI:" not in line.split(",")[1]


def test_cli_model_follows_configured_plans(tmp_path, monkeypatch, capsys):
    db = tmp_path / "cli.sqlite3"
    init_db(db)
    monkeypatch.setattr(cli, "load_settings", lambda: _settings_for(db))
    parser = cli.build_parser()

    # Built-in plan defaults resolve to the DeepSeek default chat model.
    args = parser.parse_args(["create-device", "BTF:CLI:MODEL:0001"])
    args.func(args)
    assert "model=deepseek-v4.1-flash" in capsys.readouterr().out

    # Reconfigure the enabled plans and confirm the CLI follows the database
    # instead of printing a hardcoded model name.
    with connect(db) as conn:
        conn.execute("update plans set chat_models = ? where enabled = 1", ("gpt-5.5",))

    args = parser.parse_args(["create-device", "BTF:CLI:MODEL:0002"])
    args.func(args)
    assert "model=gpt-5.5" in capsys.readouterr().out


def test_derive_default_model_falls_back_without_plans(tmp_path):
    db = tmp_path / "cli.sqlite3"
    init_db(db)
    with connect(db) as conn:
        conn.execute("update plans set enabled = 0")
    # Derivation must never fail even when no plan is enabled.
    assert cli.derive_default_model(db) == cli.DEFAULT_CHAT_MODEL


def test_import_source_enforces_size_limits(tmp_path):
    big = tmp_path / "big.txt"
    big.write_bytes(b"BTF:CLI:X\n" * (cli.MAX_IMPORT_BYTES // 10 + 1))
    with pytest.raises(ValueError):
        cli.read_import_source(str(big))


def test_import_devices_rejects_too_many_lines(tmp_path, monkeypatch):
    db = tmp_path / "cli.sqlite3"
    init_db(db)
    monkeypatch.setattr(cli, "load_settings", lambda: _settings_for(db))
    ids = tmp_path / "many.txt"
    ids.write_text("\n".join(f"BTF:CLI:{i:06d}" for i in range(cli.MAX_IMPORT_LINES + 1)))

    args = cli.build_parser().parse_args(["import-devices", str(ids)])
    with pytest.raises(ValueError):
        args.func(args)
