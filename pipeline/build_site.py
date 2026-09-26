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


def format_number(n: float | int | None) -> str:
    return "–" if n is None else f"{n:,}"


def format_month(value: str) -> str:
    d = date.fromisoformat(value + "-01")
    return f"{d.strftime('%b')} {d.year}"


def render_markdown(text: str) -> Markup:
    """Render model-written Markdown safely: escape any HTML first, and demote
    headings so they sit below the page's own h2."""
    html = markdown.markdown(escape(text), extensions=["sane_lists"])
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


def group_by(meetings: list[dict], period: str) -> list[tuple]:
    """Group meetings (already in order) by "date" (YYYY-MM-DD) or "month" (YYYY-MM-01)."""
    groups: dict = {}
    for m in meetings:
        key = m["date"] if period == "date" else m["date"][:7] + "-01"
        groups.setdefault(key, []).append(m)
    return list(groups.items())


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

    env = Environment(
        loader=FileSystemLoader([SITE_DIR / "templates", PAGES_DIR]),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(date=format_date, time=format_time, filesize=format_bytes, timestamp=format_timestamp,
                       duration=format_duration, number=format_number, month=format_month,
                       markdown=render_markdown)
    env.globals.update(group_by=group_by)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(STATIC_DIR, out_dir / "static")

    own_hosts = {site["domain"], "www." + site["domain"]}
    sections = config["sections"]
    common = dict(config=config, site=site, town=config["town"], sections=sections,
                  built_at=built_at, meetings=meetings, scorecard=scorecard)
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

    for folder in ("agendas", "minutes"):
        src = data_dir / "meetings" / folder
        if src.exists():
            shutil.copytree(src, out_dir / "meetings" / folder)

    write_support_files(out_dir, site, base_url, urls, built_at)
    return urls


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
