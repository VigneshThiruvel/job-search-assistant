import json
import sys
import time

import pytest

from job_search_assistant import db, runner


@pytest.fixture
def runs(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    db.connect(path).close()
    return runner.Runner(lambda: db.connect(path), tmp_path / "runs"), path


def wait(r: runner.Runner, run_id: int, path, timeout: float = 10) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = db.connect(path).execute("SELECT status FROM ui_runs WHERE id = ?",
                                          (run_id,)).fetchone()[0]  # fmt: skip
        if status != "running":
            return status
        time.sleep(0.05)
    raise TimeoutError


def test_runs_log_output_and_one_at_a_time(runs, monkeypatch) -> None:
    r, path = runs
    script = "import time; print('hello'); time.sleep(0.5); print('bye')"
    monkeypatch.setattr(runner, "command", lambda *a: [sys.executable, "-c", script])
    run_id = r.start("fetch")
    with pytest.raises(RuntimeError, match="still running"):
        r.start("embed")
    assert wait(r, run_id, path) == "done"
    lines, offset = r.log(run_id)
    assert lines[1:] == ["hello", "bye"] and offset == 3
    assert r.log(run_id, offset) == ([], 3)  # nothing new since


def test_cancel_kills_the_process_group(runs, monkeypatch) -> None:
    r, path = runs
    monkeypatch.setattr(
        runner, "command", lambda *a: [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    run_id = r.start("fetch")
    time.sleep(0.3)
    assert r.cancel(run_id)
    assert wait(r, run_id, path) == "cancelled"


def test_restart_marks_stale_runs_interrupted(runs) -> None:
    r, path = runs
    conn = db.connect(path)
    with conn:
        conn.execute("INSERT INTO ui_runs (kind, args_json, status, started_at) "
                     "VALUES ('fetch', '{}', 'running', 'x')")  # fmt: skip
    runner.Runner(lambda: db.connect(path), r.runs_dir)
    assert conn.execute("SELECT status FROM ui_runs").fetchone()[0] == "interrupted"


def test_claude_is_locked_down_to_job_search_tools(tmp_path) -> None:
    cmd = runner.claude_command(tmp_path / "claude", "do it")
    joined = " ".join(cmd)
    assert "--tools  " in joined or cmd[cmd.index("--tools") + 1] == ""  # no built-in tools
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__job-search__*"
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"


def test_stream_json_becomes_readable_lines() -> None:
    init = {
        "type": "system",
        "subtype": "init",
        "model": "m",
        "tools": ["mcp__job-search__get_job", "Bash"],
    }
    use = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "mcp__job-search__save_extraction",
                    "input": {"job_id": 7, "extraction": {"x": 1}},
                }
            ]
        },
    }
    err = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "is_error": True, "content": "stale prompt_version"}
            ]
        },
    }
    done = {"type": "result", "subtype": "success", "duration_ms": 4200, "num_turns": 3,
            "result": "## Report"}  # fmt: skip
    assert runner.render_event(json.dumps(init)) == (
        ["Claude Code started (m), 1 job-search tools"],
        None,
    )
    assert runner.render_event(json.dumps(use))[0] == ["→ save_extraction(job_id=7)"]
    assert runner.render_event(json.dumps(err))[0] == ["✗ stale prompt_version"]
    assert runner.render_event(json.dumps(done)) == (["✓ finished after 4 s, 3 turns"], "## Report")


def test_mail_run_fetches_only_alert_emails() -> None:
    assert runner.command("mail")[-1] == "--mail-only"
    assert runner.KINDS["mail"] == "Check mail"
