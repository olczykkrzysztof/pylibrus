import datetime
import hashlib

TRUE_VALUES = ("yes", "on", "true", "1")
FALSE_VALUES = ("no", "off", "false", "0")


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
