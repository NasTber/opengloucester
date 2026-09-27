"""Check that every data source is still updating.

Most update steps are allowed to fail without stopping the daily run, so a
source that breaks (a moved file, a changed page, a server that stops
answering) would otherwise go stale quietly. This compares each source's last
update with how often it should update, and also looks for agendas still
waiting for a summary (a sign the AI summary step is failing).

In the daily workflow it runs last and fails the run when anything is stale,
so GitHub emails the site's owner; the site is still built and deployed. The
About page shows the same table.

Usage:
    python -m pipeline.freshness [--town gloucester]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pipeline import summarize
from pipeline.config import DATA_DIR, load_config


def last_update(data_dir: Path, source: dict) -> datetime | None:
    path = data_dir / source["file"]
    if not path.exists():
        return None
    stamp = json.loads(path.read_text(encoding="utf-8")).get(source.get("field", "updated_at"))
    return datetime.fromisoformat(stamp) if stamp else None


def waiting_summaries(config: dict, data_dir: Path, now: datetime, grace_days: float) -> list[str]:
    """Agendas and minutes posted more than grace_days ago with no summary at all.

    A summary from an older prompt still shows on the site while its newer
    version waits, so only documents with nothing to show are counted.
    """
    model = config.get("summaries", {}).get("model")
    if not model:
        return []
    cutoff = now - timedelta(days=grace_days)
    return [f"{doc_kind} for {meeting['body']}, {meeting['date']}"
            for doc_kind, meeting, doc in summarize.pending_documents(data_dir, now.date().isoformat(), model)
            if datetime.fromisoformat(doc["fetched_at"]) < cutoff
            and not summarize.cached(data_dir, doc["sha256"], model, doc_kind, current=False)]


def check(config: dict, data_dir: Path, now: datetime | None = None) -> list[dict]:
    """One row per source: its label, last update, allowed age, and whether it is stale."""
    now = now or datetime.now(ZoneInfo(config["site"]["timezone"]))
    fresh = config["freshness"]
    rows = []
    for source in fresh["sources"]:
        updated = last_update(data_dir, source)
        age = (now - updated).total_seconds() / 86400 if updated else None
        rows.append({"label": source["label"], "updated_at": updated.isoformat() if updated else None,
                     "max_days": source["max_days"], "stale": age is None or age > source["max_days"]})
    waiting = waiting_summaries(config, data_dir, now, fresh["summary_grace_days"])
    rows.append({"label": "Meeting summaries", "updated_at": None, "max_days": fresh["summary_grace_days"],
                 "stale": bool(waiting), "waiting": waiting})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--town", default="gloucester")
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    rows = check(load_config(args.town), args.data)
    lines = ["| Source | Last updated | Allowed age | Status |", "|---|---|---|---|"]
    for r in rows:
        when = r["updated_at"][:16].replace("T", " ") if r["updated_at"] else "–"
        extra = f" ({len(r['waiting'])} waiting)" if r.get("waiting") else ""
        lines.append(f"| {r['label']} | {when} | {r['max_days']} days | {'STALE' if r['stale'] else 'OK'}{extra} |")
        if r["stale"]:
            detail = "; ".join(r["waiting"][:5]) if r.get("waiting") else f"last updated {when}"
            print(f"::error::{r['label']} is not updating: {detail}")
    report = "\n".join(lines)
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("## Data freshness\n\n" + report + "\n")
    return 1 if any(r["stale"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
