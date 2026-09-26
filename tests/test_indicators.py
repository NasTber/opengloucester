"""Tests for the tax bill and unemployment fetchers, offline."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from conftest import FIXTURES
from fakes import FakeResponse
from pipeline import build_site, fetch_finance, fetch_labor
from pipeline.config import load_config

NOW = datetime(2026, 9, 26, 12, tzinfo=ZoneInfo("America/New_York"))


def test_parse_tax_bill_workbook():
    record = fetch_finance.parse_workbook((FIXTURES / "dls_tax_bill.xlsx").read_bytes())
    assert record == {"fiscal_year": 2026, "average_bill": 9502, "average_value": 1020656, "parcels": 7226, "state_rank": 79}


class FakeDLS:
    def __init__(self):
        self.urls = []
        self.sheet = (FIXTURES / "dls_tax_bill.xlsx").read_bytes()

    def get(self, url):
        self.urls.append(url)
        # Only FY2026 has certified figures in this stand-in.
        return FakeResponse(self.sheet if "iclYear=2026" in url else b"")


def test_tax_bill_skips_uncertified_years(tmp_path, monkeypatch):
    real = fetch_finance.parse_workbook
    monkeypatch.setattr(fetch_finance, "parse_workbook", lambda content: real(content) if content else None)
    fetch_finance.run(load_config("gloucester"), FakeDLS(), tmp_path, now=NOW)
    saved = json.loads((tmp_path / "finance" / "tax_bill.json").read_text())
    assert [y["fiscal_year"] for y in saved["years"]] == [2026]


def test_bulk_unemployment_parse_and_year_over_year(tmp_path):
    class Client:
        request_count = 0
        def get(self, url):
            return FakeResponse((FIXTURES / "bls_la_data_sample.txt").read_bytes())
    config = load_config("gloucester")
    data = fetch_labor.from_bulk(Client(), [config["labor"]["local_series"], config["labor"]["state_series"]])
    local = data[config["labor"]["local_series"]]
    assert any(d["year"] == 2026 and d["month"] == 7 and d["rate"] == 4.8 and d["preliminary"] for d in local)
    assert not any(d["year"] == 2025 and d["month"] == 10 for d in local), "months without data are skipped"


def test_change_text_is_neutral():
    assert build_site.change_text(7, "", "last week") == "↑ 7 from last week"
    assert build_site.change_text(-0.5, " pts", "July 2025", 1) == "↓ 0.5 pts from July 2025"
    assert build_site.change_text(0.04, "%", "FY2025", 1) == "No change from FY2025"
