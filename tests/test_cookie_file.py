"""Tests for the cookie file handling behind `LibrusSession` (`pylibrus.scraper.cookies`).

`load_cookies_per_login` used to check `self._cookie_path.exists` without calling it. A bound
method is always truthy, so the "does not exist" branch was dead and a first run always fell
through to `read_text()`, raising `FileNotFoundError` and logging it at INFO as "Could not
load ..." - noise that looks like a real problem on every fresh workdir.
"""

import json
import logging

from pylibrus.pylibrus import LibrusSession, PyLibrusConfig


def _session(tmp_path) -> LibrusSession:
    config = PyLibrusConfig(workdir=str(tmp_path))
    return LibrusSession(login="test-user", passwd="test-pass", pylibrus_config=config)


def test_missing_cookie_file_is_not_reported_as_a_load_failure(tmp_path, caplog):
    session = _session(tmp_path)

    with caplog.at_level(logging.DEBUG):
        assert session.load_cookies_per_login() == {}

    assert "does not exist" in caplog.text
    assert "Could not load" not in caplog.text


def test_unreadable_cookie_file_is_reported_and_ignored(tmp_path, caplog):
    session = _session(tmp_path)
    (tmp_path / "pylibrus_cookies.json").write_text("this is not json")

    with caplog.at_level(logging.INFO):
        assert session.load_cookies_per_login() == {}

    assert "Could not load" in caplog.text


def test_cookies_round_trip_per_login(tmp_path):
    session = _session(tmp_path)
    session.cookies.set("oauth_token", "abc", domain="synergia.librus.pl", path="/")
    session.store_cookies_in_file()

    stored = json.loads((tmp_path / "pylibrus_cookies.json").read_text())
    assert stored["test-user"] == [{"name": "oauth_token", "value": "abc", "domain": "synergia.librus.pl", "path": "/"}]

    reloaded = _session(tmp_path)
    assert reloaded.cookies.get("oauth_token", domain="synergia.librus.pl") == "abc"
