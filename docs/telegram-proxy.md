# Telegram proxies

Русская версия: [ru/telegram-proxy.md](ru/telegram-proxy.md).

> **Legacy, optional.** MTProto and SOCKS5 proxies do not work in some countries and are off by default
> (`TG_PROXY_ACCESS=off`, `TG_SOCKS_ENABLED=false`); the installer no longer installs them unless asked. To help people reach Telegram,
> use the shared [Telegram access link](telegram-only.md) instead.

The bot can give every user a personal proxy for Telegram. Two kinds are supported, and both can be enabled at once:

| Kind | Where it runs | How it connects |
| --- | --- | --- |
| MTProto | a telemt server, or an `mtproto` inbound in 3x-ui 3.5+ | `tg://proxy` / `https://t.me/proxy` link, one secret per user |
| SOCKS5 | a `mixed` inbound in 3x-ui (2.x and 3.1+) | `tg://socks` / `https://t.me/socks` link, login `tg<id>` and a random password |

Who gets a proxy is set by `TG_PROXY_ACCESS` (`everyone`, `subscribers`, `off`); owners and admins always get one. SOCKS5
follows the same rules: when a subscription ends (`subscribers` mode), on a ban, when a user is deleted or when the mode
changes, the account is removed from the inbound, and it comes back with the same password when access returns. The
"New link" button in the bot and the Mini App rotates both the MTProto secret and the SOCKS5 password.

## Enabling SOCKS5

1. In the 3x-ui panel create an inbound of type **mixed** (called `socks` in older versions) on a free port. You do not
   need to set up authentication: the bot switches the inbound to `password` mode and manages the `accounts` list itself.
2. Open that port in the server firewall. SOCKS5 cannot share port 443 with a TLS service (Reality, Caddy, nginx), so pick
   another port such as 1080 or 8388. The installer's `--with-socks` / `--socks-port 2080` does this for you.
3. In the web admin open **Panels**, pick the panel, press "Refresh inbounds", then press "SOCKS5 proxy for Telegram" on
   the row with the `mixed` protocol. The API equivalent is `PATCH /api/admin/inbounds/<id>` with the body
   `{"tg_socks": true}`.
4. If the server is behind NAT or a relay, put the public address in the panel `options`:
   `{"tg_socks_host": "socks.example.com", "tg_socks_port": 1080}`. Without them the host from the panel address (or
   `tg_proxy_host`) and the inbound's port are used.
5. Users see a "SOCKS5 proxy" block in the bot's proxy section (`/proxy`) and in the Mini App.

The global switch is `TG_SOCKS_ENABLED` (on by default; nothing is shown until an inbound is selected). The number of
servers per user is `TG_PROXY_MAX_SERVERS`, and the pause between link rotations is `TG_PROXY_ROTATE_COOLDOWN_MIN`.

## How it works and what to keep in mind

- SOCKS5 accounts are not panel "clients", so the bot edits the inbound's `settings.accounts` list through the inbound
  update call (`/panel/api/inbounds/update/<id>`). Other people's entries and the rest of the inbound are preserved and
  other inbounds are untouched. Edits inside the bot process are queued; after writing, the bot re-reads the inbound and
  retries if a concurrent editor overwrote the change.
- The bot manages only accounts named `tg<number>`. Accounts you created by hand in the panel are left alone, and
  orphaned `tg<number>` accounts are removed only when `DELETE_ORPHAN_CLIENTS=true`.
- The proxy is never open: the bot forces `auth = password`. Do not switch the inbound to `noauth` by hand.
- SOCKS5 does not encrypt the connection to the proxy itself. Telegram message contents stay protected by MTProto
  end-to-end encryption, but your provider can see that you connect to a proxy. If that matters, use MTProto with
  Fake-TLS.
- Passwords are stored encrypted in the database (key: `SECRET_KEY`); links containing a password are never logged.
- Speed and traffic limits do not apply to SOCKS5.
- Policy changes (subscription end, ban) are applied by a background job once a minute, so a ban or unban takes effect on
  the next pass.
