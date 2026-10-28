"""Phase 7: background runs started from the UI — the pipeline and headless Claude Code.

    fetch          python -m job_search_assistant.daily               (ingest, inbox, embed)
    mail           python -m job_search_assistant.daily --mail-only   (alert emails only)
    embed          python -m job_search_assistant.daily --skip-ingest (after pasting)
    agent_daily    claude -p <prompts.daily_run>                      (extract, rank, queue)
    agent_prepare  claude -p <prompts.prepare_applications>           (résumés, letters)
    neo4j          start Docker Desktop if needed, docker compose up -d, wait for Neo4j

Headless Claude Code is the same agent as the slash commands, with the same prompt
text (prompts.py), but locked down harder, since nobody watches it:
  --tools ""                 no built-in tools at all: no shell, no file edits, no web;
  --strict-mcp-config        only the servers in .mcp.json (just job-search);
  --allowedTools mcp__job-search__*  and --permission-mode dontAsk: anything else is
                             refused instead of waiting for a permission prompt.
So a prompt-injected posting can at most add rows to my review queue.

One run at a time (two writers would fight over SQLite; two agents over the queue).
Each run logs to data/runs/<id>.log; Claude's stream-json is rendered as readable lines.
"""

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from job_search_assistant import prompts

ROOT = Path(__file__).resolve().parents[2]
RUNS = Path("data/runs")
Kind = Literal["fetch", "mail", "embed", "agent_daily", "agent_prepare", "neo4j"]
KINDS: dict[str, str] = {
    "fetch": "Fetch new jobs",
    "mail": "Check mail",
    "embed": "Import pastes & embed",
    "agent_daily": "Daily agent",
    "agent_prepare": "Prepare drafts",
    "neo4j": "Start Neo4j",
}
EXTENSIONS = Path.home() / ".vscode/extensions"


def now() -> str:
    return datetime.now(UTC).isoformat()


def claude_bin() -> Path | None:
    """CLAUDE_BIN from the environment, else `claude` on PATH, else the newest copy bundled
    with the VS Code extension (its path changes with every extension update)."""
    if env := os.environ.get("CLAUDE_BIN"):
        return Path(env)
    if found := shutil.which("claude"):
        return Path(found)

    def version(p: Path) -> tuple[int, ...]:
        part = p.parts[-4].removeprefix("anthropic.claude-code-").split("-")[0]
        return tuple(int(x) for x in part.split(".") if x.isdigit())

    bundled = sorted(EXTENSIONS.glob("anthropic.claude-code-*/resources/native-binary/claude"),
                     key=version)  # fmt: skip
    return bundled[-1] if bundled else None


def claude_command(binary: Path, prompt: str) -> list[str]:
    return [
        str(binary), "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--tools", "",
        "--mcp-config", str(ROOT / ".mcp.json"), "--strict-mcp-config",
        "--allowedTools", "mcp__job-search__*",
        "--permission-mode", "dontAsk",
        "--no-session-persistence",
    ]  # fmt: skip


def command(kind: str, k: int = 10, max_extract: int = 30) -> list[str] | None:
    """The subprocess for a run kind (None: an in-process step, see neo4j_steps)."""
    python = [sys.executable, "-m", "job_search_assistant.daily"]
    if kind == "fetch":
        return python
    if kind == "mail":
        return [*python, "--mail-only"]
    if kind == "embed":
        return [*python, "--skip-ingest"]
    if kind in ("agent_daily", "agent_prepare"):
        binary = claude_bin()
        if binary is None:
            raise ValueError("Claude Code not found: install it or set CLAUDE_BIN in .env")
        prompt = (prompts.daily_run(str(k), str(max_extract)) if kind == "agent_daily"
                  else prompts.prepare_applications())  # fmt: skip
        return claude_command(binary, prompt)
    if kind == "neo4j":
        return None
    raise ValueError(f"unknown run kind {kind!r}")


# ------------------------------------------------------------- Claude's stream-json


def short(value: object, limit: int = 60) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_event(line: str) -> tuple[list[str], str | None]:
    """One stream-json line -> (readable log lines, final report if this is the result)."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return ([line.rstrip()] if line.strip() else []), None
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        tools = [t for t in event.get("tools", []) if t.startswith("mcp__job-search__")]
        return [f"Claude Code started ({event.get('model', '?')}), {len(tools)} job-search "
                "tools"], None  # fmt: skip
    out: list[str] = []
    for block in (event.get("message") or {}).get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            name = block.get("name", "?").removeprefix("mcp__job-search__")
            args = block.get("input") or {}
            brief = ", ".join(f"{k}={short(v, 30)}" for k, v in args.items()
                              if not isinstance(v, (dict, list)))  # fmt: skip
            out.append(f"→ {name}({brief})")
        elif block.get("type") == "tool_result" and block.get("is_error"):
            content = block.get("content")
            text = content if isinstance(content, str) else json.dumps(content)
            out.append(f"✗ {short(text, 200)}")
        elif block.get("type") == "text" and kind == "assistant" and block.get("text"):
            out.append(f"  {short(block['text'], 200)}")
    if kind == "result":
        secs = (event.get("duration_ms") or 0) / 1000
        ok = event.get("subtype") == "success" and not event.get("is_error")
        out.append(f"{'✓ finished' if ok else '✗ ended with an error'} after {secs:.0f} s, "
                   f"{event.get('num_turns', '?')} turns")  # fmt: skip
        return out, event.get("result")
    return out, None


# ------------------------------------------------------------------------- Neo4j


def neo4j_steps(say: Callable[[str], None]) -> int:
    """Start Docker Desktop if it isn't running, then the Neo4j container; wait until up."""
    docker = shutil.which("docker") or "/usr/local/bin/docker"

    def docker_up() -> bool:
        return subprocess.run([docker, "info"], capture_output=True).returncode == 0

    if not docker_up():
        say("Starting Docker Desktop…")
        subprocess.run(["open", "-a", "Docker"], check=False)
        for _ in range(60):
            if docker_up():
                break
            time.sleep(3)
        else:
            say("✗ Docker did not start")
            return 1
    say("docker compose up -d")
    up = subprocess.run([docker, "compose", "up", "-d"], cwd=ROOT, capture_output=True, text=True)
    for line in (up.stdout + up.stderr).splitlines():
        say(line)
    if up.returncode:
        return up.returncode
    from job_search_assistant import graph

    for _ in range(40):
        try:
            graph.connect_neo4j().close()
            say("✓ Neo4j is up")
            return 0
        except Exception:
            time.sleep(3)
    say("✗ Neo4j did not answer")
    return 1


# ----------------------------------------------------------------------- the runner


class Runner:
    """Starts runs in background threads and records them in the `ui_runs` table."""

    def __init__(self, connect: Callable[[], sqlite3.Connection], runs_dir: Path = RUNS):
        self.connect = connect
        self.runs_dir = runs_dir
        self.lock = threading.Lock()
        self.procs: dict[int, subprocess.Popen] = {}
        with self._db() as conn:  # a server restart killed whatever was running
            conn.execute("UPDATE ui_runs SET status = 'interrupted', finished_at = ? "
                         "WHERE status = 'running'", (now(),))  # fmt: skip

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """A short-lived connection; commits on success, always closes."""
        with closing(self.connect()) as conn, conn:
            yield conn

    def log_path(self, run_id: int) -> Path:
        return self.runs_dir / f"{run_id}.log"

    def start(self, kind: str, k: int = 10, max_extract: int = 30) -> int:
        cmd = command(kind, k, max_extract)  # raises ValueError for unknown kinds / no Claude
        with self.lock:
            with self._db() as conn:
                busy = conn.execute(
                    "SELECT id, kind FROM ui_runs WHERE status = 'running'"
                ).fetchone()
                if busy:
                    raise RuntimeError(f"run {busy[0]} ({KINDS[busy[1]]}) is still running")
                run_id = conn.execute(
                    "INSERT INTO ui_runs (kind, args_json, status, started_at) "
                    "VALUES (?, ?, 'running', ?)",
                    (kind, json.dumps({"k": k, "max_extract": max_extract}), now()),
                ).lastrowid
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._run, args=(run_id, kind, cmd), daemon=True).start()
        return run_id

    def _run(self, run_id: int, kind: str, cmd: list[str] | None) -> None:
        report = None
        with self.log_path(run_id).open("w", buffering=1) as log:

            def say(line: str) -> None:
                log.write(line.rstrip("\n") + "\n")

            try:
                if cmd is None:
                    code = neo4j_steps(say)
                else:
                    say("$ " + " ".join(cmd[:3]) + (" …" if len(cmd) > 3 else ""))
                    proc = subprocess.Popen(
                        cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, bufsize=1, start_new_session=True,  # own process group
                    )  # fmt: skip
                    self.procs[run_id] = proc
                    for line in self._lines(proc):
                        if kind.startswith("agent"):
                            rendered, result = render_event(line)
                            report = result or report
                            for r in rendered:
                                say(r)
                        else:
                            say(line)
                    code = proc.wait()
            except Exception as exc:  # never leave a run stuck in "running"
                say(f"✗ {type(exc).__name__}: {exc}")
                code = -1
        self.procs.pop(run_id, None)
        with self._db() as conn:
            current = conn.execute("SELECT status FROM ui_runs WHERE id = ?", (run_id,)).fetchone()
            status = (
                "cancelled" if current[0] == "cancelled" else ("done" if code == 0 else "failed")
            )
            conn.execute(
                "UPDATE ui_runs SET status = ?, exit_code = ?, finished_at = ?, report = ? "
                "WHERE id = ?",
                (status, code, now(), report, run_id),
            )

    @staticmethod
    def _lines(proc: subprocess.Popen) -> Iterator[str]:
        assert proc.stdout is not None
        yield from proc.stdout

    def cancel(self, run_id: int) -> bool:
        proc = self.procs.get(run_id)
        with self._db() as conn:
            conn.execute("UPDATE ui_runs SET status = 'cancelled' WHERE id = ? "
                         "AND status = 'running'", (run_id,))  # fmt: skip
        if proc is None:
            return False
        os.killpg(proc.pid, signal.SIGTERM)  # the whole group: claude and its MCP server
        return True

    def log(self, run_id: int, offset: int = 0) -> tuple[list[str], int]:
        """Log lines from `offset` on, and the offset to ask for next time."""
        path = self.log_path(run_id)
        if not path.exists():
            return [], offset
        lines = path.read_text(errors="replace").splitlines()
        return lines[offset:], len(lines)
