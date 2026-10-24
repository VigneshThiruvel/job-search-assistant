from datetime import UTC, datetime

import pytest
from test_tailor import PARENTS, SKILL_MAP, TEX, identity

from job_search_assistant import applications, daily, db, tailor
from job_search_assistant.applications import Claim
from job_search_assistant.models import Job

RESUME = """## Experience

- Implemented **Kafka-based** job processing — reducing queue latency by 20%.
"""
GUIDE = """# Cover letter guide

## Facts the agent may use beyond the résumé

- Expected graduation: Jan 2027

## Form

- Example: "20% lower queue latency" (not a fact source)
"""


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    for i in (1, 2):
        job = Job(
            source="arbeitnow", source_job_id=f"j{i}", url=f"https://example.com/{i}",
            title=f"Data Analyst {i}", company="ACME GmbH", location="Berlin", country=None,
            remote=None, description="d", posted_at=datetime(2026, 10, 1, tzinfo=UTC), raw={},
        )  # fmt: skip
        db.upsert_job(conn, job, lang="en", content_hash=f"h{i}", seen_at=datetime.now(UTC))
    yield conn
    conn.close()


def save_resume(conn, job_id: int) -> None:
    resume = tailor.parse(TEX)
    finder = tailor.SkillFinder(SKILL_MAP, PARENTS)
    applications.save_resume(conn, job_id, identity(resume), resume, finder)


def test_selecting_requests_a_resume_and_letters_only_on_request(conn) -> None:
    applications.queue(conn, 1, "apply", "Good fit.")
    with pytest.raises(ValueError, match="already"):
        applications.queue(conn, 1, "apply", "again")
    with pytest.raises(ValueError, match="no job"):
        applications.queue(conn, 99, "apply", "x")
    assert applications.needs(conn, 1) == (False, False)  # queued: nothing to do yet

    applications.select(conn, 1, note="stress Kafka")
    assert applications.needs(conn, 1) == (True, False)
    save_resume(conn, 1)
    assert applications.status_of(conn, 1) == "ready"
    with pytest.raises(ValueError, match="no open résumé request"):
        save_resume(conn, 1)  # answered; a redraft needs a new select

    applications.request_letter(conn, 1)  # later: I want a letter too
    assert applications.needs(conn, 1) == (False, True)  # the résumé is not redone
    claims = [Claim(claim="graduating soon", evidence="Expected graduation: Jan 2027")]
    applications.save_letter(conn, 1, "Dear Hiring Team", claims, applications.facts(GUIDE))
    assert applications.status_of(conn, 1) == "ready"

    applications.select(conn, 1, letter=True)  # redo both
    assert applications.needs(conn, 1) == (True, True)
    applications.close(conn, 1, "applied")  # I applied anyway, with the old drafts
    with pytest.raises(ValueError, match="only works on"):
        applications.close(conn, 1, "skipped")  # applied is final (Phase 7 adds outcomes)
    assert applications.items(conn)[0]["note"] == "stress Kafka"


def test_letter_without_selection_requests_a_resume_too(conn) -> None:
    applications.queue(conn, 2, "maybe", "Partial fit.")
    applications.request_letter(conn, 2)
    assert applications.needs(conn, 2) == (True, True)
    applications.close(conn, 2, "skipped", note="changed my mind")
    with pytest.raises(ValueError, match="I can close"):
        applications.close(conn, 2, "ready")


def test_claims_must_quote_the_resume_or_my_facts(conn) -> None:
    sources = RESUME + applications.facts(GUIDE)
    ok = [Claim(claim="Kafka", evidence="implemented Kafka-based job processing - reducing"),
          Claim(claim="graduation", evidence="expected graduation: jan 2027")]  # fmt: skip
    assert applications.check_claims(ok, sources) == []  # case, bold and dash variants ignored
    bad = [Claim(claim="Spark expert", evidence="5 years of Apache Spark")]
    assert "not in the résumé or my facts" in applications.check_claims(bad, sources)[0]
    example = [Claim(claim="fast", evidence="20% lower queue latency")]  # only in "## Form"
    assert applications.check_claims(example, applications.facts(GUIDE)) != []
    assert "no claims" in applications.check_claims([], sources)[0]


def test_letters_keep_history_and_export_writes_drafts(conn, tmp_path) -> None:
    claims = [Claim(claim="20% faster queues", evidence="reducing queue latency by 20%")]
    applications.queue(conn, 1, "apply", "Good fit.")
    with pytest.raises(ValueError, match="no letter was requested"):
        applications.save_letter(conn, 1, "Dear Hiring Team", claims, RESUME)
    applications.select(conn, 1, letter=True)
    with pytest.raises(ValueError, match="words"):
        applications.save_letter(conn, 1, "word " * 600, claims, RESUME)
    applications.save_letter(conn, 1, "First draft", claims, RESUME)
    applications.request_letter(conn, 1, note="shorter")
    applications.save_letter(conn, 1, "Second draft", claims, RESUME)

    item = applications.item(conn, 1)
    assert item["letter"] == "Second draft" and item["needs_resume"] is True
    assert conn.execute("SELECT count(*) FROM cover_letters").fetchone()[0] == 2
    paths, _ = applications.export(conn, 1, TEX, tmp_path)
    assert [p.name for p in paths] == ["cover_letter.md"]  # no résumé drafted yet
    assert "Second draft" in paths[0].read_text()


def test_review_shows_new_bullets_next_to_originals() -> None:
    resume = tailor.parse(TEX)
    t = identity(resume)
    t.entries[0].bullets = [tailor.Bullet(text="Cut queue latency by 20%.", sources=["E1.1"])]
    review = applications.resume_review(resume, t)
    assert "  • Cut queue latency by 20%.\n      was E1.1: Implemented Kafka-based" in review
    assert "Left out:\n  [E1.2] Maintained PostgreSQL databases." in review


def test_daily_status_counts_new_jobs_and_queue(conn, monkeypatch) -> None:
    monkeypatch.setattr(daily.search, "current_extraction", lambda: ("m", "v"))
    conn.execute("INSERT INTO job_groups VALUES (1, 1), (2, 1)")  # one job, two sources
    conn.execute("UPDATE jobs SET first_seen = '2026-10-01T08:00:00+00:00' WHERE id = 1")
    applications.queue(conn, 1, "apply", "Good fit.")
    assert daily.status(conn, "2026-10-01") == {
        "new_today": 1, "pending_extraction": 0, "queued": 1, "selected": 0, "ready": 0,
    }  # fmt: skip
    assert daily.status(conn, "2026-10-02")["new_today"] == 0  # first seen yesterday
