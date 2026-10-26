from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_applications import save_resume
from test_inbox import DESCRIPTION

from job_search_assistant import applications, db, inbox
from job_search_assistant.api import create_app
from job_search_assistant.models import Job

TEX = Path(__file__).parent / "fixtures/tailor/resume.tex"
ORIGIN = {"Origin": "http://localhost:5173"}


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "jobs.db"
    conn = db.connect(path)
    for source, sid, title in (("arbeitnow", "a1", "Data Analyst"),
                               ("linkedin", "l2", "Junior Data Engineer"),
                               ("arbeitnow", "a3", "Data Engineer")):  # fmt: skip
        job = Job(
            source=source, source_job_id=sid, url=f"https://example.com/{sid}", title=title,
            company="ACME", location="Berlin", country=None, remote=None,
            description=DESCRIPTION if source == "arbeitnow" else "",
            posted_at=datetime.now(UTC), raw={},
        )  # fmt: skip
        db.upsert_job(conn, job, lang="en", content_hash=sid, seen_at=datetime.now(UTC))
    # Job 3 is the same job as the LinkedIn alert 2: it belongs to the alerts list.
    conn.executemany("INSERT INTO job_groups VALUES (?, ?)", [(1, 1), (2, 2), (3, 2)])
    conn.commit()
    applications.queue(conn, 1, "maybe", "Partial fit.")
    applications.queue(conn, 3, "apply", "Strong fit.")
    conn.close()

    box = tmp_path / "inbox"
    box.mkdir()
    (box / "2-acme.txt").write_text(f"# header\nExpired: no\nNotes:\n\n{inbox.MARKER}\n\n")
    app = create_app(lambda: db.connect(path), resume_tex=TEX, inbox_dir=box,
                     drafts_dir=tmp_path / "drafts", hosts=["testserver"])  # fmt: skip
    return TestClient(app), path, box


def test_queue_lists_and_channels(setup) -> None:
    client, _, _ = setup
    cards = client.get("/api/applications", params={"status": "queued"}).json()
    by_id = {c["job_id"]: c for c in cards}
    assert by_id[1]["channel"] == "other" and by_id[1]["apply_url"] == "https://example.com/a1"
    assert by_id[3]["channel"] == "alerts" and by_id[3]["apply_url"] == "https://example.com/l2"
    assert client.get("/api/status").json()["queued"] == 2


def test_my_moves_go_through_the_queue_rules(setup) -> None:
    client, path, _ = setup
    r = client.post("/api/applications/3/select", json={"letter": False, "note": "stress SQL"},
                    headers=ORIGIN)  # fmt: skip
    assert r.status_code == 200 and r.json()["status"] == "selected"
    assert r.json()["needs_resume"] is True and r.json()["needs_letter"] is False

    r = client.post("/api/applications/99/skip", json={}, headers=ORIGIN)
    assert r.status_code == 409 and "not in the queue" in r.json()["detail"]
    client.post("/api/applications/1/applied", json={}, headers=ORIGIN)
    r = client.post("/api/applications/1/skip", json={}, headers=ORIGIN)
    assert r.status_code == 409  # applied is final

    conn = db.connect(path)
    save_resume(conn, 3)
    conn.close()
    detail = client.get("/api/applications/3").json()
    assert detail["card"]["status"] == "ready" and detail["card"]["note"] == "stress SQL"
    first = detail["resume"]["entries"][0]["bullets"][0]
    assert first["sources"][0]["id"] == "E1.1" and first["text"] == first["sources"][0]["text"]
    assert detail["resume"]["skills"] is None  # order unchanged
    assert client.get("/api/applications/42").status_code == 404


def test_pasting_in_the_ui_replaces_the_file(setup) -> None:
    client, path, box = setup
    assert [i["job_id"] for i in client.get("/api/inbox").json()] == [2]
    assert client.get("/api/status").json()["inbox_open"] == 1

    r = client.post("/api/inbox/2", json={"description": "too short"}, headers=ORIGIN)
    assert r.status_code == 409 and "too short" in r.json()["detail"]
    r = client.post("/api/inbox/2", json={"description": DESCRIPTION, "notes": "recruiter"},
                    headers=ORIGIN)  # fmt: skip
    assert r.status_code == 200
    assert client.get("/api/inbox").json() == []
    assert (box / "done" / "2-acme.txt").exists()
    conn = db.connect(path)
    assert conn.execute("SELECT description FROM jobs WHERE id = 2").fetchone()[0]
    assert inbox.my_note(conn, 2) == ("recruiter", False)


def test_cross_site_writes_and_foreign_hosts_are_blocked(setup) -> None:
    client, _, _ = setup
    # A form on another site: no allowed Origin, not JSON.
    r = client.post("/api/applications/1/skip", data={"note": "x"},
                    headers={"Origin": "https://evil.example"})  # fmt: skip
    assert r.status_code == 403
    r = client.post("/api/applications/1/skip", json={})  # no Origin at all
    assert r.status_code == 403
    r = client.post("/api/applications/1/skip", data={"note": "x"}, headers=ORIGIN)  # form body
    assert r.status_code == 403
    # DNS rebinding: a request whose Host is someone else's domain.
    r = client.get("/api/applications", headers={"Host": "evil.example"})
    assert r.status_code == 400


class FakeRuns:
    def __init__(self, path):
        self.path, self.started = path, []

    def start(self, kind, k, max_extract):
        if kind == "agent_prepare":
            raise ValueError("Claude Code not found")
        conn = db.connect(self.path)
        with conn:
            run_id = conn.execute(
                "INSERT INTO ui_runs (kind, args_json, status, started_at) "
                "VALUES (?, '{}', 'running', 'now')",
                (kind,),
            ).lastrowid
        self.started.append((kind, k, max_extract))
        return run_id

    def log(self, run_id, offset=0):
        return ["line 1", "line 2"][offset:], 2

    def cancel(self, run_id):
        return True


def test_runs_fit_and_skills_endpoints(setup, tmp_path) -> None:
    _, path, box = setup
    runs = FakeRuns(path)

    def fake_graph(kind, scope, k, **params):
        if kind == "coverage_of":
            return [{"job_id": 3, "required": 2, "coverage": 0.5, "missing": ["dbt"]}]
        if kind == "fit":
            return [{"skill": "SQL", "category": "programming", "required": True,
                     "my_credit": 1.0, "via": ["SQL"]}]  # fmt: skip
        raise ConnectionError("Neo4j down")

    taxonomy = tmp_path / "skills.yaml"
    taxonomy.write_text("skills:\n  SQL:\n    category: programming\n    parent: null\n"
                        "    aliases: [sql]\ndropped: []\n")  # fmt: skip
    app = create_app(lambda: db.connect(path), resume_tex=TEX, inbox_dir=box, hosts=["testserver"],
                     runs=runs, graph_query=fake_graph, taxonomy=taxonomy)  # fmt: skip
    client = TestClient(app)

    r = client.post("/api/runs", json={"kind": "agent_daily", "k": 5}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["label"] == "Daily agent"
    assert runs.started == [("agent_daily", 5, 30)]
    assert (
        client.post("/api/runs", json={"kind": "agent_prepare"}, headers=ORIGIN).status_code == 400
    )
    assert client.post("/api/runs", json={"kind": "rm -rf"}, headers=ORIGIN).status_code == 422
    log = client.get(f"/api/runs/{r.json()['id']}", params={"offset": 1}).json()
    assert log["lines"] == ["line 2"] and log["next_offset"] == 2
    assert client.get("/api/health").json()["running"]["kind"] == "agent_daily"

    assert client.get("/api/coverage", params={"ids": "3,1"}).json() == {
        "3": {"coverage": 0.5, "missing": ["dbt"]}
    }
    fit = client.get("/api/applications/3/fit").json()
    assert fit["available"] and fit["coverage"] == 1.0
    assert client.get("/api/skills/insights").json() == {"available": False, "rows": []}

    conn = db.connect(path)
    with conn:
        conn.execute("INSERT INTO skills VALUES ('SQL', 'programming', NULL)")
        conn.execute("INSERT INTO skill_map VALUES ('sql', 'SQL', 'x'), ('dbt core', NULL, 'x')")
    review = client.get("/api/skills/review").json()
    assert review == {"new": [{"raw": "dbt core", "skill": None, "category": None,
                               "parent": None}], "unimported": []}  # fmt: skip
    taxonomy.write_text(taxonomy.read_text().replace("programming", "database"))  # my edit
    r = client.post("/api/skills/export", json={}, headers=ORIGIN)
    assert r.status_code == 409 and "not imported" in r.json()["detail"]


def test_agent_run_summary_counts_the_queue(setup) -> None:
    from job_search_assistant.api import run_summary

    _, path, _ = setup
    conn = db.connect(path)
    with conn:
        conn.execute(
            "INSERT INTO ui_runs (kind, args_json, status, started_at, finished_at) "
            "VALUES ('agent_daily', '{}', 'done', '2000-01-01', '2999-01-01')"
        )
    run = conn.execute("SELECT * FROM ui_runs").fetchone()
    # The fixture queued job 1 (Arbeitnow only) and job 3 (also a LinkedIn alert).
    assert run_summary(conn, run) == "2 job(s) queued: 1 from LinkedIn/XING, 1 from other sites"


def test_today_queue_hides_old_jobs_and_can_dismiss_them(setup) -> None:
    client, path, _ = setup
    conn = db.connect(path)
    with conn:  # job 1 was queued two days ago; job 3 today
        conn.execute("UPDATE applications SET queued_at = '2026-01-01T09:00:00+00:00' "
                     "WHERE job_id = 1")  # fmt: skip
    queue = client.get("/api/queue").json()
    assert [c["job_id"] for c in queue["cards"]] == [3] and queue["older"] == 1
    assert client.get("/api/status").json()["queued_today"] == 1

    with conn:  # an old ad is hidden even if it was queued today
        conn.execute("UPDATE jobs SET posted_at = '2026-01-01T09:00:00+00:00' WHERE id IN (2, 3)")
    assert client.get("/api/queue").json() == {"cards": [], "older": 2}

    r = client.post("/api/queue/dismiss-older", json={}, headers=ORIGIN)
    assert r.json()["detail"] == "dismissed 2 older job(s)"
    assert applications.status_of(conn, 1) == "skipped"
    assert applications.items(conn, "skipped")[0]["note"] == "not reviewed while fresh"
