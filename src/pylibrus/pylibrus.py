import abc
import argparse
import base64
import configparser
import dataclasses
import datetime
import hashlib
import json
import logging
import mimetypes
import os
import random
import re
import smtplib
import sys
import time
from configparser import ConfigParser
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from http import client as http_client
from itertools import chain
from pathlib import Path
from textwrap import dedent
from urllib.parse import quote

import boto3
import requests
from bs4 import BeautifulSoup
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, LargeBinary, String, inspect, text
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from user_agent import generate_user_agent

Base = declarative_base()

FAILED_TO_DOWNLOAD_ATTACHMENT_DATA = "Failed to download attachment data!"
TRUE_VALUES = ("yes", "on", "true", "1")
FALSE_VALUES = ("no", "off", "false", "0")
LINK_EXPIRE_DURATION = 604800  # 7 days is maximum possible for S3 pre-signed URLs
DEFAULT_WEBHOOK_ATTACHMENTS_SOURCE = "librus_link"

logger = logging.getLogger(__name__)


def str_to_bool(s: str):
    if s is None:
        return None
    s = s.lower()
    if s in TRUE_VALUES:
        return True
    elif s in FALSE_VALUES:
        return False
    else:
        raise ValueError(f"Invalid boolean value: {s}. Should be one of: {list(TRUE_VALUES) + list(FALSE_VALUES)} ")


def str_to_int(s: str):
    if s is None:
        return None
    return int(s)


@dataclasses.dataclass(slots=True)
class PyLibrusConfig:
    workdir: str
    send_message: str = "unread"
    fetch_attachments: bool = True
    max_age_of_sending_msg_days: int = 4
    fetch_announcements: bool = True
    max_age_of_sending_announcement_days: int | None = None
    debug: bool = False
    sleep_between_librus_users: int = 10
    inbox_folder_id: int = dataclasses.field(default=5, init=False)  # Odebrane
    announcements_path: str = dataclasses.field(default="/ogloszenia", init=False)
    cookie_file: str = "pylibrus_cookies.json"

    def __post_init__(self):
        for field in dataclasses.fields(self):
            if not isinstance(field.default, dataclasses._MISSING_TYPE) and getattr(self, field.name) is None:
                setattr(self, field.name, field.default)
        if self.send_message not in ("unread", "unsent"):
            raise ValueError("SEND_MESSAGE should be 'unread' or 'unsent'")
        if self.max_age_of_sending_announcement_days is None:
            self.max_age_of_sending_announcement_days = self.max_age_of_sending_msg_days

    @classmethod
    def from_config(cls, workdir: str, config: ConfigParser) -> "PyLibrusConfig":
        global_config = config["global"]
        return cls(
            send_message=global_config.get(PyLibrusConfig.send_message.__name__, None),
            fetch_attachments=global_config.getboolean(PyLibrusConfig.fetch_attachments.__name__, None),
            max_age_of_sending_msg_days=global_config.getint(PyLibrusConfig.max_age_of_sending_msg_days.__name__, None),
            fetch_announcements=global_config.getboolean(PyLibrusConfig.fetch_announcements.__name__, None),
            max_age_of_sending_announcement_days=global_config.getint(
                PyLibrusConfig.max_age_of_sending_announcement_days.__name__, None,
            ),
            debug=global_config.getboolean(PyLibrusConfig.debug.__name__, None),
            sleep_between_librus_users=global_config.getint(PyLibrusConfig.sleep_between_librus_users.__name__, None),
            cookie_file=global_config.get(PyLibrusConfig.cookie_file.__name__, None),
            workdir=workdir,
        )

    @classmethod
    def from_env(cls, workdir: str) -> "PyLibrusConfig":
        return cls(
            send_message=os.environ.get("SEND_MESSAGE"),
            fetch_attachments=str_to_bool(os.environ.get("FETCH_ATTACHMENTS")),
            max_age_of_sending_msg_days=str_to_int(os.environ.get("MAX_AGE_OF_SENDING_MSG_DAYS")),
            fetch_announcements=str_to_bool(os.environ.get("FETCH_ANNOUNCEMENTS")),
            max_age_of_sending_announcement_days=str_to_int(os.environ.get("MAX_AGE_OF_SENDING_ANNOUNCEMENT_DAYS")),
            debug=str_to_bool(os.environ.get("LIBRUS_DEBUG")),
            workdir=workdir,
        )


def validate_fields(instance):
    for field in dataclasses.fields(instance):
        value = getattr(instance, field.name)
        if value is None or value == "":
            raise ValueError(f"The field '{field.name}' cannot be None.")


class Notify(abc.ABC):
    @staticmethod
    def is_email() -> bool:
        return False

    @staticmethod
    def is_webhook() -> bool:
        return False


@dataclasses.dataclass(slots=True)
class EmailNotify(Notify):
    smtp_user: str
    smtp_pass: str = dataclasses.field(repr=False)
    smtp_server: str
    email_dest: list[str] | str
    smtp_port: int = 587

    @staticmethod
    def is_email() -> bool:
        return True

    def __post_init__(self):
        if isinstance(self.email_dest, str):
            self.email_dest = [email.strip() for email in self.email_dest.split(",")]
        for field in dataclasses.fields(self):
            if not isinstance(field.default, dataclasses._MISSING_TYPE) and getattr(self, field.name) is None:
                setattr(self, field.name, field.default)
        validate_fields(self)

    @classmethod
    def from_env(cls) -> "EmailNotify":
        return cls(
            smtp_user=os.environ.get("SMTP_USER", "Default user"),
            smtp_pass=os.environ.get("SMTP_PASS"),
            smtp_server=os.environ.get("SMTP_SERVER"),
            smtp_port=int(os.environ.get("SMTP_PORT")),
            email_dest=os.environ.get("EMAIL_DEST"),
        )

    @classmethod
    def from_config(cls, config, section) -> "EmailNotify":
        return cls(
            smtp_user=config[section]["smtp_user"],
            smtp_pass=config[section]["smtp_pass"],
            smtp_server=config[section]["smtp_server"],
            smtp_port=int(config[section]["smtp_port"]),
            email_dest=config[section]["email_dest"],
        )


@dataclasses.dataclass(slots=True)
class WebhookNotify(Notify):
    webhook: str
    webhook_attachments_source: str = DEFAULT_WEBHOOK_ATTACHMENTS_SOURCE
    s3_region: str | None = None
    s3_access_key_id: str | None = dataclasses.field(default=None, repr=False)
    s3_secret_access_key: str | None = dataclasses.field(default=None, repr=False)
    s3_session_token: str | None = dataclasses.field(default=None, repr=False)
    s3_endpoint_url: str | None = None

    @staticmethod
    def is_webhook() -> bool:
        return True

    def __post_init__(self):
        if not self.webhook:
            raise ValueError("The field 'webhook' cannot be None.")
        if self.webhook_attachments_source != DEFAULT_WEBHOOK_ATTACHMENTS_SOURCE and not self.webhook_attachments_source.startswith("s3://"):
            raise ValueError("webhook_attachments_source should be 'librus_link' or start with 's3://'")
        if self.webhook_attachments_source.startswith("s3://"):
            bucket_name, _ = parse_s3_source(self.webhook_attachments_source)
            if not bucket_name:
                raise ValueError("Invalid webhook_attachments_source. Expected: s3://<bucket>/<optional-prefix>")
            if not self.s3_region:
                raise ValueError("s3_region is required when webhook_attachments_source uses s3://")
            if not self.s3_access_key_id:
                raise ValueError("s3_access_key_id is required when webhook_attachments_source uses s3://")
            if not self.s3_secret_access_key:
                raise ValueError("s3_secret_access_key is required when webhook_attachments_source uses s3://")

    @classmethod
    def from_env(cls) -> "WebhookNotify":
        return cls(
            webhook=os.environ.get("WEBHOOK"),
            webhook_attachments_source=os.environ.get("WEBHOOK_ATTACHMENTS_SOURCE") or DEFAULT_WEBHOOK_ATTACHMENTS_SOURCE,
            s3_region=os.environ.get("S3_REGION"),
            s3_access_key_id=os.environ.get("S3_ACCESS_KEY_ID"),
            s3_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY"),
            s3_session_token=os.environ.get("S3_SESSION_TOKEN"),
            s3_endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
        )

    @classmethod
    def from_config(cls, config, section):
        return cls(
            webhook=config[section]["webhook"],
            webhook_attachments_source=config[section].get(
                "webhook_attachments_source", DEFAULT_WEBHOOK_ATTACHMENTS_SOURCE,
            ),
            s3_region=config[section].get("s3_region"),
            s3_access_key_id=config[section].get("s3_access_key_id"),
            s3_secret_access_key=config[section].get("s3_secret_access_key"),
            s3_session_token=config[section].get("s3_session_token"),
            s3_endpoint_url=config[section].get("s3_endpoint_url"),
        )


@dataclasses.dataclass(slots=True)
class LibrusUser:
    login: str
    password: str = dataclasses.field(repr=False)
    name: str
    notify: EmailNotify | WebhookNotify
    db_name: str
    fetch_announcements: bool | None = None  # None means: fall back to the global setting

    @classmethod
    def from_config(cls, config, section) -> "LibrusUser":
        name = section.split(":", 1)[1]
        librus_user = config[section].get("librus_user")
        librus_pass = config[section].get("librus_pass")
        # Determine whether the user uses email or webhook notification
        if "email_dest" in config[section]:
            notify = EmailNotify.from_config(config, section)
        elif "webhook" in config[section]:
            notify = WebhookNotify.from_config(config, section)
        else:
            raise ValueError(f"No valid notification method for {section}")
        db_name = config[section].get("db_name")
        if not db_name:
            db_name = config["global"].get("db_name", "pylibrus.sqlite")
        fetch_announcements = config[section].getboolean("fetch_announcements", fallback=None)
        return cls(
            name=name,
            login=librus_user,
            password=librus_pass,
            notify=notify,
            db_name=db_name,
            fetch_announcements=fetch_announcements,
        )

    @classmethod
    def from_env(cls) -> "LibrusUser":
        return cls(
            login=os.environ.get("LIBRUS_USER"),
            password=os.environ.get("LIBRUS_PASS"),
            name=os.environ.get("LIBRUS_NAME"),
            notify=WebhookNotify.from_env() if str_to_bool(os.environ.get("WEBHOOK")) else EmailNotify.from_env(),
            db_name=os.environ.get("DB_NAME"),
            fetch_announcements=str_to_bool(os.environ.get("FETCH_ANNOUNCEMENTS")),
        )

    @classmethod
    def load_librus_users_from_config(cls, config: ConfigParser) -> list["LibrusUser"]:
        users = []
        for section in config.sections():
            if section.startswith("user:"):
                user = cls.from_config(config, section)
                users.append(user)
        return users


class Msg(Base):
    __tablename__ = "messages"

    url = Column(String, primary_key=True)
    folder = Column(Integer)
    sender = Column(String)
    subject = Column(String)
    date = Column(DateTime)
    contents_html = Column(String)
    contents_text = Column(String)
    email_sent = Column(Boolean, default=False)

    # No subject prefix/banner override for regular messages; see LibrusAnnouncement.
    subject_prefix = ""
    banner_text = "TO WIADOMOŚĆ Z LIBRUSA, NIE ODPOWIADAJ NA NIĄ MEJLEM"
    banner_html = "To wiadomość z Librusa, nie odpowiadaj na nią mejlem"
    webhook_item_label = "Temat"


class Attachment(Base):
    __tablename__ = "attachments"

    link_id = Column(String, primary_key=True)  # link_id seems to contain message id and attachment id
    msg_path = Column(String, ForeignKey(Msg.url))
    name = Column(String)
    data = Column(LargeBinary)
    s3_key = Column(String, nullable=True)
    s3_upload_date = Column(DateTime, nullable=True)
    s3_etag = Column(String, nullable=True)


class LibrusAnnouncement(Base):
    """A Librus announcement (AKA 'ogłoszenie'), scraped from /ogloszenia.

    Announcements have no stable id and no per-item read/unread flag (see
    ANNOUNCEMENTS_PLAN.md), so `url` is a synthetic key derived from title+author+date
    (see `announcement_id()`) rather than a real Librus URL, and `email_sent` is the
    only dedupe signal - `send_message=unread` semantics do not apply here.

    Column names deliberately match `Msg` so `LibrusNotifier.notify()` and friends can
    treat both the same way; `contents_html`/`contents_text` hold the announcement body.
    """

    __tablename__ = "announcements"

    url = Column(String, primary_key=True)  # synthetic, see announcement_id()
    sender = Column(String)  # "Dodał"
    subject = Column(String)  # title
    date = Column(DateTime)  # "Data publikacji" (date only, no time)
    contents_html = Column(String)
    contents_text = Column(String)
    content_hash = Column(String, nullable=True)  # sha1 of contents_text; unused today, for future edit-detection
    email_sent = Column(Boolean, default=False)

    subject_prefix = "Ogłoszenie: "
    banner_text = "TO OGŁOSZENIE Z LIBRUSA, NIE ODPOWIADAJ NA NIE MEJLEM"
    banner_html = "To ogłoszenie z Librusa, nie odpowiadaj na nie mejlem"
    webhook_item_label = "Ogłoszenie"


def announcement_id(title: str, author: str, date: datetime.datetime) -> str:
    # Deliberately excludes the body: an announcement edited in place (title/author/date
    # unchanged) keeps the same id, so it is not re-sent as "new". See ANNOUNCEMENTS_PLAN.md.
    digest = hashlib.sha1(f"{title}|{author}|{date:%Y-%m-%d}".encode()).hexdigest()[:16]
    return f"/ogloszenia#{digest}"


def retrieve_from(txt, start, end):
    pos = txt.find(start)
    if pos == -1:
        return ""
    idx_start = pos + len(start)
    pos = txt.find(end, idx_start)
    if pos == -1:
        return ""
    return txt[idx_start:pos].strip()


def parse_s3_source(webhook_attachments_source: str) -> tuple[str | None, str]:
    if not webhook_attachments_source or not webhook_attachments_source.startswith("s3://"):
        return None, ""
    source_without_scheme = webhook_attachments_source[len("s3://"):]
    if not source_without_scheme:
        return None, ""
    bucket_name, _, raw_prefix = source_without_scheme.partition("/")
    if not bucket_name:
        return None, ""
    return bucket_name, raw_prefix.strip("/")


def is_s3_webhook_source(webhook_attachments_source: str) -> bool:
    return bool(webhook_attachments_source and webhook_attachments_source.startswith("s3://"))


def sanitize_s3_segment(segment: str, fallback: str = "item") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (segment or "").strip())
    return cleaned or fallback


def build_download_content_disposition(file_name: str) -> str:
    safe_ascii_name = file_name.encode("ascii", "ignore").decode() or "attachment"
    encoded_file_name = quote(file_name, safe="")
    return f'attachment; filename="{safe_ascii_name}"; filename*=UTF-8\'\'{encoded_file_name}'


class S3AttachmentStorage:
    def __init__(self, webhook_notify: WebhookNotify):
        bucket_name, key_prefix = parse_s3_source(webhook_notify.webhook_attachments_source)
        if not bucket_name:
            raise ValueError("Missing bucket name in webhook_attachments_source")
        self._bucket_name = bucket_name
        self._key_prefix = key_prefix
        self._client = boto3.client(
            "s3",
            region_name=webhook_notify.s3_region,
            aws_access_key_id=webhook_notify.s3_access_key_id,
            aws_secret_access_key=webhook_notify.s3_secret_access_key,
            aws_session_token=webhook_notify.s3_session_token or None,
            endpoint_url=webhook_notify.s3_endpoint_url or None,
        )

    @staticmethod
    def _hash_msg_path(msg_path: str) -> str:
        return hashlib.sha1(msg_path.encode("utf-8")).hexdigest()[:12]

    def build_object_key(self, librus_user_name: str, msg_path: str, attachment: Attachment) -> str:
        user_segment = sanitize_s3_segment(librus_user_name, fallback="user")
        msg_segment = self._hash_msg_path(msg_path)
        attachment_segment = sanitize_s3_segment(attachment.link_id, fallback="attachment")
        name_segment = sanitize_s3_segment(attachment.name, fallback="file")
        parts = [p for p in (self._key_prefix, user_segment, msg_segment, f"{attachment_segment}_{name_segment}") if p]
        return "/".join(parts)

    def upload_attachment(self, librus_user_name: str, msg_path: str, attachment: Attachment) -> tuple[str, str]:
        object_key = attachment.s3_key or self.build_object_key(librus_user_name, msg_path, attachment)
        content_type = mimetypes.guess_type(attachment.name)[0] or "application/octet-stream"
        content_disposition = build_download_content_disposition(attachment.name)
        response = self._client.put_object(
            Bucket=self._bucket_name,
            Key=object_key,
            Body=attachment.data,
            ContentType=content_type,
            ContentDisposition=content_disposition,
        )
        return object_key, str(response.get("ETag", "")).strip('"')

    def generate_download_link(self, attachment: Attachment) -> str:
        if not attachment.s3_key:
            raise ValueError("Missing s3_key for pre-signed URL generation")
        return self._client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self._bucket_name,
                "Key": attachment.s3_key,
                "ResponseContentDisposition": build_download_content_disposition(attachment.name),
            },
            ExpiresIn=LINK_EXPIRE_DURATION,
        )


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
        if not self._cookie_path.exists:
            logger.debug(f"{self._cookie_path} does not exist")
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

    @staticmethod
    def _find_msg_header(soup, name):
        header = soup.find_all(string=name)
        return header[0].parent.parent.parent.find_all("td")[1].text.strip()

    def fetch_attachments(self, msg_path, soup, fetch_content):
        header = soup.find_all(string="Pliki:")
        if not header:
            return []

        def get_attachments_without_data() -> list[Attachment]:
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
                attachments.append(Attachment(link_id=link_id, msg_path=msg_path, name=name, data=None))

            return attachments

        attachments = get_attachments_without_data()

        if not fetch_content:
            return attachments

        for attachment in attachments:
            logger.info(f"Download attachment {attachment.name}")
            download_link = LibrusScraper.get_attachment_download_link(str(attachment.link_id))
            attachment_page = self._get(download_link)

            attach_data = None
            reason = ""
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
        sender = self._find_msg_header(soup, "Nadawca")
        subject = self._find_msg_header(soup, "Temat")
        date_string = self._find_msg_header(soup, "Wysłano")
        date = datetime.datetime.strptime(date_string, "%Y-%m-%d %H:%M:%S")
        if datetime.datetime.now() - date > datetime.timedelta(days=self._pylibrus_config.max_age_of_sending_msg_days):
            logger.info(f"Do not send '{subject}' (message too old, {date})")
            return None
        contents = soup.find_all(attrs={"class": "container-message-content"})[0]

        attachments = self.fetch_attachments(msg_path, soup, fetch_attchement_content)
        return sender, subject, date, str(contents), contents.text, attachments

    def msgs_from_folder(self, folder_id):
        self._last_folder_msg_path = self.msg_folder_path(folder_id)
        ret = self._get(self._last_folder_msg_path, referer=self.synergia_url_from_path("/rodzic/index"))
        inbox_html = ret.text
        soup = BeautifulSoup(inbox_html, "html.parser")
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
            msgs.append((link, read))
        msgs.reverse()
        return msgs

    def fetch_announcements(self) -> list[tuple[str, str, datetime.datetime, str, str]]:
        """Scrapes /ogloszenia and returns (title, author, date, contents_html, contents_text) tuples.

        Announcements have no pagination, no per-item URL and no read/unread flag - the
        whole page lists every currently valid announcement on every call, newest first
        as Librus renders it. The caller is responsible for deduplication (there is no
        stable id to key on - see `announcement_id()`). See ANNOUNCEMENTS_PLAN.md for the
        full rationale, including why unknown/missing fields are logged and skipped
        rather than raising: a cosmetic Librus markup change here must not break message
        forwarding, which is the primary feature.
        """
        resp = self._get(
            self._pylibrus_config.announcements_path,
            referer=self.synergia_url_from_path("/rodzic/index"),
        )
        if "Brak dostępu" in resp.text:
            raise RuntimeError("Brak dostępu when fetching announcements - cookies likely invalid")
        soup = BeautifulSoup(resp.text, "html.parser")

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

            announcements.append((title, author, date, contents_html, contents_text))

        return announcements


class LibrusNotifier:
    def __init__(self, pylibrus_config: PyLibrusConfig, librus_user: LibrusUser):
        self._pylibrus_config = pylibrus_config
        self._librus_user = librus_user
        self._engine = None
        self._session = None

    def _create_db(self):
        workdir_path = Path(self._pylibrus_config.workdir)
        if not workdir_path.exists:
            raise RuntimeError(f"Workdir {workdir_path} does not exist")
        self._engine = create_engine(f"sqlite:///{workdir_path / self._librus_user.db_name}")
        Base.metadata.create_all(self._engine)
        self._migrate_tables()
        session_maker = sessionmaker(bind=self._engine)
        self._session = session_maker()

    def _migrate_tables(self):
        # Hand-rolled additive migration (no Alembic here, see CLAUDE.md): new nullable
        # columns on any table need an explicit ALTER TABLE below, or existing users'
        # DBs won't pick them up. `announcements` is created fresh by create_all() above
        # for everyone, so it needs no migration today - this is the home for its future
        # column additions.
        inspector = inspect(self._engine)
        migrations = []
        if inspector.has_table("attachments"):
            existing = {c["name"] for c in inspector.get_columns("attachments")}
            if "s3_key" not in existing:
                migrations.append("ALTER TABLE attachments ADD COLUMN s3_key TEXT")
            if "s3_upload_date" not in existing:
                migrations.append("ALTER TABLE attachments ADD COLUMN s3_upload_date DATETIME")
            if "s3_etag" not in existing:
                migrations.append("ALTER TABLE attachments ADD COLUMN s3_etag TEXT")
        if not migrations:
            return
        with self._engine.begin() as connection:
            for command in migrations:
                connection.execute(text(command))

    def __enter__(self):
        self._create_db()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            if self._session:
                self._session.commit()
        else:
            self._session.rollback()

    def get_msg(self, url):
        return self._session.get(Msg, url)

    def add_msg(self, url, folder_id, sender, date, subject, contents_html, contents_text, attachments):
        msg = self.get_msg(url)
        if not msg:
            msg = Msg(
                url=url,
                folder=folder_id,
                sender=sender,
                date=date,
                subject=subject,
                contents_html=contents_html,
                contents_text=contents_text,
            )
            self._session.add(msg)
            for attachment in attachments:
                self._session.add(attachment)
        return msg

    def get_announcement(self, url):
        return self._session.get(LibrusAnnouncement, url)

    def add_announcement(self, url, sender, subject, date, contents_html, contents_text):
        announcement = self.get_announcement(url)
        if not announcement:
            announcement = LibrusAnnouncement(
                url=url,
                sender=sender,
                subject=subject,
                date=date,
                contents_html=contents_html,
                contents_text=contents_text,
                content_hash=hashlib.sha1(contents_text.encode()).hexdigest(),
            )
            self._session.add(announcement)
        return announcement

    def notify(self, msg_from_db):
        if self._librus_user.notify.is_webhook():
            logger.info(f"Sending '{msg_from_db.subject}' to webhook from {msg_from_db.sender} ({msg_from_db.date})")
            self.send_via_webhook(msg_from_db)
        else:
            logger.info(
                f"Sending '{msg_from_db.subject}' to {self._librus_user.notify.email_dest} from {msg_from_db.sender}"
            )
            self.send_email(msg_from_db)

    def _get_attachments(self, msg_from_db) -> list[Attachment]:
        if not self._session:
            return []
        return self._session.query(Attachment).filter(Attachment.msg_path == msg_from_db.url).all()

    @staticmethod
    def _librus_attachment_links(attachments: list[Attachment]) -> list[tuple[str, str]]:
        return [(a.name, LibrusScraper.get_attachment_download_link(a.link_id)) for a in attachments]

    def _s3_attachment_links(self, msg_from_db, attachments: list[Attachment]) -> list[tuple[str, str]]:
        links: list[tuple[str, str]] = []
        storage = S3AttachmentStorage(self._librus_user.notify)
        for attach in attachments:
            fallback_link = LibrusScraper.get_attachment_download_link(attach.link_id)
            if attach.data is None:
                logger.warning(f"Attachment '{attach.name}' has no data in DB, fallback to Librus link")
                links.append((attach.name, fallback_link))
                continue
            try:
                if not attach.s3_key:
                    s3_key, s3_etag = storage.upload_attachment(
                        librus_user_name=self._librus_user.name,
                        msg_path=msg_from_db.url,
                        attachment=attach,
                    )
                    attach.s3_key = s3_key
                    attach.s3_etag = s3_etag
                    attach.s3_upload_date = datetime.datetime.now()
                    logger.info(f"Uploaded attachment '{attach.name}' to S3 key '{attach.s3_key}'")
                else:
                    logger.info(f"Reusing uploaded S3 key for attachment '{attach.name}': {attach.s3_key}")
                links.append((attach.name, storage.generate_download_link(attach)))
            except Exception as ex:
                logger.warning(f"Failed to upload/presign attachment '{attach.name}' ({ex}), fallback to Librus link")
                links.append((attach.name, fallback_link))
        return links

    def _build_webhook_attachment_links(self, msg_from_db) -> list[tuple[str, str]]:
        attachments = self._get_attachments(msg_from_db)
        if not attachments:
            return []
        if not is_s3_webhook_source(self._librus_user.notify.webhook_attachments_source):
            return self._librus_attachment_links(attachments)
        try:
            return self._s3_attachment_links(msg_from_db, attachments)
        except Exception as ex:
            logger.warning(f"Failed to initialize S3 attachment storage ({ex}), fallback to Librus links")
            return self._librus_attachment_links(attachments)

    def send_via_webhook(self, msg_from_db):
        attachment_links = self._build_webhook_attachment_links(msg_from_db)

        msg = (
            dedent(f"""
        *LIBRUS {self._librus_user.name} - {msg_from_db.date}*
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

        response = requests.post(
            self._librus_user.notify.webhook,
            data=json.dumps(message),
            headers={"Content-Type": "application/json"},
        )

        if response.status_code != 200:
            logger.warning(f"Failed to send message. Status code: {response.status_code}")

    @staticmethod
    def format_sender(sender_info, sender_email):
        sender_b64 = base64.b64encode(f"{sender_info} @ Librus".encode())
        sender_info_encoded = "=?utf-8?B?" + sender_b64.decode() + "?="
        return f'"{sender_info_encoded}" <{sender_email}>'

    def send_email(self, msg_from_db):
        msg = MIMEMultipart("alternative")
        msg.set_charset("utf-8")

        msg["Subject"] = f"[LIBRUS {self._librus_user.name}] {msg_from_db.subject_prefix}{msg_from_db.subject}"
        msg["From"] = self.format_sender(msg_from_db.sender, self._librus_user.notify.smtp_user)
        msg["To"] = ", ".join(self._librus_user.notify.email_dest)

        attachments_only_with_link: list[Attachment] = []
        attachments_with_data: list[Attachment] = []
        if self._session:  # sending testing email doesn't have opened session
            attachments = self._session.query(Attachment).filter(Attachment.msg_path == msg_from_db.url).all()
            for attach in attachments:
                if attach.data is None:
                    attachments_only_with_link.append(attach)
                else:
                    attachments_with_data.append(attach)
        attachments_as_text_msg = (
            ""
            if not attachments_only_with_link
            else "\n\nZałączniki:\n"
            + "\n - ".join(
                LibrusScraper.get_attachment_download_link(att.link_id) for att in attachments_only_with_link
            )
        )
        attachments_as_html_msg = (
            ""
            if not attachments_only_with_link
            else "<br/><br/><p>Załączniki:<p><ul>"
            + "".join(
                f"<li><a href='{LibrusScraper.get_attachment_download_link(att.link_id)}'>{att.name}</a></li>"
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

        server = smtplib.SMTP(self._librus_user.notify.smtp_server, self._librus_user.notify.smtp_port)
        server.ehlo()
        server.starttls()
        server.login(self._librus_user.notify.smtp_user, self._librus_user.notify.smtp_pass)
        server.sendmail(self._librus_user.notify.smtp_user, self._librus_user.notify.email_dest, msg.as_string())
        server.close()


def read_pylibrus_config(workdir: str, config_file: str) -> tuple[PyLibrusConfig, list[LibrusUser]]:
    config_path = Path(workdir) / config_file
    if config_path.exists():
        logger.info(f"Read config from file: {config_path}")
        config = configparser.ConfigParser()
        config.read(config_path)
        pylibrus_config = PyLibrusConfig.from_config(workdir, config)
        librus_users = LibrusUser.load_librus_users_from_config(config)
        return pylibrus_config, librus_users
    else:
        logger.info(f"Could not find config file: {config_path}, read config from env variables")
        pylibrus_config = PyLibrusConfig.from_env(workdir)
        librus_users = [LibrusUser.from_env()]
    return pylibrus_config, librus_users


def send_test_notification(pylibrus_config: PyLibrusConfig, librus_user: LibrusUser):
    notifier = LibrusNotifier(pylibrus_config, librus_user)
    msg = Msg(
        url="/fake/object",
        folder="Odebrane",
        sender="Testing sender Żółta Jaźń [Nauczyciel]",
        date=datetime.datetime.now(),
        subject="Testing subject with żółta jaźć",
        contents_html="<h2>html content with żółta jażń</h2>",
        contents_text="text content with żółta jaźń",
    )
    logger.info("Sending testing notify")
    notifier.notify(msg)

    announcement = LibrusAnnouncement(
        url=announcement_id("Testing announcement", "Testing author", datetime.datetime.now()),
        sender="Testing author Żółta Jaźń [Dyrektor]",
        subject="Testing announcement with żółta jaźć",
        date=datetime.datetime.now(),
        contents_html="<p>html announcement content with żółta jażń</p>",
        contents_text="text announcement content with żółta jaźń",
    )
    logger.info("Sending testing announcement notify")
    notifier.notify(announcement)
    return 2


def handle_user(pylibrus_config: PyLibrusConfig, librus_user: LibrusUser, dry_run: bool = False):
    with LibrusScraper(librus_user.login, librus_user.password, pylibrus_config=pylibrus_config) as scraper:
        with LibrusNotifier(pylibrus_config, librus_user) as notifier:
            msgs = scraper.msgs_from_folder(pylibrus_config.inbox_folder_id)
            for msg_path, read in msgs:
                msg = notifier.get_msg(msg_path)

                if not msg:
                    logger.debug(f"Fetch {msg_path}")

                    fetch_attachment_content = pylibrus_config.fetch_attachments and (
                        librus_user.notify.is_email()
                        or (
                            librus_user.notify.is_webhook()
                            and is_s3_webhook_source(librus_user.notify.webhook_attachments_source)
                        )
                    )
                    msg_content_or_none = scraper.fetch_msg(msg_path, fetch_attachment_content)
                    if msg_content_or_none is None:
                        continue
                    sender, subject, date, contents_html, contents_text, attachments = msg_content_or_none
                    msg = notifier.add_msg(
                        msg_path,
                        pylibrus_config.inbox_folder_id,
                        sender,
                        date,
                        subject,
                        contents_html,
                        contents_text,
                        attachments,
                    )

                if dry_run:
                    # Do not notify and do not touch msg.email_sent, so a regular (non-dry) run
                    # afterwards still sends this message normally.
                    already_sent = "yes" if msg.email_sent else "no"
                    logger.info(f"[DRY RUN] message '{msg.subject}' (already sent: {already_sent})")
                    continue

                if pylibrus_config.send_message == "unsent" and msg.email_sent:
                    logger.info(f"Do not send '{msg.subject}' (message already sent)")
                elif pylibrus_config.send_message == "unread" and read:
                    logger.info(f"Do not send '{msg.subject}' (message already read)")
                else:
                    notifier.notify(msg)
                    msg.email_sent = True

            fetch_announcements = (
                librus_user.fetch_announcements
                if librus_user.fetch_announcements is not None
                else pylibrus_config.fetch_announcements
            )
            if fetch_announcements:
                # A Librus markup change on /ogloszenia must never stop message
                # forwarding above, which is the primary feature - see ANNOUNCEMENTS_PLAN.md.
                try:
                    handle_announcements(pylibrus_config, notifier, scraper, dry_run=dry_run)
                except Exception:
                    logger.exception("Failed to fetch/notify about announcements")


def handle_announcements(
    pylibrus_config: PyLibrusConfig, notifier: "LibrusNotifier", scraper: LibrusScraper, dry_run: bool = False,
):
    # Announcements have no stable id and no per-item read/unread flag, so unlike
    # messages they always dedupe the "unsent" way regardless of `send_message`
    # (see ANNOUNCEMENTS_PLAN.md).
    announcements = scraper.fetch_announcements()
    for title, author, date, contents_html, contents_text in reversed(announcements):
        age = datetime.datetime.now() - date
        if age > datetime.timedelta(days=pylibrus_config.max_age_of_sending_announcement_days):
            logger.info(f"Do not send announcement '{title}' (too old, {date})")
            continue

        url = announcement_id(title, author, date)
        announcement = notifier.get_announcement(url)
        if not announcement:
            announcement = notifier.add_announcement(url, author, title, date, contents_html, contents_text)

        if dry_run:
            # Do not notify and do not touch announcement.email_sent, so a regular
            # (non-dry) run afterwards still sends this announcement normally.
            already_sent = "yes" if announcement.email_sent else "no"
            logger.info(f"[DRY RUN] announcement '{title}' (already sent: {already_sent})")
            continue

        if announcement.email_sent:
            logger.info(f"Do not send announcement '{title}' (already sent)")
        else:
            notifier.notify(announcement)
            announcement.email_sent = True


def parse_args():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument("--debug", action="store_true", help="enable debug")

    paths = parser.add_argument_group("Paths")
    paths.add_argument(
        "--config", metavar="PATH", help="config file, can be absolute or relative to workdir", default="pylibrus.ini"
    )
    paths.add_argument(
        "--cookies",
        metavar="PATH",
        help="cookie file, can be absolute or relative to workdir",
        default="pylibrus_cookies.json",
    )
    paths.add_argument("--workdir", metavar="PATH", help="working directory with config and DBs", default=Path.cwd())

    tests = parser.add_argument_group("Test notifications")
    tests.add_argument("--test-notify", action="store_true", default=False, help="send a test notification")

    dry_run = parser.add_argument_group("Dry run")
    dry_run.add_argument(
        "--dry",
        action="store_true",
        default=False,
        help=(
            "go through all messages without sending them by email or webhook; "
            "just print each message's title and whether it was already sent "
            "(does not touch the email_sent flag, so a regular run afterwards still sends it)"
        ),
    )

    return parser.parse_args()


def main():
    args = parse_args()

    pylibrus_config, librus_users = read_pylibrus_config(args.workdir, args.config)
    pylibrus_config.debug |= args.debug
    pylibrus_config.cookie_file = args.cookies
    logger.info(f"Config: {pylibrus_config}")
    for user in librus_users:
        logger.info(f"User: {user}")

    if args.test_notify:
        return send_test_notification(pylibrus_config, librus_users[0])

    for i, librus_user in enumerate(librus_users):
        handle_user(pylibrus_config, librus_user, dry_run=args.dry)
        if i != len(librus_users) - 1:
            time.sleep(pylibrus_config.sleep_between_librus_users)


if __name__ == "__main__":
    sys.exit(main())
