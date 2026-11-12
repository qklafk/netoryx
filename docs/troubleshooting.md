# Troubleshooting

Russian version: [ru/troubleshooting.md](ru/troubleshooting.md).

## First look

```bash
netoryx status                 # containers and health
netoryx logs app               # the application (bot, web, jobs)
netoryx logs edge              # the HAProxy on port 443
netoryx logs caddy             # HTTPS and the website
curl -s https://<domain>/healthz
```

`/healthz` returns JSON such as `{"status":"ok","db":true,"bot":true,"panels":{"total":1,"healthy":1},"uptime_sec":1234}`:

| `status` | Meaning |
|---|---|
| `ok` | Database reachable, bot connected, every active panel healthy. |
| `degraded` | The app runs, but the bot has no username yet (token or Telegram connectivity) or at least one panel is failing its checks. Look at **Panels** in the admin. |
| `error` (HTTP 503) | The database is unreachable. |

The healthz endpoint exposes counts only, never secrets, and is not written to the access log.

## The VPN "connects" but there is no internet

Symptoms: the client shows connected, the connection test times out, and the server sees a TCP handshake but no data.

**Cause 1: a large TLS ClientHello is cut.** Some networks (seen on Russian ISPs) drop the second TCP segment of a large
TLS hello. A Chrome fingerprint with the post-quantum key share (`X25519MLKEM768`, about 1.8 KB) is exactly such a hello.
Fix: keep `REALITY_COMPACT_HELLO=true` (the default), which makes Reality links use a Firefox fingerprint without the
post-quantum share. **Links already imported into the app keep the old parameters**, so on each device delete the
profile and re-import the subscription (or press the subscription refresh while *not* connected). A manual per-endpoint
override in the admin (`fp`, `support-x25519mlkem768`) takes precedence over the setting.

**Cause 2: non-standard ports are not reachable.** Home and mobile ISPs often drop or throttle everything except 443. Put the
VPN, the website and the Telegram proxy on 443 behind the `edge` HAProxy (the default Docker stack does this; see
[panels.md](panels.md)). Be careful testing from your own PC: some networks answer TCP connects on any port through a
transparent proxy, so "port open" tests prove nothing. Test with a real client.

**Cause 3: the client was imported before a server change.** After you change an endpoint, inbound, port or SNI, clients
must refresh the subscription. If a profile was imported once and never refreshed, delete and re-import it.

**Cause 4: the client kept a dead connection after a server restart.** Restarting `edge`, `caddy` or the 3x-ui container
drops open connections; some apps do not reconnect by themselves. Toggle the VPN off and on. Application-only updates
(`netoryx update`) do not do this.

## Mobile internet only reaches approved hosts

Some mobile networks only allow connections to approved hosts. Give those customers a path through such a host:
add a relay endpoint (an L4 forward on an approved address pointing at your real server) with the `whitelist` ("LTE")
profile; see [panels.md](panels.md). Relay endpoints that fail their health check are hidden from subscriptions
automatically.

## Telegram proxy does not connect

- MTProto on 443 needs its `tls_domain` listed in `deploy/haproxy/sni.map` as `tgproxy` and `public_port = 443` in the
  proxy config; restart `edge` after changing the map.
- SOCKS5 cannot share 443. Open its own port in the firewall and set `tg_socks_host` / `tg_socks_port` in the panel
  options if the server is behind NAT. Details: [telegram-proxy.md](telegram-proxy.md).
- Remember the networks above: a non-443 port may simply be filtered for that customer.

## YouTube still shows ads

`ADBLOCK_ENABLED` blocks ad and tracker domains, but ads inside YouTube videos come from the same hosts as the videos and
cannot be separated by domain. The optional `YOUTUBE_EXIT_*` settings route YouTube through an exit server in a country
where YouTube shows no ads; this is best-effort and can stop working when YouTube changes. See
[configuration.md](configuration.md).

## The subscription does not update in the app

- Apps refresh on their own interval (`SUB_UPDATE_INTERVAL_HOURS`, 12 by default); pull-to-refresh forces it.
- Some apps fetch the subscription **outside** the tunnel. If the customer's network filters your domain, the refresh
  fails while the VPN works. Have the app update "through the proxy" if it has that option, or refresh while disconnected
  on a different network.
- Check `https://<domain>/sub/<token>` in a browser: a valid token returns the profile; a deleted or expired subscription
  does not. If the customer has an old copy of the link, ask them to open their subscription in the bot again.
- Expired subscriptions stop updating by design.

## "Device limit reached"

Devices are counted by the `x-hwid` header the app sends. Customers can reset devices in the bot (limited by
`DEVICE_RESET_COOLDOWN_MIN`). Apps that send no HWID are served unless `HWID_REQUIRED=true`. Reinstalling an app creates a
new HWID, which counts as a new device until the old one is forgotten (`HWID_DEVICE_TTL_DAYS`).

## HTTPS or the first start

- The first certificate can take a minute or two (Let's Encrypt, sometimes one retry after a "certificate not found").
  `netoryx logs caddy` shows progress. A domain must point to the server's IP and ports 80 and 443 must be reachable.
- `sslip.io` names work without DNS but are shared; Let's Encrypt rate limits can apply if you reinstall repeatedly.
- HTTPS does not come up after several reinstalls: Let's Encrypt issues at most 5 certificates per exact name per week, and `netoryx uninstall --purge` deletes the certificates. Wait for the limit to reset, or use a domain of your own and avoid repeated purges.
- `fwupd.service` failing during `apt upgrade` on a VPS is harmless noise.

## Bot does not answer

- `netoryx logs app`: look for "Unauthorized" (wrong `BOT_TOKEN`) or network errors. In regions where Telegram is hard to
  reach from the server, set `BOT_PROXY_URL` (SOCKS5/HTTP proxy for the bot itself) or `TELEGRAM_API_URL`.
- Two copies of the bot with the same token (an old test install) fight for updates. Stop one.

## A panel on the same server is unreachable from the bot

The app reaches a host-network panel (3x-ui) as `host.docker.internal`. With a default-deny firewall, allow the Docker
networks to reach the panel port only: `ufw allow from 172.16.0.0/12 to any port 2053` (use your panel port). Never open
the panel port to the internet. Ports that Docker publishes bypass `ufw`, so the stack publishes only 80 and 443.

## Payment was made but not credited

See [payments.md](payments.md#testing-a-payment). In short: check the webhook URL in the provider cabinet, then **Payments**
in the admin, then `netoryx logs app`. The reconciler credits paid invoices within `PAYMENT_RECONCILE_INTERVAL_SEC`
even if a webhook never arrives. An admin can also credit a payment manually; the action is idempotent.

## Reading logs safely

Logs contain no secrets and, with the default `CLIENT_IP_LOGGING=off`, no customer IP addresses. Set `LOG_LEVEL=DEBUG` in
`.env` only for a short investigation; Docker rotates logs (10 MB x 3 files per container).
