from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from job_search_assistant import db
from job_search_assistant.ingest import run_source
from job_search_assistant.models import Job


class FakeSource:
    """Satisfies the Source protocol without any network."""

    name = "fake"

    def __init__(self, records: list[dict[str, Any]], fail_fetch: bool = False) -> None:
        self.records = records
        self.fail_fetch = fail_fetch

    def fetch(self) -> Iterator[dict[str, Any]]:
        yield from self.records
        if self.fail_fetch:
            raise ConnectionError("boom")

    def normalize(self, raw: dict[str, Any]) -> Job:
        return Job(
            source=self.name,
            source_job_id=raw["id"],
            url=f"https://example.com/{raw['id']}",
            title=raw["title"],
            company="ACME",
            location="Berlin",
            country="de",
            remote=None,
            description="We are hiring a junior data analyst.",
            posted_at=datetime(2026, 10, 1, tzinfo=UTC),
            raw=raw,
        )


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    yield conn
    conn.close()


def rows(conn):
    return conn.execute("SELECT * FROM jobs ORDER BY source_job_id").fetchall()


def test_rerun_is_idempotent(conn) -> None:
    records = [{"id": "1", "title": "Data Analyst"}, {"id": "2", "title": "Data Engineer"}]

    assert run_source(conn, FakeSource(records)) == {"fetched": 2, "new": 2, "errors": 0}
    first = rows(conn)

    assert run_source(conn, FakeSource(records)) == {"fetched": 2, "new": 0, "errors": 0}
    second = rows(conn)

    assert len(second) == 2
    assert [r["first_seen"] for r in second] == [r["first_seen"] for r in first]
    assert all(s["last_seen"] > f["last_seen"] for s, f in zip(second, first, strict=True))
    assert second[0]["lang"] == "en"


def test_duplicate_within_one_run_counts_once(conn) -> None:
    records = [{"id": "1", "title": "Data Analyst"}, {"id": "1", "title": "Data Analyst"}]
    assert run_source(conn, FakeSource(records))["new"] == 1


def test_bad_record_and_fetch_failure_are_counted_not_fatal(conn) -> None:
    records = [{"id": "1", "title": "Data Analyst"}, {"no_id": True}]
    stats = run_source(conn, FakeSource(records, fail_fetch=True))

    assert stats == {"fetched": 2, "new": 0, "errors": 2}  # fetch failure rolls back the source
    run = conn.execute("SELECT * FROM runs").fetchone()
    assert (run["fetched"], run["new"], run["errors"]) == (2, 0, 2)
    assert run["finished_at"] is not None
