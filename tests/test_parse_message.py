"""Unit tests for the message-page parsers: header, body and attachment rows."""

import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from pylibrus.models import AttachmentRef, MessageHeader
from pylibrus.scraper.parse import parse_attachment_rows, parse_message_body, parse_message_header

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def soup_of(fixture: str) -> BeautifulSoup:
    return BeautifulSoup((FIXTURES_DIR / fixture).read_text(encoding="utf-8"), "html.parser")


def test_header():
    assert parse_message_header(soup_of("wiadomosc.html")) == MessageHeader(
        sender="Jan Kowalski (Nauczyciel)",
        subject="Wycieczka do muzeum",
        date=datetime.datetime(2025, 9, 8, 14, 30),
    )


def test_body_keeps_the_html_and_flattens_the_text():
    assert parse_message_body(soup_of("wiadomosc.html")) == (
        '<div class="container-message-content">Szanowni Państwo,<br/>zapraszamy na wycieczkę.</div>',
        "Szanowni Państwo,zapraszamy na wycieczkę.",
    )


def test_attachment_rows():
    assert parse_attachment_rows(soup_of("wiadomosc.html")) == [AttachmentRef(link_id="1002/77", name="zgoda.pdf")]
    assert parse_attachment_rows(soup_of("wiadomosc_z_zalacznikami.html")) == [
        AttachmentRef(link_id="4921079/3664030", name="PIERWSZY.docx"),
        AttachmentRef(link_id="4921079/3664031", name="DRUGI.pdf"),
    ]


def test_message_without_files_section_has_no_attachments():
    assert parse_attachment_rows(BeautifulSoup("<p>bez plików</p>", "html.parser")) == []
