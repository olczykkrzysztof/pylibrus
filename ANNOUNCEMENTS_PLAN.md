# Plan: support for Librus announcements (ogłoszenia) in pyLibrus

Status: proposal / design document. Nothing in this document is implemented yet —
it is the plan for adding announcement (`/ogloszenia`) support next to the existing
message (`/wiadomosci`) support.

---

## 1. What announcements are, and how other libraries scrape them

Librus Synergia has no public API, so every existing client screen-scrapes the same
page. Three independent implementations were analysed and they agree on the shape of
the data:

| Library | Language | Entry point | Selector |
|---|---|---|---|
| [findepi/librus-api-python](https://github.com/findepi/librus-api-python) | Python + requests-html | `GET https://synergia.librus.pl/ogloszenia` | `table.decorated.big` |
| [Mati365/librus-api](https://github.com/Mati365/librus-api) | Node + cheerio | `GET https://synergia.librus.pl/ogloszenia` | `div#body div.container-background table.decorated` |
| [RustySnek/librus-apix](https://github.com/RustySnek/librus-apix) | Python + BeautifulSoup | `GET https://synergia.librus.pl/ogloszenia` | `table.decorated.big.center.printable.margin-top` |

### 1.1 Page structure

`/ogloszenia` is a **single page containing every currently valid announcement** —
there is no list/detail split, no pagination and no per-announcement URL. Each
announcement is one `<table class="decorated big …">`:

```html
<table class="decorated big center printable margin-top">
  <thead>
    <tr><td colspan="2">Zebranie z rodzicami</td></tr>   <!-- title -->
  </thead>
  <tbody>
    <tr class="line0"><th>Dodał</th>            <td>Jan Kowalski</td></tr>
    <tr class="line1"><th>Data publikacji</th>  <td>2025-09-08</td></tr>
    <tr class="line0"><th>Treść</th>            <td>Treść ogłoszenia …</td></tr>
  </tbody>
</table>
```

* `thead > tr > td` → **title**
* `tbody tr` rows are label/value pairs keyed by the `<th>` text:
  `Dodał` → author, `Data publikacji` → date (`%Y-%m-%d`, **date only, no time**),
  `Treść` → content (contains HTML: `<br/>`, links, entities).
* `librus-apix` hard-asserts exactly 3 rows; `findepi` raises on any unknown `<th>`
  label. Both mean: **the row set is stable in practice but not guaranteed** — we will
  parse by label and tolerate extras instead of asserting.
* Empty state: `librus-apix` checks `div.container.border-red.resizeable.center > div > p`
  and returns `[]` (no announcements published).
* Expired/withdrawn announcements simply disappear from the page.

### 1.2 What announcements do **not** have

These gaps drive most of the design below:

1. **No stable id and no per-announcement URL.** Messages are keyed in our DB by
   `msg_path` (`/wiadomosci/5/…`); announcements have nothing equivalent.
   `librus-apix` dedupes on `title + date` (`_parse_announcements_notification()` in
   `librus_apix/notifications.py`).
2. **No per-item read/unread flag.** Messages expose it via the bold `<td style>` in the
   folder listing (`msgs_from_folder()`, `pylibrus.py:709`). For announcements the only
   "unread" signal is the aggregate counter badge on the dashboard
   (`div#graphic-menu` on `/uczen_index`, used by both `Mati365`'s
   `getNotifications()` and `librus-apix`'s `get_new_token_notification_amounts()`).
   That counter is **consumed by visiting the page** — after one scrape it drops to 0,
   so it cannot be the source of truth for a cron job that runs every 5 minutes.
3. **No attachments.** All three libraries parse exactly three fields; announcements in
   Synergia carry no file attachments. The whole `Attachment` / S3 / `singleUseKey`
   machinery is therefore *not* needed for this feature.
4. **Announcements are mutable.** A school can edit an announcement in place; the title
   and publication date normally stay, the body may change.

### 1.3 Consequence for pyLibrus' `send_message` modes

`send_message=unread` cannot be implemented faithfully for announcements (see 1.2.2).
**Announcements are always deduplicated the "unsent" way** — DB-backed, keyed by a
synthetic id. This must be stated in the config docs, because a user with
`send_message=unread` will otherwise expect the same semantics.

---

## 2. Design decisions

### D1 — Separate `announcements` table, but Msg-compatible column names

Add a new SQLAlchemy model rather than overloading `Msg` with a `kind` column (no
Alembic here, and repurposing `Msg.url`'s primary-key semantics on existing user DBs is
risky). Crucially, the new model **reuses the attribute names that
`LibrusNotifier.notify()` / `send_email()` / `send_via_webhook()` already read**
(`url`, `sender`, `subject`, `date`, `contents_html`, `contents_text`, `email_sent`),
so the whole notification path works unchanged:

```python
class LibrusAnnouncement(Base):
    __tablename__ = "announcements"

    url = Column(String, primary_key=True)      # synthetic, see D2
    sender = Column(String)                     # "Dodał"
    subject = Column(String)                    # title from <thead>
    date = Column(DateTime)                     # "Data publikacji", midnight
    contents_html = Column(String)
    contents_text = Column(String)
    content_hash = Column(String)               # sha1 of contents_text, for edit detection
    email_sent = Column(Boolean, default=False)
    first_seen = Column(DateTime)
```

`LibrusNotifier._get_attachments()` (`pylibrus.py:805`) queries
`Attachment.msg_path == msg_from_db.url`; for a synthetic announcement url nothing ever
matches, so it returns `[]` and both notifiers degrade correctly with no extra branching.

### D2 — Synthetic primary key

```python
def announcement_id(title: str, author: str, date: datetime.date) -> str:
    digest = hashlib.sha1(f"{title}|{author}|{date:%Y-%m-%d}".encode()).hexdigest()[:16]
    return f"/ogloszenia#{digest}"
```

* Deliberately **excludes the body**, so an edited announcement is not re-sent as a new
  one (matches `librus-apix`'s `title + date` dedupe, plus author for safety).
* Keeping the `/ogloszenia#…` prefix means the value is still a usable, clickable-ish
  identifier and is visibly distinct from message paths in logs.
* `content_hash` is stored so a future `resend_edited_announcements` option is a
  one-line change; **default behaviour: do not re-send edits.**

### D3 — Reuse the notification path, parameterise only the wording

`send_email()` (`pylibrus.py:888`) hardcodes `[LIBRUS {name}] {subject}` and the
"TO WIADOMOŚĆ Z LIBRUSA" banner. Introduce two optional attributes read with
`getattr(..., default)` so `Msg` needs no change:

* `subject_prefix` → `""` for messages, `"Ogłoszenie: "` for announcements
* `banner_text` / `banner_html` → "TO WIADOMOŚĆ Z LIBRUSA…" vs
  "TO OGŁOSZENIE Z LIBRUSA, NIE ODPOWIADAJ NA NIE MEJLEM!"

Same for the webhook payload in `send_via_webhook()` (`pylibrus.py:854`): the
`*LIBRUS {name} - {date}*` / `*Od: …*` / `*Temat: …*` header stays, with
`*Ogłoszenie: …*` substituted for `*Temat: …*`.

Implementation note: define them as plain Python `@property` on the model
(non-mapped attributes are fine on a declarative class) or as module-level constants
selected by `isinstance`. The `@property` route keeps `notify()` free of type checks.

### D4 — Announcements are opt-in-by-default, per user

Follow the existing `db_name` pattern: a `[global]` default with an optional per-user
override, both `from_config()` and `from_env()` kept in sync (required by `CLAUDE.md`).

### D5 — Age filtering

`fetch_msg()` (`pylibrus.py:694`) drops messages older than
`max_age_of_sending_msg_days`. Announcements carry a **date without a time**, so the
same 4-day window measured from midnight is slightly more generous — acceptable, but
worth a dedicated `max_age_of_sending_announcement_days` (default: fall back to
`max_age_of_sending_msg_days`) because schools publish announcements that stay relevant
much longer than a message.

Additional guard for first runs: an existing user's DB has no announcements at all, so
the first scrape would otherwise blast every announcement of the last N days at once.
The age window already bounds this; it is called out in the docs rather than adding a
"seed silently on first run" mode (which would silently drop genuinely new items).

---

## 3. Implementation steps

All changes are in `src/pylibrus/pylibrus.py` unless stated otherwise.

### Step 1 — Config (`PyLibrusConfig`, `pylibrus.py:65`)

Add, with `from_config()` **and** `from_env()` updated in the same commit:

| field | default | env var | INI key (`[global]`) |
|---|---|---|---|
| `fetch_announcements` | `True` | `FETCH_ANNOUNCEMENTS` | `fetch_announcements` |
| `max_age_of_sending_announcement_days` | `None` → falls back to `max_age_of_sending_msg_days` | `MAX_AGE_OF_SENDING_ANNOUNCEMENT_DAYS` | `max_age_of_sending_announcement_days` |

Add a non-init field `announcements_path: str = "/ogloszenia"` alongside the existing
`inbox_folder_id` so the path stays configurable-in-code but out of the INI surface.

### Step 2 — Per-user override (`LibrusUser`, `pylibrus.py:222`)

Add `fetch_announcements: bool | None = None` to the dataclass, read in
`from_config()` (`config[section].getboolean("fetch_announcements", fallback=None)`)
and `from_env()`. Resolution order at call time: user value → global value.

### Step 3 — Model + migration (`pylibrus.py:266`–`285`, `_migrate_attachment_table`, `pylibrus.py:749`)

* Add `LibrusAnnouncement` from D1.
* `Base.metadata.create_all()` in `_create_db()` (`pylibrus.py:739`) creates the new
  table on existing DBs automatically — **no `ALTER TABLE` needed for a brand-new
  table**. But rename `_migrate_attachment_table()` → `_migrate_tables()` and make the
  `inspect(...).get_columns(...)` calls table-aware, so future *column* additions on
  `announcements` have an obvious home (this is the hand-rolled migration hook
  `CLAUDE.md` describes).
* Guard `_migrate_tables()` against a table that does not exist yet (call
  `inspect(engine).has_table(...)` first) — `create_all()` runs before it, so this is
  belt-and-braces for reordering later.

### Step 4 — Scraper (`LibrusScraper`, `pylibrus.py:384`)

```python
@classmethod
def announcements_url(cls) -> str:
    return cls.synergia_url_from_path("/ogloszenia")

def fetch_announcements(self) -> list[tuple[str, str, datetime.date, str, str]]:
    """Returns (title, author, date, contents_html, contents_text) per announcement."""
```

Parsing rules (deliberately more forgiving than the reference libraries):

1. `GET /ogloszenia` with `referer=self.synergia_url_from_path("/rodzic/index")`,
   mirroring `msgs_from_folder()` (`pylibrus.py:709`).
2. Reuse the existing `"Brak dostępu" not in resp.text` convention from
   `are_cookies_valid()` (`pylibrus.py:515`); raise so the caller can force a re-login
   rather than silently reporting "no announcements".
3. `soup.select("table.decorated")` restricted to tables that have a `<thead>`; do not
   depend on the full `big center printable margin-top` class list (schools run
   different Synergia skins — `findepi` uses `.big`, `Mati365` uses none of them).
4. Title: `table.thead td` text, stripped.
5. Rows: for each `tbody tr`, `th` text → key, `td` → value. Match on
   `Dodał` / `Data publikacji` / `Treść`; **ignore unknown labels with a
   `logger.debug`** instead of raising (`findepi` raises — that turns a cosmetic Librus
   change into a total outage; we already have the same brittleness in
   `_find_msg_header()` and should not add more).
6. If title or `Data publikacji` is missing → `logger.warning` and skip that table.
7. Keep both `str(td)` (HTML, for the e-mail HTML part) and `td.get_text()` (text part),
   exactly as `fetch_msg()` does with `container-message-content`.
8. Date: `datetime.datetime.strptime(text, "%Y-%m-%d")`. On failure, log and skip.
9. Return newest-first order as the page provides it; `handle_announcements()` reverses
   to send oldest-first, matching `msgs_from_folder()`'s `msgs.reverse()`.

Empty page → return `[]` (no exception): both an empty
`div.container.border-red…` block and simply zero matching tables mean "nothing to do".

### Step 5 — Notifier (`LibrusNotifier`, `pylibrus.py:732`)

```python
def get_announcement(self, url): ...                     # mirrors get_msg (pylibrus.py:775)
def add_announcement(self, url, sender, subject, date, contents_html,
                     contents_text, content_hash): ...   # mirrors add_msg (pylibrus.py:778)
```

`notify()` (`pylibrus.py:795`) needs only its log lines generalised
(`msg_from_db.subject` already covers both). No change to `send_email()` beyond the
`subject_prefix` / banner lookups from D3.

### Step 6 — Wire into `handle_user()` (`pylibrus.py:990`)

Add a sibling loop after the message loop, inside the same two context managers so the
session and the DB transaction are shared:

```python
if pylibrus_config.fetch_announcements and librus_user.fetch_announcements is not False:
    handle_announcements(pylibrus_config, librus_user, scraper, notifier)
```

`handle_announcements()`:

1. `for title, author, date, html, text in reversed(scraper.fetch_announcements()):`
2. Skip if `now - date > max_age_of_sending_announcement_days` → `logger.info`
   (same message style as `fetch_msg()`).
3. `url = announcement_id(title, author, date)`; `notifier.get_announcement(url)`.
4. If present and `email_sent` → skip ("announcement already sent").
5. If absent → `add_announcement(...)`.
6. `notifier.notify(ann)` then `ann.email_sent = True`.

Wrap the whole announcement block in `try/except Exception` with a `logger.exception`:
a Librus markup change on `/ogloszenia` must never stop messages from being forwarded
(messages are the primary feature and run first).

### Step 7 — `--test-notify` (`send_test_notification`, `pylibrus.py:974`)

Extend to send a fake announcement as well as the fake message, so the e-mail/webhook
rendering of the new item type is smoke-testable without Librus credentials. Either
send both unconditionally, or add `--test-notify-announcement`; sending both keeps the
CLI surface unchanged and is the recommended option.

### Step 8 — Docs & config example

* `pylibrus.ini.example`: document `fetch_announcements`,
  `max_age_of_sending_announcement_days`, and the per-user `fetch_announcements`
  override, **including the note that announcements ignore `send_message=unread` and
  always behave as `unsent`**.
* `README.md`: remove `support announcements` from "Potential improvements", add a short
  "Announcements" section (what is forwarded, dedupe semantics, no attachments).
* `CLAUDE.md`: extend the Architecture section — the new table, the `announcements`
  scraping entry point, and the rule that `_migrate_tables()` is still the only place for
  schema changes.

---

## 4. Verification

There is no test suite today (`CLAUDE.md`), and the parser is the only genuinely
fragile part of this change, so:

1. **Offline fixture test (recommended, new).** Save one real `/ogloszenia` page to
   `tests/fixtures/ogloszenia.html` (scrub names), add `tests/test_announcements.py`
   asserting the parser returns the expected tuples, plus cases for: no announcements,
   an unknown `<th>` label, a malformed date. `pytest` as a dev-dependency in
   `pyproject.toml` `[tool.uv] dev-dependencies`. This is ~40 lines and makes every
   future Librus markup change a 1-minute diagnosis.
2. `uv run src/pylibrus/pylibrus.py --test-notify` → confirms rendering of both item
   types over the user's real e-mail/webhook.
3. Manual end-to-end against a real account with `--workdir` pointed at a scratch dir:
   run twice, confirm the second run sends nothing (dedupe), then
   `sqlite3 <db> 'select url, subject, email_sent from announcements'`.
4. Run against an **existing** production DB copy to confirm the new table is created
   and messages still flow.
5. `uv run ruff check . && uv run ruff format --check .`

---

## 5. Risks and open questions

| # | Risk | Mitigation |
|---|---|---|
| R1 | `/ogloszenia` path/markup differs for a **parent** (`rodzic`) account — pyLibrus logs in as a parent, while `librus-apix`/`findepi` are student-oriented. | Verify first against a real parent account (step 4.3). If it differs, only `announcements_url()` and the selector change; the rest of the plan holds. Log the raw response length at debug level to make this diagnosable. |
| R2 | Title+author+date collision (two announcements, same title, same day, same author) → one silently dropped. | Rare; accepted. `content_hash` is stored, so a follow-up can promote it into the key if it ever bites. |
| R3 | Announcement edited by school → not re-sent. | Intentional (D2). `content_hash` enables an opt-in `resend_edited_announcements` later. |
| R4 | First run on an existing install floods the user with every announcement inside the age window. | Bounded by `max_age_of_sending_announcement_days`; documented. A conservative default (reuse the 4-day message window) keeps the blast radius small. |
| R5 | Markup change breaks parsing and kills the whole cron run. | Label-driven parsing, skip-and-warn per table, and the `try/except` around the whole announcement block (step 6). |
| R6 | Unread-counter approach (`div#graphic-menu`) is tempting but wrong for a 5-minute cron. | Explicitly not used; see 1.2.2. |
| R7 | Announcement bodies contain raw HTML that could break the MIME HTML part. | Same exposure as messages today (`contents_html` is inserted verbatim in `send_email()`); no new risk, no new sanitisation proposed. |

---

## 6. Suggested commit sequence

1. `announcements: add model, config flags and migration hook` (steps 1–3, docs for the
   new keys) — no behaviour change yet.
2. `announcements: scrape /ogloszenia` (step 4 + fixture test from 4.1).
3. `announcements: notify about new announcements` (steps 5–6, `--test-notify` from 7).
4. `docs: document announcement support` (step 8).

Each commit leaves the tree runnable; announcements only start being sent at commit 3.
