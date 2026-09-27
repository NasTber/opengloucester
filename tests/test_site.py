"""Structural checks on the built HTML: every page is well-formed for assistive
technology and every internal link resolves."""

import json
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import pytest

from pipeline import build_site


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.attrs = []
        self.links = []
        self.ids = set()
        self.html_lang = None
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append(tag)
        self.attrs.append((tag, attrs))
        if tag == "html":
            self.html_lang = attrs.get("lang")
        if tag == "title":
            self._in_title = True
        if "id" in attrs:
            self.ids.add(attrs["id"])
        for key in ("href", "src"):
            if attrs.get(key):
                self.links.append(attrs[key])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def parse(path):
    p = PageParser()
    p.feed(path.read_text(encoding="utf-8"))
    return p


def test_expected_pages_exist(site_dir, config):
    assert (site_dir / "index.html").exists()
    assert (site_dir / "404.html").exists()
    for section in config["sections"]:
        assert (site_dir / section["slug"] / "index.html").exists(), section["slug"]
    assert (site_dir / "about" / "accessibility" / "index.html").exists()


def test_support_files(site_dir, config):
    assert (site_dir / "CNAME").read_text().strip() == config["site"]["domain"]
    assert (site_dir / ".nojekyll").exists()
    sitemap = (site_dir / "sitemap.xml").read_text()
    assert f"https://{config['site']['domain']}/311/" in sitemap
    assert "404" not in sitemap
    assert "sitemap.xml" in (site_dir / "robots.txt").read_text()


def test_share_image(site_dir, config):
    image = f"https://{config['site']['domain']}/static/share/{config['slug']}.png"
    assert f'<meta property="og:image" content="{image}">' in (site_dir / "311" / "index.html").read_text()
    assert (site_dir / "static" / "share" / f"{config['slug']}.png").exists()


def test_every_page_has_accessible_structure(page_files):
    titles = set()
    for path in page_files:
        p = parse(path)
        name = path.name if path.name != "index.html" else str(path.parent.name or "/")
        assert p.html_lang == "en", name
        assert p.title.strip(), f"{name}: missing <title>"
        assert p.title not in titles, f"{name}: duplicate <title> {p.title!r}"
        titles.add(p.title)
        assert p.tags.count("h1") == 1, f"{name}: needs exactly one <h1>"
        assert p.tags.count("main") == 1, f"{name}: needs one <main>"
        assert "header" in p.tags and "footer" in p.tags and "nav" in p.tags, name
        # Skip link is the first focusable element and targets <main>.
        first_link = next(a for t, a in p.attrs if t == "a")
        assert first_link.get("href") == "#main", f"{name}: skip link must come first"
        assert "main" in p.ids
        # Every <img> needs an alt attribute (empty is fine for decoration).
        for tag, attrs in p.attrs:
            if tag == "img":
                assert "alt" in attrs, f"{name}: <img> without alt"


def test_internal_links_resolve(site_dir, page_files):
    for path in page_files:
        p = parse(path)
        for link in p.links:
            url = urlparse(link)
            if url.scheme or url.netloc or link.startswith("mailto:"):
                continue
            if link.startswith("#"):
                assert link[1:] in p.ids, f"{path}: broken anchor {link}"
                continue
            target = site_dir / url.path.lstrip("/")
            if url.path.endswith("/"):
                target = target / "index.html"
            assert target.exists(), f"{path.relative_to(site_dir)}: broken link {link}"
            if url.fragment:
                assert url.fragment in parse(target).ids, f"{path}: broken anchor {link}"


@pytest.mark.parametrize("rel", ["about/index.html", "about/accessibility/index.html"])
def test_contact_route_present(site_dir, rel):
    html = (site_dir / rel).read_text()
    assert "/issues" in html


def test_meeting_pages(site_dir):
    page = (site_dir / "meetings" / "2026-09-28-licensing-board" / "index.html").read_text()
    assert "Cancelled" in page
    assert "Marked cancelled on the city calendar." in page
    hrc = (site_dir / "meetings" / "2026-09-28-human-rights-commission" / "index.html").read_text()
    assert "/meetings/agendas/20124.pdf" in hrc
    assert "scanned image" in hrc
    assert (site_dir / "meetings" / "agendas" / "20124.pdf").exists()
    arts = (site_dir / "meetings" / "2026-09-29-committee-for-the-arts" / "index.html").read_text()
    assert "Removed from the city calendar." in arts


def test_home_lists_this_weeks_meetings(site_dir):
    home = (site_dir / "index.html").read_text()
    assert "Meetings this week" in home
    for label in ("Open 311 requests", "Typical time for the city to acknowledge", "Average single-family tax bill", "Unemployment rate"):
        assert label in home
    assert "↓ 0.5 pts from July 2025" in home and "↑ 3.0% from FY2025" in home
    assert "/meetings/2026-10-05-city-council-ordinances-and-administration-committee/" in home


def test_outbound_and_pdf_links_open_in_new_tab_with_warning(page_files, config):
    """Visitors keep their place on the site; screen readers hear that a new tab opens."""
    import re
    own = {config["site"]["domain"], "www." + config["site"]["domain"]}
    checked = 0
    for path in page_files:
        html = path.read_text()
        for attrs, text in re.findall(r"<a\b([^>]*)>(.*?)</a>", html, re.S):
            href = re.search(r'href="([^"]*)"', attrs).group(1)
            url = urlparse(href)
            outbound = url.scheme in ("http", "https") and url.hostname not in own
            if outbound or url.path.endswith(".pdf"):
                checked += 1
                assert 'target="_blank"' in attrs and 'rel="noopener"' in attrs, f"{path}: {href}"
                assert "(opens in new tab)" in text, f"{path}: {href} missing new-tab warning"
            else:
                assert "target=" not in attrs, f"{path}: internal link {href} should open in place"
    assert checked > 0


def test_ai_preview_is_labeled_and_linked(site_dir):
    page = (site_dir / "meetings" / "2026-09-28-human-rights-commission" / "index.html").read_text()
    assert "Summary written by AI" in page
    assert "Read the full agenda" in page
    assert "https://www.gloucester-ma.gov/Archive.aspx?ADID=20124" in page
    assert "<script" not in page.split("transcript-body")[1][:2000]


def test_scorecard_page(site_dir):
    page = (site_dir / "311" / "index.html").read_text()
    assert "Requests, past 12 months" in page
    assert "Ward 1" in page
    assert "CC BY-NC-SA 3.0" in page
    assert (site_dir / "311" / "methodology" / "index.html").exists()


def test_meeting_known_from_minutes(site_dir, data_dir):
    store = json.loads((data_dir / "meetings" / "meetings.json").read_text())
    archived = next(m for m in store.values() if m.get("source") == "archive")
    page = (site_dir / "meetings" / archived["slug"] / "index.html").read_text()
    assert "What was decided" in page
    assert "Decisions recorded" in page
    assert "Archive Center" in page
    assert f"/meetings/minutes/{archived['minutes'][0]['file']}" in page


def test_311_ward_and_category_pages_and_csv(site_dir):
    assert (site_dir / "311" / "ward" / "1" / "index.html").exists()
    categories = list((site_dir / "311" / "category").iterdir())
    assert categories
    header = (site_dir / "311" / "data" / "by-category.csv").read_text().splitlines()[0]
    assert header.startswith("category,requests,closed,still_open")


def test_meeting_list_preview_is_about_the_business(site_dir):
    home = (site_dir / "index.html").read_text()
    assert "operations director and a draft plan for recruiting a student member" in home
    assert "will meet on" not in home


def test_preview_line_fallbacks():
    from pipeline.build_site import preview_line
    assert preview_line({"preview": {"items": ["Budget transfer", "Grant acceptance."]}}) == "Budget transfer; Grant acceptance."
    assert preview_line({"minutes_summary": {"decisions": ["Approved X, 5-0.", "Denied Y"]}, "preview": {"headline": "H"}}) == "Approved X, 5-0."
    long = "Recommended the City Council approve payment of prior year invoices and obligations from the School CFO's memo dated August 31, 2026, in the amount of $51,317.23"
    clipped = preview_line({"minutes_summary": {"decisions": [long]}})
    assert len(clipped) <= 141 and clipped.endswith("…")
    assert preview_line({"minutes_summary": {"is_minutes": False, "decisions": []}, "preview": {"headline": "H"}}) == "H"
    assert preview_line({}) is None


def test_repeat_locations_page_and_maps(site_dir):
    import csv
    page = (site_dir / "311" / "repeat-locations" / "index.html").read_text()
    assert "<h1>Repeat locations</h1>" in page
    assert "/static/js/map.js?v=" in page
    main = (site_dir / "311" / "index.html").read_text()
    data = json.loads(main.split('id="map-data-recent">')[1].split("</script>")[0])
    assert data and all({"lat", "lng", "title", "url"} <= p.keys() for p in data)
    assert not any(p["title"].startswith(("Health Department", "Private Property", "Animal", "Police"))
                   for p in data)
    # The map is an extra: the same requests are listed as text.
    assert main.count('href="https://seeclickfix.com/issues/') >= len(data)
    rows = list(csv.reader((site_dir / "311" / "data" / "recent-open.csv").open()))
    assert rows[0] == ["id", "submitted", "category", "location", "ward", "url"] and len(rows) == len(data) + 1
    assert (site_dir / "311" / "data" / "repeat-locations.csv").exists()
    assert (site_dir / "static" / "vendor" / "leaflet" / "leaflet.js").exists()


def test_schools_page(site_dir):
    page = (site_dir / "schools" / "index.html").read_text()
    assert "Graduation rate" in page and "84.7%" in page and "State 89.3%" in page
    assert 'href="https://gloucesterschoolsreport.com"' in page
    assert 'href="/schools/"' in (site_dir / "index.html").read_text()


def test_meeting_search_index(site_dir):
    page = (site_dir / "meetings" / "search" / "index.html").read_text()
    url = re.search(r'data-index="([^"]+)"', page).group(1)
    assert url.startswith("/meetings/search-index.json?v=")
    index = json.loads((site_dir / "meetings" / "search-index.json").read_text())
    assert index and all(m["url"].startswith("/meetings/") and m["board"] and m["date"] for m in index)
    assert [m["date"] for m in index] == sorted((m["date"] for m in index), reverse=True)
    assert any(d["text"] and "**" not in d["text"] for m in index for d in m["docs"])
    assert 'action="/meetings/search/"' in (site_dir / "meetings" / "index.html").read_text()


def test_plain_text():
    assert build_site.plain_text("# Agenda\n\n**1.** Call to order  |  x\n") == "Agenda\n1. Call to order x"


def test_budget_page(site_dir):
    page = (site_dir / "budget" / "index.html").read_text()
    assert "$140.6M" in page and "Fiscal year 2025" in page
    assert "State median $4,297" in page
    assert 'href="/budget/"' in (site_dir / "index.html").read_text()
    spending = (site_dir / "budget" / "data" / "spending.csv").read_text().splitlines()
    assert spending[0].startswith("fiscal_year,total,") and spending[-1].startswith("2025,140559783,")
    reserves = (site_dir / "budget" / "data" / "reserves.csv").read_text().splitlines()
    assert reserves[-1] == "2026,4112161,"


def test_housing_page(site_dir):
    page = (site_dir / "housing" / "index.html").read_text()
    assert "8.04%" in page and "$601K" in page and "±$17,698" in page
    assert "2020–2024" in page and "partly estimated" in page
    permits = (site_dir / "housing" / "data" / "permits.csv").read_text().splitlines()
    assert permits[0].startswith("year,homes,") and permits[-1].startswith("2025,77,")


def test_model_markdown_keeps_only_safe_links():
    html = build_site.render_markdown(
        "[a](javascript:alert(1)) [b](https://example.org) ![c](https://tracker.example/p.png) <script>x</script>")
    assert "javascript:" not in html and "<img" not in html and "<script" not in html
    assert '<a href="https://example.org">b</a>' in html and "c" in html


def test_pages_set_a_content_security_policy(page_files):
    for path in page_files:
        html = path.read_text()
        assert "Content-Security-Policy" in html and "script-src 'self'" in html, path
