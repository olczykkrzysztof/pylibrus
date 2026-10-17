"""Hand-rolled additive schema migrations (there is no Alembic here, see CLAUDE.md).

`Base.metadata.create_all()` creates missing *tables* but never adds columns to a table that
already exists, so every column added to an existing model needs an entry in `MIGRATIONS`, or
existing users' DBs won't pick it up. `announcements` is created fresh by create_all() for
everyone, so it needs none today - this is the home for its future column additions.
"""

import dataclasses

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


@dataclasses.dataclass(frozen=True)
class Migration:
    table: str
    column: str
    sql_type: str

    @property
    def ddl(self) -> str:
        return f"ALTER TABLE {self.table} ADD COLUMN {self.column} {self.sql_type}"


MIGRATIONS = [
    Migration("attachments", "s3_key", "TEXT"),
    Migration("attachments", "s3_upload_date", "DATETIME"),
    Migration("attachments", "s3_etag", "TEXT"),
]


def pending(engine: Engine) -> list[Migration]:
    """Migrations whose table exists but lacks the column. A missing table is left to create_all()."""
    inspector = inspect(engine)
    existing: dict[str, set[str]] = {}
    result = []
    for migration in MIGRATIONS:
        if not inspector.has_table(migration.table):
            continue
        if migration.table not in existing:
            existing[migration.table] = {c["name"] for c in inspector.get_columns(migration.table)}
        if migration.column not in existing[migration.table]:
            result.append(migration)
    return result


def apply(engine: Engine) -> None:
    migrations = pending(engine)
    if not migrations:
        return
    with engine.begin() as connection:
        for migration in migrations:
            connection.execute(text(migration.ddl))
