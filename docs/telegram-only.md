# Telegram access link (Telegram-only subscription)

Русская версия: [ru/telegram-only.md](ru/telegram-only.md).

One shared link that helps people get **into Telegram** (and so to your bot) when Telegram does not open for them:
**only Telegram goes through the VPN**, everything else connects directly from the device. You can publish and forward
it: put it on a website, in chats and channels, pass it on by word of mouth. It replaces the Telegram proxies of earlier
versions ([telegram-proxy.md](telegram-proxy.md), kept as an optional legacy feature).

It is not a plan and is not tied to a user. Everyone who has the link uses the same single connection.

## What you share

Share the **page address**, `https://<your domain>/tg`. It never changes. The page, written in your service name
(`BRAND_NAME`, never the project name) in Russian or English depending on the visitor's browser language:

- says in two lines what it is ("Telegram will work through this connection; other apps and sites work as usual");
- detects the device and shows the **Happ** download button for it (other platforms are one tap away);
- has an **Add to Happ** button that imports the subscription in one tap;
- shows the subscription link with a copy button and a QR code, and the manual steps ("Add subscription by URL").

The page always shows the **current** subscription link (`https://<your domain>/sub/tg/<token>`), so rotating the token
needs no new announcement. It loads nothing from other sites, has no trackers and is not cached.

Admins find both addresses in the bot admin menu (**Telegram access**) and in the web admin (**Infrastructure →
Telegram access**). The installer prints them with a QR code at the end, and the bot sends them to the owner once after
the first server is ready.

## Rotating the link

If the link has spread too far, press **New link** (bot or web admin). It issues a new subscription token and new
connection keys: the old link and the keys inside already imported configs stop working. The page address stays the
same and immediately shows the new link, but people who imported the old one have to add it again.

## How it is enforced

The restriction is on the server, not only in the client config, so the link cannot be used as a general VPN.

- On every eligible 3x-ui panel there is one dedicated client named `tg-only` on a VLESS inbound (Reality or TLS). Its id
  is generated once and kept in the database (a new one is generated when you rotate the link).
- The bot adds three rules to the panel's Xray template, ahead of all other rules (right after the panel's own API rule):
  1. `tg-only` + Telegram's address ranges → `direct`;
  2. `tg-only` + Telegram's domains → `direct`;
  3. `tg-only` → `blocked`.

  The ranges are Telegram's published list (<https://core.telegram.org/resources/cidr.txt>, stored in
  `app/services/tg_only_data.py`; there is no runtime download). The rules are tagged `tg-only-*`; the bot touches only
  those, keeps them first even next to ad blocking and the YouTube exit, and rewrites the template only when something
  changed (Xray restarts once).
- Order of operations: the rules are put in place first and the client is created afterwards, and a server is added to
  the subscription only after both succeeded. A panel that cannot be updated is not offered.
- When a server stops being eligible (country excluded, panel disabled, feature switched off) the client is deleted
  first and the rules are removed afterwards.

The client config (`/sub/tg/<token>`) sends Telegram's addresses and domains to the VPN and everything else to `direct`.
With two or more servers it uses a `leastPing` balancer; its latency probe goes to `telegram.org`, which the servers
allow. Reality uses the same compact ClientHello as the main subscription (`REALITY_COMPACT_HELLO`).

## Traffic safeguards

Because the link is public, you can cap what it carries:

- `TG_ONLY_MONTHLY_LIMIT_GB` (default `0`, unlimited): the total traffic of the shared client on each server, reset once
  a month (UTC). It is the client's traffic limit in 3x-ui and is applied through the panel API, **without restarting
  Xray**. When a server's client reaches the limit the panel switches it off until the next reset.
- `TG_ONLY_DAILY_ALERT_GB` (default `50`, `0` = off): admins get a notice when the shared client moved more than this in
  a day, at most once a day, so an abused link does not go unnoticed.

## Which servers are offered

A server is offered when all of these hold:

- the panel is **3x-ui** and active (other panel kinds are not supported yet);
- it has an active VLESS inbound with Reality or TLS and at least one active standard endpoint;
- the **country** of its location is not in `TG_ONLY_EXCLUDED_COUNTRIES`.

The country is read from the location's flag emoji (🇩🇪 → `DE`). Default exclusions: `RU,BY,CN,IR,KP,TM,VN`: countries
where Telegram is blocked or restricted, so a Telegram-only exit there would not work. A location without a country flag
(for example the globe 🌍) is **left out** unless `TG_ONLY_ALLOW_UNKNOWN_COUNTRY` is on. The installer detects the country
of the server by its IP and sets the flag of the first location; on a manually added server, set the flag on its
location. The admin screens list the servers that are not offered and why.

## Supported apps

| App | Result |
| --- | --- |
| Happ, v2RayTun | The Xray JSON config |
| Anything else (other VPN apps) | A short text explaining that the link needs Happ or v2RayTun |

Other apps get text, not server links, on purpose: the servers refuse everything except Telegram, so a general-purpose
VPN client that imported them would simply lose its internet. For debugging, `?format=json` forces the JSON.

## Settings

Runtime settings, web admin → Settings, group "Telegram access".

| Setting | Default | Meaning |
| --- | --- | --- |
| `TG_ONLY_ENABLED` | `true` | Master switch. Off: the page answers 404 and the client and rules are removed from the servers. |
| `TG_ONLY_EXCLUDED_COUNTRIES` | `RU,BY,CN,IR,KP,TM,VN` | Comma-separated country codes whose servers are not offered. |
| `TG_ONLY_ALLOW_UNKNOWN_COUNTRY` | `false` | Also offer servers whose location has no country flag. |
| `TG_ONLY_MONTHLY_LIMIT_GB` | `0` | Monthly traffic limit of the shared client per server (0 = unlimited). |
| `TG_ONLY_DAILY_ALERT_GB` | `50` | Alert admins above this daily traffic (0 = off). |

## Limitations

- 3x-ui only for now. Remnawave, Marzban, PasarGuard, Marzneshin and Hiddify servers are not offered: they manage
  their Xray configuration themselves and have no per-client routing hook we can use.
- The page answers `503` with a short friendly text while no server is ready, and `404` when the feature is off.
- Telegram's address list can change; update `app/services/tg_only_data.py` from the page above when it does.
- A server whose public address is a relay (an endpoint of kind "relay") is used as configured; the country check looks
  at the location of the inbound, not at the relay.
- One link per installation; the traffic of everyone using it is counted together.
- The page texts exist in Russian and English; other languages show the English text.
- The installer asks a public GeoIP service (`ipinfo.io`, then `ipapi.co`) for the country of the server's own IP address
  to set the flag. If that fails the flag stays 🌍 and you set it in the web admin.
