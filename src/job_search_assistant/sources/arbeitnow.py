"""Arbeitnow job board API: https://www.arbeitnow.com/blog/job-board-api"""

import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from job_search_assistant.http import PoliteFetcher
from job_search_assistant.models import Job
from job_search_assistant.text import html_to_text

log = logging.getLogger(__name__)

API_URL = "https://www.arbeitnow.com/api/job-board-api"


class Arbeitnow:
    name = "arbeitnow"

    def __init__(
        self,
        fetcher: PoliteFetcher,
        raw_dir: Path,
        max_pages: int = 20,
        today: date | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.max_pages = max_pages
        # The feed changes hourly, so the cache is per day: re-running today
        # reuses today's pages; tomorrow fetches fresh ones.
        self.cache_dir = raw_dir / self.name / (today or date.today()).isoformat()

    def fetch(self) -> Iterator[dict[str, Any]]:
        url: str | None = API_URL
        page = 1
        while url and page <= self.max_pages:
            body = self.fetcher.get_json(url, self.cache_dir / f"page-{page}.json")
            log.info("arbeitnow page %d: %d jobs", page, len(body["data"]))
            yield from body["data"]
            url = body["links"].get("next")
            page += 1

    def normalize(self, raw: dict[str, Any]) -> Job:
        created = raw.get("created_at")
        return Job(
            source=self.name,
            source_job_id=raw["slug"],
            url=raw["url"],
            title=raw["title"].strip(),
            company=raw["company_name"].strip(),
            location=raw.get("location") or None,
            country=None,  # not provided; inferred later from location
            remote=raw.get("remote"),
            description=html_to_text(raw.get("description") or ""),
            posted_at=datetime.fromtimestamp(created, tz=UTC) if created else None,
            raw=raw,
        )
