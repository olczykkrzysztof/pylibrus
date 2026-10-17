"""Test doubles shared by several test modules (pytest puts tests/ on sys.path)."""


class FakeResponse:
    """The parts of `requests.Response` the scraper reads."""

    def __init__(self, url="", text="", content=b"", ok=True, status_code=200, json_data=None):
        self.url = url
        self.text = text
        self.content = content
        self.ok = ok
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        return self._json_data


class FakeSession:
    """Stands in for `LibrusSession`: serves canned responses by path and records every request.

    `pages` maps a path or absolute URL to a FakeResponse, or to a string served as page text.
    `requests` records (method, path, referer) in order, so tests can pin the referer chain.
    """

    def __init__(self, pages=None):
        self.pages = dict(pages or {})
        self.requests: list[tuple[str, str, str | None]] = []
        self.logged_in = False

    def login(self):
        self.logged_in = True

    def get(self, path, referer=None, **kwargs):
        return self._serve("get", path, referer)

    def post(self, path, referer=None, **kwargs):
        return self._serve("post", path, referer)

    def _serve(self, method, path, referer):
        self.requests.append((method, path, referer))
        assert path in self.pages, f"unexpected request: {method} {path}"
        page = self.pages[path]
        return FakeResponse(url=path, text=page) if isinstance(page, str) else page
