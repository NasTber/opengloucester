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


class FakeJSONResponse(FakeResponse):
    def __init__(self, data):
        import json
        super().__init__(json.dumps(data).encode())
        self._data = data

    def json(self):
        return self._data


class FakeSeeClickFix:
    """Serves saved Open311 pages and a single-issue record for any id."""

    def __init__(self, open_items=None, window_items=None, issue=None, missing_ids=()):
        import json
        self.open_items = open_items if open_items is not None else json.loads((FIXTURES / "open311_open_page.json").read_text())
        self.window_items = window_items if window_items is not None else json.loads((FIXTURES / "open311_window_page.json").read_text())
        self.issue = issue or json.loads((FIXTURES / "scf_issue_acknowledged.json").read_text())
        self.missing_ids = set(missing_ids)
        self.urls = []
        self.request_count = 0

    def get(self, url):
        from pipeline.http import FetchError
        self.urls.append(url)
        self.request_count += 1
        if "/requests.json" in url:
            page = int(url.split("page=")[1].split("&")[0])
            items = self.open_items if "status=open" in url else self.window_items
            return FakeJSONResponse(items[(page - 1) * 100: page * 100])
        if "/issues/" in url:
            issue_id = url.rsplit("/", 1)[1]
            if issue_id in self.missing_ids:
                raise FetchError(f"{url}: HTTP 404", 404)
            item = next((i for i in self.open_items + self.window_items if str(i["service_request_id"]) == issue_id), None)
            data = dict(self.issue, id=int(issue_id))
            if item and item["status"] == "closed":
                data.update(status="Archived", closed_at=item["updated_datetime"], updated_at=item["updated_datetime"])
            elif item and int(issue_id) % 2:
                data.update(status="Acknowledged", acknowledged_at=item["updated_datetime"], updated_at=item["updated_datetime"])
            elif item:
                data.update(status="Open", acknowledged_at=None, updated_at=item["updated_datetime"])
            data["created_at"] = item["requested_datetime"] if item else data["created_at"]
            return FakeJSONResponse(data)
        raise AssertionError(f"unexpected URL {url}")


class FakeAnthropic:
    """Stands in for anthropic.Anthropic: returns a fixed structured preview."""

    PREVIEW = {
        "transcript": "# Human Rights Commission\n\n1. Call to order.\n2. Review and approval of July 27, 2026 minutes\n3. Meeting Joe Lucido, Assistant Director of Operations on City ADA compliance\n4. Review HRC Student Member Recruitment Search Draft Description\n5. Community updates.\n6. Next Meeting: October 26",
        "summary": "The commission will meet with the city's Assistant Director of Operations about ADA compliance and review a draft description for recruiting a student member.",
        "items": ["Approve July 27 minutes", "ADA compliance with Joe Lucido", "Student member recruitment description", "Community updates"],
        "attend": "In person at City Hall, 9 Dale Avenue, or remotely by Zoom (meeting ID 86129893412).",
    }

    def __init__(self, stop_reason: str = "end_turn"):
        from types import SimpleNamespace
        self.calls = []
        outer = self

        class Messages:
            def create(self, **kwargs):
                import json
                outer.calls.append(kwargs)
                return SimpleNamespace(
                    stop_reason=stop_reason,
                    content=[SimpleNamespace(type="text", text=json.dumps(FakeAnthropic.PREVIEW))],
                    usage=SimpleNamespace(input_tokens=1500, output_tokens=400),
                )

        self.messages = Messages()
