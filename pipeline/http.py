"""A small HTTP client that is gentle with public servers.

City websites rate-limit aggressively, so every request identifies the
project, waits between calls, and backs off on errors instead of hammering.
"""

from __future__ import annotations

import time

import requests

RETRY_STATUS = {429, 500, 502, 503, 504}


class FetchError(Exception):
    pass


class PoliteClient:
    def __init__(self, user_agent: str, delay: float = 3.0, retries: int = 3, timeout: float = 30.0):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self._last = 0.0
        self.request_count = 0

    def get(self, url: str) -> requests.Response:
        last_error = None
        for attempt in range(self.retries + 1):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            if attempt:
                time.sleep(10 * 3 ** (attempt - 1))  # 10s, 30s, 90s
            self._last = time.monotonic()
            self.request_count += 1
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as e:
                last_error = e
                continue
            if response.status_code in RETRY_STATUS:
                last_error = FetchError(f"HTTP {response.status_code}")
                continue
            if response.status_code >= 400:
                raise FetchError(f"{url}: HTTP {response.status_code}")
            return response
        raise FetchError(f"{url}: {last_error}")
