"""Compute 311 scorecard metrics from data/311/requests.json.

Writes data/311/scorecard.json, which the site reads. Definitions are
documented on the site's 311 methodology page; keep the two in sync.

Usage:
    python -m pipeline.compute_311 [--town gloucester]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median, quantiles
from zoneinfo import ZoneInfo

from pipeline.config import DATA_DIR, load_config
from pipeline.fetch_311 import load_store, save_json, store_dir, tag_wards
from pipeline.geo import PrecinctLookup

# Statistics from fewer requests than this are not shown.
MIN_SAMPLE = 5
BACKLOG_BUCKETS = [(7, "Under 1 week"), (30, "1 to 4 weeks"), (90, "1 to 3 months"),
                   (365, "3 to 12 months"), (None, "Over 1 year")]


def parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def days_between(start: datetime | None, end: datetime | None) -> float | None:
    if not start or not end:
        return None
    return max((end - start).total_seconds() / 86400, 0.0)


def closed_time(record: dict) -> datetime | None:
    """When a closed request was closed.

    Uses the exact close time when known. Requests archived without a recorded
    close time use the archive time. Requests not yet looked up use the
    Open311 last-update time, which matches the close time in most cases.
    """
    if record.get("status") != "closed":
        return None
    detail = record.get("detail") or {}
    return parse(detail.get("closed_at") or detail.get("updated_at") or record.get("updated_at"))


def acknowledged_time(record: dict) -> datetime | None:
    return parse((record.get("detail") or {}).get("acknowledged_at"))


def stats(values: list[float]) -> dict:
    n = len(values)
    if n < MIN_SAMPLE:
        return {"n": n, "median": None, "p90": None}
    p90 = quantiles(values, n=10, method="inclusive")[-1]
    return {"n": n, "median": round(median(values), 2), "p90": round(p90, 2)}


def summarize(records: list[dict]) -> dict:
    closed = [r for r in records if r["status"] == "closed"]
    with_detail = [r for r in records if r.get("detail")]
    acked = [r for r in with_detail if acknowledged_time(r)]
    return {
        "received": len(records),
        "open": len(records) - len(closed),
        "closed": len(closed),
        "time_to_close": stats([days_between(parse(r["created_at"]), closed_time(r)) for r in closed]),
        "time_to_acknowledge": stats([days_between(parse(r["created_at"]), acknowledged_time(r)) for r in acked]),
        "checked": len(with_detail),
        "acknowledged": len(acked),
    }


def backlog(open_records: list[dict], now: datetime, link_base: str) -> dict:
    buckets = [{"label": label, "max_days": limit, "count": 0} for limit, label in BACKLOG_BUCKETS]
    for r in open_records:
        age = days_between(parse(r["created_at"]), now)
        for b in buckets:
            if b["max_days"] is None or age < b["max_days"]:
                b["count"] += 1
                break
    oldest = sorted(open_records, key=lambda r: r["created_at"])[:10]
    return {
        "open": len(open_records),
        "median_age_days": round(median([days_between(parse(r["created_at"]), now) for r in open_records]), 1) if open_records else None,
        "buckets": buckets,
        "oldest": [{
            "id": r["id"], "category": r["category"], "address": r["address"], "ward": r.get("ward"),
            "created_at": r["created_at"], "age_days": round(days_between(parse(r["created_at"]), now), 1),
            "url": f"{link_base}/{r['id']}",
        } for r in oldest],
    }


def compute(config: dict, data_dir: Path, now: datetime | None = None) -> dict:
    tz = ZoneInfo(config["site"]["timezone"])
    now = now or datetime.now(tz)
    store = load_store(data_dir)
    records = [r for r in store.values() if not r.get("removed") and r.get("created_at")]
    tag_wards(store, PrecinctLookup(data_dir / "static" / config["seeclickfix"]["precincts_file"]))
    precincts = json.loads((data_dir / "static" / config["seeclickfix"]["precincts_file"]).read_text())
    population = defaultdict(int)
    for f in precincts["features"]:
        population[f["properties"]["ward"]] += f["properties"]["population_2020"]

    window_start = now - timedelta(days=365)
    in_window = [r for r in records if parse(r["created_at"]) >= window_start]
    link_base = "https://seeclickfix.com/issues"

    by_category = defaultdict(list)
    by_ward = defaultdict(list)
    for r in in_window:
        by_category[r["category"]].append(r)
        by_ward[r.get("ward") or "outside"].append(r)

    monthly = defaultdict(list)
    first_month = (now.replace(day=1) - timedelta(days=700)).replace(day=1)
    for r in records:
        created = parse(r["created_at"])
        if created >= first_month:
            monthly[created.strftime("%Y-%m")].append(r)

    open_records = [r for r in records if r["status"] == "open"]
    earliest = min((r["created_at"] for r in records), default=None)

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "window": {"start": window_start.date().isoformat(), "end": now.date().isoformat(), "days": 365},
        "data_since": earliest[:10] if earliest else None,
        "requests_recorded": len(records),
        "min_sample": MIN_SAMPLE,
        "overall": summarize(in_window),
        "backlog": backlog(open_records, now, link_base),
        "by_category": sorted(
            ({"category": c, **summarize(rs)} for c, rs in by_category.items()),
            key=lambda x: (-x["received"], x["category"]),
        ),
        "by_ward": [
            {"ward": w, "population_2020": population.get(w), **summarize(rs),
             "per_1000_residents": round(len(rs) / population[w] * 1000, 1) if population.get(w) else None}
            for w, rs in sorted(by_ward.items(), key=lambda x: (x[0] == "outside", x[0]))
        ],
        "monthly": [
            {"month": m, **summarize(rs),
             # Requests from the last two months have had little time to close.
             "recent": m >= (now - timedelta(days=60)).strftime("%Y-%m")}
            for m, rs in sorted(monthly.items())
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--town", default="gloucester")
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    scorecard = compute(load_config(args.town), args.data)
    save_json(store_dir(args.data) / "scorecard.json", scorecard)
    print(f"Scorecard: {scorecard['overall']['received']} requests in the last year, {scorecard['backlog']['open']} open")


if __name__ == "__main__":
    main()
