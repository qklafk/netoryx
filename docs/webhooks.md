# Outgoing webhooks

The bot can notify external systems (CRMs, analytics, accounting, your own scripts) about business events:
registrations, purchases, renewals, payments, expiring subscriptions and more. Each event is sent as a signed
HTTP `POST` with a JSON body.

Endpoints are managed in the web admin: **Management → Webhooks** (owner and administrators).

## Adding an endpoint

1. Open **Webhooks → Add endpoint**.
2. Enter the receiver URL. Only `https://` URLs are accepted, and the host must resolve to a public address —
   addresses in private, loopback, link-local and other reserved networks are refused (also re-checked at every
   delivery). In development mode (`DEV_MODE=true`) plain `http://localhost` is allowed as well.
3. Choose the events: `*` for everything, exact types (`purchase, renewal`) or a prefix (`payment.*`).
4. Save and **copy the signing secret** (`whsec_...`). It is shown only once; if you lose it, use
   **Rotate secret** — the old secret stops working immediately.
5. Press **Test** to send a `webhook.test` event and see the receiver's answer.

## Request format

```
POST /your/path HTTP/1.1
Content-Type: application/json
X-Webhook-Id: evt_q3Jw0T9n0m6m3uYxP1c2ZxXy
X-Webhook-Event: purchase
X-Webhook-Timestamp: 1762700000
X-Webhook-Signature: sha256=5d2c...e9

{"id":"evt_q3Jw0T9n0m6m3uYxP1c2ZxXy","type":"purchase","created_at":"2026-11-09T12:00:00.123456+00:00",
 "user_id":123456789,"amount_kop":19900,"data":{"days":30,"plan_id":2,"kind":"purchase", "...": "..."}}
```

| Field        | Meaning                                                                                 |
|--------------|-----------------------------------------------------------------------------------------|
| `id`         | Event id. The same id is sent on every retry and on manual redelivery — use it to deduplicate. |
| `type`       | Event type (see the list below).                                                        |
| `created_at` | When the event happened, ISO 8601 in UTC.                                               |
| `user_id`    | Telegram id of the user, or `null`.                                                      |
| `amount_kop` | Amount in kopecks (1 ₽ = 100), or `null`.                                                |
| `data`       | Event-specific details (plan id, provider, days, ...).                                   |

Payloads never contain secrets, access tokens, subscription links or VPN keys.

## Verifying the signature

`X-Webhook-Signature` is `sha256=` followed by the hex HMAC-SHA256 of `"{timestamp}.{raw body}"` with the
endpoint secret as the key. Always verify it against the **raw** request body, and reject old timestamps to
prevent replays.

```python
import hashlib
import hmac
import time

SECRET = "whsec_..."  # from the web admin


def verify(body: bytes, timestamp: str, signature: str, tolerance: int = 300) -> bool:
    if abs(time.time() - int(timestamp)) > tolerance:
        return False
    expected = "sha256=" + hmac.new(SECRET.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# Flask example
from flask import Flask, abort, request

app = Flask(__name__)


@app.post("/webhooks/vpn")
def receive():
    if not verify(request.get_data(), request.headers["X-Webhook-Timestamp"], request.headers["X-Webhook-Signature"]):
        abort(401)
    event = request.get_json()
    # ... process event["type"], deduplicating by event["id"] ...
    return "", 204
```

## Delivery and retries

- A delivery succeeds when the receiver answers with any `2xx` status within 10 seconds. Redirects are not
  followed; the response body is ignored.
- Events are written to an outbox together with the change that produced them, so nothing is lost on restarts
  and nothing is sent for changes that were rolled back. Deliveries normally go out within ~15 seconds.
- A failed delivery is retried after **1 min, 5 min, 30 min, 2 h, 6 h and 24 h** (7 attempts in total). After
  that it is marked `dead`.
- Deliveries may arrive more than once and out of order — deduplicate by `id` and compare `created_at`.
- After several `dead` deliveries in a row (setting **Outgoing webhooks → Disable an endpoint after N failed
  deliveries in a row**, default 3, `0` = never) the endpoint is switched off and administrators get a
  notification. Fix the receiver and turn the endpoint back on; you can redeliver individual events from
  **Deliveries**.
- Delivery history is kept for 14 days by default (setting **Keep delivery history, days**).

## Event types

| Type                        | When                                                   |
|-----------------------------|--------------------------------------------------------|
| `user.registered`           | A new user started the bot                             |
| `consent.accepted`          | The user accepted the terms                             |
| `trial.started`             | A trial subscription was issued                         |
| `purchase`                  | A plan was bought (new subscription or extension)       |
| `renewal`                   | A subscription was renewed automatically                |
| `subscription.granted`      | Access was granted without a purchase (admin, gift, ...) |
| `subscription.expired`      | A subscription expired                                  |
| `subscription.auto_renew`   | Auto-renewal was switched on or off                     |
| `subscription.link_rotated` | The user re-issued their subscription link              |
| `payment.succeeded`         | A payment was credited                                  |
| `payment.refunded`          | A payment was refunded                                  |
| `payment.held`              | The provider reported a payment as received but held    |
| `payment.repeat`            | A recurring (auto-renewal) charge was recorded          |
| `payment.amount_mismatch`   | The paid amount differs from the invoice                |
| `promo.redeemed`            | A promo code was used                                   |
| `gift.purchased`            | A gift code was bought                                  |
| `gift.redeemed`             | A gift code was activated                               |
| `referral.applied`          | A user joined through a referral link                   |
| `referral.reward`           | A referral reward was granted                           |
| `partner.transfer`          | Partner earnings were moved to the main balance         |
| `withdrawal.requested`      | A partner requested a payout                            |
| `withdrawal.paid`           | A payout was completed                                  |
| `invite.redeemed`           | An invitation was used                                  |
| `ticket.created`            | A support ticket was opened                             |
| `webhook.test`              | Sent by the **Test** button only                        |
