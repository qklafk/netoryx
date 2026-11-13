# FAQ

Russian: [ru/faq.md](ru/faq.md).

New to servers? The [beginner's guide](beginners.md) goes from renting a server to the first customer.

## Installing

**What do I need?** A Linux server (Ubuntu 22.04 / 24.04 / 26.04, Debian 12 / 13, AlmaLinux 9 or Rocky Linux 9; x86_64 or
ARM64; 2 vCPU / 2 GB RAM or more), root access over SSH and a bot token from @BotFather. Ubuntu 24.04 is the system tested
on a real server; the others are supported (beta). A domain is recommended, not required. See
[installation.md](installation.md).

**Do I need a domain?** No. Without `--domain` the installer uses `<ip-with-dashes>.sslip.io`, which gets a real HTTPS
certificate. A domain of your own looks better to customers and survives a server move: customers' links stay the same.

**Which panel should I pick?** 3x-ui is the default and supports everything, including ad blocking and the YouTube exit.
Remnawave, Marzban, PasarGuard and Marzneshin are also installed by one command (`--panel ...`) and were tested on a live
server; a few features are 3x-ui only ([installation.md](installation.md#choosing-a-panel)). A panel that already runs on
another server is added in the admin (**Servers**), see [panels.md](panels.md).

**Can it share a server with an existing 3x-ui?** Yes: add that panel in **Servers** by its URL. Keep the panel port closed
to the internet.

**How do I install a checked release instead of the newest code?** Download `install.sh` and `SHA256SUMS` from the
releases page and verify the checksum ([installation.md](installation.md#installing-a-checked-release)).

**I reinstalled several times and HTTPS stopped working.** Let's Encrypt issues at most five identical certificates a
week. Wait, or use your own domain; when moving a server, take the certificates along in a full backup
(`netoryx backup --full`).

**Where are my files?** `/opt/netoryx` (`.env`, `docker-compose.yml`, per-server files in `deploy/`), credentials in
`/root/netoryx` (root only), the install log in `/var/log/netoryx-install.log` (no secrets). Data lives in Docker volumes.

## Customers and connections

**How do customers connect?** They open the bot, start a trial or buy a plan, and tap the connect button: the link opens in
Happ, INCY, v2RayTun or Hiddify and imports the subscription. The same link works pasted by hand.

**Which apps work?** Happ, v2RayTun, INCY, Hiddify and any client that speaks VLESS Reality. Happ and v2RayTun get a JSON
subscription with an Auto entry that picks the fastest server and Russian sites going direct
([subscriptions.md](subscriptions.md)).

**The VPN "connects" but there is no internet.** Most often the network drops a large TLS hello. Netoryx gives out links
with a compact hello by default (`REALITY_COMPACT_HELLO`); refresh the subscription in the app. Other causes and checks:
[troubleshooting.md](troubleshooting.md).

**Mobile networks only reach approved hosts.** Then foreign addresses are unreachable and only a relay server inside the
allowed network helps: relay endpoints with the LTE profile ([panels.md](panels.md)). Nobody can guarantee this works.

**Why not Shadowsocks?** It does not use TLS and some networks filter it. VLESS Reality on 443 is the main protocol;
Shadowsocks-2022 can be added as a fallback (`--with-shadowsocks`), but do not rely on it alone.

**How are devices limited?** By the bot, from the HWID that Happ and v2RayTun send. No IP addresses are involved. Apps that
send none are served unless `HWID_REQUIRED=true`.

**Can one customer have several subscriptions?** Yes, each is independent with its own link and expiry.

**YouTube still shows ads.** Ads depend on the exit country. YouTube can be sent through a separate server in a country
without ads (`YOUTUBE_EXIT_ENABLED`, 3x-ui only); this is best-effort.

## Telegram access

**What is it?** One shared link and a `/tg` page: a person installs Happ, adds the link, and Telegram works while
everything else goes direct. The installer prints it and `netoryx tg-link` prints it again
([telegram-only.md](telegram-only.md)).

**Why is it safe to publish?** It is one link for everybody and costs the owner nothing but Telegram traffic. The limit is
enforced on the server: that client reaches only Telegram's addresses and domains, and faking a Telegram name to reach
another host does not work. It cannot be used as a general VPN.

**What about traffic?** Set a monthly limit per server (`TG_ONLY_MONTHLY_LIMIT_GB`) and an admin alert for high daily
traffic (`TG_ONLY_DAILY_ALERT_GB`, 50 GB by default). You can issue a new link at any time; the old one stops working.

**Which servers are used?** Any panel except Hiddify, in a country not listed in `TG_ONLY_EXCLUDED_COUNTRIES` (RU and BY,
among others, by default). With several servers the app picks the fastest.

**What happened to the MTProto and SOCKS5 proxies?** They are kept as legacy and off by default: they stopped working in
some countries ([telegram-proxy.md](telegram-proxy.md)).

**Telegram is unavailable for my customers altogether.** Enable the web cabinet ([web-cabinet.md](web-cabinet.md)) so they
can use their subscription from a browser; the Telegram access link helps them reach Telegram and your bot.

## Sales and money

**Which payment methods do I start with?** Whatever you have an account for. Telegram Stars needs no contract
(`STARS_ENABLED`). There are 28 methods: regional gateways for cards and SBP, crypto, Stripe, PayPal, manual transfer
with a receipt ([payments.md](payments.md)). Check each provider's terms for your legal status yourself.

**A payment went through but was not credited.** Crediting happens by webhook and again by a background check, so it
usually catches up within minutes. If not, check the webhook URL at the provider and the logs
([troubleshooting.md](troubleshooting.md)). A payment is credited exactly once either way.

**What happens on a refund or chargeback?** The bot takes back what that payment gave: the days bought with it, what is
left of its money on the balance and the referral commissions paid from it. Nothing goes below zero.

**How do referrals work?** Three modes: days, balance, or a partner account with withdrawals. Invitation bonuses arrive
after the invited user's first payment, so farming them with new accounts is pointless
([configuration.md](configuration.md#business-defaults)).

**Can I use it for friends and family only?** Yes: `APP_MODE=personal` (or switch in the admin) replaces sales with
invites and members; payments are off, and cost sharing reminders are optional.

**How do I change prices, texts and the name?** In the web admin: **Plans**, **Texts** (any bot text, including for your
own brand), **Menu**, **Settings**. Customers see your name (`BRAND_NAME`), not Netoryx.

## Admin and security

**How do I open the web admin again?** `sudo netoryx admin-link` prints a new one-time link, or send `/web` to the bot as an
admin.

**Which roles are there?** Owner (everything), admin (everything except managing admins, restoring backups and secrets),
support (users and tickets, no free subscription extensions) and viewer (read-only).

**Does updating disconnect my customers?** `sudo netoryx update` rebuilds only the application, so no. Restarting `edge`,
`caddy` or the panel does. See [upgrading.md](upgrading.md).

**Backups and moving servers?** A daily database backup is sent to Telegram (set `BACKUP_PASSWORD` to encrypt it).
`sudo netoryx backup --full` saves the whole server in one archive and `sudo netoryx restore <file>` puts it back, also on
a new server with the same panel ([backups.md](backups.md)).

**Is anything logged about my customers' traffic?** No visited sites, no content, and by default no IP addresses. There is
no telemetry ([privacy.md](privacy.md)).

**I found a vulnerability.** Report it privately through GitHub (Security → Report a vulnerability), not in public issues
([SECURITY.md](../SECURITY.md)).

**How do I remove it?** `sudo netoryx uninstall` (data stays) or `uninstall --purge` (deletes everything except the full
backups in `/root/netoryx-backups`). Take a backup first.

## Law and license

**Who is responsible for the service?** Netoryx is software only. Whoever installs it becomes the operator and is
responsible for complying with the laws of their country and their customers' countries, and with the terms of third-party
services ([LEGAL.md](../LEGAL.md)). Templates for terms and a privacy policy: [legal](legal/).

**Can I sell VPN subscriptions with Netoryx?** Yes. The AGPL-3.0-or-later license allows commercial use. There are no
royalties or fees.

**When do I have to publish source code?** Only if you modify the code and let users interact with the modified version
over a network: then you must offer them that modified source under the same license. Running the stock release does not
oblige you to publish anything.

**Are my settings, prices, texts and branding "code"?** No. They live in the database and `.env`. Changing them in the
admin never triggers any obligation.

**Can I fork it and rename it?** Yes, under the same license, keeping the copyright notices.

This is a plain-language summary, not legal advice; the license text in [LICENSE](../LICENSE) is what counts.
