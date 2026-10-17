import logging
import random
import time
from pathlib import Path

import requests
from user_agent import generate_user_agent

from pylibrus.scraper.cookies import load_cookies_into, load_cookies_per_login, store_cookies
from pylibrus.scraper.urls import api_url_from_path, msg_folder_path, synergia_url_from_path
from pylibrus.settings import PyLibrusConfig

logger = logging.getLogger(__name__)


class LibrusSession:
    """An HTTP session against Librus Synergia: cookies, browser-like headers and referers, and the
    OAuth login.

    Constructing one loads the login's cached cookies from the cookie file; `login()` checks them
    and only runs the OAuth sequence when Librus no longer accepts them.
    """

    def __init__(self, login: str, passwd: str, pylibrus_config: PyLibrusConfig):
        self._login = login
        self._passwd = passwd
        self._pylibrus_config = pylibrus_config
        self._session = requests.session()
        self._user_agent = generate_user_agent()
        self._last_url = synergia_url_from_path("/")
        self._cookie_path = Path(self._pylibrus_config.workdir) / self._pylibrus_config.cookie_file
        self.load_cookies_from_file()

    @property
    def cookies(self) -> requests.cookies.RequestsCookieJar:
        return self._session.cookies

    def get(self, path, referer=None, **kwargs):
        """GETs a synergia path or absolute URL; the referer defaults to the last page visited."""
        return self._get(path, referer, **kwargs)

    def post(self, path, referer=None, **kwargs):
        """POSTs to a synergia path or absolute URL; the referer defaults to the last page visited."""
        return self._post(path, referer, **kwargs)

    def load_cookies_per_login(self):
        return load_cookies_per_login(self._cookie_path)

    def load_cookies_from_file(self) -> None:
        load_cookies_into(self._session.cookies, self._cookie_path, self._login)

    def store_cookies_in_file(self):
        store_cookies(self._session.cookies, self._cookie_path, self._login)

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
        return self._session.post(api_url_from_path(path), **kwargs)

    def _api_get(self, path, referer, **kwargs):
        logger.debug(f"get {path}")
        self._set_headers(referer, kwargs)
        return self._session.get(api_url_from_path(path), **kwargs)

    def _request(self, method, path, referer=None, **kwargs):
        if referer is None:
            referer = self._last_url
        logger.debug(f"{method} {path} referrer={referer}")
        self._set_headers(referer, kwargs)
        url = synergia_url_from_path(path)
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
            msg_folder_path(self._pylibrus_config.inbox_folder_id),
            referer=synergia_url_from_path("/rodzic/index"),
        )
        return "Brak dostępu" not in resp.text

    @staticmethod
    def _gen_x_baner() -> str:
        # Mirrors the browser JS anti-bot: shift every char code by +20, join with "_"
        def shift(s: str) -> str:
            return "".join(chr(ord(c) + 20) for c in s)
        return shift(str(random.random())) + "_" + shift(str(int(time.time() * 1000)))

    def login(self):
        """Reuses the cached session if Librus still accepts it, otherwise logs in and caches it."""
        if self.are_cookies_valid():
            logger.debug(f"cookies valid for {self._login}")
            return
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
        oauth_auth_url = api_url_from_path(oauth_auth_frag)

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
