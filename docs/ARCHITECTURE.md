# BTF API Architecture

## Ownership boundaries

| Area | System of record |
| --- | --- |
| Browser login, passwords, sessions, roles | New API |
| Users and customer accounts | New API user table |
| Device identity and MAC binding | BTF gateway SQLite |
| Device plans and renewal orders | BTF gateway SQLite |
| Payment provider callbacks | BTF gateway |
| Model routing and upstream channels | New API |
| Device authorization before `/v1` | BTF gateway |
| Request counters and token usage | BTF gateway |

The gateway is a policy layer in front of New API. It does not create a second
browser login flow and it must not expose the internal New API relay token.

## Identity model

```text
New API user
  └── 0..N BTF devices
        ├── stable device_id (currently MAC)
        ├── current device API key
        ├── status: active or disabled
        ├── expires_at
        └── renewal orders and usage events
```

When a device is first created, its current key equals its normalized MAC. The
key is stored as a SHA-256 hash, while the masked/display value is stored for
the dashboard. A key rotation changes only the key hash and display value; the
device identity, owner, orders, and usage remain unchanged.

## Request flow

```text
Hardware
  Authorization: Bearer <device-key>
        |
        v
BTF /v1/*
  1. hash key and find device
  2. reject missing, disabled, or expired device
  3. replace Authorization with internal New API relay token
  4. preserve client IP forwarding headers
  5. forward request to New API
  6. record status and usage
        |
        v
New API model router
```

An expired or disabled device can still use
`GET /api/device/status` with its device key. It cannot use `/v1/*`.

## Customer flow

1. Customer logs in at the existing New API dashboard.
2. The gateway validates that browser session against New API.
3. Non-admin requests are filtered by `devices.user_id = session.user_id`.
4. Customer can view owned devices, owned orders, and owned usage.
5. Customer cannot create, reassign, disable, or approve devices/orders.
6. The public `/device/pay` page accepts a device key/MAC so an expired device
   can still create a renewal order.

## Renewal flow

```text
device key/MAC
  -> GET /api/device/status
  -> POST /api/device/orders
  -> POST /api/device/orders/{id}/pay
  -> provider callback
  -> verify signature, amount, currency, and order
  -> mark payment paid
  -> approve_order (idempotent)
  -> extend from max(old_expiry, now)
  -> set device active
```

Manual administrator confirmation uses the same `approve_order` function.
Repeated provider callbacks do not extend twice.

## Public endpoints

| Endpoint | Purpose | Credential |
| --- | --- | --- |
| `GET /health` | Gateway health | None |
| `GET /api/status` | New API status | None |
| `GET /api/device/plans` | Enabled device plans | None |
| `GET /api/device/payment-providers` | Configured real/mock providers | None |
| `GET /api/device/status` | Device status | Device key |
| `GET /device/pay` | Customer self-service renewal page | Device key required for lookup |
| `POST /api/device/orders` | Create renewal order | Device key or logged-in owned customer |
| `POST /api/device/orders/{id}/pay` | Start provider payment | Device key |
| `POST /api/device/payments/{provider}/notify` | Provider callback | Provider signature |
| `GET /v1/*` and `POST /v1/*` | OpenAI-compatible API | Device key |

## Admin/customer endpoint behavior

The dashboard uses the existing New API session cookie or access token. It does
not use `NEW_API_TOKEN` or a separate BTF admin login. The gateway keeps no
browser-facing admin credential of its own.