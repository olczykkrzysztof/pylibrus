import contextlib
import dataclasses
import datetime
import logging
import time
from collections.abc import Callable, Iterator

from pylibrus.db.schema import LibrusAnnouncement, Msg
from pylibrus.notify.notifier import LibrusNotifier
from pylibrus.scraper.client import LibrusScraper
from pylibrus.scraper.client import open_scraper as open_librus_scraper
from pylibrus.settings import LibrusUser, Notify, PyLibrusConfig
from pylibrus.storage.s3 import is_s3_webhook_source
from pylibrus.utils import announcement_id

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class CollectedItem:
    """One message or announcement as seen by one Librus user during the collect phase."""

    item: Msg | LibrusAnnouncement
    read: bool | None  # per-user unread state from the folder listing; None for announcements
    email_sent: bool = False  # as it stood when collected, i.e. before anything this run was sent


@dataclasses.dataclass
class UserCollection:
    """Everything one Librus user received in this run, with the notifier that owns the rows.

    The notifier has to be carried along: its session owns `item`, and its LibrusUser holds
    the credentials and destination config needed to send.
    """

    librus_user: LibrusUser
    notifier: "LibrusNotifier"
    items: list[CollectedItem] = dataclasses.field(default_factory=list)


@contextlib.contextmanager
def collect_all(
    pylibrus_config: PyLibrusConfig,
    librus_users: list[LibrusUser],
    open_notifier: Callable[[PyLibrusConfig, LibrusUser], LibrusNotifier] = LibrusNotifier,
    open_scraper: Callable[[PyLibrusConfig, LibrusUser], LibrusScraper] = open_librus_scraper,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[list["UserCollection"]]:
    """The collect phase: every user in config order, notifying nothing. Yields their collections.

    A context manager because each user's notifier has to outlive this phase - its session owns
    the collected rows - so the notifiers close only when the `with` block, and with it the notify
    phase, is done. A user whose collect raises (changed password, throttling) is logged and
    skipped, so the other children are still notified - see MULTI_RECIPIENT_PLAN.md.

    `open_notifier`, `open_scraper` and `sleep` are injectable so tests run this real loop with
    fakes and without the pause between users.
    """
    with contextlib.ExitStack() as stack:
        collections = []
        for i, librus_user in enumerate(librus_users):
            try:
                notifier = stack.enter_context(open_notifier(pylibrus_config, librus_user))
                with open_scraper(pylibrus_config, librus_user) as scraper:
                    collections.append(collect_user(pylibrus_config, librus_user, scraper, notifier))
            except Exception:
                # One broken account (changed password, throttling) must not stop the other
                # children being notified - see MULTI_RECIPIENT_PLAN.md.
                logger.exception(f"Failed to collect messages for {librus_user.name}")
            if i != len(librus_users) - 1:
                sleep(pylibrus_config.sleep_between_librus_users)
        yield collections


def needs_attachment_content(pylibrus_config: PyLibrusConfig, notify: Notify) -> bool:
    """Whether attachment bytes have to be downloaded at all for this destination.

    The single place deciding it - keep it in sync when adding an attachment delivery mode. Email
    embeds the bytes and an s3:// webhook re-uploads them; a librus_link webhook only links back
    to Librus, so it never downloads.
    """
    return pylibrus_config.fetch_attachments and (
        notify.is_email() or (notify.is_webhook() and is_s3_webhook_source(notify.webhook_attachments_source))
    )


def collect_user(
    pylibrus_config: PyLibrusConfig, librus_user: LibrusUser, scraper: LibrusScraper, notifier: "LibrusNotifier",
) -> UserCollection:
    """Scrapes and stores everything new for one user, notifying nothing.

    Splitting collect from notify is what makes the multi-recipient annotation possible at all:
    while a single pass is still scraping the first child, nothing yet knows that the second
    child received the same message, so there is no way to label it correctly. See
    MULTI_RECIPIENT_PLAN.md.

    The scraper and notifier are arguments rather than locals so that both phases can be
    driven with fakes in tests, the way handle_announcements() already could be.
    """
    collection = UserCollection(librus_user=librus_user, notifier=notifier)

    msgs = scraper.msgs_from_folder(pylibrus_config.inbox_folder_id)
    for msg_path, read in msgs:
        msg = notifier.get_msg(msg_path)

        if not msg:
            logger.debug(f"Fetch {msg_path}")

            fetch_attachment_content = needs_attachment_content(pylibrus_config, librus_user.notify)
            msg_content_or_none = scraper.fetch_msg(msg_path, fetch_attachment_content)
            if msg_content_or_none is None:
                continue
            sender, subject, date, contents_html, contents_text, attachments = msg_content_or_none
            msg = notifier.add_msg(
                msg_path,
                pylibrus_config.inbox_folder_id,
                sender,
                date,
                subject,
                contents_html,
                contents_text,
                attachments,
            )

        collection.items.append(CollectedItem(item=msg, read=read, email_sent=bool(msg.email_sent)))

    fetch_announcements = (
        librus_user.fetch_announcements
        if librus_user.fetch_announcements is not None
        else pylibrus_config.fetch_announcements
    )
    if fetch_announcements:
        # A Librus markup change on /ogloszenia must never stop message
        # forwarding above, which is the primary feature - see ANNOUNCEMENTS_PLAN.md.
        try:
            collection.items.extend(collect_announcements(pylibrus_config, notifier, scraper))
        except Exception:
            logger.exception("Failed to fetch announcements")

    # Do not leave an open write transaction behind while the next user's session writes to
    # the same SQLite file - see LibrusNotifier.commit().
    notifier.commit()
    return collection


def collect_announcements(
    pylibrus_config: PyLibrusConfig, notifier: "LibrusNotifier", scraper: LibrusScraper,
) -> list[CollectedItem]:
    announcements = scraper.fetch_announcements()
    collected = []
    for title, author, date, contents_html, contents_text in reversed(announcements):
        age = datetime.datetime.now() - date
        if age > datetime.timedelta(days=pylibrus_config.max_age_of_sending_announcement_days):
            logger.info(f"Do not send announcement '{title}' (too old, {date})")
            continue

        url = announcement_id(title, author, date)
        announcement = notifier.get_announcement(url)
        if not announcement:
            announcement = notifier.add_announcement(url, author, title, date, contents_html, contents_text)
        collected.append(CollectedItem(item=announcement, read=None, email_sent=bool(announcement.email_sent)))
    return collected
