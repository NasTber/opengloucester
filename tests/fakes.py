"""Offline stand-ins for network access, serving saved fixtures."""

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, content: bytes, headers: dict | None = None):
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.headers = headers or {}


class FakeCityClient:
    """Serves the saved calendar feed, one event page for every event, and one agenda PDF."""

    def __init__(self, feed: bytes | None = None, event_page: str | None = None):
        self.feed = feed or (FIXTURES / "civicplus_calendar.xml").read_bytes()
        self.event_page = event_page or (FIXTURES / "civicplus_event.html").read_text()
        self.agenda = (FIXTURES / "civicplus_agenda_scanned.pdf").read_bytes()
        self.urls = []
        self.request_count = 0

    def get(self, url: str) -> FakeResponse:
        self.urls.append(url)
        self.request_count += 1
        if "RSSFeed.aspx" in url:
            return FakeResponse(self.feed)
        if "Calendar.aspx?EID=" in url:
            return FakeResponse(self.event_page.encode())
        if "Archive.aspx?ADID=" in url:
            return FakeResponse(self.agenda, {"content-disposition": "inline;filename=September 28 2026.pdf"})
        raise AssertionError(f"unexpected URL {url}")
