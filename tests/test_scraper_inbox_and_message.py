"""Characterisation tests for the inbox and message parsers (`msgs_from_folder`, `fetch_msg`).

These pin down what the current code extracts from a page, including the request paths and
referers it sends, so that splitting parsing from fetching (REFACTOR_PLAN.md step 3) can prove
it changed nothing. No network access happens here: `LibrusScraper._get` is monkeypatched to
serve the fixtures and record what was asked for.
"""

import datetime
from pathlib import Path

from pylibrus.pylibrus import LibrusScraper, PyLibrusConfig

FIXTURES_DIR = Path(__file__).parent / "fixtures"

INBOX_PATH = "/wiadomosci/5"
MSG_PATH = "/wiadomosci/1/5/1002/f0"


class FakeResponse:
    def __init__(self, text: str):
        self.text = text


def _scraper(tmp_path, max_age_days=36500) -> tuple[LibrusScraper, list[tuple[str, str]]]:
    """Returns a scraper serving the inbox and message fixtures, plus the (path, referer) log."""
    config = PyLibrusConfig(workdir=str(tmp_path), max_age_of_sending_msg_days=max_age_days)
    scraper = LibrusScraper(login="test-user", passwd="test-pass", pylibrus_config=config)
    pages = {
        INBOX_PATH: (FIXTURES_DIR / "wiadomosci_odebrane.html").read_text(encoding="utf-8"),
        MSG_PATH: (FIXTURES_DIR / "wiadomosc.html").read_text(encoding="utf-8"),
    }
    requests_made = []

    def fake_get(path, referer=None, **kwargs):
        requests_made.append((path, referer))
        return FakeResponse(pages[path])

    scraper._get = fake_get
    return scraper, requests_made


def test_inbox_lists_links_and_unread_state(tmp_path):
    scraper, requests_made = _scraper(tmp_path)

    msgs = scraper.msgs_from_folder(5)

    # Not document order: line0 rows come before line1 rows, then the whole list is reversed.
    # Nothing downstream relies on it (the run sorts items by date before sending), but a
    # parser rewrite must not change it silently either.
    assert msgs == [
        ("/wiadomosci/1/5/1004/f0", True),
        ("/wiadomosci/1/5/1002/f0", False),  # every cell bold
        ("/wiadomosci/1/5/1003/f0", False),  # one bold cell is enough
        ("/wiadomosci/1/5/1001/f0", True),
    ]
    assert requests_made == [(INBOX_PATH, "https://synergia.librus.pl/rodzic/index")]


def test_message_headers_contents_and_attachment_refs(tmp_path):
    scraper, requests_made = _scraper(tmp_path)
    scraper.msgs_from_folder(5)

    sender, subject, date, contents_html, contents_text, attachments = scraper.fetch_msg(MSG_PATH, False)

    assert sender == "Jan Kowalski (Nauczyciel)"
    assert subject == "Wycieczka do muzeum"
    assert date == datetime.datetime(2025, 9, 8, 14, 30)
    assert contents_html == (
        '<div class="container-message-content">Szanowni Państwo,<br/>zapraszamy na wycieczkę.</div>'
    )
    assert contents_text == "Szanowni Państwo,zapraszamy na wycieczkę."
    assert [(a.link_id, a.msg_path, a.name, a.data) for a in attachments] == [
        ("1002/77", MSG_PATH, "zgoda.pdf", None),
    ]
    # The message is fetched with the inbox page as referer, the way the browser does it.
    assert requests_made[-1] == (MSG_PATH, "https://synergia.librus.pl/wiadomosci/5")


def test_message_older_than_max_age_is_dropped(tmp_path):
    scraper, _ = _scraper(tmp_path, max_age_days=4)
    scraper.msgs_from_folder(5)

    assert scraper.fetch_msg(MSG_PATH, False) is None
