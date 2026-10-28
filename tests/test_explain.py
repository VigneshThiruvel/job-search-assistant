"""search.explain must agree with search.filter_sql: the UI's reasons are the ranking's rules."""

import itertools
from datetime import UTC, datetime

from test_extract import LABELS

from job_search_assistant import db, extract, inbox, search
from job_search_assistant.models import Job

EXTRACTION = ("claude-code/claude-opus-5-5", "v1")
VARIANTS = {
    "is_target_role": [True, False],
    "seniority": ["junior", "senior", "working_student"],
    "german_level": ["plus", "basic", "fluent"],
    "years_experience_min": [None, 1, 5],
    "other_languages": [[], [{"language": "Dutch", "level": "plus"}],
                        [{"language": "French", "level": "fluent"}]],
}  # fmt: skip


def add(conn, sid: str, source: str, title: str, location: str, description: str = "d" * 500):
    job = Job(source=source, source_job_id=sid, url=f"https://x/{sid}", title=title,
              company=f"C{sid}", location=location, country=None, remote=None,
              description=description, posted_at=datetime.now(UTC), raw={})  # fmt: skip
    db.upsert_job(conn, job, lang="en", content_hash=sid, seen_at=datetime.now(UTC))
    return conn.execute("SELECT max(id) FROM jobs").fetchone()[0]


def test_explain_agrees_with_the_sql_filter() -> None:
    conn = db.connect(":memory:")
    cases = {}
    for n, combo in enumerate(itertools.product(*VARIANTS.values())):
        fields = dict(zip(VARIANTS, combo, strict=True))
        location = "London" if n % 7 == 0 else "Berlin"
        job_id = add(conn, str(n), "arbeitnow", "Data Analyst", location)
        x = extract.Extraction.model_validate({**LABELS, **fields})
        extract.store_extraction(conn, job_id, x, model=EXTRACTION[0], version=EXTRACTION[1])
        cases[job_id] = (x.model_dump(), location)
    unextracted = add(conn, "u", "arbeitnow", "Data Analyst", "Berlin")
    eligible = search.eligible_job_ids(conn, search.MY_FILTERS, EXTRACTION)
    for job_id, (x, location) in cases.items():
        reasons = search.explain(x, "Data Analyst", location)
        assert (reasons == []) == (job_id in eligible), (x, location, reasons)
    assert search.explain(None, "Data Analyst", "Berlin") == ["not extracted yet"]
    assert unextracted not in eligible

    # Expired (marked on a LinkedIn copy): the whole group drops out, extraction stays.
    passing = next(i for i in cases if i in eligible)
    copy = add(conn, "copy", "linkedin", "Data Analyst", "Berlin", description="")
    conn.executemany("INSERT INTO job_groups VALUES (?, ?)", [(passing, passing), (copy, passing)])
    conn.execute("INSERT INTO pasted_descriptions (job_id, text, expired, pasted_at) "
                 "VALUES (?, '', 1, 'now')", (copy,))  # fmt: skip
    assert passing not in search.eligible_job_ids(conn, search.MY_FILTERS, EXTRACTION)
    x, location = cases[passing]
    assert search.explain(x, "Data Analyst", location, expired=True) == ["marked expired"]
    x = {**cases[next(iter(cases))][0], "german_level": "fluent", "seniority": "senior"}
    assert search.explain(x, "Data Analyst", "Berlin") == ["senior", "needs fluent German"]


def test_alert_funnel_stages() -> None:
    conn = db.connect(":memory:")
    ok = add(conn, "1", "linkedin", "Junior Data Analyst", "Berlin")
    german = add(conn, "2", "linkedin", "Data Engineer", "Berlin")
    unpasted = add(conn, "3", "xing", "Data Scientist", "Hamburg", description="")
    waiting = add(conn, "4", "linkedin", "Data Analyst", "Munich")  # has text, not extracted
    other = add(conn, "5", "xing", "Junior Controller", "Berlin", description="")
    twin = add(conn, "6", "arbeitnow", "Data Scientist", "Köln")  # Arbeitnow-only: not alerts
    conn.executemany("INSERT INTO job_groups VALUES (?, ?)",
                     [(i, i) for i in (ok, german, unpasted, waiting, other, twin)])  # fmt: skip
    for job_id, german_level in ((ok, "basic"), (german, "fluent")):
        x = extract.Extraction.model_validate({**LABELS, "german_level": german_level,
                                               "other_languages": []})  # fmt: skip
        extract.store_extraction(conn, job_id, x, model=EXTRACTION[0], version=EXTRACTION[1])
    from job_search_assistant import applications

    applications.queue(conn, ok, "apply", "Good fit.")
    funnel = inbox.alert_funnel(conn, EXTRACTION)
    stages = {j["job_id"]: (j["stage"], j["reasons"]) for j in funnel["jobs"]}
    assert stages == {
        ok: ("queued", ["queued · apply"]),
        german: ("filtered", ["needs fluent German"]),
        unpasted: ("needs_description", []),
        waiting: ("not_extracted", []),
    }
    assert funnel["other_titles"] == 1  # the controller job never needed a description
