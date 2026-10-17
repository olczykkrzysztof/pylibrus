import argparse
import configparser
import datetime
import logging
import sys
from pathlib import Path

if __name__ == "__main__" and not __package__:
    # Run by path (`uv run src/pylibrus/pylibrus.py`, as the Procfile does): Python puts this
    # file's directory first on sys.path, where `import pylibrus` would find this very file
    # instead of the package. Swap it for src/. Goes away with this module in REFACTOR_PLAN.md
    # step 8, when the entry point becomes `uv run pylibrus`.
    _here = Path(__file__).resolve().parent
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _here]
    sys.path.insert(0, str(_here.parent))


from pylibrus.db.schema import Attachment as Attachment
from pylibrus.db.schema import Base as Base
from pylibrus.db.schema import LibrusAnnouncement as LibrusAnnouncement
from pylibrus.db.schema import Msg as Msg
from pylibrus.db.store import MessageStore as MessageStore
from pylibrus.logging_setup import configure_logging as configure_logging
from pylibrus.notify.base import Notifier as Notifier
from pylibrus.notify.notifier import LibrusNotifier as LibrusNotifier
from pylibrus.notify.notifier import make_notifier as make_notifier
from pylibrus.notify.render import build_email_message as build_email_message
from pylibrus.notify.render import build_webhook_payload as build_webhook_payload
from pylibrus.notify.render import format_sender as format_sender
from pylibrus.notify.smtp import SmtpNotifier as SmtpNotifier
from pylibrus.notify.webhook import WebhookNotifier as WebhookNotifier
from pylibrus.pipeline.collect import CollectedItem as CollectedItem
from pylibrus.pipeline.collect import UserCollection as UserCollection
from pylibrus.pipeline.collect import collect_all as collect_all
from pylibrus.pipeline.collect import collect_announcements as collect_announcements
from pylibrus.pipeline.collect import collect_user as collect_user
from pylibrus.pipeline.collect import needs_attachment_content as needs_attachment_content
from pylibrus.pipeline.dispatch import notify_collected as notify_collected
from pylibrus.pipeline.dispatch import notify_group as notify_group
from pylibrus.pipeline.dispatch import warn_on_mixed_attachments_source as warn_on_mixed_attachments_source
from pylibrus.pipeline.grouping import ItemGroup as ItemGroup
from pylibrus.pipeline.grouping import group_by_destination as group_by_destination
from pylibrus.pipeline.grouping import group_items_by_url as group_items_by_url
from pylibrus.pipeline.grouping import group_names as group_names
from pylibrus.pipeline.grouping import should_notify_group as should_notify_group
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
    # be labelled with all of them once all of them have been scraped.
    with collect_all(pylibrus_config, librus_users) as collections:
        notify_collected(pylibrus_config, collections, dry_run=args.dry)


if __name__ == "__main__":
    sys.exit(main())
