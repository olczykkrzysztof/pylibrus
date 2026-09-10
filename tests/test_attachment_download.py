"""Regression tests for `LibrusScraper.fetch_attachments`' download branch.

`get_attach_resp` used to be left unbound when neither download route matched, so the
`if get_attach_resp is not None` check raised `NameError` on the first attachment and
silently reused the previous attachment's response on later ones.

No network access happens here: `LibrusScraper._get` is monkeypatched to serve canned
responses keyed by URL.
"""

from pathlib import Path

from bs4 import BeautifulSoup

from pylibrus.pylibrus import FAILED_TO_DOWNLOAD_ATTACHMENT_DATA, LibrusScraper, PyLibrusConfig

FIXTURES_DIR = Path(__file__).parent / "fixtures"

ONLOAD_MARKER = "onload=\"window.location.href = window.location.href + '/get';"

FIRST_LINK_ID = "4921079/3664030"
SECOND_LINK_ID = "4921079/3664031"

EXPECTED_FAILURE_MARKER = f"Failed to download attachment: {FAILED_TO_DOWNLOAD_ATTACHMENT_DATA}".encode()


class FakeResponse:
    def __init__(self, url: str, text: str = "", content: bytes = b"", ok: bool = True, status_code: int = 200):
        self.url = url
        self.text = text
        self.content = content
        self.ok = ok
        self.status_code = status_code


def _soup_with_attachments() -> BeautifulSoup:
    html = (FIXTURES_DIR / "wiadomosc_z_zalacznikami.html").read_text(encoding="utf-8")
    return BeautifulSoup(html, "html.parser")


def _scraper_serving(tmp_path, pages: dict[str, FakeResponse]) -> LibrusScraper:
    config = PyLibrusConfig(workdir=str(tmp_path))
    scraper = LibrusScraper(login="test-user", passwd="test-pass", pylibrus_config=config)

    def fake_get(path, referer=None, **kwargs):
        assert path in pages, f"unexpected request: {path}"
        return pages[path]

    scraper._get = fake_get
    return scraper


def test_attachments_are_parsed_without_downloading_content(tmp_path):
    scraper = _scraper_serving(tmp_path, {})
    attachments = scraper.fetch_attachments("/wiadomosci/1/5/123", _soup_with_attachments(), fetch_content=False)

    assert [(a.name, a.link_id) for a in attachments] == [
        ("PIERWSZY.docx", FIRST_LINK_ID),
        ("DRUGI.pdf", SECOND_LINK_ID),
    ]
    assert all(a.data is None for a in attachments)


def test_unrecognized_download_page_records_failure_instead_of_raising(tmp_path):
    """Neither a singleUseKey nor an onload redirect: the attachment must come back carrying
    the failure marker as its data, not blow up with NameError."""
    first = LibrusScraper.get_attachment_download_link(FIRST_LINK_ID)
    second = LibrusScraper.get_attachment_download_link(SECOND_LINK_ID)
    scraper = _scraper_serving(
        tmp_path,
        {
            first: FakeResponse(url=first, text="<html>nothing we recognize</html>"),
            second: FakeResponse(url=second, text="<html>nothing we recognize</html>"),
        },
    )

    attachments = scraper.fetch_attachments("/wiadomosci/1/5/123", _soup_with_attachments(), fetch_content=True)

    assert [a.data for a in attachments] == [EXPECTED_FAILURE_MARKER, EXPECTED_FAILURE_MARKER]


def test_failure_after_a_success_still_records_the_failure(tmp_path):
    """The first attachment downloads fine, the second has no usable download route.

    Before the fix `get_attach_resp` leaked across iterations, so the second attachment read
    the first one's response. The wrong bytes never reached the user (the failure `reason`
    overwrote them further down), but nothing pinned that down - this does."""
    first = LibrusScraper.get_attachment_download_link(FIRST_LINK_ID)
    second = LibrusScraper.get_attachment_download_link(SECOND_LINK_ID)
    scraper = _scraper_serving(
        tmp_path,
        {
            first: FakeResponse(url=first, text=f'<html {ONLOAD_MARKER}"></html>'),
            first + "/get": FakeResponse(url=first + "/get", content=b"REAL-DOCX-BYTES"),
            second: FakeResponse(url=second, text="<html>nothing we recognize</html>"),
        },
    )

    attachments = scraper.fetch_attachments("/wiadomosci/1/5/123", _soup_with_attachments(), fetch_content=True)

    assert attachments[0].data == b"REAL-DOCX-BYTES"
    assert attachments[1].data == EXPECTED_FAILURE_MARKER


def test_http_error_on_download_is_recorded_as_failure(tmp_path):
    first = LibrusScraper.get_attachment_download_link(FIRST_LINK_ID)
    second = LibrusScraper.get_attachment_download_link(SECOND_LINK_ID)
    scraper = _scraper_serving(
        tmp_path,
        {
            first: FakeResponse(url=first, text=f'<html {ONLOAD_MARKER}"></html>'),
            first + "/get": FakeResponse(url=first + "/get", ok=False, status_code=500),
            second: FakeResponse(url=second, text="<html>nothing we recognize</html>"),
        },
    )

    attachments = scraper.fetch_attachments("/wiadomosci/1/5/123", _soup_with_attachments(), fetch_content=True)

    assert attachments[0].data == b"Failed to download attachment: http status code: 500"
