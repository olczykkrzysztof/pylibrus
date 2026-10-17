"""Unit tests for `build_email_message`: no SMTP server and no DB, just an item in and a message out.
The end-to-end version, through LibrusNotifier and a fake SMTP server, is in
test_notification_rendering.py."""

import datetime
from email.header import decode_header, make_header

from pylibrus.db.schema import Attachment, LibrusAnnouncement, Msg
from pylibrus.notify.render import build_email_message, format_sender
from pylibrus.settings import EmailNotify

DATE = datetime.datetime(2025, 9, 8, 14, 30)
NOTIFY = EmailNotify(smtp_user="smtp-user@example.com", smtp_pass="p", smtp_server="s", email_dest="a@x.pl, b@x.pl")


def msg(**kwargs) -> Msg:
    fields = {"url": "/w/1", "sender": "Jan", "subject": "Wycieczka", "date": DATE, "contents_html": "<p>t</p>"}
    return Msg(**(fields | {"contents_text": "t"} | kwargs))


def decoded(part) -> str:
    return part.get_payload(decode=True).decode(part.get_content_charset())


def test_subject_carries_the_label_and_the_per_type_prefix():
    announcement = LibrusAnnouncement(
        url="/ogloszenia#1", sender="Dyrektor", subject="Zebranie", date=DATE, contents_html="", contents_text=""
    )

    assert build_email_message(msg(), "Ania, Jas", NOTIFY, [])["Subject"] == "[LIBRUS Ania, Jas] Wycieczka"
    subject = build_email_message(announcement, "Ania", NOTIFY, [])["Subject"]
    assert str(make_header(decode_header(subject))) == "[LIBRUS Ania] Ogłoszenie: Zebranie"


def test_sender_is_named_after_the_teacher_and_sent_from_the_smtp_account():
    sent = build_email_message(msg(sender="Żaneta Ćwik"), "Ania", NOTIFY, [])

    assert sent["From"] == format_sender("Żaneta Ćwik", "smtp-user@example.com")
    assert str(make_header(decode_header(sent["From"].split(" <")[0].strip('"')))) == "Żaneta Ćwik @ Librus"
    assert sent["To"] == "a@x.pl, b@x.pl"


def test_downloaded_attachments_are_embedded_and_the_rest_are_linked():
    attachments = [
        Attachment(link_id="1/77", name="zgoda.pdf", data=b"%PDF"),
        Attachment(link_id="1/78", name="plan.docx", data=None),
    ]

    html, plain, embedded = build_email_message(msg(), "Ania", NOTIFY, attachments).get_payload()

    assert embedded.get_filename() == "zgoda.pdf"
    assert embedded.get_payload(decode=True) == b"%PDF"
    link = "https://synergia.librus.pl/wiadomosci/pobierz_zalacznik/1/78"
    assert decoded(plain).endswith(f"\n\nZałączniki:\n{link}")
    assert f"<li><a href='{link}'>plan.docx</a></li>" in decoded(html)


def test_banner_and_date_follow_the_body():
    html, plain = build_email_message(msg(), "Ania", NOTIFY, []).get_payload()

    assert decoded(plain).startswith("t\n\n-------------------\n\n!! TO WIADOMOŚĆ Z LIBRUSA")
    assert "<b> Data wysłania:</b> 2025-09-08 14:30:00 <br>" in decoded(html)
