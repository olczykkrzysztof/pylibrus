"""Offline tests for the /ogloszenia parser (`parse_announcements`), the scraper method that
fetches the page for it, and `announcement_id()`. See ANNOUNCEMENTS_PLAN.md for the design.

The parser is called directly on HTML; the fetch cases go through a FakeSession, so nothing
here touches the network.
"""

import datetime
from pathlib import Path

import pytest
from fakes import FakeSession

from pylibrus.pylibrus import LibrusScraper, PyLibrusConfig, announcement_id
from pylibrus.scraper.parse import parse_announcements

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def test_parses_fixture():
    announcements = parse_announcements(_fixture("ogloszenia.html"))

    assert len(announcements) == 2

    title, author, date, contents_html, contents_text = announcements[0]
    assert title == "Zebranie z rodzicami"
    assert author == "Jan Kowalski"
    assert date == datetime.datetime(2025, 9, 8)
    assert "Zapraszamy na zebranie" in contents_text
    assert "<br/>" in contents_html

    second_title, *_ = announcements[1]
    assert second_title == "Odwołane zajęcia"


def test_empty_page_returns_nothing():
    assert parse_announcements(_fixture("ogloszenia_empty.html")) == []


def test_skips_table_with_unparseable_date():
    html = """
    <table class="decorated big">
      <thead><tr><td>Zły termin</td></tr></thead>
      <tbody>
        <tr><th>Dodał</th><td>Ktoś</td></tr>
        <tr><th>Data publikacji</th><td>not-a-date</td></tr>
      </tbody>
    </table>
    """
    assert parse_announcements(html) == []


def test_skips_table_with_missing_date():
    html = """
    <table class="decorated big">
      <thead><tr><td>Bez daty</td></tr></thead>
      <tbody>
        <tr><th>Dodał</th><td>Ktoś</td></tr>
        <tr><th>Treść</th><td>Tresc</td></tr>
      </tbody>
    </table>
    """
    assert parse_announcements(html) == []


def test_tolerates_unknown_field():
    # A future Librus markup change adding a new labelled row must not break parsing -
    # this is intentionally more forgiving than the reference implementations that raise.
    html = """
    <table class="decorated big">
      <thead><tr><td>Z nowym polem</td></tr></thead>
      <tbody>
        <tr><th>Dodał</th><td>Ktoś</td></tr>
        <tr><th>Data publikacji</th><td>2025-01-02</td></tr>
        <tr><th>Treść</th><td>Tresc</td></tr>
        <tr><th>Nowe pole z przyszłości Librusa</th><td>wartość</td></tr>
      </tbody>
    </table>
    """
    announcements = parse_announcements(html)
    assert len(announcements) == 1
    assert announcements[0][0] == "Z nowym polem"


def test_without_content_row():
    html = """
    <table class="decorated big">
      <thead><tr><td>Bez treści</td></tr></thead>
      <tbody>
        <tr><th>Dodał</th><td>Ktoś</td></tr>
        <tr><th>Data publikacji</th><td>2025-01-02</td></tr>
      </tbody>
    </table>
    """
    announcements = parse_announcements(html)
    assert len(announcements) == 1
    _, _, _, contents_html, contents_text = announcements[0]
    assert contents_html == ""
    assert contents_text == ""


def _scraper_serving(tmp_path, html: str) -> tuple[LibrusScraper, FakeSession]:
    session = FakeSession({"/ogloszenia": html})
    return LibrusScraper(session, PyLibrusConfig(workdir=str(tmp_path))), session


def test_fetch_announcements_requests_the_page_and_parses_it(tmp_path):
    scraper, session = _scraper_serving(tmp_path, _fixture("ogloszenia.html"))

    assert scraper.fetch_announcements() == parse_announcements(_fixture("ogloszenia.html"))
    assert session.requests == [("get", "/ogloszenia", "https://synergia.librus.pl/rodzic/index")]


def test_fetch_announcements_raises_on_no_access(tmp_path):
    scraper, _ = _scraper_serving(tmp_path, "Brak dostępu")
    with pytest.raises(RuntimeError):
        scraper.fetch_announcements()


def test_announcement_id_is_stable():
    date = datetime.datetime(2025, 9, 8)
    assert announcement_id("Tytuł", "Autor", date) == announcement_id("Tytuł", "Autor", date)


def test_announcement_id_excludes_body_but_includes_title_author_date():
    date = datetime.datetime(2025, 9, 8)
    base = announcement_id("Tytuł", "Autor", date)

    assert announcement_id("Inny tytuł", "Autor", date) != base
    assert announcement_id("Tytuł", "Inny autor", date) != base
    assert announcement_id("Tytuł", "Autor", date + datetime.timedelta(days=1)) != base
