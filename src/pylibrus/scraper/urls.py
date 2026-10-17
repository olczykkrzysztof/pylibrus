"""Librus URLs, shared by the session, the scraper and the notifiers (attachment links)."""

API_URL = "https://api.librus.pl"
SYNERGIA_URL = "https://synergia.librus.pl"


def attachment_download_link(link_id: str) -> str:
    return f"{SYNERGIA_URL}/wiadomosci/pobierz_zalacznik/{link_id}"


def synergia_url_from_path(path):
    if path.startswith("https://"):
        return path
    return SYNERGIA_URL + path


def api_url_from_path(path):
    return API_URL + path


def msg_folder_path(folder_id):
    return f"/wiadomosci/{folder_id}"
