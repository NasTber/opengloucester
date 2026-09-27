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


def test_school_figures_compare_like_with_like(tmp_path):
    from fakes import FakeDESE
    from pipeline import fetch_schools
    client = FakeDESE()
    latest = fetch_schools.run(load_config("gloucester"), client, tmp_path, now=NOW)
    # The district's "adjusted cohort" rate has no state counterpart, so it is not used.
    assert all("4-Year+Graduation+Rate%27" in u or "Graduation" not in u for u in client.urls)
    assert latest["graduation"] == {"year": 2025, "town": 84.7, "state": 89.3}
    assert latest["absenteeism"]["year"] == 2026 and latest["mcas_math"]["town"] == 29.0
    saved = json.loads((tmp_path / "schools" / "schools.json").read_text())
    years = [y["year"] for y in saved["measures"]["mcas_ela"]["years"]]
    assert years == sorted(years) and 2020 not in years and len(years) <= fetch_schools.YEARS_KEPT


def test_school_year_label():
    assert build_site.school_year(2026) == "2025–26"


def test_budget_fetch(tmp_path):
    from fakes import FakeBudgetDLS
    from pipeline import fetch_budget
    config = load_config("gloucester")
    client = FakeBudgetDLS()
    fetch_budget.run(config, client, tmp_path, now=NOW)
    b = json.loads((tmp_path / "finance" / "budget.json").read_text())
    # FY2026 Schedule A is all zeros (not yet reported) and is skipped.
    assert [y["fiscal_year"] for y in b["spending"]] == [2024, 2025]
    fy25 = b["spending"][-1]
    assert fy25["total"] == 140559783 and fy25["functions"]["Education"] == 45682762
    assert sum(fy25["functions"].values()) == fy25["total"]
    assert b["revenue"][-1] == {"fiscal_year": 2026, "total": 153398542, "sources": {
        "Property tax": 109650852, "State aid": 17960146, "Local receipts": 19102669, "Other": 6684875}}
    assert len(b["revenue"]) == fetch_budget.YEARS
    assert b["levy"][-1]["excess_capacity"] == 139295
    assert b["free_cash"][-1] == {"fiscal_year": 2026, "amount": 4112161}
    assert b["stabilization"][-1] == {"fiscal_year": 2025, "amount": 3802715}
    assert b["bond_ratings"] == [{"agency": "Moody's", "rating": "Aa3", "fiscal_year": 2026},
                                 {"agency": "S&P", "rating": "AA", "fiscal_year": 2026}]
    # Per resident: the latest year nearly every community has reported.
    assert b["per_resident"] == {"fiscal_year": 2025, "town": 4711, "population": 29836,
                                 "state_median": 4297, "communities": 349}
    assert any("iclMuni=107" in u and "ScheduleA" in u for u in client.urls)

    # A second run within a week does not fetch again.
    before = len(client.urls)
    assert "skipped" in fetch_budget.run(config, client, tmp_path, now=NOW)
    assert len(client.urls) == before


def test_budget_rejects_error_page():
    import pytest
    from pipeline import fetch_budget
    from pipeline.http import FetchError
    with pytest.raises(FetchError):
        fetch_budget.rows(b"<html>ORA-01722</html>")


def test_money_format():
    assert build_site.format_money(140559783) == "$140.6 million"
    assert build_site.format_money(140559783, "short") == "$140.6M"
    assert build_site.format_money(139295, "short") == "$139K"
    assert build_site.format_money(4711) == "$4,711"
    assert build_site.format_money(None) == "–"


def test_housing_fetch(tmp_path, monkeypatch):
    from fakes import FakeHousing, shi_pdf_text
    from pipeline import fetch_housing
    monkeypatch.setattr(fetch_housing, "pdf_text", shi_pdf_text)
    config = load_config("gloucester")
    result = fetch_housing.run(config, FakeHousing(), tmp_path, now=NOW)
    assert result["problems"] == []
    h = json.loads((tmp_path / "housing" / "housing.json").read_text())
    # Only the Massachusetts Gloucester, not New Gloucester, ME or Gloucester City, NJ.
    assert h["permits"]["years"] == [
        {"year": 2024, "units": 79, "by_size": {"1 unit": 19, "2 units": 20, "3-4 units": 0, "5+ units": 40},
         "months_reported": 0, "estimated": True},
        {"year": 2025, "units": 77, "by_size": {"1 unit": 28, "2 units": 8, "3-4 units": 11, "5+ units": 30},
         "months_reported": 12, "estimated": False}]
    assert h["permits"]["year_to_date"] == {"year": 2026, "through_month": 8, "units": 50, "estimated": True}
    town, state = h["acs"]["town"], h["acs"]["state"]
    assert town["median_home_value"] == {"value": 600600, "moe": 17698}
    assert town["median_rent"] == {"value": 1411, "moe": 94}
    assert town["rent_30_plus"]["value"] == 55.1 and 0 < town["rent_30_plus"]["moe"] < 10
    assert town["owner_occupied"] + town["renter_occupied"] + town["vacant"] == town["homes"]
    assert state["median_rent"]["value"] > 0
    assert h["shi"]["percent"] == 8.04 and h["shi"]["as_of"] == "2025-09-30" and h["shi"]["shi_units"] == 1117
    assert h["parcels"]["fiscal_year"] == 2026 and h["parcels"]["types"]["Single-family homes"] == 7226


def test_housing_keeps_last_figures_when_a_source_fails(tmp_path, monkeypatch):
    from fakes import FakeHousing, shi_pdf_text
    from pipeline import fetch_housing
    from pipeline.http import FetchError
    monkeypatch.setattr(fetch_housing, "pdf_text", shi_pdf_text)
    config = load_config("gloucester")
    fetch_housing.run(config, FakeHousing(), tmp_path, now=NOW)

    class Blocked(FakeHousing):
        def get(self, url):
            if "mass.gov" in url:
                raise FetchError("HTTP 403", 403)
            return super().get(url)

    result = fetch_housing.run(config, Blocked(), tmp_path, now=NOW.replace(month=11))
    assert result["problems"] and result["problems"][0].startswith("shi:")
    h = json.loads((tmp_path / "housing" / "housing.json").read_text())
    assert h["shi"]["percent"] == 8.04
