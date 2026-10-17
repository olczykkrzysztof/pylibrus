"""Characterisation tests for what `LibrusNotifier.notify()` actually sends.

Pins the full email (headers, HTML and plain parts, banner, attachments as links and as MIME
parts, the SMTP conversation) and the full webhook payload (Librus links and S3 links), so that
extracting rendering and transport (REFACTOR_PLAN.md step 4) can prove it changed nothing.

The patches target the third-party entry points themselves (`smtplib.SMTP`, `requests.post`,
`boto3.client`) rather than names inside pylibrus, so these tests stay valid however the code
calling them is split up. The DB is a real SQLite file in tmp_path, like the other tests.
"""

import datetime
import email
import json
import smtplib
from email.header import decode_header, make_header

import boto3
import pytest
import requests

from pylibrus.pylibrus import Attachment, EmailNotify, LibrusNotifier, LibrusUser, Msg, WebhookNotify

MSG_URL = "/wiadomosci/1/5/1002/f0"
DATE = datetime.datetime(2025, 9, 8, 14, 30)
LINK_77 = "https://synergia.librus.pl/wiadomosci/pobierz_zalacznik/1002/77"
LINK_78 = "https://synergia.librus.pl/wiadomosci/pobierz_zalacznik/1002/78"


# --- fakes -----------------------------------------------------------------------------------


@pytest.fixture
def smtp_calls(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, server, port):
            calls.append(("connect", server, port))

        def ehlo(self):
            calls.append(("ehlo",))

        def starttls(self):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user, password))

        def sendmail(self, from_addr, to_addrs, body):
            calls.append(("sendmail", from_addr, to_addrs, body))

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return calls


@pytest.fixture
def posts(monkeypatch):
    posted = []

    class FakeResponse:
        status_code = 200

    def fake_post(url, data=None, headers=None, **kwargs):
        posted.append({"url": url, "data": data, "headers": headers, "kwargs": kwargs})
        return FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)
    return posted


@pytest.fixture
def s3(monkeypatch):
    """A fake boto3 S3 client; set `fail_uploads` to make put_object raise."""

    class FakeS3Client:
        def __init__(self):
            self.uploads = []
            self.client_kwargs = None
            self.fail_uploads = False

        def put_object(self, **kwargs):
            if self.fail_uploads:
                raise RuntimeError("S3 is down")
            self.uploads.append(kwargs)
            return {"ETag": '"etag-1"'}

        def generate_presigned_url(self, operation, Params, ExpiresIn):  # noqa: N803 - boto3's names
            return f"https://s3.example.com/{Params['Bucket']}/{Params['Key']}?expires={ExpiresIn}"

    client = FakeS3Client()

    def fake_client(service, **kwargs):
        assert service == "s3"
        client.client_kwargs = kwargs
        return client

    monkeypatch.setattr(boto3, "client", fake_client)
    return client


# --- helpers ---------------------------------------------------------------------------------


def email_user() -> LibrusUser:
    notify = EmailNotify(
        smtp_user="smtp-user@example.com",
        smtp_pass="secret",
        smtp_server="smtp.example.com",
        smtp_port=2525,
        email_dest="a@example.com, b@example.com",
    )
    return LibrusUser(login="l", password="p", name="Ania", notify=notify, db_name="test.sqlite")


def webhook_user(**webhook_kwargs) -> LibrusUser:
    notify = WebhookNotify(webhook="https://hooks.example.com/parents", **webhook_kwargs)
    return LibrusUser(login="l", password="p", name="Ania", notify=notify, db_name="test.sqlite")


def s3_webhook_user() -> LibrusUser:
    return webhook_user(
        webhook_attachments_source="s3://bucket/prefix",
        s3_region="eu-central-1",
        s3_access_key_id="AKIA",
        s3_secret_access_key="shh",
    )


def add_message(notifier, attachments):
    return notifier.add_msg(
        MSG_URL,
        5,
        "Jan Kowalski (Nauczyciel)",
        DATE,
        "Wycieczka do muzeum",
        "<p>Zapraszamy</p>",
        "Zapraszamy",
        attachments,
    )


def attachment(link_id, name, data):
    return Attachment(link_id=link_id, msg_path=MSG_URL, name=name, data=data)


def sent_email(smtp_calls) -> email.message.Message:
    (body,) = [call[3] for call in smtp_calls if call[0] == "sendmail"]
    return email.message_from_string(body)


def decoded(part) -> str:
    return part.get_payload(decode=True).decode(part.get_content_charset())


def header(msg, name) -> str:
    return str(make_header(decode_header(msg[name])))


# --- email -----------------------------------------------------------------------------------


def test_email_message_in_full(config, smtp_calls):
    with LibrusNotifier(config, email_user()) as notifier:
        msg = add_message(
            notifier,
            [attachment("1002/77", "zgoda.pdf", b"%PDF-zgoda"), attachment("1002/78", "regulamin.docx", None)],
        )
        notifier.notify(msg)

    assert smtp_calls[:4] == [
        ("connect", "smtp.example.com", 2525),
        ("ehlo",),
        ("starttls",),
        ("login", "smtp-user@example.com", "secret"),
    ]
    assert smtp_calls[4][1:3] == ("smtp-user@example.com", ["a@example.com", "b@example.com"])
    assert smtp_calls[5] == ("close",)

    sent = sent_email(smtp_calls)
    assert header(sent, "Subject") == "[LIBRUS Ania] Wycieczka do muzeum"
    # The sender's display name is base64-encoded by hand, inside quotes.
    assert sent["From"] == '"=?utf-8?B?SmFuIEtvd2Fsc2tpIChOYXVjenljaWVsKSBAIExpYnJ1cw==?=" <smtp-user@example.com>'
    assert sent["To"] == "a@example.com, b@example.com"
    assert sent.get_content_type() == "multipart/alternative"

    html, plain, attached = sent.get_payload()
    assert html.get_content_type() == "text/html"
    assert decoded(html) == (
        "<p>Zapraszamy</p>"
        "<br/><h3>!!! To wiadomość z Librusa, nie odpowiadaj na nią mejlem!!!</h3>\n"
        "<br><br>\n"
        "<b> Data wysłania:</b> 2025-09-08 14:30:00 <br>\n"
        "<hr>\n"
        "<br><br>"
        "<br/><br/><p>Załączniki:<p><ul>"
        f"<li><a href='{LINK_78}'>regulamin.docx</a></li>"
        "</ul>"
    )
    assert plain.get_content_type() == "text/plain"
    assert decoded(plain) == (
        "Zapraszamy"
        "\n\n-------------------\n\n"
        "!! TO WIADOMOŚĆ Z LIBRUSA, NIE ODPOWIADAJ NA NIĄ MEJLEM !!!\n"
        "Data wysłania: 2025-09-08 14:30:00\n"
        "\n-------------------\n\n"
        f"\n\nZałączniki:\n{LINK_78}"
    )
    # Downloaded attachments are embedded; the one without data is only linked (above).
    assert attached.get_filename() == "zgoda.pdf"
    assert attached["Content-Disposition"] == 'attachment; filename="zgoda.pdf"'
    assert attached.get_payload(decode=True) == b"%PDF-zgoda"


def test_email_lists_several_linked_attachments(config, smtp_calls):
    with LibrusNotifier(config, email_user()) as notifier:
        msg = add_message(notifier, [attachment("1002/77", "a.pdf", None), attachment("1002/78", "b.pdf", None)])
        notifier.notify(msg)

    _, plain = sent_email(smtp_calls).get_payload()
    # Only the second link gets the " - " bullet: the separator is joined *between* links.
    assert decoded(plain).endswith(f"\n\nZałączniki:\n{LINK_77}\n - {LINK_78}")


def test_email_display_name_overrides_the_user_name(config, smtp_calls):
    with LibrusNotifier(config, email_user()) as notifier:
        notifier.notify(add_message(notifier, []), display_name="Ania, Jas")

    assert header(sent_email(smtp_calls), "Subject") == "[LIBRUS Ania, Jas] Wycieczka do muzeum"


def test_email_without_attachments_has_no_attachment_section(config, smtp_calls):
    with LibrusNotifier(config, email_user()) as notifier:
        notifier.notify(add_message(notifier, []))

    sent = sent_email(smtp_calls)
    html, plain = sent.get_payload()
    assert "Załączniki" not in decoded(html)
    assert decoded(plain).endswith("Data wysłania: 2025-09-08 14:30:00\n\n-------------------\n\n")


def test_announcement_email_uses_its_own_prefix_and_banner(config, smtp_calls):
    with LibrusNotifier(config, email_user()) as notifier:
        announcement = notifier.add_announcement("/ogloszenia#abc", "Dyrektor", "Zebranie", DATE, "<p>a</p>", "a")
        notifier.notify(announcement)

    sent = sent_email(smtp_calls)
    assert header(sent, "Subject") == "[LIBRUS Ania] Ogłoszenie: Zebranie"
    html, plain = sent.get_payload()
    assert "!!! To ogłoszenie z Librusa, nie odpowiadaj na nie mejlem!!!" in decoded(html)
    assert "!! TO OGŁOSZENIE Z LIBRUSA, NIE ODPOWIADAJ NA NIE MEJLEM !!!" in decoded(plain)


def test_email_from_an_unopened_notifier_skips_attachments(config, smtp_calls):
    """--test-notify sends through a notifier whose DB was never opened."""
    msg = Msg(url=MSG_URL, sender="Jan", subject="Test", date=DATE, contents_html="<p>t</p>", contents_text="t")

    LibrusNotifier(config, email_user()).notify(msg)

    html, plain = sent_email(smtp_calls).get_payload()
    assert decoded(plain).startswith("t\n\n-------------------")
    assert "Załączniki" not in decoded(html)


# --- webhook ---------------------------------------------------------------------------------


def test_webhook_payload_in_full_with_librus_links(config, posts):
    with LibrusNotifier(config, webhook_user()) as notifier:
        msg = add_message(
            notifier,
            [attachment("1002/77", "zgoda.pdf", None), attachment("1002/78", "regulamin.docx", None)],
        )
        notifier.notify(msg)

    (post,) = posts
    assert post["url"] == "https://hooks.example.com/parents"
    assert post["headers"] == {"Content-Type": "application/json"}
    assert post["kwargs"] == {}
    assert json.loads(post["data"]) == {
        "text": (
            "\n*LIBRUS Ania - 2025-09-08 14:30:00*\n"
            "*Od: Jan Kowalski (Nauczyciel)*\n"
            "*Temat: Wycieczka do muzeum*\n"
            "\nZapraszamy"
            "\n\nZałączniki:\n"
            f"- <{LINK_77}|zgoda.pdf>\n"
            f"- <{LINK_78}|regulamin.docx>\n"
        ),
    }


def test_webhook_display_name_and_announcement_label(config, posts):
    with LibrusNotifier(config, webhook_user()) as notifier:
        announcement = notifier.add_announcement("/ogloszenia#abc", "Dyrektor", "Zebranie", DATE, "<p>a</p>", "a")
        notifier.notify(announcement, display_name="Ania, Jas")

    assert json.loads(posts[0]["data"])["text"] == (
        "\n*LIBRUS Ania, Jas - 2025-09-08 14:30:00*\n*Od: Dyrektor*\n*Ogłoszenie: Zebranie*\n\na"
    )


def test_webhook_s3_upload_is_linked_and_reused_on_the_next_run(config, posts, s3):
    with LibrusNotifier(config, s3_webhook_user()) as notifier:
        notifier.notify(add_message(notifier, [attachment("1002/77", "zgoda.pdf", b"%PDF-zgoda")]))

    (upload,) = s3.uploads
    assert upload["Bucket"] == "bucket"
    assert upload["Key"].startswith("prefix/Ania/")
    assert upload["Key"].endswith("/1002_77_zgoda.pdf")
    assert upload["Body"] == b"%PDF-zgoda"
    assert upload["ContentType"] == "application/pdf"
    assert s3.client_kwargs["region_name"] == "eu-central-1"
    expected_link = f"https://s3.example.com/bucket/{upload['Key']}?expires=604800"
    assert json.loads(posts[0]["data"])["text"].endswith(f"\n\nZałączniki:\n- <{expected_link}|zgoda.pdf>\n")

    # A later run finds s3_key persisted in the DB and links the same object without re-uploading.
    with LibrusNotifier(config, s3_webhook_user()) as notifier:
        notifier.notify(notifier.get_msg(MSG_URL))

    assert len(s3.uploads) == 1
    assert json.loads(posts[1]["data"])["text"].endswith(f"- <{expected_link}|zgoda.pdf>\n")


def test_webhook_s3_failure_falls_back_to_the_librus_link(config, posts, s3):
    s3.fail_uploads = True

    with LibrusNotifier(config, s3_webhook_user()) as notifier:
        notifier.notify(add_message(notifier, [attachment("1002/77", "zgoda.pdf", b"%PDF-zgoda")]))

    assert json.loads(posts[0]["data"])["text"].endswith(f"- <{LINK_77}|zgoda.pdf>\n")


def test_webhook_s3_attachment_without_data_falls_back_to_the_librus_link(config, posts, s3):
    with LibrusNotifier(config, s3_webhook_user()) as notifier:
        notifier.notify(add_message(notifier, [attachment("1002/77", "zgoda.pdf", None)]))

    assert s3.uploads == []
    assert json.loads(posts[0]["data"])["text"].endswith(f"- <{LINK_77}|zgoda.pdf>\n")
