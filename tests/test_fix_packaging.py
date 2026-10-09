"""Text-level assertions for packaging / container hardening (P1-12, P2-24).

These tests inspect Dockerfile / docker-compose.yml / CI workflow as text. The
sandbox cannot build or run containers, so only static configuration is
verified here (no docker daemon required).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _compose() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def test_dockerfile_runs_as_non_root_user() -> None:
    text = _dockerfile()
    user_lines = [
        line for line in text.splitlines()
        if line.strip().upper().startswith("USER ")
    ]
    assert user_lines, "Dockerfile must switch to a non-root USER"
    last = user_lines[-1].strip()
    assert not re.search(r"\broot\b", last, re.IGNORECASE), last
    # A fixed numeric uid keeps host bind-mount ownership predictable.
    assert "10001" in last, f"expected fixed uid 10001 in {last!r}"


def test_dockerfile_creates_user_before_switching() -> None:
    text = _dockerfile()
    assert "useradd" in text, "Dockerfile must create the unprivileged user"
    assert "chown" in text and "/data" in text, "Dockerfile must chown /data"
    # The USER directive must appear after user creation.
    assert text.index("useradd") < text.index("USER ")


def test_dockerfile_preserves_data_dir_and_port() -> None:
    text = _dockerfile()
    assert "BTF_DATA_DIR=/data" in text
    assert "EXPOSE 8080" in text
    assert "--port" in text and "8080" in text


def test_dockerfile_has_healthcheck_on_health_endpoint() -> None:
    text = _dockerfile()
    assert "HEALTHCHECK" in text
    assert "/health" in text
    # no-cache-dir requirement retained.
    assert "--no-cache-dir" in text


def test_compose_has_healthcheck() -> None:
    text = _compose()
    assert "healthcheck:" in text
    assert "/health" in text


def test_compose_has_no_new_privileges_and_read_only() -> None:
    text = _compose()
    assert "no-new-privileges:true" in text
    assert "security_opt:" in text
    assert "read_only: true" in text


def test_compose_documents_data_volume_owner() -> None:
    text = _compose()
    # Bind mount for the container data directory must be present and the
    # ownership expectation documented for operators.
    assert "./data:/data" in text
    assert "chown" in text and "10001" in text


def test_ci_workflow_runs_pytest_with_dev_requirements() -> None:
    text = CI.read_text(encoding="utf-8")
    assert "requirements-dev.txt" in text
    assert "pytest" in text
