# Privacy: what Netoryx stores and what it does not

Netoryx is built so that a VPN operator can run a service without collecting more than the business needs. This page
describes the defaults of this release; the code is open, so every statement can be checked.

## What is not stored

- **Customer IP addresses.** With the default `CLIENT_IP_LOGGING=off` they appear in no log and in no database column
  (web access log, web cabinet sessions). Rate limits and provider IP allow-lists still work, in memory only. `masked`
  keeps just the network (`203.0.113.x`, or a /48 for IPv6); `full` keeps the address. Choose deliberately, and if you
  choose anything but `off`, say so in your privacy policy.
- **Visited sites and traffic content.** Netoryx never sees them. The panels keep per-account traffic counters (bytes),
  which is what traffic limits need.
- **The Xray access log.** Device limits do not need it. With `XUI_DEVICE_LIMIT=hwid` (default) the bot pushes no IP-based
  limit (`limitIp`) to 3x-ui, so the Xray access log, which records client IPs and visited hosts, can stay off. Switching
  to `XUI_DEVICE_LIMIT=ip` needs that log; do it only if you accept the consequences.
- **Raw device identifiers.** The `x-hwid` value sent by client apps is stored only as an HMAC-SHA256 under a key derived
  from `SECRET_KEY`. Nobody can read the original from the database, only check whether a given value was seen. Devices
  not seen for `HWID_DEVICE_TTL_DAYS` (30) are forgotten.
- **Secrets in logs.** Tokens, passwords and API keys are kept out of logs and the audit trail. Stored panel passwords,
  API keys and SOCKS5 passwords are encrypted with `SECRET_KEY`.

## What is stored

- Telegram ID, username and display name, language, referral links, ban state.
- Subscriptions (plan, expiry, limits, per-subscription token), the mapping to panel accounts, device hashes.
- Payments, wallet balance and the ledger, promo and referral records, withdrawal requests, tax receipts if enabled.
- Support tickets, broadcast history, the admin audit trail (who changed what), outgoing webhook delivery records.
- E-mail addresses, only for customers who choose e-mail login to the web cabinet.
- Backups of all of the above (see [backups.md](backups.md)).

## Telegram proxy

The MTProto proxy (telemt) and the SOCKS5 inbound see connecting addresses at the network level. The proxy server enforces
its own cap on simultaneous source IPs per account to detect link sharing; Netoryx reads the resulting usage, rotates the
link on abuse, and does not store customer addresses.

## Your own infrastructure

Your hosting provider, the VPN panel, the reverse proxy and Telegram have their own logs outside Netoryx's control. The
Docker stack rotates container logs (10 MB x 3 files per container). Keep the panels' logging at the minimum you can
live with.

## Legal

Running a VPN service may carry legal duties in your country (data retention, registration, tax receipts). Netoryx does
not give legal advice. `TERMS_URL`, `PRIVACY_URL` and `LEGAL_CONSENT_REQUIRED` let you show your own documents and ask
customers for consent before they use the bot.
