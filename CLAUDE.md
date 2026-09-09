# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

pyLibrus scrapes new messages from the Librus Synergia parent gradebook portal and forwards
them by email or webhook (with optional S3-hosted attachment links). It's meant to run
unattended from cron (see `Procfile`: `*/5 * * * *`).

The entire application is one file: `src/pylibrus/pylibrus.py` (~1050 lines). There are no
other modules and no test suite.

## Commands

Dependency management and running is via `uv`.

```bash
uv sync                                   # install dependencies (incl. dev deps)
uv run src/pylibrus/pylibrus.py --help    # run the CLI
uv run src/pylibrus/pylibrus.py --workdir <dir> --config pylibrus.ini
uv run src/pylibrus/pylibrus.py --test-notify   # send a test notification for the first configured user, no scraping
uv run ruff check .                       # lint
uv run ruff format .                      # format
```

There is no test suite (`--test-notify` is the closest thing to an integration smoke test —
it exercises `LibrusNotifier.notify()` for the first user in the config without touching the
Librus site). When adding tests, there's no existing convention to follow.

`ruff` config lives in `pyproject.toml` (line length 120, py312 target, double quotes).

## Configuration

Two independent ways to configure, chosen in `read_pylibrus_config()`:
- **INI file** (`<workdir>/<config>`, default `pylibrus.ini`) — supports multiple Librus users,
  each in a `[user:<Name>]` section. See `pylibrus.ini.example` for all keys.
- **Environment variables** — fallback used only when the INI file doesn't exist, and only
  supports a single user (`LIBRUS_USER`, `LIBRUS_PASS`, `LIBRUS_NAME`, etc.)

Every settings dataclass (`PyLibrusConfig`, `EmailNotify`, `WebhookNotify`, `LibrusUser`) has a
matching pair of `from_config()` / `from_env()` classmethods — keep both in sync when adding a
new setting.

## Architecture

Per configured Librus user, `handle_user()` wires together two independently-scoped context
managers:

- **`LibrusScraper`** — logs into Librus Synergia (a multi-step OAuth dance reverse-engineered
  from the browser flow — the comments in `__enter__` explain *why* each redirect/referer is
  needed, don't simplify without understanding it) and scrapes the inbox by parsing HTML with
  BeautifulSoup (there is no real API; page structure is brittle and tied to Librus's markup).
  Session cookies are cached to a JSON file (`pylibrus_cookies.json` by default) and reused
  across runs to avoid re-login on every cron tick; `are_cookies_valid()` checks first.
- **`LibrusNotifier`** — owns a SQLAlchemy/SQLite session (one DB file per user, `db_name` in
  config) recording every seen `Msg`/`Attachment` so messages are never processed twice, then
  sends new ones out via email (`smtplib`) or webhook (`requests.post`, Slack-style `{"text":
  ...}` payload).

`handle_user()` decides per-message whether to notify based on `send_message` config
(`"unread"` vs `"unsent"`) and `max_age_of_sending_msg_days`, then marks `msg.email_sent`.

### Attachments

Attachment handling branches on notification type and `webhook_attachments_source`:
- Email always fetches attachment bytes and embeds them in the MIME message.
- Webhook with `webhook_attachments_source=librus_link` never downloads content — it just links
  back to the (session-authenticated) Librus download URL.
- Webhook with `webhook_attachments_source=s3://<bucket>/<prefix>` downloads content and
  re-uploads it to S3 (`S3AttachmentStorage`), then links a pre-signed URL
  (`LINK_EXPIRE_DURATION` = 7 days, the S3 maximum). Uploads are keyed by a hash of the message
  path so re-runs reuse the same S3 object (`attachment.s3_key` persisted in the DB) instead of
  re-uploading. Falls back to the plain Librus link on any S3 failure.

`fetch_attachment_content` in `handle_user()` is the single place deciding whether attachment
bytes need to be downloaded at all — keep it in sync when adding a new attachment delivery mode.

### Database

`Msg`/`Attachment` are SQLAlchemy declarative models (`Base`, shared across all user DBs but one
SQLite file per user). Schema changes must be added to `LibrusNotifier._migrate_attachment_table()`
(a hand-rolled additive migration run at startup) since there's no Alembic — new nullable columns
on `Attachment`/`Msg` need an explicit `ALTER TABLE` there or existing users' DBs won't pick them up.
