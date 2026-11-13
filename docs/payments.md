# Payments

Netoryx credits money to a wallet; subscriptions are bought from the wallet. Every gateway is turned on and configured
in the web admin (**Settings**, groups "Payments: ..."), or with the `*_ENABLED` and credential variables in `.env`
(see `.env.example`). The enabled gateways appear as buttons in the bot, the Mini App and the web cabinet.

Money rules that hold for every gateway:

- The payment row is written **before** the customer sees a link.
- A payment is credited **once**, by the provider's webhook and by a background reconciler that polls open payments
  every `PAYMENT_RECONCILE_INTERVAL_SEC` (30 s), whichever comes first. Webhook and poller together still produce one
  credit.
- A webhook body is never trusted on its own: it needs a valid signature or source IP, and wherever the provider has a
  status API the payment is re-fetched before it is credited.
- Open payments expire after `PAYMENT_MAX_PENDING_HOURS` (24).
- Secrets never reach logs or the audit trail.

## Webhook URLs

Webhooks arrive at `https://<domain>/webhooks/<key>`, where `<key>` is in the table below. The domain must be the public
HTTPS address (`PUBLIC_BASE_URL`). Where the table says "automatic", the URL is sent with every invoice and you do not
configure anything in the provider's cabinet. Where it says "cabinet", paste the URL there.

Currency: the shop works in rubles. Gateways that charge in another currency (Stripe, PayPal, the crypto ones) convert at
a rate you set or, for TON and USDT, optionally an automatic rate (`CRYPTO_RATE_AUTO`, from CoinGecko, cached
`CRYPTO_RATE_CACHE_SEC`, with your fixed `*_RATE_RUB` as the fallback). The wallet always receives the ruble amount.

| Gateway | Key | What to configure | Webhook |
|---|---|---|---|
| Telegram Stars | `stars` | `STARS_ENABLED`, `STARS_RUB_PER_STAR` (wallet rubles per Star, default 1.5). Donations: `DONATIONS_ENABLED`. Plan payments can use a native 30-day Stars subscription. | none (bot updates) |
| CryptoBot | `cryptobot` | `CRYPTOBOT_TOKEN` (Crypto Pay API token), `CRYPTOBOT_ACCEPTED_ASSETS`, `CRYPTOBOT_TESTNET` | cabinet (in the Crypto Pay app settings) |
| Heleket | `heleket` | `HELEKET_MERCHANT_ID`, `HELEKET_API_KEY`, currency options | automatic |
| NOWPayments | `nowpayments` | `NOWPAYMENTS_API_KEY`, `NOWPAYMENTS_IPN_SECRET`, `NOWPAYMENTS_PAY_CURRENCY` | automatic |
| Plisio | `plisio` | `PLISIO_API_KEY`, optional `PLISIO_CURRENCY` | automatic |
| TON (direct) | `ton` | `TON_WALLET_ADDRESS`, optional `TON_API_KEY`, `TON_RATE_RUB` | none (polls the blockchain) |
| USDT TRC-20 (direct) | `usdt_trc20` | `USDT_TRC20_ADDRESS`, optional `USDT_TRC20_API_KEY`, `USDT_TRC20_RATE_RUB` | none (polls TronGrid) |
| YooKassa | `yookassa` | `YOOKASSA_SHOP_ID`, `YOOKASSA_SECRET_KEY`, optional 54-FZ receipts (`YOOKASSA_RECEIPT_*`, `YOOKASSA_VAT_CODE`) | cabinet; checked by source IP (`YOOKASSA_CHECK_IP`) |
| YooMoney | `yoomoney` | `YOOMONEY_WALLET`, `YOOMONEY_NOTIFICATION_SECRET`, `YOOMONEY_PAYMENT_TYPE` (`AC` card, `PC` wallet) | cabinet (HTTP notifications) |
| FreeKassa | `freekassa` | `FREEKASSA_SHOP_ID`, `FREEKASSA_SECRET1`, `FREEKASSA_SECRET2`, optional `FREEKASSA_API_KEY` | cabinet; the notification method (GET or POST) is a shop setting there, both are accepted |
| Robokassa | `robokassa` | `ROBOKASSA_MERCHANT_LOGIN`, `ROBOKASSA_PASSWORD1`, `ROBOKASSA_PASSWORD2`, `ROBOKASSA_HASH_ALGORITHM` (`md5` or `sha256`, must match the cabinet), `ROBOKASSA_TEST_MODE` | cabinet (ResultURL) |
| Platega | `platega` | `PLATEGA_MERCHANT_ID`, `PLATEGA_SECRET`, optional fixed `PLATEGA_PAYMENT_METHOD` | cabinet |
| WATA | `wata` | `WATA_TOKEN` (terminal access token), `WATA_SANDBOX` | cabinet (the terminal's webhook); the public key is downloaded or pinned in `WATA_PUBLIC_KEY` |
| RollyPay | `rollypay` | `ROLLYPAY_API_KEY`, `ROLLYPAY_SIGNING_SECRET`, optional `ROLLYPAY_PAYMENT_METHOD` (`sbp` or `card`) | cabinet: the cashier's `callback_url` (callbacks are verified by HMAC-SHA256 and the status is re-fetched). Not tested with live money |
| cisPay | `cispay` | `CISPAY_SHOP_ID`, `CISPAY_API_KEY` (it also signs webhooks), optional `CISPAY_PAYMENT_METHOD` (`card` or `sbp`) | cabinet: the shop's `webhook_url` (HMAC-SHA256 of the body; the status is re-fetched). Sandbox payments are never credited |
| TabPay | `tabpay` | `TABPAY_API_KEY`, `TABPAY_WEBHOOK_SECRET` (a separate signing secret), optional `TABPAY_PAYMENT_METHOD` | cabinet: the shop's Webhook tab (signature with a timestamp; the status is re-fetched). Test payments are never credited |
| SeverPay | `severpay` | `SEVERPAY_MID`, `SEVERPAY_TOKEN`, `SEVERPAY_CLIENT_EMAIL` | cabinet: the terminal's `WEBHOOK_URL` (HMAC-SHA256 of the JSON; the status is re-fetched) |
| Cashera | `cashera` | `CASHERA_API_KEY`, `CASHERA_SECRET`, optional `CASHERA_PAYMENT_METHOD` | enable notifications in the cabinet; the URL is sent with every invoice. Webhooks carry the key and secret in headers, so the status is always re-fetched. Settled in USD |
| ParityPay | `paritypay` | `PARITYPAY_SHOP_ID`, `PARITYPAY_SECRET_KEY`, `PARITYPAY_SECRET_KEY2` (signs webhooks), optional `PARITYPAY_SERVICE` | the URL is sent with every invoice |
| Aurapay | `aurapay` | `AURAPAY_SHOP_ID`, `AURAPAY_API_KEY`, `AURAPAY_SECRET_KEY2` (signs webhooks), optional `AURAPAY_SERVICE` | the URL is sent with every invoice |
| Antilopay | `antilopay` | `ANTILOPAY_PROJECT_ID`, `ANTILOPAY_SECRET_ID`, `ANTILOPAY_SECRET_KEY` (your RSA private key), `ANTILOPAY_CALLBACK_KEY` (their public key) | cabinet (callback URL) |
| CloudPayments | `cloudpayments` | `CLOUDPAYMENTS_PUBLIC_ID`, `CLOUDPAYMENTS_API_SECRET` | cabinet: set Check, Pay and Fail notifications to the same URL |
| Tribute | `tribute` | `TRIBUTE_API_KEY` (it also signs webhooks), optional `TRIBUTE_SHOP_ID` | cabinet (API key settings) |
| lava.top | `lavatop` | `LAVATOP_API_KEY`, `LAVATOP_OFFER_ID`, `LAVATOP_WEBHOOK_SECRET`, `LAVATOP_PAYMENT_METHOD` | cabinet; authenticated by `X-Api-Key` or Basic auth |
| Stripe | `stripe` | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_CURRENCY`, `STRIPE_RUB_PER_UNIT` | cabinet: events `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed`, `checkout.session.expired` |
| PayPal | `paypal` | `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID`, `PAYPAL_CURRENCY`, `PAYPAL_RUB_PER_UNIT`, `PAYPAL_SANDBOX` | cabinet: events `CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.*` |
| Manual card / SBP | `manual` | `MANUAL_TRANSFER_ENABLED`, `MANUAL_TRANSFER_DETAILS` (shown to the buyer) | none: the buyer sends a receipt and a payments admin confirms it |
| Custom HTTP | `custom` | `CUSTOM_*`, described in `.env.example` and the admin | automatic |

For the signature or source-IP check of a provider that is behind a reverse proxy, the app must see the real client
address; the Docker stack already passes it through Caddy.

## Caveats worth knowing

- **Nothing here has been tested with live money.** The gateways follow the providers' documentation and are tested
  against mocks and signed fixtures. Make a small test payment with each gateway you enable and check the credit.
- **lava.top** needs a product with a *dynamic price*; put its offer id in `LAVATOP_OFFER_ID`.
- **Direct USDT** identifies payments by amount: each open invoice gets a unique fraction of a cent, and the buyer must
  send exactly the shown amount. An underpaid transfer is not credited automatically; it stays pending for an admin to
  decide. Direct TON identifies payments by the comment text, which the `ton://` link fills in.
- **Stripe and PayPal** use your fixed rate, so a currency move changes your margin until you update the rate.
- **YooKassa** no longer serves the self-employed; check that your legal status is supported before relying on it.
  Heleket and some other crypto services can be problematic for sellers in sanctioned jurisdictions: your call.
- **YooMoney** cannot be polled without OAuth, so a verified notification is trusted as is; a second transfer through the
  same link is credited as a separate top-up.
- **Platega** has no recurring API, so saved cards work with YooKassa and CloudPayments only.
- **Webhook-less gateways** (TON, USDT, Stars, manual) rely on the reconciler and the bot, so make sure the app is
  running continuously.

## Refunds and chargebacks

When a provider reports a refund or a chargeback (or Telegram reports a refunded Stars payment), the payment becomes
`refunded` and the bot takes back what it gave: the days of the plan bought with it (the subscription may end right
away), what is left of its money on the balance, and the referral commissions paid from it. Nothing goes below zero and a
repeated notification changes nothing. A refund an admin makes from the bot or the web admin keeps its own choice
("debit the balance").

## Saved cards and auto-renewal

Auto-renewal normally takes the price from the wallet. With `SAVED_CARDS_ENABLED=true`, a customer can opt in at checkout
to keep their card (YooKassa autopayments, CloudPayments tokens); when the balance is short at renewal time the missing
amount is charged to the card, once per period. The provider must enable recurring payments for your shop first.

## "Мой налог" receipts

Self-employed sellers in Russia can have a receipt registered for each payment automatically.
See [npd-receipts.md](npd-receipts.md).

## Testing a payment

1. Enable one gateway in the admin and, if it has a sandbox or test mode, turn it on.
2. In the bot, open the wallet, top up the minimum amount, pay.
3. In the admin, **Payments** shows the payment moving from pending to succeeded and the ledger entry. If it stays
   pending, check the webhook URL in the provider's cabinet and `netoryx logs app` for a "webhook ... rejected" line.
