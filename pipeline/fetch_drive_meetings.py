"""Collect agendas and minutes posted in public Google Drive folders.

Some public bodies post their agendas and minutes in Google Drive instead of on
the city website. In Gloucester that is the School Committee and its
subcommittees: the school district keeps one Drive folder of agendas and one of
minutes, each holding a folder per committee. The current school year's files
sit loose in each committee's folder, and earlier years are in school-year
folders ("2025-2026 School Year").

The meeting date and any variation come from the file name, such as
"SC Agenda 9_9_26.pdf", "Special SC Agenda 7_20_26", "SC Agenda REVISED 6_10_26"
or "Personnel Agenda (Food Service Workers) 3_16_26.pdf". Documents are saved
alongside the city's under data/meetings/ and attached to the meeting on the
city calendar when there is one; otherwise the meeting is recorded from its
documents. Executive session minutes and joint meetings with other boards
are left out; their names are listed in the run's summary.

Agendas are often posted only days before a meeting, so upcoming meetings
come from the district's published meeting schedule ("School Committee -
October 14th and 28th, 2026; ..."), for the next few weeks. Documents attach to
those meetings when they are posted. A scheduled meeting that drops off the
schedule before it happens is marked as no longer listed, not deleted.

Usage:
    python -m pipeline.fetch_drive_meetings [--town gloucester]
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pipeline.config import DATA_DIR, DEFAULT_TOWN, configured, load_config
from pipeline.fetch_meetings import load_store, meetings_dir, pdf_has_text, record_change, save_json, slugify, unique_slug
from pipeline.http import FetchError, PoliteClient

FOLDER_URL = "https://drive.google.com/embeddedfolderview?id={folder}"
FILE_URL = "https://drive.google.com/uc?export=download&id={id}"
VIEW_URL = "https://drive.google.com/file/d/{id}/view"
ENTRY = re.compile(r'<div class="flip-entry" id="entry-([\w-]+)".*?<a href="https://drive\.google\.com/([a-z]+)/'
                   r'.*?<div class="flip-entry-title">(.*?)</div>', re.S)
# "9_9_26", "10_08_25", "6_17_2026", at the end of the name.
DATE_IN_NAME = re.compile(r"(\d{1,2})_(\d{1,2})_(\d{4}|\d{2})\s*(?:\.pdf)?\s*$", re.I)
SCHOOL_YEAR = re.compile(r"\b(\d{4})-(\d{4})\b")
# Words in a file name that describe the document, not which meeting it is.
DOCUMENT_WORDS = re.compile(r"\b(?:agenda|minutes|revised|amended|special|online|sub-?committee)\b", re.I)
LEFT_OUT = re.compile(r"\b(?:ES|executive session|joint)\b", re.I)
MONTHS = {name: n for n, name in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                              "september", "october", "november", "december"], 1)}
# "School Committee - " or "Building and Finance Subcommittee - " starts a committee's dates.
SCHEDULE_ENTRY = re.compile(r"([A-Z][A-Za-z& ]*?(?:Committee|Subcommittee)) ?- ")


def list_folder(page: str) -> list[dict]:
    """Entries of a public Drive folder page: files and subfolders."""
    return [{"id": fid, "folder": kind == "drive", "name": html.unescape(name).strip()}
            for fid, kind, name in ENTRY.findall(page)]


def parse_name(name: str, abbreviations: list[str]) -> dict | None:
    """Meeting date and details from a file name, or None if it has no date.

    'SC Governance Workshop Agenda 4_8_26.pdf' -> 2026-04-08, variant
    'Governance Workshop'. abbreviations are the committees' short names in
    file names ('SC', 'B&F'), removed before the variant is read."""
    text = re.sub(r"\s+", " ", name).strip()
    found = DATE_IN_NAME.search(text)
    if not found:
        return None
    month, day, year = (int(n) for n in found.groups())
    try:
        when = date(year + 2000 if year < 100 else year, month, day)
    except ValueError:
        return None
    words = text[:found.start()]
    rest = DOCUMENT_WORDS.sub(" ", words)
    for short in sorted(abbreviations, key=len, reverse=True):
        rest = re.sub(rf"(?<!\w){re.escape(short)}(?!\w)", " ", rest, flags=re.I)
    return {
        "date": when.isoformat(),
        "revised": bool(re.search(r"\b(?:revised|amended)\b", words, re.I)),
        "special": bool(re.search(r"\bspecial\b", words, re.I)),
        "variant": re.sub(r"\s+", " ", rest).strip(" -()"),
        "left_out": bool(LEFT_OUT.search(words)),
    }


def committee_files(client, folder_id: str, since: str) -> list[dict]:
    """Files in a committee's folder, and in its school-year folders from the
    year that includes `since` on."""
    files = []
    for entry in list_folder(client.get(FOLDER_URL.format(folder=folder_id)).text):
        if not entry["folder"]:
            files.append(entry)
            continue
        year = SCHOOL_YEAR.search(entry["name"])
        if year and int(year.group(2)) >= int(since[:4]):
            files += [e for e in list_folder(client.get(FOLDER_URL.format(folder=entry["id"])).text) if not e["folder"]]
    return files


def body_for(folder_name: str, bodies: dict) -> str | None:
    """'Program Subcommittee Agendas' -> the configured body for 'Program Subcommittee'.
    'and' and '&' are the same ('Building and Finance Subcommittee')."""
    return next((body for prefix, body in bodies.items() if slugify(folder_name).startswith(slugify(prefix))), None)


def parse_schedule(page: str) -> list[tuple[str, str]]:
    """(committee, date) pairs from the district's meeting schedule page:
    'School Committee - September 9th and 23rd, 2026; October 14th and 28th, 2026'.
    The page's formatting splits numbers and their endings ('2 3rd', '202 6',
    '9t h'), so those are rejoined first."""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S | re.I)
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text)))
    text = re.sub(r"(?<=\d) (?=\d)", "", text)
    text = re.sub(r"(\d) ?(?:s ?t|n ?d|r ?d|t ?h)\b", r"\1", text)
    entries = list(SCHEDULE_ENTRY.finditer(text))
    found = []
    for i, entry in enumerate(entries):
        # A committee's dates run to the next committee, or to the page footer ("... | Phone").
        end = entries[i + 1].start() if i + 1 < len(entries) else len(text)
        month, pending = None, []
        for token in re.findall(r"[A-Za-z]+|\d+", text[entry.end():end].split("|")[0]):
            if token.lower() in MONTHS:
                month = MONTHS[token.lower()]
            elif token.isdigit() and len(token) == 4:
                for m, d in pending:
                    try:
                        found.append((entry.group(1).strip(), date(int(token), m, d).isoformat()))
                    except ValueError:
                        pass
                pending = []
            elif token.isdigit() and month:
                pending.append((month, int(token)))
    return found


def update_schedule(store: dict, page: str, settings: dict, today: date, stamp: str) -> int:
    """Record scheduled meetings from `since` to a few weeks ahead. Returns how many are new."""
    ahead = (today + timedelta(days=settings.get("schedule_days_ahead", 45))).isoformat()
    scheduled = {(body, day) for name, day in parse_schedule(page)
                 if (body := body_for(name, settings["bodies"])) and settings["since"] <= day <= ahead}
    added = 0
    for body, day in sorted(scheduled):
        meeting = find_meeting(store, body, day, "")
        if meeting is None:
            new_meeting(store, f"schedule-{slugify(body)}-{day}", body, {"date": day, "variant": "", "special": False},
                        settings, stamp, source_url=settings["schedule_url"])
            added += 1
        elif not meeting.get("listed", True) and meeting["id"].startswith("schedule-"):
            record_change(meeting, "listed", False, True, stamp)
            meeting["listed"] = True
    # An upcoming meeting known only from the schedule that has left it was probably moved or cancelled.
    if scheduled:
        for m in store.values():
            if (m["id"].startswith("schedule-") and m.get("listed", True) and m["date"] >= today.isoformat()
                    and (m["body"], m["date"]) not in scheduled and not m.get("agendas") and not m.get("minutes")):
                record_change(m, "listed", True, False, stamp)
                m["listed"] = False
    return added


def find_meeting(store: dict, body: str, day: str, variant: str) -> dict | None:
    return next((m for m in store.values()
                 if m["date"] == day and slugify(m["body"]) == slugify(body) and m.get("variant", "") == variant), None)


def new_meeting(store: dict, meeting_id: str, body: str, info: dict, settings: dict, stamp: str,
                source_url: str | None = None) -> dict:
    title = f"{body}: {info['variant']}" if info["variant"] else body
    meeting = {
        "id": meeting_id,
        "source": "drive",
        "source_url": source_url or settings["page_url"],
        "source_name": settings["source_name"],
        "first_seen": stamp,
        "last_seen": stamp,
        "slug": unique_slug(f"{info['date']}-{slugify(title)}", store),
        "date": info["date"],
        "body": body,
        "title": title,
        "variant": info["variant"],
        "status": "scheduled",
        "special": info["special"],
        "listed": True,
        "start_time": None,
        "end_time": None,
        "location": "",
    }
    store[meeting["id"]] = meeting
    return meeting


def save_document(client, file: dict, folder: Path, settings: dict, stamp: str) -> dict:
    content = client.get(FILE_URL.format(id=file["id"])).content
    if not content.startswith(b"%PDF"):
        raise FetchError(f"{file['name']}: not a PDF")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{file['id']}.pdf").write_bytes(content)
    return {
        "id": file["id"],
        "title": file["name"],
        "source_url": VIEW_URL.format(id=file["id"]),
        "posted_by": settings["source_name"],
        "posted_on": settings["posted_on"],
        "file": f"{file['id']}.pdf",
        "original_filename": file["name"],
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
        "has_text": pdf_has_text(content),
        "fetched_at": stamp,
    }


def run(config: dict, client, data_dir: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(ZoneInfo(config["site"]["timezone"]))
    stamp = now.isoformat(timespec="seconds")
    settings = config["drive_meetings"]
    since = settings["since"]
    store = load_store(data_dir)
    known = {doc["id"] for m in store.values() for field in ("agendas", "minutes") for doc in m.get(field, [])}
    scheduled = (update_schedule(store, client.get(settings["schedule_url"]).text, settings, now.date(), stamp)
                 if settings.get("schedule_url") else 0)

    found = []
    for kind, root in (("agendas", settings["agendas_folder"]), ("minutes", settings["minutes_folder"])):
        for committee in list_folder(client.get(FOLDER_URL.format(folder=root)).text):
            body = body_for(committee["name"], settings["bodies"])
            if committee["folder"] and body:
                found += [(kind, body, f) for f in committee_files(client, committee["id"], since)]

    added, created, left_out, unreadable, errors = 0, 0, [], [], []
    documents = []
    for kind, body, file in found:
        info = parse_name(file["name"], settings.get("abbreviations", []))
        if info is None:
            unreadable.append(file["name"])
        elif info["left_out"]:
            left_out.append(file["name"])
        elif info["date"] >= since and file["id"] not in known:
            documents.append((kind, body, file, info))
    # Originals before revisions, so a meeting's latest version is its last.
    for kind, body, file, info in sorted(documents, key=lambda d: (d[3]["date"], d[3]["revised"], d[2]["name"])):
        try:
            doc = save_document(client, file, meetings_dir(data_dir) / kind, settings, stamp)
        except FetchError as e:
            errors.append(str(e))
            continue
        meeting = find_meeting(store, body, info["date"], info["variant"])
        if meeting is None:
            meeting = new_meeting(store, f"drive-{file['id']}", body, info, settings, stamp)
            created += 1
        docs = meeting.setdefault(kind, [])
        if docs:
            record_change(meeting, "agenda" if kind == "agendas" else "minutes", docs[-1]["id"], doc["id"], stamp)
        docs.append(doc)
        known.add(file["id"])
        added += 1

    save_json(meetings_dir(data_dir) / "meetings.json", store)
    status_path = meetings_dir(data_dir) / "drive_status.json"
    reported = json.loads(status_path.read_text(encoding="utf-8")).get("unreadable_names", []) if status_path.exists() else []
    status = {
        "updated_at": stamp,
        "files_listed": len(found),
        "documents_added": added,
        "meetings_created": created,
        "scheduled_meetings_added": scheduled,
        "left_out": sorted(left_out),
        # File names without a readable date ("B & F Agenda 6_17_2.pdf"): check these by hand.
        "unreadable_names": sorted(unreadable),
        "new_unreadable_names": sorted(set(unreadable) - set(reported)),
        "errors": errors,
    }
    save_json(status_path, status)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--town", default=DEFAULT_TOWN)
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    config = load_config(args.town)
    if not configured(config, "drive_meetings"):
        return 0
    client = PoliteClient(config["site"]["user_agent"], delay=1.0, timeout=60)
    try:
        status = run(config, client, args.data)
    except FetchError as e:
        print(f"::error::Google Drive folders could not be read: {e}")
        return 1
    print(json.dumps(status, indent=2))
    for name in status["new_unreadable_names"]:
        print(f"::warning::No meeting date in the file name {name!r}")
    for error in status["errors"]:
        print(f"::warning::{error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
