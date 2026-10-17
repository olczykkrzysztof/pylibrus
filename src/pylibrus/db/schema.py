from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Msg(Base):
    __tablename__ = "messages"

    url = Column(String, primary_key=True)
    folder = Column(Integer)
    sender = Column(String)
    subject = Column(String)
    date = Column(DateTime)
    contents_html = Column(String)
    contents_text = Column(String)
    email_sent = Column(Boolean, default=False)

    # No subject prefix/banner override for regular messages; see LibrusAnnouncement.
    subject_prefix = ""
    banner_text = "TO WIADOMOŚĆ Z LIBRUSA, NIE ODPOWIADAJ NA NIĄ MEJLEM"
    banner_html = "To wiadomość z Librusa, nie odpowiadaj na nią mejlem"
    webhook_item_label = "Temat"


class Attachment(Base):
    __tablename__ = "attachments"

    link_id = Column(String, primary_key=True)  # link_id seems to contain message id and attachment id
    msg_path = Column(String, ForeignKey(Msg.url))
    name = Column(String)
    data = Column(LargeBinary)
    s3_key = Column(String, nullable=True)
    s3_upload_date = Column(DateTime, nullable=True)
    s3_etag = Column(String, nullable=True)


class LibrusAnnouncement(Base):
    """A Librus announcement (AKA 'ogłoszenie'), scraped from /ogloszenia.

    Announcements have no stable id and no per-item read/unread flag (see
    ANNOUNCEMENTS_PLAN.md), so `url` is a synthetic key derived from title+author+date
    (see `announcement_id()`) rather than a real Librus URL, and `email_sent` is the
    only dedupe signal - `send_message=unread` semantics do not apply here.

    Column names deliberately match `Msg` so `LibrusNotifier.notify()` and friends can
    treat both the same way; `contents_html`/`contents_text` hold the announcement body.
    """

    __tablename__ = "announcements"

    url = Column(String, primary_key=True)  # synthetic, see announcement_id()
    sender = Column(String)  # "Dodał"
    subject = Column(String)  # title
    date = Column(DateTime)  # "Data publikacji" (date only, no time)
    contents_html = Column(String)
    contents_text = Column(String)
    content_hash = Column(String, nullable=True)  # sha1 of contents_text; unused today, for future edit-detection
    email_sent = Column(Boolean, default=False)

    subject_prefix = "Ogłoszenie: "
    banner_text = "TO OGŁOSZENIE Z LIBRUSA, NIE ODPOWIADAJ NA NIE MEJLEM"
    banner_html = "To ogłoszenie z Librusa, nie odpowiadaj na nie mejlem"
    webhook_item_label = "Ogłoszenie"
