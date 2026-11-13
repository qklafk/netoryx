# Backups

Netoryx has two kinds of backups:

- **database backups** (this page, below): users, subscriptions, payments, settings, texts. Automatic, small, sent to
  Telegram. Panel accounts can be rebuilt from the Netoryx database, because the database is the source of truth;
- **a full server backup** (`netoryx backup --full`, see [Full server backup](#full-server-backup-and-moving-to-another-server)):
  everything an installation made by the installer needs, including the VPN panel's data, certificates and secrets.
  Use it before risky changes and to move to another server.

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

## Full server backup and moving to another server

```bash
sudo netoryx backup --full            # writes /root/netoryx-backups/netoryx-full-<date>.tar.gz
sudo netoryx backup --full /mnt/disk  # or into another directory
```

The archive holds the database and every data volume of the installation (the VPN panel with its keys and clients,
certificates), `.env`, the server's own files under `deploy/` (Caddy sites, the HAProxy routing table, panel certificates)
and the credentials in `/root/netoryx`. The code is not included: it comes from the repository. The services stop for a
moment while the volumes are copied, so open VPN connections drop and reconnect. `netoryx uninstall --purge` does not
delete `/root/netoryx-backups`.

The archive is not encrypted and holds passwords and keys: keep it private (`chmod 600` is set) and copy it off the server,
for example `scp root@<server>:/root/netoryx-backups/netoryx-full-*.tar.gz .`.

To restore, on the same server or on a new one:

1. On a new server, install Netoryx with the **same panel** as the old one (`--panel ...`); the answers do not matter, the
   restore replaces them.
2. Copy the archive to the server and run `sudo netoryx restore /root/netoryx-full-<date>.tar.gz`. It checks the archive
   (only the expected files, the same panel), asks for `yes` and puts everything back; `--yes` skips the question.
3. If the server's address changed, point the domain's DNS record to it. Customers' links keep working, because the
   domain, the Reality keys and the client IDs are the same.

If the backup was made by a newer version than the installed one, run `sudo netoryx update` after the restore.

## Good habits

- Copy backups off the server (the Telegram copy counts; the local volume does not survive a lost server).
- Take a manual backup before every upgrade, and a full one (`netoryx backup --full`) before moving or reinstalling.
- Test a restore on a scratch machine once.
- Keep a copy of `/opt/netoryx/.env`: it holds `SECRET_KEY` and the database password.
