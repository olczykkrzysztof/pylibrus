"""Downloading attachment content: Librus serves files through sandbox.librus.pl, either behind a
single-use key that has to be polled until ready, or behind a plain redirect to `<page>/get`.
"""

import logging
import time

from pylibrus.scraper.urls import attachment_download_link
from pylibrus.utils import retrieve_from

FAILED_TO_DOWNLOAD_ATTACHMENT_DATA = "Failed to download attachment data!"

logger = logging.getLogger(__name__)


def download_attachment(session, link_id: str) -> bytes:
    """The attachment's content, or - when the download fails - a failure message in its place."""
    download_link = attachment_download_link(str(link_id))
    attachment_page = session.get(download_link)

    attach_data = None
    reason = ""
    # Not every branch below assigns it; without this, a failed download raises UnboundLocalError.
    get_attach_resp = None
    download_key = retrieve_from(attachment_page.text, 'singleUseKey = "', '"')
    if download_key:
        referer = attachment_page.url
        check_key_url = "https://sandbox.librus.pl/index.php?action=CSCheckKey"
        get_attach_url = f"https://sandbox.librus.pl/index.php?action=CSDownload&singleUseKey={download_key}"
        for _ in range(15):
            check_ready = session.post(
                check_key_url,
                headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                referer=referer,
                data=f"singleUseKey={download_key}",
            )

            if check_ready.json().get("status") == "ready":
                get_attach_resp = session.get(get_attach_url)
                break
            else:
                logger.info(f"Waiting for doc: {check_ready.json()}")
            time.sleep(1)
        else:
            reason = "waiting for CSCheckKey singleUseKey ready"
    elif "onload=\"window.location.href = window.location.href + '/get';" in attachment_page.text:
        get_attach_resp = session.get(attachment_page.url + "/get")
    else:
        reason = FAILED_TO_DOWNLOAD_ATTACHMENT_DATA

    if get_attach_resp is not None:
        if get_attach_resp.ok:
            attach_data = get_attach_resp.content
        else:
            reason = f"http status code: {get_attach_resp.status_code}"

    if reason:
        reason = f"Failed to download attachment: {reason}"
        logger.warning(reason)
        attach_data = reason.encode()

    return attach_data
