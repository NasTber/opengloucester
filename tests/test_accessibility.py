"""Automated WCAG 2.2 AA checks with axe-core in a real browser.

Each page is checked in light and dark mode, at desktop and phone widths. Axe
finds a subset of accessibility problems; manual keyboard and screen reader
checks are still needed when new sections are added.
"""

import pytest
from conftest import PAGE_PATHS

playwright_api = pytest.importorskip("playwright.sync_api")
axe_module = pytest.importorskip("axe_playwright_python.sync_playwright")

WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]
VIEWPORTS = {"desktop": {"width": 1280, "height": 900}, "phone": {"width": 320, "height": 640}}
SCHEMES = ["light", "dark"]
PATHS = PAGE_PATHS + ["/no-such-page/"]


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture(scope="module")
def axe():
    return axe_module.Axe()


def format_violations(results):
    lines = []
    for v in results.response["violations"]:
        targets = ", ".join(str(n["target"]) for n in v["nodes"][:5])
        lines.append(f"- [{v['impact']}] {v['id']}: {v['help']} ({targets})")
    return "\n".join(lines)


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("viewport", VIEWPORTS)
@pytest.mark.parametrize("path", PATHS)
def test_axe_no_violations(browser, axe, server_url, path, viewport, scheme):
    context = browser.new_context(viewport=VIEWPORTS[viewport], color_scheme=scheme)
    page = context.new_page()
    page.goto(server_url + path)
    results = axe.run(page, options={"runOnly": {"type": "tag", "values": WCAG_TAGS}})
    context.close()
    assert results.violations_count == 0, f"{path} ({viewport}, {scheme}):\n{format_violations(results)}"


@pytest.mark.parametrize("path", PATHS)
def test_reflows_without_horizontal_scroll(browser, server_url, path):
    """WCAG 1.4.10: content fits a 320px-wide viewport without sideways scrolling."""
    context = browser.new_context(viewport={"width": 320, "height": 640})
    page = context.new_page()
    page.goto(server_url + path)
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    context.close()
    assert overflow <= 0, f"{path} scrolls horizontally by {overflow}px at 320px"


def test_text_resize_200_percent(browser, server_url):
    """WCAG 1.4.4: text can be enlarged to 200% without loss of content."""
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.goto(server_url + "/")
    page.add_style_tag(content="html { font-size: 200% !important; }")
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    context.close()
    assert overflow <= 0


def test_skip_link_is_first_tab_stop_and_moves_focus(browser, server_url):
    context = browser.new_context()
    page = context.new_page()
    page.goto(server_url + "/")
    page.keyboard.press("Tab")
    focused = page.evaluate("document.activeElement.textContent.trim()")
    assert focused == "Skip to main content"
    assert page.evaluate("document.activeElement.getBoundingClientRect().top") >= 0, "skip link should be visible on focus"
    page.keyboard.press("Enter")
    # Focus moves to <main> once the browser finishes the in-page jump.
    page.wait_for_function("document.activeElement && document.activeElement.id === 'main'", timeout=2000)
    context.close()


def test_unknown_path_serves_404_page(browser, server_url):
    context = browser.new_context()
    page = context.new_page()
    response = page.goto(server_url + "/no-such-page/")
    assert response.status == 404
    assert page.locator("h1").inner_text() == "Page not found"
    context.close()


@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_meeting_search_results(browser, axe, server_url, viewport):
    context = browser.new_context(viewport=VIEWPORTS[viewport])
    page = context.new_page()
    page.goto(server_url + "/meetings/search/?q=ada+compliance")
    page.wait_for_selector("#search-results > li")
    assert "match" in page.text_content("#search-status")
    assert page.locator("#search-results mark").first.text_content().lower() in ("ada", "compliance")
    assert page.input_value("#search-q") == "ada compliance"
    results = axe.run(page, options={"runOnly": {"type": "tag", "values": WCAG_TAGS}})
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    page.goto(server_url + "/meetings/search/?q=zzzqqq")
    page.wait_for_function("document.getElementById('search-status').textContent.startsWith('No meetings')")
    context.close()
    assert results.violations_count == 0, format_violations(results)
    assert overflow <= 0
