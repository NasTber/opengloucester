"""Collect housing figures for the town.

- New homes permitted each year, from the Census Bureau Building Permits Survey.
- Home value, rent, and rent burden, from the Census Bureau's American Community
  Survey (5-year estimates, with margins of error), through Census Reporter.
- The share of year-round homes on the state's Subsidized Housing Inventory
  (Chapter 40B), from the state's PDF.
- Residential parcels by type, from the Mass. Division of Local Services.

Writes data/housing/housing.json. Each source is fetched separately; if one
fails, its last saved figures are kept. The figures change a few times a year,
so the fetch is skipped when the saved file is less than a week old.

Usage:
    python -m pipeline.fetch_housing [--town gloucester] [--force]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import requests
from pypdf import PdfReader

from pipeline.config import DATA_DIR, load_config
from pipeline.fetch_budget import REPORT_URL, rows as dls_rows
from pipeline.fetch_finance import dls_get
from pipeline.fetch_meetings import save_json
from pipeline.http import FetchError, PoliteClient

REFRESH_DAYS = 7
PERMIT_YEARS = 10
BPS_URL = "https://www2.census.gov/econ/bps/Place"
BPS_PAGE = "https://www.census.gov/construction/bps/"
CENSUS_REPORTER = "https://api.censusreporter.org/1.0/data/show/latest"
ACS_TABLES = ["B25002", "B25003", "B25004", "B25064", "B25070", "B25077"]
SHI_PAGE = "https://www.mass.gov/info-details/subsidized-housing-inventory-shi"
PARCELS_REPORT = "PropertyTaxInformation.LA4.Parcel_counts_vals"
# DLS parcel columns shown on the page, with plain names. Parcels, not homes:
# a condominium building has one parcel per unit, an apartment building one in all.
PARCEL_TYPES = {"Single Family 101": "Single-family homes", "Condominiums 102": "Condominiums",
                "Two Family 104": "Two-family homes", "Three Family 105": "Three-family homes",
                "Apartment 111-125": "Apartment buildings (4 or more units)",
                "Miscellaneous Residential 103,109": "Other residential"}


# ---- Building permits ----

def bps_url(config: dict, name: str) -> str:
    h = config["housing"]
    return f"{BPS_URL}/{quote(h['bps_region'])}/{h['bps_prefix']}{name}.txt"


def parse_bps(text: str, state: str, place: str) -> dict | None:
    """The town's row of a Building Permits Survey place file.

    Columns 17-28 are Census estimates (reported months plus imputed ones), in
    four groups of buildings, units, and value: 1 unit, 2 units, 3-4, and 5+.
    """
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 29 or row[1].strip() != state or row[5].strip() != place:
            continue
        units = [int(row[18]), int(row[21]), int(row[24]), int(row[27])]
        return {
            "units": sum(units),
            "by_size": dict(zip(["1 unit", "2 units", "3-4 units", "5+ units"], units)),
            "months_reported": int(row[15]),
        }
    return None


def permits(client, config: dict, now: datetime) -> dict:
    h = config["housing"]
    years = []
    for year in range(now.year - 1, now.year - PERMIT_YEARS - 2, -1):
        try:
            found = parse_bps(client.get(bps_url(config, f"{year}a")).text, h["bps_state"], h["bps_place"])
        except FetchError as e:
            if e.status == 404 and year == now.year - 1:
                continue  # last year's annual file is published in the spring
            raise
        if found:
            years.append({"year": year, **found, "estimated": found["months_reported"] < 12})
        if len(years) >= PERMIT_YEARS:
            break
    # Year to date: the newest monthly cumulative file for this year.
    ytd = None
    for month in range(now.month - 1, 0, -1):
        try:
            text = client.get(bps_url(config, f"{now.year % 100:02d}{month:02d}y")).text
        except FetchError as e:
            if e.status == 404:
                continue
            raise
        found = parse_bps(text, h["bps_state"], h["bps_place"])
        if found:
            ytd = {"year": now.year, "through_month": month, "units": found["units"],
                   "estimated": found["months_reported"] < month}
        break
    if not years:
        raise FetchError("no building permit figures found")
    return {"years": sorted(years, key=lambda y: y["year"]), "year_to_date": ytd}


# ---- American Community Survey ----

def moe_sum(*moes: float) -> float:
    return math.sqrt(sum(m * m for m in moes))


def moe_share(part: float, part_moe: float, whole: float, whole_moe: float) -> float:
    """Margin of error of a proportion (Census Bureau formula), in percentage points."""
    p = part / whole
    inside = part_moe ** 2 - p * p * whole_moe ** 2
    if inside < 0:  # the Bureau's fallback when the proportion formula fails
        inside = part_moe ** 2 + p * p * whole_moe ** 2
    return math.sqrt(inside) / whole * 100


def acs_place(tables: dict) -> dict:
    def est(table, cell):
        return tables[table]["estimate"][f"{table}{cell:03d}"]

    def err(table, cell):
        return tables[table]["error"][f"{table}{cell:03d}"]

    # Rent burden: B25070 cells 7-10 are 30% or more of income; 11 is "not computed".
    renters = est("B25070", 1) - est("B25070", 11)
    renters_moe = moe_sum(err("B25070", 1), err("B25070", 11))
    over_30 = sum(est("B25070", c) for c in range(7, 11))
    over_30_moe = moe_sum(*(err("B25070", c) for c in range(7, 11)))
    over_50 = est("B25070", 10)
    return {
        "median_home_value": {"value": round(est("B25077", 1)), "moe": round(err("B25077", 1))},
        "median_rent": {"value": round(est("B25064", 1)), "moe": round(err("B25064", 1))},
        "rent_30_plus": {"value": round(over_30 / renters * 100, 1),
                         "moe": round(moe_share(over_30, over_30_moe, renters, renters_moe), 1)},
        "rent_50_plus": {"value": round(over_50 / renters * 100, 1),
                         "moe": round(moe_share(over_50, err("B25070", 10), renters, renters_moe), 1)},
        "homes": round(est("B25002", 1)),
        "owner_occupied": round(est("B25003", 2)),
        "renter_occupied": round(est("B25003", 3)),
        "vacant": round(est("B25002", 3)),
        "seasonal": round(est("B25004", 6)),
    }


def acs(client, config: dict) -> dict:
    h = config["housing"]
    url = CENSUS_REPORTER + "?" + urlencode({"table_ids": ",".join(ACS_TABLES),
                                             "geo_ids": f"{h['census_geo']},{h['state_geo']}"})
    data = client.get(url).json()
    return {
        "release": data["release"]["name"],
        "years": data["release"]["years"],
        "town": acs_place(data["data"][h["census_geo"]]),
        "state": acs_place(data["data"][h["state_geo"]]),
        "source_url": f"https://censusreporter.org/profiles/{h['census_geo']}/",
    }


# ---- Subsidized Housing Inventory ----

def pdf_text(pdf: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)


def parse_shi(text: str, town: str) -> dict:
    """The town's row of the inventory: year-round homes, development units, SHI units, percent."""
    as_of = re.search(r"as of ([A-Z][a-z]+ \d{1,2}, \d{4})", text)
    row = re.search(rf"^{re.escape(town)} \d+ ([\d,]+) ([\d,]+) ([\d,]+) ([\d.]+)%", text, re.M)
    if not row or not as_of:
        raise FetchError(f"{town} not found in the Subsidized Housing Inventory")
    as_of_date = datetime.strptime(as_of.group(1), "%B %d, %Y").date().isoformat()
    return {
        "as_of": as_of_date,
        "year_round_homes": int(row.group(1).replace(",", "")),
        "development_units": int(row.group(2).replace(",", "")),
        "shi_units": int(row.group(3).replace(",", "")),
        "percent": float(row.group(4)),
        "goal_percent": 10,
    }


def shi(client, config: dict) -> dict:
    h = config["housing"]
    response = client.get(h["shi_url"])
    if not response.content.startswith(b"%PDF"):
        raise FetchError("the Subsidized Housing Inventory link did not return a PDF")
    return {**parse_shi(pdf_text(response.content), h["shi_name"]), "source_url": SHI_PAGE}


# ---- Parcels by type ----

def parcels(client, config: dict, now: datetime) -> dict:
    name = config["finance"]["dls_municipality"]
    newest = now.year + 1 if now.month >= 7 else now.year
    for fy in (newest, newest - 1, newest - 2):
        url = REPORT_URL + "?" + urlencode({
            "rdReport": PARCELS_REPORT, "rdReportFormat": "NativeExcel", "rdExportTableID": "xtParcels",
            "rdExcelOutputFormat": "Excel2007", "iclMuni": name, "islYear": fy})
        found = dls_rows(dls_get(client, url))
        if found and found[0].get("Single Family 101"):
            row = found[0]
            return {"fiscal_year": int(row["Fiscal Year"]),
                    "types": {label: int(row[col] or 0) for col, label in PARCEL_TYPES.items()},
                    "source_url": f"{REPORT_URL}?rdReport={PARCELS_REPORT}"}
    raise FetchError("no parcel counts returned")


def run(config: dict, client, data_dir: Path, now: datetime | None = None, force: bool = False,
        shi_client=None) -> dict:
    now = now or datetime.now(ZoneInfo(config["site"]["timezone"]))
    path = data_dir / "housing" / "housing.json"
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if saved and not force and now - datetime.fromisoformat(saved["updated_at"]) < timedelta(days=REFRESH_DAYS):
        return {"skipped": f"updated less than {REFRESH_DAYS} days ago"}
    data = {"updated_at": now.isoformat(timespec="seconds"), "bps_page": BPS_PAGE}
    problems = []
    for key, fetch in (("permits", lambda: permits(client, config, now)), ("acs", lambda: acs(client, config)),
                       ("shi", lambda: shi(shi_client or client, config)), ("parcels", lambda: parcels(client, config, now))):
        try:
            data[key] = fetch()
        except (FetchError, KeyError, ValueError) as e:
            problems.append(f"{key}: {e}")
            data[key] = saved.get(key)
    if not any(data[k] for k in ("permits", "acs", "shi", "parcels")):
        raise FetchError("; ".join(problems))
    save_json(path, data)
    return {"problems": problems, "requests": getattr(client, "request_count", None)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--town", default="gloucester")
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    parser.add_argument("--force", action="store_true", help="fetch even if the saved file is recent")
    args = parser.parse_args()
    config = load_config(args.town)
    client = PoliteClient(config["site"]["user_agent"], delay=2.0, timeout=60.0)
    # mass.gov refuses the site's usual User-Agent but accepts the HTTP
    # library's own, so this one request sends that plus the site's domain.
    shi_client = PoliteClient(f"{requests.utils.default_user_agent()} ({config['site']['domain']})", timeout=60.0)
    try:
        result = run(config, client, args.data, force=args.force, shi_client=shi_client)
    except FetchError as e:
        print(f"::error::Housing figures could not be fetched: {e}")
        return 1
    for problem in result.get("problems", []):
        print(f"::warning::Housing: kept the last saved figures for {problem}")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
