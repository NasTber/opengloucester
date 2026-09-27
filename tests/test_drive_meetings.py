"""School Committee agendas and minutes from the school district's Google Drive."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeDrive, FakeResponse

from pipeline import fetch_drive_meetings
from pipeline.config import load_config
from pipeline.fetch_drive_meetings import list_folder, parse_name
from pipeline.fetch_meetings import save_json

NOW = datetime(2026, 9, 27, 7, 0, tzinfo=ZoneInfo("America/New_York"))
SHORT = load_config("gloucester")["drive_meetings"]["abbreviations"]


@pytest.mark.parametrize("name, date, variant, revised, special", [
    ("SC Agenda 9_9_26.pdf", "2026-09-09", "", False, False),
    ("SC Agenda 8_12_26", "2026-08-12", "", False, False),
    ("SC Agenda 10_08_25.pdf", "2025-10-08", "", False, False),
    ("SC Agenda REVISED Agenda 6_10_26.pdf", "2026-06-10", "", True, False),
    ("Special SC Agenda 7_20_26", "2026-07-20", "", False, True),
    ("SC Agenda Online 3_9_26.pdf", "2026-03-09", "", False, False),
    ("SC Governance Workshop Agenda 4_8_26.pdf", "2026-04-08", "Governance Workshop", False, False),
    ("B&F Subcommittee Agenda 9_16_26.pdf", "2026-09-16", "", False, False),
    ("B & F Minutes 11_12_25 .pdf", "2025-11-12", "", False, False),
    ("Program Agenda  10_16_25.pdf", "2025-10-16", "", False, False),
    ("Revised Personnel Agenda (Food Service Workers) 3_23_26.pdf", "2026-03-23", "Food Service Workers", True, False),
    ("Amended SC Minutes 6_24_20.pdf", "2020-06-24", "", True, False),
])
def test_file_names(name, date, variant, revised, special):
    info = parse_name(name, SHORT)
    assert (info["date"], info["variant"], info["revised"], info["special"]) == (date, variant, revised, special)
    assert not info["left_out"]


def test_names_without_a_date_or_left_out():
    assert parse_name("B & F Agenda 6_17_2.pdf", SHORT) is None
    assert parse_name("SC Agenda 13_40_26.pdf", SHORT) is None
    assert parse_name("ES SC Minutes 10_28_20.pdf", SHORT)["left_out"]
    assert parse_name("Joint CC & SC Minutes 9_15_20.pdf", SHORT)["left_out"]


def test_real_folder_page():
    entries = list_folder(FakeDrive().get("https://drive.google.com/embeddedfolderview?id=1uDibz6g_fSFrl7MwqyU_bt__Sb2C_iPB").text)
    assert [e["name"] for e in entries] == [
        "Ad Hoc Communications Subcommittee Minutes", "Building & Finance Subcommittee Minutes",
        "Personnel Subcommittee Minutes", "Program Subcommittee Minutes", "School Committee Minutes"]
    assert all(e["folder"] for e in entries)


def test_collects_documents_from_the_saved_folders(tmp_path):
    config = load_config("gloucester")
    status = fetch_drive_meetings.run(config, FakeDrive(), tmp_path, now=NOW)
    store = json.loads((tmp_path / "meetings" / "meetings.json").read_text())
    got = sorted((m["date"], m["body"], len(m.get("agendas", [])), len(m.get("minutes", []))) for m in store.values())
    assert got == [
        ("2026-08-12", "School Committee", 1, 1),
        ("2026-08-26", "School Committee", 1, 1),
        ("2026-09-09", "School Committee", 1, 1),
        ("2026-09-16", "School Committee Building & Finance Subcommittee", 1, 0),
    ]
    assert status["unreadable_names"] == ["B & F Agenda 6_17_2.pdf"]
    meeting = next(m for m in store.values() if m["date"] == "2026-09-09")
    assert meeting["source"] == "drive" and meeting["source_url"] == "https://schoolcommittee.gloucesterschools.com/"
    minutes = meeting["minutes"][0]
    assert minutes["original_filename"] == "SC Minutes 9_9_26.pdf"
    assert minutes["source_url"] == f"https://drive.google.com/file/d/{minutes['id']}/view"
    assert (tmp_path / "meetings" / "minutes" / minutes["file"]).exists()

    # A second run adds nothing and doesn't warn again about the same file name.
    again = fetch_drive_meetings.run(config, FakeDrive(), tmp_path, now=NOW)
    assert again["documents_added"] == 0 and again["new_unreadable_names"] == []


def entry(file_id: str, name: str, folder: bool = False) -> str:
    kind = "drive/folders" if folder else "file/d"
    return (f'<div class="flip-entry" id="entry-{file_id}" tabindex="0" role="link"><div class="flip-entry-info">'
            f'<a href="https://drive.google.com/{kind}/{file_id}" target="_blank"><div class="flip-entry-title">{name}</div>'
            f'</a></div><div class="flip-entry-last-modified"><div>Sep 15</div></div></div>')


class MadeUpDrive(FakeDrive):
    """Folders written by the test, in the markup Drive serves."""
    def __init__(self, folders: dict):
        super().__init__()
        self.folders = folders

    def get(self, url):
        if "embeddedfolderview?id=" in url:
            entries = self.folders.get(url.rsplit("=", 1)[1], [])
            return FakeResponse(f'<div class="flip-entries">{"".join(entry(*e) for e in entries)}</div>'.encode())
        return super().get(url)


def test_versions_variants_and_calendar_meetings(tmp_path):
    config = load_config("gloucester")
    settings = config["drive_meetings"]
    save_json(tmp_path / "meetings" / "meetings.json", {"12999": {
        "id": "12999", "date": "2026-10-14", "body": "School Committee", "title": "School Committee Meeting",
        "slug": "2026-10-14-school-committee", "first_seen": "2026-10-01T08:00:00-04:00"}})
    drive = MadeUpDrive({
        settings["agendas_folder"]: [("sca", "School Committee Agendas", True), ("pa", "Personnel Subcommittee Agendas", True),
                                     ("xa", "Superintendent Search Agendas", True)],
        settings["minutes_folder"]: [("scm", "School Committee Minutes", True)],
        "sca": [("rev", "SC Agenda REVISED 10_14_26.pdf"), ("orig", "SC Agenda 10_14_26.pdf"),
                ("ws", "SC Governance Workshop Agenda 10_14_26.pdf"), ("old", "SC Agenda 6_10_26.pdf"),
                ("y", "2026-2027 School Year", True), ("y0", "2024-2025 School Year", True)],
        "y": [("in-year", "Special SC Agenda 10_20_26.pdf")],
        "y0": [("too-old", "SC Agenda 10_9_24.pdf")],
        "pa": [("food", "Personnel Agenda (Food Service Workers) 10_5_26.pdf"),
               ("bus", "Personnel Agenda (Transportation Workers) 10_5_26.pdf")],
        "xa": [("other", "Search Agenda 10_1_26.pdf")],
        "scm": [("es", "ES SC Minutes 10_14_26.pdf")],
    })
    status = fetch_drive_meetings.run(config, drive, tmp_path, now=NOW)
    store = json.loads((tmp_path / "meetings" / "meetings.json").read_text())

    # Attached to the meeting on the city calendar, original first and the revision last.
    calendar = store["12999"]
    assert [a["id"] for a in calendar["agendas"]] == ["orig", "rev"]
    assert calendar["history"][0]["field"] == "agenda"
    titles = sorted(m["title"] for m in store.values() if m["id"] != "12999")
    assert titles == ["School Committee", "School Committee Personnel Subcommittee: Food Service Workers",
                      "School Committee Personnel Subcommittee: Transportation Workers", "School Committee: Governance Workshop"]
    special = next(m for m in store.values() if m["date"] == "2026-10-20")
    assert special["special"] and special["title"] == "School Committee"
    assert status["left_out"] == ["ES SC Minutes 10_14_26.pdf"]
    # Files before `since`, in older school-year folders, and in folders not listed in the config are skipped.
    assert not any(u.endswith(("=old", "=too-old", "=other", "=y0")) for u in drive.urls)
    assert status["documents_added"] == 6


def test_site_shows_school_committee_documents(site_dir):
    pages = {p.parent.name: p.read_text() for p in (site_dir / "meetings").glob("2026-09-09-school-committee*/index.html")}
    page = pages["2026-09-09-school-committee"]
    assert "Minutes on the School Committee&#39;s Google Drive" in page
    assert "Agenda on the School Committee&#39;s Google Drive" in page
    assert "Gloucester Public Schools posted this as a scanned image." in page
    assert "known from documents Gloucester Public Schools posted" in page
    assert "Not listed on the city calendar" not in page
    about = (site_dir / "about" / "index.html").read_text()
    assert "School Committee agendas and minutes:" in about
    board = (site_dir / "meetings" / "boards" / "school-committee" / "index.html").read_text()
    assert "Gloucester Public Schools website" in board


def test_calendar_takes_over_a_meeting_first_recorded_from_drive(tmp_path):
    from fakes import FIXTURES, FakeCityClient

    from pipeline import civicplus, fetch_meetings
    config = load_config("gloucester")
    feed = (FIXTURES / "civicplus_calendar.xml").read_text().replace("6:00 PM Human Rights Commission", "6:00 PM School Committee")
    event = next(e for e in civicplus.parse_calendar_feed(feed.encode(), config["meetings"]["base_url"])
                 if e["body"] == "School Committee")
    settings = config["drive_meetings"]
    drive = MadeUpDrive({settings["agendas_folder"]: [("sca", "School Committee Agendas", True)],
                         "sca": [("early", f"SC Agenda {int(event['date'][5:7])}_{int(event['date'][8:])}_26.pdf")]})
    fetch_drive_meetings.run(config, drive, tmp_path, now=NOW)
    first = next(iter(json.loads((tmp_path / "meetings" / "meetings.json").read_text()).values()))
    assert first["source"] == "drive"

    fetch_meetings.run(config, FakeCityClient(feed=feed.encode()), tmp_path, now=NOW)
    store = json.loads((tmp_path / "meetings" / "meetings.json").read_text())
    same_day = [m for m in store.values() if m["date"] == event["date"] and m["body"] == "School Committee"]
    assert len(same_day) == 1
    meeting = same_day[0]
    assert meeting["id"] == event["id"] and meeting["slug"] == first["slug"]
    assert "source" not in meeting and meeting["agendas"][0]["id"] == "early"
    assert meeting["start_time"] == "18:00"
    assert not any(h["field"] in ("title", "start_time") for h in meeting.get("history", []))
