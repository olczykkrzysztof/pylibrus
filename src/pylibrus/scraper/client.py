import datetime
import logging

from bs4 import BeautifulSoup

from pylibrus.db.schema import Attachment
from pylibrus.models import ParsedAnnouncement
from pylibrus.scraper.attachments import download_attachment
from pylibrus.scraper.parse import (
    parse_announcements,
    parse_attachment_rows,
    parse_inbox,
    parse_message_body,
    parse_message_header,
)
from pylibrus.scraper.session import LibrusSession
from pylibrus.scraper.urls import msg_folder_path, synergia_url_from_path
from pylibrus.settings import LibrusUser, PyLibrusConfig

logger = logging.getLogger(__name__)


class LibrusScraper:
    """Reads the inbox, messages, attachments and announcements through a logged-in LibrusSession.

    Used as a context manager: entering logs the session in (reusing cached cookies when Librus
    still accepts them).
    """

    def __init__(self, session: LibrusSession, pylibrus_config: PyLibrusConfig):
        self._session = session
        self._pylibrus_config = pylibrus_config
        self._last_folder_msg_path = None

    def __enter__(self):
        self._session.login()
        return self

    def __exit__(self, exc_type=None, exc_val=None, exc_tb=None):
        pass

    def _get(self, path, referer=None, **kwargs):
        return self._session.get(path, referer, **kwargs)

    def fetch_attachments(self, msg_path, soup, fetch_content):
        attachments = [
            Attachment(link_id=ref.link_id, msg_path=msg_path, name=ref.name, data=None)
            for ref in parse_attachment_rows(soup)
        ]

        if not fetch_content:
            return attachments

        for attachment in attachments:
            logger.info(f"Download attachment {attachment.name}")
            attach_data = download_attachment(self._session, attachment.link_id)
            attachment.data = attach_data
            logger.info(f"{attachment.name=} {attachment.link_id=} {len(attach_data)=}")

        return attachments

    def fetch_msg(self, msg_path, fetch_attchement_content: bool):
        msg_page = self._get(msg_path, referer=synergia_url_from_path(self._last_folder_msg_path)).text
        soup = BeautifulSoup(msg_page, "html.parser")
        sender, subject, date = parse_message_header(soup)
        if datetime.datetime.now() - date > datetime.timedelta(days=self._pylibrus_config.max_age_of_sending_msg_days):
            logger.info(f"Do not send '{subject}' (message too old, {date})")
            return None
        contents_html, contents_text = parse_message_body(soup)

        attachments = self.fetch_attachments(msg_path, soup, fetch_attchement_content)
        return sender, subject, date, contents_html, contents_text, attachments

    def msgs_from_folder(self, folder_id):
        self._last_folder_msg_path = msg_folder_path(folder_id)
        ret = self._get(self._last_folder_msg_path, referer=synergia_url_from_path("/rodzic/index"))
        return parse_inbox(ret.text)

    def fetch_announcements(self) -> list[ParsedAnnouncement]:
        """Scrapes /ogloszenia and returns (title, author, date, contents_html, contents_text) tuples.

        Announcements have no pagination, no per-item URL and no read/unread flag - the
        whole page lists every currently valid announcement on every call, newest first
        as Librus renders it. The caller is responsible for deduplication (there is no
        stable id to key on - see `announcement_id()`). See ANNOUNCEMENTS_PLAN.md for the
        full rationale; the parsing itself is `parse_announcements()`.
        """
        resp = self._get(
            self._pylibrus_config.announcements_path,
            referer=synergia_url_from_path("/rodzic/index"),
        )
        if "Brak dostępu" in resp.text:
            raise RuntimeError("Brak dostępu when fetching announcements - cookies likely invalid")
        return parse_announcements(resp.text)


def open_scraper(pylibrus_config: PyLibrusConfig, librus_user: LibrusUser) -> LibrusScraper:
    """A scraper over this user's (cookie-cached) Librus session; entering it logs in."""
    return LibrusScraper(LibrusSession(librus_user.login, librus_user.password, pylibrus_config), pylibrus_config)
