import functools
import http.server
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import shutil  # noqa: E402

from fakes import (FIXTURES, FakeAnthropic, FakeBudgetDLS, FakeCityClient, FakeDESE, FakeHousing,  # noqa: E402
                   FakePermits, FakeSeeClickFix, shi_pdf_text)
from pipeline import (build_site, compute_311, fetch_311, fetch_budget, fetch_housing, fetch_meetings,  # noqa: E402
                      fetch_minutes, fetch_permits, fetch_schools, summarize)
from pipeline.config import DATA_DIR as REAL_DATA_DIR  # noqa: E402
from pipeline.config import load_config  # noqa: E402

TZ = ZoneInfo("America/New_York")
FETCHED_AT = datetime(2026, 9, 26, 12, 0, tzinfo=TZ)
BUILT_AT = datetime(2026, 10, 2, 7, 0, tzinfo=TZ)


def make_fixture_data(data_dir: Path) -> None:
    """Run every pipeline step offline against saved source data.

    The meetings pipeline runs twice; the second run simulates the city
    cancelling one meeting and deleting another, so change history is
    exercised. Agenda previews come from a stand-in model client.
    """
    config = load_config("gloucester")
    fetch_meetings.run(config, FakeCityClient(), data_dir, now=FETCHED_AT)
    feed = (FIXTURES / "civicplus_calendar.xml").read_text()
    feed = feed.replace("6:00 PM Licensing Board Special Meeting", "CANCELLED - 6:00 PM Licensing Board Special Meeting")
    start = feed.index("<item", feed.index("Committee for the Arts") - 400)
    feed = feed[:start] + feed[feed.index("</item>", start) + len("</item>"):]
    fetch_meetings.run(config, FakeCityClient(feed=feed.encode()), data_dir, now=FETCHED_AT.replace(day=27))
    fetch_minutes.run(config, FakeCityClient(), data_dir, now=FETCHED_AT)
    summarize.run(config, FakeAnthropic(), data_dir, limit=50, now=FETCHED_AT)

    shutil.copytree(REAL_DATA_DIR / "static", data_dir / "static")
    fetch_311.save_json(data_dir / "finance" / "tax_bill.json", {
        "source_url": "https://dls-gw.dor.state.ma.us/reports/rdPage.aspx", "years": [
            {"fiscal_year": 2025, "average_bill": 9224}, {"fiscal_year": 2026, "average_bill": 9502}]})
    fetch_311.save_json(data_dir / "labor" / "unemployment.json", {
        "source_url": "https://data.bls.gov/timeseries/LAUCT252615000000003", "months": [
            {"year": 2025, "month": 7, "rate": 5.3}, {"year": 2026, "month": 7, "rate": 4.8, "preliminary": True}]})
    fetch_schools.run(config, FakeDESE(), data_dir, now=FETCHED_AT)
    fetch_budget.run(config, FakeBudgetDLS(), data_dir, now=FETCHED_AT)
    real_pdf_text, fetch_housing.pdf_text = fetch_housing.pdf_text, shi_pdf_text
    try:
        fetch_housing.run(config, FakeHousing(), data_dir, now=FETCHED_AT)
    finally:
        fetch_housing.pdf_text = real_pdf_text
    fetch_permits.run(config, FakePermits(), data_dir, now=FETCHED_AT)
    fetch_311.run(config, FakeSeeClickFix(), data_dir, now=FETCHED_AT, detail_limit=80)
    fetch_311.save_json(data_dir / "311" / "scorecard.json", compute_311.compute(config, data_dir, now=FETCHED_AT))


def _build_fixture_site() -> Path:
    base = Path(tempfile.mkdtemp(prefix="opengloucester-test-"))
    make_fixture_data(base / "data")
    build_site.build("gloucester", base / "site", data_dir=base / "data", now=BUILT_AT)
    return base


FIXTURE_BASE = _build_fixture_site()
SITE_DIR = FIXTURE_BASE / "site"
DATA_DIR = FIXTURE_BASE / "data"
# Every built page, as a URL path, plus an unknown path for the 404 page.
PAGE_PATHS = sorted(
    build_site.url_for(p.relative_to(SITE_DIR)) for p in SITE_DIR.rglob("*.html") if p.name != "404.html"
)


@pytest.fixture(scope="session")
def site_dir():
    return SITE_DIR


@pytest.fixture(scope="session")
def data_dir():
    return DATA_DIR


@pytest.fixture(scope="session")
def config():
    return load_config("gloucester")


@pytest.fixture(scope="session")
def page_files(site_dir):
    return sorted(site_dir.rglob("*.html"))


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Static server that mimics GitHub Pages: /foo -> /foo/index.html, 404.html for misses."""

    def log_message(self, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        page = Path(self.directory) / "404.html"
        if code == 404 and page.exists():
            body = page.read_bytes()
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().send_error(code, message, explain)


@pytest.fixture(scope="session")
def server_url(site_dir):
    handler = functools.partial(_Handler, directory=str(site_dir))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
