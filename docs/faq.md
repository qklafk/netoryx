# FAQ

Russian: [ru/faq.md](ru/faq.md).

## Installing

**What do I need?** A Linux server (Ubuntu 22.04 / 24.04 / 26.04, Debian 12 / 13, AlmaLinux 9 or Rocky Linux 9; x86_64 or
ARM64; 2 vCPU / 2 GB RAM or more), root access and a bot token from @BotFather. Ubuntu 24.04 is the system tested on a
real server; the others are supported (beta). A domain is recommended, not required. See
[installation.md](installation.md).

**Do I need a domain?** No. Without `--domain` the installer uses `<ip-with-dashes>.sslip.io`, which gets a real HTTPS
certificate. A domain of your own looks better to customers and survives a server move.

**Can it share a server with an existing 3x-ui?** Yes: add that panel in **Panels** by its URL. Keep the panel port
closed to the internet.

**How do I open the web admin again?** `sudo netoryx admin-link` prints a new one-time link, or send `/web` to the bot
as an admin.

**Where are my files?** `/opt/netoryx` (`.env`, `docker-compose.yml`, per-server files in `deploy/`). Data lives in Docker
volumes.

## Using it

**How do customers connect?** They open the bot, start a trial or buy a plan, and tap the connect button: the link opens in
Happ, INCY, v2RayTun or Hiddify and imports the subscription. The same link works pasted by hand.

**Can I use it for friends and family only?** Yes: `APP_MODE=personal` (or switch in the admin) replaces sales with invites
and members; payments are off, and cost sharing reminders are optional.

**Can one customer have several subscriptions?** Yes, each is independent with its own link and expiry.

**How do I change prices, plans and texts?** In the web admin: **Plans**, **Texts** (override any bot text, including
for white-labeling), **Menu** (main-menu buttons), **Settings**.

**Which payment methods do I start with?** Whatever you can get an account for. Telegram Stars needs no account (set
`STARS_ENABLED`). See [payments.md](payments.md).

**How are devices limited?** By the bot, from the HWID that Happ and v2RayTun send. No IP addresses are involved. Apps that
send none are served unless `HWID_REQUIRED=true`.

## Operating

**Does updating disconnect my customers?** `netoryx update` rebuilds only the application container, so no. Restarting
`edge`, `caddy` or the panel does. See [upgrading.md](upgrading.md).

**Is anything logged about my customers' traffic?** No visited sites, no content, and by default no IP addresses. See
[privacy.md](privacy.md).

**What if Telegram is unavailable for my customers?** Enable the web cabinet ([web-cabinet.md](web-cabinet.md)) so they
can reach their subscription without the bot. Publish the [Telegram access link](telegram-only.md) so people can reach Telegram and your bot.

**What if my customers' mobile networks only allow approved hosts?** Use relay endpoints with the LTE profile
([panels.md](panels.md)).

**How do I back up?** `sudo netoryx backup`, plus the automatic daily backup to Telegram. Set `BACKUP_PASSWORD`.
See [backups.md](backups.md).

**How do I remove it?** `sudo netoryx uninstall`. Take a backup first.

## License

**Can I sell VPN subscriptions with Netoryx?** Yes. The AGPL-3.0-or-later license allows commercial use. There are no
royalties or fees.

**When do I have to publish source code?** Only if you modify the code and let users interact with the modified version
over a network: then you must offer them that modified source under the same license. Running the stock release does not
oblige you to publish anything.

**Are my settings, prices, texts and branding "code"?** No. They live in the database and `.env`. Changing them in the
admin never triggers any obligation.

**Can I fork it and rename it?** Yes, under the same license, keeping the copyright notices.

This is a plain-language summary, not legal advice; the license text in [LICENSE](../LICENSE) is what counts.
