from pylibrus.db.store import MessageStore
from pylibrus.notify.base import Notifier
from pylibrus.notify.smtp import SmtpNotifier
from pylibrus.notify.webhook import WebhookNotifier
from pylibrus.settings import LibrusUser, PyLibrusConfig


def make_notifier(librus_user: LibrusUser, store: MessageStore) -> Notifier:
    """The transport for this user's destination. `store` supplies the attachments to send."""
    if librus_user.notify.is_webhook():
        return WebhookNotifier(librus_user, store)
    return SmtpNotifier(librus_user, store)


class LibrusNotifier:
    """One Librus user's sender, plus the MessageStore holding the rows it sends.

    The store and the sending live together until the collect phase stops handing live ORM rows
    to the notify phase (REFACTOR_PLAN.md step 6); until then the store methods are delegated.
    """

    def __init__(self, pylibrus_config: PyLibrusConfig, librus_user: LibrusUser):
        self._pylibrus_config = pylibrus_config
        self._librus_user = librus_user
        self._store = MessageStore(pylibrus_config.workdir, librus_user.db_name)
        self._sender = make_notifier(librus_user, self._store)

    def __enter__(self):
        self._store.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._store.__exit__(exc_type, exc_val, exc_tb)

    @property
    def librus_user(self) -> LibrusUser:
        return self._librus_user

    @property
    def store(self) -> MessageStore:
        return self._store

    def commit(self):
        """See MessageStore.commit(): called at each phase boundary, not only at __exit__."""
        self._store.commit()

    def get_msg(self, url):
        return self._store.get_msg(url)

    def add_msg(self, url, folder_id, sender, date, subject, contents_html, contents_text, attachments):
        return self._store.add_msg(url, folder_id, sender, date, subject, contents_html, contents_text, attachments)

    def get_announcement(self, url):
        return self._store.get_announcement(url)

    def add_announcement(self, url, sender, subject, date, contents_html, contents_text):
        return self._store.add_announcement(url, sender, subject, date, contents_html, contents_text)

    def notify(self, msg_from_db, display_name: str | None = None):
        """Sends one item out. `display_name` overrides the Librus user name shown to the reader.

        It carries the names of *every* child the item was sent to, so the reader can tell
        whether it concerns one child or several; it defaults to this notifier's own user.
        """
        self._sender.notify(msg_from_db, display_name)
