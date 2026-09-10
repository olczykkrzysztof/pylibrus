"""Tests for `LibrusScraper`'s cookie file handling.

`load_cookies_per_login` used to check `self._cookie_path.exists` without calling it. A bound
method is always truthy, so the "does not exist" branch was dead and a first run always fell
through to `read_text()`, raising `FileNotFoundError` and logging it at INFO as "Could not
load ..." - noise that looks like a real problem on every fresh workdir.
"""

import json
import logging

from pylibrus.pylibrus import LibrusScraper, PyLibrusConfig


def _scraper(tmp_path) -> LibrusScraper:
    config = PyLibrusConfig(workdir=str(tmp_path))
    return LibrusScraper(login="test-user", passwd="test-pass", pylibrus_config=config)


def test_missing_cookie_file_is_not_reported_as_a_load_failure(tmp_path, caplog):
    scraper = _scraper(tmp_path)

    with caplog.at_level(logging.DEBUG):
        assert scraper.load_cookies_per_login() == {}

    assert "does not exist" in caplog.text
    assert "Could not load" not in caplog.text


def test_unreadable_cookie_file_is_reported_and_ignored(tmp_path, caplog):
    scraper = _scraper(tmp_path)
    (tmp_path / "pylibrus_cookies.json").write_text("this is not json")

    with caplog.at_level(logging.INFO):
        assert scraper.load_cookies_per_login() == {}

    assert "Could not load" in caplog.text


def test_cookies_round_trip_per_login(tmp_path):
    scraper = _scraper(tmp_path)
    scraper._session.cookies.set("oauth_token", "abc", domain="synergia.librus.pl", path="/")
    scraper.store_cookies_in_file()

    stored = json.loads((tmp_path / "pylibrus_cookies.json").read_text())
    assert stored["test-user"] == [{"name": "oauth_token", "value": "abc", "domain": "synergia.librus.pl", "path": "/"}]

    reloaded = _scraper(tmp_path)
    assert reloaded._session.cookies.get("oauth_token", domain="synergia.librus.pl") == "abc"
