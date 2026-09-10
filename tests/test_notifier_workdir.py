"""Tests for `LibrusNotifier`'s workdir guard.

`_create_db` used to check `workdir_path.exists` without calling it, so the guard never fired
and a missing workdir surfaced much later as a SQLAlchemy "unable to open database file".
"""

import pytest

from pylibrus.pylibrus import EmailNotify, LibrusNotifier, LibrusUser, PyLibrusConfig


def _user() -> LibrusUser:
    notify = EmailNotify(smtp_user="u", smtp_pass="p", smtp_server="s", email_dest="dest@example.com")
    return LibrusUser(login="login", password="pass", name="Kid", notify=notify, db_name="test.sqlite")


def test_missing_workdir_raises_a_clear_error(tmp_path):
    config = PyLibrusConfig(workdir=str(tmp_path / "does-not-exist"))

    with pytest.raises(RuntimeError, match="does not exist"):
        with LibrusNotifier(config, _user()):
            pass


def test_existing_workdir_creates_the_db_file(tmp_path):
    config = PyLibrusConfig(workdir=str(tmp_path))

    with LibrusNotifier(config, _user()):
        pass

    assert (tmp_path / "test.sqlite").exists()
