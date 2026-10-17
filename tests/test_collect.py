"""Tests for the collect phase's own rules: which destinations need attachment bytes, and how
`collect_all()` walks the users. The grouping/labelling behaviour is in test_multi_recipient.py."""

import contextlib

import pytest

from pylibrus.pipeline.collect import collect_all, needs_attachment_content
from pylibrus.settings import EmailNotify, LibrusUser, PyLibrusConfig, WebhookNotify

EMAIL = EmailNotify(smtp_user="u", smtp_pass="p", smtp_server="s", email_dest="d@example.com")
LIBRUS_LINK_WEBHOOK = WebhookNotify(webhook="https://hooks.example.com")
S3_WEBHOOK = WebhookNotify(
    webhook="https://hooks.example.com",
    webhook_attachments_source="s3://bucket",
    s3_region="eu-central-1",
    s3_access_key_id="AKIA",
    s3_secret_access_key="shh",
)


@pytest.mark.parametrize(
    ("notify", "fetch_attachments", "expected"),
    [
        (EMAIL, True, True),  # embedded in the email
        (S3_WEBHOOK, True, True),  # re-uploaded to S3
        (LIBRUS_LINK_WEBHOOK, True, False),  # only linked back to Librus
        (EMAIL, False, False),  # switched off globally
        (S3_WEBHOOK, False, False),
    ],
)
def test_needs_attachment_content(tmp_path, notify, fetch_attachments, expected):
    config = PyLibrusConfig(workdir=str(tmp_path), fetch_attachments=fetch_attachments)

    assert bool(needs_attachment_content(config, notify)) is expected


class EmptyScraper:
    def msgs_from_folder(self, folder_id):
        return []

    def fetch_announcements(self):
        return []


class NullNotifier:
    """Enough of LibrusNotifier for collect_user() over an empty inbox."""

    def __init__(self, config, user):
        self.user = user

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        pass


def user(name) -> LibrusUser:
    return LibrusUser(login=name, password="p", name=name, notify=EMAIL, db_name="x.sqlite")


def test_users_are_visited_in_config_order_with_a_pause_between_but_not_after(tmp_path):
    config = PyLibrusConfig(workdir=str(tmp_path), sleep_between_librus_users=7, fetch_announcements=False)
    opened, sleeps = [], []

    def open_scraper(_config, librus_user):
        opened.append(librus_user.name)
        return contextlib.nullcontext(EmptyScraper())

    with collect_all(
        config, [user("Ania"), user("Jas"), user("Ola")], NullNotifier, open_scraper, sleeps.append
    ) as collections:
        assert [c.librus_user.name for c in collections] == ["Ania", "Jas", "Ola"]

    assert opened == ["Ania", "Jas", "Ola"]
    assert sleeps == [7, 7]


def test_notifiers_stay_open_until_the_with_block_ends(tmp_path):
    config = PyLibrusConfig(workdir=str(tmp_path), fetch_announcements=False)
    events = []

    class TrackingNotifier(NullNotifier):
        def __enter__(self):
            events.append(f"open {self.user.name}")
            return self

        def __exit__(self, *exc):
            events.append(f"close {self.user.name}")
            return False

    def open_scraper(_config, _user):
        return contextlib.nullcontext(EmptyScraper())

    with collect_all(config, [user("Ania"), user("Jas")], TrackingNotifier, open_scraper, lambda _s: None):
        events.append("notify phase")

    assert events == ["open Ania", "open Jas", "notify phase", "close Jas", "close Ania"]
