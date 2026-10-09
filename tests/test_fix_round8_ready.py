"""Round 8: ``/ready`` must validate DB structure, not just ``SELECT 1``.

Regression: SQLite opens a zero-byte or corrupt file and still answers
``SELECT 1``, so ``/ready`` used to report ``checks.database=true`` (a false
positive) while ``/v1`` handlers 500. Readiness now also requires the core
tables (``devices``, ``schema_version``) and a passing ``pragma quick_check``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from fastapi.testclient import TestClient

import app.main as main
from app.db import init_db


def _point_at_db(monkeypatch, db_path):
    monkeypatch.setattr(
        main,
        "settings",
        replace(main.settings, database_path=db_path, new_api_base_url=""),
    )


def test_ready_ok_with_healthy_database(monkeypatch, tmp_path):
    db = tmp_path / "healthy.sqlite3"
    init_db(db)
    _point_at_db(monkeypatch, db)

    client = TestClient(main.app)
    assert client.get("/health").status_code == 200
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["checks"]["database"] is True


def test_ready_503_for_zero_byte_database(monkeypatch, tmp_path):
    db = tmp_path / "empty.sqlite3"
    db.write_bytes(b"")  # zero bytes: SQLite treats it as an empty database
    _point_at_db(monkeypatch, db)

    client = TestClient(main.app)
    # Liveness stays green (no DB I/O).
    assert client.get("/health").status_code == 200
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["checks"]["database"] is False


def test_ready_503_for_corrupt_database_without_500(monkeypatch, tmp_path):
    db = tmp_path / "corrupt.sqlite3"
    db.write_bytes(b"\x00\x01this is not a sqlite database" * 64)
    _point_at_db(monkeypatch, db)

    client = TestClient(main.app)
    assert client.get("/health").status_code == 200
    response = client.get("/ready")
    assert response.status_code == 503  # not a 500
    body = response.json()
    assert body["ok"] is False
    assert body["checks"]["database"] is False


def test_ready_503_when_core_tables_missing(monkeypatch, tmp_path):
    # Valid SQLite file that opens fine, but lacks the core schema.
    db = tmp_path / "unrelated.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("create table unrelated (id integer)")
    conn.commit()
    conn.close()
    _point_at_db(monkeypatch, db)

    client = TestClient(main.app)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] is False
