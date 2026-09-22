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

## Device API

- `/api/device/status` returns JSON status for firmware or apps.
- `/v1/*` validates a device API key, forwards to New API, and records usage.

No standalone gateway admin pages are exposed. Device administration is handled by CLI until it is implemented inside the New API UI.

## Users and Device Binding

- `device_id` is the stable device identifier, such as SN, IMEI, MAC, or factory number.
- `api_key` is a generated secret used for `/v1` calls. It should not be the same as `device_id`.
- Devices can be bound to a user during single creation, prefix batch creation, or newline-based device identifier import.
- Device keys can be rotated without changing the device identifier or user binding.

## Payment

The current provider is manual confirmation. Devices create renewal orders from `/device`, and admins approve them in `/admin/orders`.

Set `DEMO_PAYMENT_ENABLED=true` only in test environments to show a self-service test payment button that approves the order immediately.

Real payment provider callbacks should validate the provider signature, then call the same order approval path.
