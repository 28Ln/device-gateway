# Operations

## Directory layout

```text
/opt/new-api/
  compose.yml                 New API, PostgreSQL, Redis
  .env                        New API secrets
  .admin-credentials          generated administrator login

/opt/btf-device-gateway/
  app/                        gateway source
  data/                       gateway SQLite database
  new-api-dist/               built dashboard assets served by gateway
  docker-compose.yml          gateway deployment
  .env                        gateway runtime secrets
  scripts/                    deployment, seed, and smoke scripts
  docs/                       architecture and operating guides
```

There is currently no Git remote configured for this checkout. A local commit
is possible, but `git push` cannot be performed until a remote is added.

## Start or update

```bash
cd /opt/btf-device-gateway
cp .env.example .env       # only on a new installation
docker compose build
docker compose up -d
docker compose exec -T btf-device-gateway python -m app.cli init-db
```

The existing `scripts/deploy-on-server.sh` can create the first `.env`, read a
relay token from New API, build the image, start the container, and initialize
the database.

Do not replace the mounted `new-api-dist` directory with a different build
unless the dashboard routes and device menu have been verified.

The `new-api-dist/` and `new-api-dist.bak.*/` trees are ignored by `.gitignore`
and may be mounted into the container. They are large; if a future cleanup wants
to shrink the repository or image, treat their removal as a separate, verified
change rather than deleting them casually.

A stray zero-byte `btf.db` used to sit in the repository root. It was never the
gateway database (whose name is `btf-device-gateway.sqlite3`) and has been
removed. `btf.db` is also listed in `.gitignore` to prevent it reappearing.

## Environment groups

### Core

```text
PUBLIC_BASE_URL
NEW_API_BASE_URL
NEW_API_TOKEN
Only the variables above are read on the request path. Customer and admin
requests are authenticated with the existing New API session; the gateway has
no browser-facing admin credential of its own.
```

`NEW_API_TOKEN` is the internal server-side relay token. It must not be given
to a customer or hardware client.

`PUBLIC_BASE_URL` defaults to the local placeholder `http://127.0.0.1:8080`.
Production MUST set it to the public HTTPS domain (see "Real payment
activation" below). The gateway has no separate admin username/password or
internal admin token; administrative access is the existing New API session.

### Local payment test

```text
DEMO_PAYMENT_ENABLED=true
MOCK_PAYMENT_SECRET=<random-secret>
```

Use Mock only on a test environment. It is intentionally not a real money
flow.

### WeChat Pay API v3

```text
PAYMENT_NOTIFY_URL=https://api.example.com
WECHAT_APP_ID
WECHAT_MCH_ID
WECHAT_SERIAL_NO
WECHAT_PLATFORM_SERIAL_NO
WECHAT_PRIVATE_KEY
WECHAT_API_V3_KEY
WECHAT_PLATFORM_PUBLIC_KEY
WECHAT_API_BASE_URL=https://api.mch.weixin.qq.com
```

`WECHAT_SERIAL_NO` signs requests with the merchant certificate.
`WECHAT_PLATFORM_SERIAL_NO` validates the platform signature on callbacks.
Keep PEM values in the `.env` file as quoted strings with `\n` escapes.

### Alipay RSA2

```text
PAYMENT_NOTIFY_URL=https://api.example.com
ALIPAY_APP_ID
ALIPAY_PRIVATE_KEY
ALIPAY_PUBLIC_KEY
ALIPAY_GATEWAY=https://openapi.alipay.com/gateway.do
```

The callback URLs are:

```text
https://api.example.com/api/device/payments/wechat/notify
https://api.example.com/api/device/payments/alipay/notify
```

The public callback host must be reachable from the provider and should use
HTTPS with a domain. The current raw IP HTTP endpoint is suitable for API
development but should not be used for production payment callbacks.

## Real payment activation

1. Obtain the WeChat merchant credentials and platform certificate/public key.
2. Obtain the Alipay application credentials and RSA2 public key.
3. Set `PUBLIC_BASE_URL` and `PAYMENT_NOTIFY_URL` to the HTTPS domain.
4. Put the credentials in the gateway `.env`.
5. Rebuild and restart the gateway.
6. Verify `GET /api/device/payment-providers` returns `wechat` and/or
   `alipay`.
7. Create a low-value order from `/device/pay`.
8. Complete payment and verify the callback changes the order to `approved`.
9. Verify the device expiration changed once and `/v1/models` still works.

The gateway does not need Epay, Stripe, Creem, Waffo, or Waffo Pancake for this
device subscription flow.

## Data protection

Back up `/opt/btf-device-gateway/data/btf-device-gateway.sqlite3` and the
gateway `.env` separately. The database contains customer/device/order history;
the `.env` contains credentials and must remain mode `0600`.

Do not print `NEW_API_TOKEN`, private keys, API v3 keys, or admin passwords in
support logs or customer documentation.
