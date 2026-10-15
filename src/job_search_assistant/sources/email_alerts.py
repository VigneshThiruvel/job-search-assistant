"""LinkedIn and XING postings, parsed from job-alert emails in my own inbox.

Hard rule: links in these emails are never requested -- that would be
automating LinkedIn/XING. Postings have title, company and location only.
"""

import email
import html
import logging
import re
from collections.abc import Callable, Iterator
from datetime import UTC, date
from email import policy
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from job_search_assistant.mailbox import Mailbox, since_days_ago
from job_search_assistant.models import Job
from job_search_assistant.text import content_hash

log = logging.getLogger(__name__)

Card = dict[str, str]


def parse_linkedin(text: str) -> list[Card]:
    """Cards are separated by dashed lines; each is title/company/location
    (+ optional badge lines) followed by "View job: <url>"."""
    cards = []
    for block in re.split(r"\n-{20,}\n", text):
        match = re.search(r"View job:\s*(https?://\S*/jobs/view/(\d+)\S*)", block)
        if not match:
            continue
        for paragraph in re.split(r"\n\s*\n", block[: match.start()]):
            lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
            if len(lines) >= 3 and not re.search(r"job alert|new jobs? match", paragraph, re.I):
                job_id = match.group(2)
                cards.append(
                    {
                        "job_id": job_id,
                        "title": lines[0],
                        "company": lines[1],
                        "location": lines[2],
                        "badges": "; ".join(lines[3:]),
                        # Canonical link only: the emailed one carries a sign-in token.
                        "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
                    }
                )
                break
    return cards


def parse_xing(text: str) -> list[Card]:
    """Cards are separated by dashed lines: [badge] / title / "=> <url>" /
    company / location / [employment type + salary, run together]."""
    cards = []
    for block in re.split(r"\n-{20,}\n", text):
        lines = [html.unescape(line.strip()) for line in block.splitlines()]
        link = next((i for i, line in enumerate(lines) if line.startswith("=> http")), None)
        if link is None or link == 0:
            continue
        after = [line for line in lines[link + 1 :] if line]
        if len(after) < 2:  # "Show all results" / survey links have no company+location
            continue
        # Badges are the lines directly above the title, up to the first blank line.
        badges = []
        for line in reversed(lines[: link - 1]):
            if not line:
                break
            badges.insert(0, line)
        # "Full-time46,000 € - 62,500 €" -> employment type + salary.
        employment, salary = re.match(r"([^\d]*)(.*)", after[2] if len(after) > 2 else "").groups()
        cards.append(
            {
                "title": lines[link - 1],
                "company": after[0],
                "location": after[1],
                "employment": employment.strip(),
                "salary": salary.strip(),
                "badges": "; ".join(badges),
                # Per-email redirect token; stored for manual clicking only.
                "url": lines[link][3:].strip(),
            }
        )
    return cards


class EmailAlertSource:
    """One alert sender in my mailbox; each email yields several job cards."""

    def __init__(
        self,
        name: str,
        sender: str,
        parse: Callable[[str], list[Card]],
        mailbox: Mailbox,
        raw_dir: Path,
        days: int = 30,
        today: date | None = None,
    ) -> None:
        self.name = name
        self.sender = sender
        self.parse = parse
        self.mailbox = mailbox
        self.since = since_days_ago(days, today)
        self.cache_dir = raw_dir / "email" / name

    def fetch(self) -> Iterator[dict[str, Any]]:
        for raw in self.mailbox.messages(self.sender, self.since, self.cache_dir):
            msg = email.message_from_bytes(raw, policy=policy.default)
            body = msg.get_body(preferencelist=("plain",))
            if not isinstance(body, EmailMessage):
                log.warning("%s: email without text part: %s", self.name, msg["Subject"])
                continue
            # Some alerts use CRLF line endings; parsers expect "\n" only.
            cards = self.parse(body.get_content().replace("\r\n", "\n"))
            if not cards:
                log.warning("%s: no jobs parsed from %r", self.name, msg["Subject"])
            for card in cards:
                yield {
                    "email": {
                        "message_id": msg["Message-ID"],
                        "date": msg["Date"],
                        "subject": msg["Subject"],
                    },
                    "card": card,
                }

    def normalize(self, raw: dict[str, Any]) -> Job:
        card = raw["card"]
        # XING has no stable id in its emails, so identity is the content itself.
        job_id = (
            card.get("job_id")
            or content_hash(card["title"], card["company"], card["location"])[:16]
        )
        sent = raw["email"]["date"]
        return Job(
            source=self.name,
            source_job_id=job_id,
            url=card["url"],
            title=card["title"],
            company=card["company"],
            location=card["location"],
            country=None,
            remote=None,
            description="",
            posted_at=parsedate_to_datetime(sent).astimezone(UTC) if sent else None,
            raw=raw,
        )


def linkedin(mailbox: Mailbox, raw_dir: Path, **kwargs: Any) -> EmailAlertSource:
    return EmailAlertSource(
        "linkedin", "jobalerts-noreply@linkedin.com", parse_linkedin, mailbox, raw_dir, **kwargs
    )


def xing(mailbox: Mailbox, raw_dir: Path, **kwargs: Any) -> EmailAlertSource:
    return EmailAlertSource("xing", "jobs@mail.xing.com", parse_xing, mailbox, raw_dir, **kwargs)
