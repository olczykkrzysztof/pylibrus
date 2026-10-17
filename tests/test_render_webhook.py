"""Unit tests for `build_webhook_payload`: no HTTP, just an item in and a payload out. The
end-to-end version (Librus and S3 links, through LibrusNotifier) is in
test_notification_rendering.py."""

import datetime

from pylibrus.db.schema import LibrusAnnouncement, Msg
from pylibrus.notify.render import build_webhook_payload

DATE = datetime.datetime(2025, 9, 8, 14, 30)


def test_message_payload_with_attachment_links():
    item = Msg(url="/w/1", sender="Jan", subject="Wycieczka", date=DATE, contents_html="", contents_text="tresc")

    payload = build_webhook_payload(item, "Ania, Jas", [("zgoda.pdf", "https://example.com/z")])

    assert payload == {
        "text": (
            "\n*LIBRUS Ania, Jas - 2025-09-08 14:30:00*\n*Od: Jan*\n*Temat: Wycieczka*\n"
            "\ntresc\n\nZałączniki:\n- <https://example.com/z|zgoda.pdf>\n"
        ),
    }


def test_announcement_payload_uses_its_label_and_has_no_attachment_section():
    item = LibrusAnnouncement(
        url="/ogloszenia#1", sender="Dyrektor", subject="Zebranie", date=DATE, contents_html="", contents_text="a"
    )

    assert build_webhook_payload(item, "Ania", []) == {
        "text": "\n*LIBRUS Ania - 2025-09-08 14:30:00*\n*Od: Dyrektor*\n*Ogłoszenie: Zebranie*\n\na",
    }
