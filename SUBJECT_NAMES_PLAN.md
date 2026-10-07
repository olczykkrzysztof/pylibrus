# Plan: hiding selected children's names from grouped notification labels

Status: proposal / design document. Nothing in this document is implemented yet. Extends the
feature designed in [`MULTI_RECIPIENT_PLAN.md`](MULTI_RECIPIENT_PLAN.md), whose decisions
(D1–D11 there) this document assumes and does not revisit.

---

## 1. What this adds

A message the school sends to several children currently produces one notification per
destination, labelled with every child of that destination who received it:

```
[LIBRUS Ania, Jaś] Zebranie z rodzicami
```

Some accounts add nothing to that label — a second parent login onto the same child, a
leftover account that still receives school-wide announcements, a monitoring account. Naming
them is noise, and the noise grows with the number of accounts. This change lets a per-user
config entry mark such an account as **not worth naming**, so the label above becomes:

```
[LIBRUS Ania] Zebranie z rodzicami
```

while the message is still deduplicated exactly as now — one copy, not two.

### 1.1 Suggested name for the config entry: `include_name_in_subject`

Per-user, in the `[user:<Name>]` section, boolean, **default `true`**:

```ini
[user:Jaś]
librus_user=
librus_pass=
; Whether this child's name appears in the notification label when a message was
; received by several children. Delivery and deduplication are unaffected.
include_name_in_subject=false
```

Why this name:
- **Positive sense, default true**, matching the file's existing booleans (`fetch_attachments`,
  `fetch_announcements`). Omitting it leaves every existing config behaving identically, and
  `include_name_in_subject=false` reads without a double negative.
- **"subject" is the word a reader will look for**, and the one used when this was requested.

Alternatives considered:

| Candidate | Why not |
|---|---|
| `include_in_subject` | Shorter and reads fine (`include_in_subject=false`), but leaves *what* is included implicit. Acceptable second choice. |
| `include_in_recipient_names` | Channel-neutral, but clunky, and "recipient" already means the email's `email_dest` elsewhere in the config. |
| `skip_name_in_subject` | Negative sense; `skip_name_in_subject=false` is a double negative and breaks the file's positive-default style. |
| `hide_name` / `anonymous` | Sound broader than they are — as if the name were hidden from the body or the logs too. |

One wart to accept: the label also appears in the **webhook** header, which has no "subject"
(see D4). The key governs both; the config comment says so.

---

## 2. Design decisions

### D1 — Labelling only; delivery and dedupe are untouched

The setting must not affect `Notify.destination_key()`, how items are grouped,
`should_notify_group()`, `CollectedItem.email_sent`, or which destinations receive a copy. A
hidden child is a full participant in collection and deduplication; only their *name* is left
out of the text.

This is the whole point, and it is easy to get wrong in a way that looks like a feature: if a
hidden child were instead excluded from the group, they would stop deduplicating against their
siblings and start receiving a *second, separate* copy of every shared message — the exact
problem `MULTI_RECIPIENT_PLAN.md` exists to solve.

### D2 — A name is hidden only while another name remains

`display_name` is built from the children of the group whose `include_name_in_subject` is true.
If that leaves **nothing**, the label falls back to the full recipient list (D3).

So a message that reached only hidden children still says whose it is. The alternative —
*always* hide, yielding `[LIBRUS] Zebranie` with no name at all — was rejected: in a
three-child setup where only Jaś is hidden, a message that reached Jaś alone would arrive with
no indication which child it concerned. Hiding is about removing redundancy from a list, not
about anonymising.

**Open question.** This is the one genuinely debatable decision here. If the intent is closer
to "this account must never be named anywhere", say so and D2/D3 become "always omit, and the
label degrades to `[LIBRUS]`".

### D3 — When every name in a group is hidden, fall back to the full list

Options, with the recommendation first:

1. **Full recipient list.** Predictable, never nameless, and the degenerate config is reported
   separately (below). A group of two hidden children is labelled `Ania, Jaś`.
2. *Representative only* — never nameless and never shows the full suppressed list, but which
   single name appears is an arbitrary consequence of config order.
3. *Nameless* (`[LIBRUS] Zebranie`) — self-consistent if every user is hidden, but see D2 for
   why it is wrong when only some are.

Separately, `read_pylibrus_config()` logs a warning at startup if **every** configured user has
`include_name_in_subject=false`, since the setting then changes nothing and almost certainly
means the config was misunderstood.

### D4 — The webhook header follows the subject

One `display_name` feeds both `send_email()` (`[LIBRUS <names>] …`, `pylibrus.py:1087`) and
`send_via_webhook()` (`*LIBRUS <names> - <date>*`, `pylibrus.py:1050`), so both change together.
Governing them independently would mean threading two separate values through `notify()` for no
reason anyone has asked for.

This is a deliberate widening of the literal request ("message subject"): a user who hides a
name from their email subject would be surprised to still see it in their Slack message.

### D5 — Logs keep naming everyone

`notify_group()` logs `for {display_name}` in three places (the dry-run line, the
"Do not send … (already sent/read)" line, and implicitly via `notify()`'s "Sending …"). If
those switched to the filtered label, the logs would stop recording who actually received an
item, and `--dry` — whose entire job is to report what a run would do — would under-report.

So: **the notification uses the filtered label; the logs use the full recipient list.** Where
the two differ, log both, e.g.

```
[DRY RUN] 'Zebranie z rodzicami' for Ania (received by: Ania, Jaś): would send
```

### D6 — The representative is unchanged

The group's representative (first in config order) decides which SMTP account sends, which S3
config is used, and the order names appear in. A hidden child stays eligible, keeping this
setting purely cosmetic.

Preferring a *visible* child as representative was considered and rejected: it would let a
cosmetic flag change which account sends mail, re-opening the determinism question settled in
`MULTI_RECIPIENT_PLAN.md` D6/D11 for no benefit.

### D7 — Per-user only, with no global counterpart

Unlike `fetch_announcements`, a global default is meaningless: the entry identifies *which*
account to leave unnamed, and a global `false` would hide every name (the degenerate case in
D3). So there is no `[global] include_name_in_subject`.

`LibrusUser.from_env()` must still gain it, per the `CLAUDE.md` rule that
`from_config()`/`from_env()` stay in sync — as `INCLUDE_NAME_IN_SUBJECT`. Note it is inert
there: env mode supports a single user, who is therefore always the only recipient, so under D2
their name always shows.

---

## 3. Implementation steps

### Step 1 — Config plumbing (`LibrusUser`, `pylibrus.py:254`)

- New field: `include_name_in_subject: bool = True`.
- `from_config()` (`pylibrus.py:263`): `config[section].getboolean("include_name_in_subject", fallback=True)`.
  Note this differs from the `fetch_announcements` line just above it (`pylibrus.py:277`), which
  uses `fallback=None` because `None` there means "fall back to the global setting"; there is no
  global here (D7), so the fallback is the real default.
- `from_env()` (`pylibrus.py:288`): `INCLUDE_NAME_IN_SUBJECT`.

  **Gotcha:** `LibrusUser` is a `slots=True` dataclass with **no `__post_init__`**, unlike
  `PyLibrusConfig`, which normalises `None` back to each field's default. So `from_env()` must
  not pass the bare `str_to_bool(os.environ.get(...))` — an unset variable yields `None`, which
  would persist as the field value and be falsy, silently hiding the only user's name. It needs
  an explicit default when unset.

### Step 2 — Build the two labels (`notify_group`, `pylibrus.py:1359`)

Replace the single line at `pylibrus.py:1364`:

```python
display_name = ", ".join(collection.librus_user.name for collection, _ in group)
```

with a small helper, so the rule is unit-testable on its own:

```python
def group_names(group: ItemGroup) -> tuple[str, str]:
    """Returns (display_name, recipient_names): what the reader sees, and the whole truth.

    A child configured with include_name_in_subject=false is left out of display_name, but
    only while another child remains to name - a message that reached only hidden children is
    still labelled with them rather than with nothing. See SUBJECT_NAMES_PLAN.md D2/D3.
    """
    users = [collection.librus_user for collection, _ in group]
    recipient_names = ", ".join(user.name for user in users)
    visible = [user.name for user in users if user.include_name_in_subject]
    return (", ".join(visible) if visible else recipient_names), recipient_names
```

`notify_group()` then passes `display_name` to `notify()` and uses `recipient_names` in its log
lines (D5), appending `(received by: …)` only when the two differ.

### Step 3 — Startup warning (`read_pylibrus_config`, `pylibrus.py:1154`)

Warn when `librus_users` is non-empty and no user has `include_name_in_subject` true (D3).

### Step 4 — Tests (`tests/test_multi_recipient.py`)

The existing `make_user()` helper gains an `include_name_in_subject=True` argument. New cases:

1. Hidden child in a two-child group → label is `"Ania"`, not `"Ania, Jaś"`.
2. **Still one copy, not two** — the hidden child dedupes exactly as before (D1).
3. Item that reached only the hidden child → label is `"Jaś"` (D2).
4. Every child in the group hidden → label is the full list (D3).
5. Hidden flag does not change destination grouping: two destinations still both notified (D1).
6. The webhook header is filtered too (D4).
7. Logs / `--dry` report the full recipient list even when the label is filtered (D5) — assert
   on `caplog`.
8. A hidden child that is the representative still sends through its own account (D6).
9. `from_config()` defaults to true when the key is absent, and parses `false`.
10. `from_env()` defaults to true when `INCLUDE_NAME_IN_SUBJECT` is unset (the Step 1 gotcha).

Each should be mutation-checked the way the existing suite was: confirm the test fails when the
filter is removed, when the D3 fallback is dropped, and when the logs are switched to the
filtered label.

### Step 5 — Docs

- `pylibrus.ini.example`: the key, commented, in the `[user:ChildName]` block.
- `README.md`: a short paragraph under "Messages sent to several children".
- `CLAUDE.md`: one line in the two-phase architecture section — the label is filtered,
  the logs are not, and the setting never affects delivery (D1/D5).

---

## 4. Risks

- **Mistaken for a delivery switch.** Someone reading `include_name_in_subject=false` could
  reasonably expect the child to stop receiving notifications. Mitigated only by wording: the
  ini comment, README and docstring all state that delivery and dedupe are unaffected.
- **Silent `None` from `from_env()`.** See the Step 1 gotcha; covered by test 10.
- **The setting is invisible in its common case.** With one child per destination the label has
  a single name anyway, so setting the flag appears to do nothing. The startup warning (Step 3)
  catches only the all-hidden case, not this one.

---

## 5. Suggested commit sequence

1. `feat: allow hiding a child's name from grouped notification labels` (Steps 1–3).
2. `test: cover name visibility in grouped notifications` (Step 4).
3. `docs: document include_name_in_subject` (Step 5).
