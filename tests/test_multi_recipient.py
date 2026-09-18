"""Tests for grouping one Librus item received by several children into one notification.

The same school message lands in every child's account under the same `msg_path`, so it must
go out once per *destination* rather than once per child, annotated with that destination's
children - see MULTI_RECIPIENT_PLAN.md. These tests drive `collect_user()` and
`notify_collected()` with fake scrapers so no network or SMTP call happens, while the DBs are
real SQLite files in tmp_path so the cross-session behaviour (and the shared-file locking the
two-phase split introduced) is exercised for real.
"""

import contextlib
import datetime

import pytest

from pylibrus.pylibrus import (
    EmailNotify,
    LibrusNotifier,
    LibrusUser,
    WebhookNotify,
    collect_user,
    notify_collected,
)

NOW = datetime.datetime.now()


class FakeScraper:
    """Serves a fixed inbox listing; `fetch_msg` synthesises a body for any path in it.

    Handing the same path to two users' scrapers is the shared-message case: Librus gives the
    same message the same path in each child's account.
    """

    def __init__(self, msgs=(), announcements=(), dates=None):
        self._msgs = list(msgs)  # [(msg_path, read)]
        self._announcements = list(announcements)
        self._dates = dates or {}

    def msgs_from_folder(self, folder_id):
        return list(self._msgs)

    def fetch_msg(self, msg_path, fetch_attachment_content):
        date = self._dates.get(msg_path, NOW - datetime.timedelta(hours=1))
        return "Nauczyciel", f"Temat {msg_path}", date, "<p>tresc</p>", "tresc", []

    def fetch_announcements(self):
        return list(self._announcements)


@pytest.fixture
def send_log():
    """Collects (via_user, subject, display_name) in the order notifications were sent.

    Ordering assertions need a single log shared by every notifier; a per-notifier list only
    shows each user's own sends and hides how the run interleaved them.
    """
    return []


@pytest.fixture
def notifier_factory(send_log):
    class RecordingNotifier(LibrusNotifier):
        def notify(self, item, display_name=None):
            send_log.append((self.librus_user.name, item.subject, display_name))

    return RecordingNotifier


def email_to(dest) -> EmailNotify:
    return EmailNotify(smtp_user="u", smtp_pass="p", smtp_server="s", email_dest=dest)


def make_user(name, notify, db_name="shared.sqlite") -> LibrusUser:
    return LibrusUser(
        login=f"login-{name}",
        password="pass",
        name=name,
        notify=notify,
        db_name=db_name,
        fetch_announcements=False,
    )


def run(config, notifier_factory, users_and_scrapers, dry_run=False):
    """One pylibrus run: collect every user, then notify, the way main() does."""
    with contextlib.ExitStack() as stack:
        collections = []
        for user, scraper in users_and_scrapers:
            notifier = stack.enter_context(notifier_factory(config, user))
            collections.append(collect_user(config, user, scraper, notifier))
        notify_collected(config, collections, dry_run=dry_run)


def labels(send_log):
    return [display_name for _, _, display_name in send_log]


def test_one_destination_gets_one_notification_naming_both_children(config, notifier_factory, send_log):
    dest = email_to("parents@example.com")
    users = [make_user("Ania", dest), make_user("Jas", dest)]

    run(config, notifier_factory, [(u, FakeScraper([("/wiadomosci/5/111", False)])) for u in users])

    assert labels(send_log) == ["Ania, Jas"]


def test_each_destination_is_notified_once_about_only_its_own_children(config, notifier_factory, send_log):
    users = [
        make_user("Ania", email_to("ania-parents@example.com")),
        make_user("Jas", WebhookNotify(webhook="https://hooks.example.com/jas")),
    ]

    run(config, notifier_factory, [(u, FakeScraper([("/wiadomosci/5/222", False)])) for u in users])

    assert sorted(labels(send_log)) == ["Ania", "Jas"]


def test_each_destination_is_notified_although_the_children_share_one_msg_row(
    config_unsent, notifier_factory, send_log,
):
    """Regression: in "unsent" mode the first destination's send must not mute the others.

    Children on different destinations share a single `Msg` row (one url, one DB file), so a
    send decision taken group by group sees the `email_sent=True` the first destination's send
    just committed and skips every later destination - exactly one destination would ever be
    notified. The decision is therefore taken for all groups before any send.

    This needs "unsent" mode specifically: "unread" mode never consults `email_sent`, so it
    cannot expose the bug.
    """
    users = [
        make_user("Ania", email_to("ania-parents@example.com")),
        make_user("Jas", WebhookNotify(webhook="https://hooks.example.com/jas")),
    ]

    run(config_unsent, notifier_factory, [(u, FakeScraper([("/wiadomosci/5/230", True)])) for u in users])

    assert sorted(labels(send_log)) == ["Ania", "Jas"]


def test_two_children_on_one_destination_and_three_on_another(config, notifier_factory, send_log):
    dest_a, dest_b = email_to("a@example.com"), email_to("b@example.com")
    users = [
        make_user("A1", dest_a),
        make_user("B1", dest_b),
        make_user("A2", dest_a),
        make_user("B2", dest_b),
        make_user("B3", dest_b),
    ]

    run(config, notifier_factory, [(u, FakeScraper([("/wiadomosci/5/333", False)])) for u in users])

    assert sorted(labels(send_log)) == ["A1, A2", "B1, B2, B3"]


def test_message_only_one_child_received_names_that_child_alone(config, notifier_factory, send_log):
    dest = email_to("parents@example.com")
    users_and_scrapers = [
        (make_user("Ania", dest), FakeScraper([("/wiadomosci/5/444", False)])),
        (make_user("Jas", dest), FakeScraper([])),
    ]

    run(config, notifier_factory, users_and_scrapers)

    assert labels(send_log) == ["Ania"]


def test_label_follows_config_order_not_scrape_order(config, notifier_factory, send_log):
    """The representative and the name order must not depend on the order users are visited."""
    dest = email_to("parents@example.com")
    ania, jas = make_user("Ania", dest), make_user("Jas", dest)

    def scrapers_for(users):
        return [(u, FakeScraper([("/wiadomosci/5/555", False)])) for u in users]

    run(config, notifier_factory, scrapers_for([ania, jas]))
    assert send_log == [("Ania", "Temat /wiadomosci/5/555", "Ania, Jas")]

    send_log.clear()
    run(config, notifier_factory, scrapers_for([jas, ania]))
    assert send_log == [("Jas", "Temat /wiadomosci/5/555", "Jas, Ania")]


def test_unsent_mode_does_not_resend_on_the_next_run(config_unsent, notifier_factory, send_log):
    dest = email_to("parents@example.com")
    users = [make_user("Ania", dest), make_user("Jas", dest)]

    def run_once():
        run(config_unsent, notifier_factory, [(u, FakeScraper([("/wiadomosci/5/666", True)])) for u in users])

    run_once()
    assert labels(send_log) == ["Ania, Jas"]

    send_log.clear()
    run_once()
    assert send_log == []


def test_unread_mode_resends_while_any_child_has_it_unread(config_unread, notifier_factory, send_log):
    """`unread` mode decides from the state on Librus, not from our own email_sent flag.

    So an item nobody has opened yet keeps being sent, and the label stays the same between
    ticks - it names who received the item, not who still has it unread. See
    MULTI_RECIPIENT_PLAN.md D4.
    """
    dest = email_to("parents@example.com")
    users = [make_user("Ania", dest), make_user("Jas", dest)]

    def run_with(read_flags):
        send_log.clear()
        pairs = [(u, FakeScraper([("/wiadomosci/5/777", read)])) for u, read in zip(users, read_flags)]
        run(config_unread, notifier_factory, pairs)

    run_with([False, False])
    assert labels(send_log) == ["Ania, Jas"]

    run_with([True, False])  # Ania has read it, Jas has not
    assert labels(send_log) == ["Ania, Jas"]

    run_with([True, True])
    assert send_log == []


def test_announcements_are_grouped_the_same_way(config_with_announcements, notifier_factory, send_log):
    dest = email_to("parents@example.com")
    users = [
        LibrusUser(login="l1", password="p", name="Ania", notify=dest, db_name="shared.sqlite"),
        LibrusUser(login="l2", password="p", name="Jas", notify=dest, db_name="shared.sqlite"),
    ]
    announcements = [("Zebranie z rodzicami", "Dyrektor", NOW - datetime.timedelta(days=1), "<p>t</p>", "t")]

    run(
        config_with_announcements,
        notifier_factory,
        [(u, FakeScraper(announcements=announcements)) for u in users],
    )

    assert send_log == [("Ania", "Zebranie z rodzicami", "Ania, Jas")]


def test_dry_run_sends_nothing_and_leaves_the_item_sendable(config_unsent, notifier_factory, send_log):
    dest = email_to("parents@example.com")
    users = [make_user("Ania", dest), make_user("Jas", dest)]

    def run_once(dry_run):
        run(
            config_unsent,
            notifier_factory,
            [(u, FakeScraper([("/wiadomosci/5/888", True)])) for u in users],
            dry_run=dry_run,
        )

    run_once(dry_run=True)
    assert send_log == []

    run_once(dry_run=False)
    assert labels(send_log) == ["Ania, Jas"]


def test_children_with_separate_db_files_still_group(config, notifier_factory, send_log):
    """Grouping is in memory, so it no longer depends on the children sharing a DB file."""
    dest = email_to("parents@example.com")
    users = [
        make_user("Ania", dest, db_name="ania.sqlite"),
        make_user("Jas", dest, db_name="jas.sqlite"),
    ]

    run(config, notifier_factory, [(u, FakeScraper([("/wiadomosci/5/999", False)])) for u in users])

    assert labels(send_log) == ["Ania, Jas"]


def test_concurrent_sessions_on_one_db_file_do_not_deadlock(config, notifier_factory, send_log):
    """Both notifiers stay open across the run against one SQLite file.

    Without committing at each phase boundary, the first session's uncommitted INSERTs hold a
    write lock and the second user's INSERT fails with "database is locked".
    """
    dest = email_to("parents@example.com")
    users_and_scrapers = [
        (make_user("Ania", dest), FakeScraper([("/wiadomosci/5/1001", False)])),
        (make_user("Jas", dest), FakeScraper([("/wiadomosci/5/1002", False)])),
    ]

    run(config, notifier_factory, users_and_scrapers)

    assert sorted(labels(send_log)) == ["Ania", "Jas"]


def test_items_are_sent_oldest_first_across_children(config, notifier_factory, send_log):
    dest = email_to("parents@example.com")
    dates = {
        "/wiadomosci/5/new": NOW - datetime.timedelta(hours=1),
        "/wiadomosci/5/old": NOW - datetime.timedelta(hours=9),
        "/wiadomosci/5/mid": NOW - datetime.timedelta(hours=5),
    }
    users_and_scrapers = [
        (make_user("Ania", dest), FakeScraper([("/wiadomosci/5/new", False)], dates=dates)),
        (
            make_user("Jas", dest),
            FakeScraper([("/wiadomosci/5/old", False), ("/wiadomosci/5/mid", False)], dates=dates),
        ),
    ]

    run(config, notifier_factory, users_and_scrapers)

    assert [subject for _, subject, _ in send_log] == [
        "Temat /wiadomosci/5/old",
        "Temat /wiadomosci/5/mid",
        "Temat /wiadomosci/5/new",
    ]


def test_a_child_whose_scrape_fails_does_not_block_the_others(config, notifier_factory, send_log):
    """main() logs a failing user and carries on, so the rest are still collected and notified."""

    class BrokenScraper:
        def msgs_from_folder(self, folder_id):
            raise RuntimeError("cookies expired")

    dest = email_to("parents@example.com")
    users_and_scrapers = [
        (make_user("Ania", dest), BrokenScraper()),
        (make_user("Jas", dest), FakeScraper([("/wiadomosci/5/1101", False)])),
    ]

    with contextlib.ExitStack() as stack:
        collections = []
        for user, scraper in users_and_scrapers:
            notifier = stack.enter_context(notifier_factory(config, user))
            with contextlib.suppress(Exception):  # mirrors main()'s per-user guard
                collections.append(collect_user(config, user, scraper, notifier))
        notify_collected(config, collections)

    assert labels(send_log) == ["Jas"]
