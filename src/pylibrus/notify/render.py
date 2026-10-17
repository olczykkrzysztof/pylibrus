"""Notifications as data: the email and the webhook payload for one item. Pure - no sending.

`msg_from_db` is a Msg or a LibrusAnnouncement: they share attribute names, and the per-type
wording comes from their subject_prefix/banner_text/banner_html/webhook_item_label.
"""

import base64
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from textwrap import dedent

from pylibrus.db.schema import Attachment
from pylibrus.scraper.urls import attachment_download_link
from pylibrus.settings import EmailNotify


def format_sender(sender_info, sender_email):
    sender_b64 = base64.b64encode(f"{sender_info} @ Librus".encode())
    sender_info_encoded = "=?utf-8?B?" + sender_b64.decode() + "?="
    return f'"{sender_info_encoded}" <{sender_email}>'


def build_email_message(
    msg_from_db, name: str, email_notify: EmailNotify, attachments: list[Attachment]
) -> MIMEMultipart:
    """The email for one Msg or LibrusAnnouncement, labelled with `name`.

    Attachments with downloaded data are embedded; those without are linked back to Librus.
    """
    msg = MIMEMultipart("alternative")
    msg.set_charset("utf-8")

    msg["Subject"] = f"[LIBRUS {name}] {msg_from_db.subject_prefix}{msg_from_db.subject}"
    msg["From"] = format_sender(msg_from_db.sender, email_notify.smtp_user)
    msg["To"] = ", ".join(email_notify.email_dest)

    attachments_only_with_link: list[Attachment] = []
    attachments_with_data: list[Attachment] = []
    for attach in attachments:
        if attach.data is None:
            attachments_only_with_link.append(attach)
        else:
            attachments_with_data.append(attach)
    attachments_as_text_msg = (
        ""
        if not attachments_only_with_link
        else "\n\nZałączniki:\n"
        + "\n - ".join(attachment_download_link(att.link_id) for att in attachments_only_with_link)
    )
    attachments_as_html_msg = (
        ""
        if not attachments_only_with_link
        else "<br/><br/><p>Załączniki:<p><ul>"
        + "".join(
            f"<li><a href='{attachment_download_link(att.link_id)}'>{att.name}</a></li>"
            for att in attachments_only_with_link
        )
        + "</ul>"
    )

    header_as_text_msg = f"""

-------------------

!! {msg_from_db.banner_text} !!!
Data wysłania: {msg_from_db.date}

-------------------

"""

    header_as_html_msg = f"""<br/><h3>!!! {msg_from_db.banner_html}!!!</h3>
<br><br>
<b> Data wysłania:</b> {msg_from_db.date} <br>
<hr>
<br><br>"""

    html_part = MIMEText(msg_from_db.contents_html + header_as_html_msg + attachments_as_html_msg, "html")
    text_part = MIMEText(msg_from_db.contents_text + header_as_text_msg + attachments_as_text_msg, "plain")
    msg.attach(html_part)
    msg.attach(text_part)
    for attach in attachments_with_data:
        part = MIMEApplication(attach.data, Name=attach.name)
        part["Content-Disposition"] = f'attachment; filename="{attach.name}"'
        msg.attach(part)
    return msg


def build_webhook_payload(msg_from_db, name: str, attachment_links: list[tuple[str, str]]) -> dict:
    """The Slack-style `{"text": ...}` payload for one Msg or LibrusAnnouncement, labelled with `name`.

    `attachment_links` are (file name, URL) pairs - Librus or pre-signed S3 links.
    """
    msg = (
        dedent(f"""
    *LIBRUS {name} - {msg_from_db.date}*
    *Od: {msg_from_db.sender}*
    *{msg_from_db.webhook_item_label}: {msg_from_db.subject}*
    """)
        + f"\n{msg_from_db.contents_text}"
    )
    if attachment_links:
        msg += "\n\nZałączniki:\n"
        for attachment_name, link in attachment_links:
            msg += f"- <{link}|{attachment_name}>\n"
    message = {
        "text": msg,
    }
    return message
