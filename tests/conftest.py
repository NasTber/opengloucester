import functools
import http.server
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_site  # noqa: E402


@pytest.fixture(scope="session")
def site_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("site")
    build_site.build("gloucester", out)
    return out


@pytest.fixture(scope="session")
def config():
    return build_site.load_config("gloucester")


@pytest.fixture(scope="session")
def page_files(site_dir):
    return sorted(site_dir.rglob("*.html"))


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Static server that mimics GitHub Pages: /foo -> /foo/index.html, 404.html for misses."""

    def log_message(self, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        page = Path(self.directory) / "404.html"
        if code == 404 and page.exists():
            body = page.read_bytes()
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().send_error(code, message, explain)


@pytest.fixture(scope="session")
def server_url(site_dir):
    handler = functools.partial(_Handler, directory=str(site_dir))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
