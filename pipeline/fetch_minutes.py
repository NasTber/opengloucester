"""Collect posted meeting minutes from the city's Archive Center.

One request to the Archive Center's main page lists the newest documents in
every board's collection. Minutes dated on or after the configured start
date are downloaded, saved under data/meetings/minutes/, and attached to the
matching meeting in data/meetings/meetings.json. A meeting that was never on
the calendar feed (usually because it predates this site) gets a record
built from its minutes.

Usage:
    python -m pipeline.fetch_minutes [--town gloucester]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pipeline import civicplus
from pipeline.config import DATA_DIR, DEFAULT_TOWN, configured, load_config
from pipeline.fetch_meetings import (
    load_store, meetings_dir, normalize_body, pdf_has_text, record_change, save_json, slugify, unique_slug,
)
from pipeline.http import FetchError, PoliteClient


def body_for(collection_name: str, config: dict) -> str:
    body, _ = civicplus.collection_body(collection_name)
    body = normalize_body(body, config["archive"].get("aliases", {}))
    return normalize_body(body, config["meetings"].get("aliases", {}))


def find_meeting(store: dict, body: str, date: str, title: str) -> dict | None:
    """The recorded meeting of this body on this date. A subcommittee's minutes
    match a subcommittee meeting when there is one."""
    candidates = [m for m in store.values() if m["date"] == date and slugify(m["body"]) == slugify(body)]
    if len(candidates) > 1:
        sub = "subcommittee" in title.lower()
        preferred = [m for m in candidates if ("subcommittee" in m.get("title", "").lower()) == sub]
        candidates = preferred or candidates
    return candidates[0] if candidates else None


def new_meeting_from_minutes(store: dict, item: dict, body: str, stamp: str) -> dict:
    title = body + (" Subcommittee Meeting" if "subcommittee" in item["title"].lower() else "")
    meeting = {
        "id": f"archive-{item['id']}",
        "source": "archive",
        "first_seen": stamp,
        "last_seen": stamp,
        "slug": unique_slug(f"{item['date']}-{slugify(title)}", store),
        "date": item["date"],
        "body": body,
        "title": title,
        "status": "scheduled",
        "special": "special" in item["title"].lower(),
        "listed": True,
        "start_time": None,
        "end_time": None,
        "location": "",
    }
    store[meeting["id"]] = meeting
    return meeting


def save_document(client, item: dict, folder: Path, stamp: str) -> dict:
    response = client.get(item["url"])
    content = response.content
    if not content.startswith(b"%PDF"):
        raise FetchError(f"{item['url']}: not a PDF")
    disposition = response.headers.get("content-disposition", "")
    filename = re.search(r'filename="?([^";]+)', disposition)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{item['id']}.pdf").write_bytes(content)
    return {
        "id": item["id"],
        "title": item["title"],
        "source_url": item["url"],
        "file": f"{item['id']}.pdf",
        "original_filename": filename.group(1).strip() if filename else None,
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
        "has_text": pdf_has_text(content),
        "fetched_at": stamp,
    }


def run(config: dict, client, data_dir: Path, now: datetime | None = None) -> dict:
    tz = ZoneInfo(config["site"]["timezone"])
    now = now or datetime.now(tz)
    stamp = now.isoformat(timespec="seconds")
    settings = config["archive"]
    base_url = config["meetings"]["base_url"]
    since = settings["minutes_since"]

    page = client.get(base_url.rstrip("/") + "/" + settings["index"].lstrip("/"))
    collections = civicplus.parse_archive_index(page.text, base_url)
    store = load_store(data_dir)
    known = {doc["id"] for m in store.values() for doc in m.get("minutes", [])}

    added, created, errors = 0, 0, []
    for collection in collections:
        if civicplus.collection_body(collection["name"])[1] != "minutes":
            continue
        body = body_for(collection["name"], config)
        for item in collection["items"]:
            if not item["date"] or item["date"] < since or item["id"] in known:
                continue
            try:
                doc = save_document(client, item, meetings_dir(data_dir) / "minutes", stamp)
            except FetchError as e:
                errors.append(str(e))
                continue
            meeting = find_meeting(store, body, item["date"], item["title"])
            if meeting is None:
                meeting = new_meeting_from_minutes(store, item, body, stamp)
                created += 1
            minutes = meeting.setdefault("minutes", [])
            if minutes:
                record_change(meeting, "minutes", minutes[-1]["id"], doc["id"], stamp)
            minutes.append(doc)
            known.add(item["id"])
            added += 1

    save_json(meetings_dir(data_dir) / "meetings.json", store)
    return {"minutes_added": added, "meetings_created": created, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--town", default=DEFAULT_TOWN)
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    config = load_config(args.town)
    if not configured(config, "archive"):
        return 0
    client = PoliteClient(config["site"]["user_agent"], delay=config["meetings"].get("request_delay", 3.0))
    try:
        summary = run(config, client, args.data)
    except FetchError as e:
        print(f"::error::Archive Center could not be fetched: {e}")
        return 1
    print(json.dumps(summary, indent=2))
    for error in summary["errors"]:
        print(f"::warning::{error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
