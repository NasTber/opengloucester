"""Compute 311 scorecard metrics from data/311/requests.json.

Writes data/311/scorecard.json, which the site reads. Definitions are
documented on the site's 311 methodology page; keep the two in sync.

Usage:
    python -m pipeline.compute_311 [--town gloucester]
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median, quantiles
from zoneinfo import ZoneInfo

from pipeline.config import DATA_DIR, load_config
from pipeline.fetch_311 import load_store, save_json, store_dir, tag_wards
from pipeline.fetch_meetings import slugify
from pipeline.geo import PrecinctLookup
from pipeline.seeclickfix import street_address

# Statistics from fewer requests than this are not shown.
MIN_SAMPLE = 5
BACKLOG_BUCKETS = [(7, "Under 1 week"), (30, "1 week to 1 month"), (90, "1 to 3 months"),
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
        # Closed without ever being acknowledged, among requests looked up.
        "closed_unacknowledged": sum(1 for r in with_detail if r["status"] == "closed" and not acknowledged_time(r)),
    }


def top_category(records: list[dict]) -> dict:
    counts = defaultdict(int)
    for r in records:
        counts[r["category"]] += 1
    name, count = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    return {"category": name, "count": count}


def open_at(record: dict, when: datetime) -> bool:
    """Whether a request was open at a given moment, as far as the data shows."""
    if parse(record["created_at"]) > when:
        return False
    closed = closed_time(record)
    return closed is None or closed > when


def oldest_open(records: list[dict], now: datetime, link_base: str, n: int = 10) -> list[dict]:
    """Longest-open requests."""
    return [{
        "id": r["id"], "category": r["category"], "address": street_address(r["address"]), "ward": r.get("ward"),
        "created_at": r["created_at"], "age_days": round(days_between(parse(r["created_at"]), now), 1),
        "url": f"{link_base}/{r['id']}",
    } for r in sorted(records, key=lambda r: r["created_at"])[:n]]


def month_counts(records: list[dict], months: list[str]) -> list[dict]:
    counts = defaultdict(int)
    for r in records:
        counts[r["created_at"][:7]] += 1
    return [{"month": m, "received": counts.get(m, 0)} for m in months]


def backlog(open_records: list[dict], now: datetime, link_base: str) -> dict:
    buckets = [{"label": label, "max_days": limit, "count": 0} for limit, label in BACKLOG_BUCKETS]
    for r in open_records:
        age = days_between(parse(r["created_at"]), now)
        for b in buckets:
            if b["max_days"] is None or age < b["max_days"]:
                b["count"] += 1
                break
    return {
        "open": len(open_records),
        "median_age_days": round(median([days_between(parse(r["created_at"]), now) for r in open_records]), 1) if open_records else None,
        "buckets": buckets,
        "oldest": oldest_open(open_records, now, link_base),
    }


def distance_m(a: dict, b: dict) -> float:
    """Distance in meters between two requests' map points (haversine)."""
    lat1, lng1, lat2, lng2 = map(math.radians, (a["lat"], a["lng"], b["lat"], b["lng"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def mappable(records: list[dict]) -> list[dict]:
    """Requests with a map location, for the map and the repeat list."""
    return [r for r in records if r.get("lat") is not None and r.get("lng") is not None]


def point(r: dict) -> dict:
    # Four decimal places: about 10 meters, enough to place a marker.
    return {"lat": round(r["lat"], 4), "lng": round(r["lng"], 4)}


def repeat_locations(records: list[dict], radius_m: float, window_days: int, link_base: str) -> list[dict]:
    """Places where the same kind of problem was reported more than once.

    Requests in the same category are grouped when they are within radius_m of
    each other and submitted within window_days of each other, in a chain. A
    place is listed when a request there came in after an earlier one at the
    same place had been closed.
    """
    parent = {r["id"]: r["id"] for r in records}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_category = defaultdict(list)
    for r in records:
        by_category[r["category"]].append(r)
    window = timedelta(days=window_days)
    for rs in by_category.values():
        rs.sort(key=lambda r: r["created_at"])
        times = [parse(r["created_at"]) for r in rs]
        for i, a in enumerate(rs):
            for j in range(i + 1, len(rs)):
                if times[j] - times[i] > window:
                    break
                if distance_m(a, rs[j]) <= radius_m:
                    parent[find(a["id"])] = find(rs[j]["id"])

    groups = defaultdict(list)
    for r in records:
        groups[find(r["id"])].append(r)
    places = []
    for rs in groups.values():
        if len(rs) < 2:
            continue
        rs.sort(key=lambda r: r["created_at"])
        closes = [closed_time(r) for r in rs]
        again = sum(1 for i, r in enumerate(rs)
                    if any(c and c < parse(r["created_at"]) for c in closes[:i]))
        if not again:
            continue
        address = Counter(street_address(r["address"]) for r in rs).most_common(1)[0][0]
        ward = Counter(r.get("ward") for r in rs).most_common(1)[0][0]
        places.append({
            "category": rs[0]["category"], "slug": slugify(rs[0]["category"]),
            "address": address, "ward": ward,
            "reports": len(rs), "again_after_close": again,
            "open": sum(1 for r in rs if r["status"] == "open"),
            "first": rs[0]["created_at"][:10], "last": rs[-1]["created_at"][:10],
            "lat": round(sum(r["lat"] for r in rs) / len(rs), 4),
            "lng": round(sum(r["lng"] for r in rs) / len(rs), 4),
            "requests": [{"id": r["id"], "created_at": r["created_at"][:10], "status": r["status"],
                          "url": f"{link_base}/{r['id']}"} for r in rs],
        })
    places.sort(key=lambda p: (-p["again_after_close"], -p["reports"], p["address"]))
    return places


def recent_open(records: list[dict], now: datetime, days: int, link_base: str) -> list[dict]:
    """Open requests submitted in the last few days, newest first. Map points where known."""
    since = now - timedelta(days=days)
    rs = [r for r in records if r["status"] == "open" and parse(r["created_at"]) >= since]
    return [{
        "id": r["id"], "category": r["category"], "address": street_address(r["address"]),
        "ward": r.get("ward"), "created_at": r["created_at"], **(point(r) if mappable([r]) else {}),
        "url": f"{link_base}/{r['id']}",
    } for r in sorted(rs, key=lambda r: r["created_at"], reverse=True)]


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
    rep = config["seeclickfix"]["repeats"]
    backlog_now = backlog(open_records, now, link_base)
    backlog_now["open_week_ago"] = sum(open_at(r, now - timedelta(days=7)) for r in records)

    # Detail for the per-category and per-ward pages: past 12 months.
    last_months = [f"{(now.year * 12 + now.month - 1 - i) // 12}-{(now.month - 1 - i) % 12 + 1:02d}" for i in range(11, -1, -1)]
    categories = []
    for c, rs in sorted(by_category.items(), key=lambda x: (-len(x[1]), x[0])):
        wards = defaultdict(list)
        for r in rs:
            wards[r.get("ward") or "outside"].append(r)
        categories.append({
            "category": c, "slug": slugify(c), **summarize(rs),
            "monthly": month_counts(rs, last_months),
            "by_ward": [{"ward": w, **summarize(ws)} for w, ws in sorted(wards.items(), key=lambda x: (x[0] == "outside", x[0]))],
            "oldest": oldest_open([r for r in open_records if r["category"] == c], now, link_base),
        })
    wards_detail = []
    for w, rs in sorted(by_ward.items(), key=lambda x: (x[0] == "outside", x[0])):
        cats = defaultdict(list)
        for r in rs:
            cats[r["category"]].append(r)
        wards_detail.append({
            "ward": w, **summarize(rs),
            "monthly": month_counts(rs, last_months),
            "by_category": sorted(({"category": c, "slug": slugify(c), **summarize(cs)} for c, cs in cats.items()),
                                  key=lambda x: (-x["received"], x["category"])),
            "oldest": oldest_open([r for r in open_records if (r.get("ward") or "outside") == w], now, link_base),
        })

    on_map_window = [r for r in mappable(records) if parse(r["created_at"]) >= window_start]

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "window": {"start": window_start.date().isoformat(), "end": now.date().isoformat(), "days": 365},
        "data_since": earliest[:10] if earliest else None,
        "requests_recorded": len(records),
        "min_sample": MIN_SAMPLE,
        "overall": summarize(in_window),
        "backlog": backlog_now,
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
            {"month": m, **summarize(rs), "top_category": top_category(rs),
             # Requests from the last two months have had little time to close.
             "recent": m >= (now - timedelta(days=60)).strftime("%Y-%m")}
            for m, rs in sorted(monthly.items())
        ],
        "categories": categories,
        "wards": wards_detail,
        "repeats": {"radius_m": rep["radius_m"], "window_days": rep["window_days"],
                    "places": repeat_locations(on_map_window, rep["radius_m"], rep["window_days"], link_base)},
        "recent_open": {"days": rep["recent_open_days"],
                        "requests": recent_open(records, now, rep["recent_open_days"], link_base)},
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
