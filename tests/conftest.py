"""Shared config fixtures. Every run needs a real workdir, since LibrusNotifier writes SQLite
files into it."""

import pytest

from pylibrus.pylibrus import PyLibrusConfig


@pytest.fixture
def config(tmp_path) -> PyLibrusConfig:
    return PyLibrusConfig(workdir=str(tmp_path), fetch_announcements=False)


@pytest.fixture
def config_unsent(tmp_path) -> PyLibrusConfig:
    return PyLibrusConfig(workdir=str(tmp_path), send_message="unsent", fetch_announcements=False)


@pytest.fixture
def config_unread(tmp_path) -> PyLibrusConfig:
    return PyLibrusConfig(workdir=str(tmp_path), send_message="unread", fetch_announcements=False)


@pytest.fixture
def config_with_announcements(tmp_path) -> PyLibrusConfig:
    return PyLibrusConfig(workdir=str(tmp_path), fetch_announcements=True)
