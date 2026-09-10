# Refactor plan: from one file to testable modules

`src/pylibrus/pylibrus.py` is ~1300 lines holding config parsing, ORM models, HTTP/OAuth,
HTML scraping, S3 uploads, MIME rendering, SMTP/webhook delivery, orchestration and the CLI.
Nothing is *wrong* with it, but the pieces most likely to break (Librus markup parsing,
Polish-charset email rendering, config precedence) are the pieces hardest to reach from a
test, because they only exist behind a network call or an SMTP connection.

This document proposes a target layout, explains the seams that make each part testable, and
gives an incremental migration order where every step leaves the tree green.

## 1. What blocks testing today

| # | Problem | Where | Consequence |
|---|---------|-------|-------------|
| 1 | Constructing a scraper does I/O-adjacent work: `logging.basicConfig()`, global `http_client.HTTPConnection.debuglevel`, cookie file read | `LibrusScraper.__init__` | Every test that builds a scraper reconfigures the root logger for the whole session |
| 2 | `requests.session()` is created internally, with no injection point | `LibrusScraper.__init__` | `tests/test_announcements.py` has to monkeypatch the private `scraper._get` |
| 3 | Parsing and fetching are fused | `fetch_msg`, `fetch_announcements`, `fetch_attachments` | The brittle part (BeautifulSoup selectors tied to Librus markup) can only be tested through a fake transport |
| 4 | The scraper returns ORM objects (`Attachment`) and an untyped 6-tuple | `fetch_msg`, `fetch_attachments` | Scraping is coupled to SQLAlchemy; the tuple has to be unpacked positionally by the caller |
| 5 | `LibrusNotifier` is three things at once: DB session owner, renderer, and transport | `LibrusNotifier` | Rendering (pure, and the part with tricky UTF-8/Polish handling) needs a live SMTP server to exercise |
| 6 | `handle_user` constructs its collaborators inline | `handle_user` | The message loop can't run against fakes — note `handle_announcements` *does* take them as parameters, and is consequently the only orchestration covered by tests |
| 7 | `from_config()` / `from_env()` are hand-mirrored per dataclass | 4 dataclasses | CLAUDE.md has to warn "keep both in sync"; nothing enforces it |
| 8 | Test payloads are detached ORM rows | `send_test_notification` | A `Msg(...)` with `folder="Odebrane"` (a string in an Integer column) is used as a fake DTO |

### Latent bugs these seams are hiding

Found while reading, all three are the kind a unit test would have caught immediately:

- `pylibrus.py:741` — `get_attach_resp` is never initialised before the branch chain at
  `pylibrus.py:721-739`. If an attachment page has no `singleUseKey` and no `onload` redirect,
  `if get_attach_resp is not None` raises `NameError`. Worse, on the *second* attachment of a
  message the name is still bound from the previous iteration, so a stale response is read.
- `pylibrus.py:496` — `if not self._cookie_path.exists:` is missing the call parens. A bound
  method is always truthy, so the "does not exist" debug line can never fire.
- `pylibrus.py:864` — same missing parens on `workdir_path.exists`, so the
  `RuntimeError(f"Workdir {workdir_path} does not exist")` guard never triggers; the code
  proceeds to `create_engine` against a nonexistent directory instead.

## 2. Target layout

```
src/pylibrus/
    __init__.py            # version only, no side effects
    __main__.py            # python -m pylibrus -> cli.main()
    cli.py                 # argparse, logging setup, wiring. No business logic.
    app.py                 # run_for_user() / run_announcements(): orchestration over protocols
    logging_setup.py       # configure_logging(debug) - called from cli only
    settings.py            # PyLibrusConfig, LibrusUser, EmailNotify, WebhookNotify + field spec
    config_loader.py       # read_pylibrus_config(): INI vs env source selection
    models.py              # domain dataclasses (no SQLAlchemy): ParsedMessage, ParsedAnnouncement,
                           #   AttachmentRef, Notification
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
        store.py           # MessageStore: get/add/mark_sent/attachments_for; owns the Session
    notify/
        __init__.py
        base.py            # Notifier protocol
        render.py          # build_email_message() -> EmailMessage, build_webhook_payload() -> dict. Pure.
        email.py           # SmtpNotifier: render + smtplib
        webhook.py         # WebhookNotifier: render + requests.post
    storage/
        __init__.py
        s3.py              # S3AttachmentStorage, key building, pre-signed links
```

Roughly: three packages for the three subsystems the request names (scraper, notify, db),
flat modules for everything else. Nothing nests more than two levels.

### Why each split earns its place

**`scraper/parse.py` — the biggest win.** Pure functions, HTML string in, dataclass out:

```python
def parse_inbox(html: str) -> list[InboxEntry]: ...          # (path, read) pairs
def parse_message(html: str) -> ParsedMessage: ...           # sender/subject/date/contents
def parse_attachment_rows(html: str) -> list[AttachmentRef]: ...
def parse_announcements(html: str) -> list[ParsedAnnouncement]: ...
```

These are exactly the functions that break when Librus reshuffles a `<td>`, and after the
split a regression test is `assert parse_inbox(FIXTURE) == [...]` with no fakes at all.
`tests/test_announcements.py` currently needs a scraper instance, a tmp_path, a config object
and a monkeypatched private method to assert on parser output; it collapses to a direct call.

Note the age filter currently lives inside `fetch_msg` (`pylibrus.py:757`) — it moves out to
`app.py`, so the parser stays a parser and "too old to send" becomes a policy decision testable
on its own.

**`scraper/session.py` — the injection seam.** The OAuth sequence in `__enter__` moves here
*verbatim*, comments included; it is reverse-engineered and must not be "cleaned up". What
changes is only that `LibrusScraper` takes a session object rather than building one:

```python
class LibrusScraper:
    def __init__(self, session: LibrusSession, config: PyLibrusConfig): ...
```

Tests pass a `FakeSession` with a `get`/`post` returning canned responses, and constructing a
scraper stops touching the root logger or the filesystem (problems 1 and 2).

**`models.py` — dataclasses at the boundaries.** The scraper stops returning ORM rows and
6-tuples; `db/store.py` maps domain objects onto `Msg`/`Attachment`/`LibrusAnnouncement`.

The per-type presentation currently lives as class attributes on the ORM models
(`subject_prefix`, `banner_text`, `banner_html`, `webhook_item_label`). That trick is what lets
`notify()` treat messages and announcements identically, and it should survive — but as a
`NotificationStyle` on the domain object rather than on the ORM class, so rendering can be
tested without SQLAlchemy in the room. `send_test_notification`'s detached-`Msg` hack (problem
8) then goes away: it builds a real `Notification`.

**`notify/render.py` vs `notify/{email,webhook}.py` — pure vs I/O.** Today the Polish-charset
sender encoding (`=?utf-8?B?...`), the two banner variants, the HTML/plain alternative parts and
the attachment link markup are all inside `send_email()`, one line above `smtplib.SMTP(...)`.
Split them and each becomes a one-line assert:

```python
def test_sender_header_encodes_polish_name():
    msg = build_email_message(notification, user)
    assert msg["From"].startswith('"=?utf-8?B?')

def test_announcement_uses_announcement_banner():
    assert "TO OGŁOSZENIE Z LIBRUSA" in plain_part_of(build_email_message(announcement, user))
```

**`db/store.py` + `db/migrations.py`.** The hand-rolled migration block becomes data:

```python
MIGRATIONS = [
    Migration(table="attachments", column="s3_key", ddl="ALTER TABLE attachments ADD COLUMN s3_key TEXT"),
    ...
]
```

so a test can create a DB at the old schema, run `apply(engine)`, and assert the columns exist —
today that path only runs against a real user's aged SQLite file.

**`settings.py` — declare each setting once.** Replace the mirrored `from_config`/`from_env`
pairs with a per-field spec:

```python
@dataclass
class Setting:
    name: str
    env: str
    parse: Callable[[str], Any] = str
    default: Any = None
```

`from_config()` and `from_env()` both walk the spec. The sync hazard CLAUDE.md warns about stops
being a convention and becomes a test:

```python
def test_every_setting_is_reachable_from_both_ini_and_env():
    for field in dataclasses.fields(PyLibrusConfig):
        assert field.name in SETTINGS_BY_NAME
```

**`app.py` — orchestration over protocols.**

```python
def run_for_user(config, user, scraper: ScraperProtocol, notifier: NotifierProtocol,
                 store: MessageStore, dry_run: bool = False) -> RunSummary: ...
```

`cli.py` owns the `with LibrusScraper(...) as s, MessageStore(...) as store:` wiring. The message
loop then gets the same treatment `handle_announcements` already has — and the existing
`tests/test_handle_announcements.py` is proof the pattern works here. Returning a small
`RunSummary` (counts of fetched/sent/skipped) also gives tests something to assert on besides
log output.

**Entry point.** Add to `pyproject.toml`:

```toml
[project.scripts]
pylibrus = "pylibrus.cli:main"
```

and change `Procfile` from `uv run src/pylibrus/pylibrus.py` to `uv run pylibrus`. Running a file
by path stops working once the code is a package.

## 3. Test layout

```
tests/
    conftest.py                     # tmp workdir, config/user factories, FakeSession, FakeNotifier
    fixtures/                       # HTML captured from Librus (already has ogloszenia*.html)
        inbox.html, message.html, message_with_attachments.html, ogloszenia*.html
    unit/
        test_parse_inbox.py         # read/unread detection, ordering, rows without <a>
        test_parse_message.py       # header extraction, date parsing
        test_parse_announcements.py # (existing test, minus the monkeypatching)
        test_render_email.py        # charset, banners, subject prefix, attachment links
        test_render_webhook.py      # Slack payload shape, label per item type
        test_settings.py            # INI/env precedence, validation errors, both-sources coverage
        test_s3.py                  # key building, sanitising, content-disposition
        test_migrations.py          # old schema -> apply() -> columns present
    integration/
        test_run_for_user.py        # fake scraper + fake notifier + real SQLite: dedupe,
                                    #   unread vs unsent, dry-run leaves email_sent untouched
        test_run_announcements.py   # (existing test, relocated)
```

Everything above is offline. The only things left uncovered are the OAuth login sequence and the
real SMTP/S3 calls — appropriate, since those are integration points with third parties.

## 4. Migration order

Each step is a separate commit that leaves `uv run ruff check .` and `uv run pytest` green.

0. **Characterise first.** Add fixture-based tests against the *current* code for inbox parsing,
   message parsing and email rendering (the last one via a monkeypatched `smtplib.SMTP` capturing
   `sendmail`). These are the safety net; they get rewritten in later steps but must keep asserting
   the same values.
1. **Pure helpers out.** `str_to_bool`, `str_to_int`, `retrieve_from`, `announcement_id` →
   `utils.py`; `parse_s3_source`, `sanitize_s3_segment`, `build_download_content_disposition`,
   `S3AttachmentStorage` → `storage/s3.py`. Zero-risk moves.
2. **`db/`.** Move the ORM models, extract `MessageStore` from `LibrusNotifier`'s DB half, turn
   `_migrate_tables` into `migrations.py`.
3. **`scraper/`.** Move the class verbatim first (one commit), then split `parse.py` out of it
   (second commit), then introduce `LibrusSession` and the injection point (third). Keeping these
   apart means a bisect can tell "the move broke it" from "the split broke it".
4. **`notify/`.** Extract `render.py`, then `SmtpNotifier`/`WebhookNotifier` behind the protocol.
5. **`app.py` + `cli.py`.** Move `handle_user`/`handle_announcements`/`parse_args`/`main`. Leave
   `pylibrus.py` in place as a shim re-exporting the public names, so the current
   `uv run src/pylibrus/pylibrus.py` invocation and any existing imports keep working.
6. **Drop the shim.** Add the console script, update `Procfile`, `README.md` and `CLAUDE.md`
   (the "entire application is one file" line, the architecture section, and the
   "keep from_config/from_env in sync" note, which step 1's spec makes obsolete).

Fixing the three bugs from §1 belongs in their own commits, before the move that touches them,
so the fix is visible in the diff rather than buried in a file rename.

## 5. What deliberately does not change

- The OAuth `__enter__` sequence and every comment explaining why a redirect or referer is
  needed. It moves; it is not rewritten.
- Column names and table names — no schema change, so existing users' SQLite files keep working.
- `LibrusAnnouncement` sharing `Msg`'s attribute names, and the announcement dedupe rules
  documented in `ANNOUNCEMENTS_PLAN.md`.
- The `announcement_id()` hash (changing it re-sends every announcement).
- CLI flags and INI/env key names.

## 6. Cost

Roughly 1300 lines redistributed across ~18 modules, plus ~400 lines of new tests. Steps 0-2 are
mechanical and low-risk; step 3 is where the care is needed, which is why it is split into three
commits. The payoff is that the two things that actually break in production — Librus changing
its markup, and notification rendering — become the best-covered code in the project instead of
the least.
