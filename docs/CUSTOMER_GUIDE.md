# Customer Usage Guide

## What the customer receives

For each device, provide:

```text
API Base URL: https://<public-host>/v1
API Key: <current device key>
Default model: deepseek-v4.1-flash
Optional model: gpt-5.5 (web search enabled)
Image model: btf-image (monthly plan only)
Renewal URL: https://<public-host>/device/pay?mac=<device-key>
```

A new device starts with the API key equal to its device `mac` / `device_id`.
The gateway only stores a hash of the key, so the key is a separate credential
from the identifier. If an administrator rotates the key, the customer must
replace the key in the hardware configuration; the rotated key no longer equals
the MAC and the old key stops working immediately.

If an administrator provisions a device with a **binding code**, the customer
claims the device by entering both the `mac` and the binding code in the
dashboard device page. The binding code is a short secret that only confirms
ownership; it is not the API key, and the gateway stores only its hash and a
display mask.

## OpenAI-compatible request

```bash
curl https://<public-host>/v1/chat/completions \
  -H 'Authorization: Bearer <device-key>' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "deepseek-v4.1-flash",
    "messages": [
      {"role": "user", "content": "hello"}
    ],
    "max_tokens": 128
  }'
```

List models:

```bash
curl https://<public-host>/v1/models \
  -H 'Authorization: Bearer <device-key>'
```

When `gpt-5.5` is selected, the gateway automatically adds
`{"type":"web_search"}` to the request tools. DeepSeek remains the default
model and does not receive the web-search tool.

## Image generation

On the `30天月卡` (monthly) plan the device can also generate images through the
OpenAI-compatible endpoint:

```bash
curl https://<public-host>/v1/images/generations \
  -H 'Authorization: Bearer <device-key>' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "btf-image",
    "prompt": "a white cat"
  }'
```

`btf-image` is a compatibility alias for the gateway's GPT image model. The
generated image is stored by the gateway and returned through a public
`/device-images/...` URL. Image generation is not available on the 1-hour,
1-day, 3-day, or 7-day plans.

Check device status:

```bash
curl https://<public-host>/api/device/status \
  -H 'Authorization: Bearer <device-key>'
```

## Dashboard access

Customers use their normal New API username and password at the existing
dashboard URL. After login, the device menu is shown by the current dashboard
UI. No device-specific token is entered into other dashboard pages.

The customer can:

- see only their own devices;
- see status, expiration, request count, and token count;
- see their own renewal orders;
- see their own usage events;
- create a renewal order for an owned device.

The customer cannot:

- see another customer's device;
- create or reassign devices;
- disable or enable devices;
- rotate keys;
- approve orders;
- access the internal relay token.

## Renewal

1. Open `/device/pay`.
2. Enter the device MAC/current key, or use the `?mac=` link.
3. Check the status. Expired devices remain viewable.
4. Choose `1小时体验`, `1天通行`, `3天通行`, `7天周卡`, or `30天月卡`.
5. Create the order.
6. Choose the configured payment provider and complete payment.
7. Wait for the provider callback. The order becomes approved and the
   expiration is extended automatically.

Until official payment credentials are configured, the page may show no
payment provider. That is expected; it does not prevent manual administrator
confirmation or local Mock integration tests.

## Common failures

| Symptom | Meaning |
| --- | --- |
| `403` from `/v1` | Device is disabled or expired, or the key is invalid |
| Status page works but `/v1` is rejected | Device is expired/disabled; renew or enable it |
| Customer sees no device | Device is bound to a different New API user ID |
| Payment provider list is empty | No complete provider credentials are loaded |
| Old key stops working after rotation | Expected; provision the new key |
