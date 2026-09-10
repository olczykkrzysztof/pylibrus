# Refactor plan: from one file to testable modules

`src/pylibrus/pylibrus.py` is ~1550 lines holding config parsing, ORM models, HTTP/OAuth,
HTML scraping, S3 uploads, MIME rendering, SMTP/webhook delivery, the two-phase collect/notify
run with its cross-child grouping, and the CLI. Nothing is *wrong* with it, and the
orchestration is already well covered (`collect_user()`/`notify_collected()` take their
collaborators as arguments, and `tests/test_multi_recipient.py` drives them with fakes). But the
pieces most likely to break in production (Librus markup parsing, Polish-charset email
rendering) are still the hardest to reach from a test, because they only exist behind a network
call or an SMTP connection.

This document proposes a target layout, explains the seams that make each part testable, and
gives an incremental migration order where every step leaves the tree green.

## 1. What blocks testing today

| # | Problem | Where | Consequence |
|---|---------|-------|-------------|
| 1 | Constructing a scraper does I/O-adjacent work: `logging.basicConfig()`, global `http_client.HTTPConnection.debuglevel`, cookie file read | `LibrusScraper.__init__` | Every test that builds a scraper reconfigures the root logger for the whole session |
| 2 | `requests.session()` is created internally, with no injection point | `LibrusScraper.__init__` | `tests/test_announcements.py` and `tests/test_attachment_download.py` both monkeypatch the private `scraper._get` |
| 3 | Parsing and fetching are fused | `fetch_msg`, `msgs_from_folder`, `fetch_announcements`, `fetch_attachments` | Inbox parsing (`msgs_from_folder`) and message parsing (`fetch_msg`) have **no test at all**: every orchestration test replaces them with a `FakeScraper`. They are the two parsers every single run depends on |
| 4 | The scraper returns ORM objects (`Attachment`) and an untyped 6-tuple, and applies send policy | `fetch_msg`, `fetch_attachments` | Scraping is coupled to SQLAlchemy. The message age filter lives in the scraper (`pylibrus.py:802`) while the announcement age filter lives in `collect_announcements()`: the same policy in two layers |
| 5 | `LibrusNotifier` is three things at once: DB session owner, renderer, and transport | `LibrusNotifier` | Asserting on one `Subject` header takes a 20-line `FakeSMTP` monkeypatched over `pylibrus.smtplib.SMTP` (`test_multi_recipient.py:495`); the webhook header test patches `pylibrus.requests.post` |
| 6 | The collect phase hands **live ORM rows and open sessions** to the notify phase | `CollectedItem.item`, `UserCollection.notifier` | Two of CLAUDE.md's three "easy to break" constraints exist only because of this: the `email_sent` snapshot (reading the shared live row mutes later destinations) and `commit()` at every phase boundary (an uncommitted session locks the shared SQLite file). Both are conventions. It also means pure label/send-rule logic (`group_names`, `should_notify_group`) is only reachable through a real DB: 23 of the 29 tests in `test_multi_recipient.py` run one, though only about five are about database behaviour |
| 7 | `main()`'s per-user loop is untested; the tests copy it instead | `main()`, `test_multi_recipient.py:92`, `:339` | The test helper `run()` reimplements the loop "the way main() does", and the scrape-failure test reimplements the per-user guard as `contextlib.suppress(Exception)  # mirrors main()'s per-user guard`. Deleting the real `try/except` from `main()` would fail no test |
| 8 | Three things must be hand-synced per setting: `from_config()`, `from_env()`, and now `destination_key()` | 4 dataclasses | CLAUDE.md has to warn about all three; nothing enforces them. The None-means-default rule is also implemented two ways: `PyLibrusConfig.__post_init__` normalises generically, while `LibrusUser.from_env` (`pylibrus.py:296`) hand-codes it for `always_include_in_subject` because `LibrusUser` has no `__post_init__` |
| 9 | Test payloads are detached ORM rows | `send_test_notification` | A `Msg(...)` with `folder="Odebrane"` (a string in an Integer column) is used as a fake DTO |

## 2. Target layout

```
src/pylibrus/
    __init__.py            # version only, no side effects
    __main__.py            # python -m pylibrus -> cli.main()
    cli.py                 # argparse, logging setup, ExitStack wiring. No business logic.
    logging_setup.py       # configure_logging(debug) - called from cli only
    settings.py            # PyLibrusConfig, LibrusUser, EmailNotify, WebhookNotify + field spec
                           #   (destination_key() lives here, derived from the spec)
    config_loader.py       # read_pylibrus_config(): INI vs env source selection + config warnings
    models.py              # domain dataclasses (no SQLAlchemy): ParsedMessage, ParsedAnnouncement,
                           #   AttachmentRef, StoredItem, NotificationStyle
    scraper/
        __init__.py        # re-export LibrusScraper
        session.py         # LibrusSession: requests.Session, cookie persistence, the OAuth dance
        cookies.py         # load/store cookie JSON (pure functions over a Path)
        parse.py           # html -> domain objects. No network, no ORM, no config.
        attachments.py     # the sandbox.librus.pl single-use-key download dance
        client.py          # LibrusScraper: composes session + parse into fetch_* methods
    db/
        __init__.py
        schema.py          # Base, Msg, Attachment, LibrusAnnouncement (unchanged columns)
        migrations.py      # declarative list of additive migrations + apply()
        store.py           # MessageStore: get/add/mark_sent/attachments_for; owns the Session,
                           #   commits its own writes
    pipeline/
        __init__.py
        collect.py         # collect_all() / collect_user() / collect_announcements(),
                           #   needs_attachment_content()
        grouping.py        # pure: group_by_destination, group_items_by_url,
                           #   should_notify_group, group_names
        dispatch.py        # notify_group / notify_collected: grouping -> Notifier -> store.mark_sent
    notify/
        __init__.py
        base.py            # Notifier protocol: notify(item, display_name)
        render.py          # build_email_message() -> EmailMessage, build_webhook_payload() -> dict. Pure.
        email.py           # SmtpNotifier: render + smtplib
        webhook.py         # WebhookNotifier: render + requests.post
    storage/
        __init__.py
        s3.py              # S3AttachmentStorage, key building, pre-signed links
```

Four packages, one per subsystem: scraper, db, pipeline (the two-phase run) and notify. Flat
modules for everything else. Nothing nests more than two levels.

### Why each split earns its place

**`scraper/parse.py`: the biggest win.** Pure functions, HTML string in, dataclass out:

```python
def parse_inbox(html: str) -> list[InboxEntry]: ...          # (path, read) pairs
def parse_message(html: str) -> ParsedMessage: ...           # sender/subject/date/contents
def parse_attachment_rows(html: str) -> list[AttachmentRef]: ...
def parse_announcements(html: str) -> list[ParsedAnnouncement]: ...
```

These are exactly the functions that break when Librus reshuffles a `<td>`, and two of them
(inbox and message) have no test today. After the split a regression test is
`assert parse_inbox(FIXTURE) == [...]` with no fakes at all. `tests/test_announcements.py`
currently needs a scraper instance, a tmp_path, a config object and a monkeypatched private
method to assert on parser output; it collapses to a direct call.

The message age filter moves out of `fetch_msg` (`pylibrus.py:802`) into `pipeline/collect.py`,
next to the announcement age filter that already lives there. The parser stays a parser, and
"too old to send" becomes one policy in one place.

**`scraper/session.py`: the injection seam.** The OAuth sequence in `__enter__` moves here
*verbatim*, comments included; it is reverse-engineered and must not be "cleaned up". The only
change is that `LibrusScraper` takes a session object rather than building one:

```python
class LibrusScraper:
    def __init__(self, session: LibrusSession, config: PyLibrusConfig): ...
```

Tests pass a `FakeSession` whose `get`/`post` return canned responses, and constructing a
scraper stops touching the root logger or the filesystem (problems 1 and 2).
`test_attachment_download.py`'s URL-keyed `fake_get` is already that fake in all but name.

**`models.py` + `db/store.py`: values across the phase boundary.** This is the one change in
the plan that is more than a move, and it targets problem 6.

Today `CollectedItem.item` is a live `Msg`/`LibrusAnnouncement` row, so `UserCollection` has to
carry the notifier whose session owns it, and every session stays open from the first collect to
the last send. Instead, the collect phase returns a frozen `StoredItem` (url, kind, sender,
subject, date, contents, and `email_sent` *as read at collect time*), and dispatch writes back
through `store.mark_sent(url)`. Then:

- **The `email_sent` snapshot stops being a convention.** There is no live column on the value
  to read by mistake, so "the first destination's send mutes the others" can't be reintroduced
  by a refactor that reaches for `item.email_sent`.
- **"Commit at every phase boundary" becomes the store's job.** `MessageStore.add_*()` and
  `mark_sent()` commit their own writes, so no caller can leave an open write transaction
  holding the SQLite lock. `test_concurrent_sessions_on_one_db_file_do_not_deadlock` already
  guards this and must keep passing unchanged.
- **Grouping becomes pure.** `group_names()` and `should_notify_group()` take plain dataclasses,
  so the label and send-rule cases in `test_multi_recipient.py` become unit tests with no DB.
  The handful that are genuinely about the database (a shared `Msg` row across destinations,
  re-run dedupe, dry run, separate DB files, concurrent sessions) stay as real-SQLite
  integration tests, which is what CLAUDE.md says they need.

The per-type presentation (`subject_prefix`, `banner_text`, `banner_html`,
`webhook_item_label`) currently lives as class attributes on the ORM models. That is what lets
`notify()` treat messages and announcements identically, and it should survive, but as a
`NotificationStyle` carried on `StoredItem` rather than on the ORM class. Rendering can then be
tested without SQLAlchemy in the room, and `send_test_notification`'s detached-`Msg` hack
(problem 9) goes away: it builds a real `StoredItem`.

A possible follow-up, deliberately *not* part of this plan: one `MessageStore` per DB *file*
rather than per user. Users sharing the default `db_name` would then share a session, which
removes the many-sessions-on-one-file situation instead of managing it. It changes how users map
to sessions, so it belongs in its own change after the move.

**`pipeline/`: the two-phase run, minus the wiring.** `collect_user()`, `notify_collected()` and
friends move here as they are. Two additions:

- `collect_all(users, open_scraper, open_store) -> list[UserCollection]` takes the per-user
  loop out of `main()`, including the guard that logs a failing user and carries on and the
  `sleep_between_librus_users` pause (injectable, so tests don't sleep). The tests' `run()`
  helper and the hand-copied `contextlib.suppress` then call the real loop (problem 7).
- `needs_attachment_content(config, notify) -> bool` gives a name to the inline expression in
  `collect_user()` (`pylibrus.py:1258`). CLAUDE.md calls it "the single place deciding whether
  attachment bytes need to be downloaded at all"; as a named pure function it gets a three-row
  test (email, webhook+librus_link, webhook+s3) instead of a comment asking people to keep it
  in sync.

`cli.py` keeps only argument parsing, logging setup and the `ExitStack` that opens stores and
scrapers. Returning a small `RunSummary` (counts of collected/sent/skipped per destination) from
`notify_collected()` also gives tests something to assert on besides log output.

**`notify/render.py` vs `notify/{email,webhook}.py`: pure vs I/O.** Today the Polish-charset
sender encoding (`=?utf-8?B?...`), the `display_name` label, the two banner variants, the
HTML/plain alternative parts and the attachment link markup all sit inside `send_email()`, one
line above `smtplib.SMTP(...)`. Split them and each becomes a one-line assert:

```python
def test_subject_uses_the_display_name():
    msg = build_email_message(item, user, display_name="Ania")
    assert str(make_header(decode_header(msg["Subject"]))) == "[LIBRUS Ania] Temat"

def test_announcement_uses_announcement_banner():
    assert "TO OGŁOSZENIE Z LIBRUSA" in plain_part_of(build_email_message(announcement, user))
```

The `FakeSMTP` class and the `requests.post` monkeypatch in `test_multi_recipient.py:470-541`
reduce to those calls.

**`db/migrations.py`.** The hand-rolled migration block becomes data:

```python
MIGRATIONS = [
    Migration(table="attachments", column="s3_key", ddl="ALTER TABLE attachments ADD COLUMN s3_key TEXT"),
    ...
]
```

That lets a test create a DB at the old schema, run `apply(engine)`, and assert the columns
exist. Today that path only ever runs against a real user's aged SQLite file.

**`settings.py`: declare each setting once.** Replace the mirrored `from_config`/`from_env`
pairs with a per-field spec:

```python
@dataclass
class Setting:
    name: str
    env: str
    parse: Callable[[str], Any] = str
    default: Any = None
    destination: bool = False   # part of Notify.destination_key()
```

`from_config()`, `from_env()` and `destination_key()` all walk the spec, and None-means-default
is applied once, in the spec walker, for every class. All three sync hazards in CLAUDE.md stop
being conventions and become tests:

```python
def test_every_setting_is_reachable_from_both_ini_and_env():
    for cls in (PyLibrusConfig, LibrusUser, EmailNotify, WebhookNotify):
        for field in dataclasses.fields(cls):
            assert field.name in SPEC[cls]
```

The deliberate exception has to survive and be visible in the spec rather than in a comment:
`WebhookNotify` marks only `webhook` as `destination=True`, never `webhook_attachments_source`
(including it would post two copies of every shared message to one webhook).

**Entry point.** Add to `pyproject.toml`:

```toml
[project.scripts]
pylibrus = "pylibrus.cli:main"
```

and change `Procfile` from `uv run src/pylibrus/pylibrus.py` to `uv run pylibrus`. Running a file
by path stops working once the code is a package.

## 3. Test layout

Where today's tests end up is noted on the right.

```
tests/
    conftest.py                     # config fixtures (exists) + FakeSession, FakeNotifier, user factories
    fixtures/                       # HTML captured from Librus
        inbox.html, message.html                       # new: the two untested parsers
        wiadomosc_z_zalacznikami.html, ogloszenia*.html  # exist
    unit/
        test_parse_inbox.py         # new: read/unread detection, ordering, rows without <a>
        test_parse_message.py       # new: header extraction, date parsing
        test_parse_announcements.py # <- test_announcements.py, minus the monkeypatching
        test_attachment_download.py # <- test_attachment_download.py, fake_get -> FakeSession
        test_cookies.py             # <- test_cookie_file.py
        test_store.py               # <- test_notifier_workdir.py, + mark_sent / write-through
        test_migrations.py          # new: old schema -> apply() -> columns present
        test_grouping.py            # <- label/send-rule cases of test_multi_recipient.py, no DB
        test_render_email.py        # <- FakeSMTP case of test_multi_recipient.py, + banners, links
        test_render_webhook.py      # <- requests.post case of test_multi_recipient.py
        test_settings.py            # <- config-parsing cases of test_multi_recipient.py,
                                    #    + spec coverage, destination_key exclusions
        test_s3.py                  # new: key building, sanitising, content-disposition
    integration/
        test_two_phase_run.py       # <- DB-behaviour cases of test_multi_recipient.py,
                                    #    now through collect_all(): shared Msg row, re-run dedupe,
                                    #    dry run, separate DB files, concurrent sessions, scrape failure
        test_announcements_run.py   # <- test_handle_announcements.py
```

Everything above is offline. The only things left uncovered are the OAuth login sequence and the
real SMTP/S3 calls. That is appropriate, since those are integration points with third parties.

## 4. Migration order

Each step is a separate commit that leaves `uv run ruff check .` and `uv run pytest` green.

0. **Characterise what isn't covered yet.** Orchestration, attachments, cookies and the workdir
   guard already have tests. What's missing is fixture-based tests against the *current* code
   for inbox parsing and message parsing (via the same `_get` monkeypatch the existing scraper
   tests use), plus a fuller email-render characterisation than the one `Subject` assertion we
   have (HTML part, banner, attachments-as-links), using the existing `FakeSMTP` pattern. These
   are the safety net. They get rewritten in later steps but must keep asserting the same values.
1. **Pure helpers out.** `str_to_bool`, `str_to_int`, `retrieve_from`, `announcement_id` →
   `utils.py`; `parse_s3_source`, `sanitize_s3_segment`, `build_download_content_disposition`,
   `S3AttachmentStorage` → `storage/s3.py`. Zero-risk moves.
2. **`db/`.** Move the ORM models, extract `MessageStore` from `LibrusNotifier`'s DB half
   (keeping `commit()` exactly as is for now), turn `_migrate_tables` into `migrations.py`.
3. **`scraper/`.** Move the class verbatim first (one commit), then split `parse.py` out of it
   (second commit), then introduce `LibrusSession` and the injection point (third). Keeping these
   apart means a bisect can tell "the move broke it" from "the split broke it".
4. **`notify/`.** Extract `render.py`, then `SmtpNotifier`/`WebhookNotifier` behind the protocol.
5. **`pipeline/` as a move.** `collect_user`, `collect_announcements`, the grouping functions and
   `notify_collected` move unchanged, still carrying ORM rows. Extract `collect_all()` from
   `main()` and point the tests' `run()` helper and the scrape-failure test at it.
6. **Values across the phase boundary.** Introduce `StoredItem`, make the store write-through,
   switch `CollectedItem` and dispatch over, and split `test_multi_recipient.py` into
   `unit/test_grouping.py` and `integration/test_two_phase_run.py`. This is the riskiest step,
   because it changes how CLAUDE.md's constraints 2 and 3 are upheld, so it goes alone, after
   everything else has settled, with the existing multi-recipient tests passing unmodified
   *before* the split.
7. **`settings.py` spec**, including `destination_key()`.
8. **`cli.py`, then drop the shim.** Until here `pylibrus.py` stays in place as a shim
   re-exporting the public names, so `uv run src/pylibrus/pylibrus.py` and existing imports keep
   working. Now add the console script and update `Procfile`, `README.md` and `CLAUDE.md`: the
   "entire application is one file … no test suite" line (already stale on master), the
   architecture section, and the three keep-in-sync notes that step 7 makes obsolete.

## 5. What deliberately does not change

- The OAuth `__enter__` sequence and every comment explaining why a redirect or referer is
  needed. It moves; it is not rewritten.
- Column names and table names. No schema change, so existing users' SQLite files keep working.
- The run's shape: collect every user, then notify; users in config order (the representative
  per destination group, which fixes the sending account and the label order, must not vary
  between runs); a user whose scrape fails is logged and skipped.
- `"unread"` mode deciding purely from Librus's unread state and ignoring `email_sent`, so an
  unread item is re-sent each run. CLAUDE.md is explicit that this must not be "fixed".
- `destination_key()` semantics, in particular `WebhookNotify` keying on the URL alone.
- `always_include_in_subject` being labelling only: it never reaches grouping, the send rule or
  the choice of representative, and logs always name the full recipient list.
- `LibrusAnnouncement` sharing `Msg`'s attribute names, and the announcement dedupe rules
  documented in `ANNOUNCEMENTS_PLAN.md`.
- The `announcement_id()` hash (changing it re-sends every announcement).
- CLI flags and INI/env key names.

## 6. Cost

Roughly 1550 lines redistributed across ~22 modules. The existing ~1100 lines of tests get
mostly relocated rather than rewritten, plus ~400 new lines for the parsers, rendering, settings
and migrations. Steps 0-5 are mechanical and low-risk; step 3 needs care, which is why it is
split into three commits, and step 6 is the one genuine redesign, which is why it goes alone.
The payoff is that the two things that actually break in production, Librus changing its markup
and notification rendering, become the best-covered code in the project instead of the least,
and two of the three invariants CLAUDE.md asks people to remember become structural.
