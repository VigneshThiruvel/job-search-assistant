import base64
import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from job_search_assistant.http import PoliteFetcher
from job_search_assistant.sources.bundesagentur import Bundesagentur

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def search() -> dict:
    return json.loads((FIXTURES / "bundesagentur_search.json").read_text())


@pytest.fixture
def details() -> dict:
    return json.loads((FIXTURES / "bundesagentur_details.json").read_text())


def make_source(handler, tmp_path: Path, queries=("Data Analyst",)) -> Bundesagentur:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = PoliteFetcher(client, sleep=lambda s: None)
    return Bundesagentur(fetcher, tmp_path, queries=queries, today=date(2026, 10, 14))


def api(search: dict, details: dict, calls: list[httpx.Request]):
    """Fake API: search returns the fixture; details exist only for the first job."""
    known = base64.b64encode(details["referenznummer"].encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/pc/v6/jobs"):
            return httpx.Response(200, json=search)
        if request.url.path.endswith(f"/jobdetails/{known}"):
            return httpx.Response(200, json=details)
        return httpx.Response(404)

    return handler


def test_fetch_combines_search_and_details(search, details, tmp_path) -> None:
    calls: list[httpx.Request] = []
    records = list(make_source(api(search, details, calls), tmp_path).fetch())

    assert len(records) == 3
    assert records[0]["details"] == details
    assert records[1]["details"] is None  # 404 is tolerated, not fatal
    assert all(c.headers["X-API-Key"] == "jobboerse-jobsuche" for c in calls)
    assert calls[0].url.params["was"] == "Data Analyst"


def test_overlapping_queries_and_reruns_do_not_refetch(search, details, tmp_path) -> None:
    calls: list[httpx.Request] = []
    handler = api(search, details, calls)
    queries = ("Data Analyst", "Data Scientist")

    records = list(make_source(handler, tmp_path, queries).fetch())
    assert len(records) == 3  # second query returns the same postings
    assert len(calls) == 2 + 3  # two searches, three detail lookups

    list(make_source(handler, tmp_path, queries).fetch())
    # Re-run: searches and successful details are cached; only the two 404s retry.
    assert len(calls) == 5 + 2


def test_normalize(search, details, tmp_path) -> None:
    source = make_source(lambda r: httpx.Response(500), tmp_path)
    job = source.normalize({"search": search["ergebnisliste"][0], "details": details})

    assert job.source_job_id == details["referenznummer"]
    assert job.url.endswith(details["referenznummer"])
    assert job.company == details["firma"]
    assert job.country == "de"
    assert job.description.startswith(details["stellenangebotsBeschreibung"][:30])
    assert job.posted_at == datetime.fromisoformat(details["datumErsteVeroeffentlichung"]).replace(
        tzinfo=UTC
    )

    no_details = source.normalize({"search": search["ergebnisliste"][1], "details": None})
    assert no_details.description == ""
