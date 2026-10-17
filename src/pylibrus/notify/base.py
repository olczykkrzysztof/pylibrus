from typing import Protocol


class Notifier(Protocol):
    """Sends one item - a Msg or a LibrusAnnouncement - to one destination.

    `display_name` is the label the reader sees (every child the item reached); None means the
    notifier's own Librus user.
    """

    def notify(self, msg_from_db, display_name: str | None = None) -> None: ...
