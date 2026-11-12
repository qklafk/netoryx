# Panels, locations and endpoints

Netoryx does not carry traffic itself. It creates and updates accounts on VPN panels and builds the subscription link
that clients import. The database is the source of truth: a background job converges every panel to it, so a panel that
was offline catches up when it returns.

Three concepts:

- **Panel**: one installation of 3x-ui, Remnawave and so on, with its credentials.
- **Location**: what the customer sees ("Germany", a flag). A location groups inbounds.
- **Endpoint**: one entry in the subscription for an inbound: the address and port the client actually connects to,
  plus optional overrides of the link (SNI, fingerprint and so on). One inbound can have several endpoints (direct,
  through a relay, through a CDN).

Everything below is done in the web admin: **Panels**, **Locations**, **Endpoints** (`https://<domain>/admin`). The
same operations exist in the admin API under `/api/admin`.

## Adding a panel

**Panels > Add**. Choose the kind, enter the URL and credentials, keep "test connection" on. The panel is checked before
it is saved. Credentials are stored encrypted with `SECRET_KEY`.

| Kind | URL | Credentials | Notes |
|---|---|---|---|
| 3x-ui (`xui`) | full panel URL including the secret web base path, e.g. `https://host:2053/abc123` | username and password, or a Bearer API token (3x-ui 3.1+, preferred) | Both API generations are supported. Existing inbounds are imported on add. For a panel on the same server use `http://host.docker.internal:2053/<path>` with TLS verification off. |
| Remnawave (`remnawave`) | `https://panel.example.com`, or `http://remnawave:3000` when it shares a Docker network | API token | 3.0+. Options: `squad_uuids` (list; empty = all), `username_prefix` (default `tg_`), `direct` (default true; adds `X-Forwarded-*` headers when calling the container directly). |
| Marzban (`marzban`) | panel URL | admin username and password | Existing users can be imported. |
| Marzneshin (`marzneshin`) | panel URL | admin username and password | Marzneshin keeps deleted users (soft delete), so the same username cannot be created again. Client links are taken from the panel's subscription in the "links" format. |
| PasarGuard (`pasarguard`) | panel URL | API key with user and system read permissions | Client links are taken from the panel's subscription in the "links" format. |
| Hiddify (`hiddify`) | panel URL **including** the configured admin proxy path | the API key | Hiddify Manager API v2. |

Common `options` (JSON): `headers` (extra HTTP headers for every request, for example a reverse-proxy `X-Api-Key`) and
`cookies` (for a panel behind a cookie-protected proxy).

For 3x-ui, **Refresh inbounds** re-reads the panel's inbounds: new ones are imported (you choose whether they become
active, which location they get and which address clients connect to), and removed ones are deactivated. Panel health is checked every few minutes
(`HEALTH_CHECK_INTERVAL_MIN`), and admins are alerted after `HEALTH_FAILURES_BEFORE_ALERT` failures.

**Importing existing users.** The import preview lists a panel's existing accounts (expiry, limits, traffic) and adopts
them without changing their credentials or counters, so current links keep working. Adopted accounts are bound to
Telegram users through claim links (valid 30 days).

## Locations and endpoints

1. Create a location (name, flag).
2. Put each active inbound in a location and add an endpoint for it. The address is the host customers connect to.
3. Check the link preview in the endpoint form: it shows exactly what a client will receive.

Endpoint kinds:

| Kind | Use |
|---|---|
| `direct` | The inbound's own address and port. |
| `relay` | A port forward or L4 relay in front of the real server: only the address and port differ. Use it for a cascade (entry server in one country, exit in another) or to give networks with restricted access another route to the same inbound. |
| `cdn` | TLS/XHTTP/WebSocket through a CDN; SNI, host, path, ALPN and so on are overridden. |

Endpoint profiles: `standard` is what everyone gets. `whitelist` entries are the "LTE" profile for mobile networks that
only let approved hosts through (typically a relay inside the country whose address is on the operator's allow-list,
forwarding to your real server). They get an "LTE" suffix in the name and, for v2RayTun, a `network-filter` so they show up on mobile data
only. By default they are in the combined subscription; set `SUB_WHITELIST_SEPARATE_URL` to serve them only at
`/sub/<token>/wl`. A relay endpoint that fails its health check is hidden from subscriptions automatically and comes back
when it recovers.

`overrides` is a JSON object applied to the generated link: `security`, `sni`, `host`, `path`, `alpn`, `fp`, `mode`,
`extra`, `serviceName` and `support-x25519mlkem768`. An override beats the defaults (including
`REALITY_COMPACT_HELLO`).

## Reality on port 443, with a name of your own

Many networks drop or throttle non-standard ports, so the Docker stack puts everything on 443. The `edge` HAProxy reads
the TLS server name of each connection and passes it on untouched:

- names listed in `deploy/haproxy/sni.map` go to `reality` (the 3x-ui inbound on `REALITY_PORT`) or `tgproxy` (the
  Telegram proxy);
- every other name goes to Caddy (web admin, Mini App, subscriptions).

The most robust Reality setup is "self-steal": the inbound borrows a real site on your own server, so the name, the
certificate and the IP address all match.

1. Caddy already serves `vpn.<domain>` on `127.0.0.1:8444` with a real certificate.
2. In **Panels**, create a Reality inbound on the 3x-ui panel (the form creates fresh X25519 keys): port = your
   `REALITY_PORT` (set in `.env`, default `8443`; it only needs to be free on the host), SNI = `vpn.<domain>`, destination =
   `127.0.0.1:8444`.
3. Add `vpn.<domain> reality` to `deploy/haproxy/sni.map` and restart `edge` (`docker compose restart edge`: this
   drops open VPN connections, so do it at a quiet time).
4. Create an endpoint for the inbound with the address `<domain>` and port `443`.

Without self-steal, pick a popular external site as the Reality SNI (the form offers a list and can test that your
server reaches it) and list that name in `sni.map` instead. Visitors of `vpn.<domain>` in a browser just see a plain page.

These steps are documented so you can repeat them for another server or change the name; see
[installation.md](installation.md) for what the installer sets up on its own.

## Several servers

Add each server's panel, put its inbounds in a location, and every plan can use all of them. The subscription link then
carries one entry per active endpoint (the combined link), and the bot also offers per-server profiles and
per-server subscription URLs (`SUB_SHOW_SERVER_PROFILES`, `SUB_SHOW_SERVER_SUBSCRIPTIONS`).

## Restarts

Restarting `edge`, `caddy` or the 3x-ui container drops open connections. Most clients reconnect on their own, some need
the VPN toggled off and on. Redeploying only the application does not touch VPN sessions. Changing `ADBLOCK_ENABLED` or
the YouTube exit restarts Xray once.
