"""Phase 7b: application PDFs in my documents folder, not the browser's Downloads.

    ~/Documents/full_time/resume/thiruvel_selvam_resume.pdf              # what I send now
    ~/Documents/full_time/cover_letter/thiruvel_selvam_cover_letter.pdf
    ~/Documents/full_time/resume/dexter-health_applied-ai-engineer.pdf   # after "I applied"

The working name is what a recruiter sees. Once I applied, the file is renamed after the
job, so the next save can't overwrite what I sent. `saved_files` remembers which job each
file belongs to (with its hash): "I applied" renames only this job's file; if the working
file was overwritten by another job since, this job's PDF is rebuilt from its stored draft.

APPLICATION_DIR (folder) and DOCUMENT_PREFIX (e.g. thiruvel_selvam) come from .env.
The server writes these files itself (the browser can't choose a download folder).
"""

import hashlib
import os
import re
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from job_search_assistant import applications, tailor
from job_search_assistant.text import _GENDER_MARKER

KINDS = ("resume", "cover_letter")


def root() -> Path:
    from dotenv import load_dotenv

    load_dotenv()
    return Path(os.environ.get("APPLICATION_DIR") or "~/Documents/full_time").expanduser()


def working_name(kind: str) -> str:
    from dotenv import load_dotenv

    load_dotenv()
    prefix = os.environ.get("DOCUMENT_PREFIX", "").strip()
    return f"{prefix}_{kind}.pdf" if prefix else f"{kind}.pdf"


def slug(text: str, size: int = 50) -> str:
    text = _GENDER_MARKER.sub(" ", text.lower())
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:size].strip("-") or "job"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def letter_date(at: datetime) -> str:
    return f"{at.day} {at:%B %Y}"  # 28 October 2026


def build(
    conn: sqlite3.Connection, job_id: int, kind: str, resume_tex: str,
    drafts: Path = applications.DRAFTS, date: datetime | None = None,
) -> Path:  # fmt: skip
    """Compile the job's current draft of `kind` under data/drafts/; returns the PDF."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    i = applications.item(conn, job_id)
    base = applications.draft_dir(i, drafts)
    if kind == "resume":
        if not i["resume_json"]:
            raise ValueError(f"job {job_id} has no tailored résumé yet")
        tailored = tailor.TailoredResume.model_validate_json(i["resume_json"])
        tex = base / "resume.tex"
        tex.write_text(tailor.render(tailor.parse(resume_tex), tailored))
    else:
        if not i["letter"]:
            raise ValueError(f"job {job_id} has no cover letter")
        tex = base / "cover_letter.tex"
        day = (date or datetime.now()).astimezone()
        tex.write_text(tailor.render_letter(resume_tex, i["letter"], letter_date(day)))
    return tailor.compile_pdf(tex)[0]


def final_path(conn: sqlite3.Connection, job_id: int, kind: str, folder: Path) -> Path:
    """<company>_<title>.pdf; the job id is appended if another job already has that name."""
    i = applications.item(conn, job_id)
    path = folder / kind / f"{slug(i['company'], 40)}_{slug(i['title'])}.pdf"
    owner = conn.execute("SELECT job_id FROM saved_files WHERE path = ?", (str(path),)).fetchone()
    if path.exists() and (owner is None or owner[0] != job_id):
        path = path.with_name(f"{path.stem}_{job_id}.pdf")
    return path


def _record(conn: sqlite3.Connection, job_id: int, kind: str, path: Path) -> None:
    with conn:
        conn.execute(
            """INSERT INTO saved_files (job_id, kind, path, sha256, saved_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (job_id, kind) DO UPDATE SET path = excluded.path,
                   sha256 = excluded.sha256, saved_at = excluded.saved_at""",
            (job_id, kind, str(path), sha256(path), datetime.now(UTC).isoformat()),
        )


def save(
    conn: sqlite3.Connection, job_id: int, kind: str, resume_tex: str | None = None,
    drafts: Path = applications.DRAFTS, folder: Path | None = None,
) -> Path:  # fmt: skip
    """Build the PDF and put it in my folder: the working name before I applied, the job's
    own name after (re-saving an applied job never touches the working file)."""
    folder = folder or root()
    pdf = build(conn, job_id, kind, resume_tex or tailor.TEX.read_text(), drafts)
    applied = applications.status_of(conn, job_id) in applications.OUTCOMES
    dest = final_path(conn, job_id, kind, folder) if applied else folder / kind / working_name(kind)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pdf, dest)
    _record(conn, job_id, kind, dest)
    return dest


def finalize(
    conn: sqlite3.Connection, job_id: int, resume_tex: str | None = None,
    drafts: Path = applications.DRAFTS, folder: Path | None = None,
) -> list[Path]:  # fmt: skip
    """After "I applied": move this job's saved PDFs to their final names; returns them.

    Only files I saved get a final copy. If the working file now holds another job's PDF
    (hash differs), it stays, and this job's PDF is rebuilt from its draft instead.
    """
    folder = folder or root()
    out = []
    rows = conn.execute("SELECT * FROM saved_files WHERE job_id = ?", (job_id,)).fetchall()
    for r in rows:
        current, kind = Path(r["path"]), r["kind"]
        if current.name != working_name(kind) and current.exists():
            continue  # already final (saved after applying)
        dest = final_path(conn, job_id, kind, folder)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if current.exists() and sha256(current) == r["sha256"]:
            current.replace(dest)
        else:
            saved = datetime.fromisoformat(r["saved_at"])
            tex = resume_tex or tailor.TEX.read_text()
            shutil.copyfile(build(conn, job_id, kind, tex, drafts, date=saved), dest)
        _record(conn, job_id, kind, dest)
        out.append(dest)
    return out


def saved(conn: sqlite3.Connection, job_id: int) -> list[dict]:
    """This job's files in my folder: kind, path, whether it's still there."""
    return [
        {"kind": r["kind"], "path": r["path"], "exists": Path(r["path"]).exists()}
        for r in conn.execute(
            "SELECT kind, path FROM saved_files WHERE job_id = ? ORDER BY kind", (job_id,)
        )
    ]
