"""Tests for `LibrusSession`'s request plumbing: URL resolution, the referer chain, headers, and
the cached-cookie short-circuit in `login()`. Only the underlying `requests.Session` is stubbed;
the OAuth sequence itself is not exercised here (it needs the real Librus).
"""

import types

import pytest
from fakes import FakeSession

from pylibrus.pylibrus import EmailNotify, LibrusScraper, LibrusSession, LibrusUser, PyLibrusConfig, open_scraper


@pytest.fixture
def session_and_calls(tmp_path, monkeypatch):
    session = LibrusSession("test-user", "test-pass", PyLibrusConfig(workdir=str(tmp_path)))
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return types.SimpleNamespace(url=url, text="<html>skrzynka</html>")

    monkeypatch.setattr(session._session, "get", fake_get)
    return session, calls


def test_paths_resolve_to_synergia_and_each_request_refers_to_the_previous_page(session_and_calls):
    session, calls = session_and_calls

    session.get("/rodzic/index")
    session.get("/wiadomosci/5")
    session.get("https://synergia.librus.pl/wiadomosci/1/5/1002/f0", referer="https://portal.librus.pl/")

    assert [(url, kwargs["headers"]["Referer"]) for url, kwargs in calls] == [
        ("https://synergia.librus.pl/rodzic/index", "https://synergia.librus.pl/"),
        ("https://synergia.librus.pl/wiadomosci/5", "https://synergia.librus.pl/rodzic/index"),
        ("https://synergia.librus.pl/wiadomosci/1/5/1002/f0", "https://portal.librus.pl/"),
    ]


def test_requests_carry_browser_like_headers(session_and_calls):
    session, calls = session_and_calls

    session.get("/rodzic/index", allow_redirects=False)

    (_, kwargs) = calls[0]
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"]["Accept-Language"] == "pl"
    assert kwargs["headers"]["User-Agent"]


def test_login_reuses_cached_cookies_that_librus_still_accepts(session_and_calls):
    session, calls = session_and_calls
    session.cookies.set("oauth_token", "abc", domain="synergia.librus.pl", path="/")

    session.login()

    # Only the validity check ran - no OAuth round trip.
    assert [url for url, _ in calls] == ["https://synergia.librus.pl/wiadomosci/5"]


def test_entering_the_scraper_logs_its_session_in(tmp_path):
    session = FakeSession()

    with LibrusScraper(session, PyLibrusConfig(workdir=str(tmp_path))) as scraper:
        assert isinstance(scraper, LibrusScraper)

    assert session.logged_in


def test_open_scraper_wraps_a_session_for_that_user(tmp_path):
    notify = EmailNotify(smtp_user="u", smtp_pass="p", smtp_server="s", email_dest="d@example.com")
    user = LibrusUser(login="ania-login", password="pw", name="Ania", notify=notify, db_name="x.sqlite")

    scraper = open_scraper(PyLibrusConfig(workdir=str(tmp_path)), user)

    assert isinstance(scraper, LibrusScraper)
    assert isinstance(scraper._session, LibrusSession)
    assert scraper._session._login == "ania-login"
