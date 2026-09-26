"""Structural checks on the built HTML: every page is well-formed for assistive
technology and every internal link resolves."""

from html.parser import HTMLParser
from urllib.parse import urlparse

import pytest


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
