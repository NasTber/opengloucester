"""Collect the average single-family tax bill from the Massachusetts Division
of Local Services (DLS) Municipal Databank.

Writes data/finance/tax_bill.json with the last several fiscal years.

Usage:
    python -m pipeline.fetch_finance [--town gloucester]
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import openpyxl

from pipeline.config import DATA_DIR, load_config
from pipeline.fetch_meetings import save_json
from pipeline.http import FetchError, PoliteClient

REPORT_URL = "https://dls-gw.dor.state.ma.us/reports/rdPage.aspx"
REPORT_PAGE = REPORT_URL + "?rdReport=AverageSingleTaxBill.SingleFamTaxBill_wRange"
YEARS = 5


def report_url(municipality: str, fiscal_year: int) -> str:
    return REPORT_URL + "?" + urlencode({
        "rdReport": "AverageSingleTaxBill.SingleFamTaxBill_wRange",
        "rdReportFormat": "NativeExcel",
        "rdExportTableID": "tblSinglefamtaxbill",
        "rdExcelOutputFormat": "Excel2007",
        "iclMuni": municipality,
        "iclYear": fiscal_year,
    })


def not_a_workbook(content: bytes) -> FetchError:
    """An error that shows the start of what DLS sent back, to tell a block page from an error page."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", content[:3000].decode("utf-8", "replace"))).strip()
    return FetchError(f"DLS returned {len(content)} bytes that are not a workbook: {text[:200]!r}")


def dls_get(client, url: str) -> bytes:
    """A DLS export. The report builds the file, then redirects to a download link;
    a fast client can reach the link before the file is written and get an empty
    reply, so the link is tried again after a pause."""
    response = client.get(url)
    for attempt in range(3):
        download = getattr(response, "url", "") or ""
        if response.content.startswith(b"PK") or "rdDownload" not in download:
            break
        time.sleep(2 * (attempt + 1))
        response = client.get(download)
    if not response.content.startswith(b"PK"):
        headers = getattr(response, "headers", {}) or {}
        detail = ", ".join(f"{k}: {headers.get(k)}" for k in ("Server", "Content-Type", "Content-Length", "Location")
                           if headers.get(k))
        status = getattr(response, "status_code", "?")
        raise FetchError(f"{not_a_workbook(response.content)} (HTTP {status} from {getattr(response, 'url', url)}; {detail})")
    return response.content


def parse_workbook(content: bytes) -> dict | None:
    """Read the one data row of the report. None if the year has no certified figures."""
    if not content.startswith(b"PK"):
        raise not_a_workbook(content)
    sheet = openpyxl.load_workbook(io.BytesIO(content), read_only=True).worksheets[0]
    rows = list(sheet.iter_rows(values_only=True))
    if len(rows) < 2:
        return None
    header = [str(h or "").strip() for h in rows[0]]
    row = dict(zip(header, rows[1]))
    bill = row.get("Single-Family Tax Bill")
    if bill in (None, ""):
        return None
    return {
        "fiscal_year": int(row["Fiscal Year"]),
        "average_bill": int(round(float(bill))),
        "average_value": int(round(float(row["Average Single-Family Value"]))) if row.get("Average Single-Family Value") else None,
        "parcels": int(row["Single-Family Parcels"]) if row.get("Single-Family Parcels") else None,
        "state_rank": int(row["Rank"]) if row.get("Rank") not in (None, "") else None,
    }


def run(config: dict, client, data_dir: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(ZoneInfo(config["site"]["timezone"]))
    municipality = config["finance"]["dls_municipality"]
    # Massachusetts fiscal years start July 1.
    newest = now.year + 1 if now.month >= 7 else now.year
    years = []
    for fy in range(newest, newest - YEARS - 1, -1):
        record = parse_workbook(dls_get(client, report_url(municipality, fy)))
        if record:
            years.append(record)
        if len(years) >= YEARS:
            break
    if not years:
        raise FetchError("no tax bill figures returned")
    data = {
        "updated_at": now.isoformat(timespec="seconds"),
        "source": "Massachusetts Department of Revenue, Division of Local Services, Municipal Databank",
        "source_url": REPORT_PAGE,
        "years": sorted(years, key=lambda y: y["fiscal_year"]),
    }
    save_json(data_dir / "finance" / "tax_bill.json", data)
    return {"latest": years[0]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--town", default="gloucester")
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    config = load_config(args.town)
    client = PoliteClient(config["site"]["user_agent"], delay=2.0)
    try:
        print(json.dumps(run(config, client, args.data), indent=2))
    except FetchError as e:
        print(f"::error::Tax bill could not be fetched: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
