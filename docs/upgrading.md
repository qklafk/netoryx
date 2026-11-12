# Upgrading

Netoryx uses semantic versioning with a pre-release tag while in beta (`0.9.0-beta`). Read [CHANGELOG.md](../CHANGELOG.md)
before upgrading, especially between minor versions.

## Standard upgrade

```bash
sudo netoryx backup
sudo netoryx update
```

`netoryx update` fetches the new release into `/opt/netoryx` and rebuilds and restarts **only the application
container** (`docker compose up -d --build --no-deps app`). Database migrations run automatically at start
(`AUTO_MIGRATE=true`). VPN sessions are not interrupted, because the VPN, `edge` and `caddy` are not touched. The rebuild
takes a couple of minutes on a small server; the bot is unavailable for a few seconds while the new container starts.

If a release changes the `edge`, `caddy`, panel or Telegram proxy containers (the update tells you), restart them
deliberately at a quiet hour, one at a time, for example `docker compose up -d --no-deps edge`. That drops open
connections; most clients reconnect, some need the VPN toggled off and on.

## Manual upgrade

```bash
cd /opt/netoryx
git pull            # or unpack the new release over the directory
docker compose up -d --build --no-deps app
docker compose logs -f app
```

Your `.env`, `deploy/haproxy/sni.map` and `deploy/caddy-sites/*.caddy` are per-server files; compare `.env.example` with
your `.env` for new options.

## Checking after an upgrade

```bash
netoryx status
curl -s https://<domain>/healthz
```

In the admin, **System** shows the version and background jobs, and every panel under **Panels** should be healthy.

## Rolling back

Migrations are not reversed automatically. To go back, restore the pre-upgrade backup ([backups.md](backups.md)) and check
out the previous release. This is why the backup comes first.

## Moving databases

SQLite to PostgreSQL (or the reverse) goes through a logical backup: take a backup, point `DATABASE_URL` at the new empty
database and restore ([backups.md](backups.md)).
