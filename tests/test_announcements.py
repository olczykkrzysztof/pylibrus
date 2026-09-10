"""Offline tests for the /ogloszenia parser (`LibrusScraper.fetch_announcements`) and
`announcement_id()`. See ANNOUNCEMENTS_PLAN.md for the design this implements.

No network access happens here: `LibrusScraper._get` is monkeypatched to return a fixed
HTML fixture instead of hitting Librus.
"""

import datetime
from pathlib import Path

import pytest

from pylibrus.pylibrus import LibrusScraper, PyLibrusConfig, announcement_id

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _scraper_returning(tmp_path, html: str) -> LibrusScraper:
    config = PyLibrusConfig(workdir=str(tmp_path))
    scraper = LibrusScraper(login="test-user", passwd="test-pass", pylibrus_config=config)
    scraper._get = lambda path, referer=None: type("FakeResponse", (), {"text": html})()
    return scraper


def _scraper_for_fixture(tmp_path, fixture_name: str) -> LibrusScraper:
    return _scraper_returning(tmp_path, (FIXTURES_DIR / fixture_name).read_text(encoding="utf-8"))


def test_fetch_announcements_parses_fixture(tmp_path):
    scraper = _scraper_for_fixture(tmp_path, "ogloszenia.html")
    announcements = scraper.fetch_announcements()

    assert len(announcements) == 2

    title, author, date, contents_html, contents_text = announcements[0]
    assert title == "Zebranie z rodzicami"
    assert author == "Jan Kowalski"
    assert date == datetime.datetime(2025, 9, 8)
    assert "Zapraszamy na zebranie" in contents_text
    assert "<br/>" in contents_html

    second_title, *_ = announcements[1]
    assert second_title == "Odwołane zajęcia"


def test_fetch_announcements_empty_page_returns_nothing(tmp_path):
    scraper = _scraper_for_fixture(tmp_path, "ogloszenia_empty.html")
    assert scraper.fetch_announcements() == []


def test_fetch_announcements_skips_table_with_unparseable_date(tmp_path):
    html = """
    <table class="decorated big">
      <thead><tr><td>Zły termin</td></tr></thead>
      <tbody>
        <tr><th>Dodał</th><td>Ktoś</td></tr>
        <tr><th>Data publikacji</th><td>not-a-date</td></tr>
      </tbody>
    </table>
    """
    scraper = _scraper_returning(tmp_path, html)
    assert scraper.fetch_announcements() == []


def test_fetch_announcements_skips_table_with_missing_date(tmp_path):
    html = """
    <table class="decorated big">
      <thead><tr><td>Bez daty</td></tr></thead>
      <tbody>
        <tr><th>Dodał</th><td>Ktoś</td></tr>
        <tr><th>Treść</th><td>Tresc</td></tr>
      </tbody>
    </table>
    """
    scraper = _scraper_returning(tmp_path, html)
    assert scraper.fetch_announcements() == []


def test_fetch_announcements_tolerates_unknown_field(tmp_path):
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
    scraper = _scraper_returning(tmp_path, html)
    announcements = scraper.fetch_announcements()
    assert len(announcements) == 1
    assert announcements[0][0] == "Z nowym polem"


def test_fetch_announcements_without_content_row(tmp_path):
    html = """
    <table class="decorated big">
      <thead><tr><td>Bez treści</td></tr></thead>
      <tbody>
        <tr><th>Dodał</th><td>Ktoś</td></tr>
        <tr><th>Data publikacji</th><td>2025-01-02</td></tr>
      </tbody>
    </table>
    """
    scraper = _scraper_returning(tmp_path, html)
    announcements = scraper.fetch_announcements()
    assert len(announcements) == 1
    _, _, _, contents_html, contents_text = announcements[0]
    assert contents_html == ""
    assert contents_text == ""


def test_fetch_announcements_raises_on_no_access(tmp_path):
    scraper = _scraper_returning(tmp_path, "Brak dostępu")
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
