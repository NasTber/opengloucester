"""Collect the local unemployment rate from the Bureau of Labor Statistics,
Local Area Unemployment Statistics (LAUS).

Uses the BLS API (with BLS_API_KEY when set). If the API refuses, falls back
to the bulk Massachusetts data file. Writes data/labor/unemployment.json.

Rates for cities are not seasonally adjusted, so compare a month with the
same month a year earlier.

Usage:
    python -m pipeline.fetch_labor [--town gloucester]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pipeline.config import DATA_DIR, DEFAULT_TOWN, configured, load_config
from pipeline.fetch_meetings import save_json
from pipeline.http import FetchError, PoliteClient

API_V1 = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
API_V2 = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
BULK = "https://download.bls.gov/pub/time.series/la/la.data.28.Massachusetts"
MONTHS_KEPT = 37


def from_api(client, series: list[str], now: datetime) -> dict[str, list[dict]]:
    key = os.environ.get("BLS_API_KEY")
    start = now.year - 3
    if key:
        response = client.session.post(API_V2, json={
            "seriesid": series, "startyear": str(start), "endyear": str(now.year), "registrationkey": key,
        }, timeout=60)
        payload = response.json()
    else:
        payload = {"status": "", "Results": {"series": []}}
        for s in series:
            one = client.get(API_V1 + s).json()
            if one.get("status") != "REQUEST_SUCCEEDED":
                raise FetchError(f"BLS API: {one.get('message')}")
            payload["Results"]["series"] += one["Results"]["series"]
        payload["status"] = "REQUEST_SUCCEEDED"
    if payload.get("status") != "REQUEST_SUCCEEDED":
        raise FetchError(f"BLS API: {payload.get('message')}")
    out = {}
    for s in payload["Results"]["series"]:
        out[s["seriesID"]] = [
            {"year": int(d["year"]), "month": int(d["period"][1:]), "rate": float(d["value"]),
             "preliminary": any(f.get("code") == "P" for f in d.get("footnotes", []) if f)}
            for d in s["data"] if d["period"].startswith("M") and d["period"] != "M13" and d["value"] not in ("-", "")
        ]
    return out


def from_bulk(client, series: list[str]) -> dict[str, list[dict]]:
    text = client.get(BULK).text
    out = {s: [] for s in series}
    for line in text.splitlines()[1:]:
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) < 4 or parts[0] not in out or not parts[2].startswith("M") or parts[2] == "M13":
            continue
        if parts[3] in ("-", ""):
            continue
        out[parts[0]].append({"year": int(parts[1]), "month": int(parts[2][1:]), "rate": float(parts[3]),
                              "preliminary": "P" in (parts[4] if len(parts) > 4 else "")})
    return out


def run(config: dict, client, data_dir: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(ZoneInfo(config["site"]["timezone"]))
    labor = config["labor"]
    series = [labor["local_series"], labor["state_series"]]
    try:
        data = from_api(client, series, now)
        method = "api"
    except (FetchError, ValueError, KeyError) as e:
        print(f"BLS API unavailable ({e}); using the bulk file.")
        data = from_bulk(client, series)
        method = "bulk"
    local = sorted(data[labor["local_series"]], key=lambda d: (d["year"], d["month"]))[-MONTHS_KEPT:]
    if not local:
        raise FetchError("no unemployment figures returned")
    state = {(d["year"], d["month"]): d["rate"] for d in data[labor["state_series"]]}
    for d in local:
        d["state_rate"] = state.get((d["year"], d["month"]))
    result = {
        "updated_at": now.isoformat(timespec="seconds"),
        "source": "U.S. Bureau of Labor Statistics, Local Area Unemployment Statistics",
        "source_url": f"https://data.bls.gov/timeseries/{labor['local_series']}",
        "series_id": labor["local_series"],
        "seasonally_adjusted": False,
        "method": method,
        "months": local,
    }
    save_json(data_dir / "labor" / "unemployment.json", result)
    return {"latest": local[-1], "method": method}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--town", default=DEFAULT_TOWN)
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    config = load_config(args.town)
    if not configured(config, "labor"):
        return 0
    client = PoliteClient(config["site"]["user_agent"], delay=2.0, timeout=120)
    try:
        print(json.dumps(run(config, client, args.data), indent=2))
    except FetchError as e:
        print(f"::error::Unemployment could not be fetched: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
