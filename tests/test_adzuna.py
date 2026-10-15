import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from job_search_assistant.http import PoliteFetcher
from job_search_assistant.sources.adzuna import Adzuna

# Trimmed real response; app_id in redirect_url replaced with "APP_ID".
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

    assert len(records) == len(page["results"])
    assert {r["country"] for r in records} == {"de"}  # "at" returned the same ids
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
    raw = page["results"][0]
    job = source.normalize({"country": "de", "job": raw})

    assert job.source_job_id == str(raw["id"])
    assert job.url == raw["redirect_url"]
    assert job.title == raw["title"]
    assert job.company == raw["company"]["display_name"]
    assert job.location == raw["location"]["display_name"]
    assert job.country == "de"
    assert job.description and "<" not in job.description
    assert job.posted_at == datetime.fromisoformat(raw["created"])
    assert job.posted_at.tzinfo == UTC


def test_normalize_tolerates_missing_company(page, tmp_path) -> None:
    source = make_source(lambda r: httpx.Response(500), tmp_path)
    raw = {k: v for k, v in page["results"][0].items() if k != "company"}
    assert source.normalize({"country": "de", "job": raw}).company == ""
