# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub: open the repository's **Security** tab and choose **Report a
vulnerability** (GitHub Security Advisories). Do not open a public issue or discuss the problem in public channels until a
fix is released.

Include what you found, how to reproduce it, the affected version and, if you can, the impact. You can expect an
acknowledgement within a few days and a status update as the fix progresses. We will credit you in the release notes if you
wish.

## Supported versions

| Version | Supported |
|---|---|
| 0.9.x (beta) | yes, the latest release only |
| older | no |

While the project is in beta, fixes are shipped in a new release rather than backported.

## Scope

In scope: authentication and authorization (web admin, Mini App, web cabinet, API keys), payment crediting and webhook
verification, handling of secrets, panel credentials, injection and request-forgery problems, the installer.

Out of scope: vulnerabilities in third-party panels, payment providers or Telegram themselves (report those upstream), and
problems that need an already-compromised server or administrator account.

## Operating safely

- Keep `.env` and `SECRET_KEY` private and backed up; never commit them.
- Set `BACKUP_PASSWORD`, because backups hold customer data.
- Keep panel ports closed to the internet; expose only 80 and 443 (plus a SOCKS5 port if you enable it).
- Use a strong panel password with two-factor authentication.
- Update regularly ([docs/upgrading.md](docs/upgrading.md)).
