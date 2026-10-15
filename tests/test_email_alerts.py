from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from job_search_assistant.mailbox import Mailbox
from job_search_assistant.sources.email_alerts import linkedin, xing

# Synthetic emails that mirror the real alert structure; no personal data.
FIXTURES = Path(__file__).parent / "fixtures" / "email"


class FakeIMAP:
    """Just enough of imaplib.IMAP4 for Mailbox; records every command."""

    def __init__(self, messages: dict[bytes, bytes]) -> None:
        self.messages = messages
        self.commands: list[tuple] = []

    def login(self, user, password):
        self.commands.append(("login", user, password))

    def select(self, folder, readonly=False):
        self.commands.append(("select", folder, readonly))
        return "OK", [b"1"]

    def response(self, code):
        return code, [b"42"]

    def uid(self, command, *args):
        self.commands.append(("uid", command, *args))
        if command == "search":
            return "OK", [b" ".join(self.messages)]
        return "OK", [(b"header", self.messages[args[0]])]

    def logout(self):
        self.commands.append(("logout",))


@pytest.fixture
def linkedin_eml() -> bytes:
    return (FIXTURES / "linkedin_alert.eml").read_bytes()


@pytest.fixture
def xing_eml() -> bytes:
    return (FIXTURES / "xing_alert.eml").read_bytes()


def make(factory, imap: FakeIMAP, tmp_path: Path):
    mailbox = Mailbox("me@example.com", "abcd efgh ijkl mnop", connect=lambda: imap)
    return factory(mailbox, tmp_path, today=date(2026, 10, 15))


def test_mailbox_is_read_only_and_cached(linkedin_eml, tmp_path) -> None:
    imap = FakeIMAP({b"7": linkedin_eml})
    source = make(linkedin, imap, tmp_path)
    list(source.fetch())

    assert ("login", "me@example.com", "abcdefghijklmnop") in imap.commands
    assert ("select", '"[Gmail]/All Mail"', True) in imap.commands
    search = next(c for c in imap.commands if c[:2] == ("uid", "search"))
    assert 'FROM "jobalerts-noreply@linkedin.com" SINCE 15-Sep-2026' in search[3]
    fetches = [c for c in imap.commands if c[:2] == ("uid", "fetch")]
    assert fetches == [("uid", "fetch", b"7", "(BODY.PEEK[])")]
    assert (tmp_path / "email" / "linkedin" / "42-7.eml").exists()

    imap.commands.clear()
    list(make(linkedin, imap, tmp_path).fetch())
    assert not [c for c in imap.commands if c[:2] == ("uid", "fetch")]  # served from cache


def test_linkedin_cards(linkedin_eml, tmp_path) -> None:
    source = make(linkedin, FakeIMAP({b"1": linkedin_eml}), tmp_path)
    jobs = [source.normalize(r) for r in source.fetch()]

    assert [(j.title, j.company, j.location) for j in jobs] == [
        ("Junior Data Analyst (m/w/d)", "Example Analytics GmbH", "Berlin, Germany"),
        ("Data Engineer", "Sample Tech AG", "Hamburg"),
        ("ML Engineer", "Demo AI", "Germany"),
    ]
    assert [j.source_job_id for j in jobs] == ["1000000001", "1000000002", "1000000003"]
    assert jobs[0].url == "https://www.linkedin.com/jobs/view/1000000001/"
    assert jobs[0].posted_at == datetime(2026, 10, 15, 8, 37, 11, tzinfo=UTC)
    assert jobs[2].raw["card"]["badges"] == "Apply with resume & profile"


def test_linkedin_never_stores_sign_in_tokens(linkedin_eml, tmp_path) -> None:
    source = make(linkedin, FakeIMAP({b"1": linkedin_eml}), tmp_path)
    for raw in source.fetch():
        assert "otpToken" not in str(raw)


def test_xing_cards(xing_eml, tmp_path) -> None:
    source = make(xing, FakeIMAP({b"1": xing_eml}), tmp_path)
    records = list(source.fetch())
    jobs = [source.normalize(r) for r in records]

    assert [(j.title, j.company, j.location) for j in jobs] == [
        ("(Junior) Data Engineer (m/w/d) DWH", "Example Data GmbH", "Berlin"),
        ("Junior BI & Data Solution Engineer (m/w/d)", "Sample Group", "Erlangen"),
    ]
    first = records[0]["card"]
    assert (first["employment"], first["salary"]) == ("Full-time", "46,000 € - 62,500 €")
    assert first["badges"] == "Be an early applicant"
    assert records[1]["card"]["badges"] == ""
    assert jobs[0].url == "https://www.xing.com/m/jobTOKEN1"


def test_xing_id_is_stable_across_emails(xing_eml, tmp_path) -> None:
    # Same job, different email and redirect token -> same source_job_id.
    other = xing_eml.replace(b"jobTOKEN1", b"differentTOKEN")
    source = make(xing, FakeIMAP({b"1": xing_eml, b"2": other}), tmp_path)
    ids = [source.normalize(r).source_job_id for r in source.fetch()]
    assert ids[0] == ids[2] and ids[1] == ids[3]
