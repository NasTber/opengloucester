"""Street lookup: street-name matching, the permit fetch, and the street index."""

import json

from conftest import FETCHED_AT
from fakes import FakePermits
from pipeline import fetch_permits
from pipeline.config import load_config
from pipeline.streets import addresses_in, street_keys, street_name


def test_street_names_match_across_sources():
    # The city, agendas, and SeeClickFix write the same street differently.
    assert street_keys("33 MAPLEWOOD AV, GLOUCESTER", "Gloucester") == ["MAPLEWOOD AVE"]
    assert street_keys("33 Maplewood Avenue", "Gloucester") == ["MAPLEWOOD AVE"]
    assert street_keys("33 Maplewood Ave Gloucester MA 01930, United States", "Gloucester") == ["MAPLEWOOD AVE"]
    assert street_keys("62, 62R, 64 Eastern Point Boulevard", "Gloucester") == ["EASTERN POINT BLVD"]
    assert street_keys("Railroad Ave & Washington St", "Gloucester") == ["RAILROAD AVE", "WASHINGTON ST"]
    assert street_keys("12 Main St Unit 3", "Gloucester") == ["MAIN ST"]
    assert street_keys("Ymca", "Gloucester") == [] and street_keys("", "Gloucester") == []
    assert street_name("EASTERN POINT BLVD") == "Eastern Point Boulevard"


def test_addresses_in_agenda_text():
    text = "38 Pleasant Street (Map 14, Lot 17) - door; 62-66 Eastern Point Blvd; posted 2026 15 September Road"
    assert addresses_in(text) == ["38 Pleasant Street", "62-66 Eastern Point Blvd"]


def test_permits_newest_export_building_and_demolition_only(tmp_path):
    config = load_config("gloucester")
    client = FakePermits()
    summary = fetch_permits.run(config, client, tmp_path, now=FETCHED_AT)
    assert summary["file"].endswith("2026-09-22 09_55_31.csv")
    assert any("id=NEWFILE" in u for u in client.urls)
    saved = json.loads((tmp_path / "permits" / "permits.json").read_text())
    permits = saved["permits"]
    # Electrical and short-form permits are left out, and so is anything older than the window.
    assert [p["id"] for p in permits] == ["BLD-26-589", "DEMO-26-12", "BLD-26-400"]
    assert permits[1]["cost"] == 12500 and permits[2]["cost"] is None
    # Names of applicants, owners, and contractors aren't stored.
    assert not any("Applicant" in json.dumps(p) or "Owner" in json.dumps(p) or "Contractor" in json.dumps(p)
                   for p in permits)
    # A second run within the week doesn't download again.
    assert "skipped" in fetch_permits.run(config, FakePermits(), tmp_path, now=FETCHED_AT)


def test_street_page_and_index(site_dir):
    page = (site_dir / "streets" / "index.html").read_text()
    assert "What's happening on your street" in page and "/static/js/streets.js?v=" in page
    assert 'data-index="/streets/streets.json?v=' in page
    index = json.loads((site_dir / "streets" / "streets.json").read_text())
    assert index["suffixes"]["AV"] == "AVE"
    pleasant = index["streets"]["PLEASANT ST"]
    assert pleasant["name"] == "Pleasant Street"
    assert pleasant["permits"][0]["address"] == "38 Pleasant Street"
    assert "Owner" not in json.dumps(index)
    home = (site_dir / "index.html").read_text()
    assert 'action="/streets/"' in home
