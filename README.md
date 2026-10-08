# pyLibrus

Message scraper from Librus Synergia gradebook. Forwards every new
message from a given folder to an e-mail, and (optionally) new announcements too.

> [!WARNING]
> **Disclaimer.** pyLibrus is an unofficial, community-made tool. It is not affiliated with,
> endorsed by or supported by Librus or the Synergia gradebook. It is intended **for personal
> and education use only**: on your individual single account.
>
> pyLibrus logs into Librus and scrapes its web pages automatically. **It is your
> responsibility to check that using it complies with the Librus terms of service** (and any
> rules set by your school) before you run it.
> *Never* use it for bots, automated scrapping, monitoring or commercial purposes!
>
> The software is provided "as is", without warranty of any kind. 
> The authors accept no liability for its use, including blocked
> accounts, missed or duplicated notifications, or data sent to the wrong place.

## What it does

pyLibrus is a small command-line script meant to run unattended from cron every few minutes.
On each run, for every configured Librus parent account ("user", usually one per child), it:

1. **Logs into Librus Synergia** the way a browser does. Librus has no public API, so pyLibrus
   follows the web login flow and parses the HTML pages. Session cookies are cached in
   `pylibrus_cookies.json` and reused, so a full login only happens when the session has
   expired.
2. **Reads the inbox** ("Odebrane"), along with each message's read/unread state. Only the
   first page of the inbox listing is read, which is plenty when running every few minutes.
3. **Downloads each new message**: sender, subject, sent date, body (HTML and plain text) and
   attachments. Messages older than `max_age_of_sending_msg_days` (default 4) are skipped, so
   the first run doesn't flood you with the whole school year.
4. **Stores everything in a local SQLite database** (`pylibrus.sqlite` by default) so that
   nothing is downloaded or processed twice.
5. **Forwards new items** by e-mail or to a webhook (see [Notifications](#notifications)).

Optionally it also forwards [announcements](#announcements), and it merges
[messages sent to several children](#messages-sent-to-several-children) into one
notification.

### Which messages are forwarded

The `send_message` setting picks one of two modes:

* `unread` (default): a message is forwarded while it is **unread in Librus**. pyLibrus never
  marks messages as read itself, so an unread message is sent again on **every run** until
  somebody opens it in Librus. Treat it as a reminder that won't stop until the message is
  read. With several children, a message counts as read only once it is read in every
  child's account.
* `unsent`: each message is forwarded **exactly once**, whatever its state in Librus.

## Notifications

Each user is forwarded to one destination: e-mail or webhook. Several users can share the
same destination.

### E-mail

Set `smtp_server`, `smtp_port` (default 587), `smtp_user`, `smtp_pass` and `email_dest`, which
is a comma-separated list of recipients. pyLibrus connects with STARTTLS and logs in as
`smtp_user`, which is also the sender address. A forwarded message looks like this:

* **Subject:** `[LIBRUS <child name>] <original subject>`
* **From:** `"<Librus sender> @ Librus" <smtp_user>`, so you can see at a glance which teacher
  wrote it
* **Body:** the original message in HTML and plain text, followed by the date it was sent in
  Librus
* **Attachments:** downloaded from Librus and attached to the e-mail. With
  `fetch_attachments=false` the e-mail lists links to the attachments on Librus instead.
  Those links only work in a browser that is logged into Librus.

For Gmail, use an [app password](https://support.google.com/accounts/answer/185833) as
`smtp_pass`.

### Webhook

Set `webhook` to a URL that accepts a Slack-style JSON payload (`{"text": "..."}`), such as a
Slack incoming webhook or anything compatible. The text holds the child's name, date, sender,
subject and plain-text body, plus links to the attachments. By default those links point to
Librus. To get links that work without a Librus login, see
[Webhook attachments in S3](#webhook-attachments-in-s3).

## Running

* Make sure you have [installed `uv`](https://github.com/astral-sh/uv?tab=readme-ov-file#installation)
* Checkout **pylibrus** repository
* Verify everything's installed correctly with `uv run src/pylibrus/pylibrus.py --help`
* Setup `pylibrus.ini` according to [`pylibrus.ini.example`](pylibrus.ini.example)
* Send a test notification with `uv run src/pylibrus/pylibrus.py --test-notify` (see below)
* Run from cron every few minutes

### Configuration

The recommended setup is an INI file, `pylibrus.ini` in the working directory. It has a
`[global]` section and one `[user:<Name>]` section per Librus account. `<Name>` is the name
shown in notifications. Every key is documented in
[`pylibrus.ini.example`](pylibrus.ini.example).

If the INI file doesn't exist, pyLibrus reads its configuration from environment variables
instead. This supports a **single user with e-mail notifications** only: `LIBRUS_USER`,
`LIBRUS_PASS`, `LIBRUS_NAME`, `SMTP_USER`, `SMTP_PASS`, `SMTP_SERVER`, `SMTP_PORT` and
`EMAIL_DEST`, plus optionally `SEND_MESSAGE`, `FETCH_ATTACHMENTS`,
`MAX_AGE_OF_SENDING_MSG_DAYS`, `FETCH_ANNOUNCEMENTS`, `MAX_AGE_OF_SENDING_ANNOUNCEMENT_DAYS`,
`DB_NAME` and `LIBRUS_DEBUG`. Use the INI file for several children or webhooks.

The INI file contains your Librus and SMTP passwords, so make it readable only by you
(`chmod 600 pylibrus.ini`).

### Command-line options

| Option | Meaning |
| --- | --- |
| `--workdir PATH` | directory with the config file, databases and cookie file (default: current directory) |
| `--config PATH` | config file, absolute or relative to workdir (default: `pylibrus.ini`) |
| `--cookies PATH` | cookie cache file, absolute or relative to workdir (default: `pylibrus_cookies.json`) |
| `--test-notify` | send a fake message and a fake announcement to the **first** configured user's destination, without contacting Librus. Use it to check your SMTP or webhook settings. |
| `--dry` | scrape everything and print what would be sent, without sending anything or marking anything as sent |
| `--debug` | verbose logging |

### Scheduling

Run it from cron, for example every 5 minutes:

```
*/5 * * * * cd /path/to/pylibrus && uv run src/pylibrus/pylibrus.py --workdir /path/to/workdir
```

The [`Procfile`](Procfile) contains the same `*/5 * * * *` schedule, for hosts that read cron
jobs from a Procfile.

With several children, pyLibrus waits `sleep_between_librus_users` seconds between accounts so
that Librus doesn't throttle the logins.

### Files it creates

All of these are in the working directory:

* `pylibrus.sqlite`: every message, attachment and announcement seen so far, with whether it
  was sent. All users share this file unless `db_name` is set. Deleting it makes pyLibrus
  treat everything still within the age limit as new.
* `pylibrus_cookies.json`: the cached Librus session. Safe to delete; the next run logs in
  again.

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

If one of the accounts adds nothing to that label - a second parent login onto the same child,
a leftover account that still receives school-wide announcements - set
`always_include_in_subject=false` for it. Its name is then left out whenever at least one other
child received the same message:

```
[LIBRUS Ania] Zebranie z rodzicami
```

but it is still named when nothing else would name the message, so you never get a notification
that doesn't say whose it is. The setting is about redundancy in that list and nothing else: the
account still takes part in deduplication, so the message is still forwarded exactly once.

One consequence of collecting every child before sending: notifications arrive only after all
children have been scraped, so with several children and a large `sleep_between_librus_users`
they land a few minutes later than they used to.

See [`MULTI_RECIPIENT_PLAN.md`](MULTI_RECIPIENT_PLAN.md) for the design rationale, including
what is deliberately *not* handled — if one child's scrape fails, the message goes out labelled
with the children that were scraped successfully and is not relabelled later.

## Potential improvements

* support HTML messages
* support calendar
