# "Мой налог" receipts for the self-employed

Русская версия: [ru/npd-receipts.md](ru/npd-receipts.md).

This page is for operators in Russia who are registered as self-employed (NPD taxpayers). Such a person must register
every income in the "Мой налог" service and give the buyer a receipt. The bot can do this automatically: after a payment
is credited it registers the income in the lknpd.nalog.ru web cabinet, stores the receipt link and (optionally) sends it
to the buyer.

> **Important.** The Federal Tax Service has no official public API for "Мой налог". The bot uses the same API as the
> lknpd.nalog.ru web cabinet (as popular open-source libraries do), and the service may change it without notice.
> **The bot owner, as the taxpayer, is responsible for the correctness and timeliness of receipts.** Compare the receipt
> list in the web admin with your "Мой налог" cabinet regularly, and register income by hand if something fails.

## What is registered

- Only payments that were really credited through providers listed in `NPD_PROVIDERS`. By default these are the ruble
  methods: Platega, YooKassa, YooMoney, FreeKassa, Robokassa, CloudPayments, WATA, Antilopay, lava.top and manual
  transfer. Telegram Stars and crypto are not included by default; decide yourself how you account for them.
- Paying from the internal balance creates no receipt: the income was already registered when the balance was topped up.
- The receipt amount is the amount actually credited. The buyer is a private person and the payment type is "cash /
  electronic funds" (as in the web cabinet).
- Service name: for a plan purchase `NPD_SERVICE_NAME` (`{plan}` is replaced with the plan name), for other payments
  (top-up, donation) `NPD_TOPUP_SERVICE_NAME`.
- If a payment is refunded (a "refund" status from the provider or a refund from the admin), the receipt is cancelled
  with the reason "Refund". A receipt that has not been registered yet is simply cancelled.

## Setup

1. Web admin, **Settings**, group "Чеки «Мой налог»" (receipts):
   - **Taxpayer ID (INN)** and the **lknpd.nalog.ru password** (you can leave the password empty if you log in by SMS);
   - **Providers that get receipts**: comma-separated keys;
   - **Service names** and **Send the receipt to the buyer**.
2. Web admin, the receipts page, **Log in by SMS** (the code goes to the phone linked to "Мой налог") or **Log in with
   INN and password**. Tokens are stored encrypted in the database and refreshed automatically.
3. Turn on **Register income in "Мой налог"**.

The same options can be set with environment variables: `NPD_ENABLED`, `NPD_INN`, `NPD_PASSWORD`, `NPD_REFRESH_TOKEN`,
`NPD_SERVICE_NAME`, `NPD_TOPUP_SERVICE_NAME`, `NPD_PROVIDERS`, `NPD_SEND_TO_USER`, `NPD_MAX_ATTEMPTS`.

## How it works

- A receipt is queued in the same transaction that credits the payment, so no payment is lost.
- A background job registers the queue every 2 minutes. On an error the attempt is retried with a growing pause (1, 2, 4
  ... minutes, at most every 6 hours). After `NPD_MAX_ATTEMPTS` failures the receipt gets the `failed` status and admins
  are notified; you can retry it with a button in the web admin.
- Double registration is prevented: if the service's answer was lost (a timeout), before retrying the bot looks for an
  income with the same amount, name and time in the cabinet's income list and uses the receipt it finds.
- If login stops working (password changed, token revoked), registration is paused and admins are notified at most once
  every 6 hours. Log in again and the queue continues by itself.

## Receipt statuses

| Status | Meaning |
|---|---|
| `pending` | waiting for registration |
| `registered` | income registered, the receipt link is stored |
| `failed` | attempts exhausted; retry or register by hand |
| `cancel_pending` | the payment was refunded, the receipt will be cancelled |
| `cancelled` | the receipt was cancelled (or dropped before registration) |
