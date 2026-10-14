"""Polite HTTP: throttle, back off on 429, cache raw responses on disk."""

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "job-search-assistant/0.1 (personal, non-commercial)"


def default_client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0)


class PoliteFetcher:
    """GET JSON with a minimum delay between requests and a disk cache.

    A response is cached at `cache_path`; if that file exists, the network is
    not touched at all.
    """

    def __init__(
        self,
        client: httpx.Client,
        delay: float = 1.5,
        max_retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.delay = delay
        self.max_retries = max_retries
        self.sleep = sleep
        self._last_request: float | None = None

    def get_json(self, url: str, cache_path: Path, **kwargs: Any) -> Any:
        if cache_path.exists():
            log.debug("cache hit %s", cache_path)
            return json.loads(cache_path.read_text())

        for attempt in range(self.max_retries):
            self._throttle()
            response = self.client.get(url, **kwargs)
            if response.status_code == 429:
                wait = _retry_after(response) or 2**attempt * 5
                log.warning("429 from %s, waiting %.0fs", url, wait)
                self.sleep(wait)
                continue
            response.raise_for_status()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(response.text)
            return response.json()

        raise RuntimeError(f"Still rate-limited after {self.max_retries} attempts: {url}")

    def _throttle(self) -> None:
        if self._last_request is not None:
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.delay:
                self.sleep(self.delay - elapsed)
        self._last_request = time.monotonic()


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
