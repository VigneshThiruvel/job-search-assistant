"""Phase 3: embed postings for semantic retrieval.

    python -m job_search_assistant.embed     # embed new or changed postings (local model)

Runs a local multilingual model (no API): bge-m3 maps German and English text
into the same vector space, so "Datenmodellierung" and "data modeling" land
close together. One vector per posting, L2-normalized, stored in the
`embeddings` table; a posting is re-embedded only when its text hash changes.

Only postings ranking can use are embedded (`worth_embedding`): my role titles, not
senior or student, in scope, with a full description from a source we extract. That was
~5% of the corpus on 2026-10-28; vectors of postings outside it are deleted.
"""

import argparse
import hashlib
import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-m3"
MAX_TOKENS = 2048  # covers ~all postings (p90 1.6k tokens); 4 postings/s on an M3 Pro
BATCH_SIZE = 16

# texts -> (n, dim) float array, L2-normalized
Encoder = Callable[[Sequence[str]], np.ndarray]


def posting_text(job: dict[str, Any]) -> str:
    """What gets embedded: the same fields the extractor sees."""
    return f"{job['title']}\n{job['company']}, {job['location'] or ''}\n\n{job['description']}"


def worth_embedding(job: dict[str, Any]) -> bool:
    """The same postings extraction and ranking can use (Adzuna snippets never are)."""
    from job_search_assistant import evalset, inbox

    return (
        job["source"] in evalset.TARGET_SOURCES
        and len(job["description"] or "") >= inbox.FULL_TEXT
        and inbox.relevant_title(job["title"], job["location"])
    )


def pending(conn: sqlite3.Connection, model: str) -> tuple[list[tuple[int, str, str]], set[int]]:
    """(postings to embed as (id, text, hash), ids of stored vectors no longer in scope)."""
    known = dict(conn.execute("SELECT job_id, text_hash FROM embeddings WHERE model = ?", (model,)))
    todo, keep = [], set()
    for row in conn.execute("SELECT * FROM jobs WHERE description != '' ORDER BY id"):
        job = dict(row)
        if not worth_embedding(job):
            continue
        keep.add(job["id"])
        text = posting_text(job)
        if known.get(job["id"]) != (h := text_hash(text)):
            todo.append((job["id"], text, h))
    return todo, set(known) - keep


def prune(conn: sqlite3.Connection, model: str) -> int:
    """Delete vectors of postings no longer worth embedding (no model needed); returns count."""
    _, stale = pending(conn, model)
    with conn:
        conn.executemany("DELETE FROM embeddings WHERE job_id = ? AND model = ?",
                         [(i, model) for i in stale])  # fmt: skip
    if stale:
        log.info("deleted %d vectors of postings outside the embedding scope", len(stale))
    return len(stale)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def to_blob(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def load_encoder(model: str = DEFAULT_MODEL) -> Encoder:
    import torch
    from sentence_transformers import SentenceTransformer

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    st = SentenceTransformer(model, device=device)
    st.max_seq_length = MAX_TOKENS

    def encode(texts: Sequence[str]) -> np.ndarray:
        return st.encode(
            list(texts), batch_size=BATCH_SIZE, normalize_embeddings=True, show_progress_bar=False
        )

    return encode


def embed_jobs(
    conn: sqlite3.Connection,
    encode: Encoder,
    model: str,
    batch_size: int = 64,
    duty_cycle: tuple[float, float] | None = None,  # (work, rest) seconds
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Embed every posting worth it whose text is new or changed; returns count.

    Deletes vectors of postings no longer in scope first. Commits per batch, so an
    interrupted run keeps its progress. With a duty cycle, pauses for `rest` seconds
    after every `work` seconds of encoding (lets the laptop cool down).
    """
    prune(conn, model)
    todo, _ = pending(conn, model)
    log.info("%d postings to embed with %s", len(todo), model)

    window_start = clock()
    for start in range(0, len(todo), batch_size):
        batch = todo[start : start + batch_size]
        vectors = encode([text for _, text, _ in batch])
        now = datetime.now(UTC).isoformat()
        with conn:
            conn.executemany(
                """INSERT INTO embeddings (job_id, model, text_hash, embedded_at, vector)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (job_id, model) DO UPDATE SET
                       text_hash = excluded.text_hash,
                       embedded_at = excluded.embedded_at,
                       vector = excluded.vector""",
                [
                    (i, model, h, now, to_blob(v))
                    for (i, _, h), v in zip(batch, vectors, strict=True)
                ],
            )
        done = min(start + batch_size, len(todo))
        log.info("embedded %d/%d", done, len(todo))
        if duty_cycle and done < len(todo) and clock() - window_start >= duty_cycle[0]:
            log.info("cooling down for %.0f s", duty_cycle[1])
            sleep(duty_cycle[1])
            window_start = clock()
    return len(todo)


def main() -> None:
    from job_search_assistant import db

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m job_search_assistant.embed")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--work-minutes", type=float, help="encode this long, then rest")
    parser.add_argument("--rest-minutes", type=float, default=10)
    args = parser.parse_args()
    duty = (args.work_minutes * 60, args.rest_minutes * 60) if args.work_minutes else None
    n = embed_jobs(
        db.connect("data/jobs.db"), load_encoder(args.model), args.model, duty_cycle=duty
    )
    print(f"embedded {n} postings")


if __name__ == "__main__":
    main()
