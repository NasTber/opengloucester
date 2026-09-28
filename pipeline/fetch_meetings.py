"""Collect public meetings from the city calendar and archive their agendas.

Writes data/meetings/meetings.json (one record per meeting, never deleted) and
saves each posted agenda PDF under data/meetings/agendas/ (or in the town's
bucket; see pipeline/documents.py). Changes the city
makes after posting (new time, new place, revised agenda, cancellation) are
recorded in each meeting's history so they stay visible.

Usage:
    python -m pipeline.fetch_meetings [--town gloucester]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from pipeline import civicplus
from pipeline.config import DATA_DIR, DEFAULT_TOWN, configured, load_config
from pipeline.documents import open_documents
from pipeline.http import FetchError, PoliteClient

# Fields whose changes are recorded in a meeting's history.
TRACKED_FIELDS = ("title", "date", "start_time", "location_name", "address", "status", "listed")


def meetings_dir(data_dir: Path) -> Path:
    return data_dir / "meetings"


def load_store(data_dir: Path) -> dict:
    path = meetings_dir(data_dir) / "meetings.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def normalize_body(body: str, aliases: dict) -> str:
    for name, alias in aliases.items():
        if body.lower() == name.lower():
            return alias
    return body


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower().replace("&", "and")).strip("-")


def unique_slug(slug: str, store: dict) -> str:
    """Page slugs are fixed when a meeting is first recorded so links never break."""
    taken = {m.get("slug") for m in store.values()}
    candidate, n = slug, 2
    while candidate in taken:
        candidate, n = f"{slug}-{n}", n + 1
    return candidate


def pdf_has_text(content: bytes) -> bool | None:
    """True if the PDF has a text layer; False for a scanned image; None if unreadable."""
    try:
        reader = PdfReader(io.BytesIO(content))
        return any((page.extract_text() or "").strip() for page in reader.pages[:3])
    except Exception:
        return None


def record_change(meeting: dict, field: str, old, new, at: str) -> None:
    meeting.setdefault("history", []).append({"at": at, "field": field, "old": old, "new": new})


def merge(meeting: dict, updates: dict, at: str, track: bool) -> None:
    for field, new in updates.items():
        old = meeting.get(field)
        if field in meeting and old == new:
            continue
        if track and field in TRACKED_FIELDS and field in meeting:
            record_change(meeting, field, old, new, at)
        meeting[field] = new


def fetch_agenda(client, meeting: dict, details: dict, storage, now: str) -> None:
    agenda_id = details.get("agenda_id")
    if not agenda_id:
        return
    agendas = meeting.setdefault("agendas", [])
    if any(a["id"] == agenda_id for a in agendas):
        return
    response = client.get(details["agenda_url"])
    content = response.content
    if not content.startswith(b"%PDF"):
        raise FetchError(f"agenda {agenda_id} is not a PDF")
    disposition = response.headers.get("content-disposition", "")
    filename = re.search(r'filename="?([^";]+)', disposition)
    name = f"{agenda_id}.pdf"
    storage.put("agendas", name, content)
    if agendas:
        record_change(meeting, "agenda", agendas[-1]["id"], agenda_id, now)
    agendas.append({
        "id": agenda_id,
        "source_url": details["agenda_url"],
        "file": name,
        "original_filename": filename.group(1).strip() if filename else None,
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
        "has_text": pdf_has_text(content),
        "fetched_at": now,
    })


def adopt_drive_meeting(store: dict, event: dict) -> dict | None:
    """A meeting first recorded from documents in Google Drive (see
    fetch_drive_meetings) that has now appeared on the calendar. It becomes the
    calendar's record, keeping its page address and documents; the title and
    time are the calendar's, not changes to record."""
    for key, m in list(store.items()):
        if (m.get("source") == "drive" and not m.get("variant") and m["date"] == event["date"]
                and slugify(m["body"]) == slugify(event["body"])):
            del store[key]
            for field in ("source", "source_name", "variant", "title", "start_time", "end_time"):
                m.pop(field, None)
            m["id"] = event["id"]
            store[event["id"]] = m
            return m
    return None


def run(config: dict, client, data_dir: Path, now: datetime | None = None) -> dict:
    """Update the meetings store. Returns a summary for logging."""
    tz = ZoneInfo(config["site"]["timezone"])
    now = now or datetime.now(tz)
    stamp = now.isoformat(timespec="seconds")
    today = now.date()
    source = config["meetings"]
    base_url = source["base_url"]
    include = re.compile(source["include_pattern"], re.I)
    aliases = source.get("aliases", {})

    store = load_store(data_dir)
    storage = open_documents(config, data_dir)
    feed = client.get(base_url.rstrip("/") + "/" + source["calendar_feed"].lstrip("/"))
    events = [e for e in civicplus.parse_calendar_feed(feed.content, base_url) if include.search(e["title"])]

    seen = set()
    new_count = 0
    for event in events:
        seen.add(event["id"])
        event["body"] = normalize_body(event["body"], aliases)
        event.pop("raw_title", None)
        meeting = store.get(event["id"]) or adopt_drive_meeting(store, event)
        if meeting is None:
            meeting = store[event["id"]] = {
                "id": event["id"],
                "first_seen": stamp,
                "slug": unique_slug(f"{event['date']}-{slugify(event['body'])}", store),
            }
            new_count += 1
        merge(meeting, {**event, "listed": True}, stamp, track=True)
        meeting["last_seen"] = stamp

    # A future meeting that drops out of the feed while other meetings on or
    # after its date are still listed was most likely removed by the city.
    latest_listed = max((e["date"] for e in events), default=None)
    for meeting in store.values():
        if meeting["id"] in seen or not meeting.get("listed", True):
            continue
        if meeting["date"] >= today.isoformat() and latest_listed and meeting["date"] <= latest_listed:
            merge(meeting, {"listed": False}, stamp, track=True)

    # Event pages hold the agenda link and full location. Only upcoming
    # meetings are re-checked, so a run makes a few dozen requests at most.
    errors = []
    pages = 0
    for meeting in sorted(store.values(), key=lambda m: (m["date"], m["id"])):
        if meeting["id"] not in seen or meeting["date"] < today.isoformat():
            continue
        if pages >= source.get("max_event_pages", 40):
            break
        try:
            page = client.get(meeting["source_url"])
            pages += 1
            details = civicplus.parse_event_page(page.text, base_url)
            merge(meeting, {k: details[k] for k in ("location_name", "address", "remote_url") if details.get(k)}, stamp, track=True)
            fetch_agenda(client, meeting, details, storage, stamp)
            meeting["checked_at"] = stamp
        except FetchError as e:
            errors.append(str(e))

    save_json(meetings_dir(data_dir) / "meetings.json", store)
    status = {
        "updated_at": stamp,
        "listed_in_feed": len(events),
        "new_meetings": new_count,
        "event_pages_checked": pages,
        "requests": getattr(client, "request_count", None),
        "errors": errors,
    }
    save_json(meetings_dir(data_dir) / "status.json", status)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--town", default=DEFAULT_TOWN)
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    config = load_config(args.town)
    if not configured(config, "meetings"):
        return 0
    client = PoliteClient(config["site"]["user_agent"], delay=config["meetings"].get("request_delay", 3.0))
    try:
        status = run(config, client, args.data)
    except FetchError as e:
        # Keep the existing data; the site shows when it was last updated.
        print(f"::error::Meetings feed could not be fetched: {e}")
        return 1
    print(json.dumps(status, indent=2))
    for error in status["errors"]:
        print(f"::warning::{error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
