import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from job_search_assistant.http import PoliteFetcher
from job_search_assistant.sources.arbeitnow import API_URL, Arbeitnow
from job_search_assistant.sources.base import Source

FIXTURE = Path(__file__).parent / "fixtures" / "arbeitnow_page.json"


@pytest.fixture
def page() -> dict:
    return json.loads(FIXTURE.read_text())


def make_source(transport: httpx.MockTransport, tmp_path: Path) -> Arbeitnow:
    fetcher = PoliteFetcher(httpx.Client(transport=transport), sleep=lambda s: None)
    return Arbeitnow(fetcher, raw_dir=tmp_path, today=date(2026, 10, 14))


def test_is_a_source(tmp_path: Path) -> None:
    source: Source = make_source(httpx.MockTransport(lambda r: httpx.Response(500)), tmp_path)
    assert source.name == "arbeitnow"


def test_normalize(page: dict, tmp_path: Path) -> None:
    source = make_source(httpx.MockTransport(lambda r: httpx.Response(500)), tmp_path)
    raw = page["data"][0]
    job = source.normalize(raw)

    assert job.source == "arbeitnow"
    assert job.source_job_id == raw["slug"]
    assert job.company == raw["company_name"]
    assert job.posted_at == datetime.fromtimestamp(raw["created_at"], tz=UTC)
    assert "<" not in job.description and job.description
    assert job.raw is raw


def test_fetch_follows_next_and_caches(page: dict, tmp_path: Path) -> None:
    page1 = {**page, "links": {**page["links"], "next": API_URL + "?page=2"}}
    page2 = {**page, "data": page["data"][:1]}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=page2 if "page=2" in str(request.url) else page1)

    records = list(make_source(httpx.MockTransport(handler), tmp_path).fetch())
    assert len(records) == len(page["data"]) + 1
    assert len(calls) == 2

    # Second run the same day is served entirely from the cache.
    again = list(make_source(httpx.MockTransport(handler), tmp_path).fetch())
    assert again == records
    assert len(calls) == 2


def test_retries_on_429(page: dict, tmp_path: Path) -> None:
    responses = iter(
        [httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200, json=page)]
    )
    records = list(make_source(httpx.MockTransport(lambda r: next(responses)), tmp_path).fetch())
    assert len(records) == len(page["data"])
