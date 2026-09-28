"""Draw the image shown when a page is shared on social media or in a message.

A 1200 x 630 PNG of the site's masthead (its icon, name, and tagline), in the
site's own fonts and colors. Also writes PNG copies of the favicon for phones. Run it once per town, or again after changing the name or
tagline; the build links the image when site/static/share/<town>.png exists.
Needs Playwright (requirements-dev.txt).

Usage:
    python -m pipeline.make_share_image [--town gloucester]
"""

from __future__ import annotations

import argparse
import base64
from html import escape
from pathlib import Path

from pipeline.config import DEFAULT_TOWN, load_config

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "site" / "static"


def font_url(name: str) -> str:
    # Inline, because a page set from a string can't load local files.
    data = (STATIC / "fonts" / f"{name}.woff2").read_bytes()
    return "data:font/woff2;base64," + base64.b64encode(data).decode()


def svg_url(name: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode((STATIC / name).read_bytes()).decode()


def share_html(site: dict, sections: list[dict]) -> str:
    """The masthead as it appears at the top of every page, with the site's icon."""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@font-face {{ font-family: "Public Sans"; font-weight: 400; src: url("{font_url("public-sans-400")}"); }}
@font-face {{ font-family: "Source Serif 4"; font-weight: 700; src: url("{font_url("source-serif-4-700")}"); }}
html, body {{ margin: 0; width: 1200px; height: 630px; }}
body {{ font-family: "Public Sans", sans-serif; background: #fff; color: #011536; box-sizing: border-box;
  display: flex; flex-direction: column; justify-content: center; padding: 0 96px; }}
.brand {{ display: flex; align-items: center; gap: 40px; }}
.mark {{ width: 150px; height: 150px; border: 3px solid #011536; }}
.name {{ font-family: "Source Serif 4", serif; font-weight: 700; font-size: 100px; letter-spacing: -0.01em; line-height: 1; }}
.name span {{ color: #581824; }}
.line {{ margin: 28px 0 0; font-size: 36px; color: #3d444c; }}
.rule {{ margin: 44px 0 0; border-top: 2px solid #011536; border-bottom: 8px double #011536; height: 6px; }}
.tagline {{ margin: 36px 0 0; font-size: 34px; font-weight: 400; line-height: 1.35; color: #011536; }}
</style></head><body>
<div class="brand"><img class="mark" src="{svg_url("favicon.svg")}" alt="">
<div class="name">{escape(site["name_prefix"])}<span>{escape(site["name_suffix"])}</span></div></div>
<p class="line">{escape(site["masthead"])}</p>
<div class="rule"></div>
<p class="tagline">{" · ".join(escape(s.get("nav", s["title"])) for s in sections if s["slug"] != "about")} · Your street</p>
</body></html>"""


def write_icons(page) -> None:
    """PNG copies of the SVG icon, for phones' home screens and older browsers."""
    for size, name in ((180, "apple-touch-icon.png"), (32, "favicon-32.png")):
        page.set_viewport_size({"width": size, "height": size})
        page.set_content(f'<html><body style="margin:0"><img src="{svg_url("favicon.svg")}" '
                         f'style="display:block;width:{size}px;height:{size}px"></body></html>')
        page.screenshot(path=str(STATIC / name))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--town", default=DEFAULT_TOWN, help="config/<town>.toml to use")
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright

    config = load_config(args.town)
    out = STATIC / "share" / f"{args.town}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 630})
        page.set_content(share_html(config["site"], config["sections"]))
        page.evaluate("document.fonts.ready")
        page.screenshot(path=str(out))
        write_icons(page)
        browser.close()
    print(f"Wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
