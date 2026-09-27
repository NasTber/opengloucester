"""Tests for the 311 pipeline, run offline against saved SeeClickFix responses."""

import json
import shutil
from datetime import datetime

import pytest

from conftest import FETCHED_AT, REAL_DATA_DIR
from fakes import FakeSeeClickFix
from pipeline import compute_311, fetch_311
from pipeline.config import load_config
from pipeline.geo import PrecinctLookup


@pytest.fixture
def config():
    return load_config("gloucester")


@pytest.fixture
def data(tmp_path):
    shutil.copytree(REAL_DATA_DIR / "static", tmp_path / "static")
    return tmp_path


def load(data_dir):
    return json.loads((data_dir / "311" / "requests.json").read_text())


def test_precinct_lookup():
    lookup = PrecinctLookup(REAL_DATA_DIR / "static" / "gloucester-precincts-2022.geojson")
    city_hall = lookup.find(42.6146, -70.6634)  # 9 Dale Ave
    assert city_hall is not None and city_hall["ward"] in {"1", "2", "3", "4", "5"}
    assert lookup.find(42.3601, -71.0589) is None  # Boston
    assert lookup.find(None, None) is None


def test_run_records_requests_without_personal_details(config, data):
    fetch_311.run(config, FakeSeeClickFix(), data, now=FETCHED_AT, detail_limit=0)
    store = load(data)
    assert len(store) == 114
    record = next(iter(store.values()))
    assert {"id", "category", "created_at", "status", "lat", "lng", "ward", "precinct"} <= record.keys()
    assert "description" not in record and "media_url" not in record
    assert all(r["ward"] for r in store.values()), "every fixture request is inside the city"


def test_run_fills_exact_times_up_to_limit(config, data):
    summary = fetch_311.run(config, FakeSeeClickFix(), data, now=FETCHED_AT, detail_limit=10)
    assert summary["details_fetched"] == 10
    assert summary["details_missing"] == 104
    fetch_311.run(config, FakeSeeClickFix(), data, now=FETCHED_AT, detail_limit=500)
    assert all(r["detail"] for r in load(data).values())


def test_request_leaving_open_list_is_rechecked(config, data):
    first = FakeSeeClickFix()
    fetch_311.run(config, first, data, now=FETCHED_AT, detail_limit=500)
    closed_id = str(first.open_items[0]["service_request_id"])
    # The city closed it: it leaves the open list, and the lookup reports it closed.
    now_closed = dict(first.open_items[0], status="closed")
    later = FakeSeeClickFix(open_items=first.open_items[1:], window_items=first.window_items + [now_closed])
    fetch_311.run(config, later, data, now=FETCHED_AT.replace(day=27), detail_limit=500)
    assert any(u.endswith(f"/issues/{closed_id}") for u in later.urls), "closed request should be looked up again"
    assert load(data)[closed_id]["status"] == "closed"


def test_removed_request_is_kept_out(config, data):
    source = FakeSeeClickFix()
    gone = str(source.open_items[0]["service_request_id"])
    fetch_311.run(config, FakeSeeClickFix(missing_ids={gone}), data, now=FETCHED_AT, detail_limit=500)
    assert load(data)[gone]["removed"] is True
    scorecard = compute_311.compute(config, data, now=FETCHED_AT)
    assert scorecard["overall"]["received"] == 113


def test_close_time_prefers_exact_then_archive_then_update():
    base = {"status": "closed", "updated_at": "2026-09-05T00:00:00-04:00"}
    assert compute_311.closed_time({**base, "detail": {"closed_at": "2026-09-02T00:00:00-04:00", "updated_at": "2026-09-03T00:00:00-04:00"}}).day == 2
    assert compute_311.closed_time({**base, "detail": {"closed_at": None, "updated_at": "2026-09-03T00:00:00-04:00"}}).day == 3
    assert compute_311.closed_time({**base, "detail": None}).day == 5
    assert compute_311.closed_time({**base, "status": "open"}) is None


def test_small_samples_are_suppressed():
    assert compute_311.stats([1.0, 2.0])["median"] is None
    s = compute_311.stats([1.0, 2.0, 3.0, 4.0, 5.0])
    assert s["median"] == 3.0 and s["n"] == 5


def test_scorecard(config, data):
    fetch_311.run(config, FakeSeeClickFix(), data, now=FETCHED_AT, detail_limit=500)
    sc = compute_311.compute(config, data, now=FETCHED_AT)
    o = sc["overall"]
    assert o["received"] == o["open"] + o["closed"] == 114
    assert o["checked"] == 114
    assert 0 < o["acknowledged"] < o["checked"]
    assert o["time_to_close"]["median"] is not None
    assert sum(b["count"] for b in sc["backlog"]["buckets"]) == sc["backlog"]["open"] == 100
    assert sum(w["received"] for w in sc["by_ward"]) == 114
    assert all(w["per_1000_residents"] for w in sc["by_ward"] if w["ward"] != "outside")
    assert sc["backlog"]["oldest"][0]["url"].startswith("https://seeclickfix.com/issues/")


def test_month_windows_cover_every_month():
    from datetime import date
    windows = list(fetch_311.month_windows(date(2024, 11, 1), date(2025, 2, 10)))
    assert [w[0].isoformat() for w in windows] == ["2024-11-01", "2024-12-01", "2025-01-01", "2025-02-01"]
    assert windows[-1][1].isoformat() == "2025-03-01"


def test_block_address_hides_house_numbers():
    from pipeline.seeclickfix import block_address
    assert block_address("229 Main St Gloucester, Massachusetts, 01930") == "200 block of Main St"
    assert block_address("30 Reservoir Rd Gloucester MA 01930, United States") == "1–99 Reservoir Rd"
    assert block_address("470-580 Western Ave") == "400 block of Western Ave"
    assert block_address("124r Centennial Avenue") == "100 block of Centennial Avenue"
    assert block_address("Bray St & Salt Marsh Ln") == "Bray St & Salt Marsh Ln"
    assert block_address("01930") == ""


def request(id, created, lat=42.6150, lng=-70.6600, status="closed", closed=None, category="Pothole"):
    return {"id": id, "category": category, "address": "12 Main St", "ward": "2", "status": status,
            "created_at": created, "updated_at": closed or created, "lat": lat, "lng": lng,
            "detail": {"closed_at": closed} if closed else None}


def test_repeat_locations():
    records = [
        request("1", "2026-05-01T09:00:00-04:00", closed="2026-05-03T09:00:00-04:00"),
        # 20 meters away, after the first was closed: a repeat.
        request("2", "2026-05-20T09:00:00-04:00", lat=42.61518, status="open"),
        # Same spot, but a different category.
        request("3", "2026-05-21T09:00:00-04:00", category="Sidewalk Issue", closed="2026-05-22T09:00:00-04:00"),
        # Far away.
        request("4", "2026-05-22T09:00:00-04:00", lat=42.62, closed="2026-05-23T09:00:00-04:00"),
        # Same spot, but more than 60 days after the last request there.
        request("5", "2026-09-01T09:00:00-04:00", closed="2026-09-02T09:00:00-04:00"),
        # Two reports of the same problem before any close: not a repeat.
        request("6", "2026-06-01T09:00:00-04:00", lat=42.63, status="open"),
        request("7", "2026-06-02T09:00:00-04:00", lat=42.63, status="open"),
    ]
    places = compute_311.repeat_locations(records, 50, 60, "https://seeclickfix.com/issues")
    assert len(places) == 1
    p = places[0]
    assert [r["id"] for r in p["requests"]] == ["1", "2"]
    assert p["reports"] == 2 and p["again_after_close"] == 1 and p["open"] == 1
    assert p["address"] == "1–99 Main St"
    assert p["requests"][-1]["url"] == "https://seeclickfix.com/issues/2"


def test_private_categories_stay_off_maps(config):
    exclude = config["seeclickfix"]["repeats"]["exclude"]
    records = [request("1", "2026-09-01T09:00:00-04:00", category=c)
               for c in ("Pothole", "Health Department (Housing) - Internal", "Private Property Issue",
                         "Animal Issues", "Police Department (Non-Emergency)")]
    records.append(request("2", "2026-09-01T09:00:00-04:00", lat=None))
    assert [r["category"] for r in compute_311.mappable(records, exclude)] == ["Pothole"]


def test_scorecard_has_recent_open_and_repeats(config, data):
    fetch_311.run(config, FakeSeeClickFix(), data, now=FETCHED_AT, detail_limit=500)
    sc = compute_311.compute(config, data, now=FETCHED_AT)
    recent = sc["recent_open"]["requests"]
    assert recent and all(r["created_at"] >= "2026-08-27" for r in recent)
    assert [r["created_at"] for r in recent] == sorted((r["created_at"] for r in recent), reverse=True)
    assert all(not r["address"] or not r["address"][0].isdigit() or " block of " in r["address"] or r["address"].startswith("1–99")
               for r in recent)
    assert "places" in sc["repeats"]
