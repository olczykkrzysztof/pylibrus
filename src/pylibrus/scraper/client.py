import datetime
import json
import logging
import random
import sys
import time
from http import client as http_client
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from user_agent import generate_user_agent

from pylibrus.db.schema import Attachment
from pylibrus.models import ParsedAnnouncement
from pylibrus.scraper.parse import (
    parse_announcements,
    parse_attachment_rows,
    parse_inbox,
    parse_message_body,
    parse_message_header,
)
from pylibrus.settings import PyLibrusConfig
from pylibrus.utils import retrieve_from

FAILED_TO_DOWNLOAD_ATTACHMENT_DATA = "Failed to download attachment data!"

logger = logging.getLogger(__name__)


class LibrusScraper:
    API_URL = "https://api.librus.pl"
    SYNERGIA_URL = "https://synergia.librus.pl"

    @classmethod
    def get_attachment_download_link(cls, link_id: str):
        return f"{cls.SYNERGIA_URL}/wiadomosci/pobierz_zalacznik/{link_id}"

    @classmethod
    def synergia_url_from_path(cls, path):
        if path.startswith("https://"):
            return path
        return cls.SYNERGIA_URL + path

    @classmethod
    def api_url_from_path(cls, path):
        return cls.API_URL + path

    @staticmethod
    def msg_folder_path(folder_id):
        return f"/wiadomosci/{folder_id}"

    def __init__(self, login: str, passwd: str, pylibrus_config: PyLibrusConfig):
        self._login = login
        self._passwd = passwd
        self._pylibrus_config = pylibrus_config
        self._session = requests.session()
        self._user_agent = generate_user_agent()
        self._last_folder_msg_path = None
        self._last_url = self.synergia_url_from_path("/")
        self._cookie_path = Path(self._pylibrus_config.workdir) / self._pylibrus_config.cookie_file

        logging.basicConfig(
            level=logging.INFO,  # Set the logging level to INFO
            format="%(asctime)s %(levelname)s %(message)s",  # Set the format for log messages
            handlers=[
                logging.StreamHandler(sys.stdout)  # Log to stdout
            ],
        )
        if pylibrus_config.debug:
            http_client.HTTPConnection.debuglevel = 1
            logging.getLogger().setLevel(logging.DEBUG)
            requests_log = logging.getLogger("requests.packages.urllib3")
            requests_log.setLevel(logging.DEBUG)
            requests_log.propagate = True

        self.load_cookies_from_file()

    def load_cookies_per_login(self):
        if not self._cookie_path.exists():
            logger.debug(f"{self._cookie_path} does not exist")
            return {}
        try:
            return json.loads(self._cookie_path.read_text())
        except Exception as e:
            logger.info(f"Could not load {self._cookie_path}: {e}")
            return {}

    def load_cookies_from_file(self) -> None:
        # Cookies are stored as list[{"name","value","domain","path"}] to preserve
        # per-domain state. The legacy flat {name: value} format is discarded —
        # cross-domain name collisions (SDZIENNIKSID, DZIENNIKSID exist on api and
        # synergia with different values) made it send the wrong session id.
        cookies_per_login = self.load_cookies_per_login()
        cookies = cookies_per_login.get(self._login)
        if not isinstance(cookies, list):
            if cookies:
                logger.debug(f"Ignoring legacy cookie format for {self._login}")
            return
        for entry in cookies:
            cookie = requests.cookies.create_cookie(
                name=entry["name"], value=entry["value"],
                domain=entry.get("domain", ""), path=entry.get("path", "/"),
            )
            self._session.cookies.set_cookie(cookie)

    def store_cookies_in_file(self):
        cookies_per_login = self.load_cookies_per_login()
        cookies_per_login[self._login] = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in self._session.cookies
        ]
        self._cookie_path.write_text(json.dumps(cookies_per_login))

    def _set_headers(self, referer, kwargs):
        if "headers" not in kwargs:
            kwargs["headers"] = {}
        kwargs["headers"].update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pl",
                "User-Agent": self._user_agent,
                "Referer": referer,
            },
        )
        return kwargs

    def _api_post(self, path, referer, **kwargs):
        logger.debug(f"post {path}")
        self._set_headers(referer, kwargs)
        return self._session.post(self.api_url_from_path(path), **kwargs)

    def _api_get(self, path, referer, **kwargs):
        logger.debug(f"get {path}")
        self._set_headers(referer, kwargs)
        return self._session.get(self.api_url_from_path(path), **kwargs)

    def _request(self, method, path, referer=None, **kwargs):
        if referer is None:
            referer = self._last_url
        logger.debug(f"{method} {path} referrer={referer}")
        self._set_headers(referer, kwargs)
        url = self.synergia_url_from_path(path)
        logger.debug(f"Making request: {method} {url} with cookies: {self._session.cookies.get_dict()}")
        if method == "get":
            resp = self._session.get(url, **kwargs)
        elif method == "post":
            resp = self._session.post(url, **kwargs)
        else:
            raise ValueError(f"Unsupported method: {method}")
        self._last_url = resp.url
        return resp

    def _post(self, path, referer=None, **kwargs):
        return self._request("post", path, referer, **kwargs)

    def _get(self, path, referer=None, **kwargs):
        return self._request("get", path, referer, **kwargs)

    def clear_cookies(self):
        self._session.cookies.clear()

    def are_cookies_valid(self):
        has_oauth_token = any(
            c.name == "oauth_token" and c.domain == "synergia.librus.pl"
            for c in self._session.cookies
        )
        if not has_oauth_token:
            return False
        resp = self._get(
            self.msg_folder_path(self._pylibrus_config.inbox_folder_id),
            referer=self.synergia_url_from_path("/rodzic/index"),
        )
        return "Brak dostępu" not in resp.text

    @staticmethod
    def _gen_x_baner() -> str:
        # Mirrors the browser JS anti-bot: shift every char code by +20, join with "_"
        def shift(s: str) -> str:
            return "".join(chr(ord(c) + 20) for c in s)
        return shift(str(random.random())) + "_" + shift(str(int(time.time() * 1000)))

    def __enter__(self):
        if self.are_cookies_valid():
            logger.debug(f"cookies valid for {self._login}")
            return self
        logger.debug(f"cookies are not valid from {self._login}, login")
        self.clear_cookies()

        # The OAuth flow must begin at synergia's /loguj/portalRodzina so that
        # synergia generates a `state` and later activates the session by setting
        # the oauth_token cookie. Starting directly at api.librus.pl/OAuth/...
        # completes login but leaves synergia with no oauth_token, so
        # /rodzic/index and /wiadomosci/... return "Brak dostępu".
        portal_ref = "https://portal.librus.pl/"
        init_resp = self._get(
            f"/loguj/portalRodzina?v={int(time.time() * 1000)}",
            referer=portal_ref, allow_redirects=False,
        )
        state_redirect = init_resp.headers.get("Location", "")
        if "state=" not in state_redirect:
            raise RuntimeError(f"Expected state in Location from /loguj/portalRodzina, got: {state_redirect}")
        self._get(state_redirect, referer=portal_ref)

        oauth_auth_frag = "/OAuth/Authorization?client_id=46"
        oauth_auth_url = self.api_url_from_path(oauth_auth_frag)

        self._api_get(oauth_auth_frag, referer=oauth_auth_url)
        self._api_post(
            oauth_auth_frag,
            referer=oauth_auth_url,
            data={"action": "login", "login": self._login, "pass": self._passwd},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://api.librus.pl",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "x-baner": self._gen_x_baner(),
            },
        )

        for frag in ("/OAuth/Authorization/2FA?client_id=46",
                     "/OAuth/Authorization/PerformLogin?client_id=46"):
            self._api_get(frag, referer=oauth_auth_url, allow_redirects=False)
        grant_resp = self._api_get("/OAuth/Authorization/Grant?client_id=46",
                                   referer=oauth_auth_url, allow_redirects=False)
        final_url = grant_resp.headers.get("Location", "")
        if not final_url:
            raise RuntimeError("Missing Location from /OAuth/Authorization/Grant")
        self._get(final_url, referer=oauth_auth_url)

        syn_cookies = {c.name for c in self._session.cookies if c.domain == "synergia.librus.pl"}
        if "oauth_token" not in syn_cookies:
            raise RuntimeError(f"Login failed: no oauth_token cookie set on synergia. Cookies: {syn_cookies}")

        self.store_cookies_in_file()
        return self

    def __exit__(self, exc_type=None, exc_val=None, exc_tb=None):
        pass

    "#body > div.container.static > div > table > tbody > tr:nth-child(1) > td"

    def fetch_attachments(self, msg_path, soup, fetch_content):
        attachments = [
            Attachment(link_id=ref.link_id, msg_path=msg_path, name=ref.name, data=None)
            for ref in parse_attachment_rows(soup)
        ]

        if not fetch_content:
            return attachments

        for attachment in attachments:
            logger.info(f"Download attachment {attachment.name}")
            download_link = LibrusScraper.get_attachment_download_link(str(attachment.link_id))
            attachment_page = self._get(download_link)

            attach_data = None
            reason = ""
            # Must be reset per attachment: not every branch below assigns it, and a value
            # left over from the previous iteration would be read as this attachment's data.
            get_attach_resp = None
            download_key = retrieve_from(attachment_page.text, 'singleUseKey = "', '"')
            if download_key:
                referer = attachment_page.url
                check_key_url = "https://sandbox.librus.pl/index.php?action=CSCheckKey"
                get_attach_url = f"https://sandbox.librus.pl/index.php?action=CSDownload&singleUseKey={download_key}"
                for _ in range(15):
                    check_ready = self._post(
                        check_key_url,
                        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                        referer=referer,
                        data=f"singleUseKey={download_key}",
                    )

                    if check_ready.json().get("status") == "ready":
                        get_attach_resp = self._get(get_attach_url)
                        break
                    else:
                        logger.info(f"Waiting for doc: {check_ready.json()}")
                    time.sleep(1)
                else:
                    reason = "waiting for CSCheckKey singleUseKey ready"
            elif "onload=\"window.location.href = window.location.href + '/get';" in attachment_page.text:
                get_attach_resp = self._get(attachment_page.url + "/get")
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

            attachment.data = attach_data
            logger.info(f"{attachment.name=} {attachment.link_id=} {len(attach_data)=}")

        return attachments

    def fetch_msg(self, msg_path, fetch_attchement_content: bool):
        msg_page = self._get(msg_path, referer=self.synergia_url_from_path(self._last_folder_msg_path)).text
        soup = BeautifulSoup(msg_page, "html.parser")
        sender, subject, date = parse_message_header(soup)
        if datetime.datetime.now() - date > datetime.timedelta(days=self._pylibrus_config.max_age_of_sending_msg_days):
            logger.info(f"Do not send '{subject}' (message too old, {date})")
            return None
        contents_html, contents_text = parse_message_body(soup)

        attachments = self.fetch_attachments(msg_path, soup, fetch_attchement_content)
        return sender, subject, date, contents_html, contents_text, attachments

    def msgs_from_folder(self, folder_id):
        self._last_folder_msg_path = self.msg_folder_path(folder_id)
        ret = self._get(self._last_folder_msg_path, referer=self.synergia_url_from_path("/rodzic/index"))
        return parse_inbox(ret.text)

    def fetch_announcements(self) -> list[ParsedAnnouncement]:
        """Scrapes /ogloszenia and returns (title, author, date, contents_html, contents_text) tuples.

        Announcements have no pagination, no per-item URL and no read/unread flag - the
        whole page lists every currently valid announcement on every call, newest first
        as Librus renders it. The caller is responsible for deduplication (there is no
        stable id to key on - see `announcement_id()`). See ANNOUNCEMENTS_PLAN.md for the
        full rationale; the parsing itself is `parse_announcements()`.
        """
        resp = self._get(
            self._pylibrus_config.announcements_path,
            referer=self.synergia_url_from_path("/rodzic/index"),
        )
        if "Brak dostępu" in resp.text:
            raise RuntimeError("Brak dostępu when fetching announcements - cookies likely invalid")
        return parse_announcements(resp.text)
