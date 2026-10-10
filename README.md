# BTF Device Gateway

BTF Device Gateway makes a device the billable identity in front of New API.

This repository contains the device gateway, not a second user system. New API
continues to own browser login, users, roles, sessions, tokens, models, and
upstream channels. The gateway owns device identity, device-to-user binding,
plans, renewal orders, payment callbacks, and device usage counters.

Public clients use a device key:

```text
Authorization: Bearer BTF:AI:01:01:00:01   # initial key == device_id
Base URL: http://115.191.10.45/v1
Default model: deepseek-v4.1-flash
Optional model: gpt-5.5 (web search enabled)
```

The gateway validates device status and expiry, then forwards requests to New
API with the internal relay token. `/v1/models` exposes the models enabled for
the device plan. `btf-chat` remains a compatibility alias for the default
DeepSeek model; selecting `gpt-5.5` automatically adds the upstream
`web_search` tool.

## Commands

```bash
python -m app.cli init-db
python -m app.cli create-device BTF-DEVICE-0001 --days 30
python -m app.cli renew-device BTF-DEVICE-0001 --days 30
python -m app.cli disable-device BTF-DEVICE-0001
python -m app.cli list-devices
python -m app.cli approve-order ORDER_ID
```

`create-device`, `batch-create`, `import-devices`, and `rotate-key` print the
device key masked by default so full credentials do not leak into shell history
or logs. Add `--show-secret` only when the key genuinely needs to be provisioned
to hardware. The `model=` line they print is derived from the configured plans,
not hardcoded.

## Device API

- `/api/device/status` returns JSON status for firmware or apps.
- `/v1/*` validates a device API key, forwards to New API, and records usage.

Device administration is exposed inside the existing New API dashboard UI. The
gateway API is mounted behind the same public origin, so customers use their
normal New API login and do not need a second gateway token for dashboard pages.

## Users and Device Binding

- `device_id` is the stable device identifier, currently the device MAC. It is
  the device's lookup identity and is never accepted as a credential on its own.
- A newly created device starts with an API key equal to its `device_id`. This is
  a convenience default for first provisioning, not a permanent equivalence: the
  gateway stores only `sha256(key)`, so the API key is a separate credential from
  the identifier.
- Devices can be bound to a user during single creation, prefix batch creation, or newline-based device identifier import.
- An administrator can rotate a device key for security. After rotation the key
  is a random value that no longer equals the MAC / `device_id`; the old key is
  immediately invalid and the hardware must be provisioned with the new key.

## End-to-End Usage

1. An administrator creates or imports a New API customer.
2. The administrator creates devices using each hardware MAC and binds them to
   that customer.
3. The customer logs in through the normal New API dashboard and sees only
   their own devices, orders, and usage.
4. Hardware calls `http(s)://<public-host>/v1/*` with
   `Authorization: Bearer <device-key>`.
5. The gateway checks device status and expiration, then forwards the request
   to New API with the internal relay token.
6. The customer opens `/device/pay`, checks a device even when it is expired,
   chooses a plan, creates a renewal order, and pays.
7. A confirmed payment marks the order paid and approved, extends the device
   from `max(current_expiration, now)`, and re-enables it.

The customer never receives the internal New API relay token. The device key
and the New API browser session are separate credentials with different scopes.

## Payment

Payment is provider-neutral and uses the same order approval path for manual
confirmation and asynchronous provider callbacks:

1. The device creates a pending renewal order from `/device/pay`.
2. The device starts payment with `POST /api/device/orders/{order_id}/pay`.
3. The provider callback is received at
   `/api/device/payments/{provider}/notify`.
4. The gateway verifies the provider signature, amount, currency, and provider
   order id.
5. The callback marks the payment paid and calls the same idempotent
   `approve_order` path, extending the device expiration once.

Supported providers:

- `wechat`: WeChat Pay API v3 native QR payments.
- `alipay`: Alipay RSA2 precreate QR payments.
- `mock`: local integration testing only; requires
  `DEMO_PAYMENT_ENABLED=true` and `MOCK_PAYMENT_SECRET`.

The provider credentials and callback URL are intentionally environment
configuration, not source code. Copy `.env.example`, configure the merchant
credentials, and use an HTTPS public callback URL before enabling a real
provider. No real payment request is made while its required credentials are
missing.

For WeChat Pay, `WECHAT_SERIAL_NO` is the merchant API certificate serial and
`WECHAT_PLATFORM_SERIAL_NO` is the platform certificate serial sent in
`Wechatpay-Serial` callback headers. They are different values and both are
required.

Payment callback protections implemented:

- provider signature verification;
- amount and currency matching against the immutable order;
- provider transaction id matching the created payment transaction;
- idempotent handling of repeated callbacks;
- no double extension when a callback is retried.

For local tests, create an order, start it with provider `mock`, then call
`POST /api/device/payments/mock/complete/{provider_order_id}`. This endpoint is
disabled unless `DEMO_PAYMENT_ENABLED=true`.

## Operations and Documentation

- [Architecture and lifecycle](docs/ARCHITECTURE.md)
- [Customer usage guide](docs/CUSTOMER_GUIDE.md)
- [Deployment and payment setup](docs/OPERATIONS.md)
- [Acceptance checklist](docs/ACCEPTANCE.md)
- [Changelog by priority level](CHANGELOG.md)

Run the read-only gateway smoke checks with:

```bash
BTF_BASE_URL=http://115.191.10.45 \
BTF_DEVICE_KEY='BTF:AI:01:01:00:01' \
./scripts/smoke-test.sh
```

Add `RUN_CHAT=1` to also send a short real request to
`deepseek-v4.1-flash`.

## Hardening status

This tree has been hardened, optimized and verified by an isolated multi-agent
pipeline (baseline `36f88bb`, 41 tests → current HEAD, 460 tests in-repo plus
≈300 independent adversarial verification tests). See `CHANGELOG.md` for the
full P0–P3 record. The verification pass closed every historical blocker family
(SSRF, CSRF, device-key masking, referential integrity, migration safety,
payment idempotency, unhandled-exception → 500, usage double-counting,
`active`+expired state) and the last round added: structured `502` when the New
API upstream is unreachable, CLI/service day+limit bounds, `/ready` schema
validation, and single-sourced `MAX_EXPIRES_AT`. Reproduce with:

```bash
BTF_DATA_DIR=$(mktemp -d) python -m pytest -q
```
