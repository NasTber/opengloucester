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
        if url.endswith("/Archive.aspx"):
            return FakeResponse((FIXTURES / "civicplus_archive_index.html").read_bytes())
        if "Archive.aspx?ADID=" in url:
            adid = url.rsplit("=", 1)[1]
            # Each archive document gets distinct bytes (a trailing PDF comment), as real ones would.
            content = self.agenda if adid == "20124" else self.agenda + f"\n% document {adid}\n".encode()
            return FakeResponse(content, {"content-disposition": "inline;filename=September 28 2026.pdf"})
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
    """Stands in for anthropic.Anthropic: returns a fixed structured result."""

    PREVIEW = {
        "transcript": "# Human Rights Commission\n\n1. Call to order.\n2. Review and approval of July 27, 2026 minutes\n3. Meeting Joe Lucido, Assistant Director of Operations on City ADA compliance\n4. Review HRC Student Member Recruitment Search Draft Description\n5. Community updates.\n6. Next Meeting: October 26",
        "headline": "ADA compliance with the city's operations director and a draft plan for recruiting a student member.",
        "summary": "The commission will meet with the city's Assistant Director of Operations about ADA compliance and review a draft description for recruiting a student member.",
        "items": ["ADA compliance with Joe Lucido", "Student member recruitment description", "Community updates"],
    }
    MINUTES = {
        "transcript": "# Planning Board Minutes\n\nMotion to approve the site plan at 12 Main St. Vote 5-0.",
        "headline": "Approved a site plan for 12 Main St.",
        "summary": "The board approved a site plan for 12 Main St.",
        "is_minutes": True,
        "decisions": ["Approved the site plan for 12 Main St, 5-0"],
    }

    def __init__(self, stop_reason: str = "end_turn"):
        from types import SimpleNamespace
        self.calls = []
        outer = self

        def respond(kwargs):
            import json
            outer.calls.append(kwargs)
            is_minutes = "decisions" in kwargs["output_config"]["format"]["schema"]["properties"]
            payload = FakeAnthropic.MINUTES if is_minutes else FakeAnthropic.PREVIEW
            return SimpleNamespace(
                stop_reason=stop_reason,
                content=[SimpleNamespace(type="text", text=json.dumps(payload))],
                usage=SimpleNamespace(input_tokens=1500, output_tokens=400),
            )

        class Stream:
            def __init__(self, kwargs):
                self.kwargs = kwargs

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return respond(self.kwargs)

        class Messages:
            def stream(self, **kwargs):
                return Stream(kwargs)

        self.messages = Messages()


class FakeDESE:
    """Serves saved DESE open data responses, one per measure."""

    def __init__(self):
        self.urls = []

    def get(self, url):
        import json
        from urllib.parse import unquote_plus
        from pipeline.fetch_schools import MEASURES
        self.urls.append(url)
        query = unquote_plus(url)
        name = next(n for n, m in MEASURES.items() if m["dataset"] in url and m["where"] in query)
        return FakeJSONResponse(json.loads((FIXTURES / f"dese_{name}.json").read_text()))


class FakeBudgetDLS:
    """Serves saved DLS budget workbooks. Schedule A has FY2024 and FY2025; later
    years come back as zeros, earlier ones as an empty table (as the site does)."""

    def __init__(self):
        self.urls = []

    def get(self, url):
        from urllib.parse import parse_qs, urlparse
        self.urls.append(url)
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        folder = FIXTURES / "dls_budget"
        report = q["rdReport"]
        if report == "ScheduleA.GeneralFund":
            year = int(q["islYear"])
            name = f"schedule_a_{year}.xlsx" if year in (2024, 2025) else "schedule_a_2026.xlsx"
        elif report == "DLS_Bond_Ratings":
            name = "bonds_moodys.xlsx" if q["islCompany"] == "Moodys" else "bonds_sp.xlsx"
        else:
            name = {"RevenueBySource.RBS.RevbySource2": "revenue.xlsx", "Prop2.5.ExcessLevyCapandOverride_10_pres": "levy.xlsx",
                    "FreeCash2": "free_cash.xlsx", "Dashboard.TrendAnalysisReports.StabFund": "stabilization.xlsx",
                    "351GenFunperCapita": "per_capita.xlsx"}[report]
        return FakeResponse((folder / name).read_bytes())


class FakeHousing:
    """Serves trimmed Census permit files, a saved Census Reporter response, a
    stand-in PDF, and DLS parcel counts. Patch fetch_housing.pdf_text with
    shi_pdf_text so the stand-in PDF reads as the saved inventory text."""

    def __init__(self):
        self.urls = []

    def get(self, url):
        import json
        self.urls.append(url)
        folder = FIXTURES / "housing"
        if "census.gov/econ/bps" in url:
            name = url.rsplit("/", 1)[1]
            path = folder / f"bps_{name}"
            text = path.read_bytes() if path.exists() else (folder / "bps_ne2025a.txt").read_bytes().split(b"\n \n")[0]
            return FakeResponse(text)
        if "censusreporter" in url:
            return FakeJSONResponse(json.loads((folder / "census_reporter.json").read_text()))
        if "mass.gov" in url:
            return FakeResponse(b"%PDF-1.7 stand-in")
        if "Parcel_counts" in url:
            return FakeResponse((folder / "dls_parcels.xlsx").read_bytes())
        raise AssertionError(url)


def shi_pdf_text(pdf: bytes) -> str:
    return (FIXTURES / "housing" / "shi_text.txt").read_text()


class FakePermits:
    """The city's public Drive folder listing and its permit CSV."""
    def __init__(self):
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        if "embeddedfolderview" in url:
            return FakeResponse((FIXTURES / "permits" / "drive_folder.html").read_bytes())
        if "id=NEWFILE" in url:
            return FakeResponse((FIXTURES / "permits" / "inspsrvcs.csv").read_bytes())
        raise AssertionError(f"unexpected URL {url}")


class FakeDrive:
    """The school district's public Drive folders, saved as served. A folder
    with no saved page is empty; every file downloads as a distinct PDF."""
    EMPTY_FOLDER = b'<html><body><div class="flip-entries"></div></body></html>'

    def __init__(self):
        self.urls = []
        self.pdf = (FIXTURES / "civicplus_agenda_scanned.pdf").read_bytes()

    def get(self, url):
        self.urls.append(url)
        if "embeddedfolderview?id=" in url:
            page = FIXTURES / "drive" / f"{url.rsplit('=', 1)[1]}.html"
            return FakeResponse(page.read_bytes() if page.exists() else self.EMPTY_FOLDER)
        if "uc?export=download&id=" in url:
            return FakeResponse(self.pdf + f"\n% drive file {url.rsplit('=', 1)[1]}\n".encode())
        raise AssertionError(f"unexpected URL {url}")
