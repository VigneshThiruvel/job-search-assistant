"""Read-only IMAP access to job-alert emails, cached as .eml files."""

import imaplib
import logging
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

GMAIL_HOST = "imap.gmail.com"
# Gmail's "All Mail" catches alerts even if they were archived or labelled.
GMAIL_ALL_MAIL = '"[Gmail]/All Mail"'


class Mailbox:
    """Yields raw emails from one sender, never modifying the mailbox.

    The cache key is UIDVALIDITY + UID: IMAP guarantees a UID never points to a
    different message while UIDVALIDITY stays the same.
    """

    def __init__(
        self,
        address: str,
        password: str,
        connect: Callable[[], imaplib.IMAP4] = lambda: imaplib.IMAP4_SSL(GMAIL_HOST),
        folder: str = GMAIL_ALL_MAIL,
    ) -> None:
        self.address = address
        self.password = password.replace(" ", "")  # app passwords are shown with spaces
        self.connect = connect
        self.folder = folder

    def messages(self, sender: str, since: date, cache_dir: Path) -> Iterator[bytes]:
        imap = self.connect()
        try:
            imap.login(self.address, self.password)
            typ, _ = imap.select(self.folder, readonly=True)  # readonly: nothing gets marked read
            if typ != "OK":
                raise RuntimeError(f"cannot open mail folder {self.folder}")
            uidvalidity = imap.response("UIDVALIDITY")[1][0].decode()
            _, data = imap.uid("search", None, f'(FROM "{sender}" SINCE {_imap_date(since)})')
            uids = data[0].split()
            log.info("mailbox %s since %s: %d emails", sender, since, len(uids))
            for uid in uids:
                path = cache_dir / f"{uidvalidity}-{uid.decode()}.eml"
                if not path.exists():
                    # BODY.PEEK (unlike BODY) does not set the \Seen flag.
                    _, parts = imap.uid("fetch", uid, "(BODY.PEEK[])")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(parts[0][1])
                yield path.read_bytes()
        finally:
            try:
                imap.logout()
            except (imaplib.IMAP4.error, OSError):
                pass


def since_days_ago(days: int, today: date | None = None) -> date:
    return (today or date.today()) - timedelta(days=days)


def _imap_date(d: date) -> str:
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return f"{d.day:02d}-{months[d.month - 1]}-{d.year}"  # locale-independent "15-Oct-2026"
