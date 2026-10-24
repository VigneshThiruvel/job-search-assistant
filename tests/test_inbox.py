from datetime import UTC, datetime

import pytest

from job_search_assistant import db, evalset, inbox
from job_search_assistant.models import Job

DESCRIPTION = "We are looking for a junior data engineer to build pipelines in Python and SQL. " * 8


def add(conn, source: str, sid: str, title: str, description: str = "", company: str = "ACME"):
    job = Job(
        source=source, source_job_id=sid, url=f"https://example.com/{source}/{sid}", title=title,
        company=company, location="Berlin", country=None, remote=None, description=description,
        posted_at=datetime.now(UTC), raw={},
    )  # fmt: skip
    db.upsert_job(conn, job, lang=None, content_hash=sid, seen_at=datetime.now(UTC))
    return conn.execute("SELECT max(id) FROM jobs").fetchone()[0]


@pytest.fixture
def setup():
    conn = db.connect(":memory:")
    ids = {
        "li": add(conn, "linkedin", "1", "Junior Data Engineer"),
        "xing_twin": add(conn, "xing", "x1", "Junior Data Engineer"),  # same job, other alert
        "li_with_text": add(conn, "linkedin", "2", "Data Analyst", company="Beta"),
        "arbeitnow_twin": add(conn, "arbeitnow", "a2", "Data Analyst", DESCRIPTION, "Beta"),
        "senior": add(conn, "linkedin", "3", "Senior Data Engineer", company="Gamma"),
        "other": add(conn, "arbeitnow", "a4", "Data Scientist", DESCRIPTION, "Delta"),
    }
    groups = [(ids["li"], ids["li"]), (ids["xing_twin"], ids["li"]),
              (ids["li_with_text"], ids["li_with_text"]),
              (ids["arbeitnow_twin"], ids["li_with_text"]),
              (ids["senior"], ids["senior"]), (ids["other"], ids["other"])]  # fmt: skip
    conn.executemany("INSERT INTO job_groups (job_id, group_id) VALUES (?, ?)", groups)
    conn.commit()
    yield conn, ids
    conn.close()


def test_templates_only_for_alert_jobs_without_any_text(setup, tmp_path) -> None:
    conn, ids = setup
    written = inbox.write_templates(conn, tmp_path)
    # The xing twin shares the group; the Beta job has an Arbeitnow twin with text; senior out.
    assert [p.name for p in written] == [f"{ids['li']}-acme.txt"]
    assert inbox.MARKER in written[0].read_text()
    assert "https://example.com/linkedin/1" in written[0].read_text()
    assert inbox.write_templates(conn, tmp_path) == []  # existing files are left alone


def test_pasted_text_survives_reingest_and_enters_target_scope(setup, tmp_path) -> None:
    conn, ids = setup
    [template] = inbox.write_templates(conn, tmp_path)
    template.write_text(template.read_text() + DESCRIPTION)
    (tmp_path / "untouched-template.txt").write_text(f"# x\n{inbox.MARKER}\n")
    (tmp_path / f"{ids['other']}-delta.txt").write_text(DESCRIPTION)

    imported, problems = inbox.import_inbox(conn, tmp_path)
    assert imported == [ids["li"]]
    assert problems == [
        f"{ids['other']}-delta.txt: job {ids['other']} is from arbeitnow, not an alert",
        "untouched-template.txt: name must start with the job id",
    ]
    assert (tmp_path / "done" / template.name).exists() and not template.exists()
    row = conn.execute("SELECT description, lang FROM jobs WHERE id = ?", (ids["li"],)).fetchone()
    assert row["description"] == DESCRIPTION.strip() and row["lang"] == "en"

    add(conn, "linkedin", "1", "Junior Data Engineer")  # the alert email is read again
    description = "SELECT description FROM jobs WHERE id = ?"
    assert conn.execute(description, (ids["li"],)).fetchone()[0] == ""
    assert inbox.apply_pasted(conn) == 1
    assert inbox.needs_description(conn) == []  # the senior one never needed it

    target = {j["id"] for j in evalset.target_jobs(conn)}
    # Pasted alert job, the Arbeitnow twin standing in for its LinkedIn alert, and Delta.
    assert target == {ids["li"], ids["arbeitnow_twin"], ids["other"]}
    # The eval sampling view is unchanged: representatives from Arbeitnow/BA only.
    assert {j["id"] for j in evalset.candidates(conn)} == {ids["other"]}


def test_channels_follow_the_duplicate_group(setup) -> None:
    conn, ids = setup
    assert inbox.channels(conn, [ids["arbeitnow_twin"], ids["other"], ids["li"]]) == {
        ids["arbeitnow_twin"]: "alerts",  # also came in a LinkedIn alert: apply there
        ids["other"]: "other",
        ids["li"]: "alerts",
    }
