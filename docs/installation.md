# Installation

## One-line install

New to servers? Start with the [beginner's guide](beginners.md).

On a fresh server, as root:

```bash
curl -fsSL https://raw.githubusercontent.com/qklafk/netoryx/main/install.sh | sudo bash
```

The installer asks for the bot token (from @BotFather) and your Telegram id, installs Docker, builds the stack in
`/opt/netoryx`, waits for HTTPS and prints the one-time web admin link. Without a domain it uses
`<ip-with-dashes>.sslip.io`, which needs no DNS setup. Non-interactive form:

```bash
sudo bash install.sh --bot-token <token> --admin-id <telegram id> [--domain <name>]
  [--panel xui|remnawave|marzban|pasarguard|marzneshin|none] [--with-tgproxy] [--with-socks] [--socks-port 2080] [--yes]
```

| Option | Meaning |
| --- | --- |
| `--panel NAME` | VPN panel to install, default `xui`: `xui`, `remnawave`, `marzban`, `pasarguard`, `marzneshin` or `none` (see [Choosing a panel](#choosing-a-panel)). Each one gets random administrator credentials and a VLESS + Reality entry point that shares port 443 |
| `--with-xui` | same as `--panel xui` |
| `--with-tgproxy` | install the MTProto proxy for Telegram (telemt), also served on 443 |
| `--with-shadowsocks` | add a Shadowsocks-2022 VPN inbound on `--ss-port` (default 8388, tcp+udp); 3x-ui only. Shadowsocks is not TLS, so it cannot share 443 and needs a port of its own; treat it as a fallback protocol, because some networks filter fully encrypted traffic. Skipped with a notice if this version cannot create it |
| `--with-socks` | create a SOCKS5 proxy for Telegram on `--socks-port` (default 2080); 3x-ui only |
| `--tg-domain` | TLS name the MTProto proxy imitates (default `petrovich.ru`) |
| `--brand-name` | name of your service shown to customers (default `VPN`) |
| `--lang ru\|en` | language of the installer's messages |
| `--no-firewall` | do not touch ufw / firewalld |
| `--force-os` | try an operating system that is not on the list below, at your own risk |
| `--update` | fetch the latest release and restart only the application |
| `--dry-run` | print the plan and change nothing |

With a domain of your own, create A records for `<domain>` and (with `--with-xui`) `vpn.<domain>` pointing to the
server before running the installer; the installer warns when they do not resolve.

### What the installer asks

Interactive mode explains every question in your language (Russian when `LANG` starts with `ru`, English otherwise;
force it with `--lang ru|en`), checks the answer straight away and asks again if it is wrong. Enter accepts the value
in brackets; yes/no questions show `[Y/n]`.

1. Bot token: from @BotFather (`/newbot`), checked with Telegram. If Telegram cannot be reached the installer warns and goes on.
2. Your Telegram ID: the number @userinfobot replies with.
3. Domain: press Enter to use the free `<ip-with-dashes>.sslip.io` address. A domain that does not point to this server is refused, with an offer to use sslip.io instead.
4. Service name (`BRAND_NAME`, default `VPN`): the name your customers see in the bot, the Mini App and their apps. Change it later in the admin; `--brand-name` sets it without a question.
5. Which VPN panel: a numbered list with a one-line description of each (Enter = 3x-ui, the last entry is "no panel, I connect my own later"). Then, for 3x-ui, add SOCKS5 for Telegram: yes/no. Then install the MTProto proxy: yes/no.

The run is nine numbered steps, each ending with a check mark. If one fails, the installer names the step, the log
(`/var/log/netoryx-install.log`) and the command to run again; repeating is always safe. At the end a summary lists
the one-time admin link, the bot, the subscription address, where the credentials are stored (paths only), how to open
the panel through an SSH tunnel, the next steps and the `netoryx` commands. `--yes` turns every question off for
automation (values then come from the flags).

### Supported systems

Ubuntu 22.04, 24.04 and 26.04; Debian 12 and 13; AlmaLinux 9 and Rocky Linux 9; on x86_64 and aarch64. At least
2 vCPU, 2 GB RAM (a 2 GB swap file is created below 4 GB) and 10 GB of free disk. Other systems are refused unless
`--force-os` is given.

What differs per system:

- Packages: `apt` on Debian and Ubuntu, `dnf` on AlmaLinux and Rocky (`qrencode`, used for the terminal QR code, comes
  from EPEL there and is optional).
- Docker CE comes from Docker's official repository (`download.docker.com/linux/ubuntu`, `/debian`; Docker's
  CentOS repository on AlmaLinux and Rocky, which are binary compatible with it). The repository codename is read
  from `/etc/os-release`.
- Firewall: `ufw` on Debian and Ubuntu, `firewalld` on AlmaLinux and Rocky. SSH ports found in the sshd
  configuration (and the one you are connected through) always stay open, together with 80, 443 and the SOCKS5 port.
  Containers must reach the panel (3x-ui, host network) and the Reality backend port on the host: with ufw the range
  `172.16.0.0/12` is allowed to those two ports only; with firewalld that range is put into the `trusted` zone (it is
  matched by source address, so it works whichever zone Docker places its bridges in), and Docker is restarted after
  the rules change. Ports published by Docker bypass both firewalls by design; the stack publishes only 80 and 443.
- SELinux (AlmaLinux, Rocky): the bind mounts in `docker-compose.yml` carry the `:z` label, so the stack works with
  SELinux enforcing; nothing has to be disabled.

### Managing the installation

```bash
netoryx status | logs [service] | update | backup | admin-link | restart [service] | uninstall [--purge]
```

`netoryx update` fetches the new release and rebuilds only the application, so VPN connections are not interrupted.
It tells you when the edge, Caddy or the panel configuration changed and need a restart (`netoryx restart edge caddy`).
Running `install.sh` again on an existing installation upgrades or repairs it: generated secrets are kept, volumes are
never wiped. `netoryx uninstall` stops the stack and keeps the data; `--purge` also deletes volumes, code and secrets.

Generated credentials live in `/root/netoryx` (mode 0600): `xui-credentials` and `telemt-credentials`. The installer
log is `/var/log/netoryx-install.log`; it never contains secrets.

## Advanced: manual setup

The rest of this page describes the pieces the installer automates, for people who prefer to set things up by hand.

## Prerequisites

- A supported system (see above); Ubuntu Server 24.04 LTS x86_64 is the best tested.
- 2 vCPU, 2 GB RAM and 60 GB SSD for a small all-in-one installation.
- SSH access that remains available while firewall changes are planned.
- A domain and public HTTPS endpoint only for features that require them, such as Telegram Mini App and some payment
  webhooks.

## Planning modes

The installer treats these settings independently:

- Product profile: `personal` or `commercial`.
- Deployment role: `all-in-one`, `control-plane` or `vpn-node`.

`personal` does not impose a user or node limit. It only avoids enabling commercial features by default.
`commercial` is a preset for subscription operations; both profiles use the same application and database.

Run a read-only plan:

```bash
uv run python -m installer plan --profile personal --deployment-role all-in-one
```

Run a preflight for a node configuration:

```bash
uv run python -m installer preflight --profile commercial --deployment-role vpn-node --module proxy.mtproxy
```

The commands inspect local CPU, memory, free disk and selected port requirements. They emit a JSON plan and return a
non-zero status for unmet requirements. `plan` stores resumable, non-secret selection state in
`.installer-state.json`.

## Core and trusted modules

The minimal Core has no Telegram, web interface or commercial dependency. It stores only module lifecycle state in a
separate SQLite database by default:

```bash
uv run python -m app core status
uv run python -m app module list
uv run python -m app module install system.demo
uv run python -m app module configure system.demo --set label=example
uv run python -m app module enable system.demo
uv run python -m app module health system.demo
```

Only trusted modules packaged in the release are available at this stage. The manifest is declarative and cannot run
shell commands. Disabling or uninstalling a module preserves module data unless `--purge-data` is explicitly used.

## HTTPS-dependent modules

Telegram Mini App requires both `interface.telegram-bot` and a public HTTPS endpoint. Declare it only after an HTTPS
endpoint is available:

```bash
uv run python -m installer plan --profile commercial --deployment-role control-plane \
  --module interface.miniapp --public-https
```

Payment methods are evaluated individually in a later installation stage: a gateway that uses webhooks requires a
reachable endpoint, while an offline/manual flow may not.

## Security model for future apply operations

Future apply operations will require a reviewed plan, a backup before dangerous changes, health checks, resumable
state and rollback where technically possible. Release artifacts must be authenticated with a trusted signature or
public-key verification; a checksum downloaded from the same untrusted location is not proof of authenticity.

VPN nodes will receive only the minimum node runtime and selected network services. They will not receive the control
plane database, payment services or the full application backend.

## Docker Compose stack and network layout

`docker-compose.yml` runs the application, PostgreSQL, Caddy (automatic HTTPS) and `edge`, an HAProxy that owns the
only public TLS port, 443. HAProxy reads the server name of each TLS connection and passes it on untouched:

- names listed in `deploy/haproxy/sni.map` go to the VPN (`reality`, the panel's Reality inbound on `REALITY_PORT`) or
  to the Telegram proxy (`tgproxy`);
- every other name goes to Caddy, which serves the web admin, the Mini App and subscription links.

Keeping everything on 443 matters: some networks drop or slow down traffic on non-standard ports. Open only 22, 80 and 443 in the
firewall; panel ports stay closed and are reached through the Docker network or an SSH tunnel.

Copy `deploy/haproxy/sni.map.example` to `deploy/haproxy/sni.map` first (the installer writes this per-server file for
you). Optional services are enabled with profiles: `--profile xui` (3x-ui on the host network) and `--profile tgproxy`
(telemt). Set `DOMAIN`, `SECRET_KEY` and `POSTGRES_PASSWORD` in `.env`; without a domain, `<ip-with-dashes>.sslip.io`
works out of the box. The first certificate may take a minute or two to arrive.

### Reality on your own name

Point the Reality inbound at the built-in site `vpn.<DOMAIN>` (Caddy serves it on `127.0.0.1:8444`): use
`vpn.<DOMAIN>` as the server name and `127.0.0.1:8444` as the destination, and add `vpn.<DOMAIN> reality` to
`deploy/haproxy/sni.map`. The name, the certificate and the server address then match, which some networks check.

### Networks that drop large TLS handshakes

Some networks drop the second TCP segment of a large TLS ClientHello. A Chrome fingerprint with the post-quantum key
share produces such a hello, and the VPN then "connects" without passing traffic. `REALITY_COMPACT_HELLO` (on by
default) makes Reality links ask clients for a Firefox fingerprint without the post-quantum share, so the hello fits
into one segment. A per-endpoint override in the admin still takes precedence.

### Privacy

`CLIENT_IP_LOGGING=off` (default) keeps customer IP addresses out of logs and the database; rate limits still work in
memory. Device limits for 3x-ui are counted by the bot from the HWID that client apps send (`XUI_DEVICE_LIMIT=hwid`),
so the Xray access log, which would record IPs and visited sites, can stay off.

### Telegram proxies

- MTProto (`--profile tgproxy`, telemt): no public port of its own. List its `tls_domain` in
  `deploy/haproxy/sni.map` as `tgproxy` and keep `public_port = 443` in its config, so links point at 443.
- SOCKS5: create a 3x-ui `mixed` inbound with password authentication on a port of its own (SOCKS5 is not TLS and
  cannot share 443), open that port in the firewall, then mark the inbound as the Telegram SOCKS5 inbound in the
  admin and set the public host/port in the panel options. Every user gets a personal login and password.
  See [telegram-proxy.md](telegram-proxy.md).

### Ad and tracker blocking

`ADBLOCK_ENABLED` adds an Xray routing rule that sends `geosite:category-ads-all` to the blackhole outbound on every
3x-ui server. Turning it on or off restarts Xray, so active VPN sessions reconnect. Ads inside YouTube videos are
served from the same hosts as the videos and cannot be blocked this way.

### YouTube exit

`YOUTUBE_EXIT_ENABLED` together with `YOUTUBE_EXIT_LINK` (a `vless://`, `socks5://` or `ss://` share link of another
server) routes the domains in `YOUTUBE_EXIT_DOMAINS` (default `geosite:youtube`) through that server on every 3x-ui
panel, so YouTube leaves from a country where it shows no ads. Everything else exits as before. The bot adds an
outbound `youtube-exit`, a balancer with an observatory probe and one routing rule placed after the ad-blocking rule;
if the exit is unreachable, YouTube falls back to the panel's `direct` outbound. It also switches on domain sniffing
(http, tls, quic) on VLESS inbounds, which domain routing needs. Which countries get no ads changes over time, so the
method may stop working. Xray restarts once when the setting changes.

### Choosing a panel

| Panel | What the installer does | Status |
| --- | --- | --- |
| 3x-ui (`xui`, default) | `ghcr.io/mhsanaei/3x-ui` on the host network (profile `xui`), random login and web base path, Reality inbound created through its API | verified on a live server |
| Remnawave (`remnawave`) | `remnawave/backend:3.4.10` with PostgreSQL 18 and Valkey, node `remnawave/node:3.4.5`; registers the first administrator and an API token, creates a config profile with the Reality inbound, a node, a host and adds the inbound to the first internal squad | verified on a live server |
| Marzban (`marzban`) | `gozargah/marzban:v0.8.4` with a newer Xray core (the image's own core is too old for current Reality clients); administrator from the environment, Reality inbound written to the core configuration through the API, a host for it | verified on a live server |
| PasarGuard (`pasarguard`) | `pasarguard/panel:v5.6.0` and `pasarguard/node:v0.5.7`; the owner account and the node certificate are created automatically, then core configuration, group, node, host and an API key through the API | verified on a live server |
| Marzneshin (`marzneshin`) | `dawsh/marzneshin:v0.7.4` and `dawsh/marznode:v0.5.7` with a newer Xray core; administrator through `marzneshin-cli`, Xray configuration with the Reality inbound for the node, node, host and service through the API | verified on a live server |
| none (`none`) | installs only Netoryx; add your own panel in the web admin (Servers). Hiddify is connected this way: it takes over the whole server and port 443, so it cannot share a host with this stack | - |

All five panels were installed with this installer and verified end to end with a real client over port 443.

Except for 3x-ui, the panels run in the same Docker network as the stack instead of the host network used by their
own compose files, so nothing of them is published: the bot reaches the API by container name, HAProxy reaches the
Reality inbound by container name and Reality borrows the `vpn.<domain>` site of Caddy (`caddy:8444`). The files live
in `deploy/panels/<name>/`; `COMPOSE_FILE` in `.env` selects them, so every `docker compose` command (and `netoryx`)
sees the panel. Administrator login and password are in `/root/netoryx/<panel>-credentials`; the panel UI listens on
`127.0.0.1` only (open it through `ssh -L`, or publish it as described below). Image tags are pinned; update them in
`deploy/panels/<name>/compose.yml` on purpose.

Limits with other panels: SOCKS5 for Telegram and Shadowsocks-2022 need 3x-ui and are skipped with a notice; ad
blocking and the YouTube exit rewrite the 3x-ui Xray template and are 3x-ui only; the MTProto proxy (telemt) works with
any panel. The compact Reality client hello (`REALITY_COMPACT_HELLO`) is applied to 3x-ui links; for the other
panels the installer sets the `firefox` fingerprint on the host it creates. Additional inbounds on other ports need a
published port in the panel's compose file.

Shadowsocks-2022 (`--with-shadowsocks`) is optional and may be unreliable on some networks; VLESS Reality on 443 is the
recommended default.

Let's Encrypt issues at most 5 certificates per exact name per week. `netoryx uninstall --purge` deletes the
certificates, so reinstalling many times in a row can hit the limit and HTTPS will not come up until it resets. Use a
domain of your own or avoid repeated purges.

### Publishing the panel UI

Panel ports stay closed. To reach the panel UI from a browser, copy `deploy/caddy-sites/panel.caddy.example` to
`panel.caddy`, set the upstream for your panel (the file lists them) and recreate Caddy: the UI becomes available over
HTTPS at `https://panel.<DOMAIN>/` (3x-ui: followed by its web base path). Use a strong password and the panel's
two-factor authentication.

### Restarts

Redeploying only the application (`docker compose up -d --build --no-deps app`) does not interrupt VPN sessions.
Restarting `edge`, `caddy` or the panel drops open connections; most clients reconnect, some need the VPN toggled.
