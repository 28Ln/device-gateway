"""Small helpers shared between the FastAPI app and the CLI.

Kept dependency-light on purpose: importing this module must not pull in the
heavy FastAPI application (``app.main``), so the CLI can reuse these helpers
without paying for the web stack.
"""
from __future__ import annotations

from datetime import datetime


def fmt_ts(value: int | None) -> str:
    """Format a unix timestamp (seconds) for operator-facing output.

    Falsy values (``None``/``0``) render as ``"-"`` so callers never have to
    special-case missing timestamps. This is the single shared implementation
    used by both ``app.main`` and ``app.cli``; the two used to carry
    byte-for-byte duplicate copies that could silently drift apart.
    """
    if not value:
        return "-"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
