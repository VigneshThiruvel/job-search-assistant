"""Source-independent data model shared by all adapters."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Job:
    """One posting, normalized from any source.

    DB-managed fields (id, first_seen, last_seen) and derived fields (lang,
    content_hash) are added at ingest time, not by adapters.
    """

    source: str
    source_job_id: str
    url: str
    title: str
    company: str
    location: str | None
    country: str | None
    remote: bool | None
    description: str
    posted_at: datetime | None
    raw: dict[str, Any] = field(repr=False)
