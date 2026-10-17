import logging
import smtplib

from pylibrus.db.store import MessageStore
from pylibrus.notify.render import build_email_message
from pylibrus.settings import LibrusUser

logger = logging.getLogger(__name__)


class SmtpNotifier:
    """Emails items over SMTP (STARTTLS), embedding downloaded attachments."""

    def __init__(self, librus_user: LibrusUser, store: MessageStore):
        self._librus_user = librus_user
        self._store = store

    def notify(self, msg_from_db, display_name: str | None = None):
        logger.info(
            f"Sending '{msg_from_db.subject}' to {self._librus_user.notify.email_dest} from {msg_from_db.sender}"
        )
        self.send_email(msg_from_db, display_name)

    def send_email(self, msg_from_db, display_name: str | None = None):
        name = display_name or self._librus_user.name
        msg = build_email_message(
            msg_from_db,
            name,
            self._librus_user.notify,
            self._store.attachments_for(msg_from_db.url),  # none when testing: the store isn't open
        )

        server = smtplib.SMTP(self._librus_user.notify.smtp_server, self._librus_user.notify.smtp_port)
        server.ehlo()
        server.starttls()
        server.login(self._librus_user.notify.smtp_user, self._librus_user.notify.smtp_pass)
        server.sendmail(self._librus_user.notify.smtp_user, self._librus_user.notify.email_dest, msg.as_string())
        server.close()
