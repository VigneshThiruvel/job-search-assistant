"""Bundesagentur für Arbeit Jobsuche (unofficial): https://github.com/bundesAPI/jobsuche-api

Search (v6) returns summaries without description; each posting needs a
second call to the details endpoint (v4), keyed by the base64-encoded refnr.
"""

import base64
import logging
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from job_search_assistant.http import PoliteFetcher
from job_search_assistant.models import Job

log = logging.getLogger(__name__)

BASE_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
SEARCH_URL = f"{BASE_URL}/pc/v6/jobs"
DETAILS_URL = f"{BASE_URL}/pc/v4/jobdetails/{{code}}"
HEADERS = {"X-API-Key": "jobboerse-jobsuche"}  # public client ID, not a secret
PAGE_SIZE = 100

COUNTRIES = {
    "DEUTSCHLAND": "de",
    "ÖSTERREICH": "at",
    "OESTERREICH": "at",
    "SCHWEIZ": "ch",
    "NIEDERLANDE": "nl",
    "BELGIEN": "be",
    "FRANKREICH": "fr",
    "LUXEMBURG": "lu",
    "DÄNEMARK": "dk",
    "DAENEMARK": "dk",
    "POLEN": "pl",
    "TSCHECHIEN": "cz",
}


class Bundesagentur:
    name = "bundesagentur"

    def __init__(
        self,
        fetcher: PoliteFetcher,
        raw_dir: Path,
        queries: Sequence[str],
        days: int = 14,
        max_pages: int = 2,
        today: date | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.queries = queries
        self.days = days
        self.max_pages = max_pages
        base = raw_dir / self.name
        self.search_dir = base / "search" / (today or date.today()).isoformat()
        self.details_dir = base / "details"

    def fetch(self) -> Iterator[dict[str, Any]]:
        seen: set[str] = set()  # the same posting matches several queries
        for query in self.queries:
            for item in self._search(query):
                refnr = item["referenznummer"]
                if refnr in seen:
                    continue
                seen.add(refnr)
                yield {"search": item, "details": self._details(item)}

    def _search(self, query: str) -> Iterator[dict[str, Any]]:
        for page in range(1, self.max_pages + 1):
            body = self.fetcher.get_json(
                SEARCH_URL,
                self.search_dir / f"{_slug(query)}-p{page}.json",
                headers=HEADERS,
                params={
                    "was": query,
                    "angebotsart": 1,  # regular employment (no training/internship)
                    "veroeffentlichtseit": self.days,
                    "page": page,
                    "size": PAGE_SIZE,
                },
            )
            items = body.get("ergebnisliste") or []
            log.info("bundesagentur %r page %d: %d jobs", query, page, len(items))
            yield from items
            if page * PAGE_SIZE >= body.get("maxErgebnisse", 0):
                break

    def _details(self, item: dict[str, Any]) -> dict[str, Any] | None:
        refnr = item["referenznummer"]
        # Key includes the change timestamp: an edited posting is refetched,
        # an unchanged one is never requested twice.
        changed = item.get("aenderungsdatum", "")
        code = base64.b64encode(refnr.encode()).decode()
        try:
            return self.fetcher.get_json(
                DETAILS_URL.format(code=code),
                self.details_dir / f"{_slug(refnr)}_{_slug(changed)}.json",
                headers=HEADERS,
            )
        except httpx.HTTPStatusError as e:
            log.warning("bundesagentur details %s: HTTP %d", refnr, e.response.status_code)
            return None

    def normalize(self, raw: dict[str, Any]) -> Job:
        search, details = raw["search"], raw["details"] or {}
        refnr = search["referenznummer"]
        address = (search.get("stellenlokationen") or [{}])[0].get("adresse", {})
        published = search.get("datumErsteVeroeffentlichung")
        return Job(
            source=self.name,
            source_job_id=refnr,
            url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}",
            title=search["stellenangebotsTitel"].strip(),
            company=(search.get("firma") or "").strip(),
            location=address.get("ort"),
            country=COUNTRIES.get((address.get("land") or "").upper()),
            # "Home office possible" -- weaker than fully remote, but the closest signal.
            remote=search.get("homeofficemoeglich"),
            description=(details.get("stellenangebotsBeschreibung") or "").strip(),
            posted_at=(
                datetime.fromisoformat(published).replace(tzinfo=UTC) if published else None
            ),
            raw=raw,
        )


def _slug(value: str) -> str:
    return re.sub(r"[^\w.-]+", "_", value).strip("_").lower()
