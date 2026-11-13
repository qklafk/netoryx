# Beginner's guide

Русская версия: [ru/beginners.md](ru/beginners.md)

A step-by-step guide for people who have never worked with a server. Allow about an hour and a bank card to pay for
the server. You do not write any code: the installer does the work and you only answer its questions.

## 1. What you will get

After the installation you have:

- **A Telegram bot**: customers send `/start`, get a free trial, pick a plan, pay and receive a subscription link.
- **A web admin**: a website where you create plans, see users and payments and configure everything else.
- **A Mini App**: the same as the bot, as an app inside Telegram (subscription, payment, referrals).
- **A VPN for customers, family and friends**: a server with a management panel that people connect to from an app
  (Happ, v2RayTun, Hiddify and similar) by importing a link.

If you install it for yourself and your family you can skip payments: the bot and the admin still hand out links.

## 2. What server you need

A server (VPS) is a rented computer on the internet that runs around the clock. The requirements are those of the
[installation guide](installation.md):

- system: Ubuntu 22.04 / 24.04 / 26.04, Debian 12 / 13, AlmaLinux 9 or Rocky Linux 9 (**Ubuntu 24.04** is
  recommended);
- at least 2 vCPU, 2 GB of RAM and 20 GB of disk (the installer needs at least 10 GB free);
- **root** access over SSH (after the purchase you get the server address, the login `root` and a password);
- free ports 80 and 443 (they are free on a new server);
- a public IPv4 address.

Choose the server's country by your goal: if customers need to reach the internet from another country, the server
has to be in that country. This guide does not recommend hosting companies: any provider that offers the parameters
above will do.

## 3. Preparation: a Telegram bot and your ID

### Create the bot

1. In Telegram find **@BotFather** (with the blue check mark) and press Start.
2. Send `/newbot`.
3. Enter the bot's **name**: how it is shown in the chat list (for example `My VPN`).
4. Enter the bot's **username**: Latin letters, must end with `bot` (for example `my_best_vpn_bot`). If it is taken,
   pick another.
5. BotFather replies with a **token**: a string like `123456789:AAH...`. Copy it.

The token is the bot's password. Never show it to anyone or publish it. If it leaks, send BotFather `/revoke` and
get a new one.

### Find your Telegram ID

Message **@userinfobot**: it replies with a number (for example `123456789`). This is your Telegram ID; the bot uses
it to recognise the owner.

### A domain (optional)

You do not need a domain. Without one the installer builds an address like `1-2-3-4.sslip.io` from your server's IP:
it works at once, with no DNS setup. To use your own domain, create an A record pointing to the server's IP; the
installer checks that it resolves.

## 4. Connecting to the server over SSH

SSH lets you control the server with commands from a text window. You need three things from your provider's email:
the **IP address**, the login (`root`) and the **password**.

### Windows

1. Press `Win`, type **PowerShell**, open it.
2. Run (use your own IP):

   ```
   ssh root@203.0.113.10
   ```
3. On the first connection you are asked about the "fingerprint" (`Are you sure you want to continue connecting`).
   That is normal: type `yes` and press Enter.
4. Type the password. **Nothing appears on the screen while you type** (no dots, no stars): this is expected. Type
   it (or paste with a right click) and press Enter.

### macOS and Linux

Open **Terminal** and run the same `ssh root@IP-address`; the rest is the same as on Windows.

### Phone

Install **Termius** (iOS and Android), create a Host: address = the server IP, user = `root`, password = the one from
your provider. Open the connection and accept the fingerprint. A computer is more comfortable, but installing from a
phone works too.

When you see a prompt like `root@server-name:~#`, you are on the server.

## 5. Running the installation

Run one command on the server (copy it whole):

```bash
curl -fsSL https://raw.githubusercontent.com/qklafk/netoryx/main/install.sh | sudo bash
```

The language is picked automatically; force English with
`curl -fsSL https://raw.githubusercontent.com/qklafk/netoryx/main/install.sh | sudo bash -s -- --lang en`.

The installation takes about **5-10 minutes**; the first build is the longest. The installer shows 9 numbered steps:

1. **Checking the system**: operating system, CPU, memory, disk space.
2. **Collecting settings**: this is where it asks questions (below).
3. **Installing Docker and required packages**: the software everything else runs in; a swap file is created if
   memory is low.
4. **Downloading Netoryx and writing the configuration**: code and settings in `/opt/netoryx`; secrets are generated
   automatically.
5. **Configuring the firewall**: only the needed ports are opened, SSH stays available.
6. **Building and starting the services**: the longest step, a few minutes.
7. **Setting up the VPN and proxies**: the panel, the Reality entry point, registering the servers in the bot.
8. **Waiting for the HTTPS certificate**: up to 1-2 minutes.
9. **Finishing**: the final summary.

### The installer's questions

Every question comes with an explanation. Enter accepts the default shown in square brackets. A wrong answer is
rejected with an explanation and asked again. The texts below are the installer's own.

**Bot token**

> Bot token. Open @BotFather in Telegram -> /newbot -> copy the string like 123456789:AAH... (input is hidden: just paste it and press Enter).
>
> `Bot token:`

Paste the token (nothing appears on screen) and press Enter. The installer checks it with Telegram and prints "Bot
found".

**Your Telegram ID**

> Your Telegram ID is the number the bot uses to recognise the owner. Message @userinfobot, it replies with a number (for example 123456789). Several IDs: comma-separated.
>
> `Your Telegram ID:`

Enter the number.

**Domain**

> Domain for the website and subscription links. No domain? Just press Enter and you get an address like 1-2-3-4.sslip.io (works at once, no DNS setup).
>
> `Domain:`

No domain: press Enter. If you enter a domain that does not point to the server, the installer asks: "The domain ...
does not point to this server (...). Create an A record for this address and wait a few minutes. Use an sslip.io
address instead?" Answer `Y` (default) to use the sslip.io address.

**Service name**

> The name of YOUR service: your customers see it in the bot, the Mini App and their apps (for example MyVPN). Not sure? Press Enter for VPN; you can change it later in the admin.
>
> `Service name [VPN]:`

**VPN management panel**

> Which VPN management panel to install? The bot creates and renews your customers' access through it.
>
> 1. 3x-ui: simple and light, proven in practice (recommended; the only one that supports the Telegram link and Shadowsocks)
> 2. Remnawave: panel for several servers and nodes (beta)
> 3. Marzban: popular Xray panel (beta)
> 4. PasarGuard: Marzban's successor (beta)
> 5. Marzneshin: panel with marznode nodes (beta)
> 6. No panel: you already run a panel on another server: connect it later in the web admin (Hiddify is connected this way too)
>
> `Panel number [1]:`

As a beginner: press Enter (3x-ui).

With `--yes` and the `--bot-token` / `--admin-id` flags there are no questions at all (all flags are listed in the
[installation guide](installation.md)).

## 6. What you see at the end

The installer prints a summary "Netoryx is installed" with these blocks:

- **Bot**: the link to your bot.
- **Subscription address**: your website address.
- **Web admin**: a one-time login link (valid for a few minutes). If it expires, run `netoryx admin-link` for a new one.
- **Logins and passwords**: the file they are stored in (usually under `/root/netoryx/`, for example
  `xui-credentials`). Read with `sudo cat <file>`. Never share them.
- **Your shared Telegram link** (3x-ui): a page address (`/tg`) you can publish and a subscription link with a QR code.
  People open the page, install Happ and tap "Add to Happ": only Telegram will go through the VPN ([details](telegram-only.md)).
- The installer log: `/var/log/netoryx-install.log`.

### First steps

1. Open the web admin link and create a **plan** (Plans section).
2. Open the bot and send `/start`: check the free trial.
3. Install **Happ** (or v2RayTun) on your phone, press "My subscription" in the bot and import the link into the app.
   Turn the VPN on and check that the internet works.
4. Then set up payments ([payments](payments.md)) if you want to sell subscriptions.

The 3x-ui panel is closed to the internet: open it through an SSH tunnel; the summary shows the command.

## 7. Everyday commands

Run them on the server (over SSH):

| Command | What it does |
| --- | --- |
| `netoryx status` | shows the containers and the application's health |
| `netoryx logs [service]` | follows the log (default: the `app` service); leave with `Ctrl+C` |
| `netoryx update` | fetches the new version and restarts only the application; VPN connections are not interrupted |
| `netoryx backup` | creates a database backup now |
| `netoryx backup --full` | backs up the whole server (panel, certificates, settings) to `/root/netoryx-backups`; see [backups](backups.md) |
| `netoryx restore <file>` | puts a full backup back, also on a new server |
| `netoryx admin-link` | prints a one-time login link for the web admin |
| `netoryx restart [service]` | restarts everything or the named services (restarting `edge`, `caddy` or the panel drops VPN connections for a few seconds) |
| `netoryx uninstall [--purge]` | stops Netoryx; data stays. With `--purge` data, code and secrets are deleted |

If a command asks for rights, put `sudo` in front of it.

## 8. Common problems

- **The installer says the token was not accepted.** The token was copied incompletely or the bot was deleted. Check
  it in @BotFather (`/mybots`) and run the installer again: finished steps are skipped.
- **Ports 80 or 443 are busy.** Another website or proxy already runs on the server. Stop it (or use a clean server)
  and repeat the installation.
- **HTTPS is not ready** ("https://... does not answer yet"). Wait a couple of minutes. With your own domain, check the
  A record points at the server's IP. Let's Encrypt limits how many certificates one domain gets per week: after many
  reinstalls the address may temporarily not get one. `netoryx logs caddy` shows the reason.
- **The installation stopped at some step.** Nothing is lost: the installer names the step and the command to
  repeat; run it again. Details are in `/var/log/netoryx-install.log` (it contains no passwords).
- **The VPN connects but there is no internet.** See [troubleshooting](troubleshooting.md).
- **You lost the admin link.** Run `netoryx admin-link`.
- **You need the list of options.** `sudo bash install.sh --help`.
