# Backups

Netoryx backs up its own database: users, subscriptions, payments, settings, texts. It does not back up the VPN panels
(3x-ui and so on); back those up with their own tools. Panel accounts can be rebuilt from the Netoryx database, because
the database is the source of truth.

## Automatic backups

On by default (`BACKUP_ENABLED=true`), daily at `BACKUP_TIME` (04:00, in `TZ`). Each backup is written to `BACKUP_DIR`
(`data/backups`, that is `/app/data/backups` in the `app-data` volume of the app container); the newest `BACKUP_KEEP`
files (7) are kept, and the file is sent to the Telegram chat `BACKUP_CHAT_ID` (default: the first admin).

Set `BACKUP_PASSWORD` to encrypt backups (scrypt key derivation, AES-256-GCM). **Do this**: a backup holds customer data
and encrypted secrets, and it travels through Telegram. Keep the password apart from the backups; a lost password means
an unreadable backup.

Two formats, both gzip-compressed:

- logical (`.jsonl.gz`): every table as JSON lines. Works on SQLite, PostgreSQL and MySQL and restores into any of them,
  so it is also a way to move between databases;
- SQLite snapshot (`.sqlite.gz`): a fast consistent copy for the SQLite setup.

Encrypted files end in `.enc`.

## Backup now

```bash
sudo netoryx backup
```

or **Backups** in the web admin.

## Restore

Restoring **replaces all current data**. You need the backup file and, if it is encrypted, its password.

```bash
cd /opt/netoryx
docker compose cp /path/to/<backup file> app:/app/data/backups/
docker compose stop app
docker compose run --rm app /app/.venv/bin/python -m app restore /app/data/backups/<backup file> --yes --password '<password>'
docker compose up -d app
```

`--password` can be omitted when `BACKUP_PASSWORD` is set in `.env`. The restore runs in one transaction; afterwards the
application converges the panels to the restored state. On a new server, install first, then restore.

## Good habits

- Copy backups off the server (the Telegram copy counts; the local volume does not survive a lost server).
- Take a manual backup before every upgrade.
- Test a restore on a scratch machine once.
- Keep a copy of `/opt/netoryx/.env`: it holds `SECRET_KEY` and the database password.
