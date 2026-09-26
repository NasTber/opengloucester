"""Tests for the meetings pipeline, run offline against saved city pages."""

import json
from datetime import datetime

import pytest

from conftest import FETCHED_AT, FIXTURES
from fakes import FakeCityClient
from pipeline import civicplus, fetch_meetings
from pipeline.config import load_config

BASE = "https://www.gloucester-ma.gov"


@pytest.mark.parametrize("title, expected", [
    ("6:00 PM Human Rights Commission", ("Human Rights Commission", "Human Rights Commission", "scheduled", False)),
    ("6:00 PM Licensing Board Special Meeting", ("Licensing Board Special Meeting", "Licensing Board", "scheduled", True)),
    ("Board of Health Meeting, 5:30 PM", ("Board of Health Meeting", "Board of Health", "scheduled", False)),
    ("CANCELLED - 6:00 PM Planning Board", ("Planning Board", "Planning Board", "cancelled", False)),
    ("6:30 PM Joint Special City Council & Planning Board Meeting",
     ("Joint Special City Council & Planning Board Meeting", "Joint City Council & Planning Board", "scheduled", True)),
])
def test_parse_title(title, expected):
    t = civicplus.parse_title(title)
    assert (t["title"], t["body"], t["status"], t["special"]) == expected


@pytest.mark.parametrize("value, expected", [
    ("06:00 PM - 11:59 PM", ("18:00", None)),       # CivicPlus default end time means "not given"
    ("06:30 PM - 08:30 PM", ("18:30", "20:30")),
    ("12:00 AM - 11:59 PM", (None, None)),           # all-day event
    ("", (None, None)),
])
def test_parse_times(value, expected):
    assert civicplus.parse_times(value) == expected


def test_parse_calendar_feed():
    events = civicplus.parse_calendar_feed((FIXTURES / "civicplus_calendar.xml").read_bytes(), BASE)
    assert len(events) == 21
    hrc = next(e for e in events if e["id"] == "12928")
    assert hrc["date"] == "2026-09-28"
    assert hrc["start_time"] == "18:00"
    assert hrc["source_url"] == f"{BASE}/Calendar.aspx?EID=12928"


def test_parse_event_page():
    details = civicplus.parse_event_page((FIXTURES / "civicplus_event.html").read_text(), BASE)
    assert details == {
        "agenda_url": f"{BASE}/Archive.aspx?ADID=20124",
        "agenda_id": "20124",
        "start": "2026-09-28T18:00:00",
        "location_name": "City Council Committee Room",
        "address": "9 Dale Ave, 1st Floor, Gloucester, MA 01930",
        "remote_url": "https://gloucester-ma-gov.zoom.us/j/86129893412",
    }


def test_parse_event_page_without_agenda():
    assert civicplus.parse_event_page("<html><body>No details</body></html>", BASE) == {}


@pytest.fixture
def config():
    return load_config("gloucester")


def load(data_dir):
    return json.loads((data_dir / "meetings" / "meetings.json").read_text())


def test_run_keeps_meetings_and_skips_community_events(config, tmp_path):
    status = fetch_meetings.run(config, FakeCityClient(), tmp_path, now=FETCHED_AT)
    store = load(tmp_path)
    titles = {m["title"] for m in store.values()}
    assert "The Backyard Growcery" not in titles
    assert "Community Safety Day" not in titles
    assert "Planning Board" in titles
    assert status["new_meetings"] == len(store) == 17
    assert status["errors"] == []


def test_run_saves_agenda_once(config, tmp_path):
    fetch_meetings.run(config, FakeCityClient(), tmp_path, now=FETCHED_AT)
    agenda = load(tmp_path)["12928"]["agendas"][0]
    saved = tmp_path / "meetings" / "agendas" / agenda["file"]
    assert saved.read_bytes().startswith(b"%PDF")
    assert agenda["has_text"] is False  # the city's agendas are scanned images
    assert agenda["original_filename"] == "September 28 2026.pdf"

    second = FakeCityClient()
    fetch_meetings.run(config, second, tmp_path, now=FETCHED_AT)
    assert not any("Archive.aspx" in u for u in second.urls), "agenda should not be downloaded again"
    assert len(load(tmp_path)["12928"]["agendas"]) == 1


def test_run_records_revised_agenda(config, tmp_path):
    fetch_meetings.run(config, FakeCityClient(), tmp_path, now=FETCHED_AT)
    revised = (FIXTURES / "civicplus_event.html").read_text().replace("ADID=20124", "ADID=20200")
    fetch_meetings.run(config, FakeCityClient(event_page=revised), tmp_path, now=FETCHED_AT.replace(day=27))
    m = load(tmp_path)["12928"]
    assert [a["id"] for a in m["agendas"]] == ["20124", "20200"]
    assert {"field": "agenda", "old": "20124", "new": "20200"}.items() <= m["history"][-1].items()


def test_run_records_changes_and_keeps_links_stable(data_dir):
    store = load(data_dir)  # built by conftest: second run cancels one meeting and drops another
    licensing = store["12963"]
    assert licensing["status"] == "cancelled"
    assert licensing["history"][0]["field"] == "status"
    assert licensing["slug"] == "2026-09-28-licensing-board"
    arts = store["12172"]
    assert arts["listed"] is False
    assert arts["history"][0] == {"at": "2026-09-27T12:00:00-04:00", "field": "listed", "old": True, "new": False}


def test_run_does_not_recheck_past_meetings(config, tmp_path):
    fetch_meetings.run(config, FakeCityClient(), tmp_path, now=FETCHED_AT)
    later = FakeCityClient()
    fetch_meetings.run(config, later, tmp_path, now=datetime(2026, 10, 6, 12, tzinfo=FETCHED_AT.tzinfo))
    checked = {u.split("EID=")[1] for u in later.urls if "EID=" in u}
    past = {m["id"] for m in load(tmp_path).values() if m["date"] < "2026-10-06"}
    assert checked and not checked & past


def test_placeholder_location_is_dropped():
    page = (FIXTURES / "civicplus_event.html").read_text().replace("City Council Committee Room", "Event Location")
    assert "location_name" not in civicplus.parse_event_page(page, BASE)
