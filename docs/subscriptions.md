# Subscription formats

Русская версия: [ru/subscriptions.md](ru/subscriptions.md).

The personal link `/sub/<token>` (and the per-location `/sub/<token>/loc/<id>`) answers in one of two formats,
depending on the app that asks:

| App | Format |
|---|---|
| Happ, v2RayTun | **JSON subscription**: an array of complete Xray configs, one per server, plus an "Auto" config |
| Everything else (INCY, Hiddify, Streisand, v2rayNG, ...) | The usual base64 list of share links |

The apps are recognized by their `User-Agent`. Both formats come from the same data, so servers, names, Reality
parameters and the response headers (profile title, traffic usage, update interval, announcement, device limit)
are identical. An expired or suspended subscription yields a single placeholder entry in either format.

## What is in the JSON

Every config has local SOCKS (`127.0.0.1:10808`) and HTTP (`127.0.0.1:10809`) inbounds with traffic sniffing, the
server as the `proxy` outbound, `direct` and `block` outbounds, routing and DNS. The entry name is the config's
`remarks`.

With two or more servers the first config is "Auto" (`<brand name> · Auto`): all servers sit behind a `leastPing`
balancer fed by a burst observatory that probes `https://www.google.com/generate_204` every ten minutes, and the
first server is the fallback. With one server there is no Auto entry.

Routing, first match wins:

1. DNS queries (to the domestic resolver directly, otherwise through the VPN);
2. domains from `SUB_JSON_PROXY_DOMAINS` through the VPN;
3. private and local network ranges directly;
4. Russian domains directly (`.ru`, `.su`, `.рф` and a short list of large services on other domains, plus
   `SUB_JSON_DIRECT_DOMAINS`);
5. everything else through the VPN (or the Auto balancer).

Names that match a direct rule are resolved by `77.88.8.8`, everything else by `1.1.1.1` through the VPN. Only
explicit domain rules are used, no geo data files, so the result does not depend on what the app ships.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `SUB_JSON_ENABLED` | `true` | Serve JSON to Happ and v2RayTun. `false`: everybody gets the link list. |
| `SUB_JSON_AUTO` | `true` | Add the Auto config when there are two or more servers. |
| `SUB_JSON_DIRECT_RU` | `true` | Russian domains connect directly. `false`: everything goes through the VPN and a single DNS is used. |
| `SUB_JSON_DIRECT_DOMAINS` | empty | Extra domains that connect directly. Comma or newline separated. |
| `SUB_JSON_PROXY_DOMAINS` | empty | Domains that always use the VPN even when a direct rule matches, for example a Russian site that is unreachable from Russia. |

Domain entries are `example.com` (the domain and its subdomains) or carry an Xray prefix: `full:host.example`,
`keyword:...`, `regexp:...`. They are also available in the web admin under the subscription link settings.

## Notes

- The apps pass an imported JSON config to Xray unchanged, so their own routing profile does not apply to it; the
  rules above are the only ones.
- Entries that Xray cannot express exactly (for example TCP HTTP obfuscation) stay in the base64 list only.
- Reality links keep the compact hello (`REALITY_COMPACT_HELLO`) through the Firefox fingerprint.
- Sources: [Happ](https://www.happ.su/main/dev-docs/examples-of-links-and-parameters),
  [Xray routing](https://xtls.github.io/en/config/routing.html), [Xray DNS](https://xtls.github.io/en/config/dns.html).
