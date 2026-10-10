# Changelog

All notable changes to the BTF Device Gateway are recorded here. Entries are
grouped by priority level (P0 = critical, P3 = hygiene) so the outstanding
risk surface stays visible.

## Unreleased

### P0 — /v1 upstream failure is now a structured 502 (round 8)

- `proxy_v1` used to let an `httpx` transport error (e.g. New API unreachable)
  escape as a bare `500 Internal Server Error`. The send-error branch now maps
  `httpx.HTTPError` to a structured `502 {"detail": "upstream request failed"}`
  (matching `proxy_new_api`), while cancellation/interrupts still re-raise
  untouched and the per-device concurrency slot is released exactly once.

### P1 — CLI/service input bounds (round 8)

- Service-layer `validate_days` (1–36500) and `validate_limit` (0–100000) reject
  out-of-range values before any DB access, so `create/renew/enable --days 10**16`
  or `list-usage --limit 10**20` can no longer raise an uncaught `OverflowError`
  or persist an absurd `expires_at`. The CLI surfaces these as friendly
  `SystemExit` messages via its existing `except (DeviceError, ValueError)`.
- `create_device` now catches `sqlite3.IntegrityError` from the `key_hash` unique
  constraint and raises the domain `Conflict` ("device key already exists"),
  so `create-device --key <dup>` no longer leaks a traceback.
- `MAX_EXPIRES_AT` is now defined once in `app/service.py` and reused by
  `app/main.py`; `create/renew/enable` clamp `expires_at` to it so a device
  created at the CLI day cap can still be edited through `PUT /api/device/`.

### P2 — Readiness probe schema validation (round 8)

- `/ready` previously reported `checks.database = true` for an empty or corrupt
  database file (SQLite opens it and `SELECT 1` succeeds). `check_database_ready`
  now verifies the core tables (`devices`, `schema_version`) and runs
  `pragma quick_check`, failing closed to `503` without ever raising. `/health`
  remains a pure liveness probe.

### P3 — Documentation completeness (round 8)

- `.env.example` now lists every variable `app/config.py` reads
  (`BTF_DATA_DIR`, `BTF_ORDER_EXPIRES_SECONDS` and the three
  `BTF_*_COST_PER_*` billing knobs); `docs/OPERATIONS.md` documents the same
  runtime/storage/cost group.

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
- **P1 — Request hardening:** CSRF/Origin + `Sec-Fetch-Site` middleware,
  request-id correlation, structured logging, token-bucket payment-callback
  limits (rounds 3–5).
- **P1 — Input validation:** every dashboard write path binds through
  `int_or_400` / `json_body_or_400` (rejecting malformed, non-object,
  non-finite and out-of-int64 values) instead of raising `500` (rounds 5–7).
- **P1 — Referential integrity:** `pragma foreign_keys=on` on every connection
  plus versioned migrations guarded by `schema_version`; rename/binding guarded
  against cross-account changes (merges w7, w24).
- **P1 — Image storage:** HMAC signed `?exp=&sig=` URLs for uploaded images,
  with unsigned links refused (merge w12).
- **P2 — Usage accounting:** deduplicated multi-device usage counts so
  `/api/log/self` and `/api/device/data/self` agree (rounds 6–7).
- **P2 — Upstream numeric safety:** `_safe_int` clamping and `record_usage`
  bounds prevent `OverflowError` from hostile upstream `usage` payloads and
  session/response ids (merge w30).
- **P2 — Device lifecycle:** `enable_device` rejects non-positive renewal
  windows and never produces an `active` + expired row (merge w31).
- **P2 — Packaging:** non-root container user, `/health` healthcheck,
  read-only compose, `.dockerignore`, CI workflow (rounds 3–7).
