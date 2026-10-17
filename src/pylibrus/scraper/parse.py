"""Librus HTML -> domain values. No network, no ORM, no config.

These are the functions that break when Librus reshuffles its markup, so each takes a page (or
an already-parsed soup) and returns plain values that tests can compare directly. The shapes are
named versions of the tuples LibrusScraper has always returned.

A message is parsed in three parts - header, body, attachment rows - rather than all at once,
because LibrusScraper.fetch_msg() applies the age filter right after the header: a message too
old to send is never body-parsed and never has its attachments downloaded.
"""

import datetime
import logging
from itertools import chain

from bs4 import BeautifulSoup

from pylibrus.models import AttachmentRef, InboxEntry, MessageHeader, ParsedAnnouncement
from pylibrus.utils import retrieve_from

logger = logging.getLogger(__name__)


def parse_inbox(html: str) -> list[InboxEntry]:
    """(path, read) for every message row of a folder listing."""
    soup = BeautifulSoup(html, "html.parser")
    lines0 = soup.find_all("tr", {"class": "line0"})
    lines1 = soup.find_all("tr", {"class": "line1"})
    msgs = []
    for msg in chain(lines0, lines1):
        all_a_elems = msg.find_all("a")
        if not all_a_elems:
            continue
        link = all_a_elems[0]["href"].strip()
        read = True
        for td in msg.find_all("td"):
            if "bold" in td.get("style", ""):
                read = False
                break
        msgs.append(InboxEntry(link, read))
    msgs.reverse()
    return msgs


def _find_msg_header(soup, name):
    header = soup.find_all(string=name)
    return header[0].parent.parent.parent.find_all("td")[1].text.strip()


def parse_message_header(soup: BeautifulSoup) -> MessageHeader:
    sender = _find_msg_header(soup, "Nadawca")
    subject = _find_msg_header(soup, "Temat")
    date_string = _find_msg_header(soup, "Wysłano")
    date = datetime.datetime.strptime(date_string, "%Y-%m-%d %H:%M:%S")
    return MessageHeader(sender, subject, date)


def parse_message_body(soup: BeautifulSoup) -> tuple[str, str]:
    """(contents_html, contents_text) of a message page."""
    contents = soup.find_all(attrs={"class": "container-message-content"})[0]
    return str(contents), contents.text


def parse_attachment_rows(soup: BeautifulSoup) -> list[AttachmentRef]:
    """The attachments listed under a message's "Pliki:" row, without their content."""
    header = soup.find_all(string="Pliki:")
    if not header:
        return []

    attachments = []
    for attachment in header[0].parent.parent.parent.next_siblings:
        r"""Example of str(attachment):
        <tr>
        <td>
        <!-- icon -->
        <img src="/assets/img/filetype_icons/doc.png"/>
        <!-- name -->
                                KOPIOWANIE.docx                    </td>
        <td>
                                 
                                <!-- download button -->
        <a href="javascript:void(0);">
        <img class="" onclick='

                                otworz_w_nowym_oknie(
                                    "\/wiadomosci\/pobierz_zalacznik\/4921079\/3664030",
                                    "o2",
                                    420,
                                    250                        )

                                            ' src="/assets/img/homework_files_icons/download.png" title=""/>
        </a>
        </td>
        </tr>
        """
        name = retrieve_from(str(attachment), "<!-- name -->", "</td>")
        if not name:
            continue
        link_id = retrieve_from(str(attachment).replace("\\", ""), "/wiadomosci/pobierz_zalacznik/", '",')
        attachments.append(AttachmentRef(link_id=link_id, name=name))

    return attachments


def parse_announcements(html: str) -> list[ParsedAnnouncement]:
    """Every announcement table on /ogloszenia, newest first as Librus renders it.

    Unknown/missing fields are logged and skipped rather than raising: a cosmetic Librus markup
    change here must not break message forwarding, which is the primary feature. See
    ANNOUNCEMENTS_PLAN.md.
    """
    soup = BeautifulSoup(html, "html.parser")

    announcements = []
    for table in soup.find_all("table", class_="decorated"):
        thead = table.find("thead")
        if thead is None:
            continue
        title = thead.get_text().strip()

        fields = {}
        for row in table.find_all("tr"):
            th = row.find("th")
            td = row.find("td")
            if th is None or td is None:
                continue
            fields[th.get_text().strip()] = td

        date_td = fields.get("Data publikacji")
        if not title or date_td is None:
            logger.warning(f"Skipping announcement with missing title/date: title={title!r}")
            continue

        date_text = date_td.get_text().strip()
        try:
            date = datetime.datetime.strptime(date_text, "%Y-%m-%d")
        except ValueError:
            logger.warning(f"Skipping announcement '{title}' with unparseable date: {date_text!r}")
            continue

        unknown_labels = set(fields) - {"Dodał", "Data publikacji", "Treść"}
        if unknown_labels:
            logger.debug(f"Announcement '{title}' has unrecognized fields: {unknown_labels}")

        author_td = fields.get("Dodał")
        content_td = fields.get("Treść")
        author = author_td.get_text().strip() if author_td is not None else ""
        contents_html = str(content_td) if content_td is not None else ""
        contents_text = content_td.get_text().strip() if content_td is not None else ""

        announcements.append(ParsedAnnouncement(title, author, date, contents_html, contents_text))

    return announcements
