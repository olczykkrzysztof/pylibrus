"""The per-login cookie file (`pylibrus_cookies.json` by default) that lets a cron run reuse the
previous run's Librus session instead of logging in again."""

import json
import logging
from pathlib import Path

import requests
from requests.cookies import RequestsCookieJar

logger = logging.getLogger(__name__)


def load_cookies_per_login(cookie_path: Path) -> dict:
    if not cookie_path.exists():
        logger.debug(f"{cookie_path} does not exist")
        return {}
    try:
        return json.loads(cookie_path.read_text())
    except Exception as e:
        logger.info(f"Could not load {cookie_path}: {e}")
        return {}


def load_cookies_into(jar: RequestsCookieJar, cookie_path: Path, login: str) -> None:
    # Cookies are stored as list[{"name","value","domain","path"}] to preserve
    # per-domain state. The legacy flat {name: value} format is discarded —
    # cross-domain name collisions (SDZIENNIKSID, DZIENNIKSID exist on api and
    # synergia with different values) made it send the wrong session id.
    cookies_per_login = load_cookies_per_login(cookie_path)
    cookies = cookies_per_login.get(login)
    if not isinstance(cookies, list):
        if cookies:
            logger.debug(f"Ignoring legacy cookie format for {login}")
        return
    for entry in cookies:
        cookie = requests.cookies.create_cookie(
            name=entry["name"], value=entry["value"],
            domain=entry.get("domain", ""), path=entry.get("path", "/"),
        )
        jar.set_cookie(cookie)


def store_cookies(jar: RequestsCookieJar, cookie_path: Path, login: str) -> None:
    cookies_per_login = load_cookies_per_login(cookie_path)
    cookies_per_login[login] = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in jar
    ]
    cookie_path.write_text(json.dumps(cookies_per_login))
