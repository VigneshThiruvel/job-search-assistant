"""Adzuna search API: https://developer.adzuna.com/docs/search

Free tier: 25 hits/min, 250/day, 1000/week, 2500/month. Descriptions are
snippets only; never fetch `redirect_url` pages (ToS: all queries via Adzuna).
"""

import logging
from collections.abc import Iterator, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

from job_search_assistant.http import PoliteFetcher
from job_search_assistant.models import Job
from job_search_assistant.text import html_to_text

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
RESULTS_PER_PAGE = 50  # API maximum
# Germany plus the neighbours most likely to have English-language roles. Kept
# small so a daily run (roles x countries) stays under the tightest reported quota.
COUNTRIES = ("de", "at", "ch")


class Adzuna:
    name = "adzuna"

    def __init__(
        self,
        fetcher: PoliteFetcher,
        raw_dir: Path,
        app_id: str,
        app_key: str,
        queries: Sequence[str],
        countries: Sequence[str] = COUNTRIES,
        days: int = 14,
        max_pages: int = 1,
        today: date | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.credentials = {"app_id": app_id, "app_key": app_key}
        self.queries = queries
        self.countries = countries
        self.days = days
        self.max_pages = max_pages
        self.cache_dir = raw_dir / self.name / (today or date.today()).isoformat()

    def fetch(self) -> Iterator[dict[str, Any]]:
        seen: set[str] = set()
        for country in self.countries:
            for query in self.queries:
                for job in self._search(country, query):
                    if job["id"] in seen:
                        continue
                    seen.add(job["id"])
                    # The response has no country field, so keep the one we asked for.
                    yield {"country": country, "job": job}

    def _search(self, country: str, query: str) -> Iterator[dict[str, Any]]:
        slug = query.lower().replace(" ", "-")
        for page in range(1, self.max_pages + 1):
            try:
                body = self.fetcher.get_json(
                    SEARCH_URL.format(country=country, page=page),
                    self.cache_dir / f"{country}-{slug}-p{page}.json",
                    params={
                        **self.credentials,
                        "what_phrase": query,
                        "max_days_old": self.days,
                        "results_per_page": RESULTS_PER_PAGE,
                        "sort_by": "date",
                    },
                )
            except httpx.HTTPStatusError as e:
                # Re-raise without the URL, which contains the API key.
                raise RuntimeError(
                    f"adzuna {country}/{query}: HTTP {e.response.status_code}"
                ) from None
            results = body.get("results") or []
            log.info("adzuna %s %r page %d: %d jobs", country, query, page, len(results))
            yield from results
            if len(results) < RESULTS_PER_PAGE:
                break

    def normalize(self, raw: dict[str, Any]) -> Job:
        job = raw["job"]
        created = job.get("created")
        return Job(
            source=self.name,
            source_job_id=str(job["id"]),
            url=job["redirect_url"],
            title=html_to_text(job["title"]),
            company=(job.get("company") or {}).get("display_name", "").strip(),
            location=(job.get("location") or {}).get("display_name"),
            country=raw["country"],
            remote=None,  # not provided
            description=html_to_text(job.get("description") or ""),
            posted_at=datetime.fromisoformat(created) if created else None,
            raw=raw,
        )
