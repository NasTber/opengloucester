"""Tests for collecting minutes from the Archive Center, offline."""

import json

import pytest

from conftest import FETCHED_AT, FIXTURES
from fakes import FakeAnthropic, FakeCityClient, FakeResponse
from pipeline import civicplus, fetch_meetings, fetch_minutes, summarize
from pipeline.config import load_config

BASE = "https://www.gloucester-ma.gov"


@pytest.mark.parametrize("title, expected", [
    ("May 21, 2026", "2026-05-21"),
    ("September 17, 2026 REVISED", "2026-09-17"),
    ("June15, 2026 REVISED", "2026-06-15"),
    ("MARCH 19, 2024 -NOTICE OF RECOUNT", "2024-03-19"),
    ("Affordable Housing Trust Minutes 3-9-2026", "2026-03-09"),
    ("11-17-25", "2025-11-17"),
    ("Minutes_2015.03.26", "2015-03-26"),
    ("June 4, FY26 Budget Departmental Review meeting #4", None),
    ("Plastic Bag Guide", None),
])
def test_parse_title_date(title, expected):
    assert civicplus.parse_title_date(title) == expected


@pytest.mark.parametrize("name, expected", [
    ("Planning Board - Minutes", ("Planning Board", "minutes")),
    ("City Council Agendas and Packets", ("City Council", "agendas")),
    ("Budget and Finance Standing Committee Minutes", ("Budget and Finance Standing Committee", "minutes")),
    ("Zoning Board of Appeals Meeting Results", ("Zoning Board of Appeals", "results")),
    ("Contributory Retirement System: Minutes", ("Contributory Retirement System", "minutes")),
])
def test_collection_body(name, expected):
    assert civicplus.collection_body(name) == expected


def test_parse_archive_index():
    collections = civicplus.parse_archive_index((FIXTURES / "civicplus_archive_index.html").read_text(), BASE)
    assert len(collections) == 7
    minutes = next(c for c in collections if c["name"] == "Planning Board - Minutes")
    assert minutes["items"][0]["url"].startswith(f"{BASE}/Archive.aspx?ADID=")
    assert all(i["date"] for i in minutes["items"])


@pytest.fixture
def config():
    return load_config("gloucester")


def load(data_dir):
    return json.loads((data_dir / "meetings" / "meetings.json").read_text())


def test_collects_minutes_since_start_date_only(config, tmp_path):
    client = FakeCityClient()
    summary = fetch_minutes.run(config, client, tmp_path, now=FETCHED_AT)
    store = load(tmp_path)
    minutes = [d for m in store.values() for d in m.get("minutes", [])]
    assert summary["minutes_added"] == len(minutes) > 0
    since = config["archive"]["minutes_since"]
    assert all(m["date"] >= since for m in store.values())
    # Agenda collections are not collected here.
    assert all(m.get("minutes") for m in store.values())
    # Budget & Finance minutes land under the calendar's name for that committee.
    assert any(m["body"] == "City Council Budget & Finance Committee" for m in store.values())
    again = FakeCityClient()
    assert fetch_minutes.run(config, again, tmp_path, now=FETCHED_AT)["minutes_added"] == 0
    assert [u for u in again.urls if "ADID=" in u] == [], "saved minutes are not downloaded again"


def test_minutes_attach_to_recorded_meeting(config, tmp_path):
    fetch_meetings.run(config, FakeCityClient(), tmp_path, now=FETCHED_AT)
    page = (FIXTURES / "civicplus_archive_index.html").read_text()
    # Pretend the Human Rights Commission posted minutes for its Sept 28 meeting.
    page = page.replace(">March 16, 2026 MINUTES", ">September 28, 2026 MINUTES", 1)

    class Client(FakeCityClient):
        def get(self, url):
            if url.endswith("/Archive.aspx"):
                self.urls.append(url)
                return FakeResponse(page.encode())
            return super().get(url)

    fetch_minutes.run(config, Client(), tmp_path, now=FETCHED_AT.replace(day=29))
    hrc = load(tmp_path)["12928"]
    assert hrc["minutes"] and hrc["minutes"][0]["title"].startswith("September 28, 2026")
    assert not any(m.get("source") == "archive" and m["date"] == "2026-09-28" for m in load(tmp_path).values())


def test_minutes_summary_records_decisions(config, tmp_path):
    fetch_minutes.run(config, FakeCityClient(), tmp_path, now=FETCHED_AT)
    client = FakeAnthropic()
    summarize.run(config, client, tmp_path, limit=50, now=FETCHED_AT)
    documents = sum(len(m.get("minutes", [])) and 1 for m in load(tmp_path).values())
    assert len(client.calls) == documents
    assert all("decisions" in c["output_config"]["format"]["schema"]["properties"] for c in client.calls)
    saved = [json.loads(p.read_text()) for p in (tmp_path / "summaries").glob("*.json")]
    assert all(s["kind"] == "minutes" and s["decisions"] for s in saved)


def test_spending_limit_stops_run(config, tmp_path):
    fetch_meetings.run(config, FakeCityClient(), tmp_path, now=FETCHED_AT)
    config["summaries"]["max_cost_per_run"] = 0.0
    result = summarize.run(config, FakeAnthropic(), tmp_path, limit=50, now=FETCHED_AT)
    assert result["summarized"] == 0
    assert "spending limit" in result["errors"][0]
