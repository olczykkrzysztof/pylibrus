# pyLibrus

Message scraper from crappy Librus Synergia gradebook. Forwards every new
message from a given folder to an e-mail, and (optionally) new announcements too.

## Running

* Make sure you have [installed `uv`](https://github.com/astral-sh/uv?tab=readme-ov-file#installation)
* Checkout **pylibrus** repository
* Verify everything's installed correctly with `uv run src/pylibrus/pylibrus.py --help`
* Setup `pylibrus.ini` according to [`pylibrus.ini.example`](pylibrus.ini.example)
* Run from cron every few minutes

## Running in Docker

The image runs [`supercronic`](https://github.com/aptible/supercronic) in the foreground as
its own cron daemon, so the container just idles between ticks (no separate scheduler process
to babysit) and job output goes straight to `docker logs`.

```bash
docker build -t pylibrus .

docker run -d --name pylibrus \
  -e CRON_SCHEDULE="*/5 * * * *" \
  -v "$(pwd)/pylibrus.ini:/config/pylibrus.ini:ro" \
  -v pylibrus-data:/data \
  pylibrus
```

Or with `docker-compose.yml` (included in the repo): `docker compose up -d`.

Configuration knobs, all via environment variables:

- `CRON_SCHEDULE` - standard 5-field crontab expression, default `*/5 * * * *` (matches the
  `Procfile`).
- `CONFIG_FILE` - where pylibrus looks for `pylibrus.ini`, default `/config/pylibrus.ini`. Mount
  your `pylibrus.ini` there (see [`pylibrus.ini.example`](pylibrus.ini.example) for multi-user
  setups); it's only ever read, so a read-only mount is fine.
- `DATA_DIR` - working directory for state, default `/data`. This is where cookies
  (`pylibrus_cookies.json`) and each user's SQLite DB get written between runs - mount a volume
  here or that state is lost whenever the container is recreated.
- If nothing is mounted at `CONFIG_FILE`, pylibrus automatically falls back to single-user
  env-var configuration (`LIBRUS_USER`, `LIBRUS_PASS`, `LIBRUS_NAME`, etc. - see
  `read_pylibrus_config()` and the `from_env()` classmethods in `pylibrus.py` for the full list).
  Just pass those as container env vars instead of mounting a config file; a `DATA_DIR` volume
  is still recommended so cookies/DB persist.
- `PYLIBRUS_EXTRA_ARGS` - extra CLI flags appended to each run, e.g. `--debug`.

## Webhook attachments in S3

Webhook notifications can send attachment links from Librus or from S3.

Configuration (in each webhook user section, e.g. `[user:ChildName2]`):
- `webhook_attachments_source=librus_link` keeps sends links to attachments on Librus webpage
- `webhook_attachments_source=s3://<bucket>/<optional-prefix>` uploads attachments to S3 and sends pre-signed links
- when using `s3://...` for that user, set:
  - `s3_region`
  - `s3_access_key_id`
  - `s3_secret_access_key`
  - optional `s3_session_token`
  - optional `s3_endpoint_url` (for S3-compatible storage)

S3 link expiration is fixed in code:
- `LINK_EXPIRE_DURATION = 604800` (7 days, max for S3 pre-signed URLs)

Required IAM permissions for provided credentials:
- `s3:PutObject`
- `s3:GetObject`

Objects are never deleted from bucket so please set correct lifecycle rules for your bucket.
Recommended setting is to automatically remove files after 7 days.

One bucket can be used for many users at the same time.

## Announcements

pyLibrus can also forward Librus announcements ("ogłoszenia") - school-wide or
class-wide notices, separate from the inbox. Enabled by default
(`fetch_announcements=true`), can be turned off globally or per child; see
[`pylibrus.ini.example`](pylibrus.ini.example).

Announcements are sent through the same e-mail/webhook path as messages, with a
distinct subject prefix ("Ogłoszenie: ...") and banner. A few things are inherently
different from messages, because Librus exposes announcements differently:

* Announcements have no attachments and no per-item read/unread flag, so
  `send_message=unread` does not apply to them - each announcement is forwarded once
  and never re-sent, regardless of that setting.
* Announcements have no stable id in Librus; pyLibrus derives one from
  title+author+publication date, so editing an announcement's body afterwards does not
  trigger a re-send.
* Only announcements published within `max_age_of_sending_announcement_days` are
  forwarded (defaults to `max_age_of_sending_msg_days`).

See [`ANNOUNCEMENTS_PLAN.md`](ANNOUNCEMENTS_PLAN.md) for the full design rationale.

## Potential improvements

* support HTML messages
* support calendar
