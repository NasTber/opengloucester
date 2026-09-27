"""Draw the image shown when a page is shared on social media or in a message.

A 1200 x 630 PNG with the site's mark, name, and tagline, in the site's own
font and colors. Run it once per town, or again after changing the name or
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

from pipeline.config import load_config

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "site" / "static"


def font_url(weight: int) -> str:
    # Inline, because a page set from a string can't load local files.
    data = (STATIC / "fonts" / f"public-sans-{weight}.woff2").read_bytes()
    return "data:font/woff2;base64," + base64.b64encode(data).decode()


def share_html(site: dict) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@font-face {{ font-family: "Public Sans"; font-weight: 400; src: url("{font_url(400)}"); }}
@font-face {{ font-family: "Public Sans"; font-weight: 800; src: url("{font_url(800)}"); }}
html, body {{ margin: 0; width: 1200px; height: 630px; }}
body {{ font-family: "Public Sans", sans-serif; background: #fff; color: #011536;
  display: flex; flex-direction: column; justify-content: center; padding: 0 96px; box-sizing: border-box;
  border-top: 16px solid #581824; border-bottom: 16px solid #1e3e80; }}
.brand {{ display: flex; align-items: center; gap: 36px; }}
.mark {{ display: grid; place-items: center; width: 150px; height: 150px; border-radius: 50%;
  background: #1e3e80; color: #fff; font-weight: 800; font-size: 88px; line-height: 1; }}
.name {{ font-weight: 800; font-size: 104px; letter-spacing: -0.02em; }}
.name span {{ color: #1e3e80; }}
.tagline {{ margin: 56px 0 0; font-size: 38px; line-height: 1.35; color: #545c65; max-width: 980px; }}
</style></head><body>
<div class="brand"><div class="mark">{escape(site["mark"])}</div>
<div class="name">{escape(site["name_prefix"])}<span>{escape(site["name_suffix"])}</span></div></div>
<p class="tagline">{escape(site["tagline"])}</p>
</body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--town", default="gloucester", help="config/<town>.toml to use")
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright

    config = load_config(args.town)
    out = STATIC / "share" / f"{args.town}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 630})
        page.set_content(share_html(config["site"]))
        page.evaluate("document.fonts.ready")
        page.screenshot(path=str(out))
        browser.close()
    print(f"Wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
