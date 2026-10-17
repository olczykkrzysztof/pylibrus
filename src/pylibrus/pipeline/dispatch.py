import logging

from pylibrus.pipeline.collect import UserCollection
from pylibrus.pipeline.grouping import (
    ItemGroup,
    group_by_destination,
    group_items_by_url,
    group_names,
    should_notify_group,
)
from pylibrus.settings import PyLibrusConfig

logger = logging.getLogger(__name__)


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
