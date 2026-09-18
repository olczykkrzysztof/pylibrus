# pyLibrus

Message scraper from crappy Librus Synergia gradebook. Forwards every new
message from a given folder to an e-mail, and (optionally) new announcements too.

## Running

* Make sure you have [installed `uv`](https://github.com/astral-sh/uv?tab=readme-ov-file#installation)
* Checkout **pylibrus** repository
* Verify everything's installed correctly with `uv run src/pylibrus/pylibrus.py --help`
* Setup `pylibrus.ini` according to [`pylibrus.ini.example`](pylibrus.ini.example)
* Run from cron every few minutes

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

## Messages sent to several children

The school often sends one message to several children. Each child's Librus account receives
its own copy, but pyLibrus forwards **one** notification per destination rather than one per
child, and the subject names every child it was sent to:

```
[LIBRUS Ania, Jaś] Zebranie z rodzicami
```

so you can tell at a glance whether a message concerns one child or several. A message that
reached only one child names only that child, exactly as before.

Each destination is told only about the children configured to it: with two children forwarded
to one address and three to another, each address receives a single grouped notification naming
its own children. Children on separate `db_name` files are grouped too.

One consequence of collecting every child before sending: notifications arrive only after all
children have been scraped, so with several children and a large `sleep_between_librus_users`
they land a few minutes later than they used to.

See [`MULTI_RECIPIENT_PLAN.md`](MULTI_RECIPIENT_PLAN.md) for the design rationale, including
what is deliberately *not* handled — if one child's scrape fails, the message goes out labelled
with the children that were scraped successfully and is not relabelled later.

## Potential improvements

* support HTML messages
* support calendar
