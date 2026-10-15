import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from job_search_assistant.http import PoliteFetcher
from job_search_assistant.sources.adzuna import Adzuna

# Hand-written from https://developer.adzuna.com/docs/search (no live credentials yet).
FIXTURE = Path(__file__).parent / "fixtures" / "adzuna_search.json"


@pytest.fixture
def page() -> dict:
    return json.loads(FIXTURE.read_text())


def make_source(handler, tmp_path: Path, countries=("de",)) -> Adzuna:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Adzuna(
        PoliteFetcher(client, sleep=lambda s: None),
        tmp_path,
        app_id="id",
        app_key="secret-key",
        queries=("Data Analyst",),
        countries=countries,
        today=date(2026, 10, 14),
    )


def test_fetch_tags_country_and_dedupes(page, tmp_path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=page)

    records = list(make_source(handler, tmp_path, countries=("de", "at")).fetch())

    assert [r["country"] for r in records] == ["de", "de"]  # "at" returned the same ids
    assert calls[0].url.path == "/v1/api/jobs/de/search/1"
    assert calls[0].url.params["what_phrase"] == "Data Analyst"
    assert len(calls) == 2  # short page -> no request for page 2


def test_http_error_hides_api_key(tmp_path) -> None:
    source = make_source(lambda r: httpx.Response(401), tmp_path)
    with pytest.raises(RuntimeError) as exc:
        list(source.fetch())
    assert "secret-key" not in str(exc.value)


def test_normalize(page, tmp_path) -> None:
    source = make_source(lambda r: httpx.Response(500), tmp_path)
    job = source.normalize({"country": "de", "job": page["results"][0]})

    assert job.source_job_id == "4711000001"
    assert job.title == "Junior Data Analyst (m/w/d)"
    assert job.company == "Example Analytics GmbH"
    assert job.country == "de"
    assert job.description.endswith("Munich…")
    assert job.posted_at == datetime(2026, 10, 12, 8, 15, tzinfo=UTC)

    no_company = source.normalize({"country": "de", "job": page["results"][1]})
    assert no_company.company == ""
