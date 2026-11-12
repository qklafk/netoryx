# Contributing

Thanks for helping. Bug reports, fixes, translations and new payment or panel integrations are all welcome.

## Development setup

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 20+ for the web frontend.

```bash
git clone https://github.com/qklafk/netoryx.git
cd netoryx
uv sync --all-extras
cp .env.example .env        # set BOT_TOKEN (a test bot) and ADMIN_IDS
uv run python -m app check --offline
uv run python -m app        # bot + web + background jobs (SQLite by default)
```

Frontend (React, Vite, TypeScript), in `web/`:

```bash
cd web
npm ci
npm run dev          # web admin on 127.0.0.1 with hot reload
npm run dev:app      # Mini App on port 5174
npm run typecheck && npm test
npm run build        # writes web/dist/admin and web/dist/app
```

## Checks before a pull request

```bash
uv run python -m pytest -q
uv run python -m ruff check app installer tests
uv run python -m ruff format app installer tests
uv run python -m mypy app installer
```

The offline suite takes a few minutes. Tests that need Docker or the network are skipped unless you opt in with
`XUI_INTEGRATION=1`, `REMNAWAVE_INTEGRATION=1` or `CORE_DB_INTEGRATION=1`. CI runs the same checks.

## Code conventions

- Line length 120, type hints everywhere, mypy-clean, short docstrings, English comments.
- Money is integer kopecks. Every balance change goes through `app/services/wallet.py`.
- Timestamps are timezone-aware UTC.
- SQL must stay portable across SQLite, PostgreSQL and MySQL: no dialect-specific upserts; "insert if absent" is a
  savepoint plus `IntegrityError`.
- Functions that take a `session` never commit; functions that take a `session_factory` own short transactions.
- **No network calls (Telegram, panels, payment APIs) while a database transaction is open.**
- The database is the source of truth for panel state; writes to panels are idempotent.
- Payments are stored before a link is shown and credited idempotently; webhooks are never trusted without a signature or
  IP check and, where possible, a re-fetch from the provider.
- No secrets in logs or the audit trail. `aiohttp` is the only HTTP client.
- New behavior needs a test. A bug fix needs a test that fails without the fix.
- New user-visible strings go into the locale files (at least Russian and English).
- Schema changes need an Alembic migration that works on all three databases.

## Commits and pull requests

Small, focused commits with an imperative subject line ("Fix renewal of unlimited subscriptions"). Explain the why in the
body when it is not obvious. Describe how you tested the change in the pull request. Do not commit secrets, real tokens
or server addresses.

## License

Netoryx is licensed under the GNU AGPL-3.0-or-later. The code stays AGPL; contributions are accepted under the
[Contributor License Agreement](CLA.md).

### Contributor License Agreement (CLA)

Every contributor signs the CLA once. Signing works like this:

1. Open a pull request. The CLA bot comments on it with a link to [CLA.md](CLA.md).
2. Read it, then reply to the bot with exactly: `I have read the CLA Document and I hereby sign the CLA`
3. The bot records your GitHub username and marks the check as passed. The signature covers your later pull requests.
   Comment `recheck` if the check does not update.

Why a CLA: it lets the maintainer keep the option of offering Netoryx under a commercial license (for example to
companies that cannot comply with the AGPL) while the public code stays AGPL. You keep the copyright in your
contribution and may use it elsewhere. If you cannot agree to the CLA, open an issue to discuss before writing code.

## Pull request checklist

- [ ] Tests added or updated; pytest, ruff and mypy pass (see above)
- [ ] User-visible strings are in the locale files (Russian and English at least)
- [ ] Alembic migration for schema changes, working on SQLite, PostgreSQL and MySQL
- [ ] No secrets, tokens, server addresses or customer data anywhere in the change
- [ ] Docs updated when behavior or settings change
- [ ] CLA signed

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Reporting problems

Use GitHub Issues for bugs and ideas. For security problems follow [SECURITY.md](SECURITY.md) instead.
