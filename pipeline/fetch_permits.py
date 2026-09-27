"""Collect building and demolition permits from the city's Data Hub.

The city publishes every Inspectional Services permit since 2017 as a CSV in a
public Google Drive folder, linked from its Data Hub page. The file name
changes with each export, so the newest file whose name starts with the
configured prefix is used.

Only building and demolition permits from the last few years are kept, with
address, date, type, status, estimated cost, and the description of work.
Names of applicants, owners, and contractors aren't needed by the site and
aren't stored.

Writes data/permits/permits.json. The city refreshes the file about weekly,
so the fetch is skipped when the saved file is less than a week old.

Usage:
    python -m pipeline.fetch_permits [--town gloucester] [--force]
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pipeline.config import DATA_DIR, DEFAULT_TOWN, configured, load_config
from pipeline.fetch_meetings import save_json
from pipeline.http import FetchError, PoliteClient

REFRESH_DAYS = 7
FOLDER_URL = "https://drive.google.com/embeddedfolderview?id={folder}"
FILE_URL = "https://drive.google.com/uc?export=download&id={id}"
FOLDER_ENTRY = re.compile(r'href="https://drive\.google\.com/file/d/([\w-]+)/[^"]*".*?<div class="flip-entry-title">(.*?)</div>', re.S)


def newest_file(folder_html: str, prefix: str) -> tuple[str, str]:
    """(file id, file name) of the newest export; names end in its date and time."""
    files = [(fid, html.unescape(name)) for fid, name in FOLDER_ENTRY.findall(folder_html)]
    matching = sorted((f for f in files if f[1].startswith(prefix)), key=lambda f: f[1])
    if not matching:
        raise FetchError(f"no file starting with {prefix!r} in the Data Hub folder")
    return matching[-1]


def parse_permits(text: str, types: list[str], since: str) -> list[dict]:
    permits = []
    for row in csv.DictReader(io.StringIO(text)):
        row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        submitted = row.get("Date Submitted", "")[:10]
        if row.get("Record Type") not in types or submitted < since:
            continue
        cost = row.get("IS:  Estimated Cost") or row.get("IS: Estimated Cost") or ""
        try:
            cost = round(float(cost.replace(",", "").replace("$", ""))) if cost else None
        except ValueError:
            cost = None
        permits.append({
            "id": row.get("Record #", ""),
            "type": row["Record Type"],
            "submitted": submitted,
            "address": row.get("Address", "").split(",")[0].strip(),
            "status": row.get("Record Status", ""),
            "cost": cost,
            "work": re.sub(r"\s+", " ", row.get("IS: Description of Work", "")),
            "use": row.get("IS: Property/Occupancy Type", ""),
        })
    return sorted(permits, key=lambda p: (p["submitted"], p["id"]), reverse=True)


def run(config: dict, client, data_dir: Path, now: datetime | None = None, force: bool = False) -> dict:
    now = now or datetime.now(ZoneInfo(config["site"]["timezone"]))
    path = data_dir / "permits" / "permits.json"
    if path.exists() and not force:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if now - datetime.fromisoformat(saved["updated_at"]) < timedelta(days=REFRESH_DAYS):
            return {"skipped": f"updated less than {REFRESH_DAYS} days ago"}
    cfg = config["permits"]
    folder = client.get(FOLDER_URL.format(folder=cfg["drive_folder"])).text
    file_id, file_name = newest_file(folder, cfg["file_prefix"])
    response = client.get(FILE_URL.format(id=file_id))
    if b"Record Type" not in response.content[:2000]:
        raise FetchError(f"{file_name} did not download as a CSV")
    since = f"{now.year - cfg['years']}-{now.month:02d}-01"
    permits = parse_permits(response.content.decode("utf-8-sig", errors="replace"), cfg["types"], since)
    if not permits:
        raise FetchError(f"{file_name} has no {', '.join(cfg['types'])} since {since}")
    save_json(path, {
        "updated_at": now.isoformat(timespec="seconds"),
        "source": "City of Gloucester Inspectional Services, Data Hub",
        "source_url": cfg["page_url"],
        "file": file_name,
        "since": since,
        "permits": permits,
    })
    return {"file": file_name, "permits": len(permits)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--town", default=DEFAULT_TOWN)
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    parser.add_argument("--force", action="store_true", help="fetch even if the saved file is recent")
    args = parser.parse_args()
    config = load_config(args.town)
    if not configured(config, "permits"):
        return 0
    client = PoliteClient(config["site"]["user_agent"], delay=2.0, timeout=180.0)
    try:
        print(json.dumps(run(config, client, args.data, force=args.force), indent=2))
    except FetchError as e:
        print(f"::error::Building permits could not be fetched: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
