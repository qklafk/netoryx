# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses semantic versioning.

## [Unreleased]

## [0.9.1-beta] - 2026-11-13

### Added

- Shared Telegram-only link (`/sub/tg/<token>`, Happ and v2RayTun) with a public landing page at `/tg`: only Telegram goes
  through the VPN, everything else connects directly, and the 3x-ui server refuses that client anything but Telegram.
  Optional monthly traffic limit and a daily traffic alert, a bot admin screen and a web admin page with "new link";
  the installer prints the page and link instead of a proxy link. See `docs/telegram-only.md`.
  It uses a `leastPing` balancer over eligible servers (`TG_ONLY_EXCLUDED_COUNTRIES` keeps some countries out), works with
  3x-ui, Remnawave, Marzban, PasarGuard and Marzneshin, and `netoryx tg-link` prints the link again.
- Certificate expiry alerts: the app checks the TLS certificate of `PUBLIC_BASE_URL` (and `TLS_WATCH_HOSTS`) every 12 hours,
  warns the owner and admins in time, and shows the state in the web admin under System, Certificates. See
  `docs/troubleshooting.md`.

- Full server backup and restore: `netoryx backup --full` saves the database, the VPN panel's data, certificates, settings
  and credentials into one archive, and `netoryx restore <file>` puts it back on the same or a new server (installed with
  the same panel). See `docs/backups.md`.

### Changed

- The public pages (`/tg`, `/open`, gift and payment pages) follow the visitor's light or dark system theme.

- The Telegram MTProto and SOCKS5 proxies are legacy and off by default (`TG_PROXY_ACCESS=off`, `TG_SOCKS_ENABLED=false`);
  the installer no longer asks about them (`--with-tgproxy` and `--with-socks` still work) and the menu button and Mini App
  screen stay hidden until they are turned on.
- The Russian README is now the primary one (`README.md`); the English version is `README.en.md`.
- `docker-compose.yml` requires `SECRET_KEY` in `.env`; manual installs must set it before updating (see
  `docs/upgrading.md`). The installer already generates it.
- Shadowsocks-2022 is optional and may be unreliable on some networks; VLESS Reality on 443 stays the recommended default.

### Fixed

- Marzneshin: changes to existing users (renewal, limits, enabling) were refused by the panel with HTTP 422.
- PasarGuard: users that must start disabled (for example the Telegram-only account) could not be created.
- Remnawave: the nodes now reload after the Telegram-only account changes, so the link works without a manual node restart.

### Security

- Security hardening pass: the Docker stack no longer starts with a missing or empty `SECRET_KEY`, so stored panel passwords
  and API keys are always encrypted with a key you control.
- The shared Telegram-only link could reach any host by sending a Telegram server name in the TLS handshake (the sniffed
  name only chose the route). Sniffing now overrides the destination, so that client really reaches only Telegram; the
  servers are corrected on the next sync.
- Hidden plans can no longer be listed or bought from the Mini App without their signed link, and are never sold as gifts.
- Testing a connection to a free-form panel address in the web admin is owner-only (it lets the server connect anywhere).
- A user can have at most 20 unpaid invoices at a time, so the payment providers cannot be flooded through the bot.
- `/status` answers only in private chats.
- Support, channel, terms and privacy links accept only `http(s)://` and `tg://` links; a link without a scheme gets
  `https://`.
- Database backup files are created readable by their owner only.
- GitHub Actions from third parties are pinned to commit hashes.
- A refund or chargeback reported by the payment provider takes back what the payment gave: the days of the plan bought
  with it, what is left of it on the balance and the referral commissions paid from it (never below zero). Refunds made
  by an admin keep their own "debit the balance" choice.
- New permission `subs_grant` for free service (adding days, a later expiry, higher limits, changing the plan): admins
  and the owner have it, the support role no longer does.
- The referral sign-up bonus in rubles reaches the invited user after their first payment
  (`REFERRAL_INVITEE_REQUIRES_PAYMENT`, on by default), so throwaway accounts cannot collect it.
- A single-use win-back offer (`WINBACK_USAGE=once`) discounts one purchase even if several invoices were created while
  it was active.
- The 3x-ui image is pinned to a tested release (`XUI_VERSION`). Releases carry `install.sh` (installing that release)
  and `SHA256SUMS`; see "Installing a checked release" in `docs/installation.md`.

## [0.9.0-beta] - 2026-11-12

First public beta.

### Added

- Telegram bot, web admin (React) and Telegram Mini App for selling or sharing VPN access; personal mode for private use.
- Panel backends: 3x-ui (2.x and 3.x APIs), Remnawave (3.0+), Marzban, Marzneshin, PasarGuard and Hiddify Manager, several at
  once; locations, direct / relay / CDN endpoints, a separate LTE profile, import of existing users.
- Subscriptions: plans by duration, devices and traffic, trial, several subscriptions per user, wallet, auto-renew, gifts,
  promo codes and groups, win-back discounts, traffic and device add-ons, audience rules for plans.
- Personal subscription link for Happ, INCY, v2RayTun and Hiddify with combined and per-server links.
- Xray JSON subscription for Happ and v2RayTun: automatic choice of the fastest server, Russian sites directly and
  split DNS (configurable).
- Optional Shadowsocks-2022 entry point for 3x-ui.
- Bot-side HWID device limit that needs no customer IP addresses.
- Payments: Telegram Stars, CryptoBot, Heleket, NOWPayments, Plisio, direct TON and USDT TRC-20, YooKassa, YooMoney,
  FreeKassa, Robokassa, Platega, RollyPay, cisPay, TabPay, SeverPay, Cashera, ParityPay, AuraPay, WATA, Antilopay,
  CloudPayments, Tribute, lava.top, Stripe, PayPal, manual transfer and a custom HTTP gateway (28 methods); saved cards
  for auto-renewal; "Мой налог" receipts.
- Referral program with days, balance and partner modes, multi-level rewards and withdrawals.
- Personal MTProto and SOCKS5 proxies for Telegram.
- Web cabinet at `/cabinet` with bot-link, Telegram Login Widget and e-mail login.
- Ad and tracker blocking and an optional YouTube exit for 3x-ui servers.
- Admin: roles, runtime settings, text overrides, main-menu editor, broadcasts, tickets, analytics, audit log, encrypted
  backups, outgoing webhooks, public API keys, external blacklist.
- Languages: Russian, English, Ukrainian, Kazakh, Uzbek, Persian, Chinese.
- One-command installer (`install.sh`) with a choice of panel: 3x-ui, Remnawave, Marzban, PasarGuard or Marzneshin,
  each installed and verified end to end on a real server; bilingual prompts; servers download only what they run.
- Docker stack (PostgreSQL, Caddy with automatic HTTPS, HAProxy routing by TLS server name on port 443) and the
  `netoryx` management command.
- Mini App: app cards with icons for the user's platform, subscription ring and traffic bar, light and dark themes,
  operator logo (`BRAND_LOGO_URL`); the bot keeps a single screen in private chats.
- Project website and documentation in Russian and English, including a step-by-step guide for beginners.
- Privacy defaults: no customer IP addresses stored (`CLIENT_IP_LOGGING=off`), device identifiers stored only as keyed
  hashes.
- Compact Reality ClientHello in links (`REALITY_COMPACT_HELLO`) for networks that drop large handshakes.

### Known limitations

- Beta software: payment gateways are tested against provider documentation and mocks, not with live money.
- Translations other than Russian and English are not reviewed by native speakers.
- The installer targets Ubuntu 22.04 / 24.04 / 26.04, Debian 12 / 13, AlmaLinux 9 and Rocky Linux 9 (x86_64 and
  arm64); only Ubuntu 24.04 x86_64 has been tested on a real server.
- Shadowsocks may be unreliable on some networks; VLESS Reality on port 443 is the recommended default.

[0.9.0-beta]: https://github.com/qklafk/netoryx/releases/tag/v0.9.0-beta
