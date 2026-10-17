# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

pyLibrus scrapes new messages (and, optionally, announcements) from the Librus Synergia parent
gradebook portal and forwards them by email or webhook (with optional S3-hosted attachment
links). It's meant to run unattended from cron (see `Procfile`: `*/5 * * * *`).

The code is mid-way through the split described in `REFACTOR_PLAN.md` (steps 0-5 done; its
"Progress" section lists what is left and where the code deviates from the plan). Layout under
`src/pylibrus/`:

- `pylibrus.py` — the CLI (`main()`, `parse_args()`, `read_pylibrus_config()`,
  `send_test_notification()`, the single-user `handle_user()`/`handle_announcements()`), and a
  re-export of every name that used to live here, so `from pylibrus.pylibrus import X` keeps
  working. It also repairs `sys.path` when run by path (see Commands). Goes away in plan step 8.
- `settings.py` — `PyLibrusConfig`, `LibrusUser`, `EmailNotify`/`WebhookNotify` (+ `Notify`).
- `utils.py` — `str_to_bool`/`str_to_int`, `retrieve_from`, `announcement_id`.
- `models.py` — plain NamedTuples passed between layers (`InboxEntry`, `MessageHeader`,
  `AttachmentRef`, `ParsedAnnouncement`).
- `scraper/` — `session.py` (`LibrusSession`: HTTP, referers, cookie cache, OAuth `login()`),
  `cookies.py`, `parse.py` (pure HTML -> values), `attachments.py` (download dance), `urls.py`,
  `client.py` (`LibrusScraper`, `open_scraper()`).
- `db/` — `schema.py` (ORM models), `store.py` (`MessageStore`), `migrations.py`.
- `notify/` — `render.py` (pure email/webhook builders), `smtp.py`/`webhook.py` (transports),
  `base.py` (`Notifier` protocol), `notifier.py` (`make_notifier()`, `LibrusNotifier`).
- `pipeline/` — the two-phase run: `collect.py`, `grouping.py`, `dispatch.py`.
- `storage/s3.py` — S3 attachment re-upload and pre-signed links.
- `logging_setup.py` — `configure_logging()`, called from `main()` only.

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

Tests live in `tests/` (`uv run pytest`). They use fake scrapers, a `FakeSession` standing in
for `LibrusSession` (`tests/fakes.py`), a `LibrusNotifier` subclass that records instead of
sending, and fakes patched over `smtplib.SMTP`/`requests.post`/`boto3.client` themselves, so no
test touches the network or SMTP; but the DBs are real SQLite files in `tmp_path` — several
behaviours (cross-session visibility, write locking) only reproduce against a real shared file.
Shared config fixtures are in `tests/conftest.py`. `test_scraper_inbox_and_message.py` and
`test_notification_rendering.py` are characterisation tests pinning exact parser output and the
exact email/webhook sent: when one fails, the behaviour changed. `--test-notify` is a manual smoke test on top of that: it exercises
`LibrusNotifier.notify()` for the first configured user without touching the Librus site.

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

Per configured Librus user, two independently-scoped context managers:

- **`LibrusScraper`** (`scraper/client.py`) — scrapes the inbox, messages and announcements
  through a `LibrusSession` (`scraper/session.py`); entering the scraper calls `login()`, a
  multi-step OAuth dance reverse-engineered from the browser flow — its comments explain *why*
  each redirect/referer is needed, don't simplify without understanding it. HTML is parsed with
  BeautifulSoup in `scraper/parse.py` (there is no real API; page structure is brittle and tied
  to Librus's markup). Session cookies are cached to a JSON file (`pylibrus_cookies.json` by
  default) and reused across runs to avoid re-login on every cron tick; `are_cookies_valid()`
  checks first.
- **`LibrusNotifier`** (`notify/notifier.py`) — owns a `MessageStore` (`db/store.py`), a
  SQLAlchemy/SQLite session (`db_name`, which falls back to `[global]`, so by default every user
  shares **one** file) recording every seen `Msg`/`Attachment`/`LibrusAnnouncement` so items are
  never processed twice, and sends new ones through the transport `make_notifier()` picks:
  `SmtpNotifier` or `WebhookNotifier` (`requests.post`, Slack-style `{"text": ...}` payload),
  both rendering with `notify/render.py`. The store and the sending live together only until
  plan step 6.

### The run is two phases, not one pass per user

`main()` **collects every user first, then notifies** — it does not scrape-and-send per user.
The collect loop is `collect_all()` (`pipeline/collect.py`), a context manager: the notifiers it
opens stay open until its `with` block, i.e. the notify phase, ends.
This exists so that a message the school sent to several children can be annotated with all of
them: while the first child is still being scraped, nothing yet knows the second child received
the same message. See `MULTI_RECIPIENT_PLAN.md` for the full rationale.

- `collect_user()` scrapes and stores one user's messages and announcements, notifying nothing,
  and returns a `UserCollection` of `CollectedItem`s. It takes the scraper and notifier as
  arguments (not locals) so both phases are testable with fakes.
- `notify_collected()` then groups by `Notify.destination_key()` and, within a destination, by
  item `url`, and sends **one notification per (destination, item)** labelled with that
  destination's children (`display_name` on `notify()`/`send_email()`/`send_via_webhook()`).
  Two children on one address and three on another therefore get one grouped notification each,
  naming only their own children. Items go out oldest first.
- The send rule (`should_notify_group()`) is the old per-message dispatch lifted to a group:
  `"unsent"` and announcements skip what `email_sent` marks; `"unread"` decides purely from the
  unread state scraped from Librus and deliberately **ignores** `email_sent`, so an unread item
  is re-sent every run until somebody opens it. Do not "fix" that into an `email_sent` check.
- `group_names()` builds two strings: the label the reader sees and the full recipient list. A
  user with `always_include_in_subject=false` (per-user config, default true) is left out of the
  label, but *only* while another name remains — an item that reached only such children is
  still labelled with them, and a group where nobody is always-included falls back to the full
  list. The entry is labelling only; it must never reach `destination_key()`, the grouping, the
  send rule or the representative. Logs always use the full recipient list, so `--dry` and the
  skip lines never under-report. See `SUBJECT_NAMES_PLAN.md`.
- `handle_user()` and `handle_announcements()` remain single-user wrappers; grouping across
  children needs `collect_all()`.

Three constraints that are easy to break:

1. **`Notify.destination_key()` must be kept in sync** when adding a notification setting, like
   `from_config()`/`from_env()`. `WebhookNotify` keys on the URL *alone* on purpose — including
   `webhook_attachments_source` would split users who share a webhook and post two copies to it.
2. **`CollectedItem.email_sent` is a snapshot** taken during collect. Children on different
   destinations share one `Msg` row, so reading the live column instead would let the first
   destination's send mark that row and mute every other destination.
3. **`LibrusNotifier.commit()` (i.e. `MessageStore.commit()`) at each phase boundary.** Several notifiers are open at once
   against (by default) one SQLite file, and SQLAlchemy defers `BEGIN` until the first DML, so a
   session holding uncommitted `INSERT`s makes the next user's `INSERT` fail with
   `database is locked`.

Users are visited in config order; the earlier random shuffle was reverted, because the
representative user per destination group — which fixes the sending account and the name order
in the label — must not vary between runs. A user whose scrape raises is logged and skipped (in
`collect_all()`) so the other children are still notified.

`configure_logging()` runs in `main()` *after* the `Config:`/`User:` log lines on purpose: those
lines have never been printed (logging used to be configured by the first scraper, created after
them), and printing them would put each user's webhook URL — `WebhookNotify.webhook` has no
`repr=False` — into the cron log on every run. Don't move it earlier without fixing that.

### Announcements

Librus announcements ("ogłoszenia") are a separate feature from messages, scraped from
`/ogloszenia` by `LibrusScraper.fetch_announcements()` (parsed by `parse_announcements()`) and
collected by `collect_announcements()`, called from `collect_user()` inside a broad
`try/except` so a markup change here never blocks message forwarding. Unlike messages, Librus gives
announcements no stable id, no per-item URL and no per-item read/unread flag — see
`ANNOUNCEMENTS_PLAN.md` for the full design rationale. Consequences that matter when touching
this code:
- `LibrusAnnouncement` (stored in the `announcements` table) uses a synthetic primary key from
  `announcement_id(title, author, date)` — a sha1 of `title|author|date`, deliberately excluding
  the body so an announcement edited in place isn't re-sent as new.
- `send_message` (`"unread"`/`"unsent"`) does not apply to announcements — they always dedupe
  the "unsent" way via `email_sent`, gated only by `max_age_of_sending_announcement_days`
  (`PyLibrusConfig`, falls back to `max_age_of_sending_msg_days`).
- `LibrusAnnouncement` deliberately reuses `Msg`'s attribute names (`url`, `sender`, `subject`,
  `date`, `contents_html`, `contents_text`, `email_sent`) so `LibrusNotifier.notify()`,
  `send_email()` and `send_via_webhook()` work on both unchanged; the only per-type
  customization is the `subject_prefix`/`banner_text`/`banner_html`/`webhook_item_label` class
  attributes each model defines.
- `fetch_announcements` is toggled globally (`PyLibrusConfig`) and per-user (`LibrusUser`,
  `None` meaning "fall back to global") — keep both `from_config()`/`from_env()` in sync as
  usual.

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

`needs_attachment_content()` in `pipeline/collect.py` is the single place deciding whether
attachment bytes need to be downloaded at all — keep it in sync when adding a new attachment
delivery mode.

### Database

`Msg`/`Attachment`/`LibrusAnnouncement` are SQLAlchemy declarative models (`db/schema.py`, one
`Base` for every DB file). New tables need nothing beyond `Base.metadata.create_all()` (run
automatically when a `MessageStore` is entered), but schema changes on *existing* tables must be
added to `MIGRATIONS` in `db/migrations.py` (hand-rolled additive migrations run at startup)
since there's no Alembic — new nullable columns on `Attachment`/`Msg`/`LibrusAnnouncement` need a
`Migration(table, column, sql_type)` entry there or existing users' DBs won't pick them up.
`tests/test_migrations.py` shows how to test one against an aged DB.
