"""Collect public 311 requests for the town from SeeClickFix.

Daily run:
  1. Page through all open requests (Open311).
  2. Page through every request submitted in the last few weeks, any status.
  3. Requests we had as open that are no longer open are marked for a check.
  4. Look up exact acknowledged/closed times (APIv2) for new and changed
     requests, then for older requests still missing them, up to a limit.
  5. Tag each request with its ward and precinct.

Backfill (once): python -m pipeline.fetch_311 --backfill 2024-01
  walks month by month from that date and records every request. Exact
  times are then filled in gradually by the daily runs.

Writes data/311/requests.json and data/311/status.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pipeline import seeclickfix
from pipeline.config import DATA_DIR, load_config
from pipeline.geo import PrecinctLookup
from pipeline.http import FetchError, PoliteClient

PAGE_SIZE = 100
MAX_PAGES = 60


def store_dir(data_dir: Path) -> Path:
    return data_dir / "311"


def load_store(data_dir: Path) -> dict:
    path = store_dir(data_dir) / "requests.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


class SeeClickFix:
    def __init__(self, config: dict, client):
        self.source = config["seeclickfix"]
        self.client = client

    def requests_url(self, **params) -> str:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.source['open311_base'].rstrip('/')}/{self.source['organization_id']}/requests.json?{query}"

    def list_requests(self, **params) -> list[dict]:
        items = []
        for page in range(1, MAX_PAGES + 1):
            batch = self.client.get(self.requests_url(page_size=PAGE_SIZE, page=page, **params)).json()
            items.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        return [seeclickfix.parse_open311(i) for i in items]

    def issue(self, issue_id: str) -> dict:
        url = f"{self.source['api_base'].rstrip('/')}/issues/{issue_id}"
        return seeclickfix.parse_issue(self.client.get(url).json())


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def merge_listing(store: dict, items: list[dict], stamp: str) -> tuple[int, int]:
    """Add or update requests from an Open311 listing. Returns (new, changed)."""
    new = changed = 0
    for item in items:
        record = store.get(item["id"])
        if record is None:
            store[item["id"]] = {**item, "first_seen": stamp, "last_seen": stamp, "detail": None}
            new += 1
            continue
        if item["updated_at"] != record.get("updated_at") or item["status"] != record.get("status"):
            changed += 1
        record.update(item)
        record["last_seen"] = stamp
    return new, changed


def needs_detail(record: dict) -> bool:
    detail = record.get("detail")
    if record.get("removed"):
        return False
    if not detail:
        return True
    # Recheck when Open311 shows an update after our last check.
    return (record.get("updated_at") or "") > detail.get("checked_at", "")


def fill_details(api: SeeClickFix, store: dict, stamp: str, limit: int, deadline: float,
                 checkpoint=None) -> tuple[int, list[str]]:
    changed = sorted((r for r in store.values() if r.get("detail") and needs_detail(r)), key=lambda r: r["created_at"] or "", reverse=True)
    missing = sorted((r for r in store.values() if not r.get("detail") and needs_detail(r)), key=lambda r: r["created_at"] or "", reverse=True)
    # Newest missing first so the scorecard's recent window fills in quickly.
    queue = changed + missing
    done, errors = 0, []
    for record in queue[:limit]:
        if time.monotonic() > deadline:
            break
        try:
            detail = api.issue(record["id"])
        except FetchError as e:
            if e.status in (403, 404, 410):
                # Deleted or made private: keep the record but stop checking it.
                record["removed"] = True
                record["removed_at"] = stamp
                continue
            errors.append(str(e))
            if len(errors) >= 5:
                break
            continue
        detail["checked_at"] = stamp
        record["detail"] = detail
        # The detailed status is newer than Open311's open/closed.
        record["status"] = "open" if detail["status"] in ("open", "acknowledged") else "closed"
        done += 1
        if checkpoint and done % 100 == 0:
            checkpoint()
    return done, errors


def tag_wards(store: dict, lookup: PrecinctLookup) -> int:
    tagged = 0
    for record in store.values():
        if "ward" in record:
            continue
        place = lookup.find(record.get("lat"), record.get("lng"))
        record["ward"] = place["ward"] if place else None
        record["precinct"] = place["district"] if place else None
        tagged += 1
    return tagged


def month_windows(start: date, end: date):
    current = start.replace(day=1)
    while current <= end:
        nxt = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield current, nxt
        current = nxt


def run(config: dict, client, data_dir: Path, now: datetime | None = None,
        backfill_from: date | None = None, detail_limit: int | None = None,
        time_budget: float | None = None) -> dict:
    source = config["seeclickfix"]
    tz = ZoneInfo(config["site"]["timezone"])
    now = now or datetime.now(tz)
    stamp = iso(now)
    started = time.monotonic()
    deadline = started + (time_budget if time_budget is not None else source.get("time_budget_seconds", 2400))
    api = SeeClickFix(config, client)
    store = load_store(data_dir)
    summary = {"updated_at": stamp}

    if backfill_from:
        total_new = 0
        for start, end in month_windows(backfill_from, now.date()):
            items = api.list_requests(start_date=f"{start.isoformat()}T00:00:00-05:00", end_date=f"{end.isoformat()}T00:00:00-05:00")
            new, _ = merge_listing(store, items, stamp)
            total_new += new
            print(f"{start:%Y-%m}: {len(items)} requests, {new} new", flush=True)
        summary["backfill_new"] = total_new

    previously_open = {r["id"] for r in store.values() if r.get("status") == "open" and not r.get("removed")}
    open_items = api.list_requests(status="open")
    since = (now - timedelta(days=source.get("recent_days", 21))).isoformat(timespec="seconds")
    recent_items = api.list_requests(start_date=since)
    new_open, changed_open = merge_listing(store, open_items, stamp)
    new_recent, changed_recent = merge_listing(store, recent_items, stamp)

    # Anything that dropped off the open list has been closed (or removed); check it.
    open_now = {i["id"] for i in open_items}
    for request_id in previously_open - open_now:
        record = store[request_id]
        if record.get("status") == "open":
            record["status"] = "closed"
            if record.get("detail"):
                record["detail"]["checked_at"] = ""  # force a recheck

    limit = detail_limit if detail_limit is not None else source.get("max_details_per_run", 400)
    lookup = PrecinctLookup(data_dir / "static" / source["precincts_file"])
    tagged = tag_wards(store, lookup)

    def checkpoint():
        save_json(store_dir(data_dir) / "requests.json", store)
        print(f"{time.monotonic() - started:.0f}s: saved progress", flush=True)

    checkpoint()
    details_done, errors = fill_details(api, store, stamp, limit, deadline, checkpoint)

    save_json(store_dir(data_dir) / "requests.json", store)
    summary.update({
        "requests_total": len(store),
        "open_listed": len(open_items),
        "new": new_open + new_recent,
        "changed": changed_open + changed_recent,
        "details_fetched": details_done,
        "details_missing": sum(1 for r in store.values() if not r.get("detail") and not r.get("removed")),
        "ward_tagged": tagged,
        "requests_made": getattr(client, "request_count", None),
        "errors": errors,
    })
    save_json(store_dir(data_dir) / "status.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--town", default="gloucester")
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    parser.add_argument("--backfill", metavar="YYYY-MM", help="record every request since this month")
    parser.add_argument("--details", type=int, help="max exact-time lookups this run")
    parser.add_argument("--time-budget", type=float, help="seconds to spend on lookups")
    args = parser.parse_args()
    config = load_config(args.town)
    client = PoliteClient(config["site"]["user_agent"], delay=config["seeclickfix"].get("request_delay", 3.2))
    backfill = date.fromisoformat(args.backfill + "-01") if args.backfill else None
    try:
        summary = run(config, client, args.data, backfill_from=backfill, detail_limit=args.details, time_budget=args.time_budget)
    except FetchError as e:
        print(f"::error::SeeClickFix could not be reached: {e}")
        return 1
    print(json.dumps(summary, indent=2))
    for error in summary["errors"]:
        print(f"::warning::{error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
