from pylibrus.pipeline.collect import CollectedItem, UserCollection
from pylibrus.settings import PyLibrusConfig

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
