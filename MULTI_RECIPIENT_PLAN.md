# Plan: annotate forwarded notifications with *all* children a message was sent to

Status: proposal / design document. Nothing in this document is implemented yet.

---

## 1. The problem

`main()` (`pylibrus.py:1281`, loop at `pylibrus.py:1296`) walks the configured Librus users one at a time and calls
`handle_user()`, which **scrapes and notifies in the same pass**:

```python
for librus_user in shuffled(librus_users):
    handle_user(pylibrus_config, librus_user, dry_run=args.dry)   # scrape AND notify
    time.sleep(pylibrus_config.sleep_between_librus_users)
```

When the school sends one message to several children, every child's account receives it.
Because `Msg.url` is the primary key and the users share a DB file by default (`db_name`
falls back to `[global]`, `pylibrus.py:253`), the second user finds the row already present
with `email_sent=True` and skips it (`pylibrus.py:1192`). Exactly one notification goes out —
good — but it is annotated with whichever child happened to be processed first:

```
Subject: [LIBRUS Ania] Zebranie z rodzicami
```

The reader cannot tell whether this concerns Ania only, or Ania *and* Jaś.

### 1.1 Why this is not fixable by a schema change alone

At the moment we send the notification for Ania, Jaś has not been scraped yet. The
information "Jaś got this too" **does not exist anywhere** — not in memory, not in the DB,
not in Librus's response we have already read. There is a `time.sleep()` between the two
users. No column, table or index can answer a question about a page that has not been
fetched.

The run therefore has to be split into a **collect phase** and a **notify phase**. That is
the substance of this change; everything else is bookkeeping.

### 1.2 What `send_message=unread` does, and why it stays that way

The dispatch at `pylibrus.py:1192` only consults `email_sent` in `unsent` mode:

```python
if pylibrus_config.send_message == "unsent" and msg.email_sent:
    skip
elif pylibrus_config.send_message == "unread" and read:
    skip
else:
    notify
```

That is **intentional and is preserved by this plan**: in `unread` mode the send decision is
taken from the unread state on the scraped service, not from our own `email_sent` bookkeeping.
`email_sent` is the `unsent` mode's signal; `unread` mode deliberately ignores it.

The practical consequence is worth spelling out, because it interacts with the grouping below.
`read` comes from *that user's* inbox listing. The first user's `fetch_msg()` GETs the message
page, which marks it read **in that account only**; for a second user whose row already exists
`fetch_msg()` is never called, so that account's copy stays unread and the item is re-notified
on every cron tick until a human opens it in Librus. That is the mode working as designed —
it repeats until somebody actually reads it.

What the grouping *does* improve here is volume. Today, three children who all have the item
unread produce **three** notifications per tick, each labelled with one child. Under D4 they
produce **one** grouped notification per tick, labelled with all three.

---

## 2. Design decisions

### D1 — Split the run into collect → notify, within a single run

Phase 1 visits every user and records what they received, writing `Msg` /
`LibrusAnnouncement` rows exactly as today but **sending nothing**. Phase 2 then groups the
collected items and sends.

Rejected alternative — *defer one cron tick*: keep the loop as-is, never notify an item
first seen in this run, send it on the next tick once every user's view is recorded. Smaller
diff, but adds a whole cron period (~5 min) of latency on top of the scrape time and needs a
persisted "first seen" timestamp. Not worth it.

**Cost:** notifications now arrive after *all* users have been scraped, i.e. up to
`sleep_between_librus_users x (n_users - 1)` later than today — about 3 minutes for two
children at the 180 s used in `pylibrus.ini.example`. Accepted.

### D2 — Group in memory, no new table and no new column

The grouping lives in a run-scoped dict and is discarded when the process exits. Nothing is
persisted about *who* received what.

Rejected alternative — a `msg_recipients` association table: durable, would allow correcting
a notification on a later run, and would survive a partial scrape. Rejected as more machinery
than this problem justifies; see §5.1 for the limitation this accepts.

**A pleasant side effect:** because grouping is in memory and no longer relies on the DB row
being shared, this works *regardless* of whether the users share a `db_name` or each have
their own SQLite file. Today, per-user DB files disable cross-user dedupe entirely.

### D3 — Identity: two items are "the same" iff their `url` is equal

For messages that is the Librus `msg_path`; for announcements it is the synthetic
`announcement_id(title, author, date)` (`pylibrus.py:347`), which is identical across children
by construction.

URL equality across sibling accounts has been **verified in practice against a year of
received messages**. It is not a documented guarantee — Librus is scraped, not APIed, and
could change the path scheme at any time. If that happens, this change degrades to a no-op
(each child's copy becomes its own group and we are back to today's behaviour with one email
per child) rather than misbehaving. The fix at that point would be to swap the key function
for a content hash, e.g. `sha1(sender|subject|date)` in the style of `announcement_id()` —
a single-function change. We will deal with it if and when it happens.

### D4 — One notification per (destination, item), labelled with that destination's children

Recipients are grouped first by **destination**, then by item URL. Each destination receives
one notification per item, annotated with the names of *its own* children who received it.

The motivating case is not one-child-per-destination. With, say, two children forwarded to
destination A and three to destination B, each destination gets a single grouped,
deduplicated notification naming the children **that destination cares about**. Telling
destination A about the three children it is not configured for would be noise, not
information.

Worked example — message sent to Ania and Jaś (→ webhook A) and to Zosia (→ email B):

| Destination | Copies sent | Label |
|---|---|---|
| webhook A | 1 | `LIBRUS Ania, Jaś` |
| email B | 1 | `[LIBRUS Zosia]` |

And the common single-destination case is unchanged in shape: one copy, `[LIBRUS Ania, Jaś]`.

Rejected alternatives:
- *One copy per destination naming **all** children* — identical machinery, but leaks names
  of children a destination is not configured for.
- *One copy total, first user's destination wins* — children on other destinations silently
  receive nothing, and `main()` shuffles the user order (`4754f06`), so which destination wins
  varies run to run.

**Group send rule** (applied per destination group, per item) — a faithful per-group lift of
today's per-message dispatch, with no change to either mode's criterion:
- `send_message=unsent`, and announcements always: notify if **no** member of the group has
  `email_sent` set.
- `send_message=unread`: notify if **at least one** member is unread. `email_sent` is
  deliberately **not** consulted, per §1.2 — the scraped unread state is the whole signal in
  this mode, so an item that stays unread for any child in the group keeps being sent.
- After sending, `email_sent = True` is set on the row for **every** member of the group. In
  `unread` mode that flag is written but not read back, exactly as today; it stays meaningful
  if the config later switches to `unsent`.

**The label is the recipient list, not the unread list.** A group is labelled with every child
of that destination who *received* the item, regardless of who has read it. In `unread` mode a
re-send on a later tick therefore carries the same label as the first send. Labelling only the
still-unread children was considered and rejected: the label would then change between ticks
(`Ania, Jaś` then `Jaś`), reintroducing exactly the "who is this about?" ambiguity this change
exists to remove.

### D5 — Reuse the existing notification path; parameterise only the displayed name

`self._librus_user.name` is interpolated in exactly two places: the email subject
(`pylibrus.py:1044`) and the webhook header line (`pylibrus.py:1011`). `notify()` grows an
optional `display_name` argument that both fall back from:

```python
def notify(self, msg_from_db, display_name: str | None = None):
    name = display_name or self._librus_user.name
```

Nothing else in `send_email()` / `send_via_webhook()` / the attachment paths changes.
Names are joined with `", "` → `[LIBRUS Ania, Jaś] Zebranie z rodzicami`.

### D6 — Destination identity, and the representative sender

`Notify` gains a `destination_key()`:

- `EmailNotify` → `("email", smtp_server, smtp_port, smtp_user, tuple(sorted(email_dest)))`
- `WebhookNotify` → `("webhook", webhook)`

This joins the list of things in `CLAUDE.md` that must be kept in sync when a notification
setting is added.

`LibrusNotifier` is bound to one `LibrusUser` (it owns the SMTP credentials, the S3 config
and the DB session), so each destination group picks a **representative** — the first member
**in config order**.

This needs carrying explicitly, and is easy to get wrong. `main()` shuffles a *copy* of the
user list (`librus_users.copy()`, `pylibrus.py:1294`) deliberately, so that Librus is not
always hit in the same order (`4754f06`). Phase 1 therefore produces `UserCollection`s in
*shuffled* order, and grouping over that order would pick a different representative — hence a
different sending SMTP account, a different S3 config and a different name order in the label
— on every run. So `UserCollection` carries a `config_index` (its position in the unshuffled
`librus_users`), and phase 2 sorts by it before bucketing. The shuffle keeps affecting only
the scrape order, which is all it was ever for.

Two deliberate simplifications:
- S3 settings are **not** part of the destination key. Two users on the same webhook with
  different `webhook_attachments_source` would otherwise be split into two groups and both
  copies would land on the same webhook — worse than the alternative. The representative's S3
  config is used; log a warning when grouped users disagree on `webhook_attachments_source`.
- With S3 attachments, `build_object_key()` (`pylibrus.py:412`) embeds the user name, so a
  shared message's object is keyed under the representative. Harmless; the key is a hash-path,
  not a display string.

### D7 — Commit each user's session at the end of its collect

Today only one `LibrusNotifier` session exists at a time. Phase 2 needs the sessions of *all*
users still open, which introduces a real hazard: SQLAlchemy defers `BEGIN` until the first
DML, so user 1's uncommitted `INSERT`s hold a write lock on the shared SQLite file while
user 2 tries to insert → `database is locked`.

So: `LibrusNotifier` gains an explicit `commit()`, called at the end of **each** user's
collect phase and after **each** notification in phase 2, so that no session ever holds an
open write transaction while another user's session writes. The existing
commit-on-clean-`__exit__` (`pylibrus.py:902`) stays as a backstop.

Note this is only a hazard because the users share one SQLite *file* by default; it is not a
hazard the current code can hit, since today exactly one notifier exists at a time. It is
introduced by D1 and must be closed in the same commit. §4 test 12 covers it against a real
shared file rather than a mock, since an in-memory or per-test-DB fake would never reproduce
the lock.

### D8 — A failed user does not block the others

Phase 1 wraps each user in `try/except`, logs the failure with `logger.exception`, and
continues; phase 2 then runs over whoever was collected successfully.

Rejected alternative — abort the whole run if any user fails, so that no notification is ever
sent on partial data. Correctness-maximal, but one permanently broken account (changed
password) would then silently block notifications for every other child, which is a worse
failure than a mislabelled subject line. Note that today a mid-run exception already kills the
remaining users outright, so this is an improvement either way. See §5.1.

### D9 — `--dry` reports the grouping

Phase 2 is skipped entirely in dry-run mode; instead each group logs what it *would* have
sent, including the recipient list and destination. `email_sent` is untouched, as today
(`pylibrus.py:1185`). This makes `--dry` the natural way to verify the change against a live
account without sending anything.

### D10 — Deliver in date order

Within a destination group, items are sorted by `date` before sending, so a multi-child run
delivers chronologically instead of interleaving per-user scrape order. Today's per-user
ordering (`msgs.reverse()`, `pylibrus.py:795`; `reversed(announcements)`,
`pylibrus.py:1221`) is oldest-first, so this preserves the existing intent.

---

## 3. Implementation steps

### Step 1 — `destination_key()` on the notify dataclasses (`pylibrus.py:124`–`229`)

Add to `Notify`, `EmailNotify`, `WebhookNotify` per D6.

### Step 2 — `display_name` on the notification path (`LibrusNotifier`, `pylibrus.py:947`)

- `notify(self, msg_from_db, display_name=None)`
- thread it through to `send_email()` and `send_via_webhook()`, replacing the two
  `self._librus_user.name` interpolations (`pylibrus.py:1011`, `pylibrus.py:1044`).
- add `LibrusNotifier.commit()` (D7).

### Step 3 — Collect phase

Introduce small run-scoped records (plain dataclasses, no ORM):

```python
@dataclasses.dataclass
class CollectedItem:
    item: Msg | LibrusAnnouncement   # the ORM object, bound to this user's session
    read: bool | None                # None for announcements

@dataclasses.dataclass
class UserCollection:
    librus_user: LibrusUser
    notifier: LibrusNotifier
    config_index: int                # position in the unshuffled librus_users list, see D6
    items: list[CollectedItem]
```

`collect_user(pylibrus_config, librus_user, scraper, notifier) -> UserCollection` takes the
scraper and notifier as **parameters** — mirroring `handle_announcements()`
(`pylibrus.py:1214`), which is the only currently unit-testable part of the pipeline — so the
tests can inject fakes. It contains today's `handle_user()` body from `msgs_from_folder()`
down to `add_msg()`, minus every `notify()` / `email_sent` line, plus the announcement
collection (keeping the broad `try/except` around announcements, `pylibrus.py:1208`). It ends
with `notifier.commit()` (D7).

Taking the scraper and notifier as arguments is what makes this testable at all: today
`handle_user()` constructs `LibrusScraper` itself (`pylibrus.py:1154`), which is why no test
covers it, while `handle_announcements()` — which receives both — has a test file of its own.
Phase 2 is then testable without any fake at all, since `notify_collected()` consumes plain
`UserCollection` records.

### Step 4 — Notify phase

`notify_collected(pylibrus_config, collections, dry_run=False)`:

1. sort `collections` by `config_index` (D6), then bucket by
   `librus_user.notify.destination_key()`;
2. within each bucket, bucket `CollectedItem`s by `item.url`;
3. sort groups by `item.date` (D10);
4. apply the D4 send rule; on send, use the representative's notifier (the group's first
   member, now genuinely in config order) with `display_name=", ".join(names)`, then set
   `email_sent` on every member and `commit()` each affected session (D7);
5. warn when members of one webhook group disagree on `webhook_attachments_source` (D6).

### Step 5 — Rewire `main()` (`pylibrus.py:1281`)

```python
config_index = {user.name: i for i, user in enumerate(librus_users)}   # unshuffled, D6

with contextlib.ExitStack() as stack:
    collections = []
    for librus_user in users_to_handle:          # still shuffled: scrape order only
        try:
            notifier = stack.enter_context(LibrusNotifier(pylibrus_config, librus_user))
            with LibrusScraper(...) as scraper:
                collections.append(collect_user(
                    pylibrus_config, librus_user, scraper, notifier,
                    config_index=config_index[librus_user.name],
                ))
        except Exception:
            logger.exception(f"Failed to collect for {librus_user.name}")   # D8
        if more users remain:
            time.sleep(pylibrus_config.sleep_between_librus_users)
    notify_collected(pylibrus_config, collections, dry_run=args.dry)
```

`config_index` is keyed on `name`, which is the `[user:<Name>]` section name and therefore
already unique per config.

Keep `handle_user()` as a thin single-user wrapper (`collect_user` + `notify_collected` on a
one-element list) so the existing entry point and its semantics survive.

### Step 6 — Docs

- `CLAUDE.md`: the **Architecture** section currently describes `handle_user()` as deciding
  per-message whether to notify — rewrite for the two-phase flow, the destination grouping and
  `destination_key()`'s sync requirement. Two pre-existing inaccuracies to fix while there:
  it claims "no test suite" (there is a `tests/` directory with 5 files) and "one DB file per
  user" (the default is one **shared** file via `[global] db_name`).
- `README.md` / `pylibrus.ini.example`: note that a message sent to several children produces
  one notification per destination listing that destination's children, and that notifications
  now arrive after all users have been scraped.

---

## 4. Verification

New `tests/test_multi_recipient.py`, following the existing fake-scraper / `RecordingNotifier`
convention from `tests/test_handle_announcements.py`:

1. Two users, same destination, same `msg.url` → **one** notify, `display_name == "A, B"`.
2. Two users, **different** destinations, same `msg.url` → **two** notifies, each labelled
   with only its own user (the D4 core case).
3. 2 users on destination A + 3 on destination B → one notify per destination, labelled
   `"A1, A2"` and `"B1, B2, B3"` respectively.
4. Message present for one user only → labelled with that user alone (no regression).
5. `unsent` mode, second run → nothing sent.
6. `unread` mode: item unread for user B and already `email_sent` → **is** re-sent, once,
   labelled with both children (§1.2 — pins the intended behaviour so a later refactor does
   not "fix" it into an `email_sent` check).
7. Announcements grouped identically, keyed on `announcement_id()`.
8. `--dry` → no notify calls, `email_sent` untouched on every member.
9. Groups delivered in `date` order (D10).
10. Users with **different** `db_name` files still group correctly (D2's side effect).
11. A user raising during collect does not prevent the others being notified (D8).
12. **Shared-DB locking (D7):** two users on the *same* `db_name` file, both collecting new
    messages, then both notified — must complete without `sqlite3.OperationalError: database
    is locked`. Use a real file in `tmp_path`, as `tests/test_handle_announcements.py`
    already does; a per-test or in-memory DB would not reproduce the lock.
13. **Representative determinism (D6):** with the user list passed in shuffled order, the
    chosen representative, the SMTP account used and the name order in the label are all
    identical across runs and follow config order, not scrape order.

Plus `uv run ruff check .` / `uv run ruff format .`, and a manual `--dry` run against the
live config to confirm the grouping the logs report matches expectations before a real send.

---

## 5. Risks and accepted limitations

### 5.1 No self-correction after a partial scrape — **accepted**

Because the grouping is in memory only (D2) and a failed user does not abort the run (D8):
if Ania is scraped successfully but Jaś's scrape fails (network, expired cookies, Librus
throttling), the notification goes out labelled `[LIBRUS Ania]` and every member's row is
marked `email_sent=True`. On the next tick Jaś's copy of that message is found already in the
DB and already sent, so the "actually this was for both" correction **never happens**.

This is understood and accepted as the price of not adding a recipients table. The blast
radius is one mislabelled subject line on an otherwise correctly delivered message, and the
failed scrape is logged at exception level. If it ever becomes a real annoyance, the fix is
D2's rejected alternative — persist recipients and re-evaluate on the next run.

The same applies, more benignly, if Librus surfaces a message in one child's account minutes
before the other's: whichever tick sees it first sends and labels it.

### 5.2 URL equality is an observed property, not a contract

See D3. Verified over a year of received messages; degrades to today's behaviour rather than
to a bug if Librus changes it.

### 5.3 Notification latency grows

See D1 — up to `sleep_between_librus_users x (n_users - 1)`.

### 5.4 `unread` mode keeps repeating until read — **by design, unchanged**

See §1.2 and D4's send rule. In `unread` mode an item that stays unread in any child's account
is re-sent every tick, because the send decision belongs to the scraped service's unread state
rather than to our `email_sent` flag. This plan deliberately does **not** change that; it only
collapses what used to be one notification per unread child per tick into one grouped
notification per tick.

Noted here so that it is on the record as a decision rather than an oversight: a future reader
finding `email_sent` written but never read in `unread` mode should leave it alone.

### 5.5 Sessions held open across the whole run

See D7. The explicit `commit()` after each phase is what keeps SQLite from locking; a future
change that adds DB writes inside the collect phase must not leave a transaction open.

---

## 6. Suggested commit sequence

1. `refactor: add destination_key() and display_name to the notification path` (Steps 1–2,
   no behaviour change, `display_name` unused).
2. `refactor: split handle_user() into collect and notify phases` (Steps 3–5, single-user
   behaviour identical).
3. `feat: label notifications with every child a message was sent to` (D4 grouping; both
   modes' send criteria unchanged from today).
4. `test: cover multi-recipient grouping and destination fan-out` (Step 4 / §4).
5. `docs: describe the two-phase run and destination grouping` (Step 6).
