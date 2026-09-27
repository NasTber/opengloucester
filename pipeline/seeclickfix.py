"""Parsers for SeeClickFix public data.

Two public endpoints are used:
- Open311 GeoReport v2 (/open311/v2/<org>/requests.json): lists requests with
  category, location, and open/closed status. Paged, 100 per page, newest first.
- APIv2 single issue (/api/v2/issues/<id>): exact acknowledged, closed, and
  reopened times, and the detailed status (Open, Acknowledged, Closed, Archived).

Descriptions, photos, and reporter details are not stored.
"""

from __future__ import annotations

import re


def short_address(address: str, town: str) -> str:
    """'29 Emerson Avenue Gloucester, Massachusetts, 01930' -> '29 Emerson Avenue',
    given the town's name ('Gloucester')."""
    short = re.split(rf",?\s+{re.escape(town)}\b", address or "", maxsplit=1, flags=re.I)[0].strip(" ,")
    return short or address


def street_address(address: str, town: str) -> str:
    """The address as SeeClickFix lists it, without the town, state, and ZIP code."""
    short = short_address(address, town)
    return "" if re.fullmatch(r"\d{5}", short) else short  # a ZIP code alone


def parse_open311(item: dict) -> dict:
    return {
        "id": str(item["service_request_id"]),
        "category": (item.get("service_name") or "Uncategorized").strip(),
        "service_code": item.get("service_code"),
        "status": (item.get("status") or "").lower(),  # "open" includes acknowledged
        "created_at": item.get("requested_datetime"),
        "updated_at": item.get("updated_datetime"),
        "lat": item.get("lat"),
        "lng": item.get("long"),
        "address": (item.get("address") or "").strip(),
    }


def parse_issue(issue: dict) -> dict:
    return {
        "status": (issue.get("status") or "").lower(),  # open, acknowledged, closed, archived
        "acknowledged_at": issue.get("acknowledged_at"),
        "closed_at": issue.get("closed_at"),
        "reopened_at": issue.get("reopened_at"),
        "updated_at": issue.get("updated_at"),
        "created_at": issue.get("created_at"),
    }
