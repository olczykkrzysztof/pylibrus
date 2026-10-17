import hashlib
from pathlib import Path

from sqlalchemy.engine import create_engine
from sqlalchemy.orm import Session, sessionmaker

from pylibrus.db import migrations
from pylibrus.db.schema import Attachment, Base, LibrusAnnouncement, Msg


class MessageStore:
    """One SQLite file of seen messages, attachments and announcements, behind one session.

    Opened as a context manager: entering creates/migrates the DB, leaving commits (or rolls
    back on an exception).
    """

    def __init__(self, workdir: str, db_name: str):
        self._workdir = workdir
        self._db_name = db_name
        self._engine = None
        self._session = None

    def _create_db(self):
        workdir_path = Path(self._workdir)
        if not workdir_path.exists():
            raise RuntimeError(f"Workdir {workdir_path} does not exist")
        self._engine = create_engine(f"sqlite:///{workdir_path / self._db_name}")
        Base.metadata.create_all(self._engine)
        migrations.apply(self._engine)
        session_maker = sessionmaker(bind=self._engine)
        self._session = session_maker()

    def __enter__(self):
        self._create_db()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            if self._session:
                self._session.commit()
        else:
            self._session.rollback()

    @property
    def session(self) -> Session | None:
        """The underlying session, or None until the store is entered."""
        return self._session

    def commit(self):
        """Flushes pending writes now instead of at __exit__.

        Several stores are open at once during a run (one per Librus user) and by default
        they all point at the same SQLite file. SQLAlchemy defers BEGIN until the first DML, so
        one session sitting on uncommitted INSERTs holds a write lock that makes the next
        user's INSERT fail with "database is locked" - hence committing eagerly at each phase
        boundary rather than relying on __exit__ alone. See MULTI_RECIPIENT_PLAN.md.
        """
        if self._session:
            self._session.commit()

    def get_msg(self, url):
        return self._session.get(Msg, url)

    def add_msg(self, url, folder_id, sender, date, subject, contents_html, contents_text, attachments):
        msg = self.get_msg(url)
        if not msg:
            msg = Msg(
                url=url,
                folder=folder_id,
                sender=sender,
                date=date,
                subject=subject,
                contents_html=contents_html,
                contents_text=contents_text,
            )
            self._session.add(msg)
            for attachment in attachments:
                self._session.add(attachment)
        return msg

    def get_announcement(self, url):
        return self._session.get(LibrusAnnouncement, url)

    def add_announcement(self, url, sender, subject, date, contents_html, contents_text):
        announcement = self.get_announcement(url)
        if not announcement:
            announcement = LibrusAnnouncement(
                url=url,
                sender=sender,
                subject=subject,
                date=date,
                contents_html=contents_html,
                contents_text=contents_text,
                content_hash=hashlib.sha1(contents_text.encode()).hexdigest(),
            )
            self._session.add(announcement)
        return announcement

    def attachments_for(self, msg_path: str) -> list[Attachment]:
        """Stored attachments of one message; empty when the store was never entered.

        --test-notify sends through a notifier whose store is never opened, and must not fail.
        """
        if not self._session:
            return []
        return self._session.query(Attachment).filter(Attachment.msg_path == msg_path).all()
