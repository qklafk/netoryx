# Web cabinet

Русская версия: [ru/web-cabinet.md](ru/web-cabinet.md).

The web cabinet is the customer area in an ordinary browser: the same interface as the Telegram Mini App (subscription,
connection, plans, payment, balance, referrals, support), but without Telegram. It is for when Telegram is unavailable or
unreachable. The cabinet address is `https://<your domain>/cabinet/`.

## Requirements

- `PUBLIC_BASE_URL` set to the HTTPS address of your domain (the session cookie is issued with the `Secure` flag).
- The cabinet turned on: `CABINET_ENABLED=true` (or in the web admin: Settings, "Web cabinet"). It is off by default.
- At least one login method enabled (below). If you run behind a reverse proxy, list it in `TRUSTED_PROXIES`, otherwise
  login rate limits are counted per proxy address.

The return address after payment is chosen at start-up, so restart the application after enabling the cabinet; payment
providers will then send customers back to a neutral `/pay/return` page with links to the cabinet and the bot. Telegram
Stars payment is not available in the cabinet (it only works inside Telegram).

## Login methods

Each method is enabled separately.

### 1. Link from the bot (`CABINET_LOGIN_BOT`, on by default)

The bot profile shows a "Web cabinet" button (also the `/cabinet` command; in the Mini App: "Open in browser"). The bot
issues a one-time link `https://<domain>/cabinet/login/<token>` valid for 10 minutes. This works as long as the customer
can open the bot at least once (for example through a VPN or a proxy).

### 2. Telegram Login Widget (`CABINET_LOGIN_TELEGRAM`)

The customer presses "Log in with Telegram" on the login page. To make it work:

1. Open [@BotFather](https://t.me/BotFather), send `/setdomain`, choose your bot and enter the domain (for example
   `vpn.example.com`), the same one as in `PUBLIC_BASE_URL`.
2. Set `CABINET_LOGIN_TELEGRAM=true`.

The data signature is checked against the bot token (HMAC-SHA256); data older than 15 minutes and replayed data are
rejected. Telegram itself must be reachable for the customer at the moment of login.

### 3. E-mail link (`CABINET_LOGIN_EMAIL`)

The customer binds an address once in the cabinet (Profile, Web cabinet, "Login e-mail") and confirms it from the e-mail
link. After that they enter the address on the login page and receive a one-time link. The server's answer does not
depend on whether the address is known; requests are rate-limited per address and per IP.

An SMTP server is required:

| Setting | Value |
| --- | --- |
| `SMTP_HOST`, `SMTP_PORT` | server and port (587 for STARTTLS, 465 for SSL) |
| `SMTP_SECURITY` | `starttls`, `ssl` or `none` |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | credentials, if the server needs them |
| `SMTP_FROM` | sender address, for example `VPN <no-reply@example.com>` |

Set up SPF and DKIM for the sender domain so the messages do not land in spam.

## Sessions and security

- A session lasts `CABINET_SESSION_DAYS` days (30 by default) and lives in an `HttpOnly`, `SameSite=Lax`, `Secure`
  cookie. Only a hash of the token is stored in the database.
- State-changing requests need an `X-CSRF-Token` header and an allowed `Origin`.
- "Log out" ends the current session; "Log out everywhere" ends all of the customer's sessions.
- A banned user cannot log in and their open sessions stop working. In personal mode only members and admins can log in.
- Expired sessions and links are removed automatically.
