# Acceptance Checklist

## Infrastructure

- [ ] `new-api` container is healthy.
- [ ] `btf-device-gateway` container is running.
- [ ] `GET /health` returns `200`.
- [ ] `GET /api/status` returns New API success.
- [ ] The dashboard loads from the public origin.
- [ ] The device menu is present in the existing dashboard.

## Account and binding

- [ ] Three real New API customer accounts exist.
- [ ] Each customer has three devices.
- [ ] Each device is bound by New API numeric user ID.
- [ ] A customer sees only their own devices.
- [ ] An administrator sees all devices.
- [ ] A customer cannot edit, disable, rotate, or approve another device.

## Device authorization

- [ ] A valid MAC/current key can call `/v1/models`.
- [ ] A valid MAC/current key can call `deepseek-v4.1-flash`.
- [ ] Usage counters increase after a successful request.
- [ ] A disabled device receives a rejected `/v1` request.
- [ ] An expired device receives a rejected `/v1` request.
- [ ] An expired device can still call `/api/device/status`.
- [ ] An expired device can create a renewal order.
- [ ] A rotated old key is rejected.
- [ ] The new key works after rotation.

## Orders and renewal

- [ ] Plans are returned by `/api/device/plans`.
- [ ] An order is linked to exactly one device and plan.
- [ ] Manual approval extends from `max(old_expiry, now)`.
- [ ] An already approved order cannot extend twice.
- [ ] A customer can list only their own orders.
- [ ] A customer cannot approve an order.

## Mock payment

- [ ] Mock is enabled only in a test environment.
- [ ] Starting a Mock payment creates a payment transaction.
- [ ] Mock completion verifies its signature.
- [ ] Wrong amount is rejected.
- [ ] Replaying the same callback does not extend twice.

## Real payment readiness

- [ ] HTTPS callback domain is reachable externally.
- [ ] WeChat merchant and platform serial numbers are configured separately.
- [ ] Alipay RSA2 keys are configured.
- [ ] `/api/device/payment-providers` lists only the providers with complete
      credentials.
- [ ] A low-value real payment reaches the callback.
- [ ] The callback verifies amount, currency, signature, and order ID.
- [ ] The payment transaction stores the provider transaction ID.
- [ ] The device expiration extends exactly once.
