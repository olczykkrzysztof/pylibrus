import argparse
import configparser
import contextlib
import dataclasses
import datetime
import json
import logging
import smtplib
import sys
import time
from pathlib import Path

if __name__ == "__main__" and not __package__:
    # Run by path (`uv run src/pylibrus/pylibrus.py`, as the Procfile does): Python puts this
    # file's directory first on sys.path, where `import pylibrus` would find this very file
    # instead of the package. Swap it for src/. Goes away with this module in REFACTOR_PLAN.md
    # step 8, when the entry point becomes `uv run pylibrus`.
    _here = Path(__file__).resolve().parent
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _here]
    sys.path.insert(0, str(_here.parent))

import requests

from pylibrus.db.schema import Attachment as Attachment
from pylibrus.db.schema import Base as Base
from pylibrus.db.schema import LibrusAnnouncement as LibrusAnnouncement
from pylibrus.db.schema import Msg as Msg
from pylibrus.db.store import MessageStore as MessageStore
from pylibrus.logging_setup import configure_logging as configure_logging
from pylibrus.notify.render import build_email_message as build_email_message
from pylibrus.notify.render import build_webhook_payload as build_webhook_payload
from pylibrus.notify.render import format_sender as format_sender
from pylibrus.scraper.attachments import FAILED_TO_DOWNLOAD_ATTACHMENT_DATA as FAILED_TO_DOWNLOAD_ATTACHMENT_DATA
from pylibrus.scraper.client import LibrusScraper as LibrusScraper
from pylibrus.scraper.client import open_scraper as open_scraper
from pylibrus.scraper.session import LibrusSession as LibrusSession
from pylibrus.scraper.urls import attachment_download_link as attachment_download_link
from pylibrus.settings import DEFAULT_WEBHOOK_ATTACHMENTS_SOURCE as DEFAULT_WEBHOOK_ATTACHMENTS_SOURCE
from pylibrus.settings import EmailNotify as EmailNotify
from pylibrus.settings import LibrusUser as LibrusUser
from pylibrus.settings import Notify as Notify
from pylibrus.settings import PyLibrusConfig as PyLibrusConfig
from pylibrus.settings import WebhookNotify as WebhookNotify
from pylibrus.settings import validate_fields as validate_fields
from pylibrus.storage.s3 import LINK_EXPIRE_DURATION as LINK_EXPIRE_DURATION
from pylibrus.storage.s3 import S3AttachmentStorage as S3AttachmentStorage
from pylibrus.storage.s3 import build_download_content_disposition as build_download_content_disposition
from pylibrus.storage.s3 import is_s3_webhook_source as is_s3_webhook_source
from pylibrus.storage.s3 import parse_s3_source as parse_s3_source
from pylibrus.storage.s3 import sanitize_s3_segment as sanitize_s3_segment
from pylibrus.utils import FALSE_VALUES as FALSE_VALUES
from pylibrus.utils import TRUE_VALUES as TRUE_VALUES
from pylibrus.utils import announcement_id as announcement_id
from pylibrus.utils import retrieve_from as retrieve_from
from pylibrus.utils import str_to_bool as str_to_bool
from pylibrus.utils import str_to_int as str_to_int

logger = logging.getLogger(__name__)


class LibrusNotifier:
    """One Librus user's sender, plus the MessageStore holding the rows it sends.

    The store and the sending live together until the collect phase stops handing live ORM rows
    to the notify phase (REFACTOR_PLAN.md step 6); until then the store methods are delegated.
    """

    def __init__(self, pylibrus_config: PyLibrusConfig, librus_user: LibrusUser):
        self._pylibrus_config = pylibrus_config
        self._librus_user = librus_user
        self._store = MessageStore(pylibrus_config.workdir, librus_user.db_name)

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
        if self._librus_user.notify.is_webhook():
            logger.info(f"Sending '{msg_from_db.subject}' to webhook from {msg_from_db.sender} ({msg_from_db.date})")
            self.send_via_webhook(msg_from_db, display_name)
        else:
            logger.info(
                f"Sending '{msg_from_db.subject}' to {self._librus_user.notify.email_dest} from {msg_from_db.sender}"
            )
            self.send_email(msg_from_db, display_name)

    def _get_attachments(self, msg_from_db) -> list[Attachment]:
        return self._store.attachments_for(msg_from_db.url)

    @staticmethod
    def _librus_attachment_links(attachments: list[Attachment]) -> list[tuple[str, str]]:
        return [(a.name, attachment_download_link(a.link_id)) for a in attachments]

    def _s3_attachment_links(self, msg_from_db, attachments: list[Attachment]) -> list[tuple[str, str]]:
        links: list[tuple[str, str]] = []
        storage = S3AttachmentStorage(self._librus_user.notify)
        for attach in attachments:
            fallback_link = attachment_download_link(attach.link_id)
            if attach.data is None:
                logger.warning(f"Attachment '{attach.name}' has no data in DB, fallback to Librus link")
                links.append((attach.name, fallback_link))
                continue
            try:
                if not attach.s3_key:
                    s3_key, s3_etag = storage.upload_attachment(
                        librus_user_name=self._librus_user.name,
                        msg_path=msg_from_db.url,
                        attachment=attach,
                    )
                    attach.s3_key = s3_key
                    attach.s3_etag = s3_etag
                    attach.s3_upload_date = datetime.datetime.now()
                    logger.info(f"Uploaded attachment '{attach.name}' to S3 key '{attach.s3_key}'")
                else:
                    logger.info(f"Reusing uploaded S3 key for attachment '{attach.name}': {attach.s3_key}")
                links.append((attach.name, storage.generate_download_link(attach)))
            except Exception as ex:
                logger.warning(f"Failed to upload/presign attachment '{attach.name}' ({ex}), fallback to Librus link")
                links.append((attach.name, fallback_link))
        return links

    def _build_webhook_attachment_links(self, msg_from_db) -> list[tuple[str, str]]:
        attachments = self._get_attachments(msg_from_db)
        if not attachments:
            return []
        if not is_s3_webhook_source(self._librus_user.notify.webhook_attachments_source):
            return self._librus_attachment_links(attachments)
        try:
            return self._s3_attachment_links(msg_from_db, attachments)
        except Exception as ex:
            logger.warning(f"Failed to initialize S3 attachment storage ({ex}), fallback to Librus links")
            return self._librus_attachment_links(attachments)

    def send_via_webhook(self, msg_from_db, display_name: str | None = None):
        attachment_links = self._build_webhook_attachment_links(msg_from_db)
        name = display_name or self._librus_user.name

        message = build_webhook_payload(msg_from_db, name, attachment_links)

        response = requests.post(
            self._librus_user.notify.webhook,
            data=json.dumps(message),
            headers={"Content-Type": "application/json"},
        )

        if response.status_code != 200:
            logger.warning(f"Failed to send message. Status code: {response.status_code}")

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


def read_pylibrus_config(workdir: str, config_file: str) -> tuple[PyLibrusConfig, list[LibrusUser]]:
    config_path = Path(workdir) / config_file
    if config_path.exists():
        logger.info(f"Read config from file: {config_path}")
        config = configparser.ConfigParser()
        config.read(config_path)
        pylibrus_config = PyLibrusConfig.from_config(workdir, config)
        librus_users = LibrusUser.load_librus_users_from_config(config)
    else:
        logger.info(f"Could not find config file: {config_path}, read config from env variables")
        pylibrus_config = PyLibrusConfig.from_env(workdir)
        librus_users = [LibrusUser.from_env()]
    if librus_users and not any(user.always_include_in_subject for user in librus_users):
        logger.warning(
            "No configured user has always_include_in_subject enabled, so every grouped "
            "notification falls back to naming all of its children - the setting has no effect",
        )
    return pylibrus_config, librus_users


def send_test_notification(pylibrus_config: PyLibrusConfig, librus_user: LibrusUser):
    notifier = LibrusNotifier(pylibrus_config, librus_user)
    msg = Msg(
        url="/fake/object",
        folder="Odebrane",
        sender="Testing sender Żółta Jaźń [Nauczyciel]",
        date=datetime.datetime.now(),
        subject="Testing subject with żółta jaźć",
        contents_html="<h2>html content with żółta jażń</h2>",
        contents_text="text content with żółta jaźń",
    )
    logger.info("Sending testing notify")
    notifier.notify(msg)

    announcement = LibrusAnnouncement(
        url=announcement_id("Testing announcement", "Testing author", datetime.datetime.now()),
        sender="Testing author Żółta Jaźń [Dyrektor]",
        subject="Testing announcement with żółta jaźć",
        date=datetime.datetime.now(),
        contents_html="<p>html announcement content with żółta jażń</p>",
        contents_text="text announcement content with żółta jaźń",
    )
    logger.info("Sending testing announcement notify")
    notifier.notify(announcement)
    return 2


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

            fetch_attachment_content = pylibrus_config.fetch_attachments and (
                librus_user.notify.is_email()
                or (
                    librus_user.notify.is_webhook()
                    and is_s3_webhook_source(librus_user.notify.webhook_attachments_source)
                )
            )
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


ItemGroup = list[tuple[UserCollection, CollectedItem]]


def group_by_destination(collections: list[UserCollection]) -> dict[tuple, list[UserCollection]]:
    """Buckets users by where their notifications go.

    Insertion order is config order (the run no longer shuffles users), and dict preserves it,
    so each group's first member is its representative - which fixes the sending account and
    the order names appear in the label. See MULTI_RECIPIENT_PLAN.md.
    """
    groups: dict[tuple, list[UserCollection]] = {}
    for collection in collections:
        groups.setdefault(collection.librus_user.notify.destination_key(), []).append(collection)
    return groups


def group_items_by_url(destination_collections: list[UserCollection]) -> list[ItemGroup]:
    """Groups one destination's items by url, oldest first.

    Librus gives the same message the same path in every child's account, so an equal `url` is
    what identifies one item received by several children (for announcements it is the
    synthetic announcement_id, equal across children by construction).
    """
    groups: dict[str, ItemGroup] = {}
    for collection in destination_collections:
        for collected in collection.items:
            groups.setdefault(collected.item.url, []).append((collection, collected))
    return sorted(groups.values(), key=lambda group: group[0][1].item.date)


def should_notify_group(pylibrus_config: PyLibrusConfig, group: ItemGroup) -> tuple[bool, str]:
    """Per-group lift of the old per-message dispatch; neither mode's criterion changes.

    In "unread" mode the decision belongs to the unread state on Librus rather than to our own
    email_sent bookkeeping, so an item nobody has read yet is re-sent on every run - that is
    the mode working as intended, and email_sent is deliberately not consulted.

    Reads CollectedItem.email_sent, the value captured during collect, never the live column.
    Children on different destinations share one Msg row, so consulting the column would let
    the first destination's send mark that row and mute every later destination - only one
    destination would ever be notified. See MULTI_RECIPIENT_PLAN.md.
    """
    # Announcements have no per-item read/unread flag, so they always dedupe the "unsent" way
    # regardless of send_message - see ANNOUNCEMENTS_PLAN.md.
    is_announcement = group[0][1].read is None

    if pylibrus_config.send_message == "unread" and not is_announcement:
        if all(collected.read for _, collected in group):
            return False, "already read"
        return True, ""

    if any(collected.email_sent for _, collected in group):
        return False, "already sent"
    return True, ""


def group_names(group: ItemGroup) -> tuple[str, str]:
    """Returns (display_name, recipient_names): what the reader sees, and the whole truth.

    A child configured with `always_include_in_subject=false` is left out of `display_name`,
    but only while another child remains to name - an item that reached only such children is
    still labelled with them rather than with nothing at all. See SUBJECT_NAMES_PLAN.md.
    """
    users = [collection.librus_user for collection, _ in group]
    recipient_names = ", ".join(user.name for user in users)
    always_named = [user.name for user in users if user.always_include_in_subject]
    return (", ".join(always_named) if always_named else recipient_names), recipient_names


def notify_group(
    pylibrus_config: PyLibrusConfig, group: ItemGroup, should_notify: bool, reason: str, dry_run: bool = False,
):
    representative, first = group[0]
    item = first.item
    display_name, recipient_names = group_names(group)
    # Logs name every child that received the item even when the notification itself leaves
    # some of them out, so that --dry and the skip lines never under-report who it reached.
    logged_for = display_name
    if display_name != recipient_names:
        logged_for = f"{display_name} (received by: {recipient_names})"

    if dry_run:
        # Do not notify and do not touch email_sent, so a regular (non-dry) run afterwards
        # still sends this item normally.
        verdict = "would send" if should_notify else f"would skip ({reason})"
        logger.info(f"[DRY RUN] '{item.subject}' for {logged_for}: {verdict}")
        return

    if not should_notify:
        logger.info(f"Do not send '{item.subject}' for {logged_for} ({reason})")
        return

    warn_on_mixed_attachments_source(group)
    representative.notifier.notify(item, display_name=display_name)
    for collection, collected in group:
        collected.item.email_sent = True
        collected.email_sent = True
        collection.notifier.commit()


def warn_on_mixed_attachments_source(group: ItemGroup):
    """Warns when users grouped onto one webhook disagree on where attachments come from.

    destination_key() groups them on purpose (see WebhookNotify.destination_key), but only the
    representative's attachment config is used, so say so rather than silently picking one.
    """
    sources = {
        collection.librus_user.notify.webhook_attachments_source
        for collection, _ in group
        if collection.librus_user.notify.is_webhook()
    }
    if len(sources) > 1:
        representative = group[0][0].librus_user.name
        logger.warning(
            f"Users sharing one webhook disagree on webhook_attachments_source ({sorted(sources)}), "
            f"using {representative}'s",
        )


def notify_collected(pylibrus_config: PyLibrusConfig, collections: list[UserCollection], dry_run: bool = False):
    """Sends one notification per (destination, item), labelled with that destination's children.

    A message the school sent to several children appears in each of their accounts under the
    same url, so it must go out once per destination rather than once per child - and say which
    children it concerns, which is the point of the whole exercise. Each destination hears only
    about the children it is configured for: with two children forwarded to one address and
    three to another, each address gets one grouped notification naming its own children.
    """
    for destination_collections in group_by_destination(collections).values():
        for group in group_items_by_url(destination_collections):
            should_notify, reason = should_notify_group(pylibrus_config, group)
            notify_group(pylibrus_config, group, should_notify, reason, dry_run=dry_run)


def handle_user(pylibrus_config: PyLibrusConfig, librus_user: LibrusUser, dry_run: bool = False):
    """Collects and notifies a single user. Cross-user grouping needs main()'s loop."""
    with open_scraper(pylibrus_config, librus_user) as scraper:
        with LibrusNotifier(pylibrus_config, librus_user) as notifier:
            collection = collect_user(pylibrus_config, librus_user, scraper, notifier)
            notify_collected(pylibrus_config, [collection], dry_run=dry_run)


def handle_announcements(
    pylibrus_config: PyLibrusConfig, notifier: "LibrusNotifier", scraper: LibrusScraper, dry_run: bool = False,
):
    """Collects and notifies a single user's announcements."""
    collection = UserCollection(
        librus_user=notifier.librus_user,
        notifier=notifier,
        items=collect_announcements(pylibrus_config, notifier, scraper),
    )
    notify_collected(pylibrus_config, [collection], dry_run=dry_run)


def parse_args():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument("--debug", action="store_true", help="enable debug")

    paths = parser.add_argument_group("Paths")
    paths.add_argument(
        "--config", metavar="PATH", help="config file, can be absolute or relative to workdir", default="pylibrus.ini"
    )
    paths.add_argument(
        "--cookies",
        metavar="PATH",
        help="cookie file, can be absolute or relative to workdir",
        default="pylibrus_cookies.json",
    )
    paths.add_argument("--workdir", metavar="PATH", help="working directory with config and DBs", default=Path.cwd())

    tests = parser.add_argument_group("Test notifications")
    tests.add_argument("--test-notify", action="store_true", default=False, help="send a test notification")

    dry_run = parser.add_argument_group("Dry run")
    dry_run.add_argument(
        "--dry",
        action="store_true",
        default=False,
        help=(
            "go through all messages without sending them by email or webhook; "
            "just print each message's title and whether it was already sent "
            "(does not touch the email_sent flag, so a regular run afterwards still sends it)"
        ),
    )

    return parser.parse_args()


def main():
    args = parse_args()

    pylibrus_config, librus_users = read_pylibrus_config(args.workdir, args.config)
    pylibrus_config.debug |= args.debug
    pylibrus_config.cookie_file = args.cookies
    logger.info(f"Config: {pylibrus_config}")
    for user in librus_users:
        logger.info(f"User: {user}")
    # Only now, so the two lines above stay unprinted as they always have been (logging used to be
    # configured by the first LibrusScraper, created after them): printing them would put each
    # user's webhook URL - a credential - in the cron log on every run.
    configure_logging(pylibrus_config.debug)

    if args.test_notify:
        return send_test_notification(pylibrus_config, librus_users[0])

    # Collect every user before notifying anyone: a message sent to several children can only
    # be labelled with all of them once all of them have been scraped. The notifiers stay open
    # across both phases because their sessions own the collected rows.
    with contextlib.ExitStack() as stack:
        collections = []
        for i, librus_user in enumerate(librus_users):
            try:
                notifier = stack.enter_context(LibrusNotifier(pylibrus_config, librus_user))
                with open_scraper(pylibrus_config, librus_user) as scraper:
                    collections.append(collect_user(pylibrus_config, librus_user, scraper, notifier))
            except Exception:
                # One broken account (changed password, throttling) must not stop the other
                # children being notified - see MULTI_RECIPIENT_PLAN.md.
                logger.exception(f"Failed to collect messages for {librus_user.name}")
            if i != len(librus_users) - 1:
                time.sleep(pylibrus_config.sleep_between_librus_users)

        notify_collected(pylibrus_config, collections, dry_run=args.dry)


if __name__ == "__main__":
    sys.exit(main())
