"""Integration-style tests for `handle_announcements()`: age filtering, dedupe across
runs, and the fact that dedupe ignores `send_message` (announcements always behave as
"unsent" - see ANNOUNCEMENTS_PLAN.md). Uses a fake scraper/notifier so no network or SMTP
call happens; the DB is a real SQLite file in tmp_path so `LibrusNotifier`'s persistence
is exercised for real.
"""

import datetime

from pylibrus.pylibrus import (
    EmailNotify,
    LibrusAnnouncement,
    LibrusNotifier,
    LibrusUser,
    PyLibrusConfig,
    handle_announcements,
)


class RecordingNotifier(LibrusNotifier):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent_subjects = []

    def notify(self, item):
        self.sent_subjects.append(item.subject)


class FakeScraper:
    def __init__(self, announcements):
        self._announcements = announcements

    def fetch_announcements(self):
        return self._announcements


def _make_user(tmp_path, db_name="test.sqlite") -> LibrusUser:
    notify = EmailNotify(smtp_user="u", smtp_pass="p", smtp_server="s", email_dest="dest@example.com")
    return LibrusUser(login="login", password="pass", name="Kid", notify=notify, db_name=db_name)


def test_new_announcement_is_sent_once(tmp_path):
    config = PyLibrusConfig(workdir=str(tmp_path))
    user = _make_user(tmp_path)
    now = datetime.datetime.now()
    announcements = [("Nowe ogłoszenie", "Jan", now - datetime.timedelta(days=1), "<p>a</p>", "a")]

    with RecordingNotifier(config, user) as notifier:
        handle_announcements(config, notifier, FakeScraper(announcements))
    assert notifier.sent_subjects == ["Nowe ogłoszenie"]

    # Second run over the same announcement must not re-send it.
    with RecordingNotifier(config, user) as notifier:
        handle_announcements(config, notifier, FakeScraper(announcements))
    assert notifier.sent_subjects == []


def test_old_announcement_is_never_sent_or_stored(tmp_path):
    config = PyLibrusConfig(workdir=str(tmp_path), max_age_of_sending_msg_days=4)
    user = _make_user(tmp_path)
    now = datetime.datetime.now()
    announcements = [("Stare ogłoszenie", "Jan", now - datetime.timedelta(days=30), "<p>old</p>", "old")]

    with RecordingNotifier(config, user) as notifier:
        handle_announcements(config, notifier, FakeScraper(announcements))
        assert notifier.sent_subjects == []
        # Too-old announcements must not even be persisted (mirrors fetch_msg()'s
        # age-skip for messages, which also never reaches add_msg()).
        assert notifier._session.query(LibrusAnnouncement).count() == 0


def test_max_age_of_sending_announcement_days_overrides_message_default(tmp_path):
    config = PyLibrusConfig(
        workdir=str(tmp_path),
        max_age_of_sending_msg_days=4,
        max_age_of_sending_announcement_days=60,
    )
    user = _make_user(tmp_path)
    now = datetime.datetime.now()
    # Older than the message window but within the announcement-specific window.
    announcements = [("Ogłoszenie sprzed miesiąca", "Jan", now - datetime.timedelta(days=30), "<p>x</p>", "x")]

    with RecordingNotifier(config, user) as notifier:
        handle_announcements(config, notifier, FakeScraper(announcements))
    assert notifier.sent_subjects == ["Ogłoszenie sprzed miesiąca"]


def test_dry_run_does_not_notify_or_mark_sent(tmp_path):
    config = PyLibrusConfig(workdir=str(tmp_path))
    user = _make_user(tmp_path)
    now = datetime.datetime.now()
    announcements = [("Nowe ogłoszenie", "Jan", now - datetime.timedelta(days=1), "<p>a</p>", "a")]

    with RecordingNotifier(config, user) as notifier:
        handle_announcements(config, notifier, FakeScraper(announcements), dry_run=True)
        assert notifier.sent_subjects == []
        # A dry run still persists the announcement (so its title/dedupe-id is known)
        # but must not mark it as sent.
        stored = notifier._session.query(LibrusAnnouncement).one()
        assert stored.email_sent is False

    # A subsequent regular run must still send it normally.
    with RecordingNotifier(config, user) as notifier:
        handle_announcements(config, notifier, FakeScraper(announcements))
    assert notifier.sent_subjects == ["Nowe ogłoszenie"]


def test_editing_an_announcement_body_does_not_trigger_a_resend(tmp_path):
    config = PyLibrusConfig(workdir=str(tmp_path))
    user = _make_user(tmp_path)
    date = datetime.datetime.now() - datetime.timedelta(days=1)

    with RecordingNotifier(config, user) as notifier:
        handle_announcements(
            config,
            notifier,
            FakeScraper([("Tytuł", "Jan", date, "<p>original</p>", "original")]),
        )
    assert notifier.sent_subjects == ["Tytuł"]

    # Same title/author/date, edited body -> same synthetic id, so it's recognized as
    # already-seen and not re-sent.
    with RecordingNotifier(config, user) as notifier:
        handle_announcements(
            config,
            notifier,
            FakeScraper([("Tytuł", "Jan", date, "<p>edited</p>", "edited")]),
        )
    assert notifier.sent_subjects == []
