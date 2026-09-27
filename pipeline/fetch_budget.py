"""Collect city budget figures from the Massachusetts Division of Local
Services (DLS) Municipal Databank.

General fund spending by function (Schedule A), revenue by source, the tax
levy against its Proposition 2 1/2 limit, free cash, the stabilization fund,
bond ratings, and general fund spending per resident next to the state median.
Writes data/finance/budget.json. The figures change a few times a year, so the
fetch is skipped when the saved file is less than a week old.

Usage:
    python -m pipeline.fetch_budget [--town gloucester] [--force]
"""

from __future__ import annotations

import argparse
import io
import json
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import openpyxl

from pipeline.config import DATA_DIR, DEFAULT_TOWN, configured, load_config
from pipeline.fetch_finance import REPORT_URL, dls_get, not_a_workbook
from pipeline.fetch_meetings import save_json
from pipeline.http import FetchError, PoliteClient

YEARS = 10
REFRESH_DAYS = 7
FUNCTIONS = ["General Government", "Public Safety", "Education", "Public Works", "Human Services",
             "Culture and Recreation", "Fixed Costs", "Intergov Assessments", "Other Expenditures", "Debt Service"]
# Plain names for the Schedule A columns.
FUNCTION_LABELS = {"Intergov Assessments": "State and county assessments", "Other Expenditures": "Other",
                   "Fixed Costs": "Fixed costs"}
REVENUE = {"Tax Levy": "Property tax", "State Aid": "State aid", "Local Receipts": "Local receipts", "All Other": "Other"}


def page_url(report: str) -> str:
    return f"{REPORT_URL}?rdReport={report}"


def export_url(report: str, table: str, **params) -> str:
    return REPORT_URL + "?" + urlencode({
        "rdReport": report, "rdReportFormat": "NativeExcel", "rdExportTableID": table,
        "rdExcelOutputFormat": "Excel2007", **params,
    })


def rows(content: bytes) -> list[dict]:
    """Workbook rows as dicts keyed by the header row (the first row with 'DOR Code')."""
    if not content.startswith(b"PK"):
        raise not_a_workbook(content)
    sheet = openpyxl.load_workbook(io.BytesIO(content), read_only=True).worksheets[0]
    table = list(sheet.iter_rows(values_only=True))
    start = next((i for i, r in enumerate(table) if str(r[0] or "").strip().upper() == "DOR CODE"), None)
    if start is None:
        return []
    header = [str(h or "").strip() for h in table[start]]
    return [dict(zip(header, r)) for r in table[start + 1:] if str(r[0] or "").strip() not in ("", "Totals:")]


def number(value) -> float | None:
    """123, '123', '$4,487', or '29,952' -> float. None for blanks."""
    if value in (None, ""):
        return None
    return float(str(value).replace("$", "").replace(",", "").strip())


def years_param(first: int, last: int) -> str:
    return ",".join(str(y) for y in range(first, last + 1))


def spending(client, code: str, newest: int) -> list[dict]:
    """Schedule A general fund spending by function, latest YEARS fiscal years."""
    years = []
    for fy in range(newest, newest - YEARS - 3, -1):
        found = rows(dls_get(client, export_url("ScheduleA.GeneralFund", "xtGenFund",
                                               iclMuni=code, islYear=fy, islAmountType="Expenditures")))
        row = found[0] if found else None
        # Years not yet reported come back as zeros.
        if row and number(row.get("Total Expenditures")):
            years.append({
                "fiscal_year": int(row["Fiscal Year"]),
                "total": round(number(row["Total Expenditures"])),
                "functions": {FUNCTION_LABELS.get(f, f.capitalize()): round(number(row.get(f)) or 0) for f in FUNCTIONS},
            })
        if len(years) >= YEARS:
            break
    return sorted(years, key=lambda y: y["fiscal_year"])


def revenue(client, municipality: str, first: int, last: int) -> list[dict]:
    found = rows(dls_get(client, export_url("RevenueBySource.RBS.RevbySource2", "dtCurrent",
                                           iclMuni2=municipality, iclYear2=years_param(first, last))))
    out = []
    for r in found:
        sources = {label: round(number(r.get(col)) or 0) for col, label in REVENUE.items()}
        total = sum(sources.values())
        if total:
            out.append({"fiscal_year": int(r["Fiscal Year"]), "total": total, "sources": sources})
    return sorted(out, key=lambda y: y["fiscal_year"])[-YEARS:]


def levy(client, municipality: str, first: int, last: int) -> list[dict]:
    found = rows(dls_get(client, export_url("Prop2.5.ExcessLevyCapandOverride_10_pres", "tblExcess",
                                           iclMuni=municipality, iclYear=years_param(first, last))))
    return sorted(({
        "fiscal_year": int(r["Fiscal Year"]),
        "levy": round(number(r["Total Tax Levy"])),
        "max_levy": round(number(r["Maximum Levy Limit"])),
        "excess_capacity": round(number(r["Excess Levy Capacity"])),
        "levy_ceiling": round(number(r["Levy Ceiling"])),
        "assessed_value": round(number(r["Total Assessed Value"])),
    } for r in found if number(r.get("Total Tax Levy"))), key=lambda y: y["fiscal_year"])[-YEARS:]


def free_cash(client, municipality: str, first: int, last: int) -> list[dict]:
    """The report has one column per fiscal year."""
    found = rows(dls_get(client, export_url("FreeCash2", "xtblFreeCash",
                                           iclMuni=municipality, iclYear=years_param(first, last))))
    if not found:
        return []
    return [{"fiscal_year": int(k), "amount": round(number(v))}
            for k, v in found[0].items() if k.isdigit() and number(v) is not None][-YEARS:]


def stabilization(client, municipality: str, first: int, last: int) -> list[dict]:
    found = rows(dls_get(client, export_url("Dashboard.TrendAnalysisReports.StabFund", "tblStabilization",
                                           iclMuni=municipality, iclYear=years_param(first, last))))
    return sorted(({"fiscal_year": int(r["Fiscal Year"]), "amount": round(number(r["Stabilization Fund Amount"]))}
                   for r in found if number(r.get("Stabilization Fund Amount")) is not None),
                  key=lambda y: y["fiscal_year"])[-YEARS:]


def bond_ratings(client, municipality: str, first: int, last: int) -> list[dict]:
    """Latest rating from each agency. Ratings are listed only for years with a bond sale."""
    out = []
    for agency, name in (("Moodys", "Moody's"), ("S&P", "S&P")):
        found = rows(dls_get(client, export_url("DLS_Bond_Ratings", "xtblBondRatings", iclMuni=municipality,
                                               iclYear=years_param(first, last), islCompany=agency)))
        rated = sorted((int(k), v) for k, v in (found[0].items() if found else []) if k.isdigit() and v)
        if rated:
            out.append({"agency": name, "rating": rated[-1][1], "fiscal_year": rated[-1][0]})
    return out


def per_resident(client, code: str) -> dict | None:
    """General fund spending per resident, with the median of all communities.

    Uses the latest year the town has reported in which nearly all communities have too.
    """
    found = rows(dls_get(client, export_url("351GenFunperCapita", "tblGenFundPerCap")))
    by_year = {}
    for r in found:
        value = number(r.get("Total General Fund Expenditures per Capita"))
        if value:
            by_year.setdefault(int(r["Fiscal Year"]), []).append((r, value))
    for year in sorted(by_year, reverse=True):
        reported = by_year[year]
        town = next((r for r, _ in reported if str(r["DOR Code"]).zfill(3) == code.zfill(3)), None)
        if town and len(reported) >= 0.95 * max(len(v) for v in by_year.values()):
            return {
                "fiscal_year": year,
                "town": round(number(town["Total General Fund Expenditures per Capita"])),
                "population": round(number(town["Population"])),
                "state_median": round(statistics.median(v for _, v in reported)),
                "communities": len(reported),
            }
    return None


def run(config: dict, client, data_dir: Path, now: datetime | None = None, force: bool = False) -> dict:
    now = now or datetime.now(ZoneInfo(config["site"]["timezone"]))
    path = data_dir / "finance" / "budget.json"
    if path.exists() and not force:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if now - datetime.fromisoformat(saved["updated_at"]) < timedelta(days=REFRESH_DAYS):
            return {"skipped": f"updated less than {REFRESH_DAYS} days ago"}
    fin = config["finance"]
    name, code = fin["dls_municipality"], fin["dls_code"]
    # Massachusetts fiscal years start July 1.
    newest = now.year + 1 if now.month >= 7 else now.year
    first = newest - YEARS - 1
    data = {
        "updated_at": now.isoformat(timespec="seconds"),
        "source": "Massachusetts Department of Revenue, Division of Local Services, Municipal Databank",
        "source_urls": {
            "spending": page_url("ScheduleA.GenFund_MAIN"),
            "revenue": page_url("RevenueBySource.RBS.RevbySourceMAIN"),
            "levy": page_url("Prop2.5.ExcessLevyCapandOverride_MAIN"),
            "free_cash": page_url("FreeCash2"),
            "stabilization": page_url("Dashboard.TrendAnalysisReports.StabFund"),
            "bond_ratings": page_url("DLS_bond_ratings"),
            "per_resident": page_url("351GenFunperCapita"),
        },
        "spending": spending(client, code, newest),
        "revenue": revenue(client, name, first, newest),
        "levy": levy(client, name, first, newest),
        "free_cash": free_cash(client, name, first, newest),
        "stabilization": stabilization(client, name, first, newest),
        "bond_ratings": bond_ratings(client, name, first, newest),
        "per_resident": per_resident(client, code),
    }
    if not data["spending"] or not data["revenue"]:
        raise FetchError("no spending or revenue figures returned")
    save_json(path, data)
    return {"spending": data["spending"][-1]["fiscal_year"], "revenue": data["revenue"][-1]["fiscal_year"],
            "requests": getattr(client, "request_count", None)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--town", default=DEFAULT_TOWN)
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    parser.add_argument("--force", action="store_true", help="fetch even if the saved file is recent")
    args = parser.parse_args()
    config = load_config(args.town)
    if not configured(config, "finance"):
        return 0
    client = PoliteClient(config["site"]["user_agent"], delay=2.0)
    try:
        print(json.dumps(run(config, client, args.data, force=args.force), indent=2))
    except FetchError as e:
        print(f"::error::Budget figures could not be fetched: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
