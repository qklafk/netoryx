# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses semantic versioning.

## [Unreleased]

### Added

- Shared Telegram-only link (`/sub/tg/<token>`, Happ and v2RayTun) with a public landing page at `/tg`: only Telegram goes
  through the VPN, everything else connects directly, and the 3x-ui server refuses that client anything but Telegram.
  Optional monthly traffic limit and a daily traffic alert, a bot admin screen and a web admin page with "new link";
  the installer prints the page and link instead of a proxy link. See `docs/telegram-only.md`.

### Changed

- The Telegram MTProto and SOCKS5 proxies are legacy and off by default (`TG_PROXY_ACCESS=off`, `TG_SOCKS_ENABLED=false`);
  the installer no longer asks about them (`--with-tgproxy` and `--with-socks` still work) and the menu button and Mini App
  screen stay hidden until they are turned on.

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
