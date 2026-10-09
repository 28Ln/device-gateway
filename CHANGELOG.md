# Changelog

All notable changes to the BTF Device Gateway are recorded here. Entries are
grouped by priority level (P0 = critical, P3 = hygiene) so the outstanding
risk surface stays visible.

## Unreleased

### P3 — Repository hygiene & dead-code cleanup (round 2)

- Extracted the byte-for-byte duplicated `fmt_ts()` helper out of `app/main.py`
  and `app/cli.py` into the new dependency-light `app/util.py`; both modules now
  import the single shared implementation. The CLI no longer carries a second
  copy that could drift from the app's formatting.
- Removed the unused `SYSTEM_QUOTA_DAYS` constant from `app/service.py` (zero
  references across the tree).
- `DEFAULT_IMAGE_MODEL_TEXT` in `app/main.py` is now derived from
  `DEFAULT_IMAGE_MODELS` instead of repeating the `"[b2]gpt-image-2"` literal.
- `default_chat_model()` in `app/main.py` no longer hardcodes
  `"deepseek-v4.1-flash"`; it resolves against the `DEEPSEEK_MODELS` constant so
  the alias default has a single source of truth. `app/cli.py`'s
  `DEFAULT_CHAT_MODEL` is likewise derived from `DEFAULT_CHAT_MODELS`.
- Renamed `new_api_page()` to the semantically correct `paginate()` in
  `app/main.py` and updated its call site.
- Dropped the duplicated `import hashlib` in `app/payments.py`; the canonical
  `from hashlib import sha256` is the only import kept.
- Declared `requirements.txt` the single source of truth for runtime
  dependencies: `pyproject.toml` now keeps metadata/test extras only, with a
  comment warning against re-adding the drifting duplicate list.
- Added `tests/test_fix_hygiene.py` covering the shared helper, absence of the
  removed constants/duplicate imports, the `paginate` rename, and dependency
  single-sourcing.

### Earlier rounds (already merged)

- **P0/P1 — Payment hardening:** payment state machine, idempotent approve,
  provider validation, Decimal amounts (merge w6).
- **P1 — Secrets:** device-key masking, constant-time comparison, optional
  explicit key (merge w1).
- **P1 — Network/write safety:** SSRF hardening, atomic writes, explicit
  logging (merge w2).
- **P1 — Proxy/billing:** streaming proxy, SSE usage accounting, shared HTTP
  client, fail-safe billing (merge w4).
