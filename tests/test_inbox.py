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
        "student": add(conn, "xing", "x5", "Werkstudent Data Analytics", company="Eps"),
        "old_mail": add(conn, "linkedin", "6", "Junior Data Analyst", company="Zeta"),
    }
    conn.execute("UPDATE jobs SET posted_at = '2026-01-01T08:00:00+00:00' WHERE id = ?",
                 (ids["old_mail"],))  # fmt: skip
    groups = [(ids["li"], ids["li"]), (ids["xing_twin"], ids["li"]),
              (ids["li_with_text"], ids["li_with_text"]),
              (ids["arbeitnow_twin"], ids["li_with_text"]),
              (ids["senior"], ids["senior"]), (ids["other"], ids["other"]),
              (ids["student"], ids["student"]), (ids["old_mail"], ids["old_mail"])]  # fmt: skip
    conn.executemany("INSERT INTO job_groups (job_id, group_id) VALUES (?, ?)", groups)
    conn.commit()
    yield conn, ids
    conn.close()


def test_templates_only_for_alert_jobs_without_any_text(setup, tmp_path) -> None:
    conn, ids = setup
    written = inbox.write_templates(conn, tmp_path)
    # The xing twin shares the group; the Beta job has an Arbeitnow twin with text; senior
    # and working-student titles are out of scope; the January email is not fresh.
    assert [p.name for p in written] == [f"{ids['li']}-acme.txt"]
    assert inbox.MARKER in written[0].read_text()
    assert "https://example.com/linkedin/1" in written[0].read_text()
    assert inbox.write_templates(conn, tmp_path) == []  # existing files are left alone
    assert inbox.is_untouched(written[0])


def test_pasted_text_survives_reingest_and_enters_target_scope(setup, tmp_path) -> None:
    conn, ids = setup
    template = inbox.write_templates(conn, tmp_path)[0]
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
    assert inbox.needs_description(conn) == []  # pasted; the January one is not fresh

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


def test_a_fresh_job_missed_by_an_earlier_run_still_gets_a_file(setup, tmp_path) -> None:
    conn, ids = setup
    # Runs happened after the email arrived (e.g. the title rule matched only later):
    # freshness, not the time of the last run, decides.
    conn.execute("INSERT INTO daily_runs (started_at) VALUES (?)", (datetime.now(UTC).isoformat(),))
    assert [r["id"] for r in inbox.needs_description(conn)] == [ids["li"]]


def test_notes_and_expired_lines(setup, tmp_path) -> None:
    conn, ids = setup
    note = "Author note: closed, keep for the dataset"
    old_style = f"# header\n{note}\n{inbox.MARKER}\n{DESCRIPTION}"
    assert inbox.parse(old_style) == (DESCRIPTION.strip(), note, False)
    new_style = f"Expired: yes\nNotes:\nposting gone\n{inbox.MARKER}\n\n"
    assert inbox.parse(new_style) == ("", "posting gone", True)

    # Expired with no description: imported (closes the job), nothing pasted into `jobs`.
    (tmp_path / f"{ids['li']}-acme.txt").write_text(new_style)
    assert inbox.import_inbox(conn, tmp_path) == ([ids["li"]], [])
    assert (
        conn.execute("SELECT description FROM jobs WHERE id = ?", (ids["li"],)).fetchone()[0] == ""
    )
    # Every copy counts as expired (the XING alert for the same job too).
    assert inbox.expired_job_ids(conn) == {ids["li"], ids["xing_twin"]}
    assert inbox.my_note(conn, ids["xing_twin"]) == ("posting gone", True)
    assert inbox.my_note(conn, ids["other"]) == (None, False)


def test_alert_jobs_are_fresh_by_email_date(setup) -> None:
    conn, ids = setup
    fresh = inbox.fresh_job_ids(conn)
    assert ids["li"] in fresh and ids["other"] in fresh
    # First seen today (the email was first read today), but the email is from January.
    assert ids["old_mail"] not in fresh


def test_old_ads_are_not_fresh_even_if_first_seen_today(setup) -> None:
    conn, ids = setup
    # Arbeitnow lists an ad posted 9 days ago; we first saw it today.
    conn.execute("UPDATE jobs SET posted_at = '2026-01-10T08:00:00+00:00' WHERE id = ?",
                 (ids["arbeitnow_twin"],))  # fmt: skip
    fresh = inbox.fresh_job_ids(conn)
    assert ids["arbeitnow_twin"] not in fresh
    # Its LinkedIn alert came today, but it's the same old ad: the earliest copy counts.
    assert ids["li_with_text"] not in fresh
    assert ids["other"] in fresh
