"""The interface every source adapter implements."""

from collections.abc import Iterator
from typing import Any, Protocol

from job_search_assistant.models import Job


class Source(Protocol):
    """Any class with these members is a Source -- no inheritance needed."""

    name: str

    def fetch(self) -> Iterator[dict[str, Any]]:
        """Yield raw job records exactly as the source returns them."""
        ...

    def normalize(self, raw: dict[str, Any]) -> Job:
        """Turn one raw record into a Job."""
        ...
