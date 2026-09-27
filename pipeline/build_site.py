"""Build the static site into _site/.

Each file under site/pages/ is a Jinja template that extends the shared
layout. A page at site/pages/<path>/index.html is served at /<path>/, so every
section and sub-page has a real, linkable URL on GitHub Pages. Pages for
individual records (one per meeting, one per board) are generated from the
JSON in data/.

Usage:
    python -m pipeline.build_site [--town gloucester] [--out _site]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import markdown
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup, escape

from pipeline.config import DATA_DIR, ROOT, load_config
from pipeline import summarize
from pipeline.fetch_meetings import slugify
from pipeline.seeclickfix import short_address

SITE_DIR = ROOT / "site"
PAGES_DIR = SITE_DIR / "pages"
STATIC_DIR = SITE_DIR / "static"

# Built but kept out of the sitemap.
UNLISTED_PAGES = {"/404.html"}


def url_for(rel_path: Path) -> str:
    """Map a page file path to its public URL path, e.g. 311/index.html -> /311/."""
    parts = rel_path.parts
    if parts[-1] == "index.html":
        parts = parts[:-1]
        return "/" + "/".join(parts) + ("/" if parts else "")
    return "/" + "/".join(parts)


# ---- Links that leave the site ---------------------------------------------

LINK_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S)
NEW_TAB_NOTE = '<span class="visually-hidden"> (opens in new tab)</span>'


def opens_new_tab(href: str, own_hosts: set[str]) -> bool:
    """Links to other sites and to PDFs open in a new tab, so visitors keep their place here."""
    url = urlparse(href)
    if url.scheme in ("http", "https") and url.hostname not in own_hosts:
        return True
    return url.path.lower().endswith(".pdf")


def mark_new_tab_links(html: str, own_hosts: set[str]) -> str:
    """Add target=_blank, rel=noopener, an arrow icon, and screen-reader text to outbound links."""
    def fix(match: re.Match) -> str:
        attrs, text = match.group(1), match.group(2)
        href = re.search(r'href="([^"]*)"', attrs)
        if not href or "target=" in attrs or not opens_new_tab(href.group(1), own_hosts):
            return match.group(0)
        if 'class="' in attrs:
            attrs = attrs.replace('class="', 'class="external ', 1)
        else:
            attrs += ' class="external"'
        return f'<a{attrs} target="_blank" rel="noopener">{text}{NEW_TAB_NOTE}</a>'
    return LINK_RE.sub(fix, html)


# ---- Template filters ------------------------------------------------------

def format_date(value: str | date, fmt: str = "long") -> str:
    d = date.fromisoformat(value) if isinstance(value, str) else value
    if fmt == "short":
        return f"{d.strftime('%a')}, {d.strftime('%b')} {d.day}"
    if fmt == "month":
        return d.strftime("%B %Y")
    if fmt == "plain":
        return f"{d.strftime('%B')} {d.day}, {d.year}"
    if fmt == "mon":
        return d.strftime("%b")
    if fmt == "day":
        return str(d.day)
    if fmt == "weekday":
        return d.strftime("%A")
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}"


def format_time(value: str | None) -> str:
    if not value:
        return ""
    h, m = (int(x) for x in value.split(":"))
    suffix = "AM" if h < 12 else "PM"
    return f"{(h % 12) or 12}:{m:02d} {suffix}"


def format_bytes(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def format_duration(days: float | None) -> str:
    if days is None:
        return "Not enough data"
    hours = days * 24
    if hours < 1:
        return "Under 1 hour"
    if hours < 36:
        n = round(hours)
        return f"{n} hour{'s' if n != 1 else ''}"
    return f"{days:.1f} days" if days < 10 else f"{days:.0f} days"


def format_duration_cell(days: float | None) -> str:
    """Duration for a table cell: a dash when there are too few requests."""
    return "–" if days is None else format_duration(days)


def model_name(model_id: str) -> str:
    """'claude-sonnet-5' -> 'Claude Sonnet 5'."""
    return " ".join(part.capitalize() for part in model_id.split("-"))


def school_year(year: int) -> str:
    """DESE labels a school year by the year it ends: 2026 -> '2025–26'."""
    return f"{year - 1}–{str(year)[2:]}"


def format_number(n: float | int | None) -> str:
    return "–" if n is None else f"{n:,}"


def format_money(n: float | int | None, style: str = "long") -> str:
    """140559783 -> '$140.6 million' (long) or '$140.6M' (short). Short rounds
    thousands too ('$139K'); long gives amounts under a million in full."""
    if n is None:
        return "–"
    if abs(n) >= 1_000_000:
        return f"${n / 1_000_000:,.1f}" + ("M" if style == "short" else " million")
    if style == "short" and abs(n) >= 10_000:
        return f"${n / 1000:,.0f}K"
    return f"${n:,.0f}"


def format_month(value: str) -> str:
    d = date.fromisoformat(value + "-01")
    return f"{d.strftime('%b')} {d.year}"


SAFE_HREF = re.compile(r"(https?://|mailto:)", re.I)


def render_markdown(text: str) -> Markup:
    """Render model-written Markdown safely: escape any HTML first, keep only
    web and mail links (no javascript: and the like), drop images (they would
    load from other sites), and demote headings below the page's own h2."""
    html = markdown.markdown(escape(text), extensions=["sane_lists"])
    html = re.sub(r'<a href="([^"]*)"[^>]*>(.*?)</a>',
                  lambda m: m.group(0) if SAFE_HREF.match(m.group(1)) else m.group(2), html, flags=re.S)
    html = re.sub(r'<img [^>]*?alt="([^"]*)"[^>]*>|<img [^>]*>', lambda m: m.group(1) or "", html)
    for level in (3, 2, 1):
        html = html.replace(f"<h{level}>", f"<h{level + 2}>").replace(f"</h{level}>", f"</h{level + 2}>")
    return Markup(html)


def format_timestamp(value: str) -> str:
    dt = datetime.fromisoformat(value)
    return f"{dt.strftime('%B')} {dt.day}, {dt.year}"


# ---- Data ------------------------------------------------------------------

def load_meetings(data_dir: Path, today: date, summary_model: str | None = None) -> dict:
    path = data_dir / "meetings" / "meetings.json"
    store = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    status_path = data_dir / "meetings" / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else None

    meetings = sorted(store.values(), key=lambda m: (m["date"], m.get("start_time") or "", m["body"], m["id"]))
    optional = dict(start_time=None, end_time=None, location="", location_name="", address="",
                    remote_url=None, agendas=[], history=[], status="scheduled", special=False, listed=True,
                    source="calendar", source_url=None)
    for m in meetings:
        for key, default in optional.items():
            m.setdefault(key, default)
        m["url"] = f"/meetings/{m['slug']}/"
        m["body_slug"] = slugify(m["body"])
        m["body_url"] = f"/meetings/boards/{m['body_slug']}/"
        m["agenda"] = m["agendas"][-1] if m.get("agendas") else None
        # Only previews from the current model and prompt are shown; older ones
        # are regenerated by the next summarize run.
        m["preview"] = summarize.cached(data_dir, m["agenda"]["sha256"], summary_model, "agenda") if m["agenda"] and summary_model else None
        m.setdefault("minutes", [])
        m["minutes_doc"] = m["minutes"][-1] if m["minutes"] else None
        m["minutes_summary"] = (
            summarize.cached(data_dir, m["minutes_doc"]["sha256"], summary_model, "minutes")
            if m["minutes_doc"] and summary_model else None
        )
        m["minutes_too_large"] = bool(m["minutes_doc"]) and summarize.too_large(m["minutes_doc"])
        m["preview_line"] = preview_line(m)

    today_s = today.isoformat()
    upcoming = [m for m in meetings if m["date"] >= today_s]
    past = [m for m in meetings if m["date"] < today_s][::-1]

    boards = defaultdict(list)
    for m in meetings:
        boards[m["body_slug"]].append(m)
    board_list = sorted(
        ({"slug": s, "name": ms[-1]["body"], "url": ms[0]["body_url"], "meetings": ms[::-1],
          "next": next((m for m in ms if m["date"] >= today_s), None)} for s, ms in boards.items()),
        key=lambda b: b["name"].lower(),
    )
    week_end = (today + timedelta(days=7)).isoformat()
    return {
        "all": meetings,
        "upcoming": upcoming,
        "this_week": [m for m in upcoming if m["date"] < week_end],
        "past": past,
        "boards": board_list,
        "status": status,
        "tracking_since": min((m["first_seen"] for m in meetings), default=None),
    }


def plain_text(transcript: str | None) -> str:
    """Markdown transcript -> plain lines, for search."""
    lines = (re.sub(r"\s+", " ", re.sub(r"[*_`#>|]+", " ", line)).strip() for line in (transcript or "").splitlines())
    return "\n".join(line for line in lines if line)


def search_index(meetings: list[dict]) -> list[dict]:
    """Every meeting's board, date, and document text, for /meetings/search/."""
    rows = []
    for m in meetings:
        docs = []
        if m["preview"]:
            docs.append({"kind": "Agenda", "text": plain_text(m["preview"].get("transcript"))})
        ms = m["minutes_summary"]
        if ms:
            docs.append({"kind": "Minutes" if ms.get("is_minutes", True) else "Agenda",
                         "text": plain_text(ms.get("transcript"))})
        rows.append({"url": m["url"], "board": m["body"], "date": m["date"],
                     "date_text": format_date(m["date"]), "docs": [d for d in docs if d["text"]]})
    return rows[::-1]


def group_by(meetings: list[dict], period: str) -> list[tuple]:
    """Group meetings (already in order) by "date" (YYYY-MM-DD) or "month" (YYYY-MM-01)."""
    groups: dict = {}
    for m in meetings:
        key = m["date"] if period == "date" else m["date"][:7] + "-01"
        groups.setdefault(key, []).append(m)
    return list(groups.items())


def change_text(diff: float, unit: str, since: str, digits: int = 0) -> str:
    """'↑ 7 from last week' / 'No change from last week'. Neutral wording, no judgment."""
    if round(diff, digits) == 0:
        return f"No change from {since}"
    arrow = "↑" if diff > 0 else "↓"
    amount = f"{abs(diff):,.{digits}f}"
    return f"{arrow} {amount}{unit} from {since}"


def clip(text: str, limit: int = 140) -> str:
    """Shorten to a whole word within limit characters."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:—– ") + "…"


def preview_line(meeting: dict) -> str | None:
    """One line for meeting lists: what the meeting is about, never the board,
    date, or time (those are already shown). Prefers the AI headline; older
    summaries without one fall back to their own items or decisions."""
    minutes, agenda = meeting.get("minutes_summary"), meeting.get("preview")
    if minutes and minutes.get("is_minutes", True):
        if minutes.get("headline"):
            return minutes["headline"]
        if minutes.get("decisions"):
            return clip(minutes["decisions"][0])
    if agenda:
        if agenda.get("headline"):
            return agenda["headline"]
        if agenda.get("items"):
            return clip("; ".join(i.rstrip(".") for i in agenda["items"][:4]) + ".")
    return None


def headline_numbers(data_dir: Path, scorecard: dict | None) -> list[dict]:
    """The home page's headline row. Each number links to where it comes from."""
    numbers = []
    if scorecard:
        backlog = scorecard["backlog"]
        numbers.append({
            "label": "Open 311 requests", "value": f"{backlog['open']:,}", "href": "/311/#open",
            "change": change_text(backlog["open"] - backlog.get("open_week_ago", backlog["open"]), "", "last week"),
        })
        overall = scorecard["overall"]
        # The median leaves out requests never acknowledged, so show how many were.
        change = "Median, past 12 months"
        if overall.get("checked"):
            change += f" · {round(overall['acknowledged'] / overall['checked'] * 100)}% of requests were acknowledged"
        numbers.append({
            "label": "Typical time for the city to acknowledge a request",
            "value": format_duration(overall["time_to_acknowledge"]["median"]), "href": "/311/#speed", "change": change,
        })
    tax_path = data_dir / "finance" / "tax_bill.json"
    if tax_path.exists():
        tax = json.loads(tax_path.read_text(encoding="utf-8"))
        latest, prior = tax["years"][-1], (tax["years"][-2] if len(tax["years"]) > 1 else None)
        change = ""
        if prior:
            pct = (latest["average_bill"] - prior["average_bill"]) / prior["average_bill"] * 100
            change = change_text(pct, "%", f"FY{prior['fiscal_year']}", 1)
        numbers.append({
            "label": "Average single-family tax bill", "value": f"${latest['average_bill']:,}",
            "href": tax["source_url"], "change": change,
            "source": f"FY{latest['fiscal_year']} · Mass. Division of Local Services",
        })
    labor_path = data_dir / "labor" / "unemployment.json"
    if labor_path.exists():
        labor = json.loads(labor_path.read_text(encoding="utf-8"))
        latest = labor["months"][-1]
        month_name = date(latest["year"], latest["month"], 1).strftime("%B")
        year_ago = next((m for m in labor["months"] if m["year"] == latest["year"] - 1 and m["month"] == latest["month"]), None)
        numbers.append({
            "label": "Unemployment rate", "value": f"{latest['rate']:.1f}%", "href": labor["source_url"],
            # City rates are not seasonally adjusted: compare with the same month a year earlier.
            "change": change_text(latest["rate"] - year_ago["rate"], " pts", f"{month_name} {latest['year'] - 1}", 1) if year_ago else "",
            "source": f"{month_name} {latest['year']}{' (preliminary)' if latest.get('preliminary') else ''} · U.S. Bureau of Labor Statistics",
        })
    return numbers


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def map_points(sc: dict | None) -> dict:
    """Points for the 311 maps. The pages list the same places as text."""
    if not sc:
        return {"recent": [], "repeats": []}
    recent = [{
        "lat": r["lat"], "lng": r["lng"], "title": r["category"], "url": r["url"],
        "text": f"{r['address'] or 'No street address'}. Submitted {format_date(r['created_at'][:10], 'plain')}.",
    } for r in sc.get("recent_open", {}).get("requests", []) if r.get("lat") is not None]
    repeats = [{
        "lat": p["lat"], "lng": p["lng"], "title": p["address"] or "No street address",
        "text": f"{p['category']}. {plural(p['reports'], 'request')}, {p['again_after_close']:,} after an earlier one was closed.",
        "url": p["requests"][-1]["url"], "link": "Latest request on SeeClickFix",
        "size": 5 + min(p["again_after_close"], 8),
    } for p in sc.get("repeats", {}).get("places", [])]
    return {"recent": recent, "repeats": repeats}


# ---- Build -----------------------------------------------------------------

def build(town: str, out_dir: Path, data_dir: Path = DATA_DIR, now: datetime | None = None) -> list[str]:
    """Render every page and write supporting files. Returns the page URLs built."""
    config = load_config(town)
    site = config["site"]
    base_url = f"https://{site['domain']}"
    built_at = now or datetime.now(ZoneInfo(site["timezone"]))
    meetings = load_meetings(data_dir, built_at.date(), config.get("summaries", {}).get("model"))
    scorecard_path = data_dir / "311" / "scorecard.json"
    scorecard = json.loads(scorecard_path.read_text(encoding="utf-8")) if scorecard_path.exists() else None
    schools_path = data_dir / "schools" / "schools.json"
    schools = json.loads(schools_path.read_text(encoding="utf-8")) if schools_path.exists() else None
    budget_path = data_dir / "finance" / "budget.json"
    budget = json.loads(budget_path.read_text(encoding="utf-8")) if budget_path.exists() else None
    housing_path = data_dir / "housing" / "housing.json"
    housing = json.loads(housing_path.read_text(encoding="utf-8")) if housing_path.exists() else None

    env = Environment(
        loader=FileSystemLoader([SITE_DIR / "templates", PAGES_DIR]),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(date=format_date, time=format_time, filesize=format_bytes, timestamp=format_timestamp,
                       duration=format_duration, number=format_number, money=format_money, month=format_month,
                       markdown=render_markdown, duration_cell=format_duration_cell, street=short_address,
                       model_name=model_name, capitalize_first=lambda t: Markup(t[:1].upper() + t[1:]),
                       school_year=school_year)
    # Versioned asset URLs, so a browser never pairs new pages with an old cached stylesheet.
    css_version = hashlib.sha256((STATIC_DIR / "css" / "site.css").read_bytes()).hexdigest()[:10]
    def versioned(path: str) -> str:
        """'/static/js/map.js' -> '/static/js/map.js?v=<hash>'."""
        digest = hashlib.sha256((STATIC_DIR / path.removeprefix("/static/")).read_bytes()).hexdigest()[:10]
        return f"{path}?v={digest}"
    env.filters["versioned"] = versioned
    env.globals.update(group_by=group_by, reserve_rows=reserve_rows, today=built_at.date().isoformat(), css_version=css_version, plural=plural,
                       change=lambda diff, since: change_text(diff, "", since))

    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(STATIC_DIR, out_dir / "static")

    own_hosts = {site["domain"], "www." + site["domain"]}
    sections = config["sections"]
    # Newest first; the version in the URL changes whenever the text does.
    search_json = json.dumps(search_index(meetings["all"]), ensure_ascii=False, separators=(",", ":"))
    search_url = f"/meetings/search-index.json?v={hashlib.sha256(search_json.encode()).hexdigest()[:10]}"
    share_path = STATIC_DIR / "share" / f"{town}.png"
    share_image = f"{base_url}/static/share/{town}.png" if share_path.exists() else None
    common = dict(config=config, site=site, town=config["town"], sections=sections, share_image=share_image, search_url=search_url,
                  built_at=built_at, meetings=meetings, scorecard=scorecard, schools=schools, budget=budget, housing=housing,
                  headline=headline_numbers(data_dir, scorecard), map_points=map_points(scorecard))
    urls = []

    def render(template: str, url: str, **context) -> None:
        section_slug = url.strip("/").split("/")[0] or None
        section = next((s for s in sections if s["slug"] == section_slug), None)
        html = env.get_template(template).render(
            **common, **context, section=section, page_url=url, canonical_url=base_url + url
        )
        html = mark_new_tab_links(html, own_hosts)
        dest = out_dir / (url.lstrip("/") + ("index.html" if url.endswith("/") else ""))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(html, encoding="utf-8")
        if url not in UNLISTED_PAGES:
            urls.append(url)

    for page_path in sorted(PAGES_DIR.rglob("*.html")):
        rel = page_path.relative_to(PAGES_DIR)
        render(rel.as_posix(), url_for(rel))

    for m in meetings["all"]:
        render("meeting.html", m["url"], meeting=m)
    for b in meetings["boards"]:
        render("board.html", b["url"], board=b)
    if scorecard:
        populations = {w["ward"]: w for w in scorecard["by_ward"]}
        for w in scorecard.get("wards", []):
            if w["ward"] != "outside":
                render("ward.html", f"/311/ward/{w['ward']}/", ward={**populations.get(w["ward"], {}), **w})
        for c in scorecard.get("categories", []):
            render("category.html", f"/311/category/{c['slug']}/", category=c)
        write_311_csvs(out_dir / "311" / "data", scorecard)
    if budget:
        write_budget_csvs(out_dir / "budget" / "data", budget)
    if housing and housing.get("permits"):
        write_csv(out_dir / "housing" / "data" / "permits.csv",
                  ["year", "homes", "in_1_unit_buildings", "in_2_unit_buildings", "in_3_4_unit_buildings",
                   "in_5_plus_unit_buildings", "partly_estimated"],
                  [[y["year"], y["units"], *y["by_size"].values(), y["estimated"]] for y in housing["permits"]["years"]])

    for folder in ("agendas", "minutes"):
        src = data_dir / "meetings" / folder
        if src.exists():
            shutil.copytree(src, out_dir / "meetings" / folder)

    (out_dir / "meetings" / "search-index.json").write_text(search_json, encoding="utf-8")
    write_support_files(out_dir, site, base_url, urls, built_at)
    return urls


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def write_budget_csvs(folder: Path, b: dict) -> None:
    """Downloadable tables behind the budget page. Amounts are in dollars."""
    functions = list(b["spending"][-1]["functions"]) if b["spending"] else []
    write_csv(folder / "spending.csv", ["fiscal_year", "total", *functions],
              [[y["fiscal_year"], y["total"], *(y["functions"].get(f) for f in functions)] for y in b["spending"]])
    sources = list(b["revenue"][-1]["sources"]) if b["revenue"] else []
    write_csv(folder / "revenue.csv", ["fiscal_year", "total", *sources],
              [[y["fiscal_year"], y["total"], *(y["sources"].get(s) for s in sources)] for y in b["revenue"]])
    write_csv(folder / "levy.csv", ["fiscal_year", "levy", "max_allowable_levy", "unused_levy_capacity", "levy_ceiling", "assessed_value"],
              [[y["fiscal_year"], y["levy"], y["max_levy"], y["excess_capacity"], y["levy_ceiling"], y["assessed_value"]]
               for y in b["levy"]])
    write_csv(folder / "reserves.csv", ["fiscal_year", "free_cash", "stabilization_fund"],
              [[y, *rv] for y, rv in reserve_rows(b)])


def reserve_rows(b: dict) -> list[tuple[int, tuple]]:
    """[(fiscal_year, (free_cash, stabilization))], oldest first; None where not reported."""
    free = {y["fiscal_year"]: y["amount"] for y in b["free_cash"]}
    stab = {y["fiscal_year"]: y["amount"] for y in b["stabilization"]}
    return [(y, (free.get(y), stab.get(y))) for y in sorted(set(free) | set(stab))]


def write_311_csvs(folder: Path, sc: dict) -> None:
    """Downloadable tables behind the 311 charts. Durations are in days."""
    def med(s):
        return s["median"]
    summary = ["requests", "closed", "still_open", "median_days_to_acknowledge", "median_days_to_close"]
    def row(x):
        return [x["received"], x["closed"], x["open"], med(x["time_to_acknowledge"]), med(x["time_to_close"])]
    write_csv(folder / "monthly.csv", ["month", *summary], [[m["month"], *row(m)] for m in sc["monthly"]])
    write_csv(folder / "by-ward.csv", ["ward", "population_2020", "per_1000_residents", *summary],
              [[w["ward"], w.get("population_2020"), w.get("per_1000_residents"), *row(w)] for w in sc["by_ward"]])
    write_csv(folder / "by-category.csv", ["category", *summary], [[c["category"], *row(c)] for c in sc["categories"]])
    write_csv(folder / "recent-open.csv", ["id", "submitted", "category", "location", "ward", "url"],
              [[r["id"], r["created_at"][:10], r["category"], r["address"], r["ward"], r["url"]]
               for r in sc.get("recent_open", {}).get("requests", [])])
    write_csv(folder / "repeat-locations.csv",
              ["location", "category", "ward", "requests", "after_a_close", "still_open", "first", "last", "request_urls"],
              [[p["address"], p["category"], p["ward"], p["reports"], p["again_after_close"], p["open"], p["first"], p["last"],
                " ".join(r["url"] for r in p["requests"])] for p in sc.get("repeats", {}).get("places", [])])
    write_csv(folder / "open-by-age.csv", ["open_for", "requests"], [[b["label"], b["count"]] for b in sc["backlog"]["buckets"]])
    for w in sc.get("wards", []):
        write_csv(folder / f"ward-{w['ward']}.csv", ["category", *summary], [[c["category"], *row(c)] for c in w["by_category"]])
    for c in sc.get("categories", []):
        write_csv(folder / f"category-{c['slug']}.csv", ["ward", *summary], [[w["ward"], *row(w)] for w in c["by_ward"]])


def write_support_files(out_dir: Path, site: dict, base_url: str, urls: list[str], built_at: datetime) -> None:
    # Custom domain for GitHub Pages. With Actions deploys the domain is also set
    # in the repo's Pages settings; the file keeps the build self-describing.
    (out_dir / "CNAME").write_text(site["domain"] + "\n", encoding="utf-8")
    # Serve files as-is; don't run Jekyll over the output.
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    lastmod = built_at.date().isoformat()
    entries = "\n".join(
        f"  <url><loc>{base_url}{u}</loc><lastmod>{lastmod}</lastmod></url>" for u in sorted(urls)
    )
    (out_dir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}\n"
        "</urlset>\n",
        encoding="utf-8",
    )
    (out_dir / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\n\nSitemap: {base_url}/sitemap.xml\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--town", default="gloucester", help="config/<town>.toml to build")
    parser.add_argument("--out", type=Path, default=ROOT / "_site", help="output directory")
    parser.add_argument("--data", type=Path, default=DATA_DIR, help="data directory")
    args = parser.parse_args()
    urls = build(args.town, args.out, args.data)
    print(f"Built {len(urls)} pages into {args.out}")


if __name__ == "__main__":
    main()
