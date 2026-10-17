"""Unit tests for `parse_inbox`: folder listing HTML in, (path, read) entries out."""

from pathlib import Path

from pylibrus.models import InboxEntry
from pylibrus.scraper.parse import parse_inbox

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_inbox_fixture():
    entries = parse_inbox((FIXTURES_DIR / "wiadomosci_odebrane.html").read_text(encoding="utf-8"))

    assert entries == [
        InboxEntry("/wiadomosci/1/5/1004/f0", read=True),
        InboxEntry("/wiadomosci/1/5/1002/f0", read=False),
        InboxEntry("/wiadomosci/1/5/1003/f0", read=False),
        InboxEntry("/wiadomosci/1/5/1001/f0", read=True),
    ]
    # Still unpacks like the (path, read) tuples callers have always iterated over.
    path, read = entries[0]
    assert (path, read) == ("/wiadomosci/1/5/1004/f0", True)


def test_first_link_in_a_row_wins_and_is_stripped():
    html = '<table><tr class="line0"><td><a href=" /first ">a</a></td><td><a href="/second">b</a></td></tr></table>'

    assert parse_inbox(html) == [InboxEntry("/first", read=True)]


def test_rows_without_a_link_or_without_a_line_class_are_ignored():
    html = (
        "<table>"
        '<tr><td><a href="/no-class">x</a></td></tr>'
        '<tr class="line1"><td>Brak wiadomości</td></tr>'
        "</table>"
    )

    assert parse_inbox(html) == []


def test_empty_page_has_no_entries():
    assert parse_inbox("<html><body></body></html>") == []
