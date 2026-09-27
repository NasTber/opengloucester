"""Collect school district figures from the Massachusetts Department of
Elementary and Secondary Education (DESE) open data portal.

Three measures, each for the district and the state: the four-year graduation
rate, the chronic absenteeism rate, and the share of students in grades 3 to 8
meeting or exceeding expectations on MCAS in English and math.
Writes data/schools/schools.json.

Usage:
    python -m pipeline.fetch_schools [--town gloucester]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from pipeline.config import DATA_DIR, DEFAULT_TOWN, configured, load_config
from pipeline.fetch_meetings import save_json
from pipeline.http import FetchError, PoliteClient

STATE_CODE = "00000000"
YEARS_KEPT = 8

# Dataset id, the rows to use, and the column holding the value (a fraction).
MEASURES = {
    "graduation": {
        "dataset": "n2xa-p822", "value": "grad_pct",
        # Districts also get an "adjusted cohort" rate; the state does not.
        # Use the rate published for both so they compare.
        "where": "grad_rate_type = '4-Year Graduation Rate'",
    },
    "absenteeism": {
        "dataset": "ak6h-9k7x", "value": "pct_chron_abs_10",
        "where": "attend_period = 'End of Year'",
    },
    "mcas_ela": {
        "dataset": "i9w6-niyt", "value": "m_plus_e_pct",
        "where": "test_grade = 'ALL (03-08)' AND subject_code = 'ELA'",
    },
    "mcas_math": {
        "dataset": "i9w6-niyt", "value": "m_plus_e_pct",
        "where": "test_grade = 'ALL (03-08)' AND subject_code = 'MATH'",
    },
}


def query_url(portal: str, dataset: str, district: str, where: str, value: str) -> str:
    soql = (f"SELECT sy, org_code, {value} WHERE org_code IN ('{district}', '{STATE_CODE}') "
            f"AND stu_grp = 'All Students' AND {where} ORDER BY sy DESC LIMIT 200")
    return f"{portal}/resource/{dataset}.json?" + urlencode({"$query": soql})


def series(rows: list[dict], district: str, value: str) -> list[dict]:
    """[{year, town, state}] as percentages, oldest first, for years the district has."""
    by_year: dict[int, dict] = {}
    for row in rows:
        if row.get(value) in (None, ""):
            continue
        key = "town" if row["org_code"] == district else "state"
        by_year.setdefault(int(row["sy"]), {})[key] = round(float(row[value]) * 100, 1)
    return [{"year": y, "town": v["town"], "state": v.get("state")}
            for y, v in sorted(by_year.items()) if "town" in v][-YEARS_KEPT:]


def run(config: dict, client, data_dir: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(ZoneInfo(config["site"]["timezone"]))
    schools = config["schools"]
    portal, district = schools["portal"], schools["district_code"]
    measures = {}
    for name, m in MEASURES.items():
        rows = client.get(query_url(portal, m["dataset"], district, m["where"], m["value"])).json()
        years = series(rows, district, m["value"])
        if not years:
            raise FetchError(f"no {name} figures returned")
        measures[name] = {"dataset_url": f"{portal}/d/{m['dataset']}", "years": years}
    result = {
        "updated_at": now.isoformat(timespec="seconds"),
        "source": "Massachusetts Department of Elementary and Secondary Education",
        "district": schools["district_name"],
        "measures": measures,
    }
    save_json(data_dir / "schools" / "schools.json", result)
    return {name: m["years"][-1] for name, m in measures.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--town", default=DEFAULT_TOWN)
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    config = load_config(args.town)
    if not configured(config, "schools"):
        return 0
    client = PoliteClient(config["site"]["user_agent"], delay=1.0, timeout=60)
    try:
        print(json.dumps(run(config, client, args.data), indent=2))
    except (FetchError, ValueError, KeyError) as e:
        print(f"::error::School figures could not be fetched: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
