"""Round-8 final-verification findings: LOW/INFO fixes.

Covers three findings:

* Finding 1 (LOW): ``service.create_device`` only translated a duplicate
  ``device_id`` to ``Conflict``; a reused ``device_key`` leaked a raw
  ``sqlite3.IntegrityError`` (UNIQUE ``key_hash``) out of the CLI as an
  uncaught traceback. It must now raise a domain ``Conflict``.
* Finding 2 (LOW): CLI ``create-device --days MAX_DEVICE_DAYS`` could persist
  an ``expires_at`` beyond the Web layer's ``MAX_EXPIRES_AT``, so a later
  write-back (``PUT /api/device/``) was refused with 400. The computed expiry is
  now clamped to the single shared ``MAX_EXPIRES_AT`` constant.
* Finding 3 (INFO): ``.env.example`` was missing five env vars read by
  ``app.config``.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import app.main as main
import app.service as service
from app.db import connect, init_db
from app.service import (
    MAX_DEVICE_DAYS,
    MAX_EXPIRES_AT,
    Conflict,
    create_device,
    renew_device,
    enable_device,
)

ROOT = Path(__file__).resolve().parents[1]

# Env vars read by app/config.py that must be documented in .env.example.
DOCUMENTED_ENV = [
    "BTF_DATA_DIR",
    "BTF_ORDER_EXPIRES_SECONDS",
    "BTF_CHAT_PROMPT_COST_PER_MILLION",
    "BTF_CHAT_COMPLETION_COST_PER_MILLION",
    "BTF_IMAGE_COST_PER_REQUEST",
]


def _row(db, device_id: str):
    with connect(db) as conn:
        return conn.execute(
            "select device_id, expires_at, key_hash from devices where device_id = ?",
            (device_id,),
        ).fetchone()


# --- Finding 1: duplicate key -> Conflict (not IntegrityError) -------------


def test_create_device_duplicate_key_raises_conflict(tmp_path):
    db = tmp_path / "r8f-dup-key.sqlite3"
    init_db(db)
    create_device(db, "BTF:R8F:A", 30, device_key="shared")
    with pytest.raises(Conflict):
        create_device(db, "BTF:R8F:B", 30, device_key="shared")
    # The first device is untouched and the second never landed.
    assert _row(db, "BTF:R8F:A") is not None
    assert _row(db, "BTF:R8F:B") is None


def test_create_device_duplicate_id_still_conflict(tmp_path):
    db = tmp_path / "r8f-dup-id.sqlite3"
    init_db(db)
    create_device(db, "BTF:R8F:SAME", 30)
    with pytest.raises(Conflict):
        create_device(db, "BTF:R8F:SAME", 30)


def test_conflict_message_has_no_sql(tmp_path):
    db = tmp_path / "r8f-msg.sqlite3"
    init_db(db)
    create_device(db, "BTF:R8F:A2", 30, device_key="shared2")
    with pytest.raises(Conflict) as exc:
        create_device(db, "BTF:R8F:B2", 30, device_key="shared2")
    message = str(exc.value)
    assert "UNIQUE" not in message
    assert "constraint" not in message
    assert "sqlite" not in message.lower()


def test_cli_duplicate_key_exits_cleanly(tmp_path):
    """CLI subprocess: rc != 0 and no raw traceback / IntegrityError on stderr."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env = {
        "BTF_DATA_DIR": str(data_dir),
        "PATH": "/usr/bin:/bin",
    }
    first = subprocess.run(
        [sys.executable, "-m", "app.cli", "create-device", "AAA:1", "--key", "shared"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stderr

    second = subprocess.run(
        [sys.executable, "-m", "app.cli", "create-device", "BBB:2", "--key", "shared"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert second.returncode != 0
    assert "Traceback" not in second.stderr
    assert "IntegrityError" not in second.stderr
    assert "already exists" in second.stderr


# --- Finding 2: expires_at clamped to MAX_EXPIRES_AT -----------------------


def test_create_device_clamps_expires_at(tmp_path):
    db = tmp_path / "r8f-cap.sqlite3"
    init_db(db)
    # 36500 days * 86400 easily exceeds MAX_EXPIRES_AT (~2100) but stays a legal
    # day count, so it must be accepted and clamped rather than rejected.
    _key, device = create_device(db, "BTF:R8F:CAP", MAX_DEVICE_DAYS)
    assert device["expires_at"] <= MAX_EXPIRES_AT
    assert device["expires_at"] == MAX_EXPIRES_AT


def test_clamped_expiry_accepted_by_web_parser(tmp_path):
    """The clamped value round-trips through the Web layer's validator."""
    db = tmp_path / "r8f-cap-web.sqlite3"
    init_db(db)
    _key, device = create_device(db, "BTF:R8F:CAPWEB", MAX_DEVICE_DAYS)
    # Must not raise HTTPException(400) anymore.
    assert main.positive_expires_at(device["expires_at"]) == MAX_EXPIRES_AT


def test_renew_and_enable_also_clamp(tmp_path):
    db = tmp_path / "r8f-cap-renew.sqlite3"
    init_db(db)
    create_device(db, "BTF:R8F:REN", 30)
    assert renew_device(db, "BTF:R8F:REN", MAX_DEVICE_DAYS)["expires_at"] <= MAX_EXPIRES_AT
    # Force the device into a lapsed state, then enable with an extreme window.
    with connect(db) as conn:
        conn.execute(
            "update devices set expires_at = 1 where device_id = ?", ("BTF:R8F:REN",)
        )
    assert enable_device(db, "BTF:R8F:REN", MAX_DEVICE_DAYS)["expires_at"] <= MAX_EXPIRES_AT


def test_web_and_service_share_single_constant():
    assert main.MAX_EXPIRES_AT is service.MAX_EXPIRES_AT


def test_normal_expiry_unaffected_by_clamp(tmp_path):
    db = tmp_path / "r8f-cap-normal.sqlite3"
    init_db(db)
    _key, device = create_device(db, "BTF:R8F:NORMAL", 30)
    # A routine 30-day window is far below the cap and must be untouched.
    assert device["expires_at"] < MAX_EXPIRES_AT


# --- Finding 3: .env.example documents all config env vars -----------------


@pytest.mark.parametrize("name", DOCUMENTED_ENV)
def test_env_example_documents_var(name):
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert name in text
