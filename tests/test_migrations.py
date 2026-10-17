"""Tests for the additive schema migrations in `pylibrus.db.migrations`.

The only way a migration ever runs in production is against a user's aged SQLite file, so these
build one at an old schema with plain sqlite3 and open it the way a run does.
"""

import sqlite3

from sqlalchemy import create_engine, inspect

from pylibrus.db import migrations
from pylibrus.db.store import MessageStore

# The attachments table as it was before S3 support added s3_key/s3_upload_date/s3_etag.
PRE_S3_SCHEMA = """
CREATE TABLE messages (
    url VARCHAR PRIMARY KEY, folder INTEGER, sender VARCHAR, subject VARCHAR, date DATETIME,
    contents_html VARCHAR, contents_text VARCHAR, email_sent BOOLEAN
);
CREATE TABLE attachments (
    link_id VARCHAR PRIMARY KEY, msg_path VARCHAR REFERENCES messages(url), name VARCHAR, data BLOB
);
INSERT INTO attachments (link_id, msg_path, name, data) VALUES ('1002/77', '/wiadomosci/1/5/1002/f0', 'zgoda.pdf', X'25504446');
"""


def _old_db(tmp_path, name="old.sqlite"):
    with sqlite3.connect(tmp_path / name) as connection:
        connection.executescript(PRE_S3_SCHEMA)
    return name


def _columns(tmp_path, name, table):
    return {c["name"] for c in inspect(create_engine(f"sqlite:///{tmp_path / name}")).get_columns(table)}


def test_opening_an_old_db_adds_the_missing_columns_and_keeps_the_rows(tmp_path):
    name = _old_db(tmp_path)

    with MessageStore(str(tmp_path), name) as store:
        (attachment,) = store.attachments_for("/wiadomosci/1/5/1002/f0")
        assert (attachment.name, attachment.data, attachment.s3_key) == ("zgoda.pdf", b"%PDF", None)

    assert {"s3_key", "s3_upload_date", "s3_etag"} <= _columns(tmp_path, name, "attachments")


def test_old_db_reports_exactly_the_missing_columns_as_pending(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / _old_db(tmp_path)}")

    assert [(m.table, m.column) for m in migrations.pending(engine)] == [
        ("attachments", "s3_key"),
        ("attachments", "s3_upload_date"),
        ("attachments", "s3_etag"),
    ]
    assert [m.ddl for m in migrations.pending(engine)][0] == "ALTER TABLE attachments ADD COLUMN s3_key TEXT"


def test_migrations_are_idempotent(tmp_path):
    name = _old_db(tmp_path)

    with MessageStore(str(tmp_path), name):
        pass
    with MessageStore(str(tmp_path), name):
        pass

    assert migrations.pending(create_engine(f"sqlite:///{tmp_path / name}")) == []


def test_fresh_db_needs_no_migration(tmp_path):
    with MessageStore(str(tmp_path), "fresh.sqlite"):
        pass

    assert migrations.pending(create_engine(f"sqlite:///{tmp_path / 'fresh.sqlite'}")) == []


def test_missing_table_is_left_to_create_all(tmp_path):
    assert migrations.pending(create_engine(f"sqlite:///{tmp_path / 'empty.sqlite'}")) == []
