"""Parsers for CivicPlus (CivicEngage) city websites.

Gloucester posts each public meeting as a calendar event. The calendar RSS
feed lists upcoming events; each event's page carries the start time,
location, remote-attendance link, and a "Download Agenda" link into the
city's Archive Center.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from urllib.parse import urljoin

import feedparser

TIME_PREFIX = re.compile(r"^\s*\d{1,2}:\d{2}\s*[AP]\.?M\.?\s*[-–:]?\s*", re.I)
TIME_SUFFIX = re.compile(r",?\s*\d{1,2}:\d{2}\s*[AP]\.?M\.?\s*$", re.I)
PLACEHOLDER_LOCATIONS = {"", "event location"}
STATUS_WORDS = {
    "cancelled": re.compile(r"\bcancell?ed\b", re.I),
    "postponed": re.compile(r"\bpostponed\b", re.I),
    "rescheduled": re.compile(r"\brescheduled\b", re.I),
}


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<br\s*/?>", ", ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value).replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s*,(\s*,)*\s*", ", ", value)
    return value.strip(" ,")


def parse_title(title: str) -> dict:
    """Split a calendar title like '6:00 PM Licensing Board Special Meeting'."""
    raw = clean_text(title)
    status = next((s for s, rx in STATUS_WORDS.items() if rx.search(raw)), "scheduled")
    name = raw
    for rx in STATUS_WORDS.values():
        name = rx.sub("", name)
    name = re.sub(r"^[\s\-–:*]+|[\s\-–:*]+$", "", name)
    name = TIME_SUFFIX.sub("", TIME_PREFIX.sub("", name))
    name = re.sub(r"^[\s\-–:*]+|[\s\-–:*]+$", "", name)
    name = re.sub(r"\s{2,}", " ", name)
    special = bool(re.search(r"\bspecial\b", name, re.I))
    body = re.sub(r"\s+(special\s+)?meeting$", "", name, flags=re.I)
    body = re.sub(r"\bspecial\s+", "", body, flags=re.I).strip()
    return {"title": name, "body": body, "status": status, "special": special}


def parse_time(value: str) -> str | None:
    """'06:00 PM' -> '18:00'."""
    value = value.strip().upper().replace(".", "")
    for fmt in ("%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(value, fmt).strftime("%H:%M")
        except ValueError:
            pass
    return None


def parse_times(value: str) -> tuple[str | None, str | None]:
    parts = [p for p in re.split(r"\s*-\s*", value or "") if p.strip()]
    start = parse_time(parts[0]) if parts else None
    end = parse_time(parts[1]) if len(parts) > 1 else None
    # CivicPlus fills a missing end time with 11:59 PM; an all-day event is 12:00 AM - 11:59 PM.
    if end == "23:59":
        end = None
    if start == "00:00" and end is None:
        start = None
    return start, end


def parse_calendar_feed(content: bytes | str, base_url: str) -> list[dict]:
    feed = feedparser.parse(content)
    events = []
    for entry in feed.entries:
        link = urljoin(base_url, entry.get("link", ""))
        match = re.search(r"EID=(\d+)", link)
        if not match:
            continue
        try:
            date = datetime.strptime(entry.get("calendarevent_eventdates", "").strip(), "%B %d, %Y").date()
        except ValueError:
            continue
        start, end = parse_times(entry.get("calendarevent_eventtimes", ""))
        event = {
            "id": match.group(1),
            "source_url": f"{base_url.rstrip('/')}/Calendar.aspx?EID={match.group(1)}",
            "raw_title": clean_text(entry.get("title")),
            "date": date.isoformat(),
            "start_time": start,
            "end_time": end,
            "location": clean_text(entry.get("calendarevent_location")),
        }
        event.update(parse_title(entry.get("title", "")))
        events.append(event)
    return events


def parse_event_page(page: str, base_url: str) -> dict:
    """Pull structured details from a Calendar.aspx?EID=... page."""
    details: dict = {}

    agenda = re.search(r'id="[^"]*lnkDownloadAgenda"[^>]*href="([^"]+)"', page, re.I)
    if agenda:
        url = urljoin(base_url, html.unescape(agenda.group(1)))
        adid = re.search(r"ADID=(\d+)", url)
        details["agenda_url"] = url
        details["agenda_id"] = adid.group(1) if adid else None

    start = re.search(r'itemprop="startDate"[^>]*>\s*([0-9T:\-]+)\s*<', page)
    if start:
        details["start"] = start.group(1)

    name = re.search(r'itemprop="name"[^>]*>(.*?)</div>', page, re.S)
    # "Event Location" is the CivicPlus placeholder when no facility is set.
    if name and clean_text(name.group(1)).lower() not in PLACEHOLDER_LOCATIONS:
        details["location_name"] = clean_text(name.group(1))

    address = re.search(r'itemprop="address"[^>]*>(.*?)</span>\s*</div>', page, re.S)
    if address:
        details["address"] = clean_text(address.group(1))

    link = re.search(r'id="[^"]*lnkLink"[^>]*href="([^"]+)"', page, re.I)
    if link:
        details["remote_url"] = html.unescape(link.group(1))

    return details
