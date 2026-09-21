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
- `/device/pay` creates renewal orders.
- `/api/device/status` returns JSON status for firmware or apps.

Real payment provider callbacks can be added by marking orders paid and calling the same approval path.

