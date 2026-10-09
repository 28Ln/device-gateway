"""Round-3 hygiene and data-migration regression tests.

Covers two areas that were fixed in the third pass:

1. The plan model-default upgrade used to be a *re-runnable* "value equality"
   migration. Because a plan an administrator deliberately set to the single
   model ``deepseek-v4.1-flash`` is byte-for-byte identical to the legacy
   built-in default, every startup rewrote it back to the dual-model default.
   It is now a version-guarded, run-once migration: a value the admin changes
   after the migration has run is never touched again.

2. Repository hygiene: the stray zero-byte ``btf.db`` must stay gone (nothing
   writes a database into the working directory) and the handoff/operations
   docs must match the current implementation.

Like the existing ``test_fix_hygiene`` module these are static/behavioural
assertions that avoid importing the heavy FastAPI app.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from app.db import (
    DEFAULT_CHAT_MODELS,
    DEFAULT_IMAGE_MODELS,
    MIGRATIONS,
    connect,
    init_db,
)

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
DOCS_DIR = ROOT / "docs"
MIGRATION_VERSION_UNDER_TEST = 2


def _plan_value(db: Path, column: str, plan_id: str) -> str:
    with connect(db) as conn:
        row = conn.execute(
            f"select {column} from plans where id = ?", (plan_id,)
        ).fetchone()
    return row[0]


def _applied_versions(db: Path) -> list[int]:
    with connect(db) as conn:
        return [
            row["version"]
            for row in conn.execute(
                "select version from schema_version order by version"
            ).fetchall()
        ]


# --------------------------------------------------------------------------
# Data migration: administrator values must survive repeated startups.
# --------------------------------------------------------------------------


def test_admin_single_model_choice_survives_reinit(tmp_path):
    """After the migration, forcing one chat model must stick across restarts.

    This is the exact regression the value-equality forward migration caused:
    ``init_db`` on an already-migrated database must be a no-op for plan model
    columns, even when the stored value equals a legacy default.
    """
    db = tmp_path / "test.sqlite3"
    init_db(db)

    # Administrator deliberately restricts the day plan to DeepSeek only.
    with connect(db) as conn:
        conn.execute(
            "update plans set chat_models = ? where id = ?",
            ("deepseek-v4.1-flash", "day_1"),
        )

    # "Restart" several times (each init_db call mirrors a process start).
    init_db(db)
    init_db(db)
    init_db(db)

    assert _plan_value(db, "chat_models", "day_1") == "deepseek-v4.1-flash"


def test_migration_upgrades_legacy_default_once(tmp_path):
    """A pre-migration database still gets the intended one-time upgrade."""
    db = tmp_path / "test.sqlite3"
    init_db(db)

    # Simulate a legacy database: old built-in defaults, migration not recorded.
    with connect(db) as conn:
        conn.execute(
            "update plans set chat_models = ? where id = ?",
            ("deepseek-v4.1-flash", "day_1"),
        )
        conn.execute(
            "update plans set image_models = ? where id = ?",
            ("[b]gpt-image-2", "month_1"),
        )
        conn.execute(
            "delete from schema_version where version = ?",
            (MIGRATION_VERSION_UNDER_TEST,),
        )

    init_db(db)

    assert _plan_value(db, "chat_models", "day_1") == DEFAULT_CHAT_MODELS
    assert _plan_value(db, "image_models", "month_1") == DEFAULT_IMAGE_MODELS
    # Recorded exactly once, not duplicated.
    assert _applied_versions(db).count(MIGRATION_VERSION_UNDER_TEST) == 1


def test_image_backfill_only_happens_during_its_migration(tmp_path):
    """``image_models=''`` + ``allow_image=1`` is only backfilled by migration 2.

    Clearing the image models on an already-migrated database (e.g. an admin
    disabling image generation by emptying the list) must not be silently
    repopulated on the next startup.
    """
    db = tmp_path / "test.sqlite3"
    init_db(db)

    with connect(db) as conn:
        conn.execute(
            "update plans set allow_image = 1, image_models = '' where id = ?",
            ("month_1",),
        )

    init_db(db)
    init_db(db)

    assert _plan_value(db, "image_models", "month_1") == ""


def test_no_runnable_forward_migration_reintroduced():
    """Guard against reintroducing the value-equality forward migration."""
    src = (APP_DIR / "db.py").read_text(encoding="utf-8")
    assert "_apply_forward_migration" not in src

    tree = ast.parse(src)
    funcs = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "_migration_legacy_plan_defaults" in funcs

    # Every migration in the registry is unique and sorted ascending.
    versions = [version for version, _ in MIGRATIONS]
    assert versions == sorted(set(versions))


def test_fresh_install_has_intended_plan_defaults(tmp_path):
    """A brand-new database is already correct without running any upgrade."""
    db = tmp_path / "test.sqlite3"
    init_db(db)

    assert _plan_value(db, "chat_models", "day_1") == DEFAULT_CHAT_MODELS
    assert _plan_value(db, "image_models", "month_1") == DEFAULT_IMAGE_MODELS
    # Non-image plans keep an empty image model list.
    assert _plan_value(db, "image_models", "day_1") == ""


# --------------------------------------------------------------------------
# Repository hygiene: zombie database + documentation drift.
# --------------------------------------------------------------------------


def test_root_btf_db_is_absent():
    """The stray zero-byte ``btf.db`` must not exist in the repository root."""
    assert not (ROOT / "btf.db").exists()


def test_source_never_writes_database_into_cwd():
    """No source file may reference a bare ``btf.db`` database path."""
    for path in APP_DIR.glob("*.py"):
        src = path.read_text(encoding="utf-8")
        assert "btf.db" not in src, f"{path.name} references a stray btf.db"

    config_src = (APP_DIR / "config.py").read_text(encoding="utf-8")
    assert 'data_dir / "btf-device-gateway.sqlite3"' in config_src


def test_init_db_under_temp_data_dir_creates_no_cwd_database(tmp_path):
    """Running DB init with a redirected data dir writes nothing to the CWD."""
    workdir = tmp_path / "cwd"
    data_dir = tmp_path / "data"
    workdir.mkdir()
    data_dir.mkdir()

    script = (
        "from app.config import load_settings\n"
        "from app.db import init_db\n"
        "settings = load_settings()\n"
        "init_db(settings.database_path)\n"
        "print(settings.database_path)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=workdir,
        env={
            "BTF_DATA_DIR": str(data_dir),
            "PYTHONPATH": str(ROOT),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "btf-device-gateway.sqlite3" in result.stdout
    assert list(workdir.glob("*.db")) == []
    assert not (workdir / "btf.db").exists()


def test_handoff_doc_has_no_stale_test_count():
    """The handoff doc must not hardcode a stale passing-test count."""
    doc = (DOCS_DIR / "CLAUDE_HANDOFF.md").read_text(encoding="utf-8")
    assert "49 passed" not in doc
    assert "224" in doc  # snapshotted, explicitly non-authoritative


def test_handoff_doc_documents_ready_endpoint():
    """``/ready`` exists in the app and must be listed in the API table."""
    doc = (DOCS_DIR / "CLAUDE_HANDOFF.md").read_text(encoding="utf-8")
    assert "/ready" in doc
    main_src = (APP_DIR / "main.py").read_text(encoding="utf-8")
    assert '@app.get("/ready")' in main_src


def test_handoff_doc_describes_signed_image_links():
    """The doc must describe the signed-URL gate, not "know the path" access."""
    doc = (DOCS_DIR / "CLAUDE_HANDOFF.md").read_text(encoding="utf-8")
    assert "知道完整路径后访问" not in doc
    assert "签名" in doc


def test_operations_doc_matches_absent_btf_db():
    """OPERATIONS.md claims removal only if the file is genuinely gone."""
    doc = (DOCS_DIR / "OPERATIONS.md").read_text(encoding="utf-8")
    assert "btf.db" in doc
    assert "deleted" in doc or "removed" in doc
    assert not (ROOT / "btf.db").exists()
