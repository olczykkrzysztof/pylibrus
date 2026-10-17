"""Plain values passed between layers - no SQLAlchemy here.

Each is a NamedTuple: a named version of a tuple LibrusScraper already returned, so callers that
unpack them positionally keep working.
"""

import datetime
from typing import NamedTuple


class InboxEntry(NamedTuple):
    """One row of a folder listing."""

    path: str
    read: bool


class MessageHeader(NamedTuple):
    sender: str
    subject: str
    date: datetime.datetime


class AttachmentRef(NamedTuple):
    """An attachment as listed on a message page, before any content is downloaded."""

    link_id: str
    name: str


class ParsedAnnouncement(NamedTuple):
    title: str
    author: str
    date: datetime.datetime
    contents_html: str
    contents_text: str
