import abc
import dataclasses
import os
from configparser import ConfigParser

from pylibrus.storage.s3 import parse_s3_source
from pylibrus.utils import str_to_bool, str_to_int

DEFAULT_WEBHOOK_ATTACHMENTS_SOURCE = "librus_link"


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

    def destination_key(self) -> tuple:
        """Identity of the place notifications are delivered to.

        Librus users whose configs produce the same key are notified together: one message
        sent to several children yields a single notification per destination, annotated with
        that destination's children (see MULTI_RECIPIENT_PLAN.md). Keep this in sync when
        adding a notification setting, the same way from_config()/from_env() are kept in sync.
        """
        raise NotImplementedError


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

    def destination_key(self) -> tuple:
        return "email", self.smtp_server, self.smtp_port, self.smtp_user, tuple(sorted(self.email_dest))

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

    def destination_key(self) -> tuple:
        # Deliberately keyed on the webhook URL alone. Including webhook_attachments_source
        # would split users who post to the *same* webhook with different attachment sources
        # into two groups, so that webhook would receive two copies of every shared message -
        # worse than serving both from one user's attachment config. See MULTI_RECIPIENT_PLAN.md.
        return "webhook", self.webhook

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
    # False leaves this child out of a grouped notification's label whenever another child
    # received the same message - but never when nothing else would name it. Labelling only:
    # it does not affect delivery or dedupe. See SUBJECT_NAMES_PLAN.md.
    always_include_in_subject: bool = True

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
        # Unlike fetch_announcements above, there is no global counterpart to fall back to
        # (the entry names *which* child to leave out), so the fallback is the real default.
        always_include_in_subject = config[section].getboolean("always_include_in_subject", fallback=True)
        return cls(
            name=name,
            login=librus_user,
            password=librus_pass,
            notify=notify,
            db_name=db_name,
            fetch_announcements=fetch_announcements,
            always_include_in_subject=always_include_in_subject,
        )

    @classmethod
    def from_env(cls) -> "LibrusUser":
        # LibrusUser has no __post_init__ normalising None back to each field's default (unlike
        # PyLibrusConfig), so an unset variable must not reach a defaulted bool field as None:
        # None is falsy and would quietly stop naming the single configured user.
        always_include_in_subject = str_to_bool(os.environ.get("ALWAYS_INCLUDE_IN_SUBJECT"))
        return cls(
            login=os.environ.get("LIBRUS_USER"),
            password=os.environ.get("LIBRUS_PASS"),
            name=os.environ.get("LIBRUS_NAME"),
            notify=WebhookNotify.from_env() if str_to_bool(os.environ.get("WEBHOOK")) else EmailNotify.from_env(),
            db_name=os.environ.get("DB_NAME"),
            fetch_announcements=str_to_bool(os.environ.get("FETCH_ANNOUNCEMENTS")),
            always_include_in_subject=True if always_include_in_subject is None else always_include_in_subject,
        )

    @classmethod
    def load_librus_users_from_config(cls, config: ConfigParser) -> list["LibrusUser"]:
        users = []
        for section in config.sections():
            if section.startswith("user:"):
                user = cls.from_config(config, section)
                users.append(user)
        return users
