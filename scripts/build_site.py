"""Build the static site into _site/.

Each file under site/pages/ is a Jinja template that extends the shared
layout. A page at site/pages/<path>/index.html is served at /<path>/, so every
section and sub-page has a real, linkable URL on GitHub Pages.

Usage:
    python scripts/build_site.py [--town gloucester] [--out _site]
"""

from __future__ import annotations

import argparse
import shutil
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
SITE_DIR = ROOT / "site"
PAGES_DIR = SITE_DIR / "pages"
STATIC_DIR = SITE_DIR / "static"

# Built but kept out of the sitemap.
UNLISTED_PAGES = {"404.html"}


def load_config(town: str) -> dict:
    path = CONFIG_DIR / f"{town}.toml"
    with path.open("rb") as f:
        config = tomllib.load(f)
    config["slug"] = town
    return config


def url_for(rel_path: Path) -> str:
    """Map a page file path to its public URL path, e.g. 311/index.html -> /311/."""
    parts = rel_path.parts
    if parts[-1] == "index.html":
        parts = parts[:-1]
        return "/" + "/".join(parts) + ("/" if parts else "")
    return "/" + "/".join(parts)


def build(town: str, out_dir: Path) -> list[str]:
    """Render every page and write supporting files. Returns the page URLs built."""
    config = load_config(town)
    site = config["site"]
    base_url = f"https://{site['domain']}"
    built_at = datetime.now(ZoneInfo(site["timezone"]))

    env = Environment(
        loader=FileSystemLoader([SITE_DIR / "templates", PAGES_DIR]),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )

    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(STATIC_DIR, out_dir / "static")

    sections = config["sections"]
    urls = []
    for page_path in sorted(PAGES_DIR.rglob("*.html")):
        rel = page_path.relative_to(PAGES_DIR)
        url = url_for(rel)
        section_slug = rel.parts[0] if len(rel.parts) > 1 else None
        section = next((s for s in sections if s["slug"] == section_slug), None)

        html = env.get_template(rel.as_posix()).render(
            config=config,
            site=site,
            town=config["town"],
            sections=sections,
            section=section,
            page_url=url,
            canonical_url=base_url + url,
            built_at=built_at,
        )
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(html, encoding="utf-8")
        if rel.as_posix() not in UNLISTED_PAGES:
            urls.append(url)

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
        f"  <url><loc>{base_url}{u}</loc><lastmod>{lastmod}</lastmod></url>" for u in urls
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
    args = parser.parse_args()
    urls = build(args.town, args.out)
    print(f"Built {len(urls)} pages into {args.out}")


if __name__ == "__main__":
    main()
