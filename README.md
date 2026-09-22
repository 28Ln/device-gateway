# BTF Device Gateway

BTF Device Gateway makes a device the billable identity in front of New API.

Public clients use a device key:

```text
Authorization: Bearer btf_dev_xxx
Base URL: http://115.191.10.45/v1
Model: deepseek-v4.1-flash
```

The gateway validates device status and expiry, then forwards requests to New API with the internal relay token.

## Commands

```bash
python -m app.cli init-db
python -m app.cli create-device BTF-DEVICE-0001 --days 30
python -m app.cli renew-device BTF-DEVICE-0001 --days 30
python -m app.cli disable-device BTF-DEVICE-0001
python -m app.cli list-devices
python -m app.cli approve-order ORDER_ID
```

## Customer Pages

- `/device` checks device authorization status.
- `/device/pay` starts renewal flow.
- `/api/device/status` returns JSON status for firmware or apps.

## Admin Pages

- `/admin` dashboard
- `/admin/devices` create, batch create, renew, disable, enable, rotate keys
- `/admin/orders` approve renewal orders
- `/admin/usage` inspect device usage

Admin login uses `BTF_ADMIN_USERNAME` and `BTF_ADMIN_PASSWORD`. `INTERNAL_ADMIN_TOKEN` is still accepted through `x-admin-token` for internal scripts.

## Payment

The current provider is manual confirmation. Devices create renewal orders from `/device`, and admins approve them in `/admin/orders`.

Set `DEMO_PAYMENT_ENABLED=true` only in test environments to show a self-service test payment button that approves the order immediately.

Real payment provider callbacks should validate the provider signature, then call the same order approval path.
