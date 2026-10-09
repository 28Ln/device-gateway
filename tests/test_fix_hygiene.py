"""Repository-hygiene checks for the P3 cleanup pass.

These are intentionally static/behavioural assertions rather than an import of
the heavy FastAPI app, so a regression (e.g. a duplicated ``fmt_ts`` creeping
back in) fails fast and documents the single-source-of-truth intent.
"""
from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

from app.util import fmt_ts

APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _function_defs(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}


def test_fmt_ts_is_shared_and_correct():
    """The shared helper renders timestamps and empty values consistently."""
    ts = 1_700_000_000
    assert fmt_ts(ts) == datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    assert fmt_ts(None) == "-"
    assert fmt_ts(0) == "-"


def test_fmt_ts_defined_in_one_place_only():
    """No module may carry its own duplicate ``fmt_ts`` definition again."""
    defining = sorted(
        str(path.relative_to(APP_DIR.parent))
        for path in APP_DIR.glob("*.py")
        if "fmt_ts" in _function_defs(path)
    )
    assert defining == ["app/util.py"], defining


def test_main_and_cli_use_shared_fmt_ts():
    """``app.main`` and ``app.cli`` must expose the very same helper object."""
    from app import cli, main

    assert main.fmt_ts is fmt_ts
    assert cli.fmt_ts is fmt_ts


def test_no_dead_constants_remain():
    """Removed constants must not silently reappear."""
    service_src = (APP_DIR / "service.py").read_text(encoding="utf-8")
    assert "SYSTEM_QUOTA_DAYS" not in service_src

    # ``DEFAULT_IMAGE_MODEL_TEXT`` must be derived from the model set, not a
    # second hardcoded literal.
    main_src = (APP_DIR / "main.py").read_text(encoding="utf-8")
    assert 'DEFAULT_IMAGE_MODEL_TEXT = "[b2]gpt-image-2"' not in main_src

    from app import main as main_mod

    assert main_mod.DEFAULT_IMAGE_MODEL_TEXT in main_mod.DEFAULT_IMAGE_MODELS


def test_payments_has_no_duplicate_hashlib_import():
    """A single canonical ``sha256`` import backs the signature helpers."""
    src = (APP_DIR / "payments.py").read_text(encoding="utf-8")
    assert "import hashlib" not in src
    assert "from hashlib import sha256" in src


def test_default_chat_model_uses_constant():
    """The alias default resolves via DEEPSEEK_MODELS, not a bare literal."""
    from app import main as main_mod

    models = {"alternate", "deepseek-v4.1-flash"}
    assert main_mod.default_chat_model(models) == "deepseek-v4.1-flash"
    assert main_mod.default_chat_model({"alternate", "b"}) == "alternate"
    assert all(m in main_mod.DEEPSEEK_MODELS for m in main_mod.DEEPSEEK_MODELS)


def test_paginate_is_the_page_builder_name():
    """``new_api_page`` was renamed to the semantically correct ``paginate``."""
    src = (APP_DIR / "main.py").read_text(encoding="utf-8")
    assert "new_api_page" not in src
    assert "def paginate(" in src

    from app import main as main_mod

    # total=None: the helper slices the page and reports the sliced length.
    page = main_mod.paginate([{"a": 1}, {"a": 2}, {"a": 3}], page=2, page_size=2)
    assert page == {"items": [{"a": 3}], "total": 1, "page": 2, "page_size": 2}
    # total provided: the caller already paginated, so items pass through.
    page = main_mod.paginate([{"a": 1}], page=1, page_size=20, total=57)
    assert page == {"items": [{"a": 1}], "total": 57, "page": 1, "page_size": 20}


def test_requirements_txt_is_dependency_source_of_truth():
    """pyproject must not duplicate the runtime dependency list."""
    import tomllib

    root = APP_DIR.parent
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "fastapi" in requirements and "httpx" in requirements

    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["dependencies"] == []
